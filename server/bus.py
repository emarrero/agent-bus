"""Message Bus — shared message queue between agents.

Backend: SQLite (easy to migrate to Redis).
Stores:
- Messages: message history
- AgentCards: registered agents
- Tasks: delegated task state
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Callable

# Zero-config import: package-relative when loaded as part of a package,
# sibling-file load when executed without package context.
try:
    from .protocol import (
        AgentCard,
        Message,
        MessageType,
        TaskRequest,
        TaskResponse,
        TaskStatus,
    )
except ImportError:
    import importlib.util as _ilu
    import sys as _sys

    _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protocol.py")
    _spec = _ilu.spec_from_file_location("_agentbus_protocol", _path)
    _mod = _sys.modules.get("_agentbus_protocol")
    if _mod is None:
        _mod = _ilu.module_from_spec(_spec)
        _sys.modules["_agentbus_protocol"] = _mod
        _spec.loader.exec_module(_mod)
    AgentCard, Message, MessageType = _mod.AgentCard, _mod.Message, _mod.MessageType
    TaskRequest, TaskResponse, TaskStatus = _mod.TaskRequest, _mod.TaskResponse, _mod.TaskStatus


class MessageBus:
    """Shared message bus between agents.

    Each agent connects to the bus, publishes its AgentCard,
    and can read/send messages to other agents.
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
            db_path = os.path.join(hermes_home, "agent_bus.db")
        self.db_path = db_path
        self._lock = threading.Lock()
        self._callbacks: dict[str, list[Callable]] = {}
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        type TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT '',
                        target TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL DEFAULT '',
                        reply_to TEXT,
                        timestamp TEXT NOT NULL,
                        metadata TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE TABLE IF NOT EXISTS agent_cards (
                        agent_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        card_json TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        is_active INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        source_agent TEXT NOT NULL DEFAULT '',
                        target_agent TEXT NOT NULL DEFAULT '',
                        goal TEXT NOT NULL DEFAULT '',
                        context TEXT NOT NULL DEFAULT '',
                        toolsets TEXT NOT NULL DEFAULT '[]',
                        modalities TEXT NOT NULL DEFAULT '["text"]',
                        status TEXT NOT NULL DEFAULT 'pending',
                        result TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_messages_target
                        ON messages(target);
                    CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                        ON messages(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_tasks_target
                        ON tasks(target_agent);
                    CREATE INDEX IF NOT EXISTS idx_tasks_status
                        ON tasks(status);
                """)
                conn.commit()
            finally:
                conn.close()

    # ── Agent registration ───────────────────────────────────────────

    def register_agent(self, card: AgentCard) -> None:
        """Register or update an agent on the bus."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO agent_cards
                       (agent_id, name, description, card_json, last_seen, is_active)
                       VALUES (?, ?, ?, ?, ?, 1)""",
                    (card.agent_id, card.name, card.description,
                     card.to_json(), datetime.utcnow().isoformat() + "Z"),
                )
                conn.commit()
            finally:
                conn.close()

        # Broadcast announcement
        self.publish(Message(
            type=MessageType.AGENT_ANNOUNCE,
            source=card.agent_id,
            target="",
            payload={"name": card.name, "skills": card.skills},
        ))

    def unregister_agent(self, agent_id: str) -> None:
        """Mark an agent as inactive."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE agent_cards SET is_active = 0 WHERE agent_id = ?",
                    (agent_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def list_agents(self, active_only: bool = True) -> list[AgentCard]:
        """List all registered agents on the bus."""
        with self._lock:
            conn = self._get_conn()
            try:
                if active_only:
                    rows = conn.execute(
                        "SELECT card_json FROM agent_cards WHERE is_active = 1"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT card_json FROM agent_cards"
                    ).fetchall()
                return [AgentCard.from_json(r["card_json"]) for r in rows]
            finally:
                conn.close()

    def find_agent_by_skill(self, skill: str) -> list[AgentCard]:
        """Find agents that have a specific skill."""
        agents = self.list_agents(active_only=True)
        return [a for a in agents if skill in a.skills]

    # ── Message publishing ───────────────────────────────────────────

    def publish(self, msg: Message) -> None:
        """Publish a message on the bus (stores it and notifies callbacks)."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO messages
                       (id, type, source, target, payload, reply_to, timestamp, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (msg.id, msg.type.value if hasattr(msg.type, 'value') else msg.type,
                     msg.source, msg.target,
                     json.dumps(msg.payload, ensure_ascii=False) if not isinstance(msg.payload, str) else msg.payload,
                     msg.reply_to, msg.timestamp,
                     json.dumps(msg.metadata, ensure_ascii=False)),
                )
                conn.commit()
            finally:
                conn.close()

        self._notify(msg)

    # ── Message reading ──────────────────────────────────────────────

    def read_messages(
        self,
        agent_id: str | None = None,
        msg_type: str | None = None,
        limit: int = 50,
        since: str | None = None,
    ) -> list[Message]:
        """Read messages from the bus with optional filters."""
        with self._lock:
            conn = self._get_conn()
            try:
                query = "SELECT * FROM messages WHERE 1=1"
                params = []

                if agent_id:
                    query += " AND (target = ? OR target = '' OR source = ?)"
                    params.extend([agent_id, agent_id])
                if msg_type:
                    query += " AND type = ?"
                    params.append(msg_type)
                if since:
                    query += " AND timestamp > ?"
                    params.append(since)

                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(query, params).fetchall()
                messages = []
                for r in reversed(rows):
                    payload = r["payload"]
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    messages.append(Message(
                        id=r["id"],
                        type=MessageType(r["type"]),
                        source=r["source"],
                        target=r["target"],
                        payload=payload,
                        reply_to=r["reply_to"],
                        timestamp=r["timestamp"],
                        metadata=json.loads(r["metadata"]) if r["metadata"] else {},
                    ))
                return messages
            finally:
                conn.close()

    def wait_for_message(
        self,
        agent_id: str,
        msg_type: str | None = None,
        timeout: float = 30.0,
    ) -> Message | None:
        """Wait (polling) for a message to arrive for an agent.

        Useful for agents running in a listen loop on the bus.
        """
        deadline = time.time() + timeout
        last_count = 0

        while time.time() < deadline:
            msgs = self.read_messages(agent_id=agent_id, msg_type=msg_type, limit=20)
            if len(msgs) > last_count:
                new = msgs[-(len(msgs) - last_count):] if last_count > 0 else msgs
                if new:
                    return new[-1]
                last_count = len(msgs)
            time.sleep(0.5)

        return None

    # ── Task management ──────────────────────────────────────────────

    def submit_task(self, task: TaskRequest) -> str:
        """Submit a task to the bus for another agent to process."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO tasks
                       (task_id, source_agent, target_agent, goal, context,
                        toolsets, modalities, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task.task_id, task.source_agent, task.target_agent,
                     task.goal, task.context,
                     json.dumps(task.toolsets), json.dumps(task.modalities),
                     task.status.value, task.created_at),
                )
                conn.commit()
            finally:
                conn.close()

        self.publish(task.to_message())
        return task.task_id

    def claim_task(self, agent_id: str) -> TaskRequest | None:
        """Reserve the next pending task directed to this agent."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT * FROM tasks
                       WHERE target_agent = ? AND status = 'pending'
                       ORDER BY created_at ASC LIMIT 1""",
                    (agent_id,),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE tasks SET status = ? WHERE task_id = ?",
                        (TaskStatus.IN_PROGRESS.value, row["task_id"]),
                    )
                    conn.commit()
                    return TaskRequest(
                        task_id=row["task_id"],
                        source_agent=row["source_agent"],
                        target_agent=row["target_agent"],
                        goal=row["goal"],
                        context=row["context"],
                        toolsets=json.loads(row["toolsets"]),
                        modalities=json.loads(row["modalities"]),
                        status=TaskStatus.IN_PROGRESS,
                        created_at=row["created_at"],
                    )
                return None
            finally:
                conn.close()

    def complete_task(self, response: TaskResponse) -> None:
        """Mark a task as completed."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """UPDATE tasks
                       SET status = ?, result = ?, error = ?, completed_at = ?
                       WHERE task_id = ?""",
                    (response.status.value, json.dumps(response.result),
                     response.error, response.completed_at,
                     response.task_id),
                )
                conn.commit()
            finally:
                conn.close()

        self.publish(response.to_message())

    def get_task_status(self, task_id: str) -> dict | None:
        """Query the status of a task."""
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row:
                    result = dict(row)
                    for field in ("toolsets", "modalities", "result"):
                        if isinstance(result.get(field), str):
                            try:
                                result[field] = json.loads(result[field])
                            except (json.JSONDecodeError, TypeError):
                                pass
                    return result
                return None
            finally:
                conn.close()

    # ── Callbacks / subscriptions ────────────────────────────────────

    def subscribe(self, event: str, callback: Callable[[Message], None]) -> None:
        """Subscribe a callback to a message type.

        event can be a MessageType or a pattern (e.g. 'task_*').
        """
        event_key = event.value if isinstance(event, MessageType) else str(event)
        if event_key not in self._callbacks:
            self._callbacks[event_key] = []
        self._callbacks[event_key].append(callback)

    def _notify(self, msg: Message) -> None:
        """Notify all callbacks subscribed to the message type."""
        type_key = msg.type.value if hasattr(msg.type, 'value') else str(msg.type)
        for key, callbacks in self._callbacks.items():
            if key == type_key or key == "*":
                for cb in callbacks:
                    try:
                        cb(msg)
                    except Exception:
                        pass

    # ── Statistics ────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return bus statistics."""
        with self._lock:
            conn = self._get_conn()
            try:
                msg_count = conn.execute(
                    "SELECT COUNT(*) as c FROM messages"
                ).fetchone()["c"]
                agent_count = conn.execute(
                    "SELECT COUNT(*) as c FROM agent_cards WHERE is_active = 1"
                ).fetchone()["c"]
                pending_tasks = conn.execute(
                    "SELECT COUNT(*) as c FROM tasks WHERE status = 'pending'"
                ).fetchone()["c"]
                return {
                    "total_messages": msg_count,
                    "active_agents": agent_count,
                    "pending_tasks": pending_tasks,
                }
            finally:
                conn.close()
