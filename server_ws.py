"""AgentBus WebSocket Server — Meeting point for agents.

Agents connect via WebSocket, authenticate with their token,
and receive messages in real time. The server routes messages
only to agents that share the same token (private channel).

Architecture:
  ┌────────────┐  WS connect   ┌──────────────────┐
  │ Agent A    │──────────────▶│                  │
  │ token=abc  │               │   AgentBus       │
  │ id=inv     │◀══════════════│   WS Server      │
  └────────────┘   broadcast   │   :9876          │
                               │                  │
  ┌────────────┐  WS connect   │  ┌────────────┐  │
  │ Agent B    │──────────────▶│  │token=abc   │  │
  │ token=abc  │◀══════════════│  │private ch. │  │
  │ id=writer  │   broadcast   │  └────────────┘  │
  └────────────┘               │                  │
                               │  ┌────────────┐  │
  ┌────────────┐  WS connect   │  │token=xyz   │  │
  │ Agent C    │──────────────▶│  │separate ch.│  │
  │ token=xyz  │◀══════════════│  └────────────┘  │
  └────────────┘   broadcast   └──────────────────┘

Usage:
  python3 server_ws.py                  # WS on :9876, HTTP on :9877
  python3 server_ws.py --ws-port 9000
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import time
from typing import Any

from agent_bus.server import AgentBusServer, TokenNetwork

logging.basicConfig(level=logging.INFO, format="[AgentBus-WS] %(message)s")
log = logging.getLogger(__name__)


# ── Flow Recorder (Wireshark-style message tap) ──────────────────────

class FlowRecorder:
    """Captures every message that flows through the bus.

    Like a packet sniffer for agent traffic: keeps a ring buffer of recent
    events and fans each new event out to live subscribers (for SSE streaming).
    Useful for spotting loops, runaway agents, and debugging message flow.
    """

    def __init__(self, maxlen: int = 1000):
        self.buffer: collections.deque = collections.deque(maxlen=maxlen)
        self.subscribers: set[asyncio.Queue] = set()
        self.seq = 0

    def record(self, token: str, kind: str, source: str = "", target: str = "",
               payload: Any = None, delivered: int | None = None,
               extra: dict | None = None) -> dict:
        """Record one flow event and push it to live subscribers."""
        self.seq += 1

        preview: Any = payload
        if isinstance(payload, (dict, list)):
            preview = json.dumps(payload, ensure_ascii=False)
        if isinstance(preview, str) and len(preview) > 300:
            preview = preview[:300] + "…"

        event = {
            "seq": self.seq,
            "ts": round(time.time(), 3),
            "token": token,
            "kind": kind,                  # register|message|task|task_complete|disconnect|ping
            "source": source,
            "target": target or "*",       # * = broadcast to everyone
            "payload": preview,
            "delivered": delivered,        # number of recipients reached
        }
        if extra:
            event.update(extra)

        self.buffer.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                pass  # subscriber too slow — drop, don't block the bus
        return event

    def recent(self, limit: int = 100, token: str | None = None,
               kind: str | None = None) -> list[dict]:
        """Return recent events, optionally filtered by token / kind."""
        items = list(self.buffer)
        if token:
            items = [e for e in items if e["token"] == token]
        if kind:
            items = [e for e in items if e["kind"] == kind]
        return items[-limit:]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    @staticmethod
    def mask(event: dict) -> dict:
        """Return a copy with the token shortened (don't leak full tokens)."""
        e = dict(event)
        tok = e.get("token", "")
        e["token"] = (tok[:8] + "…") if len(tok) > 8 else tok
        return e


# ── WebSocket Server ─────────────────────────────────────────────────

class WebSocketAgentBusServer:
    """WebSocket server for the agent network.

    Maintains active agent connections and routes messages
    in real time by channel (token).
    """

    def __init__(self, http_port: int = 9877, entry_token: str | None = None,
                 rolling_channel: bool = False):
        self.bus = AgentBusServer()
        self.http_port = http_port

        # Active connections: {token: {agent_id: websocket}}
        self.connections: dict[str, dict[str, Any]] = {}

        # Reverse map: websocket → (token, agent_id)
        self.ws_to_agent: dict[Any, tuple[str, str]] = {}

        # Wireshark-style tap on all message traffic
        self.flow = FlowRecorder()

        self.start_time = time.time()

        # Anti-loop: payloads that are just dots/emoji/whitespace are dropped
        self.loop_protection = True

        # Agent-pair rate limiter: {(from, to): [timestamps]}
        # Prevents rapid back-and-forth loops between the same agents
        self._pair_traffic: dict[tuple[str, str], list[float]] = {}
        self._pair_limit = 5          # max messages per window
        self._pair_window = 30        # seconds

        # Content-based loop detection: {(token, source, target): [(ts, hash)]}
        # Drops near-duplicate payloads in a short window — catches reply
        # loops where agents echo each other with slight variations.
        self._recent_msgs: dict[tuple[str, str, str], list[tuple[float, str]]] = {}
        self._dup_window = 20.0       # seconds
        self._dup_max_repeats = 2     # >N identical hashes in window → drop

        # Canal hash mechanism (opt-in): rolling channel redirect.
        # When disabled (default), agents stay on the token they register
        # with — no redirect, no split-brain across hashed channels.
        self.entry_token: str | None = entry_token or None
        self.channel_hash: str | None = None
        self.rolling_channel = rolling_channel
        if entry_token and rolling_channel:
            self._set_channel(entry_token)

    @staticmethod
    def _is_noop_payload(payload: Any) -> bool:
        """Check if a message payload is just dots/emoji/whitespace (no-op).
        
        These are typical loop-spam messages that agents send to each other
        in infinite reply chains. They are dropped silently to break the loop.
        """
        if not payload:
            return True
        text = str(payload).strip()
        if not text:
            return True
        # Strip common loop characters
        stripped = text.replace(".", "").replace(" ", "").replace("\n", "").replace("\r", "")
        for emoji in ["💛","🖖","❤️","♥","💚","💙","💜","🧡","💕","👍","👋","😊","✅","❌","🔄","👀","💬","🤖"]:
            stripped = stripped.replace(emoji, "")
        return len(stripped.strip()) == 0

    def _payload_hash(self, payload: Any) -> str:
        """Stable short hash of a payload for dup detection (normalized)."""
        if isinstance(payload, (dict, list)):
            try:
                s = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            except Exception:
                s = repr(payload)
        else:
            s = str(payload or "")
        s = s.strip().lower()
        return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]

    def _is_dup_loop(self, token: str, source: str, target: str, payload: Any) -> bool:
        """Return True if this (src→tgt, payload) repeats too often in window."""
        key = (token, source, target)
        h = self._payload_hash(payload)
        now = time.time()
        bucket = self._recent_msgs.setdefault(key, [])
        # Drop entries older than the window
        bucket[:] = [(t, ph) for (t, ph) in bucket if now - t < self._dup_window]
        repeats = sum(1 for (_, ph) in bucket if ph == h)
        bucket.append((now, h))
        # Cap bucket size to avoid unbounded growth on unique chatter
        if len(bucket) > 50:
            del bucket[:-50]
        return repeats >= self._dup_max_repeats

    def _set_channel(self, entry_token: str) -> None:
        """Set the canonical channel for the network.

        Derives an initial channel_hash from the entry_token so that
        channel_hash != entry_token. Agents connecting with entry_token
        are redirected to channel_hash, ensuring the entry token acts as
        a stable "door" while the active channel can be rolled.
        """
        self.entry_token = entry_token
        self.channel_hash = self._derive_hash(entry_token, 0)
        log.info("🔐 Canal canónico: %s → hash %s…",
                 entry_token[:16], self.channel_hash[:16])

    def _derive_hash(self, entry_token: str, counter: int) -> str:
        """Derive a deterministic channel hash from entry_token + counter."""
        raw = f"{entry_token}:{counter}".encode()
        return hashlib.sha256(raw).hexdigest()[:32]

    def roll_channel(self) -> str:
        """Roll to a new channel hash and return it.

        Agents on the old channel should be kicked so they reconnect and
        get redirected to the new channel_hash automatically.
        """
        if not self.entry_token:
            raise ValueError("No entry_token configured — cannot roll")
        # Use current time as the counter so each roll is unique
        self.channel_hash = hashlib.sha256(
            f"{self.entry_token}:{time.time()}".encode()
        ).hexdigest()[:32]
        log.info("🔄 Canal rolled → new hash %s…", self.channel_hash[:16])
        return self.channel_hash

    async def handle_client(self, websocket, path: str = "/"):
        """Handle a WebSocket connection from an agent.

        The agent sends a JSON registration message:
            {"type": "register", "agent_id": "inv", "token": "abc", "card": {...}}

        Then receives message pushes and can send messages:
            {"type": "message", ...}
            {"type": "task", ...}
        """
        agent_id = None
        token = None

        # Client IP (Tailscale/LAN address of the connecting agent)
        client_ip = "?"
        try:
            addr = websocket.remote_address
            if addr:
                client_ip = addr[0]
        except Exception:
            pass

        try:
            # Wait for registration message
            raw = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = json.loads(raw)

            if data.get("type") != "register":
                await websocket.send(json.dumps({"status": "error", "message": "Must register first"}))
                return

            agent_id = data.get("agent_id", "")
            token = data.get("token", "")
            card = data.get("card", {})
            card["ip"] = client_ip  # surface in /agents and agents_list

            if not agent_id or not token:
                await websocket.send(json.dumps({"status": "error", "message": "agent_id and token required"}))
                return

            # ── Canal hash redirect ─────────────────────────────────────
            # If the server has an entry_token configured and this agent's
            # token doesn't match the derived channel_hash, send an advisory
            # redirect. The agent may reconnect with the correct token.
            # The connection is NOT closed — backward compatible with old clients
            # that don't understand redirects.
            if self.channel_hash and token != self.channel_hash:
                await websocket.send(json.dumps({
                    "type": "channel_redirect",
                    "token": self.channel_hash,
                    "entry_token": self.entry_token,
                }))
                log.info("🔄 Advisory redirect sent to %s → canal %s (agent used '%s')",
                         agent_id, self.channel_hash[:12], token[:12])
                self.flow.record(token, "redirect", source=agent_id,
                                 payload=f"{self.channel_hash[:12]} (advisory)")
                # Soft redirect: let the agent continue on its current token
                # so old clients work. New clients will see the redirect and
                # reconnect with the correct hashed token.
                # Fall through to normal registration

            # Register on the bus
            result = self.bus.handle_register(token, agent_id, card)
            await websocket.send(json.dumps(result))

            # Save connection
            if token not in self.connections:
                self.connections[token] = {}
            self.connections[token][agent_id] = websocket
            self.ws_to_agent[websocket] = (token, agent_id)

            # Notify other agents on the same token
            await self._broadcast(token, {
                "type": "agent_joined",
                "agent_id": agent_id,
                "card": card,
                "agents": self.bus.handle_list_agents(token)["agents"],
            }, exclude=agent_id)

            # Send current agent list to the newcomer
            agents_list = self.bus.handle_list_agents(token)
            await websocket.send(json.dumps({
                "type": "agents_list",
                "agents": agents_list["agents"],
            }))

            self.flow.record(token, "register", source=agent_id,
                             extra={"name": card.get("name", agent_id), "ip": client_ip})

            log.info("✅ %s connected to network '%s' from %s (%d agent(s))",
                     agent_id, token, client_ip, len(self.connections[token]))

            # ── Main loop: listen for messages from the agent ──
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                    msg_type = data.get("type", "")

                    if msg_type == "message":
                        msg = data.get("message", {})
                        msg.setdefault("source", agent_id)
                        msg.setdefault("type", "text")
                        
                        # ── Anti-loop filter ────────────────────────────
                        payload = msg.get("payload", "")
                        if self.loop_protection and self._is_noop_payload(payload):
                            # Drop silently — don't store, don't broadcast
                            await websocket.send(json.dumps({
                                "type": "message_ack",
                                "message_id": None,
                                "dropped": True,
                                "reason": "noop",
                            }))
                            self.flow.record(token, "drop", source=agent_id,
                                             payload=repr(payload)[:40],
                                             extra={"reason": "noop"})
                            continue
                        # ── Content-based loop detection ────────────────
                        target = msg.get("target", "")
                        if self.loop_protection and self._is_dup_loop(token, agent_id, target, payload):
                            await websocket.send(json.dumps({
                                "type": "message_ack",
                                "message_id": None,
                                "dropped": True,
                                "reason": f"dup_loop ({self._dup_max_repeats}+ identical in {int(self._dup_window)}s)",
                            }))
                            self.flow.record(token, "drop", source=agent_id,
                                             target=target,
                                             payload=repr(payload)[:60],
                                             extra={"reason": "dup_loop"})
                            log.warning("🛑 Dup loop: %s→%s dropping repeated payload",
                                        agent_id, target or "*")
                            continue
                        # ── Agent-pair rate limiter ──────────────────────
                        if target and self.loop_protection:
                            pair = (agent_id, target)
                            now = time.time()
                            # Prune old entries
                            self._pair_traffic.setdefault(pair, [])
                            self._pair_traffic[pair] = [
                                t for t in self._pair_traffic[pair]
                                if now - t < self._pair_window
                            ]
                            if len(self._pair_traffic[pair]) >= self._pair_limit:
                                # Too many messages between this pair — drop
                                await websocket.send(json.dumps({
                                    "type": "message_ack",
                                    "message_id": None,
                                    "dropped": True,
                                    "reason": f"pair_rate_limit ({self._pair_limit}/{self._pair_window}s)",
                                }))
                                self.flow.record(token, "drop", source=agent_id,
                                                 target=target,
                                                 payload=repr(payload)[:60],
                                                 extra={"reason": "pair_rate_limit"})
                                log.warning(
                                    "🛑 Pair rate limit: %s→%s (%d msgs in %ds) — dropping",
                                    agent_id, target,
                                    len(self._pair_traffic[pair]), self._pair_window,
                                )
                                continue
                            self._pair_traffic[pair].append(now)
                        # ─────────────────────────────────────────────────
                        
                        stored = self.bus.handle_send_message(token, msg)

                        if target:
                            # Direct message: only to recipient
                            ok = await self._broadcast_to(token, target, {
                                "type": "new_message",
                                "message": msg,
                            })
                            delivered = 1 if ok else 0
                        else:
                            # Broadcast: everyone EXCEPT sender
                            await self._broadcast(token, {
                                "type": "new_message",
                                "message": msg,
                            }, exclude=agent_id)
                            delivered = max(0, len(self.connections.get(token, {})) - 1)

                        self.flow.record(token, "message", source=agent_id,
                                         target=target, payload=msg.get("payload"),
                                         delivered=delivered,
                                         extra={"msg_type": msg.get("type", "text"), "ip": client_ip,
                                                "message_id": stored.get("message_id", "")})

                        # Acknowledge to sender
                        await websocket.send(json.dumps({
                            "type": "message_ack",
                            "message_id": stored.get("message_id"),
                        }))

                    elif msg_type == "task":
                        task = data.get("task", {})
                        task.setdefault("source_agent", agent_id)
                        result = self.bus.handle_submit_task(token, task)

                        self.flow.record(token, "task", source=agent_id,
                                         target=task.get("target_agent", ""),
                                         payload=task.get("goal", ""),
                                         extra={"task_id": result.get("task_id", "")[:12], "ip": client_ip})

                        await websocket.send(json.dumps({
                            "type": "task_ack",
                            "task_id": result.get("task_id"),
                        }))

                    elif msg_type == "task_complete":
                        task_id = data.get("task_id", "")
                        result = data.get("result")
                        error = data.get("error")
                        completed = self.bus.handle_complete_task(token, task_id, result, error)

                        # Notify the originating agent
                        if completed.get("status") == "ok":
                            task_data = completed.get("task", {})
                            source = task_data.get("source_agent", "")
                            if source and source in self.connections.get(token, {}):
                                ws = self.connections[token][source]
                                try:
                                    await ws.send(json.dumps({
                                        "type": "task_completed",
                                        "task": task_data,
                                    }))
                                except Exception:
                                    pass

                        self.flow.record(token, "task_complete", source=agent_id,
                                         payload=str(result)[:120] if result else error,
                                         extra={"task_id": str(task_id)[:12],
                                                "ok": completed.get("status") == "ok"})

                        completed.setdefault("type", "task_complete_result")
                        await websocket.send(json.dumps(completed))

                    elif msg_type == "claim_task":
                        claimed = self.bus.handle_claim_task(token, agent_id)
                        claimed["type"] = "claim_task_result"
                        await websocket.send(json.dumps(claimed))

                    elif msg_type == "ping":
                        self.flow.record(token, "ping", source=agent_id)
                        await websocket.send(json.dumps({"type": "pong"}))

                    else:
                        await websocket.send(json.dumps({
                            "status": "error",
                            "message": f"Unknown type: {msg_type}",
                        }))

                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "status": "error", "message": "Invalid JSON",
                    }))

        except asyncio.TimeoutError:
            log.warning("Timeout waiting for registration from %s", websocket.remote_address)
        except Exception as e:
            log.debug("Error with %s: %s", agent_id or "?", e)
        finally:
            if websocket in self.ws_to_agent:
                tok, aid = self.ws_to_agent.pop(websocket)
                if tok in self.connections and aid in self.connections[tok]:
                    del self.connections[tok][aid]
                    if not self.connections[tok]:
                        del self.connections[tok]

                    # Remove from the agent registry so /agents and agents_list
                    # no longer include this agent
                    self.bus.handle_unregister(tok, aid)

                    self.flow.record(tok, "disconnect", source=aid)

                    log.info("❌ %s disconnected from network '%s'", aid, tok)
                    await self._broadcast(tok, {
                        "type": "agent_left",
                        "agent_id": aid,
                        "agents": self.bus.handle_list_agents(tok)["agents"],
                    }, exclude=aid)

    async def _broadcast(self, token: str, data: dict, exclude: str | None = None):
        """Send a message to all connected agents on a token."""
        if token not in self.connections:
            return
        payload = json.dumps(data, ensure_ascii=False)
        for aid, ws in list(self.connections[token].items()):
            if exclude and aid == exclude:
                continue
            try:
                await ws.send(payload)
            except Exception:
                pass

    def _resolve_target(self, token: str, target: str) -> str | None:
        """Resolve a target to an agent_id — supports name aliases."""
        if not token or not target:
            return None
        conns = self.connections.get(token, {})
        # Direct match (agent_id)
        if target in conns:
            return target
        # Name alias resolution — scan all agents on this token
        for agent_id in conns:
            card = self.bus._get_network(token).agents.get(agent_id, {})
            name = card.get("name", "")
            if name and name.lower() == target.lower():
                return agent_id
        return None

    async def _broadcast_to(self, token: str, target: str, data: dict) -> bool:
        """Send a message to a specific agent on the token."""
        agent_id = self._resolve_target(token, target)
        if agent_id:
            ws = self.connections.get(token, {}).get(agent_id)
            if ws:
                try:
                    await ws.send(json.dumps(data, ensure_ascii=False))
                    return True
                except Exception:
                    pass
        return False

    async def kick_agent(self, token: str, agent_id: str, reason: str = "kicked") -> bool:
        """Force-disconnect an agent's WebSocket session.

        Closing the socket triggers the agent's auto-reconnect logic, so this is
        a clean way to reset a stuck/looping agent: it drops and rejoins fresh.
        Returns True if the agent was connected and a close was issued.
        """
        ws = self.connections.get(token, {}).get(agent_id)
        if ws is None:
            return False

        self.flow.record(token, "kick", source="server", target=agent_id, payload=reason)
        log.info("🥾 kicking %s from network '%s' (%s)", agent_id, token, reason)
        try:
            # 1000 = normal closure; the client sees a clean close and reconnects
            await ws.close(code=1000, reason=reason[:120])
        except Exception:
            # Even if close races, the handler's finally block cleans up state
            pass
        return True

    async def get_connected_agents(self, token: str) -> int:
        """Number of agents connected on a token."""
        return len(self.connections.get(token, {}))

    def get_stats(self) -> dict:
        stats = self.bus.handle_global_stats()
        stats["ws_connections"] = sum(len(v) for v in self.connections.values())
        stats["ws_networks"] = len(self.connections)
        return stats


