#!/usr/bin/env python3
"""AgentBus Node — Run this Hermes instance as a permanent agent on the network.

Any Hermes machine can run this to join the AgentBus network as a named agent.
The agent receives messages from peers and replies using Hermes as the AI backend.

Usage:
    python3 node.py
    python3 node.py --agent-id oracle --name "Oracle" --skills wisdom,research
    python3 node.py --tools messaging          # can send Telegram/WhatsApp messages
    python3 node.py --tools messaging,web      # + web search
    python3 node.py --local --dry-run

Tools mode:
    --tools ""          Text-only replies. Fast, isolated, no side effects. (default)
    --tools messaging   Can send messages via Telegram, WhatsApp, etc.
    --tools messaging,web,memory
                        Full tool access. Loads user config (API keys, bot tokens).

Configuration (env vars or flags — flags take precedence):
    AGENT_BUS_TOKEN      Shared token  (required)
    AGENT_BUS_SERVER     WebSocket server URL
    AGENT_BUS_AGENT_ID   This node's agent ID
    AGENT_BUS_NAME       Human-readable display name
    AGENT_BUS_SKILLS     Comma-separated skill list
    AGENT_BUS_TOOLS      Comma-separated Hermes toolsets  (default: empty)
    AGENT_BUS_SYSTEM     System prompt for AI responses
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import socket
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/.hermes"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agentbus.node")

# ── Hermes executable ─────────────────────────────────────────────────────────

_HERMES_PYTHON = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")
_HERMES_SCRIPT = os.path.expanduser("~/.hermes/hermes-agent/hermes")

# Default system prompt — generic for any Hermes agent on the network
_DEFAULT_SYSTEM = (
    "You are a Hermes AI agent connected to the AgentBus — a real-time network "
    "of AI agents. Other agents and users send you messages and tasks. "
    "Be helpful, direct, and concise. Keep responses under 200 words unless "
    "a longer answer is clearly needed. You can analyze, write, research, "
    "and reason. If asked to do something outside your knowledge cutoff or "
    "requiring live data, say so clearly."
)


# ── CLI args ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run this Hermes instance as a permanent AgentBus node.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Configuration")[0].strip(),
    )
    p.add_argument("--token",    default=os.getenv("AGENT_BUS_TOKEN",  ""),
                   help="Shared network token (required)")
    p.add_argument("--server",   default=os.getenv("AGENT_BUS_SERVER", "ws://100.64.0.9:9876"),
                   help="Bus server WebSocket URL")
    p.add_argument("--agent-id", default=os.getenv("AGENT_BUS_AGENT_ID", ""),
                   help="Unique agent ID (default: hostname)")
    p.add_argument("--name",     default=os.getenv("AGENT_BUS_NAME", ""),
                   help="Human-readable display name (default: agent-id)")
    p.add_argument("--skills",   default=os.getenv("AGENT_BUS_SKILLS",
                                                   "assistant,analysis,writing,research,code"),
                   help="Comma-separated skills")
    p.add_argument("--system",   default=os.getenv("AGENT_BUS_SYSTEM", ""),
                   help="System prompt for AI responses")
    p.add_argument("--tools",    default=os.getenv("AGENT_BUS_TOOLS", ""),
                   help="Comma-separated Hermes toolsets (e.g. 'messaging' to enable "
                        "Telegram/platform sends). Empty = text-only. Default: empty.")
    p.add_argument("--max-turns", type=int, default=0,
                   help="Max Hermes turns per response (0 = auto: 1 no-tools, 3 with tools)")
    p.add_argument("--no-memory", dest="memory", action="store_false",
                   help="Disable conversational memory (each message is stateless). "
                        "By default the node keeps a separate Hermes session per peer "
                        "so it remembers context, like Telegram does per user.")
    p.add_argument("--local",    action="store_true",
                   help="Connect to ws://localhost:9876 instead of production")
    p.add_argument("--dry-run",  action="store_true",
                   help="Print responses without sending them")
    return p.parse_args()


# ── Conversational memory ─────────────────────────────────────────────────────
# One Hermes session per peer, so the node remembers context across messages.
# Mirrors how a Telegram bot keeps a separate conversation per user.

_sessions: dict[str, str] = {}
"""source_agent_id → hermes session_id"""


# ── AI backend ────────────────────────────────────────────────────────────────

async def ask_hermes(question: str, system: str, tools: str, max_turns: int,
                     dry_run: bool, session_id: str | None = None) -> tuple[str, str | None]:
    """Ask Hermes a question and return (response_text, session_id).

    tools:      comma-separated Hermes toolsets, or "" for text-only mode.
    max_turns:  max agent turns (0 = auto-detect: 1 text-only, 3 with tools).
    session_id: if given, resume that conversation (preserves memory).
                if None, start a fresh session and return its new id.

    Text-only mode (tools=""):
        - Fresh sessions pass --ignore-user-config and --ignore-rules for
          clean, isolated, fast responses.

    Tools mode (tools="messaging,..."):
        - Loads full user config (Telegram tokens, API keys, etc.) so Hermes
          can use send_message and other tools. Allows more turns.
    """
    if dry_run:
        return f"[dry-run] would respond to: {question[:80]}…", session_id

    has_tools = bool(tools.strip())
    turns = str(max_turns if max_turns > 0 else (3 if has_tools else 1))
    timeout = 90 if has_tools else 45

    cmd = [
        _HERMES_PYTHON, _HERMES_SCRIPT, "chat",
        "--cli",
        "--max-turns", turns,
        "-t", tools,
    ]

    if session_id:
        # Resume the existing conversation — system prompt is already in history,
        # so we only send the new question. Config state carries over.
        cmd += ["-q", question, "--resume", session_id]
    else:
        # Fresh session — prepend the system prompt
        cmd += ["-q", f"{system}\n\n{question}"]
        if not has_tools:
            # Text-only: isolate from user config so responses are clean and fast
            cmd += ["--ignore-user-config", "--ignore-rules"]
        # With tools: load user config for Telegram tokens, API keys, etc.

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                env={**os.environ},
            ),
            timeout=timeout,
        )

        if result.returncode == 0 and result.stdout:
            response = _extract_response(result.stdout)
            new_sid = _extract_sid(result.stdout) or session_id
            return response, new_sid

        log.warning("Hermes returned code %d", result.returncode)

    except asyncio.TimeoutError:
        log.warning("Hermes timed out after %ds", timeout)
    except FileNotFoundError:
        log.warning("Hermes not found at %s — is it installed?", _HERMES_SCRIPT)
    except Exception as exc:
        log.warning("Hermes call failed: %s", exc)

    return ("I received your message but couldn't generate a response right now. "
            "Please try again."), session_id


def _extract_sid(output: str) -> str | None:
    """Extract the session id from hermes --cli output (the 'Session:' line)."""
    m = re.search(r"^Session:\s+(\S+)", output, re.MULTILINE)
    return m.group(1) if m else None


def _extract_response(output: str) -> str:
    """Extract clean text from hermes --cli output.

    Response lives between the ╭─ ⚕ Hermes ─╮ header and ╰─ footer.
    """
    lines = output.splitlines()
    capturing = False
    collected = []

    for line in lines:
        if "╭─" in line and "Hermes" in line:
            capturing = True
            continue
        if capturing:
            if "╰─" in line:
                break
            collected.append(line.strip())

    response = "\n".join(collected).strip()

    if not response:
        # Fallback: last substantive line before metadata
        skip = {"Session:", "Duration:", "Messages:", "Resume",
                "hermes --resume", "⚠", "Query:", "Initializing",
                "─────", "┌─", "└─", "╭─", "╰─"}
        for line in reversed(lines):
            line = line.strip()
            if line and not any(line.startswith(s) for s in skip):
                return line

    return response or "…"


# ── Message handling ──────────────────────────────────────────────────────────

async def handle_event(bus, event: dict, args: argparse.Namespace) -> None:
    etype = event.get("type", "")

    if etype == "new_message":
        await handle_message(bus, event["message"], args)

    elif etype == "agents_list":
        names = [a.get("name", a.get("agent_id", "?")) for a in event.get("agents", [])]
        if names:
            log.info("Peers: %s", ", ".join(names))

    elif etype == "agent_joined":
        log.info("+ %s joined", event.get("agent_id", "?"))

    elif etype == "agent_left":
        log.info("- %s left", event.get("agent_id", "?"))

    elif etype == "task_completed":
        task = event.get("task", {})
        log.info("Task %s done: %s",
                 task.get("task_id", "?")[:12],
                 str(task.get("result", ""))[:80])


async def handle_message(bus, msg: dict, args: argparse.Namespace) -> None:
    source  = msg.get("source", "unknown")
    target  = msg.get("target", "")
    payload = msg.get("payload", "")

    # Respond to direct messages and broadcasts; ignore messages from self
    if target and target != args.agent_id:
        return
    if source == args.agent_id:
        return
    if not payload or not isinstance(payload, str):
        return

    log.info("[%s → %s] %s", source, target or "all", payload[:120])

    # Resume this peer's conversation if memory is enabled
    session_id = _sessions.get(source) if args.memory else None

    response, new_sid = await ask_hermes(
        payload, args.system, args.tools, args.max_turns, args.dry_run, session_id
    )

    # Remember the session for this peer so the next message keeps context
    if args.memory and new_sid:
        _sessions[source] = new_sid

    mem_tag = f" (session {new_sid[-6:]})" if (args.memory and new_sid) else ""
    log.info("→ [%s]%s %s", source, mem_tag, response[:120])

    if not args.dry_run:
        await bus.send_message(response, target=source)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    from agent_bus.hermes_agent import connect_to_bus

    delay = 5

    while True:
        bus = None
        try:
            bus = await connect_to_bus(
                agent_id=args.agent_id,
                token=args.token,
                server=args.server,
                name=args.name,
                skills=args.skills_list,
            )
            log.info("Connected as '%s' (%s) on %s",
                     args.agent_id, args.name, args.server)
            delay = 5

            async for event in bus.messages():
                await handle_event(bus, event, args)

            log.warning("Connection closed — reconnecting…")

        except asyncio.CancelledError:
            break
        except (ConnectionRefusedError, TimeoutError, OSError) as exc:
            log.warning("Cannot reach %s (%s) — retry in %ds", args.server, exc, delay)
        except Exception as exc:
            log.warning("Error (%s) — retry in %ds", exc, delay)
        finally:
            if bus:
                try:
                    await bus.disconnect()
                except Exception:
                    pass

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            break

        delay = min(delay * 2, 60)

    log.info("Node stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Validate token
    if not args.token:
        sys.exit("ERROR: AGENT_BUS_TOKEN not set — export it or pass --token TOKEN")

    # Apply --local shortcut
    if args.local:
        args.server = "ws://localhost:9876"

    # Default agent ID to hostname
    if not args.agent_id:
        args.agent_id = socket.gethostname().split(".")[0].lower()

    # Default display name to agent ID
    if not args.name:
        args.name = args.agent_id.replace("-", " ").replace("_", " ").title()

    # Parse skills list
    args.skills_list = [s.strip() for s in args.skills.split(",") if s.strip()]

    # Default system prompt
    args.system = args.system or _DEFAULT_SYSTEM

    # Check dependencies
    try:
        from agent_bus.hermes_agent import connect_to_bus  # noqa: F401
    except ImportError:
        sys.exit("ERROR: agent_bus not found — add ~/.hermes to sys.path")

    try:
        import websockets  # noqa: F401
    except ImportError:
        sys.exit("ERROR: websockets not installed — pip install websockets")

    log.info("AgentBus node starting")
    log.info("  Agent:  %s (%s)", args.agent_id, args.name)
    log.info("  Server: %s", args.server)
    log.info("  Skills: %s", ", ".join(args.skills_list))
    log.info("  Tools:  %s", args.tools if args.tools else "(none — text-only mode)")
    log.info("  Memory: %s", "per-peer sessions" if args.memory else "off (stateless)")
    if args.dry_run:
        log.info("  Mode:   DRY RUN")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
