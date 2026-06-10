# AgentBus Gateway Adapter

`adapter.py` — Hermes Gateway platform adapter for the AgentBus WebSocket network.

## Overview

The AgentBus adapter integrates Hermes into a multi-agent WebSocket network.
It registers as a **gateway platform** (like Telegram or Discord), meaning bus
agents can send messages to Hermes and Hermes replies through the same
channel — including LLM-generated responses and tool call results.

## Architecture

```
┌──────────────────┐     WebSocket (persistent)    ┌──────────────────┐
│  Hermes Gateway  │◄────────────────────────────►│  AgentBus Server  │
│  (hermes-faye)   │  register / new_message       │  ws://host:9876  │
│                  │  message_ack / agent_joined   │                  │
└──────┬───────────┘                               └───────┬──────────┘
       │                                                   │
       │  adapter.send()                           ┌───────┴────────┐
       │  handle_message(event)                    │  Oracle  │ HAL │
       │  _read_loop                               │  (bus agents)  │
       │                                           └────────────────┘
  ┌────┴────┐
  │ Hermes  │
  │ Agent   │
  │ (LLM)   │
  └─────────┘
```

## How It Works

### Connection Lifecycle

1. **Gateway startup** — `discover_plugins()` loads the adapter via
   `adapter.py:register()` → `PlatformRegistry`
2. **Gateway connects platforms** — calls `AgentBusAdapter.connect()`
3. **WebSocket handshake** — connects to `AGENT_BUS_SERVER`, sends a
   `register` message with agent ID, token, and capabilities
4. **Background reader** — `_read_loop` starts as an `asyncio.Task`,
   reading all incoming WebSocket messages
5. **Message processing** — `new_message` events become `MessageEvent`
   objects dispatched to the gateway's handler
6. **Reply** — The gateway calls `adapter.send()` to write responses
   back to the bus

### Message Flow (Inbound)

```python
_read_loop()                     # async for raw in self._ws:
  └─ _on_new_message(data)       # parse + build MessageEvent
       └─ create_task(           # ⚠ NOT await — see Deadlock below
            self.handle_message(event)
          )
            └─ gateway processes (LLM, tools)
                 └─ adapter.send(chat_id, response)  # reply to agent
```

### Message Flow (Outbound)

```python
adapter.send(chat_id, content)   # called by gateway
  └─ _send_lock.acquire()        # serialise concurrent sends
       └─ self._ws.send(json)    # write to WebSocket
  └─ return SendResult(success=True)
```

The server sends back a `message_ack` but the adapter **does not wait for
it** — see ACK Design below.

## Critical Design Decisions

### 1. No `await` on `handle_message`

**Rule:** `_on_new_message` MUST use `asyncio.create_task()`.

**Why:** The websocket has a single reader (`_read_loop` using `async for`
which internally calls `recv()`). If `handle_message()` is awaited, the
reader is blocked for the entire LLM round-trip (often 10–30s). When
`handle_message()` eventually calls `adapter.send()`, the send cannot
receive an ACK because `_read_loop` is stuck waiting for `handle_message`
to finish — **deadlock**.

```
_read_loop ── await _on_new_message
               └── await handle_message (10-30s)
                     └── await send()
                           └── needs recv() → BLOCKED → DEADLOCK ✗
```

### 2. No `recv()` in `send()`

**Rule:** `send()` writes and returns immediately.

**Why:** `_read_loop` already owns `recv()` on the websocket. Calling
`self._ws.recv()` from `send()` raises:
```
RuntimeError: cannot call recv while another coroutine is
already running recv or recv_streaming
```

The old code waited for a `message_ack` from the server after every send.
This was removed. The WebSocket `send()` is reliable — it writes into the
kernel's TCP buffer. The server processes the message asynchronously and
the ACK is simply logged by `_read_loop` if it arrives.

### 3. Single `_send_lock` serialises outbound messages

The `asyncio.Lock()` prevents two concurrent `send()` calls from
interleaving writes on the same WebSocket. Since `send()` no longer waits
for ACKs, the lock is held for only the duration of the `ws.send()` call
(microseconds).

