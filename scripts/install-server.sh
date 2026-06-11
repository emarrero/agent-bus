#!/usr/bin/env bash
# ============================================================
#  AgentBus Server — System Service Installer
# ============================================================
#  Installs the AgentBus WebSocket server as a persistent
#  system service that starts automatically at boot.
#
#  Supported platforms:
#    macOS   → LaunchDaemon in /Library/LaunchDaemons/
#    Linux   → systemd unit in /etc/systemd/system/
#
#  Usage (run once on the central server machine):
#    sudo bash install-server.sh
#    sudo bash install-server.sh --ws-port 9876 --http-port 9877
#    sudo bash install-server.sh --user myuser --install-dir /opt/agentbus
#    sudo bash install-server.sh --uninstall
#    bash install-server.sh --dry-run
#
#  Options:
#    --ws-port PORT      WebSocket port (default: 9876)
#    --http-port PORT    HTTP health port (default: 9877)
#    --host HOST         Bind host (default: 0.0.0.0)
#    --entry-token TOK   Stable token for rolling mode — agents always connect with
#                        this token and get redirected to the derived channel_hash.
#                        Use POST /roll to rotate the channel without changing this token.
#    --user USER         Run service as this user (default: current user)
#    --install-dir DIR   Where to install server files (default: /opt/agentbus)
#    --python PATH       Python executable to use
#    --log-dir DIR       Log directory (default: /var/log/agentbus)
#    --uninstall         Stop and remove the service
#    --dry-run           Show what would be done without doing it
# ============================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
info() { echo -e "  ${BLUE}→${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}!${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*" >&2; }
step() { echo -e "\n${BOLD}$*${RESET}"; }
die()  { err "$*"; exit 1; }

# ── Detect OS ─────────────────────────────────────────────────────────────────

OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *)      die "Unsupported platform: $OS (macOS and Linux only)" ;;
esac

# ── Defaults ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_USER="${SUDO_USER:-${USER:-$(id -un)}}"

OPT_WS_PORT=9876
OPT_HTTP_PORT=9877
OPT_HOST="0.0.0.0"
OPT_ENTRY_TOKEN=""
OPT_USER="$DEFAULT_USER"
OPT_INSTALL_DIR="/opt/agentbus"
OPT_PYTHON=""
OPT_LOG_DIR="/var/log/agentbus"
OPT_UNINSTALL=0
OPT_DRY=0

SERVICE_NAME="agentbus"
SERVICE_LABEL="com.agentbus.server"   # macOS
PYTHONPATH_DIR=""  # set after install dir is known

# ── Arg parsing ───────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case $1 in
        --ws-port)     OPT_WS_PORT="$2";     shift 2 ;;
        --http-port)   OPT_HTTP_PORT="$2";   shift 2 ;;
        --host)        OPT_HOST="$2";        shift 2 ;;
        --entry-token) OPT_ENTRY_TOKEN="$2"; shift 2 ;;
        --user)        OPT_USER="$2";        shift 2 ;;
        --install-dir) OPT_INSTALL_DIR="$2"; shift 2 ;;
        --python)      OPT_PYTHON="$2";     shift 2 ;;
        --log-dir)     OPT_LOG_DIR="$2";    shift 2 ;;
        --uninstall)   OPT_UNINSTALL=1;     shift ;;
        --dry-run)     OPT_DRY=1;           shift ;;
        -h|--help)
            sed -n '3,25p' "$0" | sed 's/^#//'
            exit 0
            ;;
        *) die "Unknown option: $1 (use --help for usage)" ;;
    esac
done

PYTHONPATH_DIR="$OPT_INSTALL_DIR"

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
echo "  ║   AgentBus Server — Service Installer        ║"
printf "  ║   Platform: %-32s║\n" "$PLATFORM"
echo "  ╚══════════════════════════════════════════════╝"
echo -e "${RESET}"

[[ $OPT_DRY -eq 1 ]] && warn "DRY RUN — no changes will be made"

# ── Sudo check ────────────────────────────────────────────────────────────────

