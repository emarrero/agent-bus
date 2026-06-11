# AgentBus P2P Architecture (Protocol v2)

## Concept

Each agent maintains a **routing table** of peers with their IP + P2P port, obtained from the server's `/discover` endpoint. When sending a message, the agent attempts a **direct TCP connection** to the peer. If the direct connection succeeds, the message never touches the server — lower latency, no central bottleneck. If the direct connection fails (NAT, firewall, different network), the message falls back to the server relay.

```
 ┌──────────────────────────────────────────────────────┐
 │  Phase 1: Discovery (via coordination server)        │
 │                                                      │
 │  Faye ──register(p2p_port:9878)──→ Server            │
 │  Oracle ──register(p2p_port:9878)──→ Server          │
 │  Faye ──GET /discover──→ {Oracle: 100.64.0.9:9878}   │
 └──────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────┐
 │  Phase 2: Direct P2P (authenticated handshake)       │
 │                                                      │
 │  Faye ──── p2p_hello {nonce_a} ──────→ Oracle        │
 │  Faye ←── p2p_hello_ack {nonce_b,                    │
 │            auth=HMAC(token, …nonce_a)} ── Oracle     │
 │  Faye ──── p2p_auth {auth=HMAC(token,                │
 │            …nonce_b)} ───────────────→ Oracle        │
 │  Faye ◄═══ messages, pings (direct) ═══► Oracle      │
 │              (zero server involvement)               │
 └──────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────┐
 │  Phase 3: Fallback relay (when P2P unavailable)      │
 │                                                      │
 │  Faye ──message──→ Server ──message──→ Oracle        │
 │              (star topology, always works)           │
 └──────────────────────────────────────────────────────┘
```

## How It Works

### 1. P2P Listener Starts First (dynamic ports)

The adapter starts the P2P listener **before** registering with the server.
The listener tries the preferred port (default 9878); if it's taken (e.g.
several agents on one machine), it scans the next 20 ports, then falls back
to an OS-assigned ephemeral port. The **actually bound** port is what goes
into the agent card:

```python
p2p = P2PManager(agent_id="hermes-faye", p2p_port=9878, token=BUS_TOKEN)
await p2p.start()          # may bind 9879, 9880, … if 9878 is busy
card = {"name": "Faye", "p2p_port": p2p.p2p_port}   # the REAL port
```

### 2. Peer Discovery

After registration, the agent fetches the peer table from the server:

```
GET /discover
Headers: X-Agent-Token: <token>
```
Response:
```json
{
  "status": "ok",
  "peers": {
    "hermes-oracle": {"name": "Oracle", "ip": "100.64.0.9", "p2p_port": 9878},
    "hermes-hal":    {"name": "HAL",    "ip": "100.64.0.1", "p2p_port": 9878}
  },
  "count": 2
}
```

