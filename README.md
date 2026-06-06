# AgentBus — Red de comunicación multi-agente

**AgentBus** es una red de comunicación en tiempo real para agentes de IA. Permite que múltiples agentes (Hermes, Claude Code, Codex, etc.) se descubran, envíen mensajes, deleguen tareas y compartan información de forma asíncrona, usando un token compartido como clave de red privada.

Inspirado en el protocolo **A2A (Agent-to-Agent)** de Google.

---

## Arquitectura

```
┌─────────────────────────────────────────────────┐
│               AgentBus Server                    │
│  ┌─────────────┐  ┌─────────────┐              │
│  │ Red "abc"   │  │ Red "xyz"   │  ← tokens    │
│  │ (privada)   │  │ (privada)   │    separados  │
│  └──────┬──────┘  └──────┬──────┘              │
└─────────┼─────────────────┼─────────────────────┘
          │                 │
     ┌────┴────┐       ┌───┴───┐
     │ Agente A │       │Agente C│  ← WS connect
     │ token=abc│       │token=xyz
     └─────────┘       └───────┘
     ┌─────────┐
     │ Agente B │
     │ token=abc│
     └─────────┘
```

Cada **token** define una red privada. Agentes con el mismo token se ven y pueden comunicarse. Agentes con tokens diferentes están completamente aislados.

### Componentes

| Componente | Descripción |
|---|---|
| **Server HTTP** (`server.py`) | Servidor central HTTP, ideal para polling y entornos simples |
| **Server WebSocket** (`server_ws.py`) | Servidor con WS + HTTP, mensajes en tiempo real, monitor live |
| **CLI** (`cli.py`) | Herramienta de línea de comandos para usar la red |
| **Node** (`node.py`) | Ejecuta Hermes como agente permanente en la red |
| **Librería Python** (`client.py`, `bus.py`, etc.) | API Python para integrar agentes programáticamente |

---

## Requisitos

- **Python 3.10+**
- `pip` o `uv` para instalar dependencias
- Opcional: `websockets` (para modo WS y Node)

---

## Instalación

### 1. Clonar el repositorio

```bash
git clone https://github.com/emarrero/agent-bus.git
cd agent-bus
```

### 2. Instalar dependencias

El paquete se llama `agent_bus`. Puedes instalarlo de varias formas:

#### Opción A: Instalación editable (recomendada para desarrollo)

```bash
pip install -e .
```

#### Opción B: Con uv (más rápido)

```bash
uv pip install -e .
```

#### Opción C: Instalación mínima (solo dependencias del sistema)

```bash
# El servidor HTTP no requiere dependencias externas
# Solo usa la biblioteca estándar de Python

# Para el cliente WebSocket necesitas:
pip install websockets

# O con uv:
uv pip install websockets
```

### 3. Verificar la instalación

```bash
python3 -c "import agent_bus; print(agent_bus.__version__)"
```

Debería mostrar `0.1.0`.

---

## Inicio rápido

### 1. Levantar el servidor

**Opción HTTP** (más simple, sin dependencias extra):

```bash
python3 server.py --port 9876
```

**Opción WebSocket** (recomendada — mensajes en tiempo real + monitor web):

```bash
python3 server_ws.py --ws-port 9876 --http-port 9877
```

El servidor WebSocket levanta dos puertos:
- `:9876` — conexiones WebSocket de agentes
- `:9877` — API HTTP + monitor web en `/monitor`

### 2. Conectar agentes con el CLI

Desde otra terminal:

```bash
# Configurar el token (red privada)
export AGENT_BUS_TOKEN="mi_red_secreta"

# Registrar un agente
agent-bus register --name "Investigador" --skills research,analysis

# Enviar un mensaje
agent-bus send --target escritor --message "Hola, ¿puedes buscar info sobre transformers?"

# Leer mensajes
agent-bus read

# Listar agentes conectados
agent-bus peers

# Ver estadísticas del servidor
agent-bus stats
```

### 3. Conectar agentes vía WebSocket (Node)

El modo Node ejecuta Hermes como un agente permanente en la red:

```bash
python3 node.py --token mi_red_secreta \
  --agent-id investigador \
  --name "Investigador" \
  --skills research,analysis,code
```

Cada mensaje que recibe el agente se procesa con Hermes y se responde automáticamente.

---

## Instalación del Servidor (Server)

### Para producción

```bash
git clone https://github.com/emarrero/agent-bus.git /opt/agent-bus
cd /opt/agent-bus

# Crear virtualenv
python3 -m venv venv
source venv/bin/activate
pip install websockets  # para modo WS
```

#### Iniciar con systemd (opcional)

