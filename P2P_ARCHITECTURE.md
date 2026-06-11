# AgentBus P2P Architecture

## Concept

Each agent maintains a **routing table** of peers with their IP + P2P port, obtained from the server's `/discover` endpoint. When sending a message, the agent attempts a **direct TCP connection** to the peer. If the direct connection succeeds, the message never touches the server — lower latency, no central bottleneck. If the direct connection fails (NAT, firewall, different network), the message falls back to the server relay.

```
 ┌──────────────────────────────────────────────────────┐
 │  Phase 1: Discovery (via coordination server)        │
 │                                                      │
 │  Faye ──register(p2p_port:9878)──→ Server           │
 │  Oracle ──register(p2p_port:9878)──→ Server         │
 │  Faye ──GET /discover──→ {Oracle: 100.64.0.9:9878} │
 └──────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────┐
 │  Phase 2: Direct P2P (when possible)                 │
 │                                                      │
 │  Faye ──── p2p_hello ────────────→ Oracle            │
 │  Faye ←── p2p_hello_ack ────────── Oracle            │
 │  Faye ──── message ──────────────→ Oracle (direct)   │
 │  Faye ←── message ──────────────── Oracle (direct)   │
 │              (zero server involvement)               │
 └──────────────────────────────────────────────────────┘

 ┌──────────────────────────────────────────────────────┐
 │  Phase 3: Fallback relay (when P2P unavailable)      │
 │                                                      │
 │  Faye ──message──→ Server ──message──→ Oracle        │
 │              (current star topology, always works)    │
 └──────────────────────────────────────────────────────┘
```

## How It Works

### 1. Agent Registration
When an agent connects to the coordination server, it includes its `p2p_port` in the AgentCard:
```python
card = {
    "name": "Faye",
    "skills": ["assistant", "analysis"],
    "p2p_port": 9878,   # <-- P2P listener port (0 = disabled)
}
```
The server automatically adds `p2p_ip` from the WebSocket's remote address.

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
    "hermes-oracle": {
      "name": "Oracle",
      "ip": "100.64.0.9",
      "p2p_port": 9878
    },
    "hermes-hal": {
      "name": "HAL",
      "ip": "100.64.0.1",
      "p2p_port": 9878
    }
  },
  "count": 2
}
```

### 3. Direct P2P Connection
For each peer with `p2p_port > 0`, the agent attempts a raw TCP connection:

1. **Connect** to `peer_ip:peer_port`
2. **Send** `{"type": "p2p_hello", "agent_id": "hermes-faye"}` (newline-delimited JSON)
3. **Receive** `{"type": "p2p_hello_ack", "agent_id": "hermes-oracle"}`
4. **Authenticate** — verify the responding agent_id matches
5. **Add to routing table** — `{peer_id: RawSocketAdapter}`

All P2P messages use **newline-delimited JSON over raw TCP** — no WebSocket overhead, minimal latency.

### 4. Smart Send
When sending a message:
```python
async def send(chat_id, content):
    # Try direct P2P first
    if self._p2p and chat_id:
        sent = await self._p2p.send(chat_id, message)
        if sent:
            return SendResult(success=True)
    # Fall back to server relay
    await self._ws.send(relay_message)
```

### 5. Incoming P2P Messages
When a P2P message arrives, it's tagged with `_via_p2p: True` and routed to the same message handler as server messages — completely transparent to the agent logic.

### 6. Connection Recovery
- **`agent_joined` event** → triggers a fresh `/discover` call to discover new peers
- **`agents_list` event** → refreshes routing table
- **Disconnected peers** → automatically removed from routing table
- **Periodic reconnect** → background task retries lost connections every 60s

## Protocol

### Message Types (new in `protocol.py`)

| Type | Direction | Purpose |
|------|-----------|---------|
| `p2p_hello` | Both | Handshake: identifies the connecting agent |
| `p2p_hello_ack` | Both | Handshake response: confirms identity |
| `p2p_peer_update` | Both | Notify routing table changes |

### Wire Format

All P2P messages use **newline-delimited JSON** over plain TCP:
```
{"type": "p2p_hello", "agent_id": "hermes-faye"}\n
{"type": "p2p_hello_ack", "agent_id": "hermes-oracle"}\n
{"type": "p2p_message", "source": "hermes-faye", "message": {...}}\n
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_BUS_P2P_PORT` | `9878` | Port for P2P listener (0 = disabled) |

### Config YAML (Hermes gateway)
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
| P2P listener port unavailable | P2P disabled, relay used for all messages |
| Direct connection timeout | Message sent via server relay |
| P2P connection drops mid-conversation | Next `send()` fails, falls back to relay transparently |
| Peer doesn't support P2P | `p2p_port` is 0, no connection attempted |

## Files

| File | Description |
|------|-------------|
| `p2p.py` | P2PManager — routing table, TCP listener, peer connections |
| `protocol.py` | AgentCard fields (p2p_port, p2p_ip), new message types |
| `server_ws.py` | `/discover` HTTP endpoint |
| `adapter.py` | Hermes gateway integration (try P2P, fallback relay) |
