"""agent-bus — CLI to connect agents to the network.

USAGE (for the agent):
    export AGENT_BUS_TOKEN="mynetwork"
    export AGENT_BUS_SERVER="ws://localhost:9876"

    # Register on the network
    agent-bus register --name "Researcher" --skills research,analysis

    # Send a message
    agent-bus send -t writer -m "Hello, can you research X?"

    # Read new messages
    agent-bus read

    # Delegate a task
    agent-bus task -t writer -g "Research what transformers are"

    # Listen for one message (connect + wait, useful in a loop)
    agent-bus listen

    # View connected agents
    agent-bus peers

The agent ONLY executes commands. Never modifies code.

For help:
    agent-bus --help
    agent-bus <command> --help
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.error


# ── Config from environment ──────────────────────────────────────────

def get_token() -> str:
    token = os.environ.get("AGENT_BUS_TOKEN", "")
    if not token:
        print("❌ AGENT_BUS_TOKEN not set. Export the variable:", file=sys.stderr)
        print("   export AGENT_BUS_TOKEN='mynetwork'", file=sys.stderr)
        sys.exit(1)
    return token


def get_server_url() -> str:
    return os.environ.get("AGENT_BUS_SERVER", "ws://localhost:9876")


def get_http_url() -> str:
    ws = get_server_url()
    if "9876" in ws:
        return ws.replace("ws://", "http://").replace(":9876", ":9877")
    return ws.replace("ws://", "http://")


def get_agent_id() -> str:
    """Get or generate the agent_id."""
    aid = os.environ.get("AGENT_BUS_AGENT_ID", "")
    if not aid:
        import socket
        aid = socket.gethostname()
    return aid


# ── HTTP helpers ─────────────────────────────────────────────────────

def http_get(path: str, params: dict | None = None) -> dict:
    import urllib.parse
    url = f"{get_http_url()}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url += f"?{qs}"
    req = urllib.request.Request(url, headers={"X-Agent-Token": get_token()})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"status": "error", "message": e.read().decode()[:200]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def http_post(path: str, body: dict) -> dict:
    import urllib.parse
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{get_http_url()}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Agent-Token": get_token(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"status": "error", "message": e.read().decode()[:200]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Commands ─────────────────────────────────────────────────────────

def cmd_register(args: argparse.Namespace) -> None:
    """Register this agent on the network."""
    agent_id = args.agent_id or get_agent_id()
    name = args.name or agent_id
    skills = args.skills.split(",") if args.skills else []
    modalities = args.modalities or ["text"]

    card = {
        "agent_id": agent_id,
        "name": name,
        "description": args.description or f"Agent {name}",
        "skills": skills,
        "modalities": modalities,
    }

    result = http_post("/register", {
        "agent_id": agent_id,
        "card": card,
        "token": get_token(),
    })

    if result.get("status") == "ok":
        print(f"✅ {name} registered on network (token: {get_token()[:8]}...)")
        print(f"   Agents on network: {result.get('agents', '?')}")
        os.environ["AGENT_BUS_AGENT_ID"] = agent_id
    else:
        print(f"❌ Error: {result.get('message', 'unknown')}")


def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to another agent."""
    agent_id = args.agent_id or get_agent_id()
    result = http_post("/message", {
        "message": {
            "source": agent_id,
            "target": args.target or "",
            "payload": args.message,
            "type": "text",
        },
        "token": get_token(),
    })
    if result.get("status") == "ok":
        print(f"📤 Message sent to '{args.target or 'broadcast'}'")
    else:
        print(f"❌ Error: {result.get('message', 'unknown')}")


def cmd_read(args: argparse.Namespace) -> None:
    """Read messages directed to this agent."""
    agent_id = args.agent_id or get_agent_id()
    params = {"token": get_token(), "agent_id": agent_id, "limit": str(args.limit)}
    result = http_get("/messages", params=params)
    if result.get("status") != "ok":
        print(f"❌ Error: {result.get('message', 'unknown')}")
        return

    msgs = result.get("messages", [])
    if not msgs:
        print("📭 No new messages")
        return

    for m in msgs:
        mtype = m.get("type", "?")
        source = m.get("source", "?")
        payload = m.get("payload", "")
        if isinstance(payload, dict):
            payload = json.dumps(payload, ensure_ascii=False)[:100]
        timestamp = m.get("timestamp", "")[11:19] if m.get("timestamp") else ""
        print(f"[{timestamp}] {source} ({mtype}): {payload}")


