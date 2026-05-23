"""
异步消息总线 (Async Message Bus)

实现聊天通道 (Channels) 与 Agent 核心之间的解耦通信。

设计模式：
- 生产者-消费者模式：通道生产消息，Agent 消费消息
- 异步队列：基于 asyncio.Queue 的非阻塞队列
- 双向通信：入站（用户→Agent）和出站（Agent→用户）分离

数据流::

    ┌─────────────┐    publish_inbound()    ┌─────────────┐
    │   Channel   │ ───────────────────────→ │  Inbound Q  │
    │  (Telegram) │                         │  (asyncio)  │
    └─────────────┘                         └──────┬──────┘
                                                   │ consume_inbound()
                                                   ▼
                                            ┌─────────────┐
                                            │  AgentLoop  │
                                            │  (处理逻辑)  │
                                            └──────┬──────┘
                                                   │ publish_outbound()
                                                   ▼
                                            ┌─────────────┐
                                            │ Outbound Q  │
                                            │ (asyncio)   │
                                            └──────┬──────┘
                                                   │ consume_outbound()
                                                   ▼
                                            ┌─────────────┐
                                            │   Channel   │
                                            │  (发送回复)  │
                                            └─────────────┘

使用场景：
1. 多通道支持：Telegram、Discord、Slack 等共享同一个 Agent
2. 异步处理：通道发送后立即返回，不阻塞等待响应
3. 缓冲机制：处理速度不匹配时的缓冲

线程安全：
- asyncio.Queue 本身是协程安全的
- 不需要额外的锁机制
- 必须在同一个事件循环中操作
"""

import asyncio

from nanobot.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    消息总线 (Message Bus)

    使用两个 asyncio.Queue 实现双向异步通信。

    队列说明：
    - inbound: 入站队列，存放来自渠道的用户消息
    - outbound: 出站队列，存放 Agent 的回复消息

    典型使用方式（在 Channel 中）::

        # 发送用户消息到 Agent
        await bus.publish_inbound(InboundMessage(
            channel="telegram",
            sender_id="user123",
            chat_id="456",
            content="你好"
        ))

        # 等待并获取 Agent 回复
        response = await bus.consume_outbound()
        # 发送回复给用户...
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
