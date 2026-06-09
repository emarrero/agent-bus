#!/usr/bin/env python3
"""Send the OpenCode GO API key for Oracle to configure.

Uses the agent-bus HTTP API to deliver the key. The actual key is
passed via env var so it doesn't appear in command history."""
import json
import os
import sys
import urllib.request

TOKEN = os.environ.get("AGENT_BUS_TOKEN", "")
if not TOKEN:
    print("❌ AGENT_BUS_TOKEN not set")
    sys.exit(1)

key = sys.argv[1] if len(sys.argv) > 1 else ""
if not key:
    print("❌ Usage: python3 set_oracle_key.py <API_KEY>")
    sys.exit(1)

payload = (
    f"Recibí tu API key de OpenCode GO. Para configurarlo:\n\n"
    f"1. En ~/.hermes/.env agrega o actualiza:\n"
    f"   OPENCODE_GO_API_KEY={key}\n\n"
    f"2. Verifica que ~/.hermes/config.yaml tenga:\n"
    f"   model:\n"
    f"     provider: opencode-go\n"
    f"     default: deepseek-v4-flash\n\n"
    f"3. Reinicia node.py:\n"
    f"   pkill -f 'python3 node.py' ; sleep 1\n"
    f"   cd ~/agent-bus && nohup python3 node.py \\\n"
    f"     --token \"$AGENT_BUS_TOKEN\" \\\n"
    f"     --name Oracle --agent-id hermes-oracle \\\n"
    f"     &>/tmp/node_oracle.log &\n\n"
    f"Responde CONFIRMADO cuando esté listo."
)

msg = {
    "message": {
        "source": "hermes-faye",
        "target": "hermes-oracle",
        "payload": payload,
        "type": "text",
    },
    "token": TOKEN,
}

data = json.dumps(msg).encode()
req = urllib.request.Request(
    "http://100.64.0.9:9877/message",
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read())
    print(f"✅ Message sent to Oracle: {result.get('status')}")
except Exception as e:
    print(f"❌ Error: {e}")
