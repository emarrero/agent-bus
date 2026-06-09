#!/usr/bin/env python3
"""Revisa el inbox de HAL y muestra mensajes nuevos no leídos."""
import json, os
from datetime import datetime

INBOX = os.path.expanduser("~/.hermes/agent_bus/inbox.jsonl")
MARCA = os.path.expanduser("~/.hermes/agent_bus/inbox_leido.txt")

def check():
    if not os.path.exists(INBOX):
        print("📭 No hay mensajes")
        return

    # Leer última marca de lectura
    last_read = 0
    if os.path.exists(MARCA):
        with open(MARCA) as f:
            last_read = int(f.read().strip() or "0")

    # Leer mensajes nuevos
    nuevos = []
    with open(INBOX) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if i > last_read:
                try:
                    entry = json.loads(line)
                    nuevos.append(entry)
                except json.JSONDecodeError:
                    pass

    if not nuevos:
        print("📭 Sin novedades")
        # Guardar marca anyway
        with open(MARCA, "w") as f:
            f.write(str(_total_lines(INBOX)))
        return

    # Mostrar novedades
    for e in nuevos:
        t = e.get("type", "?")
        s = e.get("source", "")
        p = e.get("payload", "")
        ts = e.get("ts", "")[11:19] if "ts" in e else ""
        icono = {"new_message": "📩", "task_completed": "✅",
                 "agent_joined": "👋", "agent_left": "🚪",
                 "_connected": "🔗", "_disconnected": "❌"}.get(t, "📨")
        print(f"{icono} [{ts}] {s}: {str(p)[:200]}")

    # Guardar marca de lectura
    with open(MARCA, "w") as f:
        f.write(str(_total_lines(INBOX)))

    print(f"\n📊 {len(nuevos)} mensaje(s) nuevo(s)")


def _total_lines(path):
    with open(path) as f:
        return sum(1 for _ in f)


if __name__ == "__main__":
    check()
