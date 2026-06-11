#!/usr/bin/env bash
# ============================================================
#  AgentBus — Hermes Client Installer
# ============================================================
#  Installs the AgentBus module and platform adapter into
#  Hermes so the gateway connects permanently to the central
#  AgentBus server (like Telegram — always connected).
#
#  Run this on each machine that has Hermes installed.
#  To install the central server, use: sudo bash scripts/install-server.sh
#
#  Usage:
#    bash install.sh                         # interactive
#    bash install.sh --token T --server URL  # non-interactive
#    bash install.sh --uninstall             # remove integration
#
#  Options:
#    --token TOKEN       Shared network token (required)
#    --server URL        Central bus server URL (e.g. ws://10.0.0.1:9876)
#    --agent-id ID       Hermes agent ID on the bus (default: hermes)
#    --no-sync           Skip syncing agent_bus module files
#    --uninstall         Remove the Hermes plugin
#    --dry-run           Show what would be done without doing it
# ============================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
info() { echo -e "  ${BLUE}→${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}!${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*" >&2; }
step() { echo -e "\n${BOLD}$*${RESET}"; }
die()  { err "$*"; exit 1; }

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/agentbus"
MODULE_DST="$HERMES_HOME/agent_bus"
CONFIG_FILE="$HERMES_HOME/config.yaml"
OPT_WS_PORT=9876  # shown in uninstall summary only

# ── Arg parsing ───────────────────────────────────────────────────────────────

OPT_TOKEN=""
OPT_SERVER=""
OPT_AGENT_ID=""
OPT_NO_SYNC=0
OPT_UNINSTALL=0
OPT_DRY=0

while [[ $# -gt 0 ]]; do
    case $1 in
        --token)      OPT_TOKEN="$2";    shift 2 ;;
        --server)     OPT_SERVER="$2";   shift 2 ;;
        --agent-id)   OPT_AGENT_ID="$2"; shift 2 ;;
        --no-sync)    OPT_NO_SYNC=1;     shift ;;
        --uninstall)  OPT_UNINSTALL=1;   shift ;;
        --dry-run)    OPT_DRY=1;         shift ;;
        -h|--help)
            sed -n '3,25p' "$0" | sed 's/^#//'
            exit 0
            ;;
        *) die "Unknown option: $1 (use --help for usage)" ;;
    esac
done

# Dry-run wrapper
run() {
    if [[ $OPT_DRY -eq 1 ]]; then
        echo -e "  ${YELLOW}[dry]${RESET} $*"
    else
        eval "$@"
    fi
}

# ── Banner ────────────────────────────────────────────────────────────────────

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║      AgentBus × Hermes — Installer           ║"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${RESET}"

[[ $OPT_DRY -eq 1 ]] && warn "DRY RUN — no changes will be made"

# ── Uninstall path ────────────────────────────────────────────────────────────

if [[ $OPT_UNINSTALL -eq 1 ]]; then
    step "Uninstalling AgentBus from Hermes…"

    # Remove plugin
    if [[ -d "$PLUGIN_DIR" ]]; then
        run "rm -rf '$PLUGIN_DIR'"
        ok "Plugin removed: $PLUGIN_DIR"
    fi

    # Remove from config.yaml
    python3 - "$CONFIG_FILE" "$OPT_DRY" <<'PYEOF'
import sys, os, re

config_path = sys.argv[1]
dry = sys.argv[2] == "1"

if not os.path.exists(config_path):
    sys.exit(0)

with open(config_path) as f:
    text = f.read()

# Remove agentbus from plugins.enabled list
text = re.sub(r'\n  - agentbus\n', '\n', text)

# Remove gateway.platforms.agentbus block
# Match "    agentbus:" and all following indented lines
text = re.sub(
    r'\n    agentbus:\n(      [^\n]*\n)*( {8}[^\n]*\n)*',
    '\n',
    text,
)

