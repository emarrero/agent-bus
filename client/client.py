"""AgentBusClient — Unified client for multi-agent communication.

Supports TWO modes:

1. LOCAL mode (default):
   - Uses local MessageBus with SQLite
   - All agents in the same process
   - No token required

2. NETWORK mode:
   - Connects to an AgentBus Server via HTTP
   - Shared token defines the private channel
   - Agents in different processes/machines

Typical usage (network mode):
    # Agent A
    agent = AgentBusClient("agent1", token="secret123",
                           server_url="http://localhost:9876")
    agent.register(skills=["research"])

    # Agent B (another process)
    agent = AgentBusClient("agent2", token="secret123",
                           server_url="http://localhost:9876")
    agent.register(skills=["writing"])

    # They can see each other because they share the same token
    agent.send_text("Hello!", target="agent2")
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any, Callable

# Zero-config import: package-relative when deployed flat (protocol.py is a
# sibling in ~/.hermes/agent_bus/), file load from ../server/ in the repo
# layout (protocol.py lives in server/).
try:
    from .protocol import AgentCard, Message, MessageType, TaskRequest, TaskResponse, TaskStatus
except ImportError:
    import importlib.util as _ilu
    import sys as _sys

    _here = os.path.dirname(os.path.abspath(__file__))
    _candidates = [
        os.path.join(_here, "protocol.py"),                    # deployed flat
        os.path.join(_here, "..", "server", "protocol.py"),    # repo layout
    ]
    _mod = _sys.modules.get("_agentbus_protocol")
    if _mod is None:
        for _path in _candidates:
            if os.path.exists(_path):
                _spec = _ilu.spec_from_file_location("_agentbus_protocol", _path)
                _mod = _ilu.module_from_spec(_spec)
                _sys.modules["_agentbus_protocol"] = _mod
                _spec.loader.exec_module(_mod)
                break
        else:
            raise
    AgentCard, Message, MessageType = _mod.AgentCard, _mod.Message, _mod.MessageType
    TaskRequest, TaskResponse, TaskStatus = _mod.TaskRequest, _mod.TaskResponse, _mod.TaskStatus


class AgentBusClient:
    """Bus client for an agent.

    Args:
        agent_id: Unique agent ID
        name: Human-readable name (optional, default=agent_id)
        description: Agent description
        server_url: AgentBus Server URL (None = local mode)
        token: Shared token for the private channel (network mode)
        db_path: SQLite path (local mode only)
    """

    def __init__(
        self,
        agent_id: str,
        name: str = "",
        description: str = "",
        server_url: str | None = None,
        token: str = "",
        db_path: str | None = None,
    ):
        self.agent_id = agent_id
        self.name = name or agent_id
        self.description = description
        self.server_url = server_url.rstrip("/") if server_url else None
        self.token = token or ""
        self._card: AgentCard | None = None
        self._local_bus = None
        self._multimodal = None

        if not self.server_url:
            from .bus import MessageBus
            self._local_bus = MessageBus(db_path=db_path)
            from .router import AgentRouter
            self._router = __import__("agent_bus.router", fromlist=["AgentRouter"]).AgentRouter(self._local_bus)

        self._mm = None

    @property
    def multimodal(self):
        if self._mm is None:
            from .multimodal import MultimodalLayer
            self._mm = MultimodalLayer()
        return self._mm

    @property
    def card(self) -> AgentCard | None:
        return self._card

    @property
    def is_network(self) -> bool:
        return self.server_url is not None

    # ── HTTP helpers ─────────────────────────────────────────────────

    def _api(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        """Make an HTTP call to the server."""
        url = f"{self.server_url}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url += f"?{qs}"

        headers = {
            "Content-Type": "application/json",
            "X-Agent-Token": self.token,
        }
        data = json.dumps(body, ensure_ascii=False).encode() if body else None

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            try:
                return json.loads(error_body)
            except json.JSONDecodeError:
                return {"status": "error", "message": f"HTTP {e.code}: {error_body}"}
        except urllib.error.URLError as e:
            return {"status": "error", "message": f"Connection failed: {e.reason}"}

    # ── Registration ─────────────────────────────────────────────────

    def register(
        self,
        skills: list[str] | None = None,
        modalities: list[str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> AgentCard:
        """Register this agent on the network (or local bus)."""
        self._card = AgentCard(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            skills=skills or [],
            modalities=modalities or ["text"],
            tags=tags or {},
        )

        if self.is_network:
            result = self._api("POST", "/register", {
                "agent_id": self.agent_id,
                "card": self._card.to_dict(),
                "token": self.token,
            })
            if result.get("status") != "ok":
                print(f"[AgentBus] Registration error: {result.get('message', 'unknown')}")
        else:
            self._local_bus.register_agent(self._card)

        return self._card

    def shutdown(self):
        """Unregister the agent from the network."""
        if self.is_network:
            self._api("POST", "/unregister", {
                "agent_id": self.agent_id,
                "token": self.token,
            })
        elif self._local_bus:
            self._local_bus.unregister_agent(self.agent_id)

    # ── Sending messages ─────────────────────────────────────────────

    def send_text(self, text: str, target: str = "") -> str:
        """Send a text message to another agent (broadcast if target='')."""
        msg_id = str(__import__("uuid", fromlist=["uuid4"]).uuid4())

        if self.is_network:
            self._api("POST", "/message", {
                "message": {
                    "id": msg_id,
                    "type": "text",
                    "source": self.agent_id,
                    "target": target,
                    "payload": text,
                },
                "token": self.token,
            })
        else:
            msg = Message(
                id=msg_id,
                type=MessageType.TEXT,
                source=self.agent_id,
                target=target,
                payload=text,
            )
            self._local_bus.publish(msg)

        return msg_id

    def send_audio(self, audio_path: str, target: str = "") -> str:
        """Send an audio file (automatically transcribed)."""
        msg_id = str(__import__("uuid", fromlist=["uuid4"]).uuid4())

        if self.is_network:
            self._api("POST", "/message", {
                "message": {
                    "id": msg_id,
                    "type": "audio",
                    "source": self.agent_id,
                    "target": target,
                    "payload": audio_path,
                },
                "token": self.token,
            })
        else:
            msg = Message(
                id=msg_id,
                type=MessageType.AUDIO,
                source=self.agent_id,
                target=target,
                payload=audio_path,
            )
            self._local_bus.publish(msg)

        return msg_id

    def send_task(
        self,
        goal: str,
        context: str = "",
        target: str = "",
        toolsets: list[str] | None = None,
        modalities: list[str] | None = None,
    ) -> str:
        """Delegate a task to another agent.

        If target is empty, the router decides who receives it.
        In network mode, an empty target lets the first available agent take it.
        """
        task_id = str(__import__("uuid", fromlist=["uuid4"]).uuid4())

        if self.is_network:
            self._api("POST", "/task", {
                "task": {
                    "task_id": task_id,
                    "source_agent": self.agent_id,
                    "target_agent": target,
                    "goal": goal,
                    "context": context,
                    "toolsets": toolsets or [],
                    "modalities": modalities or ["text"],
                    "status": "pending",
                },
                "token": self.token,
            })
        else:
            task = TaskRequest(
                task_id=task_id,
                source_agent=self.agent_id,
                target_agent=target,
                goal=goal,
                context=context,
                toolsets=toolsets or [],
                modalities=modalities or ["text"],
            )
            self._local_bus.submit_task(task)

        return task_id

    # ── Receiving messages ───────────────────────────────────────────

    def poll(self, since: str | None = None, limit: int = 20) -> list[Message]:
        """Read messages directed to this agent."""
        if self.is_network:
            params = {"token": self.token, "agent_id": self.agent_id, "limit": str(limit)}
            if since:
                params["since"] = since
            result = self._api("GET", "/messages", params=params)
            if result.get("status") == "ok":
                return [Message.from_dict(m) for m in result.get("messages", [])]
            return []
        else:
            return self._local_bus.read_messages(agent_id=self.agent_id, limit=limit, since=since)

    def listen(self, callback: Callable[[Message], None], msg_type: str | None = None):
        """Blocking listen loop. Executes callback for each new message."""
        last_id = None
        while True:
            msgs = self.poll(limit=20)
            for msg in msgs:
                if last_id and msg.id == last_id:
                    break
                if msg_type is None or msg.type.value == msg_type:
                    try:
                        callback(msg)
                    except Exception as e:
                        print(f"[AgentBus] Callback error: {e}")
                last_id = msg.id
            time.sleep(1)

    # ── Tasks ────────────────────────────────────────────────────────

    def claim_next_task(self) -> dict | None:
        """Take the next pending task."""
        if self.is_network:
            params = {"token": self.token, "agent_id": self.agent_id}
            result = self._api("GET", "/task", params=params)
            if result.get("status") == "ok":
                return result.get("task")
            return None
        else:
            return self._local_bus.claim_task(self.agent_id)

    def respond_to_task(self, task_id: str, result: Any, error: str | None = None):
        """Respond with the result of a delegated task."""
        if self.is_network:
            self._api("POST", "/task/complete", {
                "task_id": task_id,
                "result": result,
                "error": error,
                "token": self.token,
            })
        else:
            self._local_bus.complete_task(TaskResponse(
                task_id=task_id,
                source_agent=self.agent_id,
                target_agent="",
                status=TaskStatus.COMPLETED if error is None else TaskStatus.FAILED,
                result=result,
                error=error,
            ))

    def get_task_status(self, task_id: str) -> dict | None:
        """Query the status of a task."""
        if self.is_network:
            params = {"token": self.token, "task_id": task_id}
            result = self._api("GET", "/task", params=params)
            if result.get("status") == "ok":
                return result.get("task")
            return None
        else:
            return self._local_bus.get_task_status(task_id)

    # ── Discovery ────────────────────────────────────────────────────

    def find_agents(self, skill: str | None = None) -> list[AgentCard]:
        """List agents connected to the same network (same token)."""
        if self.is_network:
            params = {"token": self.token}
            result = self._api("GET", "/agents", params=params)
            if result.get("status") != "ok":
                return []
            agents = [AgentCard.from_dict(a) for a in result.get("agents", [])]
        else:
            agents = self._local_bus.list_agents(active_only=True)

        if skill:
            agents = [a for a in agents if skill in a.skills]
        return agents

    # ── Multimodal ───────────────────────────────────────────────────

    def process_audio(self, audio_path: str) -> Message:
        """Transcribe an audio file to text."""
        if self._mm is None:
            from .multimodal import MultimodalLayer
            self._mm = MultimodalLayer()
        text = self._mm.transcribe(audio_path)
        return Message(
            type=MessageType.TEXT,
            source=self.agent_id,
            target="",
            payload=text,
            metadata={"original_audio": audio_path, "transcribed": True},
        )

    def text_to_audio(self, text: str, target: str = "") -> Message:
        """Convert text to audio."""
        if self._mm is None:
            from .multimodal import MultimodalLayer
            self._mm = MultimodalLayer()
        audio_path = self._mm.synthesize(text)
        return Message(
            type=MessageType.AUDIO,
            source=self.agent_id,
            target=target,
            payload={"text": text, "audio_path": audio_path},
        )

    # ── Statistics ───────────────────────────────────────────────────

    def stats(self) -> dict:
        if self.is_network:
            params = {"token": self.token}
            result = self._api("GET", "/stats", params=params)
            return result.get("stats", {})
        else:
            return self._local_bus.stats()