Crea `/etc/systemd/system/agentbus.service`:

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

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agentbus.service
# Ver logs:
sudo journalctl -u agentbus.service -f
```

---

## Instalación del Cliente (Client)

Cada máquina que ejecute agentes necesita el cliente.

### En máquinas remotas

```bash
git clone https://github.com/emarrero/agent-bus.git
cd agent-bus
pip install -e .
pip install websockets  # para modo WS
```

### Variables de entorno del cliente

| Variable | Descripción | Defecto |
|---|---|---|
| `AGENT_BUS_TOKEN` | Token compartido (define la red) | *(requerido)* |
| `AGENT_BUS_SERVER` | URL del servidor WS | `ws://localhost:9876` |
| `AGENT_BUS_AGENT_ID` | ID único del agente | hostname |
| `AGENT_BUS_NAME` | Nombre visible | agent-id |
| `AGENT_BUS_SKILLS` | Habilidades del agente | `assistant,analysis` |
| `AGENT_BUS_TOOLS` | Toolsets de Hermes | *(vacío = solo texto)* |

---

## Uso del CLI

```
agent-bus register    Registrar agente en la red
agent-bus send        Enviar mensaje a otro agente
agent-bus read        Leer mensajes entrantes
agent-bus task        Delegar una tarea
agent-bus claim       Tomar la siguiente tarea pendiente
agent-bus complete    Completar una tarea
agent-bus peers       Listar agentes conectados
agent-bus listen      Escuchar mensajes en tiempo real (WS)
agent-bus health      Verificar que el servidor responde
agent-bus stats       Estadísticas del servidor
```

### Ejemplos

```bash
# Registrar con habilidades específicas
agent-bus register --name "Traductor" --skills translation,writing

# Delegar tarea a un agente específico
agent-bus task --target escritor --goal "Escribe un resumen del proyecto"

# Escuchar mensajes entrantes (modo loop)
while true; do
  mensaje=$(agent-bus listen --timeout 30)
  echo "Recibido: $mensaje"
  # procesar...
done
```

---

## Uso como Librería Python

```python
from agent_bus.client import AgentBusClient

# Conectar a la red
agent = AgentBusClient(
    agent_id="mi_agente",
    token="mi_red_secreta",
    server_url="http://localhost:9876",
)
agent.register(skills=["research", "writing"])

# Enviar mensaje
agent.send_text("Hola desde Python!", target="otro_agente")

# Leer mensajes
mensajes = agent.poll(limit=10)
for msg in mensajes:
    print(f"{msg.source}: {msg.payload}")

# Delegar tarea
task_id = agent.send_task(
    goal="Investiga qué son los transformers",
    target="investigador",
)

# Verificar estado
status = agent.get_task_status(task_id)

# Cerrar conexión
agent.shutdown()
```

### Modo WebSocket

```python
import asyncio
from agent_bus.hermes_agent import connect_to_bus

async def main():
    bus = await connect_to_bus(
        agent_id="mi_agente",
        token="mi_red_secreta",
        server="ws://localhost:9876",
        skills=["research"],
    )

    # Escuchar mensajes en tiempo real
    async for event in bus.messages():
        if event["type"] == "new_message":
            msg = event["message"]
            print(f"{msg['source']}: {msg['payload']}")
            # Responder automáticamente
            await bus.send_message(
                "Mensaje recibido!", target=msg["source"]
            )

asyncio.run(main())
```

---

## Monitor Web

Cuando usas el servidor WebSocket (`server_ws.py`), el puerto HTTP (`:9877`) incluye:

- **`/monitor`** — Dashboard web en tiempo real con el flujo de mensajes
- **`/flow`** — Historial de eventos (formato JSON)
- **`/flow/stream`** — SSE (Server-Sent Events) para consumir en vivo
- **`/health`** — Health check
- **`/stats`** — Estadísticas del servidor

---

## Redes Privadas (Tokens)

El token es el mecanismo de aislamiento. Agentes con el mismo token pertenecen a la misma red privada:

```bash
# Red "ventas" — solo se ven entre sí
AGENT_BUS_TOKEN="ventas" agent-bus register --name "Agente Ventas"

# Red "soporte" — red separada, no ve a ventas
AGENT_BUS_TOKEN="soporte" agent-bus register --name "Agente Soporte"
```

---

## Canal Hash (Convergencia de Red)

Cuando el servidor se inicia con `--entry-token`, define un **canal canónico** al que todos los agentes deben converger. Los agentes que se conectan con un token diferente reciben un mensaje `channel_redirect` con el token correcto.

**Mecanismo:**

1. El servidor se inicia con `--entry-token CANONICAL_TOKEN`
2. Ese token es el canal canónico de la red
3. Cuando un agente se conecta con un token **diferente**, el servidor:
   - Envía un mensaje `channel_redirect` con el token correcto
   - **No cierra la conexión** — los clientes antiguos siguen funcionando
4. Los clientes que entienden `channel_redirect` reconectan automáticamente con el token correcto
5. Todos los agentes convergen en el mismo canal

```bash
# Servidor con canal canónico
python3 server_ws.py --entry-token "mi_token_secreto" --ws-port 9876 --http-port 9877

# Los clientes pueden conectarse con cualquier token;
# el servidor los redirige al canal correcto
export AGENT_BUS_TOKEN="otro_token"
python3 node.py --name "Agente Viajero"  # ← recibirá redirect
```

### Cómo maneja el cliente el redirect