if not dry:
    with open(config_path, 'w') as f:
        f.write(text)
    print("  \033[0;32m✓\033[0m  config.yaml updated (agentbus removed)")
else:
    print("  \033[1;33m!\033[0m  [dry] would update config.yaml")
PYEOF

    echo ""
    ok "Uninstall complete."
    echo ""
    echo "  Note: the agent_bus module was NOT removed from:"
    echo "  $MODULE_DST"
    echo "  (other tools may depend on it)"
    exit 0
fi

# ── Prerequisites ─────────────────────────────────────────────────────────────

step "Checking prerequisites…"

# Python 3.10+
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" &>/dev/null; then
    die "python3 not found — install Python 3.10+"
fi
PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
if [[ $PY_MAJOR -lt 3 ]] || { [[ $PY_MAJOR -eq 3 ]] && [[ $PY_MINOR -lt 10 ]]; }; then
    die "Python 3.10+ required (found $PY_VERSION)"
fi
ok "Python $PY_VERSION"

# Hermes installed
if [[ ! -d "$HERMES_HOME" ]]; then
    die "Hermes not found at $HERMES_HOME — install Hermes first or set HERMES_HOME"
fi
ok "Hermes home: $HERMES_HOME"

# config.yaml exists
if [[ ! -f "$CONFIG_FILE" ]]; then
    die "config.yaml not found at $CONFIG_FILE"
fi
ok "config.yaml found"

# websockets package
if ! "$PYTHON" -c "import websockets" 2>/dev/null; then
    warn "websockets not installed — installing…"
    run "$PYTHON -m pip install websockets --quiet"
fi
ok "websockets available"

# Source agent-bus repo directory
if [[ ! -f "$SCRIPT_DIR/plugin/__init__.py" ]]; then
    die "Must be run from the agent-bus repo root (got: $SCRIPT_DIR)"
fi
ok "Source directory: $SCRIPT_DIR"

# ── Detect existing token ─────────────────────────────────────────────────────

DETECTED_TOKEN=""
DETECTED_SERVER=""

# Search common places
for candidate in \
    "$MODULE_DST/escucha_global.py" \
    "$MODULE_DST/listener.sh" \
    "$MODULE_DST/start_listener.sh"
do
    if [[ -f "$candidate" ]]; then
        t=$(grep -oE 'AGENT_BUS_TOKEN[= ]*"?([^"[:space:]]+)"?' "$candidate" 2>/dev/null \
            | head -1 | grep -oE '"[^"]+"' | tr -d '"' || true)
        [[ -n "$t" && -z "$DETECTED_TOKEN" ]] && DETECTED_TOKEN="$t"

        s=$(grep -oE 'AGENT_BUS_SERVER[= ]*"?([^"[:space:]]+)"?' "$candidate" 2>/dev/null \
            | head -1 | grep -oE '"[^"]+"' | tr -d '"' || true)
        [[ -n "$s" && -z "$DETECTED_SERVER" ]] && DETECTED_SERVER="$s"
    fi
done

# Env vars override detected values
[[ -n "${AGENT_BUS_TOKEN:-}" ]]  && DETECTED_TOKEN="$AGENT_BUS_TOKEN"
[[ -n "${AGENT_BUS_SERVER:-}" ]] && DETECTED_SERVER="$AGENT_BUS_SERVER"

# CLI flags override everything
[[ -n "$OPT_TOKEN" ]]  && DETECTED_TOKEN="$OPT_TOKEN"
[[ -n "$OPT_SERVER" ]] && DETECTED_SERVER="$OPT_SERVER"

# ── Interactive configuration ──────────────────────────────────────────────────

# Non-interactive when all values are provided via flags or env
NON_INTERACTIVE=0
[[ -n "$OPT_TOKEN" && -n "$OPT_SERVER" && -n "$OPT_AGENT_ID" ]] && NON_INTERACTIVE=1
[[ $OPT_DRY -eq 1 && -n "$DETECTED_TOKEN" ]] && NON_INTERACTIVE=1

