"""
AgentBus Platform Adapter for Hermes Gateway.

Connects Hermes to an AgentBus WebSocket network so bus agents (Oracle,
HAL, Mariana, etc.) can message Hermes and receive replies as if they
were a native messaging platform (Telegram, Discord, etc.).

── Architecture ──────────────────────────────────────────────────────────

The AgentBus network uses a star topology: a central WebSocket server
(``server_ws.py``) routes ``new_message`` events between connected agents.
Each agent maintains a single persistent WebSocket connection to the
server.  This adapter implements one such client connection inside the
Hermes gateway process.

    ┌──────────────┐     WebSocket      ┌──────────────┐
    │  Hermes      │◄──────────────────►│  AgentBus     │
    │  Gateway     │   register +       │  Server       │
    │  (Faye)      │   new_message      │  (ws://...)   │
    └──────┬───────┘   message_ack      └──────┬────────┘
           │                                    │
    ┌──────┴───────┐                   ┌────────┴────────┐
    │ AgentBus     │                   │  Oracle  │  HAL │
    │ Adapter      │                   │  (other agents) │
    │ (adapter.py) │                   └─────────────────┘
    └──────────────┘

── Message Flow ──────────────────────────────────────────────────────────

INBOUND (bus agent → Hermes):

    1. Server pushes ``{"type":"new_message","message":{...}}`` via WS
    2. ``_read_loop`` (background task) receives and parses it
    3. ``_on_new_message`` builds a ``MessageEvent`` and fires
       ``self.handle_message(event)`` in a **background asyncio task**
       (never awaited — see Deadlock Warning below)
    4. The gateway processes the message (LLM call, tool use, etc.)
    5. Reply is sent back via ``adapter.send()``

OUTBOUND (Hermes → bus agent):

    1. Gateway calls ``adapter.send(chat_id=target_agent_id, content=...)``
    2. ``send()`` acquires ``_send_lock`` and writes to the WebSocket
    3. Server receives, stores, and routes to the target agent
    4. Server sends ``message_ack`` back (logged but NOT awaited here)

── Concurrency & the Deadlock Warning ────────────────────────────────────

There is ONE websocket connection with TWO coroutines sharing it:

    • ``_read_loop``   — calls ``async for raw in self._ws:`` (reads ALL
                         messages, including ACKs, pongs, agent_joined…)
    • ``handle_message`` (via ``_on_new_message`` → ``create_task``) —
                         processes the LLM conversation and calls
                         ``adapter.send()`` to reply

 ⚠  NEVER await ``handle_message()`` inside ``_read_loop``.

    If ``_on_new_message`` does ``await self.handle_message(event)``,
    the call chain becomes:

        _read_loop ── await _on_new_message
                         └── await handle_message
                               └── await send()
                                     └── needs _read_loop to read ACK
                                         → DEADLOCK

    The fix: ``_on_new_message`` uses ``asyncio.create_task()`` so the
    handler runs concurrently and ``_read_loop`` stays free to read.

 ⚠  NEVER call ``self._ws.recv()`` directly.

    ``_read_loop`` already owns ``async for raw in self._ws:`` (which
    internally calls ``recv()``).  A second ``recv()`` from ``send()``
    raises ``RuntimeError: cannot call recv while another coroutine is
    already running recv``.  ACK-waiting was removed — ``send()`` just
    writes and returns; the websocket library and TCP guarantee delivery.

── Configuration ─────────────────────────────────────────────────────────

In ``config.yaml`` (recommended)::

    gateway:
      platforms:
        agentbus:
          enabled: true
          extra:
            token: "<shared-bus-token>"
            server: "ws://<host>:9876"
            agent_id: "hermes-faye"
            name: "Faye"
            skills: "assistant,analysis,writing,research,code"

Environment variable fallbacks (used when ``config.extra`` keys are absent):
    AGENT_BUS_TOKEN
    AGENT_BUS_SERVER
    AGENT_BUS_AGENT_ID
    AGENT_BUS_NAME
    AGENT_BUS_SKILLS

── Authorisation ─────────────────────────────────────────────────────────

This adapter registers ``allow_all_env="AGENT_BUS_ALLOW_ALL_USERS"`` and
``allowed_users_env="AGENT_BUS_ALLOWED_USERS"``.  See
``gateway/authz_mixin.py`` ``_is_user_authorized()`` for how the gateway
uses these to decide whether to accept or reject an incoming message
(default: pairing handshake).

── Registration (plugin system) ──────────────────────────────────────────

The ``register()`` function at the bottom of this file is the Hermes plugin
entry point.  It is discovered and called by ``discover_plugins()`` →
``_load_plugin()`` in ``hermes_cli/plugins.py``.  The ``PluginContext``
receives the ``PlatformEntry`` and registers it in the global
``gateway/platform_registry.py`` singleton, making the adapter available to
``gateway/run.py`` when it connects the platform.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger("gateway.platforms.agentbus")

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

# ── Helpers ──────────────────────────────────────────────────────────────


def _get_config(key: str, default: str = "") -> str:
    """Read a config value from env var as fallback (for check_fn/validate)."""
    env_map = {
        "token": "AGENT_BUS_TOKEN",
        "server": "AGENT_BUS_SERVER",
        "agent_id": "AGENT_BUS_AGENT_ID",
        "name": "AGENT_BUS_NAME",
        "skills": "AGENT_BUS_SKILLS",
    }
    env_var = env_map.get(key)
    if env_var:
        val = os.environ.get(env_var, "")
        if val:
            return val
    return default


def check_requirements() -> tuple[bool, str]:
    """Verify that the websockets package is available."""
    if websockets is None:
        return False, "Missing 'websockets' package. Run: pip install websockets"
    return True, ""


def is_connected() -> bool:
    """Return whether the adapter has a live connection (stub for registry)."""
    return False


def validate_config(config: Any) -> bool:
    """Validate platform config. Returns True if valid."""
    token = _get_config("token")
    if not token:
        return False
    return True


def _env_enablement() -> dict[str, Any]:
    """Seed PlatformConfig.extra from env vars so env-only setups register.
    
    Only includes env vars that are NOT set in config.yaml extra,
    so explicit config values take precedence.
    """
    extras: dict[str, Any] = {}
    token = os.environ.get("AGENT_BUS_TOKEN", "")
    if token:
        extras["token"] = token
    server = os.environ.get("AGENT_BUS_SERVER", "")
    if server:
        extras["server"] = server
    agent_id = os.environ.get("AGENT_BUS_AGENT_ID", "")
    if agent_id:
        extras["agent_id"] = agent_id
    skills = os.environ.get("AGENT_BUS_SKILLS", "")
    if skills:
        extras["skills"] = skills
    return extras


# ── Adapter ──────────────────────────────────────────────────────────────


class AgentBusAdapter(BasePlatformAdapter):
    """Gateway adapter for AgentBus WebSocket network.

    Connects to the AgentBus server, registers this agent,
    and relays messages between bus peers and the Hermes gateway.
    """

    supports_code_blocks: bool = False

    def __init__(self, config: Any) -> None:
        super().__init__(config, Platform("agentbus"))

        # Config: extra from config.yaml, fall back to env vars
        extra = getattr(config, "extra", {}) or {}
        logger.info(
            "AgentBus config extra: %s (type=%s)",
            {k: v for k, v in extra.items() if k != "token"},
            type(extra).__name__,
        )

        self._bus_token: str = (
            extra.get("token")
            or os.environ.get("AGENT_BUS_TOKEN")
            or ""
        )
        self._server_url: str = (
            extra.get("server")
            or os.environ.get("AGENT_BUS_SERVER")
            or "ws://100.64.0.9:9876"
        )
        self._agent_id: str = (
            extra.get("agent_id")
            or os.environ.get("AGENT_BUS_AGENT_ID")
            or "hermes-faye"
        )
        self._display_name: str = (
            extra.get("name")
            or self._agent_id
        )
        self._skills: list[str] = (
            [s.strip() for s in extra.get("skills", "").split(",") if s.strip()]
            or [
                s.strip()
                for s in os.environ.get("AGENT_BUS_SKILLS", "").split(",")
                if s.strip()
            ]
            or ["assistant"]
        )

        # WebSocket state
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        self._connected = False
        self._send_lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect to the AgentBus server and register."""
        if self._connected:
            return True

        if websockets is None:
            logger.error("websockets package not installed")
            return False

        if not self._bus_token:
            logger.error("AgentBus token not configured")
            return False

        try:
            self._ws = await websockets.connect(self._server_url)
            logger.info(
                "Connected to %s as %s ('%s')",
                self._server_url,
                self._agent_id,
                self._display_name,
            )

            # Build agent card
            card = {
                "name": self._display_name,
                "skills": self._skills,
                "modalities": ["text"],
            }

            # Register
            register_msg = {
                "type": "register",
                "agent_id": self._agent_id,
                "token": self._bus_token,
                "card": card,
            }
            await self._ws.send(json.dumps(register_msg))

            # Read registration response
            resp_raw = await self._ws.recv()
            resp = json.loads(resp_raw)
            if resp.get("status") != "ok":
                error_msg = resp.get("message", "Unknown registration error")
                logger.error("Registration failed: %s", error_msg)
                await self._ws.close()
                self._ws = None
                return False

            # Read agents_list (always follows registration response)
            await self._ws.recv()

            self._connected = True

            # Start background reader for incoming messages
            self._reader_task = asyncio.create_task(
                self._read_loop(),
                name=f"agentbus-reader-{self._agent_id}",
            )

            logger.info(
                "✅ AgentBus adapter ready: %s (%s)",
                self._display_name,
                self._agent_id,
            )
            return True

        except Exception as exc:
            logger.error("Connection failed: %s", exc)
            if self._ws:
                await self._ws.close()
                self._ws = None
            return False

    async def disconnect(self) -> None:
        """Disconnect from the AgentBus server."""
        self._connected = False

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info("AgentBus adapter disconnected: %s", self._agent_id)

    # ── Sending ──────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to a specific agent on the bus.

        ``chat_id`` is the target agent_id, or empty for broadcast.

        Does NOT wait for a server ACK — websockets.send() is reliable for
        delivery and waiting for ACKs from a separate reader coroutine
        introduces deadlock risks. The gateway base class handles retries.
        """
        if not self._connected or not self._ws:
            return SendResult(
                success=False,
                error="Not connected to AgentBus",
                retryable=True,
            )

        msg_payload = {
            "type": "message",
            "message": {
                "source": self._agent_id,
                "target": chat_id if chat_id else "",
                "payload": content,
                "type": "text",
            },
        }

        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(msg_payload))
            return SendResult(success=True)
        except Exception as exc:
            logger.error("Send failed to %s: %s", chat_id, exc)
            return SendResult(
                success=False,
                error=str(exc),
                retryable=True,
            )

    # ── Message handling ─────────────────────────────────────────────────

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Get info about a chat (agent on the bus).

        Returns basic info about the agent identified by chat_id.
        """
        return {
            "name": chat_id,
            "type": "dm",
        }

    async def _read_loop(self) -> None:
        """Background reader: receive messages from the bus."""
        try:
            async for raw in self._ws:
                if not self._connected:
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "new_message":
                    await self._on_new_message(data)
                elif msg_type == "agent_joined":
                    logger.info("Agent joined: %s", data.get("agent_id"))
                elif msg_type == "agent_left":
                    logger.info("Agent left: %s", data.get("agent_id"))
                elif msg_type == "agents_list":
                    # Periodic agent list refresh (ignore)
                    pass
                # Ignore other types (ping/pong handled by server)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self._connected:
                logger.warning("Reader error: %s", exc)
        finally:
            self._connected = False

    async def _on_new_message(self, data: dict) -> None:
        """Handle an incoming message from another agent."""
        message = data.get("message", {})
        source = message.get("source", "unknown")
        payload = message.get("payload", "")
        msg_type = message.get("type", "text")
        msg_id = message.get("id", "")

        if msg_type != "text":
            return  # Only handle text for now

        if not payload:
            return

        logger.debug("Message from %s: %.80s", source, payload)

        # Build a MessageEvent for the gateway message handler.
        # Use the source agent_id as the chat_id so replies route back.
        from gateway.session import SessionSource
        from gateway.platforms.base import MessageEvent

        event = MessageEvent(
            source=SessionSource(
                platform=self.platform,
                chat_id=source,
                user_id=source,
                thread_id=source,
                chat_type="dm",
            ),
            text=payload,
            message_id=msg_id,
            raw_message=data,
        )

        # Dispatch to the gateway's message handler in a background task
        # so _read_loop stays free to process ACKs and other messages.
        # Awaiting here would deadlock _read_loop when handle_message
        # eventually calls self.send() and waits for an ACK future that
        # only _read_loop can resolve.
        asyncio.create_task(
            self.handle_message(event),
            name=f"agentbus-handle-{msg_id}",
        )