El cliente `HermesBusConnection.connect()` en `hermes_agent.py` detecta el mensaje `channel_redirect` y:
1. Cierra la conexión actual
2. Actualiza su token al nuevo canal
3. Reconecta automáticamente (hasta 3 intentos)
4. El nodo `node.py` persiste el token actualizado para futuras reconexiones

Esto asegura que **todos los agentes terminen en el mismo canal**, incluso si se configuraron con tokens diferentes.

---

## API HTTP (servidor WebSocket)

Endpoint | Método | Descripción
---|---|---|---
`/register` | POST | Registrar un agente
`/unregister` | POST | Dar de baja un agente
`/message` | POST | Enviar mensaje
`/messages` | GET | Leer mensajes (`?agent_id=ID&limit=N`)
`/task` | POST | Delegar tarea
`/task` | GET | Reclamar tarea (`?agent_id=ID`) o consultar (`?task_id=ID`)
`/task/complete` | POST | Completar tarea
`/agents` | GET | Listar agentes en la red
`/kick` | POST | Desconectar un agente forzadamente
`/health` | GET | Health check
`/stats` | GET | Estadísticas

Todas las rutas requieren el header `X-Agent-Token: <token>` o parámetro `?token=`.

### Opciones del servidor WebSocket

| Parámetro | Descripción |
|---|---|
| `--ws-host` | Host para WebSocket (default: `0.0.0.0`) |
| `--ws-port` | Puerto WebSocket (default: `9876`) |
| `--http-port` | Puerto HTTP / monitor (default: `9877`) |
| `--entry-token` | Token canónico para canal hash. Agentes que se conecten con otro token reciben `channel_redirect` |

---

## Protocolo WebSocket

### Conexión

```json
{"type": "register", "agent_id": "mi_agente", "token": "mi_token", "card": {"name": "Mi Agente", "skills": ["research"]}}
```

### Mensajes entrantes (del servidor)

| Tipo | Descripción |
|---|---|---|
| `new_message` | Nuevo mensaje de otro agente |
| `agent_joined` | Un agente se conectó |
| `agent_left` | Un agente se desconectó |
| `agents_list` | Lista actual de agentes |
| `task_completed` | Una tarea delegada fue completada |
| `task_ack` | Confirmación de tarea recibida |
| `message_ack` | Confirmación de mensaje recibido |
| `channel_redirect` | Redirección al canal canónico (contiene `token` correcto) |
| `pong` | Respuesta a ping |

### Mensajes salientes (del agente)

| Tipo | Descripción |
|---|---|
| `message` | Enviar mensaje a otro agente |
| `task` | Delegar tarea |
| `task_complete` | Marcar tarea como completada |
| `claim_task` | Reclamar la siguiente tarea pendiente |
| `ping` | Verificar conexión |

---

## Estructura del proyecto

```
agent-bus/
├── __init__.py         # Versión y docstring del paquete
├── __main__.py         # Entry point del paquete
├── server.py           # Servidor HTTP (std lib, sin dependencias)
├── server_ws.py        # Servidor WebSocket + HTTP + monitor
├── client.py           # Cliente Python unificado (HTTP + local)
├── hermes_agent.py     # Conexión WebSocket para Hermes
├── node.py             # Ejecuta Hermes como agente permanente
├── bus.py              # MessageBus con SQLite (modo local)
├── protocol.py         # Protocolo: AgentCard, Message, Task
├── router.py           # Enrutamiento inteligente de mensajes
├── cli.py              # CLI: register, send, read, task, etc.
├── multimodal.py       # Capa multimodal (STT/TTS)
├── .gitignore
└── README.md           # Este archivo
```

---

## Solución de problemas

### "ModuleNotFoundError: No module named 'agent_bus'"

Asegúrate de instalar el paquete:

```bash
pip install -e /ruta/a/agent-bus
```

O agrega la ruta al PYTHONPATH:

```bash
export PYTHONPATH="$HOME/agent-bus:$PYTHONPATH"
```

### "websockets required"

```bash
pip install websockets
```

### "Connection refused"

Verifica que el servidor esté corriendo:

```bash
agent-bus health
# O con curl:
curl http://localhost:9877/health
```

### Los agentes no se ven entre sí

- Verifica que usan el **mismo token**
- Verifica que apuntan al **mismo servidor**
- Usa `agent-bus peers` para listar agentes conectados

---

## Variables de entorno

| Variable | Usada por | Descripción |
|---|---|---|
| `AGENT_BUS_TOKEN` | Todos | Token compartido de red |
| `AGENT_BUS_SERVER` | Clientes | URL del servidor WS |
| `AGENT_BUS_AGENT_ID` | Clientes | ID del agente |
| `AGENT_BUS_NAME` | Node | Nombre visible |
| `AGENT_BUS_SKILLS` | Node | Habilidades del agente |
| `AGENT_BUS_TOOLS` | Node | Toolsets de Hermes |
| `AGENT_BUS_SYSTEM` | Node | System prompt personalizado |
| `AGENT_BUS_ALLOW_ALL` | Server | Permitir cualquier token |

---

## Licencia

MIT
