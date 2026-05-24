"""
Agent 循环 (Agent Loop) - nanobot 的核心处理引擎 (Core Processing Engine)

本模块实现了 Agent 的事件驱动状态机，是整个框架的中枢神经系统。

核心职责：
1. 从消息总线接收用户消息
2. 构建完整的上下文（系统提示 + 历史 + 记忆 + 技能）
3. 调用 LLM 获取响应
4. 执行工具调用循环
5. 将结果返回给调用方

架构设计：
- 采用有限状态机 (FSM) 模式处理消息流转
- 状态序列: RESTORE -> COMPACT -> COMMAND -> BUILD -> RUN -> SAVE -> RESPOND -> DONE
- 支持并发会话处理（每个会话独立锁）
- 支持消息中途注入（turn injection）实现流式交互
- 集成 MCP (Model Context Protocol) 工具服务器

关键组件：
- MessageBus: 异步消息队列，解耦通道和 Agent
- SessionManager: 会话持久化与管理
- ToolRegistry: 工具注册与调度
- Consolidator: 长对话记忆压缩
- SubagentManager: 子 Agent 管理器

线程安全说明：
- 每个会话有独立的 asyncio.Lock 保证串行处理
- 使用 asyncio.Semaphore 控制全局并发数
- 文件状态通过 contextvars 实现会话隔离
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from contextlib import AsyncExitStack, nullcontext, suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent import model_presets as preset_helpers
from nanobot.agent.autocompact import AutoCompact
from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, CompositeHook
from nanobot.agent.memory import Consolidator, Dream
from nanobot.agent.progress_hook import AgentProgressHook
from nanobot.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.self import MyTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.cli_apps import utils as cli_app_utils
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands
from nanobot.config.schema import AgentDefaults, ModelPresetConfig
from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import ProviderSnapshot
from nanobot.session.goal_state import (
    runner_wall_llm_timeout_s,
)
from nanobot.session.manager import Session, SessionManager
from nanobot.session.webui_turns import (
    WebuiTurnCoordinator,
    build_bus_progress_callback,
    mark_webui_session,
)
from nanobot.utils.document import extract_documents
from nanobot.utils.helpers import image_placeholder_text
from nanobot.utils.helpers import truncate_text as truncate_text_fn
from nanobot.utils.image_generation_intent import image_generation_prompt
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

if TYPE_CHECKING:
    from nanobot.config.schema import (
        ChannelsConfig,
        ProviderConfig,
        ToolsConfig,
    )
    from nanobot.cron.service import CronService


# 统一会话模式下的默认会话标识
# 当 unified_session 开启时，所有通道共享此会话
UNIFIED_SESSION_KEY = "unified:default"

class TurnState(Enum):
    """
    Agent 处理回合的状态枚举 (Turn State Machine)

    定义消息处理的完整生命周期状态，采用有限状态机模式流转：

    状态流转图：
        RESTORE ──ok──> COMPACT ──ok──> COMMAND
                                              │
                              ┌─shortcut──────┤
                              │               │ dispatch
                              ▼               ▼
                            BUILD <──────────┘
                              │ ok
                              ▼
                             RUN ──ok──> SAVE ──ok──> RESPOND ──ok──> DONE

    各状态说明:
    - RESTORE:   恢复中断的回合（检查点恢复、待处理用户消息）
    - COMPACT:   会话压缩（清理过期会话、触发记忆合并）
    - COMMAND:   命令解析（处理 /new, /stop 等斜杠命令）
    - BUILD:     构建上下文（组装系统提示 + 历史消息 + 工具定义）
    - RUN:       运行 Agent 循环（调用 LLM + 执行工具调用）
    - SAVE:      保存结果（持久化消息到会话文件）
    - RESPOND:   组装响应（生成 OutboundMessage 发送到总线）
    - DONE:      回合完成（清理资源，等待下一条消息）
    """
    RESTORE = auto()     # 恢复状态：加载检查点和中途用户消息
    COMPACT = auto()     # 压缩状态：处理过期会话和记忆整理
    COMMAND = auto()     # 命令状态：检测和处理斜杠命令
    BUILD = auto()       # 构建状态：准备 LLM 调用的完整上下文
    RUN = auto()         # 运行状态：执行核心 Agent 循环（LLM + 工具）
    SAVE = auto()        # 保存状态：将本轮结果持久化到会话
    RESPOND = auto()     # 响应状态：组装最终回复消息
    DONE = auto()        # 完成状态：清理并结束当前回合


@dataclass
class StateTraceEntry:
    """
    状态追踪条目 (State Trace Entry)

    记录单个状态处理过程的详细信息，用于调试和性能分析。

    Attributes:
        state: 当前状态 (TurnState 枚举值)
        started_at: 状态开始处理的时间戳 (time.perf_counter)
        duration_ms: 该状态的执行耗时（毫秒）
        event: 触发状态转换的事件名称（如 "ok", "dispatch", "shortcut"）
        error: 如果该状态处理出错，记录错误信息；否则为 None
    """
    state: TurnState           # 当前所处状态
    started_at: float          # 开始时间 (perf_counter)
    duration_ms: float         # 执行耗时 (毫秒)
    event: str                 # 触发的转换事件
    error: str | None = None   # 错误信息（如果有）


@dataclass
class TurnContext:
    """
    回合上下文 (Turn Context)

    存储单次消息处理的完整运行时状态，在各个状态处理器之间传递。

    这是 Agent 处理一条用户消息时的"工作内存"，包含了从接收到回复的
    全过程数据。每个字段都有明确的用途和生命周期：

    核心字段:
        - msg: 原始入站消息（不可变）
        - session_key: 会话标识符，用于路由和持久化
        - state: 当前 FSM 状态

    消息历史:
        - history: 从会话加载的历史消息列表
        - initial_messages: 发送给 LLM 的完整消息数组（包含 system prompt）

    结果字段:
        - final_content: LLM 的最终文本回复
        - tools_used: 本轮调用的工具名称列表
        - all_messages: 完整的消息交互记录（含工具调用）
        - stop_reason: LLM 停止原因 ("stop", "end_turn", "max_iterations" 等)

    流式回调:
        - on_progress: 进度更新回调（工具调用提示等）
        - on_stream: 文本流式输出回调（每个 delta 调用）
        - on_stream_end: 流式段落结束回调
        - on_retry_wait: 重试等待通知回调

    中途注入:
        - pending_queue: 待注入的消息队列（支持 turn 内接收新消息）
        - pending_summary: 待注入的消息摘要（来自压缩）
    """
    msg: InboundMessage                                    # 触发本轮的用户消息
    session_key: str                                       # 会话标识 (如 "telegram:12345")
    state: TurnState                                       # 当前 FSM 状态
    turn_id: str                                           # 唯一的回合 ID (用于日志追踪)
    session: Session | None = None                         # 会话对象（RESTORE 阶段加载）

    # ===== 消息相关 =====
    history: list[dict[str, Any]] = field(default_factory=list)      # 原始历史消息
    initial_messages: list[dict[str, Any]] = field(default_factory=list)  # LLM 输入消息

    # ===== 运行结果 =====
    final_content: str | None = None                      # 最终文本回复内容
    tools_used: list[str] = field(default_factory=list)   # 调用的工具名列表
    all_messages: list[dict[str, Any]] = field(default_factory=list)  # 完整消息记录
    stop_reason: str = ""                                 # LLM 停止原因
    had_injections: bool = False                          # 是否有中途注入消息

    # ===== 持久化控制 =====
    user_persisted_early: bool = False                    # 用户消息是否提前持久化
    save_skip: int = 0                                    # 保存时跳过的消息数

    # ===== 输出 =====
    outbound: OutboundMessage | None = None               # 最终出站消息

    # ===== 回调函数 =====
    on_progress: Callable[..., Awaitable[None]] | None = None     # 进度回调
    on_stream: Callable[[str], Awaitable[None]] | None = None      # 流式文本回调
    on_stream_end: Callable[..., Awaitable[None]] | None = None    # 流式结束回调
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None  # 重试等待回调

    # ===== 中途注入 =====
    pending_queue: asyncio.Queue | None = None            # 待注入消息队列
    pending_summary: str | None = None                    # 压缩摘要

    # ===== 性能监控 =====
    turn_wall_started_at: float = field(default_factory=time.time)  # 回合开始时间
    turn_latency_ms: int | None = None                    # 总延迟（毫秒）

    # ===== 调试追踪 =====
    trace: list[StateTraceEntry] = field(default_factory=list)  # 状态转换轨迹


class AgentLoop:
    """
    Agent 循环 (Agent Loop) - 核心处理引擎

    这是 nanobot 框架的中枢组件，负责：
    1. 从消息总线 (MessageBus) 接收用户消息
    2. 使用状态机模式处理每条消息（RESTORE -> COMPACT -> ... -> DONE）
    3. 构建完整的 LLM 上下文（系统提示 + 历史 + 记忆 + 技能）
    4. 调用大语言模型获取响应
    5. 执行工具调用循环（最多 max_iterations 次）
    6. 将响应发送回消息总线

    架构特点：
    - 异步设计：基于 asyncio，支持高并发
    - 会话隔离：每个会话独立处理，互不干扰
    - 可扩展：通过钩子 (AgentHook) 机制支持自定义行为
    - 容错：支持检查点恢复、消息重试、优雅降级

    典型工作流程示例::

        # 创建 AgentLoop
        loop = AgentLoop.from_config(config)

        # 启动后台任务处理消息
        task = asyncio.create_task(loop.run())

        # 发布一条消息
        await loop.bus.publish_inbound(InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="你好"
        ))

        # 等待并获取响应
        response = await loop.bus.consume_outbound()
        print(response.content)  # "你好！有什么可以帮你的吗？"

    配置项说明：
        - model: 使用的模型名称（如 "anthropic/claude-sonnet-4"）
        - max_iterations: 单轮最大工具调用次数（默认 200）
        - context_window_tokens: 上下文窗口大小（默认 65536）
        - workspace: 工作目录路径（存储会话、记忆等数据）
    """

    # ----------------------------------------------------------------
    # 只读属性 (Read-only Properties)
    # ----------------------------------------------------------------
    @property
    def current_iteration(self) -> int:
        """返回当前回合中 Agent 已经执行了多少次工具调用（iteration 计数）
        由 AgentProgressHook 在每次迭代时更新，主要用于 MyTool 等工具
        向用户展示当前的循环进度。"""
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        """返回当前注册的所有工具名称列表。
        比如：['read', 'write', 'bash', 'web_search', ...]
        外部可以通过 loop.tool_names 快速查看有哪些可用工具。"""
        return self.tools.tool_names

    # ----------------------------------------------------------------
    # LLM 运行时状态获取
    # ----------------------------------------------------------------
    def llm_runtime(self) -> LLMRuntime:
        """获取当前 LLM 运行时状态的快照——返回一个 LLMRuntime 对象，包含
        provider（如 Anthropic/OpenRouter）和 model（如 claude-sonnet-4）。

        调用前会先执行 _refresh_provider_snapshot() 确保配置是最新的，
        这样如果用户在运行时通过 /model 切换了模型，这里能拿到最新值。

        主要用于 WebUI 标题生成等需要知道"当前用的是哪个模型"的场景。"""
        self._refresh_provider_snapshot()
        return LLMRuntime(self.provider, self.model)

    # ----------------------------------------------------------------
    # 状态转换表 (State Transition Table)
    # 当前状态 + 发生事件 -> 下一状态 的哈希映射。
    # 例如：(RESTORE, "ok") -> COMPACT 表示 RESTORE 状态处理成功
    # 后进入 COMPACT 状态。
    # ----------------------------------------------------------------
    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        image_generation_provider_config: ProviderConfig | None = None,
        image_generation_provider_configs: dict[str, ProviderConfig] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
    ):
        from nanobot.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader
        self._runtime_model_publisher = runtime_model_publisher
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(provider_signature)
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self._image_generation_provider_configs = dict(image_generation_provider_configs or {})
        if (
            image_generation_provider_config is not None
            and "openrouter" not in self._image_generation_provider_configs
        ):
            self._image_generation_provider_configs["openrouter"] = image_generation_provider_config
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._pending_turn_latency_ms: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self._webui_turns = WebuiTurnCoordinator(
            bus=self.bus,
            sessions=self.sessions,
            schedule_background=lambda coro: self._schedule_background(coro),
        )
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # NANOBOT_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=self.model,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    # ----------------------------------------------------------------
    # 工厂方法 (Factory Method)
    # ----------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """从配置对象创建 AgentLoop 实例的工厂方法。

        这是推荐的创建方式——从 nanobot config 中自动解析所有参数：
        - provider: 从 config 创建 LLMProvider
        - model: 从 config 解析模型预设
        - max_iterations, context_window_tokens 等: 从 config.agents.defaults 读取
        
        extra 参数会直接传递给 __init__，允许调用方覆盖配置。"""
        from nanobot.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop("preset_snapshot_loader", None) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            disabled_skills=defaults.disabled_skills,
            session_ttl_minutes=defaults.session_ttl_minutes,
            consolidation_ratio=defaults.consolidation_ratio,
            max_messages=defaults.max_messages,
            tools_config=config.tools,
            model_presets=preset_helpers.configured_model_presets(config),
            model_preset=defaults.model_preset,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
            **extra,
        )

    def _sync_subagent_runtime_limits(self) -> None:
        """将 AgentLoop 当前的最大迭代次数同步给 SubagentManager，
        确保子 Agent 与主 Agent 使用相同的 max_iterations 限制。

        在每次调用 _run_agent_loop() 前执行，因为 max_iterations 可能在
        运行时被修改（比如通过 /my set max_iterations 200 动态调整）。"""
        self.subagents.max_iterations = self.max_iterations

    # ----------------------------------------------------------------
    # 模型/Provider 热切换 (Hot-swap Model & Provider)
    # ----------------------------------------------------------------
    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """将一个新的 ProviderSnapshot 应用到 AgentLoop 的所有依赖组件上，
        实现在不重启服务的情况下切换 LLM 模型/provider。

        会同时更新以下组件的 provider/model：
        - self.runner（AgentRunner，负责实际调用 LLM）
        - self.subagents（SubagentManager，子 Agent 管理器）
        - self.consolidator（Consolidator，记忆压缩器）
        - self.dream（Dream，记忆梦境生成器）

        publish_update=True 时会通过 runtime_model_publisher 回调通知
        外部（如 WebUI）模型已变更。"""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self.dream.set_provider(provider, model)
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self) -> None:
        """检查配置文件中的 provider 是否有变更，如果有则热切换到新配置。
        
        工作流程：
        1. 通过 provider_snapshot_loader 加载最新的 ProviderSnapshot
        2. 如果有激活的 model_preset，尝试刷新预设快照
        3. 如果没有变更（signature 一致），直接返回
        4. 如果有变更，调用 _apply_provider_snapshot 执行热切换
        
        在每个 turn 开始时被调用，确保每次 LLM 调用都使用最新配置。"""
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(snapshot.signature)
        if self._active_preset and self._default_selection_signature in (None, default_selection):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(snapshot.signature)
        self._apply_provider_snapshot(snapshot)

    # ----------------------------------------------------------------
    # 模型预设管理 (Model Preset Management)
    # ----------------------------------------------------------------
    @property
    def model_preset(self) -> str | None:
        """返回当前激活的模型预设名称（如 "fast"、"smart" 等），没有则返回 None。"""
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        """设置模型预设的便捷语法：loop.model_preset = "fast" """
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        """根据预定义的模型预设名称，解析出对应的 ProviderSnapshot。
        预设包含了 provider、model、context_window_tokens 等完整配置。"""
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """根据名称切换模型预设（如 "fast"、"smart"、"balanced"）。
        
        1. 解析预设名（支持别名和空值标准化）
        2. 构建对应的 ProviderSnapshot
        3. 调用 _apply_provider_snapshot 热切换所有组件"""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name

    # ----------------------------------------------------------------
    # 工具注册 (Tool Registration)
    # ----------------------------------------------------------------
    def _register_default_tools(self) -> None:
        """通过 ToolLoader 插件系统加载并注册所有默认工具。

        步骤：
        1. 构建 ToolContext（包含 workspace、bus、session 等上下文信息）
        2. 使用 ToolLoader 扫描并注册内置工具（如 read、write、bash 等）
        3. 单独注册 MyTool（需要直接引用 AgentLoop 的运行时状态）

        注册完成后，所有工具可通过 self.tools 访问，用于后续 LLM 工具调用。"""
        from nanobot.agent.tools.context import ToolContext
        from nanobot.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            image_generation_provider_configs=self._image_generation_provider_configs,
            timezone=self.context.timezone or "UTC",
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        logger.info("Registered {} tools: {}", len(registered), registered)

    # ----------------------------------------------------------------
    # MCP 服务器连接 (Model Context Protocol)
    # ----------------------------------------------------------------
    async def _connect_mcp(self) -> None:
        """懒加载方式连接配置的 MCP (Model Context Protocol) 服务器。

        - 只在第一次被调用时连接，后续调用直接返回
        - 连接成功后，MCP 服务器提供的工具会被注册到 self.tools
        - 连接失败不会崩，下次消息到来时自动重试
        
        MCP 允许外部工具服务器（如数据库、API 服务）以标准化协议
        向 Agent 暴露工具能力。"""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
            if self._mcp_stacks:
                self._mcp_connected = True
            else:
                logger.warning("No MCP servers connected successfully (will retry next message)")
        except asyncio.CancelledError:
            logger.warning("MCP connection cancelled (will retry next message)")
            self._mcp_stacks.clear()
        except BaseException as e:
            logger.warning("Failed to connect MCP servers (will retry next message): {}", e)
            self._mcp_stacks.clear()
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """为所有 ContextAware 工具设置当前请求的上下文信息。

        在每个 turn 开始前调用，把当前的消息通道、会话 ID 等信息
        注入到所有工具实例中。这样工具在执行时就能知道：
        - 这个请求来自哪个 channel（如 cli、telegram、websocket）
        - 对应的 session_key 是什么
        - 消息的 metadata
        
        通过 session_key 可以实现消息路由和会话级别的工具状态隔离。"""
        from nanobot.agent.tools.context import ContextAware, RequestContext

        if session_key is not None:
            effective_key = session_key
        elif self._unified_session:
            effective_key = UNIFIED_SESSION_KEY
        else:
            effective_key = f"{channel}:{chat_id}"

        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """返回在构建 LLM 上下文时使用的 chat_id。

        优先使用 metadata 中的 context_chat_id（某些通道需要别名映射），
        如果不存在则使用 msg.chat_id。这确保 LLM 看到的聊天 ID 与实际
        路由 ID 可以不同。"""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """构建一个进度回调函数，当 Agent 在执行工具调用时会通过此回调
        把进度信息发布到消息总线上。前端可以据此展示"正在执行 XXX 工具..."。

        例如：Agent 调用 bash 工具时，用户会先看到 "Running: bash..."""
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """构建一个重试等待回调，当 LLM API 返回限流/重试时，
        通过此回调向用户频道发送等待提示消息（如 "Rate limited, retrying..."）。"""
        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )
        return _on_retry_wait

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """在 turn 完全开始前，提前把用户消息持久化到会话历史中。

        为什么要提前持久化？
        - 这样即使后续处理出错，用户的消息也不会丢失
        - WebUI 可以更早地在历史中看到这条消息
        
        返回 True 表示成功持久化，False 表示消息为空无需持久化。
        
        额外参数 (_command 等) 会附加到消息的 metadata 中。"""
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = ({"media": list(media_paths)} if media_paths else {}) | cli_app_utils.session_extra(msg.metadata)
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
    ) -> list[dict[str, Any]]:
        """构建发送给 LLM 的初始消息列表（system prompt + history + user message）。

        这是 LLM 每次调用的"输入"：
        - 包含 system prompt（Agent 的角色和行为指令）
        - 包含过去对话的历史记录
        - 包含当前用户消息（经过 image_generation_prompt 处理后）
        - 如果消息含图片，将图片数据作为 multimodal content 附加
        - 附带 session 级别的元数据信息
        
        返回的消息数组直接作为 LLM API 的 messages 参数。"""
        return self.context.build_messages(
            history=history,
            current_message=image_generation_prompt(msg.content, msg.metadata),
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata, current_runtime_lines=cli_app_utils.runtime_lines(msg, self.context.workspace),
        )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """在主事件循环中直接派发命令（不走 FSM 状态机流程）。

        用于 /stop 等需要在当前 turn 外立即执行的命令。
        命令执行结果直接发布到消息总线，不经过 RESTORE->COMPACT->... 流程。
        
        参数：
        - dispatch_fn：命令分发函数（self.commands.dispatch 或 dispatch_priority）"""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """取消指定会话的所有活跃 task 和子 Agent。

        这是 /stop 命令的核心实现：
        1. 找到该会话的所有活跃 asyncio.Task 并 cancel
        2. 等待所有 task 完成取消
        3. 取消该会话下所有运行中的子 Agent
        
        返回取消的总数（task 数 + 子 Agent 数）。"""
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """返回用于任务路由和消息中途注入的有效会话键。

        - 如果启用了 unified_session 且消息没有覆盖键，所有通道共享 UNIFIED_SESSION_KEY
        - 否则使用消息自带的 session_key（如 "telegram:12345"）"""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _replay_token_budget(self) -> int:
        """根据 context_window_tokens 计算出用于会话历史回放的 token 预算。

        预算 = 上下文窗口大小 - 输出预留 - 安全余量(1024)
        
        例如：context_window=65536，max_output=4096
              预算 = 65536 - 4096 - 1024 = 60416 tokens
        
        这个预算限制了向 LLM 回放历史消息的 token 数量，防止超出窗口限制。"""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """执行核心的 Agent 工具调用循环——这是 LLM + 工具的交互核心。

        工作流程：
        1. 同步子 Agent 的运行时限制
        2. 创建 AgentProgressHook（进度/流式/重试回调）
        3. 设置检查点回调（运行时状态快照，用于崩溃恢复）
        4. 设置消息注入回调（支持 turn 中途接收新消息）
        5. 通过 contextvars 绑定文件状态（会话级别的文件读写追踪）
        6. 调用 self.runner.run() 执行 LLM 调用 + 工具执行循环

        返回值：(final_content, tools_used, all_messages, stop_reason, had_injections)
        
        on_stream: 每收到一个文本 delta 就调用（逐字流式输出）
        on_stream_end(resuming): 流式输出结束时调用
          - resuming=True：工具调用即将开始（前端继续显示加载动画）
          - resuming=False：最终回答结束"""
        self._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
        )
        hook: AgentHook = (
            CompositeHook([loop_hook] + self._extra_hooks) if self._extra_hooks else loop_hook
        )

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = extract_documents(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                return {"role": "user", "content": user_content}

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=self.tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=self.workspace,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                # Sustained goals may legitimately exceed NANOBOT_LLM_TIMEOUT_S; idle stall
                # is still capped by NANOBOT_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=(session.metadata if session is not None else None),
                ),
            ))
        finally:
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    # ----------------------------------------------------------------
    # 主循环入口与消息分发 (Main Loop & Message Dispatch)
    # ----------------------------------------------------------------
    async def run(self) -> None:
        """启动 Agent 循环的主入口——这是 AgentLoop 的"心跳"线程。

        循环逻辑：
        1. 连接 MCP 服务器
        2. 持续从 MessageBus 消费入站消息（1 秒超时）
        3. 超时时执行 auto_compact 检查（清理过期会话）
        4. 收到消息时：
           - 如果是优先命令（如 /stop），立即内联执行
           - 如果该会话已有活跃任务，将消息注入 pending_queue（中途注入）
           - 否则创建新的 asyncio.Task 异步处理消息
        5. 随着 while 循环持续运转，保持响应性"""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, msg.session_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            effective_key = self._effective_session_key(msg)
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """处理一条消息：同一会话内部串行，不同会话之间并发执行。

        并发控制：
        - 每个会话通过 asyncio.Lock 保证串行处理（同一会话的消息不会并发）
        - 全局通过 asyncio.Semaphore 控制最大并发数（默认 3）
        
        流式支持：
        - 如果消息标记了 _wants_stream，构建 on_stream/on_stream_end 回调
        - 每个 delta 增量通过 bus.publish_outbound 以 _stream_delta 元数据发出
        
        错误处理：
        - CancelledError：恢复检查点并重新抛出
        - 其他异常：返回 "Sorry, I encountered an error." 错误消息
        
        清理：
        - finally 块中清空 pending_queue，未处理的消息重新发布到 bus"""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        # Register a pending queue so follow-up messages for this session are
        # routed here (mid-turn injection) instead of spawning a new task.
        pending = asyncio.Queue(maxsize=20)
        self._pending_queues[session_key] = pending

        try:
            async with lock, gate:
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                    if msg.channel == "websocket":
                        turn_lat = self._pending_turn_latency_ms.pop(session_key, None)
                        await self._webui_turns.handle_turn_end(
                            msg,
                            session_key=session_key,
                            latency_ms=turn_lat,
                        )
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
        finally:
            # Drain any messages still in the pending queue and re-publish
            # them to the bus so they are processed as fresh inbound messages
            # rather than silently lost.
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover, session_key,
                    )
            await self._webui_turns.publish_run_status(msg, "idle")
            self._pending_turn_latency_ms.pop(session_key, None)
            self._webui_turns.discard(session_key)

    async def close_mcp(self) -> None:
        """优雅关闭：先等待所有后台档案任务完成，再关闭所有 MCP 连接。

        关闭顺序很重要——如果先关 MCP 再等后台任务，后台任务可能
        访问已关闭的连接导致错误。"""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()

    def _schedule_background(self, coro) -> None:
        """将一个协程作为后台任务调度执行，并在 close_mcp() 时被统一等待。

        用于不阻塞当前 turn 的异步操作，比如：
        - 后台执行记忆合并（consolidation）
        - 后台生成会话标题"""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """停止 Agent 循环——设置 _running = False，run() 循环将自然退出。
        注意：这只是发出停止信号，当前正在执行的 turn 不会被立即中止。
        /stop 命令使用 _cancel_active_tasks 来立即中止运行中的任务。"""
        self._running = False
        logger.info("Agent loop stopping")

    # ----------------------------------------------------------------
    # 消息处理核心 (Message Processing Core)
    # ----------------------------------------------------------------
    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """处理来自系统通道（channel="system"）的消息。

        主要用于子 Agent 的执行结果通知：当子 Agent 完成后，
        会发送一条 system 消息，这个方法将其作为用户消息的背景上下文处理：
        - 恢复检查点和待处理回合
        - 执行记忆压缩
        - 持久化子 Agent 结果到会话
        - 以 assistant_role 的身份调用 LLM 生成回复
        - 将结果通过 OutboundMessage 返回"""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        await self.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        self._set_tool_context(
            channel, chat_id, msg.metadata.get("message_id"),
            msg.metadata, session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"

        messages = self.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata, current_runtime_lines=cli_app_utils.runtime_lines(msg, self.context.workspace, skip=is_subagent),
        )
        t_wall = time.time()
        final_content, _, all_msgs, stop_reason, _ = await self._run_agent_loop(
            messages, session=session, channel=channel, chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._save_turn(session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms)
        if channel == "websocket":
            self._pending_turn_latency_ms[key] = latency_ms
        session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._max_messages,
            )
        )
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if channel == "slack" and key.startswith("slack:") and key.count(":") >= 2:
            outbound_metadata["slack"] = {"thread_ts": key.split(":", 2)[2]}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """处理单条入站消息——这是 FSM 状态机的"驱动器"。

        执行流程：
        1. 刷新 provider 快照（检查配置变更）
        2. 如果是 system 消息，转给 _process_system_message 处理
        3. 创建 TurnContext 并初始化状态为 RESTORE
        4. while 循环驱动状态机：
           - 根据当前状态名找到对应的 _state_xxx() 方法
           - 执行状态处理器，获得事件字符串
           - 记录 trace 用于调试
           - 查 _TRANSITIONS 表得到下一状态
        5. 状态到达 DONE 后退出循环，返回 ctx.outbound"""
        self._refresh_provider_snapshot()

        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        key = session_key or msg.session_key
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=f"{key}:{time.time_ns()}",
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
        )

        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return ctx.outbound

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """将 turn 执行结果组装成最终的 OutboundMessage。

        返回值逻辑：
        - 如果 MessageTool 在本轮发送了消息，且没有中途注入，返回 None（不重复发送）
        - 否则将 final_content 包装成 OutboundMessage
        
        元数据附加：
        - _streamed=True：本次是通过流式传输的
        - latency_ms：本轮处理的耗时（毫秒）"""
        # 如果 MessageTool 本轮已直接发消息给用户，不再重复发送
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    # ----------------------------------------------------------------
    # FSM 状态处理器 (State Handlers)
    # 每个 _state_xxx 方法对应 TurnState 枚举中的一个状态。
    # 返回事件字符串（"ok"/"dispatch"/"shortcut"）驱动状态转换。
    # ----------------------------------------------------------------
    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """[RESTORE 状态] 恢复上一轮未完成的检查点，提取文档内容。

        做什么：
        1. 如果有 media 附件，从消息中提取文档/图片内容
        2. 获取或创建该会话的 Session 对象
        3. 恢复运行时检查点（上一轮 /stop 时保存的工具调用状态）
        4. 恢复待处理的用户回合（上一轮用户消息已持久化但未得到回复）
        
        返回 "ok" 进入 COMPACT 状态。"""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        mark_webui_session(ctx.session, msg.metadata)

        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        """[COMPACT 状态] 执行会话压缩和清理。

        调用 auto_compact 检查：
        - 会话是否过期（超过 session_ttl_minutes）
        - 是否需要触发记忆合并
        
        如果有 pending_summary，说明本轮需要将记忆摘要注入 LLM 上下文。
        返回 "ok" 进入 COMMAND 状态。"""
        ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
        ctx.pending_summary = pending
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        """[COMMAND 状态] 检测和处理斜杠命令（如 /new, /stop, /model 等）。

        - 如果消息匹配到内置命令：执行并持久化结果，返回 "shortcut"（跳到 DONE）
        - 如果消息不是命令：返回 "dispatch"（进入正常的 BUILD 流程）

        特殊处理：/new 命令不持久化历史（因为它会清空会话）。"""
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message(
                    "assistant", result.content, _command=True
                )
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self, ctx: TurnContext) -> str:
        """[BUILD 状态] 构建发送给 LLM 的完整上下文。

        步骤：
        1. 执行 token 级别的记忆合并（防止历史消息超出上下文窗口）
        2. 为所有工具设置当前请求上下文（通道、会话信息）
        3. 初始化 MessageTool（重置 turn 内的消息发送状态）
        4. 从会话加载历史消息（按 max_messages 和 token_budget 限制）
        5. 捕获 WebUI 标题生成上下文
        6. 构建 initial_messages（system prompt + history + user message）
        7. 提前持久化用户消息（防止处理失败时丢失）
        8. 构建进度回调和重试回调
        
        返回 "ok" 进入 RUN 状态。"""
        await self.consolidator.maybe_consolidate_by_tokens(
            ctx.session,
            replay_max_messages=self._max_messages,
        )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._webui_turns.capture_title_context(
            ctx.session_key,
            ctx.msg,
            self.llm_runtime(),
        )

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg, ctx.session, ctx.history, ctx.pending_summary
        )
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        """[RUN 状态] 执行核心 Agent 循环——调用 LLM + 执行工具调用。

        这是 FSM 中最核心的状态：
        1. 向 WebUI 发布 "running" 状态
        2. 调用 _run_agent_loop() 传入完整的消息上下文和回调
        3. 将返回的 5 个结果写入 TurnContext：
           - final_content：LLM 的最终文本回复
           - tools_used：本轮调用的工具列表
           - all_messages：完整交互记录（含工具调用）
           - stop_reason：停止原因
           - had_injections：是否有中途注入消息
        
        返回 "ok" 进入 SAVE 状态。"""
        await self._webui_turns.publish_run_status(ctx.msg, "running")
        result = await self._run_agent_loop(
            ctx.initial_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            pending_queue=ctx.pending_queue,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        """[SAVE 状态] 将本轮执行结果持久化到会话历史。

        步骤：
        1. 如果 LLM 没有返回内容，填入 EMPTY_FINAL_RESPONSE_MESSAGE
        2. 计算 save_skip（跳过的历史消息数）
        3. 计算 turn_latency_ms（本轮总耗时）
        4. 调用 _save_turn 保存新增消息到会话
        5. 强制执行文件上限检查（防止磁盘膨胀）
        6. 清理运行时检查点和待处理回合标记
        7. 后台调度记忆合并任务
        
        返回 "ok" 进入 RESPOND 状态。"""
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.save_skip = 1 + len(ctx.history) + (1 if ctx.user_persisted_early else 0)

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))
        self._save_turn(
            ctx.session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.msg.channel == "websocket":
            self._pending_turn_latency_ms[ctx.session_key] = ctx.turn_latency_ms
        ctx.session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        )
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        """[RESPOND 状态] 将运行结果组装成 OutboundMessage。

        调用 _assemble_outbound 将 TurnContext 中的执行结果
        包装成 OutboundMessage，存入 ctx.outbound。
        
        如果 MessageTool 本轮已直接回复用户，outbound 可能为 None。
        返回 "ok" 进入 DONE 状态。"""
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        return "ok"

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """清洗消息内容块（Content Blocks），去除不适合持久化的数据。

        持久化前的"消毒"处理：
        1. 移除运行时上下文标签块（如 <runtime_context>...</runtime_context>）
        2. 将内联的 base64 图片替换为占位文本（"[Image: path/to/img.png]"）
        3. 如果 should_truncate_text=True，裁剪过长文本到 max_tool_result_chars
        
        为什么要清洗？
        - base64 图片二进制数据非常大，不应存入会话历史
        - 运行时上下文每次构建时动态生成，不应持久化
        - 工具返回结果可能很长，需要截断
        
        返回清洗后的 content blocks 列表。"""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """将本轮新增的消息保存到 Session 中。

        参数：
        - messages：本轮完整的 LLM 交互消息列表
        - skip：跳过前 skip 条（已有的历史消息，不需要重复保存）
        - turn_latency_ms：本轮耗时，会附加到最后一条 assistant 消息上
        
        处理逻辑（按 role 分别处理）：
        - assistant：跳过空内容且无 tool_calls 的消息
        - tool：截断超过 max_tool_result_chars 的结果
        - user：移除运行时上下文标签，清洗 content blocks
        - 每条消息附加上 timestamp
        
        保存后更新 session.updated_at 为当前时间。"""
        from datetime import datetime

        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """将子 Agent 的执行结果持久化到会话历史中。

        去重逻辑：
        - 如果 msg 没有内容，不持久化
        - 如果同一个 subagent_task_id 已经存在于会话中，不重复持久化
        
        持久化时使用 role="assistant" + injected_event="subagent_result"，
        这样在后续获取历史时可以通过 injected_event 过滤或识别。
        
        返回 True 表示新增了一条记录，False 表示被去重或无内容。"""
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    # ----------------------------------------------------------------
    # 检查点与恢复 (Checkpoint & Recovery)
    # 用于处理 /stop 中断和崩溃恢复的场景。
    # ----------------------------------------------------------------
    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """保存运行时检查点到 session.metadata。

        在 Agent 执行过程中，每次工具调用完成后将当前状态快照
        （assistant_message + completed_tool_results + pending_tool_calls）
        保存到 session 中。如果本轮被 /stop 中断，这些数据可以被恢复。"""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        """标记存在一个待处理的用户回合——用户消息已持久化但尚未得到回复。
        如果之后崩溃，下次启动时 _restore_pending_user_turn 会补充错误回复。"""
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        """清除待处理用户回合标记——正常完成回复后调用。"""
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        """清除运行时检查点——turn 正常完成后调用。"""
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        """从一条消息中提取用于去重比较的"键"。

        当从检查点恢复消息时，需要判断哪些消息已经存在于会话中
        （避免重复插入）。通过比较这个消息键可以确定重叠部分。

        键包含：role, content, tool_call_id, name, tool_calls,
        reasoning_content, thinking_blocks"""
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """从 session.metadata 中恢复上一次未完成的 turn 状态。

        这是 /stop 和崩溃恢复的核心机制：
        1. 从检查点读取上次保存的 assistant_message、completed_tool_results
           （已完成工具结果）和 pending_tool_calls（未完成的工具调用）
        2. 将恢复的消息与现有会话历史做去重比较，找到重叠部分
        3. 将新消息追加到会话历史末尾，pending 的工具调用标记为中断错误
        4. 清理检查点和待处理标记
        
        返回 True 表示成功恢复了数据。"""

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """恢复一个"只持久化了用户消息但未得到回复"的回合。

        场景：用户发了一条消息，系统在 BUILD 阶段提前持久化了用户消息，
        但在 RUN 阶段之前崩溃了。此时会话历史上最后一条是 user 消息，
        但没有对应的 assistant 回复。
        
        恢复操作：追加一条 assistant 错误消息到会话历史。"""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """直接处理一条消息并返回 OutboundMessage——不经过 MessageBus。

        这是 CLI 和测试用例常用的便捷方法：
        1. 连接 MCP 服务器
        2. 构造 InboundMessage
        3. 调用 _process_message 走完整的 FSM 流程
        4. 直接返回 OutboundMessage（不发布到总线）
        
        与 run() 循环的区别：run() 通过 bus.consume_inbound() 获取消息，
        而 process_direct 直接传入 content 字符串。"""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [],
        )
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
