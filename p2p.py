"""AgentBus P2P — Direct agent-to-agent connections (raw TCP).

Each agent listens on a P2P port for incoming direct connections and
maintains a routing table of peers. When sending a message, the agent
prefers a direct P2P connection but falls back to the server relay.

Messages use newline-delimited JSON over raw TCP — no WebSocket overhead,
lower latency, simpler protocol.


Usage (inside an agent's connect loop):
    p2p = P2PManager(agent_id="hermes-faye", p2p_port=9878)
    await p2p.start()
    # After receiving /discover data:
    await p2p.update_peers(discover_data)
    # Send a message:
    success = await p2p.send("hermes-oracle", message_dict)
    if not success:
        # Fall back to server relay
        await server_send(...)
    await p2p.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("agent-bus.p2p")


class P2PManager:
    """Manages direct P2P WebSocket connections to other agents.

    Routing table: agent_id → P2PPeer(ws, ip, port, connected_at)
    """

    def __init__(
        self,
        agent_id: str,
        p2p_port: int = 0,
        listen_ip: str = "0.0.0.0",
        auto_reconnect: bool = True,
    ):
        self.agent_id = agent_id
        self.p2p_port = p2p_port
        self.listen_ip = listen_ip
        self.auto_reconnect = auto_reconnect

        # Routing table: agent_id → connection info
        self._peers: dict[str, P2PPeer] = {}

        # P2P listener server
        self._server: asyncio.Server | None = None

        # Background tasks
        self._listener_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None

        # Lock for routing table
        self._lock = asyncio.Lock()

        # Flag
        self._running = False

    @property
    def peer_count(self) -> int:
        return len(self._peers)

    @property
    def peer_ids(self) -> list[str]:
        return list(self._peers.keys())

    async def start(self) -> None:
        """Start the P2P listener so other agents can connect to us."""
        if self._running:
            return

        self._running = True

        if self.p2p_port > 0:
            try:
                self._server = await asyncio.start_server(
                    self._handle_incoming,
                    host=self.listen_ip,
                    port=self.p2p_port,
                )
                self._listener_task = asyncio.create_task(
                    self._server.serve_forever(),
                    name=f"p2p-listener-{self.agent_id}",
                )
                logger.info(
                    "🔗 P2P listener on %s:%d — accepting direct connections",
                    self.listen_ip, self.p2p_port,
                )
            except OSError as exc:
                logger.warning("⚠️ P2P listener on port %d failed: %s", self.p2p_port, exc)
                self.p2p_port = 0  # Disable P2P, fall back to relay only

        if self.auto_reconnect:
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(),
                name=f"p2p-reconnect-{self.agent_id}",
            )

        logger.info("🚀 P2P Manager started (port=%s, %d known peer(s))",
                     self.p2p_port or "relay-only", len(self._peers))

    async def stop(self) -> None:
        """Stop the P2P listener and disconnect all peers."""
        self._running = False

        # Cancel background tasks
        for t in [self._listener_task, self._reconnect_task]:
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        # Close server
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Disconnect all peers
        async with self._lock:
            for peer_id, peer in list(self._peers.items()):
                await peer.close()
            self._peers.clear()

        logger.info("🛑 P2P Manager stopped")

    async def update_peers(self, discover_data: dict) -> None:
        """Update routing table from server's /discover response.

        ``discover_data`` is the JSON response with ``peers`` dict:
            {agent_id: {name, ip, p2p_port}}
        """
        peers = discover_data.get("peers", {})
        my_ip_guess = discover_data.get("my_ip", "")

        for peer_id, info in peers.items():
            if peer_id == self.agent_id:
                continue  # Skip self

            p2p_port = info.get("p2p_port", 0)
            ip = info.get("ip", "")

            if not p2p_port or not ip:
                continue  # Peer doesn't support P2P

            async with self._lock:
                if peer_id in self._peers:
                    # Already connected; update info
                    self._peers[peer_id].ip = ip
                    self._peers[peer_id].port = p2p_port
                    continue

            # New peer — try to connect
            asyncio.create_task(
                self._connect_to_peer(peer_id, ip, p2p_port),
                name=f"p2p-connect-{peer_id}",
            )

    async def _connect_to_peer(self, peer_id: str, ip: str, port: int) -> None:
        """Try to establish a direct P2P connection to a peer using raw TCP."""
        url = f"{ip}:{port}"
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=5,
            )

            # Send p2p_hello with our identity (newline-delimited JSON)
            hello = json.dumps({
                "type": "p2p_hello",
                "agent_id": self.agent_id,
            })
            writer.write(hello.encode() + b"\n")
            await writer.drain()

            # Wait for p2p_hello_ack
            resp_raw = await asyncio.wait_for(reader.readline(), timeout=5)
            resp = json.loads(resp_raw.decode().strip())

            if resp.get("type") != "p2p_hello_ack":
                logger.warning("⚠️ P2P %s → %s: unexpected response: %s",
                               self.agent_id, peer_id, resp_raw.decode()[:100])
                writer.close()
                return

            remote_id = resp.get("agent_id", "")
            if remote_id != peer_id:
                logger.warning("⚠️ P2P %s → %s: agent_id mismatch (got '%s')",
                               self.agent_id, peer_id, remote_id)
                writer.close()
                return

            # Wrap as RawSocketAdapter for uniform message handling
            adapter = _RawSocketAdapter(reader, writer)

            # Store in routing table
            async with self._lock:
                self._peers[peer_id] = P2PPeer(
                    agent_id=peer_id,
                    ws=adapter,
                    ip=ip,
                    port=port,
                )

            # Start background reader for this peer
            asyncio.create_task(
                self._read_from_peer(peer_id, adapter),
                name=f"p2p-reader-{peer_id}",
            )

            logger.info("🔗 P2P connected: %s ↔ %s  (%s:%d)",
                        self.agent_id, peer_id, ip, port)

        except asyncio.TimeoutError:
            logger.debug("⏱️ P2P timeout connecting to %s (%s:%d) — relay fallback",
                         peer_id, ip, port)
        except Exception as exc:
            logger.debug("⚠️ P2P connect to %s (%s:%d) failed: %s — relay fallback",
                         peer_id, ip, port, exc)

    async def _handle_incoming(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming TCP connection (upgrade to WebSocket)."""
        # We use raw TCP for simplicity, but the peers will try to
        # establish WebSocket. Let's do a simple protocol:
        # 1. Read the p2p_hello JSON line
        # 2. Respond with p2p_hello_ack
        # 3. Maintain a bidirectional message channel
        peer_ip, peer_port = writer.get_extra_info("peername", ("?", 0))
        peer_id = "?"

        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            data = json.loads(raw.decode().strip())

            if data.get("type") != "p2p_hello":
                writer.write(json.dumps({"type": "error", "message": "Expected p2p_hello"}).encode() + b"\n")
                await writer.drain()
                writer.close()
                return

            peer_id = data.get("agent_id", "unknown")
            if not peer_id:
                writer.write(json.dumps({"type": "error", "message": "agent_id required"}).encode() + b"\n")
                await writer.drain()
                writer.close()
                return

            # Send hello ack
            ack = json.dumps({
                "type": "p2p_hello_ack",
                "agent_id": self.agent_id,
            })
            writer.write(ack.encode() + b"\n")
            await writer.drain()

            # Wrap raw socket as a P2PPeer (simple text-line protocol)
            ws_adapter = _RawSocketAdapter(reader, writer)

            async with self._lock:
                self._peers[peer_id] = P2PPeer(
                    agent_id=peer_id,
                    ws=ws_adapter,  # duck-typed: has .send() and __aiter__
                    ip=peer_ip,
                    port=peer_port,
                    incoming=True,
                )

            logger.info("🔗 P2P incoming connection from %s (%s:%d)",
                        peer_id, peer_ip, peer_port)

            # Start background reader for this peer
            asyncio.create_task(
                self._read_from_peer(peer_id, ws_adapter),
                name=f"p2p-reader-{peer_id}",
            )

        except asyncio.TimeoutError:
            logger.debug("⏱️ P2P incoming timeout from %s:%d", peer_ip, peer_port)
            writer.close()
        except Exception as exc:
            logger.debug("⚠️ P2P incoming error from %s:%d: %s", peer_ip, peer_port, exc)
            # Clean up if we registered them
            if peer_id and peer_id != "?":
                async with self._lock:
                    self._peers.pop(peer_id, None)
            writer.close()

    async def send(self, target: str, message: dict) -> bool:
        """Send a message via P2P direct connection.

        Returns True if sent via P2P, False if caller should use server relay.
        """
        async with self._lock:
            peer = self._peers.get(target)

        if peer is None or not peer.is_connected:
            return False  # No direct connection — caller uses relay

        try:
            payload = json.dumps({
                "type": "p2p_message",
                "message": message,
                "source": self.agent_id,
            })
            await peer.ws.send(payload)
            return True
        except Exception as exc:
            logger.warning("⚠️ P2P send to %s failed: %s — removing from routing table", target, exc)
            async with self._lock:
                self._peers.pop(target, None)
            return False

    async def _read_from_peer(self, peer_id: str, ws_adapter: Any) -> None:
        """Background reader for an established P2P connection."""
        try:
            async for raw in ws_adapter:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "p2p_message":
                    message = data.get("message", {})
                    # Re-emit as if it came from the bus server
                    message["source"] = message.get("source", peer_id)
                    message["_via_p2p"] = True
                    # Put into the main inbox for processing
                    if self._on_message:
                        await self._on_message(message)

                elif msg_type == "pong":
                    pass  # Keepalive

        except Exception as exc:
            logger.debug("P2P reader for %s ended: %s", peer_id, exc)
        finally:
            async with self._lock:
                self._peers.pop(peer_id, None)
            logger.info("🔌 P2P disconnected: %s", peer_id)

    async def _reconnect_loop(self) -> None:
        """Periodically try to reconnect to peers we lost."""
        while self._running:
            await asyncio.sleep(60)
            # Reconnection happens automatically when update_peers is called
            # after receiving a fresh agents_list from the server.

    # ── Incoming message callback ──────────────────────────────────────

    _on_message = None

    def on_message(self, callback):
        """Register a callback for incoming P2P messages.

        The callback receives the parsed message dict.
        """
        self._on_message = callback
        return self


class P2PPeer:
    """Represents a direct P2P connection to another agent."""

    def __init__(
        self,
        agent_id: str,
        ws,
        ip: str = "",
        port: int = 0,
        incoming: bool = False,
    ):
        self.agent_id = agent_id
        self.ws = ws
        self.ip = ip
        self.port = port
        self.incoming = incoming

    @property
    def is_connected(self) -> bool:
        return self.ws is not None

    async def close(self) -> None:
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


class _RawSocketAdapter:
    """Duck-typed WebSocket adapter for raw TCP sockets.

    Implements ``send()`` and ``__aiter__`` so it can be used
    where a websockets library WebSocket is expected.
    Uses newline-delimited JSON for framing.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def send(self, data: str) -> None:
        self._writer.write(data.encode() + b"\n")
        await self._writer.drain()

    async def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise StopAsyncIteration
            line = raw.decode().strip()
            if line:
                return line
