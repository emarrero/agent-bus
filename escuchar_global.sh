#!/usr/bin/env bash
# Escuchar el AgentBus global
export PATH="$HOME/bin:$PATH"
export AGENT_BUS_TOKEN="68fd11d8d1740996c6da70c70cc4d2a3"
export AGENT_BUS_SERVER="ws://100.64.0.9:9876"
exec agent-bus listen --agent-id "hermes_emarreros-Mac-M2P.local" --timeout 3600
