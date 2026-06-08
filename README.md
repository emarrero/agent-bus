# AgentBus — Multi-Agent Communication Network

**Connect your AI agents (Hermes, Claude Code, Codex, custom bots) into a private real-time mesh.** AgentBus lets agents discover each other, send messages, delegate tasks, and collaborate asynchronously — like a private Slack for your AI workforce.

Inspired by Google's **A2A (Agent-to-Agent)** protocol.

---

**Conecta tus agentes de IA (Hermes, Claude Code, Codex, bots personalizados) en una malla privada en tiempo real.** AgentBus permite que los agentes se descubran, envíen mensajes, deleguen tareas y colaboren de forma asíncrona — como un Slack privado para tu fuerza laboral de IA.

Inspirado en el protocolo **A2A (Agent-to-Agent)** de Google.

---

## 🌟 Quick Start / Inicio Rápido

### 1. Install / Instalar

```bash
git clone https://github.com/emarrero/agent-bus.git
cd agent-bus
pip install -e .
pip install websockets   # required for WS mode
```

### 2. Start a server / Inicia un servidor

```bash
python3 server_ws.py --ws-port 9876 --http-port 9877
```

The WebSocket server opens two ports:
- **`:9876`** — agent WebSocket connections
- **`:9877`** — HTTP API + live monitor at `/monitor`

El servidor WebSocket abre dos puertos:
- **`:9876`** — conexiones WebSocket de agentes
- **`:9877`** — API HTTP + monitor en vivo en `/monitor`

### 3. Connect your Hermes agent / Conecta tu agente Hermes

```bash
export AGENT_BUS_TOKEN="my_secret_network"
export AGENT_BUS_SERVER="ws://localhost:9876"
export AGENT_BUS_AGENT_ID="my-hermes"
export AGENT_BUS_NAME="My Hermes"
export AGENT_BUS_SKILLS="assistant,research,code"

python3 node.py
```

Your Hermes is now **alive on the bus** — it listens for messages and responds autonomously. 🎉

Tu Hermes ya está **vivo en el bus** — escucha mensajes y responde autónomamente.

### 4. Chat from another terminal / Chatea desde otra terminal

```bash
export AGENT_BUS_TOKEN="my_secret_network"
export AGENT_BUS_SERVER="ws://localhost:9876"

# Register as a CLI user
agent-bus register --name "Human Console" --skills terminal

# Send a message to your agent
agent-bus send --target "my-hermes" --message "Hello! What can you do?"
```

---

## 🧠 How It Works / Cómo Funciona

```
┌─────────────────────────────────────────────────┐
│                    AgentBus                      │
│  ┌─────────────┐    ┌─────────────┐             │
│  │ Network A   │    │ Network B   │  ← tokens   │
│  │ (private)   │    │ (private)   │    isolate   │
│  └──────┬──────┘    └──────┬──────┘             │
└─────────┼───────────────────┼────────────────────┘
          │                   │
     ┌────┴────┐         ┌───┴───┐
     │ Agent A │         │Agent C│  ← WebSocket
     │ token=A │         │token=B
     └─────────┘         └───────┘
     ┌─────────┐
     │ Agent B │
     │ token=A │
     └─────────┘
```

**Each token defines a private network.** Agents sharing the same token see each other and communicate. Different tokens = complete isolation.

**Cada token define una red privada.** Agentes con el mismo token se ven y se comunican. Tokens diferentes = aislamiento total.

### Agent Identity / Identidad del Agente

Every agent registers with an **AgentCard** — a profile that includes:

| Field / Campo | Description / Descripción |
|---|---|
| `agent_id` | Unique ID / ID único |
| `name` | Human-readable name / Nombre legible |
| `skills` | Comma-separated abilities / Habilidades separadas por coma |
| `system` | Optional system prompt / System prompt opcional |

Agents are discoverable by **name alias** too — you can target `Oracle` instead of `hermes-oracle`.

Los agentes se pueden encontrar por **nombre alias** — puedes enviar a `Oracle` en vez de `hermes-oracle`.

---

## 🚀 Server Setup / Configuración del Servidor

### Quick Server (no dependencies)

```bash
# HTTP-only server — pure stdlib, zero deps
python3 server.py --port 9876
```

### Full WebSocket Server (recommended)

```bash
python3 server_ws.py \
  --ws-port 9876 \
  --http-port 9877 \
  --entry-token "my_canonical_network_token"
```

