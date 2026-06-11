## Task: Review & Make AgentBus P2P Production-Ready

AgentBus is a multi-agent messaging network (Hermes gateway plugin at `emarrero/agent-bus`). I added a P2P layer for direct agent-to-agent TCP connections, but it's not establishing connections reliably. Need a thorough code review and fixes.

### Current State

4 agents (Faye, Oracle, HAL, Mariana) all with `p2p_port: 9878` configured. All connect to the bus via server. The P2PManager starts, port 9878 is open, `GET /discover` returns all peers with correct IPs and ports. But **zero P2P connections are established** — all messages go through server relay. No errors in logs, P2PManager just silently doesn't connect.

### Architecture

**Files** (all in `emarrero/agent-bus` repo):

1. **`p2p.py`** — `P2PManager` class
   - `start()` — opens TCP listener, starts reconnect loop
   - `update_peers(discover_data)` — for each peer with p2p_port>0, calls `_connect_to_peer()`
   - `_connect_to_peer()` — opens TCP, does HMAC handshake, adds to routing table
   - `_handle_incoming()` — accepts TCP, does HMAC handshake, adds peer
   - `send(target, message)` — sends via direct connection, returns False for relay fallback
   - **Auth**: HMAC-SHA256 challenge-response using shared bus token
   - **Keepalive**: ping/pong every 20s, evict after 3 misses
   - **Port**: dynamic allocation (scans +20 from preferred port)

2. **`adapter.py`** — Hermes gateway adapter
   - `_get_p2p_manager()` — imports P2PManager from p2p.py via file path (zero-config)
   - `connect()` — starts P2PManager, calls `_discover_peers()`
   - `send()` — tries `self._p2p.send(target, msg)` first, falls back to server WS
   - `_discover_peers()` — HTTP GET to `/discover`, passes to `p2p.update_peers()`
   - Listens for `agent_joined` / `agents_list` events to re-discover

3. **`protocol.py`** — Message types and AgentCard with p2p_port, p2p_ip

4. **`server_ws.py`** — Coordination server with `GET /discover` endpoint

### Known Issues

1. **P2P connections never establish** despite all config being correct. Need to find why `update_peers` doesn't result in connections. Is `_connect_to_peer()` failing silently? Are peers not being iterated? Is the handshake failing?

2. **No logging** — P2PManager uses `logging.getLogger("agent-bus.p2p")` but no messages appear in gateway logs (only "gateway.platforms.agentbus" logger shows). The gateway might be suppressing the "agent-bus.p2p" logger.

3. **Race condition** — `update_peers()` is called from `_discover_peers()` which fires after connect(). But the discover happens once. If peers join later, `agent_joined` triggers rediscover, but this might race with ongoing connection attempts.

4. **No retry** — If `_connect_to_peer()` fails (timeout, auth mismatch), the peer is silently skipped. No retry mechanism. Only retriggered by another `agent_joined` event.

5. **Discover vs agents list mismatch** — `/discover` returns agents from `handle_list_agents()` which reads from `TokenNetwork.agents` dict. But connected WebSocket agents are in `WebSocketAgentBusServer.connections` dict. If an agent disconnects from WS but the card remains in TokenNetwork, discover will list stale agents.

6. **Adapter import** — `_get_p2p_manager()` uses `importlib.util.spec_from_file_location()` which works but is fragile if the plugin dir is in a different location than expected.

7. **No P2P fallback verification** — When `send()` falls back to relay, there's no log message saying "P2P failed, using relay." Just silence.

### Files to Review

https://github.com/emarrero/agent-bus

Key files:
- `p2p.py` (main P2P logic)
- `adapter.py` (gateway integration)
- `protocol.py` (data structures)
- `server_ws.py` (discover endpoint)
- `P2P_ARCHITECTURE.md` (protocol docs)

### What I Need

1. **Root cause analysis** — Why are P2P connections not establishing? Walk through the code path from adapter.connect() → discover → update_peers → _connect_to_peer and identify every possible failure point.

2. **Code fixes** — Specific, minimal changes to make P2P actually connect:
   - Add debug logging that shows up in gateway logs
   - Fix any race conditions or silent failures
   - Add retry logic for failed peer connections
   - Ensure discover returns only currently connected agents

3. **Production hardening**:
   - Handle gateway restart without losing peer table
   - Reconnect P2P if connection drops
   - Log when P2P vs relay is used per message
   - Proper cleanup on disconnect

4. **Security review**: HMAC handshake, token exposure, replay protection

5. **Testing**: Minimal test that two P2PManagers can connect and exchange messages (I have a local test that works, but it doesn't catch the gateway integration issues)

Be specific: line numbers, exact code changes, and why each fix matters.
