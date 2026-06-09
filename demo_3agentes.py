"""Demo End-to-End: 3 Agentes Reales

Flujo completo:
  1. Inicia servidor AgentBus
  2. Registra 3 agentes: Investigador, Escritor, Traductor
  3. Investigador investiga un tema y pasa resultado a Escritor
  4. Escritor redacta un artículo y pasa a Traductor
  5. Traductor traduce el artículo
  6. Muestra el resultado final

Uso:
    python3 demo_3agentes.py
    python3 demo_3agentes.py --ws-port 9876  # puerto personalizado

Para audio (TTS):
    python3 demo_3agentes.py --audio       # requiere edge-tts
"""

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.expanduser("~/.hermes"))

from agent_bus.hermes_agent import connect_to_bus


# ── Config ────────────────────────────────────────────────────────────

SERVER = "ws://localhost:9876"
TOKEN = "demo_3agentes"
AUDIO_ENABLED = "--audio" in sys.argv


# ── Agentes ───────────────────────────────────────────────────────────

class Investigador:
    """Busca y analiza información."""

    def __init__(self, bus):
        self.bus = bus
        self.name = "Investigador"

    async def investigar(self, tema: str) -> str:
        """Simula investigación de un tema."""
        print(f"\n🔬 {self.name}: Investigando '{tema}'...")
        await asyncio.sleep(0.5)

        resultado = (
            f"INFORME DE INVESTIGACIÓN: {tema}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Los transformers son una arquitectura de deep learning "
            f"introducida por Google en 2017 ('Attention is All You Need'). "
            f"Reemplazaron a las RNN/LSTM en NLP usando el mecanismo de "
            f"auto-atención (self-attention). Son la base de modelos como "
            f"GPT, BERT, Claude, Gemini y Llama.\n\n"
            f"Arquitectura clave:\n"
            f"• Encoder-Decoder con atención multi-cabeza\n"
            f"• Positional encoding para orden secuencial\n"
            f"• Feed-forward networks por capa\n"
            f"• Normalización y conexiones residuales\n\n"
            f"Impacto: revolucionaron el NLP y ahora se aplican en "
            f"visión (ViT), audio (Whisper) y más."
        )

        print(f"   ✅ Investigación completa ({len(resultado)} chars)")
        return resultado


class Escritor:
    """Redacta documentos y resúmenes."""

    def __init__(self, bus):
        self.bus = bus
        self.name = "Escritor"

    async def redactar(self, material: str, tema: str) -> str:
        """Redacta un artículo basado en material de investigación."""
        print(f"\n✍️  {self.name}: Redactando artículo sobre '{tema}'...")
        await asyncio.sleep(0.5)

        articulo = (
            f"ARTÍCULO: {tema}\n"
        )

        if "transformers" in material.lower():
            articulo += (
                f"\nLos Transformers: La Revolución del Deep Learning\n"
                f"{'='*50}\n\n"
                f"En 2017, Google publicó 'Attention is All You Need', "
                f"introduciendo los transformers, una arquitectura que "
                f"cambió para siempre el procesamiento del lenguaje natural.\n\n"
                f"¿Qué los hace especiales? A diferencia de las RNNs "
                f"tradicionales, los transformers procesan toda la secuencia "
                f"en paralelo usando un mecanismo de auto-atención. Esto "
                f"permite capturar relaciones entre palabras lejanas sin "
                f"los problemas de desvanecimiento de gradiente.\n\n"
                f"Hoy, los transformers son la base de GPT, BERT, Claude, "
                f"Gemini y prácticamente todos los modelos de lenguaje modernos."
            )

        print(f"   ✅ Artículo redactado ({len(articulo)} chars)")
        return articulo

    async def resumir(self, texto: str) -> str:
        """Genera un resumen ejecutivo."""
        print(f"\n✍️  {self.name}: Generando resumen ejecutivo...")
        await asyncio.sleep(0.3)
        resumen = (
            "RESUMEN EJECUTIVO:\n"
            "Los transformers (2017) son una arquitectura de IA "
            "revolucionaria basada en auto-atención. Reemplazaron "
            "a las RNNs y son la base de todos los LLMs modernos "
            "(GPT, Claude, Gemini, etc.)."
        )
        print(f"   ✅ Resumen generado")
        return resumen


