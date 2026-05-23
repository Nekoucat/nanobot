"""
Agent 生命周期钩子 (Lifecycle Hooks)

本模块定义了 Agent 运行过程中的钩子接口，允许用户自定义和监控 Agent 行为。

设计理念：
- 观察者模式：在 Agent 运行的关键节点插入回调
- 组合优于继承：通过 CompositeHook 实现多钩子组合
- 错误隔离：单个钩子的异常不会影响其他钩子和主流程

钩子类型：
1. AgentHook: 基础抽象类，定义所有生命周期回调
2. CompositeHook: 组合钩子，将多个钩子串联执行
3. SDKCaptureHook: 专门用于 SDK 模式的结果捕获钩子

生命周期事件顺序：
    before_iteration → [on_stream*] → before_execute_tools
    → [emit_reasoning*] → emit_reasoning_end
    → after_iteration → finalize_content

使用示例::

    class MyLoggingHook(AgentHook):
        async def on_tool_call(self, name, args):
            print(f"工具调用: {name}({args})")

        async def after_iteration(self, context):
            print(f"迭代 {context.iteration} 完成")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMResponse, ToolCallRequest


@dataclass(slots=True)
class AgentHookContext:
    """
    钩子上下文 (Hook Context)

    每次迭代时传递给钩子的可变状态对象，包含当前运行的所有关键信息。

    这个数据类是钩子与 Agent Runner 之间的数据桥梁，允许钩子读取运行状态
    和（在某些情况下）修改行为。

    Attributes:
        iteration: 当前迭代次数（从 1 开始计数）
        messages: 当前完整的消息列表（可变）
        response: 最近一次 LLM 响应
        usage: Token 使用统计 {"prompt": n, "completion": n}
        tool_calls: 本次迭代的工具调用请求列表
        tool_results: 已完成的工具结果列表
        tool_events: 工具事件日志（用于 UI 显示）
        streamed_content: 是否有流式内容输出
        streamed_reasoning: 是否有推理内容输出
        final_content: 最终文本内容（迭代结束后设置）
        stop_reason: LLM 停止原因
        error: 错误信息（如果有）
    """
    iteration: int                                          # 当前迭代次数 (1-based)
    messages: list[dict[str, Any]]                          # 完整消息历史（可变引用）
    response: LLMResponse | None = None                     # 最近一次 LLM 响应
    usage: dict[str, int] = field(default_factory=dict)     # Token 用量统计
    tool_calls: list[ToolCallRequest] = field(default_factory=list)   # 工具调用请求
    tool_results: list[Any] = field(default_factory=list)           # 工具执行结果
    tool_events: list[dict[str, str]] = field(default_factory=list) # 工具事件日志
    streamed_content: bool = False                           # 是否输出流式文本
    streamed_reasoning: bool = False                         # 是否输出推理过程
    final_content: str | None = None                         # 最终回复内容
    stop_reason: str | None = None                           # 停止原因
    error: str | None = None                                 # 错误信息


class AgentHook:
    """
    Agent 钩子基类 (Base Agent Hook)

    定义 Agent 运行过程中的生命周期回调接口。
    
    所有回调方法都是异步的（async），即使实现是同步的也需要用 async def 声明。

    回调触发顺序：
    1. before_iteration() - 每次迭代开始前
    2. [on_stream()*] - 流式文本片段（如果有）
    3. [on_stream_end()] - 流式段落结束
    4. before_execute_tools() - 工具执行前
    5. [emit_reasoning()*] - 推理内容片段（如果有）
    6. emit_reasoning_end() - 推理结束
    7. after_iteration() - 每次迭代结束后
    8. finalize_content() - 最终内容处理（同步方法）

    使用示例::

        class ProgressPrinter(AgentHook):
            async def before_iteration(self, ctx):
                print(f"开始第 {ctx.iteration} 次迭代...")
            
            async def after_iteration(self, ctx):
                print(f"迭代完成，停止原因: {ctx.stop_reason}")
            
            async def on_stream(self, ctx, delta: str):
                print(delta, end="", flush=True)

    Args:
        reraise: 如果为 True，钩子异常会向上抛出；
                如果为 False（默认），异常会被 CompositeHook 捕获并记录日志
    """

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise

    def wants_streaming(self) -> bool:
        """
        是否需要流式输出支持。
        
        返回 True 时，AgentRunner 会启用流式模式，
        并在每次收到文本 delta 时调用 on_stream()。
        
        Returns:
            bool: 默认返回 False，流式感知的钩子应重写此方法返回 True
        """
        return False

    async def before_iteration(self, context: AgentHookContext) -> None:
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        pass

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        pass

    async def emit_reasoning_end(self) -> None:
        """Mark the end of an in-flight reasoning stream.

        Hooks that buffer ``emit_reasoning`` chunks (for in-place UI updates)
        flush and freeze the rendered group here. One-shot hooks ignore.
        """
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return content


class CompositeHook(AgentHook):
    """
    组合钩子 (Composite Hook) - 将多个钩子串联执行

    采用"广播"模式：每个回调事件会依次发送给所有子钩子。

    错误处理策略：
    - 默认情况下，单个钩子的异常不会影响其他钩子（异常被捕获并记录）
    - 设置 _reraise=True 的钩子，其异常会被向上抛出
    - finalize_content() 是管道模式：前一个钩子的输出作为下一个的输入

    使用示例::

        combined = CompositeHook([
            ProgressPrinter(),        # 打印进度
            MetricsCollector(),      # 收集指标
            LoggingHook(reraise=True),  # 记录日志（异常会抛出）
        ])
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)

    def wants_streaming(self) -> bool:
        """只要有一个子钩子需要流式就启用。"""
        return any(h.wants_streaming() for h in self._hooks)

    async def _for_each_hook_safe(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        for h in self._hooks:
            if getattr(h, "_reraise", False):
                await getattr(h, method_name)(*args, **kwargs)
                continue

            try:
                await getattr(h, method_name)(*args, **kwargs)
            except Exception:
                logger.exception("AgentHook.{} error in {}", method_name, type(h).__name__)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_iteration", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._for_each_hook_safe("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._for_each_hook_safe("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_execute_tools", context)

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        await self._for_each_hook_safe("emit_reasoning", reasoning_content)

    async def emit_reasoning_end(self) -> None:
        await self._for_each_hook_safe("emit_reasoning_end")

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content


class SDKCaptureHook(AgentHook):
    """
    SDK 捕获钩子 (SDK Capture Hook)

    专门用于 Nanobot.run() SDK 接口的结果收集。
    
    职责：
    1. 记录每次迭代中调用的工具名称
    2. 捕获完整的消息历史快照
    
    工作原理：
    - 在 after_iteration() 中刷新工具列表和消息快照
    - 由于 Runner 会原地修改 messages 列表，
      每次迭代后都需要重新获取最新状态
    - 最后一次 after_iteration() 的结果即为最终状态

    使用方式（内部使用，用户通常不需要直接创建）::

        capture = SDKCaptureHook()
        # ... 运行 Agent ...
        result = RunResult(
            content=final_content,
            tools_used=capture.tools_used,   # ["web_search", "read_file"]
            messages=capture.messages,        # 完整消息历史
        )
    """

    def __init__(self) -> None:
        super().__init__()
        self.tools_used: list[str] = []                  # 累积的工具调用名称
        self.messages: list[dict[str, Any]] = []          # 最新消息快照

    async def after_iteration(self, context: AgentHookContext) -> None:
        """
        每次迭代结束后刷新捕获数据。
        
        收集本轮的工具调用并更新消息快照。
        Runner 原地修改 messages，所以需要每次都复制。
        """
        for call in context.tool_calls:
            self.tools_used.append(call.name)
        self.messages = list(context.messages)
