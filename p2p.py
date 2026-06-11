"""AgentBus P2P — Direct agent-to-agent connections (raw TCP).

Each agent listens on a P2P port for incoming direct connections and
maintains a routing table of peers. When sending a message, the agent
prefers a direct P2P connection but falls back to the server relay.

Messages use newline-delimited JSON over raw TCP — no WebSocket overhead,
lower latency, simpler protocol.

Protocol v2 adds:
- HMAC challenge-response handshake (mutual auth using the shared bus token)
- Keepalive ping/pong with RTT tracking and dead-peer eviction
- Dynamic port allocation (scan upward from the preferred port)
- Glare resolution (deterministic winner when both sides dial at once)
- Read limits and drain timeouts (backpressure / slow-peer protection)

Usage (inside an agent's connect loop):
    p2p = P2PManager(agent_id="hermes-faye", p2p_port=9878, token=BUS_TOKEN)
    await p2p.start()          # binds listener; p2p.p2p_port is the REAL port
    # Register with the server using p2p.p2p_port, then after /discover:
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
import hashlib
import hmac as hmac_mod
import json
import logging
import secrets
import time
from typing import Any, Callable

logger = logging.getLogger("agent-bus.p2p")

PROTOCOL_VERSION = 2

# How many ports above the preferred one to try before giving up and
# letting the OS pick an ephemeral port.
PORT_SCAN_RANGE = 20

# Largest accepted JSON line (1 MiB). Protects the reader from a peer
# streaming an unbounded line.
MAX_LINE_BYTES = 1024 * 1024

# Keepalive cadence. A peer that misses 3 intervals is evicted.
KEEPALIVE_INTERVAL = 20.0
KEEPALIVE_MISSES = 3

# How long send() waits for the kernel buffer to drain before declaring
# the peer too slow and dropping the connection.
DRAIN_TIMEOUT = 10.0

HANDSHAKE_TIMEOUT = 10.0
CONNECT_TIMEOUT = 5.0
RECONNECT_INTERVAL = 30.0


def _auth_tag(token: str, *parts: str) -> str:
    """HMAC-SHA256 over the handshake transcript, keyed by the bus token."""
    msg = "|".join(parts).encode()
    return hmac_mod.new(token.encode(), msg, hashlib.sha256).hexdigest()


class P2PManager:
    """Manages direct P2P TCP connections to other agents.

    Routing table: agent_id → P2PPeer(conn, ip, port, rtt, last_seen)
    """

    def __init__(
        self,
        agent_id: str,
        p2p_port: int = 0,
        listen_ip: str = "0.0.0.0",
        auto_reconnect: bool = True,
        token: str = "",
        keepalive_interval: float = KEEPALIVE_INTERVAL,
    ):
        self.agent_id = agent_id
        self.p2p_port = p2p_port  # preferred port; replaced by the bound port in start()
        self.listen_ip = listen_ip
        self.auto_reconnect = auto_reconnect
        self.token = token  # shared bus token; enables handshake auth when set
        self.keepalive_interval = keepalive_interval

        # Routing table: agent_id → connection info
        self._peers: dict[str, P2PPeer] = {}

        # Last known address per peer (from /discover), used by the
        # reconnect loop even after a connection drops.
        self._known_addrs: dict[str, tuple[str, int]] = {}

        # Peers with a dial in progress (avoid duplicate connect tasks)
        self._connecting: set[str] = set()

        # P2P listener server
        self._server: asyncio.Server | None = None

        # Background tasks
        self._listener_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None

        # Lock for routing table
        self._lock = asyncio.Lock()

        # Flag
        self._running = False

        # Incoming message callback
        self._on_message: Callable | None = None

    @property
    def peer_count(self) -> int:
        return len(self._peers)

    @property
    def peer_ids(self) -> list[str]:
        return list(self._peers.keys())

    def peer_stats(self) -> dict[str, dict[str, Any]]:
        """Per-peer routing info: address, direction, RTT, last activity."""
        now = time.monotonic()
        return {
            pid: {
                "ip": p.ip,
                "port": p.port,
                "incoming": p.incoming,
                "rtt_ms": p.rtt_ms,
                "idle_s": round(now - p.last_seen, 1),
            }
            for pid, p in self._peers.items()
        }

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the P2P listener so other agents can connect to us.

        Tries the preferred port first, then scans upward, then falls back
        to an OS-assigned ephemeral port. After this returns, ``p2p_port``
        holds the port actually bound — advertise THAT in the agent card.
        """
        if self._running:
            return

        self._running = True

        if self.p2p_port > 0:
            candidates = [self.p2p_port + i for i in range(PORT_SCAN_RANGE)] + [0]
            for candidate in candidates:
                try:
                    self._server = await asyncio.start_server(
                        self._handle_incoming,
                        host=self.listen_ip,
                        port=candidate,
                        limit=MAX_LINE_BYTES,
                    )
                    bound = self._server.sockets[0].getsockname()[1]
                    if bound != self.p2p_port:
                        logger.info("P2P port %d busy — bound %d instead",
                                    self.p2p_port, bound)
                    self.p2p_port = bound
                    break
                except OSError:
                    continue

            if self._server is None:
                logger.warning("⚠️ P2P listener failed on all candidate ports — relay only")
                self.p2p_port = 0
            else:
                self._listener_task = asyncio.create_task(
                    self._server.serve_forever(),
                    name=f"p2p-listener-{self.agent_id}",
                )
                logger.info(
                    "🔗 P2P listener on %s:%d — accepting direct connections%s",
                    self.listen_ip, self.p2p_port,
                    " (authenticated)" if self.token else " (NO AUTH — token unset)",
                )

        if self.auto_reconnect:
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(),
                name=f"p2p-reconnect-{self.agent_id}",
            )

        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(),
            name=f"p2p-keepalive-{self.agent_id}",
        )

        logger.info("🚀 P2P Manager started (port=%s, %d known peer(s))",
                    self.p2p_port or "relay-only", len(self._peers))

    async def stop(self) -> None:
        """Stop the P2P listener and disconnect all peers."""
        self._running = False

        # Close peer connections FIRST: on Python 3.12+ both a cancelled
        # serve_forever() and Server.wait_closed() block until every client
        # transport is gone, and incoming peer sockets belong to the server.
        async with self._lock:
            for peer in self._peers.values():
                await peer.close()
            self._peers.clear()

        # Cancel background tasks (bounded waits — never hang shutdown)
        for t in [self._listener_task, self._reconnect_task, self._keepalive_task]:
            if t and not t.done():
                t.cancel()
                try:
                    await asyncio.wait_for(t, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        self._listener_task = self._reconnect_task = self._keepalive_task = None

        # Close server
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except asyncio.TimeoutError:
                logger.debug("P2P server wait_closed timed out — continuing shutdown")
            self._server = None

        logger.info("🛑 P2P Manager stopped")

    # ── Discovery ──────────────────────────────────────────────────────

    async def update_peers(self, discover_data: dict) -> None:
        """Update routing table from server's /discover response.

        ``discover_data`` is the JSON response with ``peers`` dict:
            {agent_id: {name, ip, p2p_port}}
        """
        peers = discover_data.get("peers", {})

        for peer_id, info in peers.items():
            if peer_id == self.agent_id:
                continue  # Skip self

            p2p_port = info.get("p2p_port", 0)
            ip = info.get("ip", "")

            if not p2p_port or not ip:
                self._known_addrs.pop(peer_id, None)
                continue  # Peer doesn't support P2P

            self._known_addrs[peer_id] = (ip, p2p_port)

            async with self._lock:
                if peer_id in self._peers:
                    # Already connected; update info
                    self._peers[peer_id].ip = ip
                    self._peers[peer_id].port = p2p_port
                    continue

            self._spawn_connect(peer_id, ip, p2p_port)

    def _spawn_connect(self, peer_id: str, ip: str, port: int) -> None:
        if peer_id in self._connecting:
            return
        self._connecting.add(peer_id)
        task = asyncio.create_task(
            self._connect_to_peer(peer_id, ip, port),
            name=f"p2p-connect-{peer_id}",
        )
        task.add_done_callback(lambda _t: self._connecting.discard(peer_id))

    # ── Outgoing connections ───────────────────────────────────────────

    async def _connect_to_peer(self, peer_id: str, ip: str, port: int) -> None:
        """Establish a direct P2P connection (v2 authenticated handshake).

            us → peer:  p2p_hello     {agent_id, version, nonce}
            peer → us:  p2p_hello_ack {agent_id, version, nonce, auth}
                        auth = HMAC(token, "ack" | peer | us | our_nonce)
            us → peer:  p2p_auth      {auth}
                        auth = HMAC(token, "auth" | us | peer | peer_nonce)
        """
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port, limit=MAX_LINE_BYTES),
                timeout=CONNECT_TIMEOUT,
            )

            nonce = secrets.token_hex(16)
            hello: dict[str, Any] = {
                "type": "p2p_hello",
                "agent_id": self.agent_id,
                "version": PROTOCOL_VERSION,
            }
            if self.token:
                hello["nonce"] = nonce
            writer.write(json.dumps(hello).encode() + b"\n")
            await writer.drain()

            resp_raw = await asyncio.wait_for(reader.readline(), timeout=HANDSHAKE_TIMEOUT)
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

            if self.token:
                expected = _auth_tag(self.token, "ack", peer_id, self.agent_id, nonce)
                if not hmac_mod.compare_digest(resp.get("auth", ""), expected):
                    logger.warning("⚠️ P2P %s → %s: handshake auth failed — peer "
                                   "has wrong token or speaks v1", self.agent_id, peer_id)
                    writer.close()
                    return
                peer_nonce = resp.get("nonce", "")
                proof = {
                    "type": "p2p_auth",
                    "auth": _auth_tag(self.token, "auth", self.agent_id, peer_id, peer_nonce),
                }
                writer.write(json.dumps(proof).encode() + b"\n")
                await writer.drain()

            conn = _LineConnection(reader, writer)
            peer = P2PPeer(agent_id=peer_id, conn=conn, ip=ip, port=port)

            if not await self._register_peer(peer):
                await conn.close()
                return

            asyncio.create_task(
                self._read_from_peer(peer),
                name=f"p2p-reader-{peer_id}",
            )

            logger.info("🔗 P2P connected: %s ↔ %s  (%s:%d)",
                        self.agent_id, peer_id, ip, port)

        except asyncio.TimeoutError:
            logger.debug("⏱️ P2P timeout connecting to %s (%s:%d) — relay fallback",
                         peer_id, ip, port)
            if writer:
                writer.close()
        except Exception as exc:
            logger.debug("⚠️ P2P connect to %s (%s:%d) failed: %s — relay fallback",
                         peer_id, ip, port, exc)
            if writer:
                writer.close()

    # ── Incoming connections ───────────────────────────────────────────

    async def _handle_incoming(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming TCP connection: hello → ack(+auth) → [auth proof]."""
        peername = writer.get_extra_info("peername") or ("?", 0)
        peer_ip, peer_port = peername[0], peername[1]

        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=HANDSHAKE_TIMEOUT)
            data = json.loads(raw.decode().strip())

            if data.get("type") != "p2p_hello":
                await self._reject(writer, "Expected p2p_hello")
                return

            peer_id = data.get("agent_id", "")
            if not peer_id:
                await self._reject(writer, "agent_id required")
                return

            peer_nonce = data.get("nonce", "")
            if self.token and not peer_nonce:
                # v1 peer (or stripped hello) against an authenticated node
                await self._reject(writer, "auth required: upgrade peer to P2P v2")
                return

            ack: dict[str, Any] = {
                "type": "p2p_hello_ack",
                "agent_id": self.agent_id,
                "version": PROTOCOL_VERSION,
            }
            my_nonce = secrets.token_hex(16)
            if self.token:
                ack["nonce"] = my_nonce
                ack["auth"] = _auth_tag(self.token, "ack", self.agent_id, peer_id, peer_nonce)
            writer.write(json.dumps(ack).encode() + b"\n")
            await writer.drain()

            if self.token:
                # Require the dialer to prove it holds the token too —
                # the ack alone only authenticates US to THEM.
                proof_raw = await asyncio.wait_for(reader.readline(), timeout=HANDSHAKE_TIMEOUT)
                proof = json.loads(proof_raw.decode().strip())
                expected = _auth_tag(self.token, "auth", peer_id, self.agent_id, my_nonce)
                if proof.get("type") != "p2p_auth" or not hmac_mod.compare_digest(
                    proof.get("auth", ""), expected
                ):
                    logger.warning("⚠️ P2P incoming from '%s' (%s:%s): auth proof failed",
                                   peer_id, peer_ip, peer_port)
                    writer.close()
                    return

            conn = _LineConnection(reader, writer)
            peer = P2PPeer(
                agent_id=peer_id, conn=conn,
                ip=peer_ip, port=peer_port, incoming=True,
            )

            if not await self._register_peer(peer):
                await conn.close()
                return

            asyncio.create_task(
                self._read_from_peer(peer),
                name=f"p2p-reader-{peer_id}",
            )

            logger.info("🔗 P2P incoming connection from %s (%s:%s)",
                        peer_id, peer_ip, peer_port)

        except asyncio.TimeoutError:
            logger.debug("⏱️ P2P incoming timeout from %s:%s", peer_ip, peer_port)
            writer.close()
        except Exception as exc:
            logger.debug("⚠️ P2P incoming error from %s:%s: %s", peer_ip, peer_port, exc)
            writer.close()

    async def _reject(self, writer: asyncio.StreamWriter, reason: str) -> None:
        try:
            writer.write(json.dumps({"type": "error", "message": reason}).encode() + b"\n")
            await writer.drain()
        except Exception:
            pass
        writer.close()

    async def _register_peer(self, peer: P2PPeer) -> bool:
        """Add a freshly handshaken connection to the routing table.

        Resolves glare (both sides dialing at once): the connection whose
        INITIATOR has the lexicographically smaller agent_id wins, so both
        sides deterministically keep the same socket.

        Returns False if the new connection lost the tie-break (caller
        closes it).
        """
        async with self._lock:
            existing = self._peers.get(peer.agent_id)
            if existing and existing.is_connected:
                new_initiator = peer.agent_id if peer.incoming else self.agent_id
                old_initiator = existing.agent_id if existing.incoming else self.agent_id
                if new_initiator == old_initiator:
                    # Same direction — peer reconnected; replace stale socket.
                    await existing.close()
                elif min(new_initiator, old_initiator) == new_initiator:
                    await existing.close()
                else:
                    return False  # keep existing, drop the new one
            self._peers[peer.agent_id] = peer
        return True

    # ── Sending ────────────────────────────────────────────────────────

    async def send(self, target: str, message: dict) -> bool:
        """Send a message via P2P direct connection.

        Returns True if sent via P2P, False if caller should use server relay.
        """
        async with self._lock:
            peer = self._peers.get(target)

        if peer is None or not peer.is_connected:
            return False  # No direct connection — caller uses relay

        # Don't trust a connection the keepalive hasn't heard from in a
        # while — fail fast to relay instead of writing into a black hole.
        if time.monotonic() - peer.last_seen > self.keepalive_interval * KEEPALIVE_MISSES:
            await self._drop_peer(peer, "stale (no keepalive)")
            return False

        try:
            payload = json.dumps({
                "type": "p2p_message",
                "message": message,
                "source": self.agent_id,
            })
            await peer.conn.send(payload)
            return True
        except Exception as exc:
            await self._drop_peer(peer, f"send failed: {exc}")
            return False

    async def _drop_peer(self, peer: P2PPeer, reason: str) -> None:
        logger.warning("⚠️ P2P dropping %s: %s", peer.agent_id, reason)
        async with self._lock:
            # Only evict if the table still holds THIS connection — the
            # peer may have already reconnected with a fresh socket.
            if self._peers.get(peer.agent_id) is peer:
                self._peers.pop(peer.agent_id, None)
        await peer.close()

    # ── Reading ────────────────────────────────────────────────────────

    async def _read_from_peer(self, peer: P2PPeer) -> None:
        """Background reader for an established P2P connection."""
        try:
            async for raw in peer.conn:
                peer.last_seen = time.monotonic()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "p2p_message":
                    message = data.get("message", {})
                    # Re-emit as if it came from the bus server
                    message["source"] = message.get("source", peer.agent_id)
                    message["_via_p2p"] = True
                    if self._on_message:
                        await self._on_message(message)

                elif msg_type == "p2p_ping":
                    try:
                        await peer.conn.send(json.dumps(
                            {"type": "p2p_pong", "ts": data.get("ts")}
                        ))
                    except Exception:
                        break

                elif msg_type == "p2p_pong":
                    ts = data.get("ts")
                    if isinstance(ts, (int, float)):
                        peer.rtt_ms = round((time.monotonic() - ts) * 1000, 1)

        except Exception as exc:
            logger.debug("P2P reader for %s ended: %s", peer.agent_id, exc)
        finally:
            async with self._lock:
                if self._peers.get(peer.agent_id) is peer:
                    self._peers.pop(peer.agent_id, None)
            await peer.close()
            logger.info("🔌 P2P disconnected: %s", peer.agent_id)

    # ── Background maintenance ─────────────────────────────────────────

    async def _keepalive_loop(self) -> None:
        """Ping every peer periodically; evict peers that go silent."""
        while self._running:
            await asyncio.sleep(self.keepalive_interval)
            now = time.monotonic()
            async with self._lock:
                peers = list(self._peers.values())

            for peer in peers:
                if now - peer.last_seen > self.keepalive_interval * KEEPALIVE_MISSES:
                    await self._drop_peer(peer, "keepalive timeout")
                    continue
                try:
                    await peer.conn.send(json.dumps(
                        {"type": "p2p_ping", "ts": time.monotonic()}
                    ))
                except Exception as exc:
                    await self._drop_peer(peer, f"ping failed: {exc}")

    async def _reconnect_loop(self) -> None:
        """Periodically redial known peers we have no live connection to."""
        while self._running:
            await asyncio.sleep(RECONNECT_INTERVAL)
            for peer_id, (ip, port) in list(self._known_addrs.items()):
                if peer_id in self._peers:
                    continue
                self._spawn_connect(peer_id, ip, port)

    # ── Incoming message callback ──────────────────────────────────────

    def on_message(self, callback: Callable) -> "P2PManager":
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
        conn: "_LineConnection",
        ip: str = "",
        port: int = 0,
        incoming: bool = False,
    ):
        self.agent_id = agent_id
        self.conn = conn
        self.ip = ip
        self.port = port
        self.incoming = incoming
        self.connected_at = time.monotonic()
        self.last_seen = time.monotonic()
        self.rtt_ms: float | None = None

    @property
    def is_connected(self) -> bool:
        return self.conn is not None and not self.conn.closed

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()


class _LineConnection:
    """Newline-delimited-JSON framing over an asyncio TCP stream.

    Implements ``send()`` and async iteration over received lines. A
    per-connection write lock keeps concurrent senders (message path +
    keepalive) from interleaving partial writes, and ``drain()`` is bounded
    so one slow peer can't stall its sender forever.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self.closed = False

    async def send(self, data: str) -> None:
        async with self._write_lock:
            self._writer.write(data.encode() + b"\n")
            await asyncio.wait_for(self._writer.drain(), timeout=DRAIN_TIMEOUT)

    async def close(self) -> None:
        self.closed = True
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
                self.closed = True
                raise StopAsyncIteration
            line = raw.decode().strip()
            if line:
                return line