class Traductor:
    """Traduce textos entre idiomas."""

    def __init__(self, bus):
        self.bus = bus
        self.name = "Traductor"

    async def traducir(self, texto: str, idioma: str = "inglés") -> str:
        """Traduce texto a otro idioma."""
        print(f"\n🌐 {self.name}: Traduciendo a {idioma}...")
        await asyncio.sleep(0.5)

        if idioma == "inglés":
            traduccion = (
                "ARTICLE: The Transformer Revolution in Deep Learning\n"
                "================================================\n\n"
                "In 2017, Google published 'Attention is All You Need', "
                "introducing transformers, an architecture that forever "
                "changed natural language processing.\n\n"
                "What makes them special? Unlike traditional RNNs, "
                "transformers process the entire sequence in parallel "
                "using a self-attention mechanism. This captures "
                "relationships between distant words without gradient "
                "vanishing problems.\n\n"
                "Today, transformers are the foundation of GPT, BERT, "
                "Claude, Gemini, and virtually all modern language models."
            )
        elif idioma == "portugués":
            traduccion = (
                "ARTIGO: A Revolução dos Transformers no Deep Learning\n"
                "====================================================\n\n"
                "Em 2017, o Google publicou 'Attention is All You Need'..."
            )
        else:
            traduccion = f"[Traducción a {idioma} no implementada]"

        print(f"   ✅ Traducción completada")
        return traduccion


# ── Flujo principal ───────────────────────────────────────────────────

