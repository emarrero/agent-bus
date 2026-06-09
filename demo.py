"""Demo end-to-end del AgentBus.

Simula 3 agentes que se comunican a través del bus:
1. Investigador — busca información
2. Traductor — traduce textos
3. Escritor — redacta documentos

Prueba: flujo completo de registro, envío de tarea, procesamiento y respuesta.
"""

import json
import os
import sys
import time

# Asegurar que agent_bus está en el path
sys.path.insert(0, os.path.expanduser("~/.hermes"))
sys.path.insert(0, os.path.expanduser("~/.hermes/agent_bus"))

from agent_bus.client import AgentBusClient
from agent_bus.protocol import MessageType, TaskRequest, TaskStatus


def demo_text_communication():
    """Demo 1: Comunicación básica de texto."""
    print("=" * 60)
    print("DEMO 1: Comunicación de texto entre agentes")
    print("=" * 60)

    # Usar DB temporal
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "test_bus.db")

    # Crear 2 agentes
    alice = AgentBusClient("alice", "Alice", "Agente asistente general", db_path=db_path)
    alice.register(skills=["writing", "analysis"], modalities=["text"])

    bob = AgentBusClient("bob", "Bob", "Agente investigador", db_path=db_path)
    bob.register(skills=["research"], modalities=["text"])

    print(f"\n✅ Agentes registrados: {len(alice.find_agents())}")

    # Alice envía mensaje a Bob
    msg_id = alice.send_text("Hola Bob, ¿puedes investigar sobre IA?", target="bob")
    print(f"\n📤 Alice -> Bob: 'Hola Bob, ¿puedes investigar sobre IA?'")

    # Bob lee los mensajes
    time.sleep(0.1)
    msgs = bob.poll(limit=5)
    received = [m for m in msgs if m.target == "bob" or m.target == ""]

    print(f"📥 Bob recibió {len(received)} mensaje(s):")
    for m in received:
        print(f"   [{m.source} -> {m.target}] {m.payload}")

    # Bob responde
    bob.send_text("¡Hola Alice! Claro, investigo sobre IA ahora.", target="alice")
    time.sleep(0.1)
    alice_msgs = alice.poll(limit=5)
    print(f"📥 Alice recibió respuesta: '{alice_msgs[-1].payload}'")

    print(f"\n✅ Demo 1 completada. Stats: {alice.stats()}")

    alice.shutdown()
    bob.shutdown()


def demo_task_delegation():
    """Demo 2: Delegación de tareas entre agentes."""
    print("\n" + "=" * 60)
    print("DEMO 2: Delegación de tareas entre agentes")
    print("=" * 60)

    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "test_bus2.db")

    # Crear agentes especializados
    research = AgentBusClient(
        "research_agent", "Investigador",
        "Agente especializado en investigación y búsqueda de información",
        db_path=db_path,
    )
    research.register(skills=["research", "analysis"], modalities=["text"])

    writer = AgentBusClient(
        "writer_agent", "Escritor",
        "Agente especializado en redacción de documentos y resúmenes",
        db_path=db_path,
    )
    writer.register(skills=["writing"], modalities=["text"])

    print(f"\n✅ Agentes disponibles: {len(research.find_agents())}")
    for a in research.find_agents():
        print(f"   - {a.name} ({a.agent_id}): {a.skills}")

    # Buscar el mejor agente para una tarea
    best = research.find_agent_for_task("Necesito investigar sobre transformers")
    print(f"\n🔍 Mejor agente para 'investigar': {best.name if best else 'N/A'}")

    best2 = writer.find_agent_for_task("Escribe un resumen del tema")
    print(f"🔍 Mejor agente para 'escribe': {best2.name if best2 else 'N/A'}")

    # Delegar tarea del escritor al investigador
    task_id = writer.send_task(
        goal="Investiga qué son los transformers en NLP",
        context="Es para un artículo técnico. Necesito definición, historia y aplicaciones clave.",
        toolsets=["research"],
    )
    print(f"\n📤 Escritor delegó tarea al Investigador (task_id: {task_id})")

    # El investigador reclama la tarea
    time.sleep(0.1)
    task = research.claim_next_task()
    if task:
        print(f"📥 Investigador reclamó la tarea: '{task.goal}'")
        print(f"   Contexto: {task.context[:80]}...")

        # Procesar (simulado)
        result = (
            "Los transformers son una arquitectura de redes neuronales "
            "introducida en 2017 por Google. Son la base de modelos como "
            "GPT, BERT y Claude. Reemplazaron a las RNN/LSTM para NLP."
        )

        # Completar la tarea
        research.respond_to_task(task.task_id, result)
        print(f"📤 Investigador respondió con el resultado")

    # El escritor verifica el resultado
    time.sleep(0.1)
    status = writer.bus.get_task_status(task_id)
    if status:
        print(f"\n📊 Estado de la tarea: {status['status']}")
        result_data = status.get("result", "{}")
        if isinstance(result_data, str):
            try:
                result_data = json.loads(result_data)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(result_data, str):
            print(f"📄 Resultado: {result_data[:100]}...")
        elif isinstance(result_data, dict):
            print(f"📄 Resultado: {str(result_data.get('result', result_data))[:100]}...")

    print(f"\n✅ Demo 2 completada. Stats: {research.stats()}")

    research.shutdown()
    writer.shutdown()


