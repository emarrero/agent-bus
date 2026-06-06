"""AgentBus Server — Central HTTP server for the agent network.

Agents connect using a shared token. The token defines the private
channel: agents with the same token form a private network and can
communicate with each other.

Architecture:
  ┌──────────────┐
  │ AgentBus     │  ← HTTP server (this file)
  │ Server       │
  └──────┬───────┘
     ┌───┴───┐
  ┌──┴──┐ ┌──┴──┐
  │Agt A│ │Agt B│  ← Agents with token "abc" form a private network
  └─────┘ └─────┘

  ┌──┴──┐
  │Agt C│           ← Agent with token "xyz" — separate network, cannot see A/B
  └─────┘

Usage:
  python3 server.py                   # Port 9876
  python3 server.py --port 8888       # Custom port
  python3 server.py --token public    # Default public token
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse, parse_qs


# ── In-memory database (per token) ──────────────────────────────────

class TokenNetwork:
    """A private network identified by a token.

    Each token has its own:
    - Agent registry
    - Message queue
    - Task queue
    """

    def __init__(self, token: str):
        self.token = token
        self.agents: dict[str, dict] = {}       # agent_id → AgentCard dict
        self.messages: list[dict] = []           # bus messages
        self.tasks: dict[str, dict] = {}         # task_id → task dict
        self.created_at = datetime.utcnow().isoformat() + "Z"

    def add_agent(self, agent_id: str, card: dict) -> None:
        card["agent_id"] = agent_id  # always present regardless of registration method
        card["last_seen"] = datetime.utcnow().isoformat() + "Z"
        self.agents[agent_id] = card

    def remove_agent(self, agent_id: str) -> None:
        self.agents.pop(agent_id, None)

    def get_agents(self) -> list[dict]:
        return list(self.agents.values())

    def add_message(self, msg: dict) -> dict:
        if "id" not in msg:
            msg["id"] = str(uuid.uuid4())
        if "timestamp" not in msg:
            msg["timestamp"] = datetime.utcnow().isoformat() + "Z"
        self.messages.append(msg)
        # Cap at 1000 messages in memory
        if len(self.messages) > 1000:
            self.messages = self.messages[-500:]
        return msg

    def get_messages(self, agent_id: str | None = None, since: str | None = None, limit: int = 50) -> list[dict]:
        msgs = self.messages
        if agent_id:
            msgs = [m for m in msgs if m.get("target") in ("", agent_id) or m.get("source") == agent_id]
        if since:
            msgs = [m for m in msgs if m.get("timestamp", "") > since]
        return msgs[-limit:]

    def add_task(self, task: dict) -> dict:
        if "task_id" not in task:
            task["task_id"] = str(uuid.uuid4())
        if "created_at" not in task:
            task["created_at"] = datetime.utcnow().isoformat() + "Z"
        task["status"] = task.get("status", "pending")
        self.tasks[task["task_id"]] = task
        return task

    def claim_task(self, agent_id: str) -> dict | None:
        for tid, task in self.tasks.items():
            if task["status"] == "pending" and task.get("target_agent", "") == agent_id:
                task["status"] = "in_progress"
                return task
        return None

    def complete_task(self, task_id: str, result: Any, error: str | None = None) -> dict | None:
        task = self.tasks.get(task_id)
        if task:
            task["status"] = "completed" if error is None else "failed"
            task["result"] = result
            task["error"] = error
            task["completed_at"] = datetime.utcnow().isoformat() + "Z"
        return task

    def stats(self) -> dict:
        return {
            "token": self.token,
            "agents": len(self.agents),
            "messages": len(self.messages),
            "tasks": len(self.tasks),
            "pending_tasks": sum(1 for t in self.tasks.values() if t.get("status") == "pending"),
            "created_at": self.created_at,
        }


class AgentBusServer:
    """Central server for the agent network.

    Maintains multiple private networks, each identified by a token.
    """

    def __init__(self, public_token: str | None = None):
        self.networks: dict[str, TokenNetwork] = {}
        self.public_token = public_token or ""
        self.start_time = time.time()

    def _get_network(self, token: str) -> TokenNetwork:
        """Get or create the network for a token."""
        if token not in self.networks:
            self.networks[token] = TokenNetwork(token)
        return self.networks[token]

    def handle_register(self, token: str, agent_id: str, card: dict) -> dict:
        """Register an agent on the token's network."""
        network = self._get_network(token)
        network.add_agent(agent_id, card)

        network.add_message({
            "type": "agent_announce",
            "source": agent_id,
            "target": "",
            "payload": {"name": card.get("name", agent_id), "skills": card.get("skills", [])},
        })

        return {"status": "ok", "agent_id": agent_id, "network": token, "agents": len(network.agents)}

    def handle_unregister(self, token: str, agent_id: str) -> dict:
        network = self.networks.get(token)
        if network:
            network.remove_agent(agent_id)
        return {"status": "ok", "agent_id": agent_id}

    def handle_list_agents(self, token: str) -> dict:
        network = self._get_network(token)
        return {"status": "ok", "agents": network.get_agents(), "count": len(network.agents)}

    def handle_send_message(self, token: str, msg: dict) -> dict:
        network = self._get_network(token)
        stored = network.add_message(msg)
        return {"status": "ok", "message_id": stored["id"]}

    def handle_get_messages(self, token: str, agent_id: str | None = None, since: str | None = None, limit: int = 50) -> dict:
        network = self._get_network(token)
        msgs = network.get_messages(agent_id=agent_id, since=since, limit=limit)
        return {"status": "ok", "messages": msgs, "count": len(msgs)}

    def handle_submit_task(self, token: str, task: dict) -> dict:
        network = self._get_network(token)
        stored = network.add_task(task)
        network.add_message({
            "type": "task_request",
            "source": task.get("source_agent", ""),
            "target": task.get("target_agent", ""),
            "payload": task,
            "metadata": {"task_id": stored["task_id"]},
        })
        return {"status": "ok", "task_id": stored["task_id"]}

    def handle_claim_task(self, token: str, agent_id: str) -> dict:
        network = self.networks.get(token)
        if not network:
            return {"status": "ok", "task": None}
        task = network.claim_task(agent_id)
        return {"status": "ok", "task": task}

    def handle_complete_task(self, token: str, task_id: str, result: Any, error: str | None = None) -> dict:
        network = self.networks.get(token)
        if not network:
            return {"status": "error", "message": "Network not found"}
        task = network.complete_task(task_id, result, error)
        if task:
            network.add_message({
                "type": "task_response",
                "source": task.get("target_agent", ""),
                "target": task.get("source_agent", ""),
                "payload": task,
                "metadata": {"task_id": task_id},
            })
            return {"status": "ok", "task": task}
        return {"status": "error", "message": "Task not found"}

    def handle_stats(self, token: str) -> dict:
        network = self._get_network(token)
        stats = network.stats()
        stats["server_uptime"] = int(time.time() - self.start_time)
        return {"status": "ok", "stats": stats}

    def handle_global_stats(self) -> dict:
        return {
            "status": "ok",
            "networks": len(self.networks),
            "total_agents": sum(len(n.agents) for n in self.networks.values()),
            "total_messages": sum(len(n.messages) for n in self.networks.values()),
            "server_uptime": int(time.time() - self.start_time),
        }