step "Configuration…"

# Token
if [[ -z "$DETECTED_TOKEN" && $NON_INTERACTIVE -eq 0 ]]; then
    echo ""
    echo -e "  ${BOLD}AgentBus Token${RESET}"
    echo "  Shared secret that defines the private network."
    echo "  All agents with the same token can see each other."
    echo ""
    printf "  Token: "
    read -r DETECTED_TOKEN
    [[ -z "$DETECTED_TOKEN" ]] && die "Token is required"
elif [[ -z "$DETECTED_TOKEN" ]]; then
    die "Token is required — pass --token TOKEN or set AGENT_BUS_TOKEN"
fi
echo -e "  Token:      ${BOLD}${DETECTED_TOKEN:0:8}…${RESET}"

# Server URL
[[ -z "$DETECTED_SERVER" ]] && DETECTED_SERVER="ws://localhost:9876"
if [[ $NON_INTERACTIVE -eq 0 ]]; then
    printf "  Server URL  [${DETECTED_SERVER}]: "
    read -r INPUT_SERVER
    [[ -n "$INPUT_SERVER" ]] && DETECTED_SERVER="$INPUT_SERVER"
fi
echo -e "  Server:     ${BOLD}${DETECTED_SERVER}${RESET}"

# Agent ID
[[ -z "$OPT_AGENT_ID" ]] && OPT_AGENT_ID="hermes"
if [[ $NON_INTERACTIVE -eq 0 ]]; then
    printf "  Agent ID    [${OPT_AGENT_ID}]: "
    read -r INPUT_ID
    [[ -n "$INPUT_ID" ]] && OPT_AGENT_ID="$INPUT_ID"
fi
echo -e "  Agent ID:   ${BOLD}${OPT_AGENT_ID}${RESET}"

# ── Step 1 — Sync agent_bus module ───────────────────────────────────────────

step "Step 1 — Syncing agent_bus module to Hermes…"

if [[ $OPT_NO_SYNC -eq 1 ]]; then
    info "Skipped (--no-sync)"
else
    # Library files (repo layout: server/, client/, plugin/) — deployed
    # FLAT into $MODULE_DST so existing imports (from agent_bus.X import …)
    # keep working unchanged on every machine.
    PY_FILES=(
        plugin/__init__.py plugin/adapter.py
        client/__main__.py client/cli.py client/client.py
        client/hermes_agent.py client/node.py client/p2p.py
        server/bus.py server/protocol.py server/router.py
        server/server.py server/server_ws.py
    )

    run "mkdir -p '$MODULE_DST'"

    UPDATED=0
    for f in "${PY_FILES[@]}"; do
        src="$SCRIPT_DIR/$f"
        dst="$MODULE_DST/$(basename "$f")"
        if [[ -f "$src" ]]; then
            if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
                run "cp '$src' '$dst'"
                info "  synced: $f"
                UPDATED=$((UPDATED + 1))
            fi
        fi
    done

    if [[ $UPDATED -eq 0 ]]; then
        ok "Module already up to date ($MODULE_DST)"
    else
        ok "Synced $UPDATED file(s) → $MODULE_DST"
    fi
fi

# ── Step 2 — Install/update plugin ───────────────────────────────────────────

step "Step 2 — Installing Hermes plugin…"

# The plugin dir must ship the files the Hermes loader needs:
#   __init__.py  — entry point (loader imports this and calls register())
#   adapter.py   — AgentBusAdapter + register()
#   plugin.yaml  — platform manifest
#   p2p.py       — direct-connection manager (adapter loads it from its
#                  own dir; without this copy P2P is silently unavailable)
PLUGIN_FILES=(
    plugin/__init__.py plugin/adapter.py plugin/plugin.yaml
    client/p2p.py
)
for f in "${PLUGIN_FILES[@]}"; do
    if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
        die "Plugin source missing: $SCRIPT_DIR/$f — re-clone the project"
    fi
done