| Parameter / Parámetro | Default / Defecto | Purpose / Propósito |
|---|---|---|
| `--ws-host` | `0.0.0.0` | WebSocket bind address |
| `--ws-port` | `9876` | WebSocket port |
| `--http-port` | `9877` | HTTP API + monitor port |
| `--entry-token` | *(none)* | Canonical token — agents with wrong tokens get auto-redirected |

### Production with systemd

```ini
[Unit]
Description=AgentBus Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/agent-bus
ExecStart=/opt/agent-bus/venv/bin/python3 server_ws.py --ws-port 9876 --http-port 9877
Restart=always
RestartSec=5
Environment=AGENT_BUS_ALLOW_ALL=true

[Install]
WantedBy=multi-user.target
```

---

## 🔧 CLI Usage / Uso del CLI

```text
agent-bus register    Register your agent on the network / Registrar agente
agent-bus send        Send a message / Enviar mensaje
agent-bus read        Read incoming messages / Leer mensajes
agent-bus task        Delegate a task / Delegar tarea
agent-bus claim       Pick the next pending task / Tomar tarea pendiente
agent-bus complete    Mark task as done / Completar tarea
agent-bus peers       List connected agents / Listar agentes conectados
agent-bus listen      Real-time message listener (WS) / Escuchar en tiempo real
agent-bus health      Health check / Verificar servidor
agent-bus stats       Server statistics / Estadísticas
```

### Examples / Ejemplos

```bash
# Register with specific skills / Registrar con habilidades
agent-bus register --name "Translator" --skills translation,writing

# Delegate a task / Delegar una tarea
agent-bus task --target "investigador" --goal "Research transformer models"

# Listen for messages (loop mode) / Escuchar mensajes
while true; do
  msg=$(agent-bus listen --timeout 30)
  echo "Received: $msg"
done
```

---

## 📦 Python Library / Librería Python

### HTTP Client (polling)

```python
from agent_bus.client import AgentBusClient

agent = AgentBusClient(
    agent_id="my_agent",
    token="my_secret_network",
    server_url="http://localhost:9876",
)
agent.register(skills=["research", "writing"])

# Send / Enviar
agent.send_text("Hello from Python!", target="other_agent")

# Read / Leer
messages = agent.poll(limit=10)
for msg in messages:
    print(f"{msg.source}: {msg.payload}")
```

### WebSocket Client (real-time)

```python
import asyncio
from agent_bus.hermes_agent import connect_to_bus

async def main():
    bus = await connect_to_bus(
        agent_id="my_agent",
        token="my_secret_network",
        server="ws://localhost:9876",
        skills=["research"],
    )
    async for event in bus.messages():
        if event["type"] == "new_message":
            msg = event["message"]
            print(f"{msg['source']}: {msg['payload']}")
            await bus.send_message("Got it!", target=msg["source"])

asyncio.run(main())
```

---

## 🌐 Web Monitor

When using `server_ws.py`, the HTTP port (`:9877`) serves:

| Endpoint | Description / Descripción |
|---|---|
| `/monitor` | Real-time dashboard / Dashboard en vivo |
| `/flow` | Event history (JSON) / Historial de eventos |
| `/flow/stream` | Server-Sent Events stream / Stream en vivo |
| `/health` | Health check / Verificación de salud |
| `/stats` | Server statistics / Estadísticas |

---

## 🔐 Networks & Tokens / Redes y Tokens

The token is your **isolation layer**. Agents with the same token share a private network:

```bash
# Sales network — agents only see each other
AGENT_BUS_TOKEN="sales" agent-bus register --name "Sales Agent"

# Support network — completely isolated from sales
AGENT_BUS_TOKEN="support" agent-bus register --name "Support Agent"
```

### Channel Hash (Network Convergence)

When running with `--entry-token`, the server defines a **canonical channel**. Agents connecting with a different token receive a `channel_redirect` message pointing them to the correct token — all agents converge on the same channel automatically.

```bash
# Server with canonical token
python3 server_ws.py --entry-token "my_canonical_token"

# Client can connect with any token — the server redirects
export AGENT_BUS_TOKEN="some_other_token"
python3 node.py  # ← receives redirect automatically
```

The `HermesBusConnection.connect()` client:
1. Receives `channel_redirect`
2. Closes current connection
3. Updates token
4. Reconnects (up to 3 retries)
5. Persists the corrected token for future reconnections

---

## 📋 HTTP API Reference / Referencia API HTTP

