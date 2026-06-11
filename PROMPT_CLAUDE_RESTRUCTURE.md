## Task: Restructure AgentBus Project

Restructure the `emarrero/agent-bus` repository into a clean, modular layout with clear separation between server, client, plugin, and documentation.

### Current Structure (messy)

```
agent-bus/
├── adapter.py          ← Hermes gateway plugin (client)
├── p2p.py              ← P2P direct connections
├── server_ws.py        ← WebSocket + HTTP server
├── server.py           ← HTTP-only server (legacy)
├── hermes_agent.py     ← Async WS client library
├── client.py           ← HTTP polling client
├── node.py             ← Standalone agent runner
├── protocol.py         ← Data structures
├── bus.py              ← SQLite message bus
├── router.py           ← Message routing
├── __init__.py          ← Package init
├── __main__.py          ← CLI entry
├── cli.py              ← CLI commands
├── install.sh          ← Client installer
├── install-server.sh   ← Server installer
├── plugin.yaml         ← Hermes plugin manifest
├── setup.py            ← Package setup
├── README.md           ← Bilingual docs
├── CLAUDE.md           ← AI instructions
├── P2P_ARCHITECTURE.md ← P2P docs
├── P2P_TROUBLESHOOTING.md ← Debug guide
├── PROMPT_CLAUDE_REVIEW.md ← Code review prompt
├── AGENTBUS_COMPLETE.md ← Full docs (Spanish)
├── AGENTBUS_COMPLETE_EN.md ← Full docs (English)
├── ADAPTER.md          ← Adapter docs
├── PROTOCOL.md         ← Protocol spec
├── ORACLE_UPDATE.md    ← Update guide
├── check_bus.py, check_inbox.py, demo.py, demo_3agentes.py,
├── demo_network.py, escucha_global.py, listener.sh,
├── multimodal.py, set_oracle_key.py, start_listener.sh,
├── test_token_rolling.py, inbox.jsonl, inbox_leido.txt,
├── ultimo_check.txt, pyproject.toml, .gitignore
└── scripts/, skills/, .claude/
```

### Target Structure

```
agent-bus/
├── README.md              ← Single, clean English README (all instructions)
│
├── server/
│   ├── __init__.py
│   ├── server_ws.py       ← WebSocket + HTTP server (main)
│   └── protocol.py        ← Shared data structures (moved here)
│
├── client/
│   ├── __init__.py
│   ├── hermes_agent.py    ← Async WS client library
│   └── p2p.py             ← P2P direct connections module
│
├── plugin/
│   ├── __init__.py        ← Package init (version, exports)
│   ├── adapter.py         ← Hermes gateway adapter
│   ├── p2p.py             ← P2P manager (symlinked or imported from client/)
│   └── plugin.yaml        ← Hermes plugin manifest
│
├── docs/
│   ├── P2P_ARCHITECTURE.md
│   ├── P2P_TROUBLESHOOTING.md
│   └── PROTOCOL.md
│
├── scripts/
│   ├── install.sh         ← Combined install script (server + client)
│   └── install-server.sh  ← Server-only install
│
├── pyproject.toml         ← Package config
├── setup.py               ← Setup (keep for compatibility)
├── .gitignore
└── CLAUDE.md              ← Updated AI instructions
```

### What to Do

1. **Create directories** `server/`, `client/`, `plugin/`, `docs/scripts/`

2. **Move files** to their new locations

3. **Fix all imports** across all files so the package still works after the move

4. **Rewrite README.md** — single English file with:
   - Quick start (install server, install plugin on Hermes)
   - Architecture overview (server + client + P2P)
   - P2P configuration guide
   - Troubleshooting checklist
   - All env vars reference
   - All commands reference
   - Links to docs/

5. **Remove all legacy files** that are no longer needed:
   - demo.py, demo_3agentes.py, demo_network.py
   - escucha_global.py, escuchar_global.sh, listener.sh, start_listener.sh
   - check_bus.py, check_inbox.py
   - test_token_rolling.py
   - set_oracle_key.py
   - multimodal.py
   - inbox.jsonl, inbox_leido.txt, ultimo_check.txt
   - ADAPTER.md, ORACLE_UPDATE.md (content merged into README)
   - AGENTBUS_COMPLETE.md, AGENTBUS_COMPLETE_EN.md (content merged into README)
   - PROMPT_CLAUDE_REVIEW.md (moved to docs/)
   - pyproject.toml, router.py, bus.py, cli.py, __main__.py

6. **Update CLAUDE.md** to reflect new structure

7. **Update P2P_ARCHITECTURE.md** and **P2P_TROUBLESHOOTING.md** with the final protocol v2 details (HMAC auth, keepalive, dynamic port, import zero-config)

8. **Bump version** to 0.8.0 in `plugin/__init__.py`

9. **Ensure all code compiles** after the move — test imports for server, client, and plugin separately

### Outcome

A clean, professional repository where:
- `server/` can be deployed independently
- `client/` can be installed as a Python library
- `plugin/` is the Hermes gateway plugin
- `README.md` is the single source of truth for setup and usage
- All legacy clutter is gone