# ── Companion HTTP server ────────────────────────────────────────────

class HTTPHealthHandler:
    """Minimal HTTP handler for health checks and stats.

    Runs on a separate port alongside the WS server.
    """

    def __init__(self, ws_server: WebSocketAgentBusServer):
        self.ws_server = ws_server

    async def handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await reader.read(4096)
            request_str = request.decode()

            lines = request_str.split("\r\n")
            first_line = lines[0] if lines else ""
            parts = first_line.split(" ")
            method = parts[0] if len(parts) > 0 else "GET"
            path_raw = parts[1] if len(parts) > 1 else "/"

            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(path_raw)
            path = parsed.path.rstrip("/")
            qs = parse_qs(parsed.query)

            token = ""
            for line in lines[1:]:
                if line.lower().startswith("x-agent-token:"):
                    token = line.split(":", 1)[1].strip()
                    break
            if not token:
                token = qs.get("token", [""])[0]

            body_start = request_str.find("\r\n\r\n")
            body = {}
            if body_start > 0:
                raw_body = request_str[body_start + 4:]
                if raw_body.strip():
                    try:
                        body = json.loads(raw_body)
                    except json.JSONDecodeError:
                        pass

            flow = self.ws_server.flow
            bus = self.ws_server.bus

            result = None

            # ── Chat UI (root — no token required) ─────────────────────
            if path == "/chat":
                html = _CHAT_HTML
                resp = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    "Access-Control-Allow-Origin: *\r\n"
                    f"Content-Length: {len(html.encode('utf-8'))}\r\n"
                    "\r\n"
                    f"{html}"
                )
                writer.write(resp.encode())
                await writer.drain()
                return

            elif path == "/chat/agents":
                # Root: list ALL agents across ALL networks
                result = bus.handle_list_all_agents()

            elif path == "/chat/history":
                agent_id = qs.get("agent_id", [None])[0]
                tok = qs.get("token", [None])[0]
                limit_str = qs.get("limit", ["100"])[0]
                limit = min(int(limit_str), 500)
                result = bus.handle_get_all_messages(agent_id=agent_id, token=tok, limit=limit)

            elif path == "/chat/send" and method == "POST":
                tok = body.get("token", "")
                target = body.get("target", "")
                payload = body.get("payload", "")
                msg_type = body.get("type", "text")
                source = body.get("source", "monitor")
                reply_to = body.get("reply_to", "")
                conversation_id = body.get("conversation_id", "")
                if tok and payload:
                    msg = {
                        "type": msg_type,
                        "source": source,
                        "target": target,
                        "payload": payload,
                    }
                    if reply_to:
                        msg["reply_to"] = reply_to
                    if conversation_id:
                        msg["conversation_id"] = conversation_id
                    if self.ws_server.loop_protection and self.ws_server._is_noop_payload(payload):
                        self.ws_server.flow.record(tok, "drop", source=source,
                                                   payload=repr(payload)[:40],
                                                   extra={"reason": "noop", "via": "chat"})
                        result = {"status": "ok", "message_id": None, "dropped": True, "reason": "noop"}
                    elif (self.ws_server.loop_protection and source != "monitor"
                          and self.ws_server._is_dup_loop(tok, source, target, payload)):
                        # Block agent dup loops via chat injection; allow humans
                        self.ws_server.flow.record(tok, "drop", source=source,
                                                   target=target,
                                                   payload=repr(payload)[:60],
                                                   extra={"reason": "dup_loop", "via": "chat"})
                        result = {"status": "ok", "message_id": None, "dropped": True, "reason": "dup_loop"}
                    else:
                        result = bus.handle_send_message(tok, msg)
                        ws_event = {"type": "new_message", "message": msg}
                        if target:
                            ok = await self.ws_server._broadcast_to(tok, target, ws_event)
                            delivered = 1 if ok else 0
                        else:
                            await self.ws_server._broadcast(tok, ws_event, exclude=source)
                            delivered = max(0, len(self.ws_server.connections.get(tok, {})))
                        self.ws_server.flow.record(tok, "message", source=source,
                                                   target=target, payload=payload,
                                                   delivered=delivered,
                                                   extra={"via": "chat", "msg_type": msg_type,
                                                          "message_id": result.get("message_id", "")})
                    # Return the message_id so the UI can track reply_to for next message
                    result["message_id"] = result.get("message_id", "")
                else:
                    result = {"status": "error", "message": "token and payload required"}

            # ── All other endpoints ─────────────────────────────────
            elif path == "/monitor":
                html = _MONITOR_HTML
                resp = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    "Access-Control-Allow-Origin: *\r\n"
                    f"Content-Length: {len(html.encode('utf-8'))}\r\n"
                    "\r\n"
                    f"{html}"
                )
                writer.write(resp.encode())
                await writer.drain()
                return

            # ── SSE live stream (Wireshark-style tap) ────────────────
            elif path == "/flow/stream":
                writer.write(
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/event-stream\r\n"
                    "Cache-Control: no-cache\r\n"
                    "Connection: keep-alive\r\n"
                    "Access-Control-Allow-Origin: *\r\n"
                    "\r\n".encode()
                )
                await writer.drain()
                q = flow.subscribe()
                try:
                    # Send recent backlog first so the viewer isn't empty
                    for e in flow.recent(limit=50, token=token or None):
                        writer.write(f"data: {json.dumps(flow.mask(e))}\n\n".encode())
                    await writer.drain()
                    while True:
                        try:
                            event = await asyncio.wait_for(q.get(), timeout=15)
                            if token and event["token"] != token:
                                continue
                            writer.write(f"data: {json.dumps(flow.mask(event))}\n\n".encode())
                        except asyncio.TimeoutError:
                            writer.write(b": keepalive\n\n")  # SSE comment, keeps conn alive
                        await writer.drain()
                except Exception:
                    pass
                finally:
                    flow.unsubscribe(q)
                    writer.close()
                return

            elif path == "/flow":
                kind = qs.get("kind", [None])[0]
                limit = min(int(qs.get("limit", ["100"])[0]), 1000)
                events = flow.recent(limit=limit, token=token or None, kind=kind)
                result = {"status": "ok",
                          "events": [flow.mask(e) for e in events],
                          "count": len(events)}

            elif path == "/health":
                result = {"status": "ok", "uptime": int(time.time() - self.ws_server.start_time),
                          "ws_connections": sum(len(v) for v in self.ws_server.connections.values())}

            elif path == "/stats":
                if token and "token" in parsed.query:
                    result = bus.handle_stats(token)
                else:
                    global_stats = bus.handle_global_stats()
                    global_stats["ws_connections"] = sum(
                        len(v) for v in self.ws_server.connections.values()
                    )
                    result = global_stats

            elif path == "/agents" and token:
                result = bus.handle_list_agents(token)

            elif path == "/messages" and token:
                agent_id = qs.get("agent_id", [None])[0]
                since = qs.get("since", [None])[0]
                limit_str = qs.get("limit", ["50"])[0]
                limit = min(int(limit_str), 200)
                result = bus.handle_get_messages(token, agent_id, since, limit)

            elif path == "/register" and method == "POST":
                agent_id = body.get("agent_id", "")
                card = body.get("card", {})
                tok = body.get("token", token)
                if agent_id and tok:
                    result = bus.handle_register(tok, agent_id, card)
                    await self.ws_server._broadcast(tok, {
                        "type": "agent_joined",
                        "agent_id": agent_id,
                        "card": card,
                        "agents": bus.handle_list_agents(tok)["agents"],
                    })
                else:
                    result = {"status": "error", "message": "agent_id and token required"}

            elif path == "/unregister" and method == "POST":
                agent_id = body.get("agent_id", "")
                tok = body.get("token", token)
                if agent_id and tok:
                    result = bus.handle_unregister(tok, agent_id)
                    log.info("🗑️ Unregistered %s from network '%s'", agent_id, tok[:12])
                else:
                    result = {"status": "error", "message": "agent_id and token required"}

            elif path == "/message" and method == "POST":
                msg = body.get("message", body)
                tok = body.get("token", token)
                if tok and msg:
                    # ── Anti-loop filter ────────────────────────────────
                    payload = msg.get("payload", "")
                    src = msg.get("source", "http")
                    tgt = msg.get("target", "")
                    if self.ws_server.loop_protection and self.ws_server._is_noop_payload(payload):
                        self.ws_server.flow.record(tok, "drop", source=src,
                                                   payload=repr(payload)[:40],
                                                   extra={"reason": "noop", "via": "http"})
                        result = {"status": "ok", "message_id": None, "dropped": True, "reason": "noop"}
                    elif (self.ws_server.loop_protection
                          and self.ws_server._is_dup_loop(tok, src, tgt, payload)):
                        self.ws_server.flow.record(tok, "drop", source=src, target=tgt,
                                                   payload=repr(payload)[:60],
                                                   extra={"reason": "dup_loop", "via": "http"})
                        result = {"status": "ok", "message_id": None, "dropped": True, "reason": "dup_loop"}
                    else:
                        result = bus.handle_send_message(tok, msg)
                    target = msg.get("target", "")
                    ws_event = {"type": "new_message", "message": msg}
                    if target:
                        ok = await self.ws_server._broadcast_to(tok, target, ws_event)
                        delivered = 1 if ok else 0
                    else:
                        await self.ws_server._broadcast(tok, ws_event, exclude=msg.get("source"))
                        delivered = max(0, len(self.ws_server.connections.get(tok, {})))
                    self.ws_server.flow.record(tok, "message", source=msg.get("source", "http"),
                                               target=target, payload=msg.get("payload"),
                                               delivered=delivered,
                                               extra={"via": "http", "msg_type": msg.get("type", "text")})
                else:
                    result = {"status": "error", "message": "message and token required"}

            elif path == "/task" and method == "POST":
                task = body.get("task", body)
                tok = body.get("token", token)
                if tok and task:
                    result = bus.handle_submit_task(tok, task)
                else:
                    result = {"status": "error", "message": "task and token required"}

            elif path == "/kick" and method == "POST":
                aid = body.get("agent_id", "")
                tok = body.get("token", token)
                reason = body.get("reason", "kicked by admin")
                if tok and aid:
                    kicked = await self.ws_server.kick_agent(tok, aid, reason)
                    result = {
                        "status": "ok" if kicked else "error",
                        "kicked": kicked,
                        "agent_id": aid,
                        "message": (
                            "session closed; agent will reconnect if it has auto-reconnect"
                            if kicked else "agent not currently connected"
                        ),
                    }
                else:
                    result = {"status": "error", "message": "agent_id and token required"}

            elif path == "/purge" and method == "POST":
                tok = body.get("token", token)
                if tok:
                    agents = list(self.ws_server.connections.get(tok, {}).keys())
                    kicked = 0
                    for aid in agents:
                        if await self.ws_server.kick_agent(tok, aid, "purge from monitor"):
                            kicked += 1
                    result = {"status": "ok", "kicked": kicked, "agents": agents}
                    log.info("🧹 Purged %d agent(s) from token '%s'", kicked, tok[:12])
                else:
                    result = {"status": "error", "message": "token required"}

            elif path == "/roll" and method == "POST":
                # Roll the channel: generate a new channel_hash so existing
                # agents (which get kicked here) and new arrivals with the
                # original entry_token all converge on the new hash.
                ws = self.ws_server
                if not ws.entry_token:
                    result = {"status": "error", "message": "server has no entry_token configured"}
                else:
                    old_hash = ws.channel_hash
                    new_hash = ws.roll_channel()
                    # Kick all agents on both the old channel_hash AND entry_token
                    kicked = 0
                    for kick_tok in {old_hash, ws.entry_token}:
                        for aid in list(ws.connections.get(kick_tok, {}).keys()):
                            if await ws.kick_agent(kick_tok, aid, "channel rolled"):
                                kicked += 1
                    log.info("🔄 Rolled: %s… → %s… (kicked %d)",
                             (old_hash or "")[:12], new_hash[:12], kicked)
                    result = {
                        "status": "ok",
                        "channel_hash": new_hash,
                        "old_hash": old_hash,
                        "kicked": kicked,
                    }

            elif path == "/task/complete" and method == "POST":
                task_id = body.get("task_id", "")
                result_val = body.get("result")
                error = body.get("error")
                tok = body.get("token", token)
                result = bus.handle_complete_task(tok, task_id, result_val, error)

            elif path == "/task" and method == "GET":
                agent_id = qs.get("agent_id", [None])[0]
                if agent_id and token:
                    result = bus.handle_claim_task(token, agent_id)
                elif token:
                    task_id = qs.get("task_id", [None])[0]
                    if task_id:
                        network = bus.networks.get(token)
                        t = network.tasks.get(task_id) if network else None
                        result = {"status": "ok", "task": t}
                    else:
                        result = {"status": "error", "message": "agent_id or task_id required"}
                else:
                    result = {"status": "error", "message": "token required"}

            else:
                result = {"status": "error", "message": "not found", "path": path}

            if result is None:
                result = {"status": "error", "message": "internal error"}

            body = json.dumps(result, ensure_ascii=False)
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                "\r\n"
                f"{body}"
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception as e:
            try:
                err_body = json.dumps({"status": "error", "message": str(e)})
                response = (
                    "HTTP/1.1 500 Internal Server Error\r\n"
                    "Content-Type: application/json\r\n"
                    "Access-Control-Allow-Origin: *\r\n"
                    f"Content-Length: {len(err_body.encode('utf-8'))}\r\n"
                    "\r\n"
                    f"{err_body}"
                )
                writer.write(response.encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()


# ── Chat UI (interactive agent chat) ─────────────────────────────────

_CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AgentBus Chat</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; height: 100vh; display: flex; flex-direction: column; }

  header { display: flex; align-items: center; gap: 12px; padding: 10px 16px;
           background: #161b22; border-bottom: 1px solid #30363d; flex-shrink: 0; }
  header h1 { font-size: 16px; color: #58a6ff; }
  #dot { width: 9px; height: 9px; border-radius: 50%; background: #f85149; transition: background .3s; }
  #dot.live { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  header .spacer { flex: 1; }
  .stat { color: #8b949e; font-size: 12px; }

  .main { display: flex; flex: 1; overflow: hidden; }

  /* ── Sidebar ── */
  .sidebar { width: 260px; background: #161b22; border-right: 1px solid #30363d;
             display: flex; flex-direction: column; flex-shrink: 0; }
  .sidebar-header { padding: 10px 12px; font-size: 11px; text-transform: uppercase;
                    letter-spacing: .5px; color: #8b949e; border-bottom: 1px solid #21262d; }
  .agent-list { flex: 1; overflow-y: auto; }

  .network-group { border-bottom: 1px solid #21262d; }
  .network-label { padding: 6px 12px; font-size: 10px; text-transform: uppercase;
                   letter-spacing: .5px; color: #6e7681; background: #0d1117;
                   display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .network-label:hover { color: #c9d1d9; }
  .network-label .arrow { font-size: 9px; transition: transform .2s; }
  .network-label.collapsed .arrow { transform: rotate(-90deg); }
  .network-label .bcast { color: #d29922; }
  .network-agents { overflow: hidden; }
  .network-label.collapsed + .network-agents { display: none; }

  .agent-item { padding: 7px 12px 7px 28px; cursor: pointer;
                display: flex; align-items: center; gap: 8px; transition: background .15s; }
  .agent-item:hover { background: #1c2128; }
  .agent-item.active { background: #1f6feb20; border-left: 2px solid #1f6feb; padding-left: 26px; }
  .agent-item .dot { width: 7px; height: 7px; border-radius: 50%; background: #3fb950; flex-shrink: 0; }
  .agent-item .name { font-weight: 600; font-size: 12px; }
  .agent-item .desc { font-size: 10px; color: #6e7681; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .broadcast-item { padding: 8px 12px; cursor: pointer; border-bottom: 1px solid #21262d;
                    display: flex; align-items: center; gap: 8px; color: #d29922; }
  .broadcast-item:hover { background: #1c2128; }
  .broadcast-item.active { background: #d2992220; border-left: 2px solid #d29922; }

  /* ── Chat area ── */
  .chat-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  .chat-header { padding: 10px 16px; border-bottom: 1px solid #21262d;
                 display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
  .chat-header .target-name { font-weight: 600; font-size: 14px; }
  .chat-header .target-hint { color: #6e7681; font-size: 12px; }

  .messages { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; }
  .msg { max-width: 75%; padding: 8px 12px; border-radius: 10px; font-size: 13px;
         line-height: 1.5; word-break: break-word; white-space: pre-wrap; }
  .msg.in { align-self: flex-start; background: #1c2128; border: 1px solid #30363d; }
  .msg.out { align-self: flex-end; background: #1f6feb25; border: 1px solid #1f6feb40; }
  .msg .meta { font-size: 10px; color: #6e7681; margin-bottom: 3px; }
  .msg .meta .src { color: #58a6ff; font-weight: 600; }
  .msg .meta .tok { color: #6e7681; font-size: 9px; }
  .msg.system { align-self: center; background: transparent; color: #6e7681; font-size: 11px;
                font-style: italic; padding: 4px 8px; max-width: 90%; text-align: center; }

  .input-area { padding: 10px 16px; border-top: 1px solid #21262d; display: flex;
                gap: 8px; align-items: flex-end; flex-shrink: 0; }
  .input-area textarea { flex: 1; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
                         border-radius: 8px; padding: 8px 12px; font: inherit; font-size: 13px;
                         resize: none; min-height: 38px; max-height: 120px; }
  .input-area textarea:focus { outline: none; border-color: #1f6feb; }
  .input-area button { background: #1f6feb; color: #fff; border: none; border-radius: 8px;
                       padding: 8px 16px; font: inherit; font-size: 13px; cursor: pointer;
                       white-space: nowrap; }
  .input-area button:hover { background: #388bfd; }
  .input-area button:disabled { background: #30363d; color: #6e7681; cursor: default; }

  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
</style>
</head>
<body>
<header>
  <div id="dot"></div>
  <h1>AgentBus Chat</h1>
  <span class="spacer"></span>
  <span class="stat" id="connStatus">disconnected</span>
</header>
<div class="main">
  <div class="sidebar">
    <div class="sidebar-header">All Networks</div>
    <div class="agent-list" id="agentList">
      <div class="broadcast-item active" data-token="" data-target="">
        <span>📢</span><span>Broadcast (all networks)</span>
      </div>
    </div>
  </div>
  <div class="chat-area">
    <div class="chat-header">
      <span class="target-name" id="targetName">Broadcast</span>
      <span class="target-hint" id="targetHint">— all networks</span>
    </div>
    <div class="messages" id="messages">
      <div class="msg system">Select an agent or network to start chatting</div>
    </div>
    <div class="input-area">
      <textarea id="msgInput" placeholder="Type a message... (Enter to send, Shift+Enter for newline)" rows="1"></textarea>
      <button id="sendBtn" disabled>Send</button>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const agentList = $('agentList');
const messagesEl = $('messages');
const msgInput = $('msgInput');
const sendBtn = $('sendBtn');
const targetName = $('targetName');
const targetHint = $('targetHint');
const dot = $('dot');
const connStatus = $('connStatus');

let currentToken = '';   // which network token
let currentTarget = '';  // which agent (empty = broadcast)
let conversationId = '';  // UUID for this chat session
let lastSentId = '';      // message_id of last message we sent
let lastReplyId = '';     // message_id of last agent reply (for reply_to)
let es = null;

// ── SSE (global — no token filter) ──
function connectSSE() {
  if (es) es.close();
  es = new EventSource('/flow/stream');
  es.onopen = () => { dot.classList.add('live'); connStatus.textContent = 'connected'; };
  es.onerror = () => { dot.classList.remove('live'); connStatus.textContent = 'disconnected'; };
  es.onmessage = ev => {
    const e = JSON.parse(ev.data);
    if (e.kind === 'message') handleFlowMessage(e);
    if (e.kind === 'register') handleRegister(e);
    if (e.kind === 'disconnect') handleDisconnect(e);
  };
}

function handleFlowMessage(e) {
  // Root view: show all messages, filter only by selected agent if any
  if (currentTarget) {
    // DM view: show messages to/from this agent
    if (e.source !== currentTarget && e.target !== currentTarget) return;
    // Track message_ids for reply_to chaining
    const mid = e.extra?.message_id || '';
    if (e.source === currentTarget && mid) {
      lastReplyId = mid;
    }
    if (e.source === 'monitor' && mid) {
      lastSentId = mid;
    }
  }
  // If broadcast view (no target selected), show everything
  const dir = e.source === 'monitor' ? 'out' : 'in';
  addMessage(e.source, e.payload, e.ts, dir, e.token);
}

function handleRegister(e) {
  loadAgents();
}

function handleDisconnect(e) {
  loadAgents();
}

// ── Load all agents (root) ──
async function loadAgents() {
  try {
    const r = await fetch('/chat/agents');
    const d = await r.json();
    if (d.status === 'ok') renderAgents(d.agents);
  } catch(e) { console.error('loadAgents', e); }
}

function renderAgents(agents) {
  // Group agents by token
  const groups = {};
  agents.forEach(a => {
    const tok = a._token || 'unknown';
    if (!groups[tok]) groups[tok] = [];
    groups[tok].push(a);
  });

  let html = '<div class="broadcast-item' + (!currentToken && !currentTarget ? ' active' : '') + '" data-token="" data-target="">'
    + '<span>📢</span><span>Broadcast (all networks)</span></div>';

  Object.entries(groups).forEach(([tok, agts]) => {
    const shortTok = tok.length > 16 ? tok.slice(0, 8) + '…' + tok.slice(-6) : tok;
    const isNetActive = currentToken === tok && !currentTarget;
    html += '<div class="network-group">';
    html += '<div class="network-label' + (isNetActive ? ' active' : '') + '" data-token="' + esc(tok) + '" data-target="">'
      + '<span class="arrow">▼</span><span class="bcast">📡</span><span>' + esc(shortTok) + '</span>'
      + '<span style="margin-left:auto;font-size:10px;color:#6e7681">' + agts.length + '</span></div>';
    html += '<div class="network-agents">';
    agts.forEach(a => {
      const isActive = currentToken === tok && currentTarget === a.agent_id;
      html += '<div class="agent-item' + (isActive ? ' active' : '') + '" data-token="' + esc(tok) + '" data-target="' + esc(a.agent_id) + '">'
        + '<div class="dot"></div><div><div class="name">' + esc(a.name || a.agent_id) + '</div>'
        + '<div class="desc">' + esc(a.description || a.agent_id) + '</div></div></div>';
    });
    html += '</div></div>';
  });

  agentList.innerHTML = html;

  // Attach click handlers
  agentList.querySelectorAll('.network-label').forEach(el => {
    el.onclick = () => selectTarget(el.dataset.token, '');
  });
  agentList.querySelectorAll('.agent-item').forEach(el => {
    el.onclick = () => selectTarget(el.dataset.token, el.dataset.target);
  });
  agentList.querySelectorAll('.broadcast-item').forEach(el => {
    el.onclick = () => selectTarget('', '');
  });
}

function selectTarget(token, agentId) {
  currentToken = token;
  currentTarget = agentId;
  // New conversation thread — generate ID and reset reply chain
  conversationId = 'chat-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
  lastSentId = '';
  lastReplyId = '';

  if (!token && !agentId) {
    targetName.textContent = 'Broadcast';
    targetHint.textContent = '— all networks';
  } else if (token && !agentId) {
    targetName.textContent = 'Network: ' + (token.length > 20 ? token.slice(0, 10) + '…' + token.slice(-8) : token);
    targetHint.textContent = '— broadcast to all agents on this network';
  } else {
    targetName.textContent = agentId;
    targetHint.textContent = '— DM on network ' + (token.length > 20 ? token.slice(0, 8) + '…' : token);
  }

  renderAgentsFromCache();
  loadHistory();
  messagesEl.innerHTML = '<div class="msg system">Chatting with ' + esc(targetName.textContent) + '</div>';
}

let cachedAgents = [];
function renderAgentsFromCache() {
  // Re-render with current selection from cached data
  if (cachedAgents.length) renderAgents(cachedAgents);
}

// ── Load history ──
async function loadHistory() {
  const params = new URLSearchParams({ limit: '100' });
  if (currentToken) params.set('token', currentToken);
  if (currentTarget) params.set('agent_id', currentTarget);
  try {
    const r = await fetch('/chat/history?' + params);
    const d = await r.json();
    if (d.status === 'ok') {
      messagesEl.innerHTML = '';
      if (d.messages.length === 0) {
        messagesEl.innerHTML = '<div class="msg system">No messages yet. Say hello!</div>';
      } else {
        d.messages.forEach(m => {
          const dir = m.source === 'monitor' ? 'out' : 'in';
          addMessage(m.source, m.payload, m.timestamp, dir, m._token, false);
        });
      }
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  } catch(e) { console.error('loadHistory', e); }
}

// ── Send ──
async function sendMessage() {
  const text = msgInput.value.trim();
  if (!text) return;
  if (!currentToken && !currentTarget) {
    addMessage('system', '⚠ Select a network or agent first', null, 'system');
    return;
  }
  sendBtn.disabled = true;
  try {
    const body = { token: currentToken, target: currentTarget, payload: text, source: 'monitor' };
    if (lastReplyId) body.reply_to = lastReplyId;
    if (conversationId) body.conversation_id = conversationId;
    const r = await fetch('/chat/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (d.dropped) {
      addMessage('system', '⚠ message dropped: ' + d.reason, null, 'system');
    } else {
      lastSentId = d.message_id || '';
      msgInput.value = '';
      msgInput.style.height = 'auto';
      addMessage('monitor', text, new Date().toISOString(), 'out', currentToken);
    }
  } catch(e) {
    addMessage('system', '⚠ send failed: ' + e.message, null, 'system');
  } finally {
    sendBtn.disabled = false;
    msgInput.focus();
  }
}

sendBtn.onclick = sendMessage;
msgInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
msgInput.addEventListener('input', () => {
  sendBtn.disabled = !msgInput.value.trim();
  msgInput.style.height = 'auto';
  msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + 'px';
});

// ── Render message ──
function fmtPayload(p) {
  if (p == null) return { text: '', isObj: false };
  if (typeof p === 'string') return { text: p, isObj: false };
  if (typeof p === 'object') {
    if (typeof p.text === 'string') return { text: p.text, isObj: false };
    if (typeof p.message === 'string') return { text: p.message, isObj: false };
    if (typeof p.content === 'string') return { text: p.content, isObj: false };
    if (typeof p.goal === 'string') return { text: p.goal, isObj: false };
    try { return { text: JSON.stringify(p, null, 2), isObj: true }; }
    catch (e) { return { text: '[unserializable]', isObj: false }; }
  }
  return { text: String(p), isObj: false };
}

function addMessage(source, payload, ts, dir, token, scroll = true) {
  const div = document.createElement('div');
  div.className = 'msg ' + dir;
  const time = ts ? new Date(ts).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
  const srcLabel = dir === 'out' ? 'you' : esc(source);
  const tokLabel = token && dir === 'in' ? ' <span class="tok">[' + esc(token.slice(0, 8)) + '…]</span>' : '';
  const f = fmtPayload(payload);
  const bodyHtml = f.isObj
    ? '<pre style="margin:4px 0 0 0;white-space:pre-wrap;word-break:break-word;font:12px ui-monospace,Menlo,monospace;color:#a0a8b0">' + esc(f.text) + '</pre>'
    : esc(f.text);
  div.innerHTML = '<div class="meta"><span class="src">' + srcLabel + '</span>'
    + tokLabel
    + (time ? ' <span style="color:#6e7681">' + time + '</span>' : '') + '</div>'
    + bodyHtml;
  messagesEl.appendChild(div);
  if (scroll) messagesEl.scrollTop = messagesEl.scrollHeight;
  while (messagesEl.children.length > 300) messagesEl.removeChild(messagesEl.firstChild);
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ── Init ──
loadAgents();
connectSSE();
setInterval(loadAgents, 10000);
</script>
</body>
</html>"""


# ── Monitor HTML (live message-flow viewer, Wireshark-style) ─────────

_MONITOR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AgentBus Monitor</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #0d1117; color: #c9d1d9; }
  header { display: flex; align-items: center; gap: 12px; padding: 10px 14px;
           background: #161b22; border-bottom: 1px solid #30363d; position: sticky; top: 0; }
  header h1 { font-size: 15px; margin: 0; color: #58a6ff; }
  header .stat { color: #8b949e; }
  header .stat b { color: #c9d1d9; }
  header input, header select, header button {
    background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 6px; padding: 4px 8px; font: inherit; }
  header button { cursor: pointer; }
  header button.on { background: #238636; border-color: #238636; color: #fff; }
  #dot { width: 9px; height: 9px; border-radius: 50%; background: #f85149; }
  #dot.live { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 6px 10px; color: #8b949e; font-weight: 600;
       border-bottom: 1px solid #30363d; position: sticky; top: 49px; background: #0d1117; }
  td { padding: 4px 10px; border-bottom: 1px solid #161b22; vertical-align: top; }
  tr:hover td { background: #161b22; }
  .kind { font-weight: 700; padding: 1px 6px; border-radius: 4px; font-size: 11px; }
  .k-message { color: #58a6ff; }
  .k-task { color: #d29922; }
  .k-task_complete { color: #3fb950; }
  .k-register { color: #3fb950; }
  .k-disconnect { color: #f85149; }
  .k-kick { color: #ff7b72; font-weight: 700; }
  .k-ping { color: #6e7681; }
  .ip { color: #6e7681; white-space: nowrap; }
  .route { color: #c9d1d9; }
  .arrow { color: #6e7681; }
  .bcast { color: #d29922; }
  .payload { color: #8b949e; white-space: pre-wrap; word-break: break-word; max-width: 600px; }
  .ts { color: #6e7681; white-space: nowrap; }
  .loop td { background: #3d1d1d !important; }
  .loop .route { color: #ff7b72; font-weight: 700; }
  tr.ping { opacity: 0.45; }
</style>
</head>
<body>
<header>
  <div id="dot"></div>
  <h1>AgentBus Monitor</h1>
  <span class="stat">events <b id="count">0</b></span>
  <span class="stat">rate <b id="rate">0</b>/s</span>
  <input id="token" placeholder="filter token (optional)" size="20">
  <select id="kind">
    <option value="">all kinds</option>
    <option value="message">message</option>
    <option value="task">task</option>
    <option value="task_complete">task_complete</option>
    <option value="register">register</option>
    <option value="disconnect">disconnect</option>
  </select>
  <label class="stat"><input type="checkbox" id="hidePing" checked> hide pings</label>
  <button id="pause">Pause</button>
  <button id="clear">Clear</button>
  <span style="border-left:1px solid #30363d; height:22px"></span>
  <input id="kickId" placeholder="agent to kick" size="12">
  <button id="kick" title="Disconnect agent (it will auto-reconnect)">Kick</button>
  <button id="purge" title="Disconnect ALL agents and reset the bus" style="background:#da3633;border-color:#da3633;color:#fff">Purge All</button>
  <span class="stat" id="loopwarn" style="color:#ff7b72"></span>
</header>
<table>
  <thead><tr>
    <th>#</th><th>time</th><th>kind</th><th>route</th><th>ip</th><th>payload</th><th>→</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
<script>
const rows = document.getElementById('rows');
const dot = document.getElementById('dot');
let paused = false, total = 0, recent = [], loopHits = {};

document.getElementById('pause').onclick = e => {
  paused = !paused; e.target.textContent = paused ? 'Resume' : 'Pause';
  e.target.classList.toggle('on', paused);
};
document.getElementById('clear').onclick = () => { rows.innerHTML=''; total=0; cnt.textContent=0; };
const cnt = document.getElementById('count');

function connect() {
  const tok = document.getElementById('token').value.trim();
  const url = '/flow/stream' + (tok ? '?token=' + encodeURIComponent(tok) : '');
  const es = new EventSource(url);
  es.onopen = () => dot.classList.add('live');
  es.onerror = () => { dot.classList.remove('live'); };
  es.onmessage = ev => {
    if (paused) return;
    const e = JSON.parse(ev.data);
    addRow(e);
  };
  return es;
}
let es = connect();
document.getElementById('token').onchange = () => { es.close(); rows.innerHTML=''; es = connect(); };

function addRow(e) {
  const hidePing = document.getElementById('hidePing').checked;
  const kf = document.getElementById('kind').value;
  if (hidePing && e.kind === 'ping') return;
  if (kf && e.kind !== kf) return;

  total++; cnt.textContent = total;
  recent.push(Date.now());

  // loop detection: same source>target seen 3+ times in 4s
  const key = e.source + '>' + e.target;
  const now = Date.now();
  loopHits[key] = (loopHits[key] || []).filter(t => now - t < 4000);
  loopHits[key].push(now);
  const isLoop = loopHits[key].length >= 4;
  if (isLoop) document.getElementById('loopwarn').textContent =
      '⚠ possible loop: ' + key + ' (' + loopHits[key].length + ' in 4s)';

  const tr = document.createElement('tr');
  if (e.kind === 'ping') tr.className = 'ping';
  if (isLoop) tr.className = 'loop';
  const t = new Date(e.ts * 1000).toLocaleTimeString('en-US', {hour12:false}) +
            '.' + String(Math.floor((e.ts % 1) * 1000)).padStart(3,'0');
  const target = e.target === '*' ? '<span class="bcast">*all</span>' : escape(e.target);
  const deliv = e.delivered == null ? '' : e.delivered;
  tr.innerHTML =
    '<td>' + e.seq + '</td>' +
    '<td class="ts">' + t + '</td>' +
    '<td><span class="kind k-' + e.kind + '">' + e.kind + '</span></td>' +
    '<td class="route">' + escape(e.source) + ' <span class="arrow">→</span> ' + target + '</td>' +
    '<td class="ip">' + escape(e.ip || '') + '</td>' +
    '<td class="payload">' + escape(e.payload == null ? '' : (typeof e.payload === 'object' ? JSON.stringify(e.payload) : String(e.payload))) + '</td>' +
    '<td class="ts">' + deliv + '</td>';
  rows.insertBefore(tr, rows.firstChild);
  while (rows.children.length > 500) rows.removeChild(rows.lastChild);
}

// kick an agent (it auto-reconnects)
document.getElementById('kick').onclick = async () => {
  const aid = document.getElementById('kickId').value.trim();
  const tok = document.getElementById('token').value.trim();
  if (!aid) { alert('Enter an agent id to kick'); return; }
  if (!tok) { alert('Set the token filter first (the kicked agent\\'s network)'); return; }
  const r = await fetch('/kick', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token: tok, agent_id: aid, reason: 'kicked from monitor'})
  });
  const d = await r.json();
  document.getElementById('loopwarn').textContent =
    d.kicked ? ('🥾 kicked ' + aid + ' — will reconnect') : ('⚠ ' + (d.message || 'kick failed'));
};

// purge all agents
document.getElementById('purge').onclick = async () => {
  if (!confirm('Purge ALL agents from the bus? They will auto-reconnect.')) return;
  const tok = document.getElementById('token').value.trim();
  if (!tok) { alert('Set the token filter first (which network to purge)'); return; }
  const warn = document.getElementById('loopwarn');
  warn.textContent = '🔄 Purging all agents...';
  try {
    const r = await fetch('/purge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: tok})
    });
    const d = await r.json();
    warn.textContent = '✅ Purged ' + d.kicked + ' agents — they will reconnect';
  } catch(e) {
    warn.textContent = '⚠ Purge failed: ' + e.message;
  }
};

function escape(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

// rate counter
setInterval(() => {
  const now = Date.now();
  recent = recent.filter(t => now - t < 1000);
  document.getElementById('rate').textContent = recent.length;
}, 500);
</script>
</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="AgentBus WebSocket Server — Multi-agent meeting point")
    parser.add_argument("--ws-host", default="0.0.0.0", help="WebSocket host")
    parser.add_argument("--ws-port", type=int, default=9876, help="WebSocket port (default: 9876)")
    parser.add_argument("--http-port", type=int, default=9877, help="HTTP health port (default: 9877)")
    parser.add_argument("--entry-token", default="", help="Optional entry token for the network — recorded for use by /roll. Without --rolling-channel agents stay on the token they register with (no redirect).")
    parser.add_argument("--rolling-channel", action="store_true", help="Enable hashed channel redirect: agents registering with --entry-token are redirected to a derived channel hash. OFF by default (avoids split-brain when agents miss the redirect).")
    args = parser.parse_args()

    try:
        import websockets
    except ImportError:
        print("❌ 'websockets' required: pip install websockets")
        import sys
        sys.exit(1)

    import sys

    ws_server = WebSocketAgentBusServer(
        http_port=args.http_port,
        entry_token=args.entry_token or None,
        rolling_channel=args.rolling_channel,
    )
    http_handler = HTTPHealthHandler(ws_server)

    http_server = await asyncio.start_server(
        http_handler.handle_request,
        args.ws_host, args.http_port,
    )

    ws = await websockets.serve(
        ws_server.handle_client,
        args.ws_host, args.ws_port,
        ping_interval=30,
        ping_timeout=10,
    )

    print(f"""
+{'='*50}+
|    AgentBus Server -- Multi-Agent Network     |
+{'-'*50}+
|  WebSocket:  ws://{args.ws_host}:{args.ws_port}
|  HTTP:       http://{args.ws_host}:{args.http_port}
|  Monitor:    http://{args.ws_host}:{args.http_port}/monitor   (live flow viewer)
|
|  Agents connect with a shared token.
|  Same token = same private network.
|
|  Connect an agent (Python):
|    ws = await websockets.connect(
|      'ws://{args.ws_host}:{args.ws_port}')
|    await ws.send(json.dumps({{
|      'type': 'register',
|      'agent_id': 'my_agent',
|      'token': 'my_token',
|    }}))
+{'-'*50}+
    """)

    try:
        await asyncio.sleep(86400 * 365)
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        ws.close()
        http_server.close()


if __name__ == "__main__":
    asyncio.run(main())
