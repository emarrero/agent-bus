"""Hermes Agent — Integration with AgentBus via WebSocket.

Usage:
    from agent_bus.hermes_agent import connect_to_bus

    bus = await connect_to_bus(
        agent_id="researcher",
        token="my_network",
        server="ws://localhost:9876",
        skills=["research", "analysis"],
    )

    # Listen for messages in real time (separate loop)
    async def listen():
        async for msg in bus.messages():
            if msg["type"] == "new_message":
                print(f"Received: {msg['message']['payload']}")

    asyncio.create_task(listen())
    await bus.send_message("Hello!", target="other_agent")
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

log = logging.getLogger("agent-bus")


class ChannelRedirect(Exception):
    """Raised when the server redirects the agent to a different channel hash."""
    def __init__(self, new_token: str, entry_token: str | None = None):
        self.new_token = new_token
        self.entry_token = entry_token
        super().__init__(f"Redirect to channel {new_token[:16]}…")


class HermesBusConnection:
    """Conexión WebSocket de un agente al AgentBus.

    Usa un background reader que despacha:
    - ACKs de operaciones → futures en _pending_acks
    - Mensajes entrantes  → asyncio.Queue (_inbox)

    Esto permite enviar y recibir mensajes en paralelo sin
    que los ACKs bloqueen o descarten mensajes de otros agentes.
    """

    def __init__(
        self,
        agent_id: str,
        token: str,
        server_url: str = "ws://localhost:9876",
        card: dict | None = None,
    ):
        self.agent_id = agent_id
        self.token = token
        self.server_url = server_url
        self.card = card or {"name": agent_id, "skills": [], "modalities": ["text"]}
        self.ws = None
        self._connected = False

        # Cola de mensajes entrantes (new_message, agent_joined, task_completed, …)
        self._inbox: asyncio.Queue[dict] = asyncio.Queue()

        # Futures pendientes por tipo de respuesta esperada
        # Ej: {"message_ack": Future, "task_ack": Future}
        self._pending_acks: dict[str, asyncio.Future] = {}

        # Lock para serializar envíos (evita mezclar ACKs entre coroutines)
        self._send_lock: asyncio.Lock | None = None

        # Task del reader background
        self._reader: asyncio.Task | None = None

    # ── Tipos que son respuestas directas a comandos ──────────────────

    _ACK_TYPES = frozenset({
        "message_ack",
        "task_ack",
        "claim_task_result",
        "task_complete_result",
        "pong",
    })

    # ── Conexión ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Conecta al servidor WebSocket y arranca el reader en background.
        
        Si el servidor responde con channel_redirect, la conexión se
        re-establece automáticamente con el token correcto.
        
        Raises:
            ChannelRedirect: después de 3 intentos de redirect fallidos
            ConnectionError: si no se puede registrar
        """
        import websockets

        self._send_lock = asyncio.Lock()
        self.ws = await websockets.connect(self.server_url)

        # Registro inicial (antes del reader, para simplificar el handshake)
        await self.ws.send(json.dumps({
            "type": "register",
            "agent_id": self.agent_id,
            "token": self.token,
            "card": self.card,
        }))

        # Leer respuesta de registro directamente (reader aún no arrancó)
        resp = json.loads(await self.ws.recv())

        # ── Canal hash redirect ─────────────────────────────────────────
        # Si el server nos dice "usá este otro token", reconectamos.
        redirect_attempts = getattr(self, '_redirect_attempts', 0)
        if resp.get("type") == "channel_redirect":
            new_token = resp.get("token", "")
            if not new_token:
                raise ConnectionError("channel_redirect sin token")
            if redirect_attempts >= 3:
                raise ChannelRedirect(new_token, resp.get("entry_token"))
            self._redirect_attempts = redirect_attempts + 1
            log.info("🔄 Redirected to canal hash %s… (attempt %d/3)",
                     new_token[:12], self._redirect_attempts)
            await self.ws.close()
            self.token = new_token
            await self.connect()  # reconecta con el nuevo token
            return

        if resp.get("status") != "ok":
            raise ConnectionError(f"Error registrando agente: {resp.get('message')}")

        # Leer agents_list que sigue al registro
        await self.ws.recv()

        self._connected = True

        # Arrancar reader en background AHORA
        self._reader = asyncio.create_task(self._read_loop(), name=f"bus-reader-{self.agent_id}")

        log.info("✅ %s connected to '%s' (token='%s')", self.agent_id, self.server_url, self.token)

    async def _read_loop(self) -> None:
        """Lee mensajes del WS continuamente y los despacha.

        - Si el tipo es un ACK conocido: resuelve el future pendiente.
        - Si no: pone el mensaje en _inbox para que lo consuma la app.
        """
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type in self._ACK_TYPES and msg_type in self._pending_acks:
                    fut = self._pending_acks.pop(msg_type)
                    if not fut.done():
                        fut.set_result(data)
                else:
                    await self._inbox.put(data)

        except Exception as exc:
            log.debug("Reader %s stopped: %s", self.agent_id, exc)
        finally:
            self._connected = False
            # Cancelar futures pendientes para que no se queden colgados
            for fut in self._pending_acks.values():
                if not fut.done():
                    fut.cancel()
            self._pending_acks.clear()

    # ── Helpers internos ──────────────────────────────────────────────

    async def _request(self, send_payload: dict, ack_type: str, timeout: float = 10) -> dict:
        """Envía un comando y espera su ACK de forma segura."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        async with self._send_lock:
            self._pending_acks[ack_type] = fut
            await self.ws.send(json.dumps(send_payload))

        return await asyncio.wait_for(fut, timeout=timeout)

    # ── API pública ───────────────────────────────────────────────────

    async def send_message(self, text: str, target: str = "") -> str | None:
        """Envía un mensaje de texto y devuelve el message_id."""
        if not self._connected:
            return None
        resp = await self._request(
            {
                "type": "message",
                "message": {
                    "source": self.agent_id,
                    "target": target,
                    "payload": text,
                    "type": "text",
                },
            },
            ack_type="message_ack",
        )
        return resp.get("message_id")

    async def delegate_task(
        self,
        goal: str,
        context: str = "",
        target: str = "",
    ) -> str | None:
        """Delega una tarea a otro agente y devuelve el task_id."""
        if not self._connected:
            return None
        resp = await self._request(
            {
                "type": "task",
                "task": {
                    "source_agent": self.agent_id,
                    "target_agent": target,
                    "goal": goal,
                    "context": context,
                    "status": "pending",
                },
            },
            ack_type="task_ack",
        )
        return resp.get("task_id")

    async def claim_task(self) -> dict | None:
        """Toma la siguiente tarea pendiente para este agente."""
        if not self._connected:
            return None
        resp = await self._request(
            {"type": "claim_task"},
            ack_type="claim_task_result",
        )
        return resp.get("task")

    async def complete_task(self, task_id: str, result: Any, error: str | None = None):
        """Marca una tarea como completada."""
        if not self._connected or not self.ws:
            return
        async with self._send_lock:
            await self.ws.send(json.dumps({
                "type": "task_complete",
                "task_id": task_id,
                "result": result,
                "error": error,
            }))

    async def ping(self) -> bool:
        """Verifica que la conexión siga viva."""
        if not self._connected:
            return False
        try:
            resp = await self._request({"type": "ping"}, ack_type="pong", timeout=5)
            return resp.get("type") == "pong"
        except asyncio.TimeoutError:
            return False

    # ── Recepción de mensajes ─────────────────────────────────────────

    async def messages(self) -> AsyncIterator[dict]:
        """Itera sobre mensajes entrantes en tiempo real.

        Devuelve mensajes de tipo: new_message, agent_joined,
        agent_left, task_completed, y cualquier otro push del server.

        Uso:
            async for msg in bus.messages():
                if msg["type"] == "new_message":
                    print(msg["message"]["payload"])
        """
        while self._connected or not self._inbox.empty():
            try:
                msg = await asyncio.wait_for(self._inbox.get(), timeout=0.5)
                yield msg
            except asyncio.TimeoutError:
                continue

    async def wait_for_message(
        self,
        timeout: float = 30,
        msg_type: str | None = None,
    ) -> dict | None:
        """Espera el próximo mensaje (opcionalmente filtrando por tipo).

        Los mensajes de otros tipos que lleguen mientras se espera
        se devuelven al inbox para no perderlos.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        pending: list[dict] = []

        try:
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(
                        self._inbox.get(), timeout=min(remaining, 0.5)
                    )
                    if msg_type is None or msg.get("type") == msg_type:
                        return msg
                    # Guardar para reencolar al salir
                    pending.append(msg)
                except asyncio.TimeoutError:
                    continue
        finally:
            # Reencolar mensajes que no eran el tipo buscado
            for m in pending:
                await self._inbox.put(m)

        return None

    # ── Ciclo de vida ─────────────────────────────────────────────────

    async def disconnect(self) -> None:
        """Desconecta limpiamente del servidor."""
        self._connected = False
        if self._reader and not self._reader.done():
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
        log.info("❌ %s disconnected", self.agent_id)

    @property
    def connected(self) -> bool:
        return self._connected