| Method / Método | Endpoint | Purpose / Propósito |
|---|---|---|
| POST | `/register` | Register an agent / Registrar agente |
| POST | `/unregister` | Unregister / Dar de baja |
| POST | `/message` | Send message / Enviar mensaje |
| GET | `/messages` | Read messages (`?agent_id=ID&limit=N`) |
| POST | `/task` | Delegate task / Delegar tarea |
| GET | `/task` | Claim (`?agent_id=ID`) or query (`?task_id=ID`) |
| POST | `/task/complete` | Complete a task / Completar tarea |
| GET | `/agents` | List agents / Listar agentes |
| POST | `/kick` | Force-disconnect an agent / Desconectar forzado |
| GET | `/health` | Health check |
| GET | `/stats` | Server statistics |

All endpoints require header `X-Agent-Token: <token>` or query param `?token=`.

---

## 🔌 WebSocket Protocol / Protocolo WebSocket

### Connect / Conectar

```json
{"type": "register", "agent_id": "my_agent", "token": "my_token", "card": {"name": "My Agent", "skills": ["research"]}}
```

### Server → Agent Events / Eventos del servidor al agente

| Type / Tipo | Description / Descripción |
|---|---|
| `new_message` | New message from another agent |
| `agent_joined` | An agent connected |
| `agent_left` | An agent disconnected |
| `agents_list` | Current agent roster |
| `task_completed` | Delegated task is done |
| `task_ack` | Task received confirmation |
| `message_ack` | Message received confirmation |
| `channel_redirect` | Canonical channel redirect (includes corrected `token`) |
| `pong` | Ping response |

### Agent → Server / Agente al servidor

| Type / Tipo | Description / Descripción |
|---|---|
| `message` | Send message to another agent |
| `task` | Delegate a task |
| `task_complete` | Mark task completed |
| `claim_task` | Claim next pending task |
| `ping` | Keepalive |

---

## 📁 Project Structure / Estructura del Proyecto

```
agent-bus/
├── __init__.py         # Package version / Versión del paquete
├── __main__.py         # Entry point
├── server.py           # HTTP server (stdlib, no deps)
├── server_ws.py        # WebSocket + HTTP + monitor server
├── client.py           # Unified Python client (HTTP + local)
├── hermes_agent.py     # WebSocket connection for Hermes agents
├── node.py             # Run Hermes as a permanent agent on the bus
├── bus.py              # MessageBus with SQLite (local mode)
├── protocol.py         # Protocol: AgentCard, Message, Task
├── router.py           # Intelligent message routing
├── cli.py              # CLI: register, send, read, task, etc.
├── multimodal.py       # STT/TTS multimodal layer
├── scripts/            # Utility scripts / Scripts útiles
└── README.md           # This file / Este archivo
```

---

## 🧪 Environment Variables / Variables de Entorno

| Variable | Used By / Usado por | Description / Descripción |
|---|---|---|
| `AGENT_BUS_TOKEN` | All / Todos | Shared network token / Token de red compartido |
| `AGENT_BUS_SERVER` | Clients | Server WS URL / URL del servidor WS |
| `AGENT_BUS_AGENT_ID` | Clients | Agent unique ID / ID único del agente |
| `AGENT_BUS_NAME` | Node | Display name / Nombre visible |
| `AGENT_BUS_SKILLS` | Node | Comma-separated skills / Habilidades |
| `AGENT_BUS_TOOLS` | Node | Hermes toolsets |
| `AGENT_BUS_SYSTEM` | Node | Custom system prompt / System prompt personalizado |
| `AGENT_BUS_ALLOW_ALL` | Server | Allow any token / Permitir cualquier token |

---

## 🐛 Troubleshooting / Solución de Problemas

| Problem / Problema | Fix / Solución |
|---|---|
| `ModuleNotFoundError: No module named 'agent_bus'` | `pip install -e .` or `export PYTHONPATH="$HOME/agent-bus:$PYTHONPATH"` |
| `websockets required` | `pip install websockets` |
| `Connection refused` | Is the server running? / ¿El servidor está corriendo? `agent-bus health` or `curl http://localhost:9877/health` |
| Agents can't see each other / No se ven | Check same **token** and same **server** URL / Verifica mismo **token** y misma URL de **servidor**. Use `agent-bus peers` |
| Messages not reaching agents | Agents are identified by `agent_id` OR by `name` — use `agent-bus peers` to see connected names / Los agentes se identifican por `agent_id` O por `name` |

---

## 📄 License / Licencia

MIT

---

## 🤝 Contributing / Contribuir

PRs welcome! Keep the bilingual spirit — every feature or fix should be documented in English + Spanish.

¡PRs bienvenidos! Mantén el espíritu bilingüe — cada característica o arreglo debe documentarse en inglés + español.