# ── Standalone sender (for out-of-process cron delivery) ─────────────────


def _standalone_send(
    channel: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a message from a cron job (no gateway running).

    Uses HTTP API to post a message to the bus.
    """
    import httpx

    token = _get_config("token")
    server = _get_config("server") or "ws://100.64.0.9:9876"
    api_base = server.replace("ws://", "http://").replace("wss://", "https://")

    # Only keep host:port
    if "://" in api_base:
        parts = api_base.split("/")
        api_base = "/".join(parts[:3])  # http://host:port

    if not token:
        return {"success": False, "error": "AgentBus token not configured"}

    try:
        resp = httpx.post(
            f"{api_base}/send",
            json={
                "token": token,
                "message": {
                    "type": "text",
                    "source": "hermes-faye",
                    "target": channel,
                    "payload": text,
                },
            },
            timeout=15,
        )
        resp.raise_for_status()
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ── Plugin entry point ───────────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point: called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="agentbus",
        label="AgentBus",
        adapter_factory=lambda cfg: AgentBusAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["AGENT_BUS_TOKEN"],
        install_hint="pip install websockets httpx   # already in Hermes venv",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="AGENT_BUS_HOME_AGENT",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="AGENT_BUS_ALLOWED_USERS",
        allow_all_env="AGENT_BUS_ALLOW_ALL_USERS",
        max_message_length=10000,
        emoji="🤖",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via AgentBus, a multi-agent messaging network. "
            "Other agents send you messages through the bus. "
            "Keep responses clear and concise. Use markdown formatting."
        ),
    )
