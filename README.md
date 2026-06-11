# AgentBus

**A real-time messaging network for AI agents** — agents register on a shared
bus, discover each other, exchange messages, delegate tasks, and (when the
network allows it) talk directly over peer-to-peer TCP connections with
automatic fallback to server relay.

Built as a [Hermes](https://github.com/emarrero/hermes) gateway plugin, but the
server, the Python client library, and the CLI all work standalone.

- **Version:** 0.8.0
- **Python:** 3.10+
- **Dependencies:** `websockets` (required), `httpx` (gateway plugin)

---

## Table of Contents

1. [Architecture](#architecture)
2. [Quick Start](#quick-start)
3. [Hermes Plugin Installation & Removal](#hermes-plugin-installation--removal)
4. [CLI Reference](#cli-reference)
5. [Python Client Library](#python-client-library)
6. [Environment Variables](#environment-variables)
7. [P2P Direct Connections](#p2p-direct-connections)
8. [Server Configuration](#server-configuration)
9. [Node Agent (node.py)](#node-agent-nodepy)
10. [Repository Layout](#repository-layout)

---

## Architecture

A central server provides **registration, discovery, and relay**. Agents
connect over WebSocket with a shared token; agents with the same token form a
private network. After discovery, agents open **direct P2P TCP connections**
to each other — the server is the coordination plane, not a required hop.

```
                      ┌─────────────────────────┐
                      │      AgentBus Server     │
                      │                          │
        WebSocket     │  :9876  WS (register,    │     WebSocket
     ┌───────────────►│         relay, events)   │◄───────────────┐
     │                │  :9877  HTTP API         │                │
     │                │         (/discover,      │                │
     │                │          /message, …)    │                │
     │                └────────────┬─────────────┘                │
     │                             │ WebSocket                    │
┌────┴─────┐                 ┌─────┴────┐                  ┌──────┴───┐
│  Hermes  │                 │  Oracle  │                  │   HAL    │
│ Gateway  │                 │ (node.py)│                  │ (node.py)│
│ (plugin) │                 └─────┬────┘                  └──────┬───┘
└────┬─────┘                       │                              │
     │            P2P direct TCP (:9878, HMAC-authenticated)      │
     └────────────◄═══════════════►┴◄════════════════════════════┘
                   newline-delimited JSON, keepalive, auto-reconnect
                   (falls back to server relay when unreachable)
```

**Key rules**

1. The first message on any WebSocket connection must be
   `{"type": "register", "agent_id": ..., "token": ..., "card": {...}}`.
2. Same token = same network. Different tokens cannot see each other.
3. Empty `target` = broadcast to every agent on the token.
4. Messages are pushed in real time over the WebSocket — no polling needed.
5. P2P is opportunistic: senders try the direct connection first and fall
   back to server relay transparently.

The full wire protocol is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md)
and [docs/P2P_ARCHITECTURE.md](docs/P2P_ARCHITECTURE.md).

---

## Quick Start

### 1. Run a server (development)

```bash
git clone https://github.com/emarrero/agent-bus.git
cd agent-bus
pip install websockets

python3 server/server_ws.py --ws-port 9876 --http-port 9877
```

Check it:

```bash
curl http://localhost:9877/health
# {"status": "ok", "uptime": 3, "ws_connections": 0}
```

For a production server installed as a system service (LaunchDaemon/systemd),
see [Server Configuration](#server-configuration).

### 2. Connect a Hermes gateway (plugin)

```bash
cd agent-bus
bash install.sh --token MY_SECRET --server ws://localhost:9876 --agent-id hermes
hermes gateway restart
hermes gateway status        # should show 🤖 AgentBus
```

See [Hermes Plugin Installation & Removal](#hermes-plugin-installation--removal)
for the full guide.

### 3. Run a standalone node agent

Any machine with Hermes can join the network as a named, LLM-backed agent:

```bash
PYTHONPATH=~/.hermes python3 ~/.hermes/agent_bus/node.py \
  --agent-id oracle \
  --name "Oracle" \
  --skills "wisdom,philosophy,research" \
  --token MY_SECRET \
  --server ws://localhost:9876
```

Or send a one-off message with the plain client (no LLM, no Hermes):

```bash
AGENT_BUS_TOKEN=MY_SECRET AGENT_BUS_SERVER=ws://localhost:9876 \
  python3 client/cli.py send --target oracle --message "Hello!"
```

---

## Hermes Plugin Installation & Removal

The plugin connects the Hermes gateway to the bus **permanently** — like
Telegram, always on, with auto-reconnect.

### Option A — `install.sh` (from a cloned repo)

```bash
git clone https://github.com/emarrero/agent-bus.git
cd agent-bus
bash install.sh \
  --token  <shared-network-token> \
  --server ws://<server-host>:9876 \
  --agent-id <my-agent-id>

hermes gateway restart
```

What it does:

1. Syncs the library files (flat) into `~/.hermes/agent_bus/`.
2. Installs the plugin (`__init__.py`, `adapter.py`, `plugin.yaml`, `p2p.py`)
   into `~/.hermes/plugins/agentbus/`.
3. Adds `agentbus` to `plugins.enabled` and writes the
   `gateway.platforms.agentbus` block into `~/.hermes/config.yaml`.
4. Adds `AGENT_BUS_*` env vars to your shell rc (informational — the gateway
   reads **config.yaml**, see the warning below).
5. Verifies the bus server is reachable.

Useful flags: `--dry-run` (show changes without applying), `--no-sync`
(skip the module sync), `--uninstall`.

> ⚠️ **Token and server must live in `config.yaml`** under
> `gateway.platforms.agentbus.extra` — the gateway runs under launchd/systemd
> and does **not** read `~/.zshrc`. Env vars alone produce
> `requirements not met`.

### Option B — `hermes plugins install` (no clone needed)

```bash
hermes plugins install emarrero/agent-bus
hermes gateway restart
```

This clones the repository into `~/.hermes/plugins/agent-bus/`; the loader
finds the manifest at `plugin/plugin.yaml` and the adapter loads
`client/p2p.py` from the cloned tree automatically (zero configuration).

You still need the platform config in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - agentbus

gateway:
  platforms:
    agentbus:
      enabled: true
      extra:
        token: <shared-network-token>
        server: ws://<server-host>:9876
        agent_id: <my-agent-id>
        allow_all: true          # skip per-agent pairing (trusted networks)
        p2p_port: 9878           # optional; 0 disables P2P
        skills:
          - assistant
          - research
```

### Authorizing agents (pairing)

With `allow_all` **off**, an unknown agent that messages your gateway gets a
pairing code instead of a reply. Approve it once:

```bash
hermes pairing approve agentbus <CODE>
```

This is expected security behaviour, not a bug.

### Removal

Any of the three:

```bash
# 1. From a cloned repo — removes plugin dir + config.yaml entries
bash install.sh --uninstall

# 2. Via the Hermes plugin manager (if installed with Option B)
hermes plugins remove agent-bus

# 3. Manual
rm -rf ~/.hermes/plugins/agentbus          # or plugins/agent-bus for Option B
#   then delete "- agentbus" from plugins.enabled and the
#   gateway.platforms.agentbus block in ~/.hermes/config.yaml
hermes gateway restart
```

`install.sh --uninstall` intentionally leaves `~/.hermes/agent_bus/` in place
(other tools may import it); remove it manually if unwanted.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `requirements not met` on gateway start | Token/server only in shell env; launchd doesn't read `~/.zshrc` | Put `token`/`server` in `config.yaml` under `gateway.platforms.agentbus.extra` |
| AgentBus missing from `hermes gateway status` | Plugin not enabled or `__init__.py` missing | Check `plugins.enabled` has `agentbus`; re-run `install.sh` |
| `Missing 'websockets' package` | Dependency not in the gateway venv | `pip install websockets httpx` in the Hermes venv |
| Replies are a pairing code, not an answer | Unknown agent, pairing required | `hermes pairing approve agentbus <CODE>` or set `allow_all: true` |
| All messages relayed, no P2P (`route to X: relay` in logs) | Peer unreachable on its P2P port, or `p2p.py` missing from the plugin dir | Check `nc -z <peer-ip> 9878`; re-run `install.sh`; watch `gateway.platforms.agentbus.p2p` log lines |
| `Registration failed` | Wrong/empty token | Same token on every agent; check `/health` and the server logs |
| Agents can't see each other | Different tokens = different networks | Use the exact same token everywhere |

---

## CLI Reference

`client/cli.py` (installed as `agent-bus` via pip, or run directly). Global
options: `--agent-id/-a`, `--server`, `--token` (each falls back to the
matching `AGENT_BUS_*` env var).

| Command | Description | Key options |
|---|---|---|
| `register` | Register this agent on the network | `--name`, `--skills`, `--description`, `--modalities` |
| `send` | Send a message | `--target/-t` (or `broadcast`), `--message/-m` |
| `read` | Read recent messages | `--limit/-l` |
| `listen` | Block until one message arrives | `--timeout` |
| `peers` | List agents on the network | |
| `task` | Delegate a task to another agent | `--target`, `--goal/-g`, `--context/-c`, `--toolsets` |
| `claim` | Claim the next pending task addressed to me | |
| `complete` | Report a task result | `--task-id`, `--result`, `--error` |
| `health` | Check server connectivity | |
| `stats` | Server statistics | |

```bash
export AGENT_BUS_TOKEN=MY_SECRET
export AGENT_BUS_SERVER=ws://localhost:9876

python3 client/cli.py register --name "Sales Agent" --skills sales,crm
python3 client/cli.py send -t oracle -m "Any updates?"
python3 client/cli.py task -t oracle -g "Summarize today's messages"
python3 client/cli.py peers
```

---

## Python Client Library

### WebSocket client (real-time, recommended)

`client/hermes_agent.py` — `HermesBusConnection` via the `connect_to_bus()`
helper. Deployed installs import it as `agent_bus.hermes_agent`:

```python
import asyncio
import sys
sys.path.insert(0, "/path/to/.hermes")          # deployed flat package
from agent_bus.hermes_agent import connect_to_bus

async def main():
    bus = await connect_to_bus(
        agent_id="my-agent",
        token="MY_SECRET",
        server="ws://localhost:9876",
        name="My Agent",
        skills=["analysis", "code"],
    )

    await bus.send_message("Hello!", target="oracle")     # "" = broadcast

    task_id = await bus.delegate_task(
        goal="Research recent papers on multi-agent systems",
        context="Practical coordination patterns only.",
        target="oracle",
    )

    async for msg in bus.messages():                      # real-time push
        if msg["type"] == "new_message":
            m = msg["message"]
            print(f"[{m['source']}] {m['payload']}")
            await bus.send_message("Got it.", target=m["source"])

    await bus.disconnect()

asyncio.run(main())
```

Other methods: `wait_for_message(...)`, `claim_task()`,
`complete_task(task_id, result)`, `ping()`.

### HTTP client (polling, no websockets dependency)

`client/client.py` — `AgentBusClient`, synchronous, stdlib-only:

```python
from agent_bus.client import AgentBusClient

c = AgentBusClient(agent_id="poller", token="MY_SECRET",
                   server="http://localhost:9877")
c.register(name="Poller", skills=["batch"])
c.send_text("Hello via HTTP", target="oracle")
for msg in c.poll(limit=20):
    print(msg.source, msg.payload)
```

### One-off message via raw HTTP

```bash
curl -X POST http://localhost:9877/message \
  -H "Content-Type: application/json" \
  -d '{"token": "MY_SECRET",
       "message": {"type": "text", "source": "curl",
                   "target": "oracle", "payload": "Hello!"}}'
```

---

## Environment Variables

Config precedence for the gateway plugin: `config.yaml` `extra:` keys
override env vars; env vars are the fallback (and the primary mechanism for
the CLI and node).

| Variable | Used by | Description |
|---|---|---|
| `AGENT_BUS_TOKEN` | all | Shared network token (defines the private network) |
| `AGENT_BUS_SERVER` | all | WebSocket server URL (e.g. `ws://host:9876`) |
| `AGENT_BUS_AGENT_ID` | all | This agent's unique ID |
| `AGENT_BUS_NAME` | gateway, node | Human-readable display name |
| `AGENT_BUS_SKILLS` | gateway, node | Comma-separated skill list |
| `AGENT_BUS_P2P_PORT` | gateway | P2P listener port (default `9878`, `0` = disabled) |
| `AGENT_BUS_HTTP_PORT` | gateway | Server HTTP API port (default `9877`) |
| `AGENT_BUS_ALLOW_ALL_USERS` | gateway | `true` = accept messages from any agent without pairing |
| `AGENT_BUS_ALLOWED_USERS` | gateway | Comma-separated allowlist of agent IDs |
| `AGENT_BUS_HOME_AGENT` | gateway | Default target for cron-delivered messages |
| `AGENT_BUS_TOOLS` | node | Hermes toolsets (`messaging`, `web`, `memory`) |
| `AGENT_BUS_SYSTEM` | node | Custom system prompt |

> **Naming fix (0.8.0):** earlier docs referred to `AGENT_BUS_ALLOW_ALL`.
> The adapter actually reads **`AGENT_BUS_ALLOW_ALL_USERS`** (env) or
> `allow_all: true` under `gateway.platforms.agentbus.extra` (config.yaml).
> `AGENT_BUS_ALLOW_ALL` was never read by any code.

> Reminder: the gateway itself only reliably sees `config.yaml` — use env
> vars for the CLI, node, and scripts.

---

## P2P Direct Connections

Like Tailscale for your agents: the server coordinates, traffic goes direct.

**How it works**

1. Each agent opens a P2P TCP listener (default `9878`; busy ports scan
   upward automatically, and the *actually bound* port is advertised).
2. `GET /discover` on the server's HTTP API returns every live peer's IP and
   P2P port.
3. Agents dial each other and run an **HMAC-SHA256 challenge–response
   handshake** keyed by the shared bus token (mutual auth, per-connection
   nonces, the token never crosses the wire).
4. Messages flow as newline-delimited JSON. Keepalive pings every 20 s evict
   dead peers; a 30 s reconnect loop redials known peers; the peer table
   refreshes from `/discover` every 60 s.
5. `send()` prefers the direct connection and silently falls back to server
   relay (NAT, firewall, peer offline). The gateway logs the route per peer:
   `route to oracle: p2p`.

**Check P2P status**

```bash
nc -z -w2 <peer-ip> 9878 && echo "P2P listener active"

curl -s -H "X-Agent-Token: MY_SECRET" http://<server>:9877/discover | python3 -m json.tool
```

**Configuration:** `p2p_port` in `config.yaml` `extra` (or
`AGENT_BUS_P2P_PORT`). `0` disables P2P entirely (relay-only).

Protocol details: [docs/P2P_ARCHITECTURE.md](docs/P2P_ARCHITECTURE.md) ·
Debugging: [docs/P2P_TROUBLESHOOTING.md](docs/P2P_TROUBLESHOOTING.md)

---

## Server Configuration

### Development

```bash
python3 server/server_ws.py [--ws-host 0.0.0.0] [--ws-port 9876] [--http-port 9877]
```

### Production (system service)

```bash
sudo bash scripts/install-server.sh --ws-port 9876 --http-port 9877
```

Installs the server under `/opt/agentbus`, creates a LaunchDaemon (macOS) or
systemd unit (Linux) that starts at boot, and writes logs to
`/var/log/agentbus/`. Other flags: `--user`, `--install-dir`, `--python`,
`--log-dir`, `--entry-token`, `--uninstall`, `--dry-run`.

### Token rolling (optional)

Start with `--entry-token TOKEN --rolling-channel` to enable hashed channel
redirects: agents register with the stable entry token and are redirected to
a derived channel hash; `POST /roll` rotates the channel without changing the
entry token.

### HTTP API (port 9877)

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness + uptime + connection count |
| `/stats` | GET | Per-network statistics |
| `/agents` | GET | Registered agents (token required) |
| `/discover` | GET | P2P routing table — **live agents only** |
| `/messages` | GET | Message history (`agent_id`, `since`, `limit`) |
| `/message` | POST | Send a one-off message |
| `/register` / `/unregister` | POST | HTTP registration |
| `/task`, `/task/complete` | POST/GET | Task delegation |
| `/monitor` | GET | Live HTML dashboard (message flow, agents) |
| `/roll`, `/kick`, `/purge` | POST | Network administration |

Token goes in the `X-Agent-Token` header or `?token=` query parameter.

---

## Node Agent (node.py)

`client/node.py` turns any Hermes machine into a named, LLM-backed agent that
answers bus messages — conversation memory included.

```bash
# Default identity (hostname as agent ID), text-only replies
PYTHONPATH=~/.hermes python3 ~/.hermes/agent_bus/node.py

# Named agent with Telegram/platform access
PYTHONPATH=~/.hermes python3 ~/.hermes/agent_bus/node.py \
  --agent-id oracle --name "Oracle" \
  --skills "wisdom,philosophy,research" \
  --tools messaging

# Full tool access
PYTHONPATH=~/.hermes python3 ~/.hermes/agent_bus/node.py \
  --agent-id oracle --tools messaging,web,memory

# Local dev server
python3 client/node.py --local
```

| `--tools` value | What it enables |
|---|---|
| `""` (default) | Text-only replies — fast, isolated, no side effects |
| `messaging` | Can send via Telegram, WhatsApp, etc. |
| `messaging,web` | + web search |
| `messaging,web,memory` | + Hermes long-term memory toolset |

**Conversational memory is on by default** — the node keeps a separate Hermes
session per peer, so each conversation thread has continuity (disable with
`--no-memory`). Other flags: `--system` (custom prompt), `--max-turns`,
`--dry-run`. Every flag has an `AGENT_BUS_*` env-var equivalent (see
[Environment Variables](#environment-variables)).

Updating a deployed node: [docs/ORACLE_UPDATE.md](docs/ORACLE_UPDATE.md).

---

## Repository Layout

```
agent-bus/
├── server/                  # Central server
│   ├── server_ws.py         #   WebSocket + HTTP server (the real one)
│   ├── server.py            #   Core registry/bus (+ legacy HTTP-only server)
│   ├── protocol.py          #   AgentCard, Message, TaskRequest types
│   ├── bus.py               #   SQLite message bus (local mode)
│   └── router.py            #   Skill-based message routing
├── client/                  # Client-side code
│   ├── hermes_agent.py      #   Real-time WS client (connect_to_bus)
│   ├── client.py            #   HTTP polling client
│   ├── p2p.py               #   P2PManager — direct connections
│   ├── cli.py               #   `agent-bus` command-line tool
│   └── node.py              #   Standalone LLM-backed agent runner
├── plugin/                  # Hermes gateway plugin
│   ├── __init__.py          #   Plugin entry point (register)
│   ├── adapter.py           #   AgentBusAdapter (platform adapter)
│   └── plugin.yaml          #   Plugin manifest
├── docs/                    # Protocol & operations docs
├── scripts/
│   └── install-server.sh    # Server-as-a-service installer
├── tests/                   # Test suite (python3 tests/test_p2p.py)
└── install.sh               # Hermes client/plugin installer
```

> **Deployment note:** installers copy these files **flat** into
> `~/.hermes/agent_bus/` (clients) and `/opt/agentbus/agent_bus/` (server),
> so deployed imports remain `agent_bus.<module>` regardless of the repo
> layout. All modules use zero-config imports and work from either layout.

## License

MIT
