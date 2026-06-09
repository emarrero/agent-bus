#!/usr/bin/env bash
# Listener persistente para HAL - se reinicia si se cae
export PATH="$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONUNBUFFERED=1
cd "$HOME/.hermes"

LISTENER="$HOME/.hermes/agent_bus/escucha_global.py"
PIDFILE="$HOME/.hermes/agent_bus/hal_listener.pid"
LOGFILE="$HOME/.hermes/agent_bus/hal_listener.log"

# Si ya hay uno corriendo, no duplicar
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Listener ya corriendo (PID $OLD_PID)"
        exit 0
    fi
    rm -f "$PIDFILE"
fi

# Loop de reconexión automática
while true; do
    echo "[$(date)] Iniciando listener HAL..." >> "$LOGFILE"
    python3 "$LISTENER" >> "$LOGFILE" 2>&1
    echo "[$(date)] Listener cayó. Reconectando en 3s..." >> "$LOGFILE"
    sleep 3
done &

echo $! > "$PIDFILE"
echo "✅ HAL listener iniciado (PID $(cat $PIDFILE))"