# ── HTTP Handler ─────────────────────────────────────────────────────

class AgentBusHTTPHandler(BaseHTTPRequestHandler):
    """Handles HTTP routes for the AgentBus API."""

    bus_server: AgentBusServer = None  # type: ignore

    def log_message(self, format: str, *args: Any) -> None:
        """Silent log — only real errors."""
        if args:
            try:
                code = int(args[-1])
                if code >= 400:
                    super().log_message(format, *args)
            except (ValueError, TypeError):
                pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _get_token(self) -> str:
        """Extract token from X-Agent-Token header or token query param."""
        token = self.headers.get("X-Agent-Token", "")
        if not token:
            qs = parse_qs(urlparse(self.path).query)
            token = qs.get("token", [""])[0]
        return token

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Token")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        token = self._get_token()

        try:
            if path == "/health":
                self._send_json({"status": "ok", "uptime": int(time.time() - self.bus_server.start_time)})

            elif path == "/stats" and not token:
                self._send_json(self.bus_server.handle_global_stats())

            elif path == "/stats" and token:
                self._send_json(self.bus_server.handle_stats(token))

            elif path == "/agents" and token:
                self._send_json(self.bus_server.handle_list_agents(token))

            elif path == "/messages" and token:
                agent_id = qs.get("agent_id", [None])[0]
                since = qs.get("since", [None])[0]
                limit_str = qs.get("limit", ["50"])[0]
                limit = min(int(limit_str), 200)
                self._send_json(self.bus_server.handle_get_messages(token, agent_id, since, limit))

            elif path == "/task" and token:
                agent_id = qs.get("agent_id", [None])[0]
                if agent_id:
                    self._send_json(self.bus_server.handle_claim_task(token, agent_id))
                else:
                    task_id = qs.get("task_id", [None])[0]
                    if task_id:
                        network = self.bus_server.networks.get(token)
                        task = network.tasks.get(task_id) if network else None
                        self._send_json({"status": "ok", "task": task})
                    else:
                        self._send_json({"status": "error", "message": "agent_id or task_id required"}, 400)

            else:
                self._send_json({"status": "error", "message": f"Not found: {path}"}, 404)

        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()
        token = self._get_token()
        if not token:
            token = body.get("token", "")

        try:
            if path == "/register":
                agent_id = body.get("agent_id", "")
                card = body.get("card", {})
                tok = body.get("token", token)
                if not agent_id:
                    self._send_json({"status": "error", "message": "agent_id required"}, 400)
                elif not tok:
                    self._send_json({"status": "error", "message": "token required"}, 400)
                else:
                    self._send_json(self.bus_server.handle_register(tok, agent_id, card))

            elif path == "/unregister":
                agent_id = body.get("agent_id", "")
                tok = body.get("token", token)
                self._send_json(self.bus_server.handle_unregister(tok, agent_id))

            elif path == "/message":
                msg = body.get("message", body)
                if isinstance(msg, dict):
                    msg.setdefault("source", body.get("agent_id", ""))
                tok = body.get("token", token)
                self._send_json(self.bus_server.handle_send_message(tok, msg))

            elif path == "/task":
                task = body.get("task", body)
                tok = body.get("token", token)
                self._send_json(self.bus_server.handle_submit_task(tok, task))

            elif path == "/task/complete":
                task_id = body.get("task_id", "")
                result = body.get("result")
                error = body.get("error")
                tok = body.get("token", token)
                self._send_json(self.bus_server.handle_complete_task(tok, task_id, result, error))

            else:
                self._send_json({"status": "error", "message": f"Not found: {path}"}, 404)

        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, 500)