def cmd_task(args: argparse.Namespace) -> None:
    """Delegate a task to another agent."""
    agent_id = args.agent_id or get_agent_id()
    result = http_post("/task", {
        "task": {
            "source_agent": agent_id,
            "target_agent": args.target or "",
            "goal": args.goal,
            "context": args.context or "",
            "toolsets": args.toolsets.split(",") if args.toolsets else [],
            "status": "pending",
        },
        "token": get_token(),
    })
    if result.get("status") == "ok":
        tid = result.get("task_id", "?")[:12]
        print(f"📤 Task delegated to '{args.target or '?'}' (id: {tid}...)")
    else:
        print(f"❌ Error: {result.get('message', 'unknown')}")


def cmd_claim(args: argparse.Namespace) -> None:
    """Claim the next pending task."""
    agent_id = args.agent_id or get_agent_id()
    params = {"token": get_token(), "agent_id": agent_id}
    result = http_get("/task", params=params)
    if result.get("status") != "ok":
        print(f"❌ Error: {result.get('message', 'unknown')}")
        return

    task = result.get("task")
    if not task:
        print("📭 No pending tasks")
        return

    tid = task.get("task_id", "?")[:12]
    goal = task.get("goal", "?")
    source = task.get("source_agent", "?")
    print(f"📥 Task claimed:")
    print(f"   ID:      {tid}...")
    print(f"   From:    {source}")
    print(f"   Goal:    {goal}")
    print(f"   Context: {task.get('context', '')[:100]}")


def cmd_complete(args: argparse.Namespace) -> None:
    """Complete a task."""
    result = http_post("/task/complete", {
        "task_id": args.task_id,
        "result": args.result,
        "error": args.error,
        "token": get_token(),
    })
    if result.get("status") == "ok":
        print(f"✅ Task {args.task_id[:12]}... completed")
    else:
        print(f"❌ Error: {result.get('message', 'unknown')}")


def cmd_peers(args: argparse.Namespace) -> None:
    """List agents connected to the same network."""
    params = {"token": get_token()}
    result = http_get("/agents", params=params)
    if result.get("status") != "ok":
        print(f"❌ Error: {result.get('message', 'unknown')}")
        return

    agents = result.get("agents", [])
    if not agents:
        print("📭 No agents on this network")
        return

    print(f"👥 Agents on network ({len(agents)}):")
    for a in agents:
        name = a.get("name", a.get("agent_id", "?"))
        skills = ", ".join(a.get("skills", []))
        print(f"   • {name}  [{skills}]")


def cmd_listen(args: argparse.Namespace) -> None:
    """Listen for one message via WebSocket (blocking wait).

    Useful for agent loops:
        while true; do
            message=$(agent-bus listen --timeout 30)
            echo "Received: $message"
            # process...
        done
    """
    try:
        import websockets
    except ImportError:
        print("❌ 'websockets' required: pip install websockets", file=sys.stderr)
        sys.exit(1)

    agent_id = args.agent_id or get_agent_id()
    token = get_token()
    server = get_server_url()

    async def _listen():
        try:
            async with websockets.connect(server) as ws:
                await ws.send(json.dumps({
                    "type": "register",
                    "agent_id": agent_id,
                    "token": token,
                    "card": {"name": agent_id, "skills": [], "modalities": ["text"]},
                }))
                await ws.recv()  # register response
                await ws.recv()  # agents_list

                raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
                data = json.loads(raw)
                if data.get("type") == "new_message":
                    msg = data.get("message", {})
                    payload = msg.get("payload", "")
                    source = msg.get("source", "?")
                    print(f"{source}: {payload}")
                elif data.get("type") == "task_completed":
                    task = data.get("task", {})
                    print(f"TASK_DONE|{task.get('task_id','')[:12]}|{str(task.get('result',''))[:200]}")
                else:
                    print(json.dumps(data, ensure_ascii=False))
        except asyncio.TimeoutError:
            print("TIMEOUT")
            sys.exit(2)
        except ConnectionRefusedError:
            print(f"❌ Cannot connect to {server}. Is the server running?", file=sys.stderr)
            sys.exit(1)

    asyncio.run(_listen())


