"""WebSocket server channel: nanobot acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import hashlib
import hmac
import http
import json
import mimetypes
import re
import secrets
import shutil
import ssl
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger
from pydantic import Field, field_validator, model_validator
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.command.builtin import builtin_command_palette
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.session.goal_state import goal_state_ws_blob
from nanobot.session.webui_turns import websocket_turn_wall_started_at
from nanobot.utils.helpers import safe_filename
from nanobot.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from nanobot.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanobot.webui.settings_api import (
    WebUISettingsError,
    settings_payload,
    update_agent_settings,
    update_image_generation_settings,
    update_provider_settings,
    update_web_search_settings,
)
from nanobot.webui.cli_apps_api import (
    cli_apps_action,
    cli_apps_payload,
    normalize_cli_app_mentions,
)
from nanobot.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from nanobot.webui.thread_disk import delete_webui_thread
from nanobot.webui.transcript import append_transcript_object, build_webui_thread_response

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _normalize_config_path(path: str) -> str:
    return _strip_trailing_slash(path)


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      from ``token_issue_path`` are also accepted.
    - ``token_issue_path``: If non-empty, **GET** (HTTP/1.1) to this path returns JSON
      ``{"token": "...", "expires_in": <seconds>}``; use ``?token=...`` when opening the WebSocket.
      Must differ from ``path`` (the WS upgrade path). If the client runs in the **same process** as
      nanobot and shares the asyncio loop, use a thread or async HTTP client for GET—do not call
      blocking ``urllib`` or synchronous ``httpx`` from inside a coroutine.
    - ``token_issue_secret``: If non-empty, token requests must send ``Authorization: Bearer <secret>`` or
      ``X-Nanobot-Auth: <secret>``.
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but neither token nor "
            "token_issue_secret is set — set one to prevent unauthenticated access"
        )


def _http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        ]
    )
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, headers, body)


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """Enqueue a runtime model snapshot for websocket subscribers (fan-out in-channel)."""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _default_model_name_from_config() -> str | None:
    """Resolved model string from on-disk config (bootstrap fallback)."""
    try:
        from nanobot.config.loader import load_config

        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str | None:
    """Prefer an in-process resolver (e.g. AgentLoop); else config-derived default."""
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config()


def _parse_request_path(path_with_query: str) -> tuple[str, dict[str, list[str]]]:
    """Parse normalized path and query parameters in one pass."""
    parsed = urlparse("ws://x" + path_with_query)
    path = _strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query, keep_blank_values=True)


def _normalize_http_path(path_with_query: str) -> str:
    """Return the path component (no query string), with trailing slash normalized (root stays ``/``)."""
    return _parse_request_path(path_with_query)[0]


def _parse_query(path_with_query: str) -> dict[str, list[str]]:
    return _parse_request_path(path_with_query)[1]


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = query.get(key)
    return values[0] if values else None


def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED

_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


_LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Matches the legacy chat-id pattern but allows file-system-safe stems too,
# so the API can address sessions whose keys came from non-WebSocket channels.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def _decode_api_key(raw_key: str) -> str | None:
    """Decode a percent-encoded API path segment, then validate the result."""
    key = unquote(raw_key)
    if _API_KEY_RE.match(key) is None:
        return None
    return key


def _is_localhost(connection: Any) -> bool:
    """Return True if *connection* originated from the loopback interface."""
    addr = getattr(connection, "remote_address", None)
    if not addr:
        return False
    host = addr[0] if isinstance(addr, tuple) else addr
    if not isinstance(host, str):
        return False
    # ``::ffff:127.0.0.1`` is loopback in IPv6-mapped form.
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCALHOSTS


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def _http_error(status: int, message: str | None = None) -> Response:
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return _http_response(body, status=status)


def _bearer_token(headers: Any) -> str | None:
    """Pull a Bearer token out of standard or query-style headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 without padding — compact + friendly in URL paths."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Reverse of :func:`_b64url_encode`; caller handles ``ValueError``."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# Allowed MIME types we actually serve from the media endpoint. Anything
# outside this set is degraded to ``application/octet-stream`` so an
# attacker who somehow gets a signed URL for an unexpected file type can't
# trick the browser into sniffing executable content.
_MEDIA_ALLOWED_MIMES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/webm",
    "video/quicktime",
})


def _issue_route_secret_matches(headers: Any, configured_secret: str) -> bool:
    """Return True if the token-issue HTTP request carries credentials matching ``token_issue_secret``."""
    if not configured_secret:
        return True
    authorization = headers.get("Authorization") or headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied, configured_secret)
    header_token = headers.get("X-Nanobot-Auth") or headers.get("x-nanobot-auth")
    if not header_token:
        return False
    return hmac.compare_digest(header_token.strip(), configured_secret)


