"""Verification test for the token rolling mechanism.

Scenario:
  1. Server starts with entry_token="network_abc"
  2. Agent A connects with entry_token → gets redirected to channel_hash
  3. Agent A reconnects with channel_hash → works normally
  4. Server rolls channel → new channel_hash generated
  5. Agent A reconnects with OLD channel_hash → redirect to NEW hash ✅
  6. New agent B comes in with original entry_token → redirect to NEW hash ✅
"""
import asyncio
import hashlib
import json
import subprocess
import sys
import time


async def test_rolling():
    try:
        import websockets
    except ImportError:
        sys.exit("Install websockets: pip install websockets")

    ENTRY_TOKEN = "network_abc"
    WS_URL = "ws://localhost:19876"
    HTTP_URL = "http://localhost:19877"

    # ── Start server ────────────────────────────────────────────────
    import os
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "server_ws.py",
         "--ws-port", "19876", "--http-port", "19877",
         "--entry-token", ENTRY_TOKEN],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env,
    )
    await asyncio.sleep(1.5)  # let it start

    passed = []
    failed = []

    def ok(msg): passed.append(msg); print(f"  ✅ {msg}")
    def fail(msg): failed.append(msg); print(f"  ❌ {msg}")

    try:
        # ── Test 1: entry_token vs channel_hash ─────────────────────
        print("\n[Test 1] Initial channel_hash == entry_token (pre-fix gap)")
        ws = await websockets.connect(WS_URL)
        await ws.send(json.dumps({
            "type": "register",
            "agent_id": "agent_a",
            "token": ENTRY_TOKEN,
            "card": {"name": "Agent A"},
        }))
        resp = json.loads(await ws.recv())

        if resp.get("type") == "channel_redirect":
            new_token = resp["token"]
            ok(f"Got channel_redirect → token={new_token[:12]}…")
            # Reconnect with new token
            await ws.close()
            ws = await websockets.connect(WS_URL)
            await ws.send(json.dumps({
                "type": "register",
                "agent_id": "agent_a",
                "token": new_token,
                "card": {"name": "Agent A"},
            }))
            resp2 = json.loads(await ws.recv())
            if resp2.get("status") == "ok":
                ok(f"Reconnected with channel_hash — registered normally")
                actual_token = new_token
            else:
                fail(f"Reconnect with channel_hash failed: {resp2}")
                actual_token = ENTRY_TOKEN
        elif resp.get("status") == "ok":
            # No redirect — entry_token IS the channel (no rolling yet)
            fail(f"No redirect — entry_token == channel_hash (rolling NOT active)")
            actual_token = ENTRY_TOKEN
        else:
            fail(f"Unexpected response: {resp}")
            actual_token = ENTRY_TOKEN

        # Read the agents_list that follows registration
        try:
            await asyncio.wait_for(ws.recv(), timeout=2)
        except Exception:
            pass
        await ws.close()

        # ── Test 2: /roll endpoint ──────────────────────────────────
        print("\n[Test 2] HTTP /roll endpoint exists")
        import urllib.request
        try:
            data = json.dumps({"token": ENTRY_TOKEN}).encode()
            req = urllib.request.Request(
                f"{HTTP_URL}/roll",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                body = json.loads(r.read())
            if body.get("status") == "ok" and body.get("channel_hash"):
                ok(f"/roll returned new hash: {body['channel_hash'][:12]}…")
                new_channel = body["channel_hash"]
            else:
                fail(f"/roll returned: {body}")
                new_channel = None
        except Exception as exc:
            fail(f"/roll endpoint not found or failed: {exc}")
            new_channel = None

        # ── Test 3: reconnect with old token → redirect to new hash ─
        print("\n[Test 3] Agent with old token → redirect to rolled channel")
        if new_channel:
            ws2 = await websockets.connect(WS_URL)
            await ws2.send(json.dumps({
                "type": "register",
                "agent_id": "agent_a",
                "token": actual_token,   # old channel_hash
                "card": {"name": "Agent A"},
            }))
            resp3 = json.loads(await ws2.recv())
            if resp3.get("type") == "channel_redirect" and resp3.get("token") == new_channel:
                ok(f"Old token → redirected to new channel_hash ✅")
            elif resp3.get("status") == "ok":
                fail("Old token accepted without redirect (channel_hash not updated)")
            else:
                fail(f"Unexpected: {resp3}")
            await ws2.close()
        else:
            print("  ⏭ Skipped (no new channel from /roll)")

        # ── Test 4: new client with entry_token → redirect to new hash
        print("\n[Test 4] New agent with ORIGINAL entry_token → redirect to rolled channel")
        if new_channel:
            ws3 = await websockets.connect(WS_URL)
            await ws3.send(json.dumps({
                "type": "register",
                "agent_id": "agent_b",
                "token": ENTRY_TOKEN,    # original entry_token
                "card": {"name": "Agent B"},
            }))
            resp4 = json.loads(await ws3.recv())
            if resp4.get("type") == "channel_redirect" and resp4.get("token") == new_channel:
                ok(f"Original entry_token → redirected to new channel_hash ✅")
            elif resp4.get("status") == "ok":
                fail("entry_token accepted without redirect after roll (rolling not working)")
            else:
                fail(f"Unexpected: {resp4}")
            await ws3.close()
        else:
            print("  ⏭ Skipped (no new channel from /roll)")

    finally:
        proc.terminate()
        proc.wait()

    # ── Summary ─────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"Results: {len(passed)} passed, {len(failed)} failed")
    if failed:
        print("\nFailed:")
        for f in failed:
            print(f"  ✗ {f}")
    return len(failed) == 0


if __name__ == "__main__":
    ok = asyncio.run(test_rolling())
    sys.exit(0 if ok else 1)