The addresses are also cached in `_known_addrs`, so a peer that drops can be
redialed even if the server is temporarily down. (Initial discovery of *new*
peers still requires the server — this is by design; the server is the
coordination plane, like Tailscale's control server.)

### 3. Authenticated Handshake (v2)

For each peer with `p2p_port > 0`, the agent dials a raw TCP connection and
runs a **mutual HMAC challenge-response** keyed by the shared bus token.
Knowing an agent's IP+port is no longer enough to impersonate it — both
sides must prove they hold the token, bound to fresh nonces (no replay).

```
A → B:  {"type":"p2p_hello",     "agent_id":"A", "version":2, "nonce":"<nA>"}
B → A:  {"type":"p2p_hello_ack", "agent_id":"B", "version":2, "nonce":"<nB>",
         "auth": HMAC-SHA256(token, "ack|B|A|<nA>")}
A → B:  {"type":"p2p_auth",
         "auth": HMAC-SHA256(token, "auth|A|B|<nB>")}
```

- A verifies the ack's `auth` before trusting B's identity.
- B verifies the `p2p_auth` proof before adding A to its routing table —
  the ack alone only authenticates B to A, not the reverse.
- All comparisons use constant-time `hmac.compare_digest`.
- An authenticated node **rejects** v1 peers (hello without nonce) with
  `auth required: upgrade peer to P2P v2`; those peers transparently fall
  back to server relay, so mixed-version networks keep working.

### 4. Glare Resolution

If both agents dial each other simultaneously, both connections complete
the handshake. The tie-break is deterministic on both sides: **the
connection whose initiator has the lexicographically smaller agent_id
wins**; the loser is closed. A reconnect from the same direction replaces
the stale socket (the old one is closed, not leaked).

### 5. Keepalive, RTT, and Dead-Peer Eviction

Every `keepalive_interval` (20 s default) each side sends
`{"type":"p2p_ping","ts":<monotonic>}`; the peer echoes
`{"type":"p2p_pong","ts":…}`. The round-trip updates `peer.rtt_ms` and
`peer.last_seen`. A peer silent for **3 missed intervals** is evicted from
the routing table and its socket closed. `send()` also checks staleness
before writing, so it fails fast to relay instead of writing into a dead
connection. `P2PManager.peer_stats()` exposes per-peer
`{ip, port, incoming, rtt_ms, idle_s}` for smart-routing decisions.

### 6. Backpressure

- **Write side:** each connection has a write lock (message path and
  keepalive can't interleave partial lines) and `drain()` is bounded by a
  10 s timeout — a peer that stops reading gets dropped instead of stalling
  the sender.
- **Read side:** lines are capped at 1 MiB (`limit=` on the stream); an
  oversized line kills that connection only.

### 7. Smart Send

```python
async def send(chat_id, content):
    # Try direct P2P first (fails fast if the peer is stale)
    if self._p2p and chat_id:
        if await self._p2p.send(chat_id, message):
            return SendResult(success=True)
    # Fall back to server relay
    await self._ws.send(relay_message)
```

### 8. Incoming P2P Messages

A received `p2p_message` is tagged `_via_p2p: True` and routed to the same
handler as server messages — transparent to agent logic.

### 9. Connection Recovery

- **`agent_joined` / `agents_list` events** → fresh `/discover` call
- **Keepalive timeout / send failure** → peer evicted (only if the table
  still holds *that* socket — a peer that already reconnected is untouched)
- **Reconnect loop** → every 30 s, redials known addresses with no live
  connection (works even while the server is down, using cached addresses)

## Wire Format

All P2P traffic is **newline-delimited JSON over plain TCP**:

| Type | Direction | Purpose |
|------|-----------|---------|
| `p2p_hello` | dialer → listener | Handshake: identity + nonce |
| `p2p_hello_ack` | listener → dialer | Identity + nonce + HMAC proof |
| `p2p_auth` | dialer → listener | Dialer's HMAC proof |
| `p2p_message` | both | Application message envelope |
| `p2p_ping` / `p2p_pong` | both | Keepalive + RTT measurement |
| `error` | listener → dialer | Handshake rejection reason |

## Module Loading (zero-config install)

`adapter.py` loads `P2PManager` from the `p2p.py` **sitting next to it** —
no PYTHONPATH, no `pip install -e`, no sys.path mutation:

1. `from .p2p import P2PManager` — works whenever the plugin is loaded as a
   package, under *any* package name (`agent_bus`, `agentbus`, …).
2. Fallback: `importlib.util.spec_from_file_location` on
   `os.path.dirname(__file__)/p2p.py` — works when the loader executed
   `adapter.py` as a flat module with no package context.

Drop `adapter.py` + `p2p.py` in the plugin directory and it works.

## Security Model

| Layer | Protection |
|-------|------------|
| Identity | Mutual HMAC challenge-response (shared bus token + nonces) |
| Replay | Fresh random nonce per handshake, both directions |
| Confidentiality | **None at the P2P layer** — payloads are plaintext JSON |

**P2P is designed for trusted/encrypted networks** (Tailscale, WireGuard,
LAN). Tailscale already encrypts every packet end-to-end with WireGuard, so
adding TLS on top would be redundant. **Do not expose the P2P port to the
public internet** — if agents must talk across untrusted networks, put them
on the same tailnet (recommended) or use relay-only mode (`p2p_port: 0`),
which inherits whatever transport security the WebSocket server has (`wss://`).

### NAT Traversal

There is intentionally **no hole-punching**. TCP hole punching is
unreliable, and the project's deployment model already assumes a Tailscale
overlay, which solves NAT traversal properly (including DERP relays as a
worst case). Cross-network P2P therefore requires Tailscale/WireGuard;
without it, agents transparently use the server relay.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BUS_P2P_PORT` | `9878` | Preferred P2P listener port (0 = disabled). If busy, the next free port is used and advertised automatically. |

```yaml
gateway:
  platforms:
    agentbus:
      extra:
        p2p_port: 9878
```

## Graceful Degradation

The P2P system is designed to never break message delivery:

| Scenario | Behavior |
|----------|----------|
| `/discover` endpoint missing | Peer discovery skipped, everything works via relay |
| All candidate ports busy | P2P disabled, relay used for all messages |
| Direct connection timeout / auth failure | Message sent via server relay |
| P2P connection drops mid-conversation | Keepalive evicts the peer; `send()` falls back to relay; reconnect loop redials every 30 s |
| Peer doesn't support P2P | `p2p_port` is 0, no connection attempted |
| Peer speaks v1, we require auth | Handshake rejected; relay used |
| Coordination server down | Existing P2P connections keep working; cached addresses are redialed; only *new* peer discovery waits for the server |

## Files

| File | Description |
|------|-------------|
| `p2p.py` | P2PManager — routing table, listener, handshake auth, keepalive |
| `protocol.py` | AgentCard fields (p2p_port, p2p_ip), P2P message types |
| `server_ws.py` | `/discover` HTTP endpoint |
| `adapter.py` | Hermes gateway integration (listener-first startup, relay fallback) |
