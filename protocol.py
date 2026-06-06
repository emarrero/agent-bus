"""Agent communication protocol (inspired by A2A).

Defines:
- AgentCard: capability card for an agent
- Message: message types (text, audio, task)
- TaskRequest / TaskResponse: task delegation
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    """Message types that flow through the bus."""
    TEXT = "text"
    AUDIO = "audio"
    TASK_REQUEST = "task_request"
    TASK_RESPONSE = "task_response"
    AGENT_ANNOUNCE = "agent_announce"
    AGENT_HEARTBEAT = "agent_heartbeat"
    ERROR = "error"


class TaskStatus(str, Enum):
    """Status of a delegated task."""
    PENDING = "pending"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass
class AgentCard:
    """Agent capability card (inspired by A2A AgentCard).

    Published on the bus so other agents can discover it.
    """
    agent_id: str
    name: str
    description: str
    version: str = "0.1.0"

    # Capabilities
    skills: list[str] = field(default_factory=list)
    """Skills this agent can execute, e.g. ['research', 'writing', 'translation', 'code']"""

    modalities: list[str] = field(default_factory=lambda: ["text"])
    """Supported modalities: 'text', 'audio', 'image'."""

    # Contact
    endpoint: str | None = None
    """Optional A2A-style HTTP endpoint for direct communication."""

    # Metadata
    tags: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCard":
        valid_fields = {
            "agent_id", "name", "description", "version", "skills",
            "modalities", "endpoint", "tags", "created_at",
        }
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def from_json(cls, data: str) -> "AgentCard":
        return cls.from_dict(json.loads(data))


@dataclass
class Message:
    """Message that flows through the bus between agents."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: MessageType = MessageType.TEXT
    source: str = ""        # Sender agent ID
    target: str = ""        # Recipient agent ID ('' = broadcast)
    payload: Any = ""       # Message content
    reply_to: str | None = None  # ID of the message being replied to
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value if isinstance(self.type, MessageType) else self.type,
            "source": self.source,
            "target": self.target,
            "payload": self.payload,
            "reply_to": self.reply_to,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        data["type"] = MessageType(data["type"])
        return cls(**data)

    @classmethod
    def from_json(cls, data: str) -> "Message":
        return cls.from_dict(json.loads(data))


@dataclass
class TaskRequest:
    """Task delegation request to another agent."""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source_agent: str = ""
    target_agent: str = ""
    goal: str = ""
    context: str = ""
    toolsets: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=lambda: ["text"])
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_message(self) -> Message:
        return Message(
            type=MessageType.TASK_REQUEST,
            source=self.source_agent,
            target=self.target_agent,
            payload=self.to_dict(),
            metadata={"task_id": self.task_id},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "goal": self.goal,
            "context": self.context,
            "toolsets": self.toolsets,
            "modalities": self.modalities,
            "status": self.status.value,
            "created_at": self.created_at,
        }


@dataclass
class TaskResponse:
    """Response to a delegated task."""
    task_id: str = ""
    source_agent: str = ""
    target_agent: str = ""
    status: TaskStatus = TaskStatus.COMPLETED
    result: Any = ""
    error: str | None = None
    completed_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_message(self) -> Message:
        return Message(
            type=MessageType.TASK_RESPONSE,
            source=self.source_agent,
            target=self.target_agent,
            payload=self.to_dict(),
            metadata={"task_id": self.task_id},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "completed_at": self.completed_at,
        }
