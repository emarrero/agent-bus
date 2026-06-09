"""Demo de AgentBus en modo RED con tokens.

Muestra cómo agentes en procesos separados se comunican
compartiendo un token.

Escenario:
  1. Inicia el servidor AgentBus (proceso background)
  2. Conecta 2 agentes con token "privado123" (red privada)
  3. Conecta 1 agente con token "otrared" (red separada)
  4. Agentes con mismo token se ven y comunican
  5. Agentes con diferente token NO se ven
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

# Añadir agent_bus al path
sys.path.insert(0, os.path.expanduser("~/.hermes"))

SERVER_URL = "http://localhost:9876"


def start_server() -> subprocess.Popen:
    """Inicia el servidor AgentBus en background."""
    server_script = os.path.expanduser("~/.hermes/agent_bus/server.py")
    proc = subprocess.Popen(
        [sys.executable, server_script, "--port", "9876"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Esperar a que el servidor esté listo
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{SERVER_URL}/health", timeout=2):
                return proc
        except (urllib.error.URLError, ConnectionRefusedError):
            time.sleep(0.3)
    # Si no responde, mostrar error
    _, stderr = proc.communicate(timeout=2)
    raise RuntimeError(f"No se pudo iniciar el servidor:\n{stderr.decode()[:500]}")


def stop_server(proc: subprocess.Popen):
    proc.terminate()
    proc.wait()


def register_agent(agent_id: str, name: str, skills: list[str], token: str) -> None:
    """Registra un agente en la red vía HTTP."""
    data = json.dumps({
        "agent_id": agent_id,
        "card": {
            "agent_id": agent_id,
            "name": name,
            "description": f"Agente {name}",
            "skills": skills,
            "modalities": ["text"],
        },
        "token": token,
    }).encode()
    req = urllib.request.Request(
        f"{SERVER_URL}/register",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    assert resp["status"] == "ok", f"Error registrando {agent_id}: {resp}"
    print(f"   ✅ {name} registrado en red '{token}'")


def send_message(source: str, text: str, token: str, target: str = "") -> str:
    """Envía un mensaje a la red."""
    data = json.dumps({
        "message": {
            "type": "text",
            "source": source,
            "target": target,
            "payload": text,
        },
        "token": token,
    }).encode()
    req = urllib.request.Request(
        f"{SERVER_URL}/message",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp.get("message_id", "")


def get_messages(agent_id: str, token: str) -> list[dict]:
    """Obtiene mensajes para un agente."""
    req = urllib.request.Request(
        f"{SERVER_URL}/messages?token={token}&agent_id={agent_id}&limit=20",
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp.get("messages", [])


def list_agents(token: str) -> list[dict]:
    """Lista agentes en la red del token."""
    req = urllib.request.Request(f"{SERVER_URL}/agents?token={token}")
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp.get("agents", [])


def test_token_isolation():
    """Prueba que agentes con diferentes tokens NO se vean."""
    print("\n" + "=" * 60)
    print("🧪 TOKEN ISOLATION — Agentes con tokens diferentes")
    print("=" * 60)

    # Red privada 1 (token: privado123)
    register_agent("alice", "Alice", ["writing", "analysis"], token="privado123")
    register_agent("bob", "Bob", ["research"], token="privado123")

    # Red privada 2 (token: otrared)
    register_agent("charlie", "Charlie", ["code"], token="otrared")

    time.sleep(0.2)

    # Alice ve a Bob (mismo token)
    alice_agents = list_agents(token="privado123")
    alice_names = [a["name"] for a in alice_agents]
    print(f"\n🔍 Agentes en red 'privado123': {alice_names}")
    assert "Alice" in alice_names, "Alice debería verse a sí misma"
    assert "Bob" in alice_names, "Bob debería estar visible para Alice"
    assert "Charlie" not in alice_names, "Charlie NO debería estar en esta red"
    print("   ✅ Alice ve a Bob, NO ve a Charlie")

    # Charlie NO ve a Alice/Bob
    charlie_agents = list_agents(token="otrared")
    charlie_names = [a["name"] for a in charlie_agents]
    print(f"🔍 Agentes en red 'otrared': {charlie_names}")
    assert "Alice" not in charlie_names, "Alice NO debería estar en otrared"
    assert "Charlie" in charlie_names, "Charlie debería verse a sí mismo"
    print("   ✅ Charlie NO ve a Alice/Bob")

    print("\n✅ Aislamiento por token funciona correctamente!")


def test_communication():
    """Prueba comunicación entre agentes con el mismo token."""
    print("\n" + "=" * 60)
    print("🧪 COMMUNICATION — Mensajes dentro de la misma red")
    print("=" * 60)

    # Alice envía mensaje a Bob
    send_message("alice", "Hola Bob, ¿puedes investigar sobre transformers?", token="privado123", target="bob")
    print("\n📤 Alice -> Bob: 'Hola Bob, ¿puedes investigar sobre transformers?'")
    time.sleep(0.1)

    # Bob recibe el mensaje
    bob_msgs = get_messages("bob", token="privado123")
    bob_texts = [m["payload"] for m in bob_msgs if m.get("type") == "text"]
    print(f"📥 Bob recibe: {bob_texts}")
    assert any("transformers" in str(t) for t in bob_texts), "Bob debería recibir el mensaje"
    print("   ✅ Bob recibió el mensaje de Alice")

    # Bob responde
    send_message("bob", "¡Claro! Investigo sobre transformers ahora.", token="privado123", target="alice")
    time.sleep(0.1)

    alice_msgs = get_messages("alice", token="privado123")
    alice_texts = [m["payload"] for m in alice_msgs if m.get("type") == "text" and m.get("source") == "bob"]
    print(f"📥 Alice recibe respuesta: {alice_texts}")
    assert any("Investigo" in str(t) for t in alice_texts), "Alice debería recibir la respuesta"
    print("   ✅ Alice recibió la respuesta de Bob")

    print("\n✅ Comunicación funciona correctamente!")


def test_task_delegation():
    """Prueba delegación de tareas entre agentes."""
    print("\n" + "=" * 60)
    print("🧪 TASK DELEGATION — Delegar tareas en la red")
    print("=" * 60)

    # Delegar tarea (Alice pide a Bob investigar)
    token = "privado123"
    task_data = json.dumps({
        "task": {
            "source_agent": "alice",
            "target_agent": "bob",
            "goal": "Investiga qué son los transformers en NLP",
            "context": "Para un artículo técnico sobre IA",
            "toolsets": ["research"],
            "status": "pending",
        },
        "token": token,
    }).encode()
    req = urllib.request.Request(
        f"{SERVER_URL}/task",
        data=task_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    task_id = resp["task_id"]
    print(f"\n📤 Alice delega tarea a Bob (task_id: {task_id[:8]}...)")

    # Bob reclama la tarea
    bob_task = json.loads(urllib.request.urlopen(
        f"{SERVER_URL}/task?token={token}&agent_id=bob"
    ).read())
    claimed = bob_task.get("task")
    assert claimed, "Bob debería poder reclamar la tarea"
    print(f"📥 Bob reclama la tarea: '{claimed['goal'][:50]}...'")

    # Bob completa la tarea
    complete_data = json.dumps({
        "task_id": task_id,
        "result": "Los transformers son una arquitectura de redes neuronales introducida en 2017...",
        "error": None,
        "token": token,
    }).encode()
    req = urllib.request.Request(
        f"{SERVER_URL}/task/complete",
        data=complete_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    json.loads(urllib.request.urlopen(req).read())
    print(f"📤 Bob completa la tarea con resultado")

    # Alice verifica
    alice_task = json.loads(urllib.request.urlopen(
        f"{SERVER_URL}/task?token={token}&task_id={task_id}"
    ).read())
    completed = alice_task.get("task")
    assert completed and completed["status"] == "completed", "La tarea debería estar completada"
    print(f"📊 Alice verifica: tarea '{completed['status']}'")
    print(f"   Resultado: {str(completed['result'])[:70]}...")

    print("\n✅ Delegación de tareas funciona correctamente!")


def test_global_stats():
    """Muestra estadísticas globales del servidor."""
    print("\n" + "=" * 60)
    print("📊 GLOBAL STATS")
    print("=" * 60)

    req = urllib.request.Request(f"{SERVER_URL}/stats")
    stats = json.loads(urllib.request.urlopen(req).read())
    print(f"   Redes activas: {stats.get('networks', '?')}")
    print(f"   Agentes totales: {stats.get('total_agents', '?')}")
    print(f"   Mensajes totales: {stats.get('total_messages', '?')}")
    print(f"   Uptime: {stats.get('server_uptime', '?')}s")

    # Stats por red
    for token in ["privado123", "otrared"]:
        st = json.loads(urllib.request.urlopen(
            f"{SERVER_URL}/stats?token={token}"
        ).read())
        s = st.get("stats", {})
        print(f"\n   Red '{token}': {s.get('agents')} agentes, "
              f"{s.get('messages')} mensajes, "
              f"{s.get('pending_tasks')} tareas pendientes")


def test_agentbus_client():
    """Prueba usando AgentBusClient directamente."""
    print("\n" + "=" * 60)
    print("🧪 AGENTBUS CLIENT — Usando la clase cliente")
    print("=" * 60)

    from agent_bus.client import AgentBusClient

    # Crear agente en modo red
    agent = AgentBusClient(
        "david", "David", "Agente de prueba",
        server_url=SERVER_URL, token="privado123",
    )
    agent.register(skills=["analysis"])

    time.sleep(0.1)

    # Ver que ve a los otros agentes
    peers = agent.find_agents()
    peer_names = [a.name for a in peers]
    print(f"\n🔍 David ve en red 'privado123': {peer_names}")
    assert "Alice" in peer_names, "David debería ver a Alice"
    assert "Bob" in peer_names, "David debería ver a Bob"

    # Enviar mensaje
    msg_id = agent.send_text("Hola a todos en la red!", target="")
    print(f"📤 David envía broadcast (msg_id: {msg_id[:8]}...)")
    time.sleep(0.1)

    # Ver mensajes
    msgs = agent.poll(limit=10)
    msgs_text = [m.payload if isinstance(m.payload, str) else str(m.payload) for m in msgs]
    print(f"📥 Mensajes para David: {len(msgs)}")
    agent.shutdown()
    print("   ✅ David se desconectó correctamente")

    print("\n✅ AgentBusClient funciona en modo red!")


def main():
    print("🧪 AgentBus Network Demo — Multi-agente con tokens")
    print()

    # 1. Iniciar servidor
    print("🚀 Iniciando AgentBus Server...")
    server_proc = start_server()
    print("   ✅ Servidor listo en", SERVER_URL)

    try:
        test_token_isolation()
        test_communication()
        test_task_delegation()
        test_agentbus_client()
        test_global_stats()

        print("\n" + "=" * 60)
        print("🎉 ¡TODAS LAS PRUEBAS PASARON!")
        print("=" * 60)
        print()
        print("Resumen:")
        print("  • Token 'privado123': Alice, Bob, David")
        print("  • Token 'otrared': Charlie")
        print("  • Aislamiento total entre redes con diferentes tokens")
        print("  • Comunicación y delegación funcionan dentro de cada red")

    finally:
        stop_server(server_proc)
        print("\n🛑 Servidor detenido.")


if __name__ == "__main__":
    main()
