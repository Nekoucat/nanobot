"""
Agent 核心模块 (Agent Core Module)

本模块是 nanobot 的智能体引擎核心，导出了所有关键的 Agent 组件。

模块组成：
┌─────────────────────────────────────────────────────────────┐
│                      Agent Core                             │
├─────────────────────────────────────────────────────────────┤
│  AgentLoop      - 主处理循环（状态机驱动的消息处理引擎）     │
│  AgentRunner    - LLM 调用与工具执行迭代器                   │
│  ContextBuilder - System Prompt 组装器                       │
│  MemoryStore    - 记忆存储（文件 I/O）                       │
│  Consolidator   - 上下文压缩器                               │
│  Dream          - 梦境记忆系统（定期反思与压缩）              │
│  SkillsLoader   - 技能描述加载器                             │
│  SubagentManager- 子代理管理器（嵌套 Agent 调用）            │
│  AgentHook      - 生命周期钩子基类                           │
│  CompositeHook  - 组合钩子（支持多个钩子串联）                │
│  AgentHookContext - 钩子上下文（提供运行时信息）             │
└─────────────────────────────────────────────────────────────┘

核心数据流：
    用户消息 → AgentLoop(状态机) 
             → ContextBuilder(组装 Prompt) 
             → LLM Provider(调用模型)
             → AgentRunner(工具调用循环)
             → 返回响应

使用示例：
    from nanobot.agent import (
        AgentLoop, ContextBuilder, MemoryStore,
        Dream, SkillsLoader, SubagentManager,
        AgentHook, AgentHookContext, CompositeHook
    )
"""

from nanobot.agent.context import ContextBuilder
from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import Dream, MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.subagent import SubagentManager

__all__ = [
    # ==================== 核心引擎 ====================
    "AgentLoop",           # 主处理循环（状态机）
    
    # ==================== 上下文构建 ====================
    "ContextBuilder",      # System Prompt + 消息历史组装器
    
    # ==================== 记忆系统 ====================
    "MemoryStore",         # 长期记忆存储（文件 I/O）
    "Dream",               # 梦境记忆（反思/压缩）
    
    # ==================== 工具与技能 ====================
    "SkillsLoader",        # 技能描述加载器
    
    # ==================== 子代理 ====================
    "SubagentManager",     # 子代理管理器（嵌套调用）
    
    # ==================== 生命周期钩子 ====================
    "AgentHook",           # 钩子基类（用户可继承）
    "AgentHookContext",    # 钩子上下文对象
    "CompositeHook",       # 组合钩子（多钩子串联执行）
]