run "mkdir -p '$PLUGIN_DIR'"

for f in "${PLUGIN_FILES[@]}"; do
    src="$SCRIPT_DIR/$f"
    dst="$PLUGIN_DIR/$(basename "$f")"
    if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
        run "cp '$src' '$dst'"
        info "  installed: $(basename "$f")"
    fi
done

# __init__.py is what makes the loader recognize the plugin — verify it landed
if [[ $OPT_DRY -eq 0 && ! -f "$PLUGIN_DIR/__init__.py" ]]; then
    die "Plugin __init__.py missing after install — the loader will skip the plugin"
fi
ok "Plugin installed (__init__.py, adapter.py, plugin.yaml, p2p.py)"

# ── Step 3 — Update config.yaml ──────────────────────────────────────────────

step "Step 3 — Updating Hermes config.yaml…"

"$PYTHON" - "$CONFIG_FILE" "$OPT_AGENT_ID" "$DETECTED_TOKEN" "$DETECTED_SERVER" "$OPT_DRY" <<PYEOF
import sys, re

config_path = sys.argv[1]
agent_id    = sys.argv[2]
token       = sys.argv[3]
server      = sys.argv[4]
dry         = sys.argv[5] == "1"

with open(config_path) as f:
    text = f.read()

changes = []

# ── plugins.enabled ───────────────────────────────────────────────────────────
if "- agentbus" not in text:
    # Find the enabled: list and add agentbus
    text = re.sub(
        r'(plugins:\s*\n  enabled:\s*\n)((?:  - [^\n]+\n)*)',
        lambda m: m.group(1) + m.group(2) + "  - agentbus\n",
        text,
        count=1,
    )
    changes.append("added agentbus to plugins.enabled")

# ── gateway.platforms.agentbus ────────────────────────────────────────────────
# token + server MUST be in config.yaml — the gateway runs under launchd and
# does not inherit shell env vars (~/.zshrc), so it can't read AGENT_BUS_TOKEN
# from the environment. Without them here, the platform fails "requirements not met".
if "agentbus:" not in text:
    agentbus_block = (
        "  platforms:\n"
        "    agentbus:\n"
        "      enabled: true\n"
        "      extra:\n"
        f"        token: {token}\n"
        f"        server: {server}\n"
        f"        agent_id: {agent_id}\n"
        "        allow_all: true\n"
        "        skills:\n"
        "          - assistant\n"
        "          - analysis\n"
        "          - writing\n"
        "          - research\n"
        "          - code\n"
    )
    # Insert after "gateway:" line
    text = re.sub(
        r'(^gateway:\s*\n)',
        r'\1' + agentbus_block,
        text,
        count=1,
        flags=re.MULTILINE,
    )
    changes.append("added gateway.platforms.agentbus")

if not dry:
    with open(config_path, "w") as f:
        f.write(text)
    for c in changes:
        print(f"  \033[0;32m✓\033[0m  config.yaml: {c}")
    if not changes:
        print("  \033[0;32m✓\033[0m  config.yaml already configured")
else:
    for c in changes:
        print(f"  \033[1;33m!\033[0m  [dry] config.yaml: would {c}")
    if not changes:
        print("  \033[0;32m✓\033[0m  config.yaml already configured")
PYEOF

# ── Step 4 — Environment variables ───────────────────────────────────────────

step "Step 4 — Environment variables…"

SHELL_RC=""
if [[ -f "$HOME/.zshrc" ]];  then SHELL_RC="$HOME/.zshrc"
elif [[ -f "$HOME/.bashrc" ]]; then SHELL_RC="$HOME/.bashrc"
elif [[ -f "$HOME/.profile" ]]; then SHELL_RC="$HOME/.profile"
fi

ENV_BLOCK="
# ── AgentBus ──────────────────────────────────────────────────────────
export AGENT_BUS_TOKEN=\"${DETECTED_TOKEN}\"
export AGENT_BUS_SERVER=\"${DETECTED_SERVER}\"
export AGENT_BUS_AGENT_ID=\"${OPT_AGENT_ID}\"
# ─────────────────────────────────────────────────────────────────────"