if [[ $OPT_DRY -eq 0 && $EUID -ne 0 ]]; then
    err "This script must be run with sudo to install a system service."
    echo ""
    echo "  Run: sudo bash $0 $*"
    exit 1
fi

# ── Resolve Python ────────────────────────────────────────────────────────────

find_python() {
    local candidates=("python3" "python3.12" "python3.11" "python3.10" "/opt/homebrew/bin/python3" "/usr/local/bin/python3")
    for p in "${candidates[@]}"; do
        if command -v "$p" &>/dev/null; then
            local ver
            ver=$("$p" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            local major minor
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [[ $major -ge 3 && $minor -ge 10 ]]; then
                command -v "$p"
                return 0
            fi
        fi
    done
    return 1
}

if [[ -z "$OPT_PYTHON" ]]; then
    OPT_PYTHON=$(find_python) || die "Python 3.10+ not found — install it or use --python /path/to/python3"
fi

# Verify websockets is importable under this Python
if ! "$OPT_PYTHON" -c "import websockets" 2>/dev/null; then
    warn "websockets not available under $OPT_PYTHON"
    if [[ $OPT_DRY -eq 0 ]]; then
        "$OPT_PYTHON" -m pip install websockets --quiet || warn "Could not install websockets automatically"
    fi
fi

# ── Uninstall ─────────────────────────────────────────────────────────────────

if [[ $OPT_UNINSTALL -eq 1 ]]; then
    step "Uninstalling AgentBus server service…"

    if [[ $PLATFORM == "macos" ]]; then
        PLIST="/Library/LaunchDaemons/${SERVICE_LABEL}.plist"
        if [[ -f "$PLIST" ]]; then
            run "launchctl unload '$PLIST' 2>/dev/null || true"
            run "rm -f '$PLIST'"
            ok "LaunchDaemon removed"
        else
            info "No LaunchDaemon found at $PLIST"
        fi
        # Also remove user LaunchAgent if it exists (from old install)
        USER_PLIST="$HOME/Library/LaunchAgents/${SERVICE_LABEL}.plist"
        if [[ -f "$USER_PLIST" ]]; then
            run "launchctl unload '$USER_PLIST' 2>/dev/null || true"
            run "rm -f '$USER_PLIST'"
            ok "User LaunchAgent also removed"
        fi
    else
        UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
        if [[ -f "$UNIT" ]]; then
            run "systemctl stop '$SERVICE_NAME' 2>/dev/null || true"
            run "systemctl disable '$SERVICE_NAME' 2>/dev/null || true"
            run "rm -f '$UNIT'"
            run "systemctl daemon-reload"
            ok "systemd unit removed"
        else
            info "No systemd unit found at $UNIT"
        fi
    fi

    if [[ -d "$OPT_INSTALL_DIR" ]]; then
        echo ""
        echo "  Install directory NOT removed: $OPT_INSTALL_DIR"
        echo "  Remove manually if no longer needed:"
        echo "    sudo rm -rf $OPT_INSTALL_DIR"
    fi

    echo ""
    ok "Uninstall complete."
    exit 0
fi

# ── Step 1 — Install server files ────────────────────────────────────────────

step "Step 1 — Installing server files…"

# Repo layout: server/, client/, plugin/. Files are deployed FLAT into
# $MODULE_DIR/agent_bus so the import paths (agent_bus.server_ws, …)
# stay the same as before the repo restructure.
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SERVER_FILES=(
    "server/__init__.py"
    "plugin/adapter.py"
    "server/server.py"
    "server/server_ws.py"
    "server/protocol.py"
    "server/bus.py"
    "server/router.py"
    "client/client.py"
    "client/hermes_agent.py"
    "client/p2p.py"
    "client/cli.py"
    "client/__main__.py"
    "client/node.py"
)

MODULE_DIR="$OPT_INSTALL_DIR/agent_bus"

run "mkdir -p '$MODULE_DIR'"
run "mkdir -p '$OPT_LOG_DIR'"

UPDATED=0
for f in "${SERVER_FILES[@]}"; do
    src="$REPO_DIR/$f"
    dst="$MODULE_DIR/$(basename "$f")"
    if [[ -f "$src" ]]; then
        if [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
            run "cp '$src' '$dst'"
            info "  installed: agent_bus/$(basename "$f")"
            UPDATED=$((UPDATED + 1))
        fi
    else
        warn "  not found: $src (skipping)"
    fi
done

if [[ $UPDATED -eq 0 ]]; then
    ok "Files already up to date"
else
    ok "Installed $UPDATED file(s) to $MODULE_DIR"
fi

# Set ownership so the service user can read the files
if [[ $OPT_DRY -eq 0 ]]; then
    chown -R "$OPT_USER":$(id -gn "$OPT_USER" 2>/dev/null || echo "$OPT_USER") \
        "$OPT_INSTALL_DIR" "$OPT_LOG_DIR" 2>/dev/null || true
fi
ok "Ownership: $OPT_USER"

# ── Step 2 — Verify server starts ────────────────────────────────────────────

step "Step 2 — Verifying server script…"

if [[ $OPT_DRY -eq 0 ]]; then
    if PYTHONPATH="$OPT_INSTALL_DIR" "$OPT_PYTHON" -c \
        "import agent_bus.server_ws" 2>/dev/null; then
        ok "server_ws.py imports cleanly"
    else
        warn "Import check failed — service may fail to start"
        warn "Test manually: PYTHONPATH=$OPT_INSTALL_DIR $OPT_PYTHON -c 'import agent_bus.server_ws'"
    fi
else
    info "[dry] would verify: PYTHONPATH=$OPT_INSTALL_DIR $OPT_PYTHON -c 'import agent_bus.server_ws'"
fi

# ── Step 3 — Create system service ───────────────────────────────────────────

step "Step 3 — Creating system service ($PLATFORM)…"

SERVER_SCRIPT="$MODULE_DIR/server_ws.py"
LOG_OUT="$OPT_LOG_DIR/server.log"
LOG_ERR="$OPT_LOG_DIR/server.error.log"

if [[ $PLATFORM == "macos" ]]; then
    # ── macOS: LaunchDaemon ───────────────────────────────────────────────────
    # /Library/LaunchDaemons/ runs at boot as root, with UserName to drop privs

    PLIST_PATH="/Library/LaunchDaemons/${SERVICE_LABEL}.plist"

    # Stop existing instance if running
    if [[ $OPT_DRY -eq 0 ]] && launchctl list "$SERVICE_LABEL" &>/dev/null 2>&1; then
        info "Stopping existing service…"
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
    fi

    # Build optional entry-token args for the plist
    ENTRY_TOKEN_ARGS=""
    if [[ -n "$OPT_ENTRY_TOKEN" ]]; then
        ENTRY_TOKEN_ARGS="        <string>--entry-token</string>
        <string>${OPT_ENTRY_TOKEN}</string>"
    fi

    PLIST_CONTENT="<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\"
  \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key>
    <string>${SERVICE_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${OPT_PYTHON}</string>
        <string>${SERVER_SCRIPT}</string>
        <string>--ws-host</string>
        <string>${OPT_HOST}</string>
        <string>--ws-port</string>
        <string>${OPT_WS_PORT}</string>
        <string>--http-port</string>
        <string>${OPT_HTTP_PORT}</string>
${ENTRY_TOKEN_ARGS}
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>${OPT_INSTALL_DIR}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <!-- Run as this user, not root -->
    <key>UserName</key>
    <string>${OPT_USER}</string>

    <!-- Start at boot -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart automatically if it crashes -->
    <key>KeepAlive</key>
    <true/>

    <!-- Minimum 5s between restarts to avoid rapid loops -->
    <key>ThrottleInterval</key>
    <integer>5</integer>

    <key>StandardOutPath</key>
    <string>${LOG_OUT}</string>

    <key>StandardErrorPath</key>
    <string>${LOG_ERR}</string>

    <key>WorkingDirectory</key>
    <string>${OPT_INSTALL_DIR}</string>
</dict>
</plist>"

    if [[ $OPT_DRY -eq 0 ]]; then
        echo "$PLIST_CONTENT" > "$PLIST_PATH"
        chmod 644 "$PLIST_PATH"
        chown root:wheel "$PLIST_PATH"
        ok "LaunchDaemon written: $PLIST_PATH"

        launchctl load "$PLIST_PATH"
        ok "Service loaded (starts at boot, runs as $OPT_USER)"
    else
        info "[dry] would write: $PLIST_PATH"
        info "[dry] would run: launchctl load $PLIST_PATH"
    fi

else
    # ── Linux: systemd ────────────────────────────────────────────────────────

    UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
    USER_HOME=$(eval echo "~$OPT_USER")

    # Stop existing instance if running
    if [[ $OPT_DRY -eq 0 ]] && systemctl is-active "$SERVICE_NAME" &>/dev/null 2>&1; then
        info "Stopping existing service…"
        systemctl stop "$SERVICE_NAME" || true
    fi

    UNIT_CONTENT="[Unit]
Description=AgentBus WebSocket Server
Documentation=https://github.com/emarrero/agent-bus
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${OPT_USER}
WorkingDirectory=${OPT_INSTALL_DIR}

ExecStart=${OPT_PYTHON} ${SERVER_SCRIPT} \
    --ws-host ${OPT_HOST} \
    --ws-port ${OPT_WS_PORT} \
    --http-port ${OPT_HTTP_PORT}${OPT_ENTRY_TOKEN:+ \\
    --entry-token ${OPT_ENTRY_TOKEN}}

Environment=PYTHONPATH=${OPT_INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1

# Restart automatically on failure
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

# Logging
StandardOutput=append:${LOG_OUT}
StandardError=append:${LOG_ERR}

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${OPT_LOG_DIR}
ProtectHome=read-only
ReadOnlyPaths=${OPT_INSTALL_DIR}

[Install]
WantedBy=multi-user.target"

    if [[ $OPT_DRY -eq 0 ]]; then
        echo "$UNIT_CONTENT" > "$UNIT_PATH"
        chmod 644 "$UNIT_PATH"
        ok "systemd unit written: $UNIT_PATH"

        systemctl daemon-reload
        systemctl enable "$SERVICE_NAME"
        systemctl start "$SERVICE_NAME"
        ok "Service enabled and started (auto-starts at boot)"
    else
        info "[dry] would write: $UNIT_PATH"
        info "[dry] would run: systemctl enable --now $SERVICE_NAME"
    fi
fi

# ── Step 4 — Wait and verify ─────────────────────────────────────────────────

step "Step 4 — Verifying service is running…"

if [[ $OPT_DRY -eq 1 ]]; then
    info "[dry] would check http://127.0.0.1:${OPT_HTTP_PORT}/health"
else
    # Give the process a moment to start
    sleep 3

    if "$OPT_PYTHON" - "$OPT_HTTP_PORT" "$OPT_HOST" <<'PYEOF' 2>/dev/null; then
import sys, urllib.request, json

port = sys.argv[1]
host = sys.argv[2]
# Try both localhost and the configured bind host
for addr in ("127.0.0.1", host):
    try:
        url = f"http://{addr}:{port}/health"
        resp = json.loads(urllib.request.urlopen(url, timeout=4).read())
        if resp.get("status") == "ok":
            print(f"  \033[0;32m✓\033[0m  Service running — http://{addr}:{port}/health (uptime: {resp.get('uptime',0)}s)")
            sys.exit(0)
    except Exception:
        pass
print(f"  \033[1;33m!\033[0m  Service did not respond on port {port} — check logs")
sys.exit(1)
PYEOF
        SERVICE_OK=1
    else
        SERVICE_OK=0
        warn "Service may not have started yet. Check logs:"
        echo "      tail -f $LOG_ERR"
        if [[ $PLATFORM == "macos" ]]; then
            echo "      launchctl list $SERVICE_LABEL"
        else
            echo "      systemctl status $SERVICE_NAME"
        fi
    fi
fi

# ── Step 5 — Logrotate (Linux only) ──────────────────────────────────────────

if [[ $PLATFORM == "linux" && $OPT_DRY -eq 0 && -d /etc/logrotate.d ]]; then
    step "Step 5 — Configuring log rotation…"

    cat > "/etc/logrotate.d/${SERVICE_NAME}" <<LOGROTATE
${OPT_LOG_DIR}/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 ${OPT_USER} ${OPT_USER}
    postrotate
        systemctl kill --signal=HUP ${SERVICE_NAME} 2>/dev/null || true
    endscript
}
LOGROTATE

    ok "Log rotation configured: /etc/logrotate.d/$SERVICE_NAME"
fi

# ── Summary ───────────────────────────────────────────────────────────────────

# Detect public IPs for the connection string
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "127.0.0.1")
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║  AgentBus Server — Installation complete     ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Platform:    $OS"
echo "  Files:       $MODULE_DIR"
echo "  Logs:        $OPT_LOG_DIR"
echo "  Running as:  $OPT_USER"
echo "  Python:      $OPT_PYTHON"
[[ -n "$OPT_ENTRY_TOKEN" ]] && echo "  Entry token: ${OPT_ENTRY_TOKEN:0:8}… (rolling mode enabled)"
echo ""
echo -e "  ${BOLD}Endpoints:${RESET}"
echo "  WebSocket:   ws://0.0.0.0:${OPT_WS_PORT}"
echo "  HTTP health: http://0.0.0.0:${OPT_HTTP_PORT}/health"
echo "  Monitor:     http://0.0.0.0:${OPT_HTTP_PORT}/monitor"
[[ -n "$LOCAL_IP"     ]] && echo "  Local IP:    ws://${LOCAL_IP}:${OPT_WS_PORT}"
[[ -n "$TAILSCALE_IP" ]] && echo "  Tailscale:   ws://${TAILSCALE_IP}:${OPT_WS_PORT}"
echo ""
echo -e "  ${BOLD}Service management:${RESET}"
if [[ $PLATFORM == "macos" ]]; then
    echo "  Status:      sudo launchctl list $SERVICE_LABEL"
    echo "  Stop:        sudo launchctl unload /Library/LaunchDaemons/${SERVICE_LABEL}.plist"
    echo "  Start:       sudo launchctl load   /Library/LaunchDaemons/${SERVICE_LABEL}.plist"
    echo "  Uninstall:   sudo bash $0 --uninstall"
else
    echo "  Status:      systemctl status $SERVICE_NAME"
    echo "  Stop:        sudo systemctl stop $SERVICE_NAME"
    echo "  Start:       sudo systemctl start $SERVICE_NAME"
    echo "  Logs:        journalctl -u $SERVICE_NAME -f"
    echo "  Uninstall:   sudo bash $0 --uninstall"
fi
echo ""
echo -e "  ${BOLD}Logs:${RESET}"
echo "  stdout:  tail -f $LOG_OUT"
echo "  stderr:  tail -f $LOG_ERR"
echo ""
echo -e "  ${BOLD}Connect an agent:${RESET}"
SHOW_IP="${TAILSCALE_IP:-${LOCAL_IP:-127.0.0.1}}"
SHOW_TOKEN="${OPT_ENTRY_TOKEN:-your-shared-token}"
echo "    export AGENT_BUS_TOKEN=${SHOW_TOKEN}"
echo "    export AGENT_BUS_SERVER=ws://${SHOW_IP}:${OPT_WS_PORT}"
if [[ -n "$OPT_ENTRY_TOKEN" ]]; then
    echo ""
    echo "    # Token rolling is active — clients use AGENT_BUS_TOKEN (entry_token)."
    echo "    # The server redirects them to the current channel_hash automatically."
    echo "    # Roll the channel (breaks loops, generates new hash):"
    echo "    #   curl -X POST http://${SHOW_IP}:${OPT_HTTP_PORT}/roll \\"
    echo "    #        -H 'Content-Type: application/json' \\"
    echo "    #        -d '{\"token\": \"${OPT_ENTRY_TOKEN}\"}'"
fi
echo ""
echo -e "  ${BOLD}Install Hermes plugin on each client:${RESET}"
echo "    bash install.sh --token ${SHOW_TOKEN} --server ws://${SHOW_IP}:${OPT_WS_PORT}"
echo ""
