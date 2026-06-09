"""Escucha mensajes del AgentBus y los guarda en un inbox.
El agente Hermes lee este archivo para saber si hay novedades.
"""
import asyncio, json, os, sys
from datetime import datetime

sys.path.insert(0, "/Users/emarrero/.hermes")
from agent_bus.hermes_agent import connect_to_bus

TOKEN = "68fd11d8d1740996c6da70c70cc4d2a3"
SERVER = "ws://100.64.0.9:9876"
AGENT_ID = "hal"
INBOX = os.path.expanduser("~/.hermes/agent_bus/inbox.jsonl")

async def listen():
    bus = await connect_to_bus(
        AGENT_ID, token=TOKEN, server=SERVER,
        name="HAL", skills=["all", "assistance", "communication"],
    )
    # Marcar inicio en inbox
    _log({"type": "_connected", "msg": "HAL conectado al bus"})

    try:
        async for msg in bus.messages():
            t = msg.get("type", "?")
            entry = {"ts": datetime.utcnow().isoformat(), "type": t}

            if t == "new_message":
                m = msg.get("message", {})
                entry["source"] = m.get("source")
                entry["target"] = m.get("target")
                entry["payload"] = m.get("payload")
                entry["msg_type"] = m.get("type")
                print(f"\n📩 [{m.get('source')}]: {m.get('payload')}", flush=True)

            elif t == "task_completed":
                task = msg.get("task", {})
                entry["source"] = task.get("target_agent")
                entry["payload"] = f"Tarea {task.get('task_id','')[:12]} completada: {str(task.get('result',''))[:200]}"
                print(f"\n✅ [{entry['source']}]: {entry['payload']}", flush=True)

            elif t in ("agent_joined", "agent_left"):
                entry["source"] = msg.get("agent_id")
                entry["payload"] = f"Agente {t}: {msg.get('agent_id')}"
                print(f"\n{'👋' if t=='agent_joined' else '🚪'} {entry['payload']}", flush=True)

            elif t == "agents_list":
                agents = msg.get("agents", [])
                entry["payload"] = f"Red actual: {len(agents)} agente(s)"
                for a in agents:
                    entry["payload"] += f"\n  • {a.get('name','?')}"

            else:
                entry["payload"] = str(msg)[:200]

            _log(entry)

    except asyncio.CancelledError:
        pass
    finally:
        await bus.disconnect()
        _log({"type": "_disconnected", "msg": "HAL desconectado"})


def _log(entry: dict):
    """Escribe al inbox y también a stdout."""
    line = json.dumps(entry, ensure_ascii=False)
    with open(INBOX, "a") as f:
        f.write(line + "\n")
        f.flush()
    print(f"[inbox] {line[:120]}", flush=True)


if __name__ == "__main__":
    asyncio.run(listen())
