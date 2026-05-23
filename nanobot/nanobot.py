"""
Nanobot 高级编程接口 (High-level Programmatic Interface)

本模块提供了 nanobot 的 SDK 封装，允许用户在 Python 代码中直接调用 AI Agent，
而无需通过命令行或 HTTP API。

核心类：
- Nanobot: Agent 的高级封装，提供简洁的 run() 接口
- RunResult: 单次运行的结果数据结构

使用示例：
    # 基本用法
    bot = Nanobot.from_config()
    result = await bot.run("总结这个仓库的功能")
    print(result.content)
    print(f"使用了工具: {result.tools_used}")
    
    # 指定配置文件和会话 key
    bot = Nanobot.from_config(config_path="./my_config.json")
    result = await bot.run("分析数据", session_key="analysis:001")
    
    # 使用生命周期钩子
    from nanobot.agent.hook import AgentHook
    bot = Nanobot.from_config()
    result = await bot.run("完成任务", hooks=[MyCustomHook()])
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook, SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.providers.image_generation import image_gen_provider_configs


@dataclass(slots=True)
class RunResult:
    """
    单次 Agent 运行的结果数据类。
    
    每次调用 Nanobot.run() 都会返回此对象，包含完整的运行信息。
    
    Attributes:
        content (str): Agent 的最终文本回复内容。
                       如果 Agent 只通过工具交互而无文本输出，可能为空字符串。
        tools_used (list[str]): 本次运行中调用的工具名称列表，
                               如 ["web_search", "filesystem_read"]
        messages (list[dict[str, Any]]): 完整的消息记录，包含：
            - user/assistant/system 角色消息
            - tool_call 和 tool 结果消息
            - 可用于调试或构建更复杂的对话流程
    
    Example:
        >>> result = await bot.run("搜索最新新闻")
        >>> print(result.content)
        '根据搜索结果，最新新闻是...'
        >>> print(result.tools_used)
        ['web_search', 'extract_content']
    """
    content: str                          # 最终回复内容
    tools_used: list[str]                 # 调用的工具名称列表
    messages: list[dict[str, Any]]        # 完整的消息记录


class Nanobot:
    """
    Nanobot 编程接口门面类 (Programmatic Facade)。
    
    本类是对底层 AgentLoop 的高级封装，提供了简洁易用的异步接口，
    让用户可以在 Python 代码中直接与 AI Agent 交互，无需关心：
    - MessageBus 消息队列的设置
    - 会话管理的细节
    - 工具注册的过程
    
    典型使用场景：
    - 在 Python 脚本/Notebook 中集成 AI 能力
    - 构建基于 nanobot 的上层应用
    - 编写自动化测试用例
    - 作为其他框架/库的后端
    
    Usage::
        # 方式一：从配置文件创建（推荐）
        bot = Nanobot.from_config()
        result = await bot.run("Summarize this repo", hooks=[MyHook()])
        print(result.content)
        
        # 方式二：指定自定义配置路径
        bot = Nanobot.from_config(
            config_path="/path/to/config.json",
            workspace="./my_workspace"
        )
    """

    def __init__(self, loop: AgentLoop) -> None:
        """
        初始化 Nanobot 实例（内部使用，推荐使用 from_config() 工厂方法）。
        
        Args:
            loop: 已配置好的 AgentLoop 核心循环实例，
                  包含了 Provider、SessionManager、ToolRegistry 等所有依赖
        """
        self._loop = loop  # 内部持有的 AgentLoop 引用

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Nanobot:
        """
        工厂方法：从配置文件创建 Nanobot 实例。
        
        这是创建 Nanobot 实例的推荐方式。它会自动：
        1. 加载并解析配置文件（JSON 格式）
        2. 替换配置中的环境变量引用（如 ${ENV_VAR}）
        3. 创建 LLM Provider（支持 OpenAI/Anthropic/Azure 等）
        4. 初始化 AgentLoop 及其所有子组件
        
        Args:
            config_path: 配置文件路径。默认为 ~/.nanobot/config.json
                        支持 ~ 开头的路径扩展
            workspace:   覆盖配置中的工作目录路径。
                        工作目录用于存储会话、记忆、技能等数据
                        
        Returns:
            Nanobot: 配置完成的 Nanobot 实例，可直接调用 run()
            
        Raises:
            FileNotFoundError: 如果指定的配置文件不存在
            
        Example:
            >>> # 使用默认配置
            >>> bot = Nanobot.from_config()
            
            >>> # 使用自定义配置
            >>> bot = Nanobot.from_config(
            ...     config_path="./config.json",
            ...     workspace="./project_workspace"
            ... )
        """
        from nanobot.config.loader import load_config, resolve_config_env_vars
        from nanobot.config.schema import Config

        # 解析并验证配置文件路径
        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()  # 展开 ~ 并转为绝对路径
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        # 加载配置并替换环境变量
        config: Config = resolve_config_env_vars(load_config(resolved))
        
        # 如果指定了工作目录覆盖，更新配置
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )

        # 从配置创建 AgentLoop 核心
        loop = AgentLoop.from_config(
            config,
            image_generation_provider_configs=image_gen_provider_configs(config),
        )
        return cls(loop)

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        hooks: list[AgentHook] | None = None,
    ) -> RunResult:
        """
        执行一次 Agent 运行并返回结果。
        
        这是 Nanobot 类的核心方法。它会：
        1. 将用户消息发送给 Agent 处理
        2. Agent 可能调用多个工具来完成任务
        3. 收集最终回复、使用的工具、完整消息记录
        4. 返回封装好的 RunResult 对象
        
        Args:
            message:     用户要发送的消息内容，如 "分析这段代码的问题"
            session_key: 会话标识符，用于隔离不同的对话上下文。
                         相同 session_key 的调用共享历史记录，
                         不同 key 则是完全独立的对话。
                         默认 "sdk:default" 表示使用默认会话
            hooks:       可选的生命周期钩子列表，用于监控或干预运行过程：
                         - on_before_run: 运行前触发
                         - on_after_run:  运行后触发
                         - on_tool_call:  工具调用时触发
                         
        Returns:
            RunResult: 包含运行结果的完整数据：
                - content: Agent 的最终回复
                - tools_used: 调用的工具列表
                - messages: 完整消息历史
                
        Example:
            >>> # 基本用法
            >>> result = await bot.run("今天天气怎么样")
            >>> print(result.content)
            
            >>> # 带钩子的用法
            >>> class PrintHook(AgentHook):
            ...     async def on_tool_call(self, name, args):
            ...         print(f"调用工具: {name}")
            >>> 
            >>> result = await bot.run(
            ...     "搜索资料",
            ...     session_key="research:001",
            ...     hooks=[PrintHook()]
            ... )
        """
        # 创建 SDK 捕获钩子，用于收集运行过程中的工具使用和消息记录
        capture = SDKCaptureHook()
        
        # 保存原有的额外钩子列表，以便在运行后恢复
        prev = self._loop._extra_hooks
        base_hooks = list(hooks) if hooks is not None else list(prev or [])
        
        # 组合钩子：SDK捕获钩子 + 用户自定义钩子 + 原有钩子
        self._loop._extra_hooks = [capture, *base_hooks]
        try:
            # 调用 AgentLoop 的直接处理方法
            response = await self._loop.process_direct(
                message, session_key=session_key,
            )
        finally:
            # 无论成功失败都恢复原有钩子状态
            self._loop._extra_hooks = prev

        # 提取最终回复内容（如果 response 为空则返回空字符串）
        content = (response.content if response else None) or ""
        
        # 构建并返回 RunResult
        return RunResult(
            content=content,
            tools_used=capture.tools_used,      # 从捕获钩子获取使用的工具列表
            messages=capture.messages,          # 从捕获钩子获取完整消息记录
        )