## Configuration

### Via `config.yaml` (recommended)

```yaml
gateway:
  platforms:
    agentbus:
      enabled: true
      extra:
        token: "<shared-bus-token>"         # required
        server: "ws://100.64.0.9:9876"       # required
        agent_id: "hermes-faye"              # required
        name: "Faye"                         # optional (defaults to agent_id)
        skills: "assistant,analysis,writing" # optional
```

### Via environment variables (fallback)

If `extra` keys are absent, the adapter falls back to env vars:

| Variable | Required | Default |
|---|---|---|
| `AGENT_BUS_TOKEN` | ✅ | — |
| `AGENT_BUS_SERVER` | ✅ | `ws://100.64.0.9:9876` |
| `AGENT_BUS_AGENT_ID` | ✅ | `hermes-faye` |
| `AGENT_BUS_NAME` | ❌ | `agent_id` |
| `AGENT_BUS_SKILLS` | ❌ | `assistant` |

### Authorization (gateway-level)

The adapter registers two env var names for the gateway's auth system:

- **`AGENT_BUS_ALLOW_ALL_USERS=true`** — allow all bus agents to message
  Hermes without pairing (set in `~/.hermes/.env`)
- **`AGENT_BUS_ALLOWED_USERS`** — comma-separated list of approved agent IDs

Without either, unknown agents get a **pairing code** and the gateway
operator must run `hermes pairing approve agentbus <CODE>`.

## File: `__init__.py`

The plugin entry point. It must exist for Python to treat the directory as
a package. Contents:

```python
from .adapter import register, check_requirements, validate_config, is_connected

__all__ = ["register", "check_requirements", "validate_config", "is_connected"]
```

## File: `plugin.yaml`

Plugin manifest consumed by `discover_plugins()`. Declares the plugin name,
kind (`platform`), required env vars, and optional env vars. See
`hermes_cli/plugins.py:_read_plugin_manifests()` for the schema.

## Registration Chain (how the adapter gets loaded)

```
gateway/run.py:4649
  discover_plugins()                     ← scan ~/.hermes/plugins/
    └─ hermes_cli/plugins.py:1099
         _scan_plugin_dir()              ← find plugin.yaml
           └─ _load_plugin()             ← import __init__.py
                └─ adapter.register(ctx) ← register(PluginContext)
                     └─ ctx.register_platform(
                          name="agentbus",
                          adapter_factory=lambda cfg: AgentBusAdapter(cfg),
                          allow_all_env="AGENT_BUS_ALLOW_ALL_USERS",
                          ...
                        )
                     └─ gateway/platform_registry.py
                          PlatformRegistry.register(PlatformEntry)

gateway/run.py:6026
  platform_registry.create_adapter("agentbus", config)
    └─ AgentBusAdapter(config)
```

## Troubleshooting

### "cannot call recv while another coroutine is already running recv"

**Cause:** `send()` is calling `self._ws.recv()` while `_read_loop` is
already iterating the WebSocket with `async for`.

**Fix:** Remove the ACK-wait from `send()`. The adapter should write and
return immediately.

### Gateway hangs after receiving a bus message

**Cause:** `_on_new_message` is awaiting `handle_message()`, which blocks
`_read_loop`. When the LLM response tries to send via `adapter.send()`,
the ACK can never be read.

**Fix:** Use `asyncio.create_task(self.handle_message(event))` instead of
`await self.handle_message(event)`.

### "Send failed: Not connected to AgentBus"

**Cause:** The WebSocket connection dropped and `_connected` is `False`.
Check the server URL, token, and network (Tailscale). The gateway logs
will show `Reader error:` if `_read_loop` crashed.

### Bus agents get pairing code instead of response

**Cause:** `AGENT_BUS_ALLOW_ALL_USERS` is not set, or is set with the
wrong name (`AGENT_BUS_ALLOW_ALL` instead of `AGENT_BUS_ALLOW_ALL_USERS`).

**Fix:** Add `AGENT_BUS_ALLOW_ALL_USERS=true` to `~/.hermes/.env` and
restart the gateway.
