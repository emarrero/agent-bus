#!/usr/bin/env python3
import json, os, sys
from datetime import datetime
sys.path.insert(0, os.path.expanduser("~/.hermes"))
INBOX = os.path.expanduser("~/.hermes/agent_bus/inbox.jsonl")
TOKEN = "68fd11d8d1740996c6da70c70cc4d2a3"
SERVER = "ws://100.64.0.9:9876"
AGENT_ID = "hal"
MARCA = os.path.expanduser("~/.hermes/agent_bus/ultimo_check.txt")
def main():
    import urllib.request
    last_count = 0
    if os.path.exists(MARCA):
        with open(MARCA) as f:
            try: last_count = int(f.read().strip())
            except: pass
    try:
        req = urllib.request.Request(
            f"http://100.64.0.9:9877/messages?agent_id={AGENT_ID}&limit=20",
            headers={"X-Agent-Token": TOKEN},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        msgs = resp.get("messages", [])
        new_count = len(msgs)
        if new_count > last_count:
            nuevos = msgs[last_count:]
            for msg in nuevos:
                entry = {"ts": datetime.utcnow().isoformat(), "source": msg.get("source"), "target": msg.get("target"), "type": msg.get("type"), "payload": msg.get("payload")}
                with open(INBOX, "a") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + chr(10))
            with open(MARCA, "w") as f:
                f.write(str(new_count))
            print(f"Nuevos mensajes para HAL ({new_count - last_count}):")
            for m in nuevos:
                s = m.get("source","?")
                p = m.get("payload","")
                if isinstance(p, dict): p = str(p.get("name",""))
                print(f"  [{s}]: {str(p)[:200]}")
        else:
            print("OK")
    except Exception as e:
        print(f"CHECK_ERROR: {e}")
if __name__ == "__main__":
    main()
