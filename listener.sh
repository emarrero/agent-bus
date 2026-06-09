#!/usr/bin/env bash
export PATH="$HOME/bin:$PATH"
export AGENT_BUS_TOKEN="68fd11d8d1740996c6da70c70cc4d2a3"
export AGENT_BUS_SERVER="ws://100.64.0.9:9876"
exec agent-bus listen --agent-id "hal" --timeout 3600