# ── Helper de conexión ────────────────────────────────────────────────

async def connect_to_bus(
    agent_id: str,
    token: str,
    server: str = "ws://localhost:9876",
    name: str | None = None,
    skills: list[str] | None = None,
    modalities: list[str] | None = None,
) -> HermesBusConnection:
    """Conecta un agente al bus y devuelve la conexión lista para usar.

    Args:
        agent_id: ID único del agente
        token: Token compartido (define la red privada)
        server: URL del servidor WebSocket
        name: Nombre del agente (default: agent_id)
        skills: Lista de habilidades del agente
        modalities: Modalidades que soporta (text, audio)
    """
    card = {
        "name": name or agent_id,
        "skills": skills or [],
        "modalities": modalities or ["text"],
    }

    bus = HermesBusConnection(
        agent_id=agent_id,
        token=token,
        server_url=server,
        card=card,
    )

    await bus.connect()
    return bus


# ── Demo ──────────────────────────────────────────────────────────────

async def demo_two_agents(server_url: str = "ws://localhost:9876"):
    """Demo: two agents chat in real time without polling."""
    print("=" * 55)
    print("Demo: Real-time messaging (no polling)")
    print("=" * 55)

    inv = await connect_to_bus(
        "researcher", token="demo", server=server_url,
        name="Researcher", skills=["research"],
    )
    wri = await connect_to_bus(
        "writer", token="demo", server=server_url,
        name="Writer", skills=["writing"],
    )
    print("✅ Researcher and Writer connected")

    # Researcher sends while Writer listens in parallel
    recv_task = asyncio.create_task(
        wri.wait_for_message(timeout=5, msg_type="new_message")
    )
    await inv.send_message("Hello Writer, can you draft a summary?", target="writer")
    print("📤 Researcher → Writer: message sent")

    msg = await recv_task
    if msg:
        print(f"📥 Writer receives in real time: '{msg['message']['payload']}'")

    # Writer replies
    recv2 = asyncio.create_task(
        inv.wait_for_message(timeout=5, msg_type="new_message")
    )
    await wri.send_message("Sure! Give me the topic.", target="researcher")

    msg2 = await recv2
    if msg2:
        print(f"📥 Researcher receives: '{msg2['message']['payload']}'")

    await inv.disconnect()
    await wri.disconnect()
    print("\n✅ Demo complete — real-time messages, no polling!")


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("❌ 'websockets' required: pip install websockets")
        return
    asyncio.run(demo_two_agents())


if __name__ == "__main__":
    main()