if [[ -n "$SHELL_RC" ]]; then
    if grep -q "AGENT_BUS_TOKEN" "$SHELL_RC" 2>/dev/null; then
        warn "AGENT_BUS_TOKEN already in $SHELL_RC — update manually if needed"
    else
        if [[ $OPT_DRY -eq 0 ]]; then
            echo "$ENV_BLOCK" >> "$SHELL_RC"
            ok "Env vars added to $SHELL_RC"
        else
            info "[dry] would add env vars to $SHELL_RC"
        fi
    fi
else
    warn "Could not detect shell RC file — add these manually:"
    echo ""
    echo "$ENV_BLOCK"
fi

# ── Step 5 — Verify bus server is reachable ──────────────────────────────────

step "Step 5 — Verifying connection to bus server…"

HTTP_URL="${DETECTED_SERVER/ws:\/\//http://}"
HTTP_URL="${HTTP_URL/:9876/:9877}"

if [[ $OPT_DRY -eq 1 ]]; then
    info "[dry] would check: $HTTP_URL/health"
elif "$PYTHON" - "$HTTP_URL" <<'PYEOF' 2>/dev/null; then
import sys, urllib.request, json
url = sys.argv[1] + "/health"
try:
    resp = json.loads(urllib.request.urlopen(url, timeout=4).read())
    if resp.get("status") == "ok":
        conns = resp.get("ws_connections", "?")
        print(f"  \033[0;32m✓\033[0m  Server reachable — {url.replace('/health','')} "
              f"(uptime: {resp.get('uptime','?')}s, connections: {conns})")
        sys.exit(0)
    sys.exit(1)
except Exception as e:
    print(f"  \033[1;33m!\033[0m  Server not reachable: {e}")
    print(f"       Is the server running? Deploy it with:")
    print(f"         sudo bash scripts/install-server.sh --ws-port 9876")
    sys.exit(1)
PYEOF
    : # server reachable
else
    warn "Could not reach the bus server at $HTTP_URL"
    warn "The gateway will keep retrying — no action needed if server starts later."
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║  Hermes Plugin Installed                     ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Module:      $MODULE_DST"
echo "  Plugin:      $PLUGIN_DIR"
echo "  Agent ID:    $OPT_AGENT_ID"
echo "  Token:       ${DETECTED_TOKEN:0:8}…"
echo "  Server:      $DETECTED_SERVER"
echo ""
echo -e "${BOLD}  Next steps:${RESET}"
echo ""
echo "  1. Reload your shell (or run: source ~/.zshrc)"
echo "  2. Restart the Hermes gateway:"
echo "       hermes gateway restart"
echo "  3. Verify AgentBus is connected:"
echo "       hermes gateway status"
echo ""
echo -e "${BOLD}  Test from another agent:${RESET}"
echo "    PYTHONPATH=$HERMES_HOME AGENT_BUS_TOKEN=$DETECTED_TOKEN \\"
echo "      python3 -c \""
echo "    import asyncio"
echo "    from agent_bus.hermes_agent import connect_to_bus"
echo "    async def test():"
echo "        bus = await connect_to_bus('tester', token='${DETECTED_TOKEN:0:8}...', server='$DETECTED_SERVER')"
echo "        await bus.send_message('Hello Hermes!', target='$OPT_AGENT_ID')"
echo "        await bus.disconnect()"
echo "    asyncio.run(test())"
echo "    \""
echo ""
echo -e "${BOLD}  Useful commands:${RESET}"
echo "    hermes gateway status      # verify AgentBus 🤖 shows up"
echo "    hermes gateway restart     # apply config changes"
echo ""
echo -e "  To install the central server on the server machine:"
echo "    sudo bash scripts/install-server.sh --ws-port $OPT_WS_PORT"
echo ""