class WebSocketChannel(BaseChannel):
    """WebSocket 服务器频道 —— nanobot 的 Web 前端通信核心。

    这个类作为 WebSocket 服务器运行，承载以下职责：
    1. **HTTP 路由层**：同时处理 HTTP 请求（REST API、静态文件、Bootstrap认证）和 WebSocket 升级
    2. **连接管理**：维护 chat_id → 连接的订阅映射，支持单个 WS 连接多路复用多个 chat_id
    3. **消息转发**：将前端消息写入 MessageBus.inbound，将 AgentLoop 响应从 MessageBus.outbound 推送给前端
    4. **流式传输**：支持 reasoning_delta、delta、stream_end 等流式事件实时推送到浏览器
    5. **Token 认证**：签发临时 token（bootstrap），验证 WS 握手和 API 请求
    6. **媒体文件服务**：通过 HMAC 签名的 URL 安全地提供图片/视频等媒体
    7. **会话管理 REST API**：提供 /api/sessions、/api/settings 等接口供前端调用

    连接模型：
    - 一个 WebSocket 连接可以订阅多个 chat_id（通过 new_chat/attach envelope）
    - 默认连接时自动分配一个 default_chat_id 并发送 "ready" 事件
    - 支持新旧两种消息格式：legacy 纯文本帧和 typed JSON envelope
    """

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        runtime_model_name: Callable[[], str | None] | None = None,
    ):
        """初始化 WebSocket 频道。

        参数：
            config: WebSocket 配置（dict 或 WebSocketConfig 实例），包含 host/port/path/token 等
            bus: 消息总线，用于与 AgentLoop 通信
            session_manager: 会话管理器，用于读取/写入会话历史
            static_dist_path: WebUI 前端构建产物目录，若提供则同时充当静态文件服务器
            runtime_model_name: 回调函数，返回当前运行时模型名（用于 bootstrap 响应）

        内部状态：
            _subs: chat_id → {connection, ...} 订阅映射，用于 fan-out 出站消息
            _conn_chats: connection → {chat_id, ...} 反向映射，O(1) 断开清理
            _conn_default: connection → default_chat_id，legacy 帧的默认路由
            _issued_tokens: 一次性 token 池，WS 握手时消耗
            _api_tokens: 多次使用的 API token 池，REST 请求验证但不消耗
            _media_secret: 32字节随机密钥，用于 HMAC 签名媒体 URL（重启后过期）
        """
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> 订阅该 chat 的所有连接（fan-out 目标）
        self._subs: dict[str, set[Any]] = {}
        # connection -> 该连接订阅的所有 chat_id，断开时 O(1) 清理
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> 默认 chat_id，用于不带路由的 legacy 帧
        self._conn_default: dict[Any, str] = {}
        # 一次性 token：WebSocket 握手时验证并消耗
        self._issued_tokens: dict[str, float] = {}
        # 多次使用 token：HTTP API 路由验证但不会消耗
        self._api_tokens: dict[str, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._session_manager = session_manager
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        self._runtime_model_name = runtime_model_name
        self._settings_restart_sections: set[str] = set()
        # 进程内密钥，用于 HMAC 签名媒体 URL。签名 URL 即是能力凭证 ——
        # 任何人持有有效 URL 就能获取该文件，无法获取其他文件。
        # 重启后重新生成密钥，旧链接自动过期（调用方刷新会话列表即可）。
        self._media_secret: bytes = secrets.token_bytes(32)

    # -- Subscription bookkeeping（订阅管理）----------------------------------

    def _attach(self, connection: Any, chat_id: str) -> None:
        """将连接订阅到指定 chat_id（幂等操作）。

        更新两个方向的映射：
        - _subs[chat_id] 加入 connection（出站消息 fan-out 时遍历）
        - _conn_chats[connection] 加入 chat_id（断开时 O(1) 清理）
        多次调用同一个 (connection, chat_id) 对不会产生重复。
        """
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """移除连接的所有订阅（可安全多次调用）。

        连接断开时调用此方法：遍历该连接订阅的所有 chat_id，
        从 _subs 中移除该连接，如果某 chat_id 再无订阅者则删除该条目。
        同时清理 _conn_default 中的默认路由。
        """
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """重新连接后推送活跃的持续性目标状态。

        目标的元数据存储在会话 JSONL 中，可以跨网关重启存活。
        正常情况下客户端通过 goal_state / turn_end 帧看到目标状态；
        此方法让页面刷新或重连时立即恢复目标条，不需要新一轮模型调用。
        """
        if self._session_manager is None:
            return
        row = self._session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """推送当前 turn 的运行状态（同进程刷新场景）。

        如果某个 chat_id 的 turn 正在运行中（内存中有 wall clock 记录），
        向新订阅的连接推送 goal_status: running，让前端显示运行指示器。
        """
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """订阅后恢复状态（同进程刷新场景）。

        客户端订阅 chat_id 后调用，依次推送：
        1. 活跃的目标状态（goal_state）
        2. 当前 turn 的运行状态（goal_status: running/idle）
        确保重连/刷新后前端 UI 状态与后端一致。
        """
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """向单个连接发送控制事件（如 attached、error 等）。

        构造 JSON {"event": "<event>", ...fields} 并直接发送到连接。
        若连接已关闭，自动清理订阅；其他异常记录警告日志。
        """
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    # -- 配置与 SSL（配置读取与安全连接）------------------------------------

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回 WebSocket 频道的默认配置（所有选项均为默认值）。"""
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        """返回标准化后的 WebSocket 升级路径（去除尾部斜杠）。"""
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """构建 SSL 上下文（WSS 支持）。

        若配置了 ssl_certfile 和 ssl_keyfile，创建 TLS 1.2+ 的 SSLContext；
        若两者都为空则返回 None（纯 WS 模式）；
        若只配了一个则抛出 ValueError（必须同时配置）。
        """
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    # -- Token 管理（签发、验证、过期清理）----------------------------------

    _MAX_ISSUED_TOKENS = 10_000

    def _purge_expired_issued_tokens(self) -> None:
        """清理过期的签发 token（一次性 token 池）。

        遍历 _issued_tokens，移除所有过期时间早于当前的 token。
        每次签发或验证 token 时调用，防止 token 池无限增长。
        """
        now = time.monotonic()
        for token_key, expiry in list(self._issued_tokens.items()):
            if now > expiry:
                self._issued_tokens.pop(token_key, None)

    def _take_issued_token_if_valid(self, token_value: str | None) -> bool:
        """验证并消耗一个签发 token（一次性使用）。

        工作流程：
        1. 清理过期 token
        2. 从 _issued_tokens 中 pop 该 token
        3. 若 pop 成功且未过期 → 返回 True（token 被消耗）
        4. 若 token 不存在或已过期 → 返回 False

        使用单步 pop 最小化查找和移除之间的窗口（asyncio 单线程协作模型下安全）。
        """
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        expiry = self._issued_tokens.pop(token_value, None)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            return False
        return True

    def _handle_token_issue_http(self, connection: Any, request: Any) -> Any:
        """处理 token 签发 HTTP 请求（token_issue_path 端点）。

        安全策略：
        - 若配置了 token_issue_secret，需要 Bearer token 或 X-Nanobot-Auth header 认证
        - 若未配置 secret，记录安全警告但仍签发（开发模式）
        - 超过 MAX_ISSUED_TOKENS 上限时返回 429

        返回 JSON：{"token": "nbwt_...", "expires_in": <秒>}
        """
        secret = self.config.token_issue_secret.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self.logger.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        self._purge_expired_issued_tokens()
        if len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS:
            self.logger.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self._issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        self._issued_tokens[token_value] = time.monotonic() + float(self.config.token_ttl_s)

        return _http_json_response(
            {"token": token_value, "expires_in": self.config.token_ttl_s}
        )

    # -- HTTP 分发（HTTP 请求路由入口）--------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """HTTP 请求分发器 —— WebSocket 服务器的 HTTP 层核心路由。

        整个 Gateway 的 HTTP 流量都在这里分流处理，按优先级依次匹配：
        1. **Token 签发路径** (token_issue_path)：签发临时 token 的端点
        2. **Bootstrap** (/webui/bootstrap)：WebUI 初始化，签发 WS token + 返回模型名
        3. **REST API 路由**：/api/sessions、/api/settings、/api/commands 等前后端交互接口
        4. **WebSocket 升级** (主路径)：将 HTTP 升级为 WebSocket 连接（通过 _authorize_websocket_handshake）
        5. **静态文件服务** (SPA)：若配置了 dist 目录，提供前端构建产物（Vue/React SPA）

        注意：步骤 1-4 仅在请求路径匹配时处理；步骤 5 是回退方案。
        未匹配任何规则时返回 404。
        """
        got, query = _parse_request_path(request.path)

        # 1. Token 签发端点（可选，由 token_issue_secret 控制访问）
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue_http(connection, request)

        # 2. WebUI Bootstrap：签发 WS + API 双用途 token，返回会话元数据
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # 3. REST API 路由（按路径精确匹配）
        if got == "/api/sessions":
            return self._handle_sessions_list(request)

        if got == "/api/settings":
            return self._handle_settings(request)

        if got == "/api/commands":
            return self._handle_commands(request)

        if got == "/api/webui/sidebar-state":
            return self._handle_webui_sidebar_state(request)

        if got == "/api/webui/sidebar-state/update":
            return self._handle_webui_sidebar_state_update(request)

        if got == "/api/settings/update":
            return self._handle_settings_update(request)

        if got == "/api/settings/provider/update":
            return self._handle_settings_provider_update(request)

        if got == "/api/settings/web-search/update":
            return self._handle_settings_web_search_update(request)

        if got == "/api/settings/image-generation/update":
            return self._handle_settings_image_generation_update(request)

        if got == "/api/settings/cli-apps":
            return self._handle_settings_cli_apps(request)

        if got == "/api/settings/cli-apps/install":
            return await self._handle_settings_cli_apps_action(request, "install")

        if got == "/api/settings/cli-apps/update":
            return await self._handle_settings_cli_apps_action(request, "update")

        if got == "/api/settings/cli-apps/uninstall":
            return await self._handle_settings_cli_apps_action(request, "uninstall")

        if got == "/api/settings/cli-apps/test":
            return await self._handle_settings_cli_apps_action(request, "test")

        # 动态路径匹配：/api/sessions/<key>/messages
        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        # 动态路径匹配：/api/sessions/<key>/webui-thread
        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        # 动态路径匹配：/api/sessions/<key>/delete（注意：websockets HTTP 解析器仅接受 GET，
        # 因此无法暴露真正的 DELETE 方法，改用路径编码）
        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        # 签名媒体文件获取：/api/media/<sig>/<payload>
        # sig 是对 payload 的 HMAC，payload 解码为 media_dir 下的相对路径
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2))

        # 4. WebSocket 升级（频道的主要目的）
        # 仅对明确请求 Upgrade 的请求进行握手验证；否则普通 GET / 应回退到静态文件服务
        expected_ws = self._expected_path()
        if got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)

        # 5. 静态 SPA 文件服务（仅当配置了构建产物目录时启用）
        if self._static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    # -- HTTP 路由处理器（REST API 具体实现）-------------------------------

    def _check_api_token(self, request: WsRequest) -> bool:
        """验证 HTTP API 请求的 token（多次使用，不消耗）。

        从两个来源查找 token：
        1. Authorization: Bearer <token> header
        2. URL 查询参数 ?token=<token>

        与 WS 握手 token 不同，API token 验证后不会从池中移除（可多次使用），
        仅检查是否存在且未过期。调用方应先 _purge_expired_api_tokens。
        """
        self._purge_expired_api_tokens()
        token = _bearer_token(request.headers) or _query_first(
            _parse_query(request.path), "token"
        )
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    def _purge_expired_api_tokens(self) -> None:
        """清理过期的 API token（多次使用 token 池）。

        与 issued_tokens 不同，API token 可多次使用只需验证，不会被消耗。
        定时清理过期条目防止内存泄漏。
        """
        now = time.monotonic()
        for token_key, expiry in list(self._api_tokens.items()):
            if now > expiry:
                self._api_tokens.pop(token_key, None)

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        """WebUI 初始化端点 —— 签发 WS 连接和 API 调用所需的临时 token。

        安全策略：
        - 若配置了 token_issue_secret 或 static token，必须验证（无论来源 IP）
        - 无 secret 时仅允许 localhost（本地开发模式）
        - 防止 token 池无限增长：超过 MAX_ISSUED_TOKENS 上限返回 429

        返回 JSON：
        {
            "token": "<临时token>",
            "ws_path": "<WebSocket升级路径>",
            "expires_in": <过期秒数>,
            "model_name": "<当前模型名>"
        }

        同一个 token 同时注册到 _issued_tokens（WS 握手消耗）和 _api_tokens（REST 验证），
        实现"一次签发，双通道使用"。
        """
        # 当配置了 secret 时，无论来源 IP 都验证（适配反向代理场景）
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not _is_localhost(connection):
            # 无 secret 配置：仅允许 localhost（本地开发模式）
            return _http_error(403, "bootstrap is localhost-only")
        # 限制未消耗的 token 数量，防止恶意客户端耗尽内存
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        # 同一个 token 注册到两个池：WS 握手消耗一份，REST API 持续验证另一份
        self._issued_tokens[token] = expiry
        self._api_tokens[token] = expiry
        return _http_json_response(
            {
                "token": token,
                "ws_path": self._expected_path(),
                "expires_in": self.config.token_ttl_s,
                "model_name": _resolve_bootstrap_model_name(self._runtime_model_name),
            }
        )

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        """返回 WebSocket 会话列表（用于侧边栏/聊天列表）。

        仅返回 websocket: 前缀的会话（CLI/Slack 等渠道的会话不在 HTTP API 范围）。
        如果当前有 turn 正在运行，附加 run_started_at 时间戳。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        sessions = self._session_manager.list_sessions()
        # 仅暴露 WS 频道的会话（websocket: 前缀），其他渠道不在 HTTP 列表范围
        cleaned = []
        for s in sessions:
            key = s.get("key")
            if not (isinstance(key, str) and key.startswith("websocket:")):
                continue
            row = {k: v for k, v in s.items() if k != "path"}
            chat_id = key.split(":", 1)[1]
            started_at = websocket_turn_wall_started_at(chat_id)
            if started_at is not None:
                row["run_started_at"] = started_at
            cleaned.append(row)
        return _http_json_response({"sessions": cleaned})

    # -- Settings API（设置读写接口）----------------------------------------

    def _handle_settings(self, request: WsRequest) -> Response:
        """返回当前设置（GET 只读）。

        通过 settings_payload() 读取所有配置并序列化为 JSON，
        同时附加进程内的 restart-required 状态（某些设置修改后需重启网关）。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(self._with_settings_restart_state(settings_payload()))

    def _with_settings_restart_state(
        self,
        payload: dict[str, Any],
        *,
        section: str | None = None,
    ) -> dict[str, Any]:
        """维护"需要重启"状态，用于设置修改后的 UI 提示。

        当某个设置区域的修改需要重启网关时，将该 section 记录到 _settings_restart_sections。
        每次返回设置 payload 时会附带 restart_required_sections 列表，
        前端据此显示"需要重启"的提示条。
        """
        if section and payload.get("requires_restart"):
            self._settings_restart_sections.add(section)
        if self._settings_restart_sections:
            payload = dict(payload)
            payload["requires_restart"] = True
            payload["restart_required_sections"] = sorted(self._settings_restart_sections)
        else:
            payload = dict(payload)
            payload["restart_required_sections"] = []
        return payload

    def _handle_commands(self, request: WsRequest) -> Response:
        """返回内置命令面板（用于前端命令补全/提示）。

        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_webui_sidebar_state(self, request: WsRequest) -> Response:
        """读取 WebUI 侧边栏状态（展开/折叠、拖拽宽度等）。

        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(read_webui_sidebar_state())

    def _handle_webui_sidebar_state_update(self, request: WsRequest) -> Response:
        """更新 WebUI 侧边栏状态。

        通过查询参数 ?state=<JSON> 接收新状态，写入本地存储。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_state = _query_first(query, "state")
        if raw_state is None:
            return _http_error(400, "missing state")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError:
            return _http_error(400, "state must be JSON")
        if not isinstance(decoded, dict):
            return _http_error(400, "state must be an object")
        try:
            state = write_webui_sidebar_state(decoded)
        except ValueError as e:
            return _http_error(400, str(e))
        except OSError:
            self.logger.exception("failed to write webui sidebar state")
            return _http_error(500, "failed to write sidebar state")
        return _http_json_response(state)

    def _handle_settings_update(self, request: WsRequest) -> Response:
        """更新 Agent 运行时设置。

        通过查询参数传递设置项，调用 update_agent_settings 写入配置。
        更新后可能标记 runtime section 需要重启。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        try:
            payload = update_agent_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(
            self._with_settings_restart_state(payload, section="runtime")
        )

    def _handle_settings_provider_update(self, request: WsRequest) -> Response:
        """更新 Provider（模型提供商）设置。

        调用 update_provider_settings 写入配置，
        可能标记 image section 需要重启。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        try:
            payload = update_provider_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(self._with_settings_restart_state(payload, section="image"))

    def _handle_settings_web_search_update(self, request: WsRequest) -> Response:
        """更新 Web 搜索设置。

        调用 update_web_search_settings 写入配置，
        可能标记 web section 需要重启。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        try:
            payload = update_web_search_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(self._with_settings_restart_state(payload, section="web"))

    def _handle_settings_image_generation_update(self, request: WsRequest) -> Response:
        """更新图片生成设置。

        调用 update_image_generation_settings 写入配置，
        可能标记 image section 需要重启。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        try:
            payload = update_image_generation_settings(query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(self._with_settings_restart_state(payload, section="image"))

    def _handle_settings_cli_apps(self, request: WsRequest) -> Response:
        """返回 CLI Apps 列表（当前安装的应用）。

        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            payload = cli_apps_payload()
        except Exception:
            self.logger.exception("failed to load CLI Apps payload")
            return _http_error(500, "failed to load CLI Apps")
        return _http_json_response(payload)

    async def _handle_settings_cli_apps_action(self, request: WsRequest, action: str) -> Response:
        """执行 CLI Apps 操作（install/update/uninstall/test）。

        使用 asyncio.to_thread 在后台线程执行避免阻塞事件循环。
        action 参数通过 URL 路径区分。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        try:
            payload = await asyncio.to_thread(cli_apps_action, action, query)
        except WebUISettingsError as e:
            return _http_error(e.status, e.message)
        except Exception as e:
            status = getattr(e, "status", 500)
            message = getattr(e, "message", str(e))
            if status >= 500:
                self.logger.exception("CLI Apps action '{}' failed", action)
            return _http_error(status, message)
        return _http_json_response(payload)

    # -- 会话数据与媒体文件服务 --------------------------------------------

    @staticmethod
    def _is_websocket_channel_session_key(key: str) -> bool:
        """判断是否为 websocket 频道的会话 key（以 websocket: 开头）。

        用于确保 REST API 仅暴露 WS 频道的会话，
        防止 CLI/Slack/Telegram 等渠道的会话通过 HTTP API 被访问。
        """
        return key.startswith("websocket:")

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        """返回指定会话的完整消息历史。

        从 session_manager 读取 JSONL 文件，清理子 Agent 消息、替换媒体路径为签名 URL。
        仅处理 websocket: 前缀的会话 key。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        # 仅 websocket: 前缀的会话可通过 HTTP API 访问
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self._session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        # 为持久化的用户消息中的媒体路径替换为签名 URL，
        # 让客户端能渲染预览图（原始磁盘路径不暴露给前端）
        self._augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        """返回 WebUI thread 数据（用于前端线程渲染）。

        从会话文件中读取并构建 WebUI 线程响应，
        对用户消息的媒体路径进行签名处理。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self._augment_transcript_user_media,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        return _http_json_response(data)

    def _try_append_webui_transcript(self, chat_id: str, wire: dict[str, Any]) -> None:
        """尝试追加 WebUI 转录记录（用于调试和会话重放）。

        将流式数据的快照追加到 websocket:<chat_id> 的转录文件中。
        失败时静默忽略，不影响主流程。
        """
        sk = f"websocket:{chat_id}"
        try:
            dup = json.loads(json.dumps(wire, ensure_ascii=False))
            append_transcript_object(sk, dup)
        except (ValueError, TypeError) as e:
            self.logger.warning("webui transcript append failed: {}", e)

    def _augment_transcript_user_media(self, paths: list[str]) -> list[dict[str, Any]]:
        """将转录中的媒体路径列表转换为签名 URL 列表。

        对每个路径进行签名或暂存处理，返回包含 url、kind、name 的字典列表。
        用于 WebUI thread 渲染时展示历史图片/视频。
        """
        out: list[dict[str, Any]] = []
        for pstr in paths:
            path = Path(pstr)
            att = self._sign_or_stage_media_path(path)
            if att is None:
                continue
            mime, _ = mimetypes.guess_type(path.name)
            kind = "video" if mime and mime.startswith("video/") else "image"
            out.append(
                {"kind": kind, "url": att["url"], "name": att.get("name", path.name)},
            )
        return out

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        """处理入站消息（覆写 BaseChannel._handle_message）。

        对于 webui 来源的消息，先追加转录记录，再调用父类方法将消息写入 MessageBus.inbound。
        父类方法会：
        1. 分配/复用 session
        2. 将 InboundMessage 发布到 send_queue
        3. 最终由 AgentLoop 消费处理
        """
        meta = metadata or {}
        if meta.get("webui"):
            user_obj: dict[str, Any] = {
                "event": "user",
                "chat_id": chat_id,
                "text": content,
            }
            if media:
                user_obj["media_paths"] = list(media)
            cli_apps = meta.get("cli_apps")
            if isinstance(cli_apps, list) and cli_apps:
                user_obj["cli_apps"] = cli_apps
            self._try_append_webui_transcript(chat_id, user_obj)
        await super()._handle_message(
            sender_id,
            chat_id,
            content,
            media,
            metadata,
            session_key,
            is_dm,
        )

    def _augment_media_urls(self, payload: dict[str, Any]) -> None:
        """将 payload 中每条消息的 media 路径替换为 media_urls（签名 URL）。

        遍历 messages 列表，对每条包含 media 字段的消息：
        - 保留原始 media 列表
        - 新增 media_urls 列表（仅包含能成功签名的路径）
        - 始终删除原始 media 字段（防止泄漏服务器文件系统布局）

        找不到的路径（如文件被删除）会被静默跳过，前端使用占位图。
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            media = msg.get("media")
            if not isinstance(media, list) or not media:
                continue
            urls: list[dict[str, str]] = []
            for entry in media:
                if not isinstance(entry, str) or not entry:
                    continue
                signed = self._sign_media_path(Path(entry))
                if signed is None:
                    continue
                urls.append({"url": signed, "name": Path(entry).name})
            if urls:
                msg["media_urls"] = urls
            # 始终移除原始磁盘路径，不暴露到前端
            msg.pop("media", None)

    # -- 媒体文件签名与安全访问 --------------------------------------------

    def _sign_media_path(self, abs_path: Path) -> str | None:
        """对媒体文件路径进行 HMAC 签名，返回 /api/media/<sig>/<payload> URL。

        工作流程：
        1. 验证路径在 media_dir 目录内（防止目录穿越）
        2. 计算相对路径的 base64url 编码作为 payload
        3. 用 _media_secret 对 payload 做 HMAC-SHA256，取前 16 字节作为签名
        4. 拼接为 /api/media/<base64url(sig)>/<payload> URL

        签名 URL 自带认证能力 —— 任何人持有有效 URL 就能获取该文件，
        但无法获取其他文件（签名将路径绑定到 URL）。
        重启后 _media_secret 更换，所有旧 URL 自动失效。
        """
        try:
            media_root = get_media_dir().resolve()
            rel = abs_path.resolve().relative_to(media_root)
        except (OSError, ValueError):
            return None
        payload = _b64url_encode(rel.as_posix().encode("utf-8"))
        mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        return f"/api/media/{_b64url_encode(mac)}/{payload}"

    def _sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        """签名或暂存媒体路径，返回 {"url": ..., "name": ...}。

        两种场景：
        - 入站媒体（已位于 media_dir 内）→ 直接签名
        - 出站媒体（Agent 生成的文件，可能在任意位置）→ 先复制到 websocket media 桶再签名

        确保浏览器能通过签名 URL 获取任何文件，不暴露任意文件系统路径。
        """
        signed = self._sign_media_path(path)
        if signed is not None:
            return {"url": signed, "name": path.name}
        try:
            if not path.is_file():
                return None
            media_dir = get_media_dir("websocket")
            safe_name = safe_filename(path.name) or "attachment"
            staged = media_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
            shutil.copyfile(path, staged)
        except OSError as exc:
            self.logger.warning("failed to stage outbound media {}: {}", path, exc)
            return None
        signed = self._sign_media_path(staged)
        if signed is None:
            return None
        return {"url": signed, "name": path.name}

    def _handle_media_fetch(self, sig: str, payload: str) -> Response:
        """服务媒体文件获取请求（/api/media/<sig>/<payload>）。

        安全流程：
        1. 验证 HMAC 签名（sig 是否正确对应 payload）
        2. 解码 payload 为相对路径，验证在 media_dir 内
        3. 读取文件内容
        4. 验证 MIME 类型在白名单内（防止浏览器 MIME-sniffing）
        5. 返回文件字节 + 长期缓存头 + nosniff header

        MIME 白名单限制为 _MEDIA_ALLOWED_MIMES，不在白名单的统一降级为 octet-stream。
        """
        try:
            provided_mac = _b64url_decode(sig)
        except (ValueError, binascii.Error):
            return _http_error(401, "invalid signature")
        expected_mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            return _http_error(401, "invalid signature")
        try:
            rel_bytes = _b64url_decode(payload)
            rel_str = rel_bytes.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return _http_error(400, "invalid payload")
        # 即使 HMAC 被绕过了，也要防御目录穿越
        try:
            media_root = get_media_dir().resolve()
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError):
            return _http_error(404, "not found")
        if not candidate.is_file():
            return _http_error(404, "not found")
        try:
            body = candidate.read_bytes()
        except OSError:
            return _http_error(500, "read error")
        mime, _ = mimetypes.guess_type(candidate.name)
        if mime not in _MEDIA_ALLOWED_MIMES:
            mime = "application/octet-stream"
        return _http_response(
            body,
            content_type=mime,
            extra_headers=[
                ("Cache-Control", "private, max-age=31536000, immutable"),
                # 配合 MIME 白名单：防止浏览器将 octet-stream 嗅探为可执行 HTML
                ("X-Content-Type-Options", "nosniff"),
            ],
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        """删除指定 WebSocket 会话。

        从 session_manager 删除 JSONL 文件，同时清理 WebUI thread 数据。
        需要验证 API token。
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        # 仅 websocket 频道的会话可以在这里删除
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        deleted = self._session_manager.delete_session(decoded_key)
        delete_webui_thread(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    def _serve_static(self, request_path: str) -> Response | None:
        """提供 SPA 静态文件服务。

        工作流程：
        1. 解析请求路径，防止目录穿越（.. 和绝对路径被拒绝）
        2. 在 _static_dist_path 下查找文件
        3. 若文件存在 → 返回文件内容（hash 命名的资源设长期缓存）
        4. 若文件不存在 → 回退到 index.html（SPA history 模式）

        仅当 _static_dist_path 被配置时启用（开发模式通常不配置）。
        """
        assert self._static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        # 拒绝路径穿越和绝对路径
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self._static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self._static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            # SPA history 模式回退：未知路由返回 index.html
            index = self._static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self.logger.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        # hash 命名的构建产物可长期缓存；index.html 必须保持新鲜
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )

    # -- WebSocket 握手与服务器生命周期 ------------------------------------

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        """验证 WebSocket 握手 token。

        策略（按优先级）：
        1. 若配置了 static_token：优先匹配 static token（hmac.compare_digest），
           再尝试匹配签发 token
        2. 若 websocket_requires_token=True：必须有有效 token
        3. 若无强制要求：token 存在时仍会消耗（但不拒绝无 token 的连接）

        返回 None 表示握手通过（允许升级），否则返回 401 响应。
        """
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._take_issued_token_if_valid(supplied)
        return None

    async def start(self) -> None:
        """启动 WebSocket 服务器。

        工作流程：
        1. 重定向 websockets 库的日志到 nanobot 日志系统
        2. 构建 SSL 上下文（如果配置了证书）
        3. 注册 process_request 回调（HTTP 请求 → _dispatch_http 分流）
        4. 注册 handler 回调（WebSocket 连接 → _connection_loop 处理）
        5. 使用 websockets.serve() 启动服务器，配置 max_size、ping 等参数
        6. 进入事件循环等待 _stop_event

        scheme 根据是否配置 SSL 自动选择 ws:// 或 wss://。
        """
        from nanobot.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(
            connection: ServerConnection,
            request: WsRequest,
        ) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}://{}:{}{}",
            scheme,
            self.config.host,
            self.config.port,
            self.config.path,
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}://{}:{}{}",
                scheme,
                self.config.host,
                self.config.port,
                _normalize_config_path(self.config.token_issue_path),
            )

        async def runner() -> None:
            async with serve(
                handler,
                self.config.host,
                self.config.port,
                process_request=process_request,
                max_size=self.config.max_message_bytes,
                ping_interval=self.config.ping_interval_s,
                ping_timeout=self.config.ping_timeout_s,
                ssl=ssl_context,
            ):
                assert self._stop_event is not None
                await self._stop_event.wait()

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        """WebSocket 连接生命周期管理（单连接循环）。

        生命周期：
        1. 解析连接参数（client_id）—— 匿名连接自动生成 ID
        2. 生成 default_chat_id（UUID）
        3. 发送 "ready" 事件（携带 chat_id 和 client_id）
        4. 注册连接 → default_chat_id 订阅
        5. 恢复状态（活跃目标、运行中 turn）
        6. 进入消息循环：
           - 二进制帧解码为 UTF-8
           - 若解析为 typed envelope（JSON 带 type 字段）→ _dispatch_envelope 处理
           - 否则按 legacy 文本帧处理 → _handle_message 写入 MessageBus
        7. 连接关闭或异常时 → _cleanup_connection 清理订阅
        """
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                    },
                    ensure_ascii=False,
                )
            )
            # 仅在 ready 成功发送后注册订阅，避免乱序消息
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WebSocket 已在握手时通过 token 认证，因此不适用配对逻辑。
                # 按非 DM 模式处理，避免向已认证客户端发送配对码。
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    def _save_envelope_media(
        self,
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """解码并持久化消息 envelope 中的媒体数据。

        接收格式：list[{"data_url": str, "name"?: str | None}]

        验证规则：
        - 图片最多 4 张（_MAX_IMAGES_PER_MESSAGE）
        - 视频最多 1 个（_MAX_VIDEOS_PER_MESSAGE）
        - 图片单文件 ≤ 8MB（_MAX_IMAGE_BYTES）
        - 视频单文件 ≤ 20MB（_MAX_VIDEO_BYTES）
        - 仅允许 _UPLOAD_MIME_ALLOWED 中的白名单 MIME

        返回值：
        - 成功：(paths_list, None) —— paths 为保存后的文件路径列表
        - 失败：([], reason) —— reason 是 short token（如 "too_many_images", "size", "mime"），
          用于前端 UI 本地化。失败时已保存的临时文件会被自动清理。
        """
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            """失败时清理已写入的临时文件并返回错误原因。"""
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "failed to unlink partial media {}: {}", p, exc
                    )
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """分发 typed envelope 消息（新式 JSON 协议）。

        支持的 type：
        - "new_chat"：创建新 chat_id，自动订阅并回复 attached
        - "attach"：订阅到指定 chat_id，回复 attached
        - "message"：发送消息到指定 chat_id（含媒体处理）

        对于 "message" 类型：
        1. 验证 chat_id 和 content
        2. 处理 media 字段（解析 base64 data_url，保存到 media_dir）
        3. 先 attach 再 hydrate（首次使用无需单独 attach）
        4. 构造 metadata（webui flag、cli_apps、image_generation 等）
        5. 调用 _handle_message 写入 MessageBus
        """
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason="malformed",
                    )
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason=reason,
                    )
                    return

            # 允许纯图片消息（有媒体时 content 可为空）
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return

            # 首次使用时自动 attach，客户端无需单独发送 attach 帧
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
            cli_apps = normalize_cli_app_mentions(envelope.get("cli_apps"))
            if cli_apps:
                metadata["cli_apps"] = cli_apps
            image_generation = envelope.get("image_generation")
            if isinstance(image_generation, dict) and image_generation.get("enabled") is True:
                aspect_ratio = image_generation.get("aspect_ratio")
                metadata["image_generation"] = {
                    "enabled": True,
                    "aspect_ratio": aspect_ratio if isinstance(aspect_ratio, str) else None,
                }
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    # -- 服务器停止与资源清理 -----------------------------------------------

    async def stop(self) -> None:
        """优雅停止 WebSocket 服务器。

        工作流程：
        1. 设置 _running = False
        2. 触发 _stop_event，让 server context 退出
        3. 等待 _server_task 完成
        4. 清理所有内存状态（订阅表、连接表、token 池）
        """
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()
        self._api_tokens.clear()

    # -- 出站消息发送（AgentLoop → WebSocket 客户端）-----------------------

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """安全地向单个连接发送消息帧（带自动清理）。

        若连接已关闭（ConnectionClosed），自动调用 _cleanup_connection 清理订阅；
        其他异常记录日志后重新抛出。
        label 用于日志上下文标识消息类型。
        """
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        """发送出站消息到 chat_id 的所有订阅者（fan-out 核心方法）。

        这是 MessageBus.outbound → WebSocket 客户端的桥梁，根据 metadata 分派不同事件：

        优先级处理（依次检查）：
        1. **_runtime_model_updated**：广播运行时模型变更（全局 fan-out）
        2. **_goal_state_sync**：推送目标状态同步
        3. **_goal_status**：推送目标运行状态（running/idle）
        4. **_turn_end**：发送 turn 结束事件（含延迟、目标状态）
        5. **_session_updated**：通知会话元数据变更
        6. **_file_edit_events**：推送文件编辑事件
        7. **普通消息**：构造消息帧（含 text、media_urls、tool_events、agent_ui 等）

        对于普通消息：
        - 媒体路径通过 _sign_or_stage_media_path 转为签名 URL
        - _tool_hint 标记为 "tool_hint" kind
        - _progress 标记为 "progress" kind
        - 所有消息追加到 WebUI transcript
        """
        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return

        # 快照订阅者集合，防止迭代中 ConnectionClosed 清理引发问题
        conns = list(self._subs.get(msg.chat_id, ()))
        if not conns:
            if (
                msg.metadata.get("_progress")
                or msg.metadata.get("_file_edit_events")
                or msg.metadata.get("_turn_end")
                or msg.metadata.get("_session_updated")
                or msg.metadata.get("_goal_status")
                or msg.metadata.get("_goal_state_sync")
            ):
                self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
            else:
                self.logger.warning("no active subscribers for chat_id={}", msg.chat_id)
            return
        if msg.metadata.get("_goal_state_sync"):
            blob = msg.metadata.get("goal_state")
            await self.send_goal_state(msg.chat_id, blob if isinstance(blob, dict) else {"active": False})
            return
        if msg.metadata.get("_goal_status"):
            status = msg.metadata.get("goal_status")
            if status in ("running", "idle"):
                started_raw = msg.metadata.get("started_at", msg.metadata.get("goal_started_at"))
                await self.send_goal_status(
                    msg.chat_id,
                    status,
                    started_at=float(started_raw) if isinstance(started_raw, int | float) else None,
                )
            return
        # Agent 已完成当前 turn 的全部处理
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            gs = msg.metadata.get("goal_state")
            gs_blob = gs if isinstance(gs, dict) else None
            await self.send_turn_end(msg.chat_id, latency_ms=lat_i, goal_state=gs_blob)
            return
        if msg.metadata.get("_session_updated"):
            scope = msg.metadata.get("_session_update_scope")
            await self.send_session_updated(
                msg.chat_id,
                scope=scope if isinstance(scope, str) else None,
            )
            return
        if msg.metadata.get("_file_edit_events"):
            payload: dict[str, Any] = {
                "event": "file_edit",
                "chat_id": msg.chat_id,
                "edits": msg.metadata["_file_edit_events"],
            }
            self._try_append_webui_transcript(msg.chat_id, payload)
            raw = json.dumps(payload, ensure_ascii=False)
            for connection in conns:
                await self._safe_send_to(connection, raw, label=" ")
            return
        text = msg.content
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": text,
        }
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if msg.metadata.get("_tool_events"):
            payload["tool_events"] = msg.metadata["_tool_events"]
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # 工具调用提示和进度信息标记为从属追踪行而非对话回复
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        self._try_append_webui_transcript(msg.chat_id, payload)
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    # -- 流式数据推送（Streaming Protocols）---------------------------------

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """推送一个 reasoning 增量块（模型思考过程流式展示）。

        与 send_delta 镜像设计，客户端收到 reasoning_delta 开始渲染 thought bubble，
        在 reasoning_end 到达前持续更新。渲染在 assistant bubble 上方，带闪烁头。
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns or not delta:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """结束当前 reasoning 流段（关闭思考气泡）。"""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning_end ")

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """推送流式文本增量（LLM token-by-token 输出）。

        meta._stream_end 为 True 时发送 stream_end 事件而非 delta；
        否则发送 delta 事件携带 text 内容。
        所有事件携带 stream_id 用于多流复用。
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        meta = metadata or {}
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
    ) -> None:
        """通知客户端当前 turn 处理完毕。

        可附带：
        - latency_ms：本 turn 耗时（毫秒）
        - goal_state：如果目标状态发生变化，附带最新状态

        前端收到后展示 "done" 指示器、延迟信息、更新目标条。
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        if goal_state is not None:
            body["goal_state"] = goal_state
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """推送持续目标状态快照到指定 chat_id（多 chat 隔离）。

        目标状态包含 name、active、progress 等字段，
        前端在顶部渲染目标进度条。
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """通知订阅的客户端 turn 开始或完成（墙钟时间提示）。

        status 为 "running" 或 "idle"。
        running 时可附带 started_at 时间戳，前端据此显示运行持续时间。
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_session_updated(self, chat_id: str, *, scope: str | None = None) -> None:
        """通知客户端会话元数据在 turn 外发生了变化。

        scope 指示变更范围（如 "title"、"metadata"），前端据此刷新 UI。
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        if scope:
            body["scope"] = scope
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """广播运行时模型变更到所有 WS 连接（全局 fan-out）。

        不走 chat_id 订阅，直接遍历所有已连接客户端。
        前端收到后更新模型选择器 UI。
        """
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_model_updated ")