def cmd_health(args: argparse.Namespace) -> None:
    """Check that the server is running."""
    result = http_get("/health")
    if result.get("status") == "ok":
        print(f"✅ Server OK (uptime: {result.get('uptime', '?')}s)")
    else:
        print("❌ Server not responding")
        sys.exit(1)


def cmd_stats(args: argparse.Namespace) -> None:
    """Show server statistics."""
    result = http_get("/stats")
    if result.get("status") == "ok":
        print("📊 AgentBus Stats:")
        print(f"   Active networks:  {result.get('networks', '?')}")
        print(f"   Total agents:     {result.get('total_agents', '?')}")
        print(f"   Total messages:   {result.get('total_messages', '?')}")
        print(f"   WS connections:   {result.get('ws_connections', 'N/A')}")
        print(f"   Uptime:           {result.get('server_uptime', '?')}s")
    else:
        print(f"❌ Error: {result.get('message', 'unknown')}")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="agent-bus",
        description="Multi-agent communication network",
        epilog="""Environment variables:
  AGENT_BUS_TOKEN    Shared token (defines the private network)
  AGENT_BUS_SERVER   WebSocket server URL (default: ws://localhost:9876)
  AGENT_BUS_AGENT_ID This agent's ID (default: hostname)""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--agent-id", "-a", help="Agent ID (default: hostname)")
    parser.add_argument("--server", help="WS server (default: env AGENT_BUS_SERVER)")
    parser.add_argument("--token", help="Network token (default: env AGENT_BUS_TOKEN)")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_sub(name, help_text, with_agent=True):
        p = sub.add_parser(name, help=help_text)
        if with_agent:
            p.add_argument("--agent-id", "-a", help="Agent ID (default: hostname)")
        return p

    # register
    p = add_sub("register", "Register agent on the network")
    p.add_argument("--name", "-n", help="Agent name")
    p.add_argument("--skills", "-s", help="Comma-separated skills (e.g. research,writing)")
    p.add_argument("--description", "-d", help="Agent description")
    p.add_argument("--modalities", default="text", help="Modalities (text,audio)")

    # send
    p = add_sub("send", "Send a message")
    p.add_argument("--target", "-t", required=True, help="Target agent (or 'broadcast')")
    p.add_argument("--message", "-m", required=True, help="Message text")

    # read
    p = add_sub("read", "Read messages")
    p.add_argument("--limit", "-l", type=int, default=10, help="Max messages")

    # task
    p = add_sub("task", "Delegate a task")
    p.add_argument("--target", "-t", required=True, help="Target agent")
    p.add_argument("--goal", "-g", required=True, help="Task goal")
    p.add_argument("--context", "-c", default="", help="Additional context")
    p.add_argument("--toolsets", default="", help="Comma-separated toolsets")

    # claim
    add_sub("claim", "Claim the next pending task")

    # complete
    p = add_sub("complete", "Complete a task")
    p.add_argument("--task-id", required=True, help="Task ID")
    p.add_argument("--result", required=True, help="Task result")
    p.add_argument("--error", help="Error message (if failed)")

    # peers
    add_sub("peers", "List agents on the network", with_agent=False)

    # listen
    p = add_sub("listen", "Listen for one message (blocking)")
    p.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")

    # health
    add_sub("health", "Check server connectivity", with_agent=False)

    # stats
    add_sub("stats", "Server statistics", with_agent=False)

    args = parser.parse_args()

    if args.token:
        os.environ["AGENT_BUS_TOKEN"] = args.token
    if args.server:
        os.environ["AGENT_BUS_SERVER"] = args.server
    if args.agent_id:
        os.environ["AGENT_BUS_AGENT_ID"] = args.agent_id

    needs_token = {"register", "send", "read", "task", "claim", "complete", "peers", "listen"}
    if args.command in needs_token:
        get_token()

    commands = {
        "register": cmd_register,
        "send": cmd_send,
        "read": cmd_read,
        "task": cmd_task,
        "claim": cmd_claim,
        "complete": cmd_complete,
        "peers": cmd_peers,
        "listen": cmd_listen,
        "health": cmd_health,
        "stats": cmd_stats,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