async def flujo_completo():
    print(f"""
╔══════════════════════════════════════════════╗
║     Demo: 3 Agentes en Red                   ║
║     Token: {TOKEN}                ║
║     Audio: {'SÍ' if AUDIO_ENABLED else 'NO'}                                   ║
╚══════════════════════════════════════════════╝
    """)

    # ── 1. Conectar agentes al bus ───────────────────────────────────
    print("🔄 Conectando agentes al bus...")

    inv_bus = await connect_to_bus(
        "investigador", token=TOKEN, server=SERVER,
        name="Investigador", skills=["research", "analysis"],
        modalities=["text", "audio"] if AUDIO_ENABLED else ["text"],
    )
    inv = Investigador(inv_bus)
    print(f"   ✅ {inv.name} conectado")

    esc_bus = await connect_to_bus(
        "escritor", token=TOKEN, server=SERVER,
        name="Escritor", skills=["writing"],
        modalities=["text", "audio"] if AUDIO_ENABLED else ["text"],
    )
    esc = Escritor(esc_bus)
    print(f"   ✅ {esc.name} conectado")

    tra_bus = await connect_to_bus(
        "traductor", token=TOKEN, server=SERVER,
        name="Traductor", skills=["translation"],
        modalities=["text", "audio"] if AUDIO_ENABLED else ["text"],
    )
    tra = Traductor(tra_bus)
    print(f"   ✅ {tra.name} conectado")

    # Pequeña pausa para que todos reciban notifications
    await asyncio.sleep(0.3)

    # ── 2. Ver agentes conectados ────────────────────────────────────
    # Usar HTTP API para listar agentes
    import urllib.request
    try:
        http_url = SERVER.replace("ws://", "http://").replace(":9876", ":9877")
        req = urllib.request.Request(f"{http_url}/agents",
            headers={"X-Agent-Token": TOKEN})
        resp = json.loads(urllib.request.urlopen(req, timeout=3).read())
        peers = resp.get("agents", [])
    except Exception:
        peers = []
    print(f"\n👥 Agentes en red ({len(peers)}):")
    for a in peers:
        print(f"   • {a.get('name', '?')} — {', '.join(a.get('skills', []))}")

    # ── 3. FLUJO PRINCIPAL ───────────────────────────────────────────
    tema = "Transformers en Deep Learning"

    print(f"\n{'='*55}")
    print(f"📋 FLUJO: Investigar → Redactar → Traducir")
    print(f"{'='*55}")

    # Paso 1: Investigador investiga
    informe = await inv.investigar(tema)

    # Investigador envía resultado al Escritor vía bus
    await inv_bus.send_message(
        f"📄 Informe listo para '{tema}'. Redacta un artículo.",
        target="escritor",
    )
    print(f"\n📤 {inv.name} → Escritor: Informe enviado al bus")

    # Paso 2: Escritor redacta
    articulo = await esc.redactar(informe, tema)

    # Escritor envía artículo al Traductor vía bus
    await esc_bus.send_message(
        f"📝 Artículo redactado para '{tema}'. Traduce a inglés.",
        target="traductor",
    )
    print(f"\n📤 {esc.name} → Traductor: Artículo enviado al bus")

    # Paso 3: Escritor también genera resumen
    resumen = await esc.resumir(articulo)

    # Paso 4: Traductor traduce
    traduccion = await tra.traducir(articulo, "inglés")

    # Traductor confirma al Escritor
    await tra_bus.send_message(
        f"🌐 Traducción completada para '{tema}'.",
        target="escritor",
    )
    print(f"\n📤 {tra.name} → Escritor: Traducción completada")

    # ── 4. Mostrar resultados ────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"📊 RESULTADOS FINALES")
    print(f"{'='*55}")

    print(f"\n{'─'*55}")
    print(f"📄 INFORME DE INVESTIGACIÓN")
    print(f"{'─'*55}")
    print(informe[:300] + "...")

    print(f"\n{'─'*55}")
    print(f"📝 ARTÍCULO")
    print(f"{'─'*55}")
    print(articulo[:300] + "...")

    print(f"\n{'─'*55}")
    print(f"📋 RESUMEN EJECUTIVO")
    print(f"{'─'*55}")
    print(resumen)

    print(f"\n{'─'*55}")
    print(f"🌐 TRADUCCIÓN (inglés)")
    print(f"{'─'*55}")
    print(traduccion[:300] + "...")

    # ── 5. Mostrar estadísticas del bus ──────────────────────────────
    print(f"\n{'─'*55}")
    print(f"📊 ESTADÍSTICAS DEL BUS")
    print(f"{'─'*55}")

    # Ver cuántos mensajes hay via HTTP
    import urllib.request
    try:
        http_url = SERVER.replace("ws://", "http://").replace(":9876", ":9877")
        req = urllib.request.Request(f"{http_url}/stats")
        resp = json.loads(urllib.request.urlopen(req, timeout=3).read())
        if resp.get("status") == "ok":
            print(f"   Redes activas:     {resp.get('networks', '?')}")
            print(f"   Agentes totales:   {resp.get('total_agents', '?')}")
            print(f"   Mensajes totales:  {resp.get('total_messages', '?')}")
            print(f"   Tiempo activo:     {resp.get('server_uptime', '?')}s")
    except Exception as e:
        print(f"   (stats no disponibles: {e})")

    # ── 6. Audio (opcional) ─────────────────────────────────────────
    if AUDIO_ENABLED:
        print(f"\n{'─'*55}")
        print(f"🎧 AUDIO (TTS)")
        print(f"{'─'*55}")
        try:
            import subprocess, tempfile
            audio_path = os.path.join(tempfile.gettempdir(), "agentbus_resumen.mp3")
            result = subprocess.run(
                ["python3", "-m", "edge_tts", "--voice", "es-ES-ElviraNeural",
                 "--text", resumen, "--write-media", audio_path],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and os.path.exists(audio_path):
                size_kb = os.path.getsize(audio_path) / 1024
                print(f"   ✅ Audio generado: {audio_path}")
                print(f"   Tamaño: {size_kb:.0f} KB")
                print(f"   Contenido: '{resumen[:60]}...'")
            else:
                # Fallback con Python edge-tts
                import edge_tts
                async def _gen():
                    comm = edge_tts.Communicate(text=resumen, voice="es-ES-ElviraNeural")
                    await comm.save(audio_path)
                asyncio.ensure_future(_gen())
                print(f"   ⏳ Audio generándose en background: {audio_path}")
        except Exception as e:
            print(f"   ❌ Audio no disponible: {e}")

    # ── 7. Desconectar ──────────────────────────────────────────────
    await inv_bus.disconnect()
    await esc_bus.disconnect()
    await tra_bus.disconnect()

    print(f"\n{'='*55}")
    print(f"🎉 ¡Demo completada exitosamente!")
    print(f"{'='*55}")
    print(f"\nResumen del flujo:")
    print(f"   1. {inv.name} investigó '{tema}'")
    print(f"   2. Envió informe al {esc.name} vía bus")
    print(f"   3. {esc.name} redactó artículo + resumen")
    print(f"   4. Envió artículo al {tra.name} vía bus")
    print(f"   5. {tra.name} tradujo a inglés")
    print(f"   6. Todo el flujo con comunicación entre agentes")

    if not AUDIO_ENABLED:
        print(f"\n💡 Para activar audio: python3 demo_3agentes.py --audio")


async def main():
    # Verificar servidor
    import urllib.request
    import urllib.error
    try:
        http_url = SERVER.replace("ws://", "http://").replace(":9876", ":9877")
        resp = urllib.request.urlopen(f"{http_url}/health", timeout=3)
        if json.loads(resp.read()).get("status") != "ok":
            raise ConnectionError("Servidor no responde")
    except (urllib.error.URLError, ConnectionRefusedError, ConnectionError):
        print(f"❌ Servidor no encontrado en {SERVER}")
        print(f"   Inicia el servidor primero:")
        print(f"   python3 ~/.hermes/agent_bus/server_ws.py")
        sys.exit(1)

    await flujo_completo()


if __name__ == "__main__":
    asyncio.run(main())