def demo_multimodal():
    """Demo 3: Capa multimodal (STT/TTS)."""
    print("\n" + "=" * 60)
    print("DEMO 3: Capa multimodal (texto -> audio -> texto)")
    print("=" * 60)

    # Verificar dependencias
    try:
        import edge_tts
        has_tts = True
    except ImportError:
        has_tts = subprocess.run(
            ["which", "edge-tts"], capture_output=True
        ).returncode == 0

    try:
        import faster_whisper
        has_stt = True
    except ImportError:
        has_stt = False

    print(f"   TTS disponible: {has_tts}")
    print(f"   STT disponible: {has_stt}")

    if has_tts:
        import tempfile
        from agent_bus.multimodal import MultimodalLayer

        mm = MultimodalLayer()
        text = "Este es un mensaje de prueba para el sistema multi-agente."
        print(f"\n📝 Texto original: '{text}'")

        audio_path = mm.synthesize(text)
        print(f"🔊 Audio generado: {audio_path}")
        print(f"   Tamaño: {os.path.getsize(audio_path)} bytes")

        if has_stt:
            transcribed = mm.transcribe(audio_path)
            print(f"📝 Transcripción: '{transcribed}'")
        else:
            print("ℹ️ STT local no disponible (faster-whisper no instalado)")
    else:
        print("\nℹ️ edge-tts no instalado. Para instalarlo:")
        print("   pip install edge-tts")

    print(f"\n✅ Demo 3 completada.")


def demo_agent_discovery():
    """Demo 4: Descubrimiento y routing inteligente."""
    print("\n" + "=" * 60)
    print("DEMO 4: Descubrimiento de agentes y routing inteligente")
    print("=" * 60)

    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "test_bus3.db")

    # Registrar varios agentes
    agents = [
        ("investigador", "Investigador", "Busca y analiza información", ["research", "analysis"]),
        ("traductor", "Traductor", "Traduce entre idiomas", ["translation"]),
        ("escritor", "Escritor", "Redacta documentos y resúmenes", ["writing"]),
        ("codificador", "Codificador", "Escribe y revisa código", ["code"]),
    ]

    for agent_id, name, desc, skills in agents:
        client = AgentBusClient(agent_id, name, desc, db_path=db_path)
        client.register(skills=skills)

    # Probar routing con diferentes consultas
    queries = [
        "Necesito traducir este documento al inglés",
        "Busca información sobre el cambio climático",
        "Escribe un resumen ejecutivo",
        "Implementa una función en Python",
    ]

    router = client  # El último, cualquiera sirve
    for q in queries:
        best = router.find_agent_for_task(q)
        print(f"\n🔍 Consulta: '{q}'")
        print(f"   → Mejor agente: {best.name if best else 'N/A'} ({best.agent_id if best else '?'})")

    print(f"\n✅ Demo 4 completada.")


if __name__ == "__main__":
    import subprocess

    print("🧪 AgentBus — Suite de pruebas")
    print()

    demo_text_communication()
    demo_task_delegation()
    demo_multimodal()
    demo_agent_discovery()

    print("\n" + "=" * 60)
    print("🎉 Todas las demos completadas!")
    print("=" * 60)
