"""Intelligent message router between agents.

Analyzes incoming messages and routes them to the most suitable agent
based on their capabilities (AgentCard).
"""

from __future__ import annotations

import json
from typing import Any

# Zero-config import: package-relative when loaded as part of a package,
# sibling-file load when executed without package context.
try:
    from .protocol import AgentCard, Message, MessageType, TaskRequest
    from .bus import MessageBus
except ImportError:
    import importlib.util as _ilu
    import os as _os
    import sys as _sys

    def _load_sibling(name: str, alias: str):
        mod = _sys.modules.get(alias)
        if mod is None:
            path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), name)
            spec = _ilu.spec_from_file_location(alias, path)
            mod = _ilu.module_from_spec(spec)
            _sys.modules[alias] = mod
            spec.loader.exec_module(mod)
        return mod

    _proto = _load_sibling("protocol.py", "_agentbus_protocol")
    AgentCard, Message = _proto.AgentCard, _proto.Message
    MessageType, TaskRequest = _proto.MessageType, _proto.TaskRequest
    MessageBus = _load_sibling("bus.py", "_agentbus_bus").MessageBus


class AgentRouter:
    """Routes incoming messages to the most suitable agent.

    Routing strategies:
    1. Explicit: if the message has a target, it goes directly to that agent
    2. By skill: if the task mentions a specific skill, to the agent that has it
    3. By keywords: keyword matching on the goal vs agent description
    4. Broadcast: if no match is found, sent to all (first one to respond)
    """

    def __init__(self, bus: MessageBus):
        self.bus = bus

    def route(self, msg: Message) -> list[AgentCard]:
        """Determine which agent(s) should receive this message.

        Returns:
            List of AgentCard for the target agents.
        """
        if msg.target and msg.target != "*":
            agent = self._find_agent_by_id(msg.target)
            return [agent] if agent else []

        if msg.type in (MessageType.AGENT_ANNOUNCE, MessageType.AGENT_HEARTBEAT):
            return self.bus.list_agents(active_only=True)

        payload_text = self._get_payload_text(msg)
        return self._route_by_content(payload_text, msg.type)

    def route_task(self, task: TaskRequest) -> list[AgentCard]:
        """Route a task to the best agent."""
        if task.target_agent:
            agent = self._find_agent_by_id(task.target_agent)
            return [agent] if agent else []

        text = f"{task.goal} {task.context}"
        candidates = self._route_by_content(text, MessageType.TASK_REQUEST)

        if task.toolsets:
            candidates = [c for c in candidates
                          if any(t in task.toolsets for t in c.tags.get("toolsets", "").split(","))]

        return candidates

    def _route_by_content(self, text: str, msg_type: MessageType) -> list[AgentCard]:
        """Route based on message content."""
        text_lower = text.lower()
        agents = self.bus.list_agents(active_only=True)

        if not agents:
            return []

        # Scoring: each skill contributes points based on matching keywords
        skill_keywords = {
            "research": [
                "research", "find", "search", "lookup", "investigate",
                "discover", "explore", "gather", "data about", "look up",
                "information", "query",
            ],
            "writing": [
                "write", "draft", "document", "article", "blog", "summary",
                "summarize", "report", "essay", "compose", "create",
            ],
            "translation": [
                "translate", "translation", "language", "english", "spanish",
                "french", "german", "portuguese", "localize",
            ],
            "code": [
                "code", "program", "develop", "implement", "build", "function",
                "python", "script", "software", "programming", "algorithm",
            ],
            "analysis": [
                "analyze", "analysis", "data", "metrics", "evaluate",
                "measure", "compare", "statistics", "insights", "review",
            ],
            "audio": [
                "audio", "voice", "speech", "record", "listen", "sound",
                "transcribe", "tts", "stt",
            ],
        }

        scored: list[tuple[int, AgentCard]] = []
        for agent in agents:
            score = 0
            desc_lower = agent.description.lower()
            for skill_name, keywords in skill_keywords.items():
                if skill_name in agent.skills:
                    for kw in keywords:
                        if kw in text_lower:
                            score += 2
                        if kw in desc_lower:
                            score += 1
            if score > 0:
                scored.append((score, agent))

        if scored:
            scored.sort(key=lambda x: -x[0])
            return [scored[0][1]]

        # Broadcast — anyone can take it
        return agents

    def _find_agent_by_id(self, agent_id: str) -> AgentCard | None:
        """Find an agent by ID."""
        for agent in self.bus.list_agents(active_only=True):
            if agent.agent_id == agent_id:
                return agent
        return None

    def _get_payload_text(self, msg: Message) -> str:
        """Extract text from payload for routing analysis."""
        if isinstance(msg.payload, str):
            return msg.payload
        if isinstance(msg.payload, dict):
            return json.dumps(msg.payload)
        return str(msg.payload)
