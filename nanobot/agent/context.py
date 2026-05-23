"""
上下文构建器 (Context Builder)

负责组装发送给 LLM 的完整消息列表，包括系统提示和用户消息。

核心职责：
1. 构建系统提示（System Prompt）
   - 身份定义：Agent 的人格和行为准则
   - 启动文件：从工作区加载 AGENTS.md, SOUL.md, USER.md
   - 工具契约：工具使用说明和约束
   - 记忆系统：长期记忆和最近历史
   - 技能模块：可用技能的描述

2. 构建用户消息（User Message）
   - 文本内容
   - 图像附件（base64 编码）
   - 运行时元数据（时间、渠道、聊天 ID 等）

3. 消息合并策略
   - 避免连续同角色消息（部分供应商不支持）
   - 运行时上下文附加在用户消息末尾
   - 支持多模态内容（文本 + 图像）

架构设计：
- ContextBuilder 被 AgentLoop 持有，每次调用 build_messages()
- 使用 MemoryStore 加载长期记忆
- 使用 SkillsLoader 加载技能定义
- 从 templates/ 目录加载提示模板

输出结构::

    [
        {"role": "system", "content": "完整的系统提示..."},
        ...历史消息...,
        {"role": "user", "content": "用户消息\n\n[Runtime Context...]"}
    ]

配置依赖：
- workspace: 工作目录路径（用于加载文件和记忆）
- timezone: 时区设置（用于显示当前时间）
- disabled_skills: 要禁用的技能列表
"""

import base64
import mimetypes
import platform
from contextlib import suppress
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, Mapping, Sequence

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.session.goal_state import goal_state_runtime_lines
from nanobot.utils.helpers import (
    current_time_str,
    detect_image_mime,
    truncate_text,
)
from nanobot.utils.prompt_templates import render_template


class ContextBuilder:
    """
    上下文构建器 (Context Builder)

    负责组装完整的 LLM 输入消息，是 Agent 智能行为的核心组件。

    系统提示组成（按顺序）：
    1. 身份 (Identity): Agent 的基本定义和运行环境信息
    2. 启动文件 (Bootstrap): AGENTS.md, SOUL.md, USER.md（如果存在）
    3. 工具契约 (Tool Contract): 工具使用规范和安全约束
    4. 长期记忆 (Memory): MEMORY.md 文件的内容
    5. 常驻技能 (Always Skills): 始终激活的技能说明
    6. 技能索引 (Skills Index): 可用技能的摘要列表
    7. 最近历史 (Recent History): 未处理的历史事件
    8. 会话摘要 (Session Summary): 来自压缩的上下文摘要

    运行时元数据 (Runtime Context)：
    - 当前时间、渠道、聊天 ID、发送者 ID
    - 目标状态信息（持续目标的进度）
    - 标记为"非指令"，防止 LLM 将其作为指令执行

    Attributes:
        BOOTSTRAP_FILES: 要加载的启动文件名列表
        _RUNTIME_CONTEXT_TAG: 运行时上下文的开始标记
        _MAX_RECENT_HISTORY: 最大历史事件数量
        _MAX_HISTORY_CHARS: 历史部分的最大字符数
        _RUNTIME_CONTEXT_END: 运行时上下文的结束标记

    使用示例::

        builder = ContextBuilder(
            workspace=Path("~/.nanobot/workspace"),
            timezone="Asia/Shanghai",
            disabled_skills=["summarize"]
        )

        messages = builder.build_messages(
            history=session.get_history(),
            current_message="帮我分析这段代码",
            media=["/path/to/image.png"],
            channel="telegram",
            chat_id="12345",
        )
        # messages 现在可以传给 Provider.chat()
    """

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]   # 启动文件列表
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"  # 运行时标记开头
    _MAX_RECENT_HISTORY = 50                                    # 最大历史事件数
    _MAX_HISTORY_CHARS = 32_000                                 # 历史字符上限
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"                 # 运行时标记结尾

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        parts.append(render_template("agent/tool_contract.md"))

        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
        if entries:
            capped = entries[-self._MAX_RECENT_HISTORY:]
            history_text = "\n".join(
                f"- [{e['timestamp']}] {e['content']}" for e in capped
            )
            history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
            parts.append("# Recent History\n\n" + history_text)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        with suppress(Exception):
            tpl = pkg_files("nanobot") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        current_runtime_lines: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        extra = [
            *goal_state_runtime_lines(session_metadata),
        ]
        if current_runtime_lines:
            extra.extend(line for line in current_runtime_lines if line)
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        messages = [
            {"role": "system", "content": self.build_system_prompt(skill_names, channel=channel, session_summary=session_summary)},
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