def create_server(host: str = "0.0.0.0", port: int = 9876, public_token: str = "") -> HTTPServer:
    """Create and return an AgentBus server instance."""
    server = AgentBusServer(public_token=public_token)
    handler = type("Handler", (AgentBusHTTPHandler,), {"bus_server": server})
    httpd = HTTPServer((host, port), handler)
    return httpd


def main():
    import argparse

    parser = argparse.ArgumentParser(description="AgentBus Server — Multi-agent communication network")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9876, help="Port (default: 9876)")
    parser.add_argument("--token", default="", help="Default public token (optional)")
    args = parser.parse_args()

    httpd = create_server(host=args.host, port=args.port, public_token=args.token)

    print(f"""
╔══════════════════════════════════════════════╗
║        AgentBus Server — Agent Network       ║
╠══════════════════════════════════════════════╣
║  HTTP:   http://{args.host}:{args.port}               ║
║  Token:  {'{:<34}'.format(args.token or '(public — anyone can connect)')}║
║                                              ║
║  Connect an agent:                           ║
║    curl -X POST http://{args.host}:{args.port}/register \\║
║      -H 'X-Agent-Token: mytoken' \\           ║
║      -d '{{"agent_id":"agent1","card":{{}}}}'   ║
╚══════════════════════════════════════════════╝
    """)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nAgentBus Server stopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
