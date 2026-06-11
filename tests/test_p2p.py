"""Tests for the AgentBus P2P layer.

Covers the failure modes that broke production:
- the adapter's /discover URL pointing at the WS port instead of the HTTP port
- p2p_port: 0 in config silently re-enabling the default port
- silent dial failures / handshake auth
- glare (both sides dialing at once)
- peer pruning when an agent leaves

Run directly (no pytest needed):   python3 test_p2p.py
Or with pytest:                    pytest test_p2p.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "client"))

from p2p import P2PManager  # noqa: E402

TOKEN = "test-token-123"


async def _wait_for(predicate, timeout: float = 5.0, what: str = "condition"):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _discover_payload(*managers: P2PManager) -> dict:
    """Build a /discover-shaped payload advertising the given managers."""
    return {
        "status": "ok",
        "peers": {
            m.agent_id: {"name": m.agent_id, "ip": "127.0.0.1", "p2p_port": m.p2p_port}
            for m in managers
        },
    }


# ── P2P core ─────────────────────────────────────────────────────────────


async def test_connect_and_exchange():
    """Two managers handshake and exchange messages in both directions."""
    received_a, received_b = [], []
    a = P2PManager("alice", p2p_port=19901, token=TOKEN, auto_reconnect=False)
    b = P2PManager("bob", p2p_port=19911, token=TOKEN, auto_reconnect=False)

    async def on_a(m):
        received_a.append(m)

    async def on_b(m):
        received_b.append(m)

    a.on_message(on_a)
    b.on_message(on_b)

    try:
        await a.start()
        await b.start()
        await a.update_peers(_discover_payload(a, b))

        await _wait_for(lambda: a.peer_count == 1 and b.peer_count == 1,
                        what="mutual peer registration")

        assert await a.send("bob", {"payload": "hi bob", "source": "alice"})
        await _wait_for(lambda: received_b, what="bob receiving alice's message")
        assert received_b[0]["payload"] == "hi bob"
        assert received_b[0]["_via_p2p"] is True

        assert await b.send("alice", {"payload": "hi alice", "source": "bob"})
        await _wait_for(lambda: received_a, what="alice receiving bob's reply")
        assert received_a[0]["payload"] == "hi alice"
    finally:
        await a.stop()
        await b.stop()


async def test_auth_mismatch_rejected():
    """A peer with a different token must not enter the routing table."""
    a = P2PManager("alice", p2p_port=19921, token=TOKEN, auto_reconnect=False)
    b = P2PManager("mallory", p2p_port=19931, token="wrong-token", auto_reconnect=False)
    try:
        await a.start()
        await b.start()
        await a.update_peers(_discover_payload(a, b))
        await asyncio.sleep(1.0)

        assert a.peer_count == 0, f"alice connected to wrong-token peer: {a.peer_ids}"
        assert b.peer_count == 0, f"mallory got a connection: {b.peer_ids}"
        assert not await a.send("mallory", {"payload": "x"})
    finally:
        await a.stop()
        await b.stop()


async def test_glare_resolves_to_single_connection():
    """Both sides dial simultaneously → exactly one usable connection each."""
    received_b = []

    async def on_b(m):
        received_b.append(m)

    a = P2PManager("alice", p2p_port=19941, token=TOKEN, auto_reconnect=False)
    b = P2PManager("bob", p2p_port=19951, token=TOKEN, auto_reconnect=False)
    b.on_message(on_b)
    try:
        await a.start()
        await b.start()
        await asyncio.gather(
            a.update_peers(_discover_payload(a, b)),
            b.update_peers(_discover_payload(a, b)),
        )
        # Let both dials and the tie-break settle
        await asyncio.sleep(1.0)

        assert a.peer_count == 1, f"alice peers: {a.peer_stats()}"
        assert b.peer_count == 1, f"bob peers: {b.peer_stats()}"
        assert await a.send("bob", {"payload": "after glare", "source": "alice"})
        await _wait_for(lambda: received_b, what="message delivery after glare")
    finally:
        await a.stop()
        await b.stop()


async def test_forget_peer_stops_redial():
    """forget_peer drops the connection and clears the redial address."""
    a = P2PManager("alice", p2p_port=19961, token=TOKEN, auto_reconnect=False)
    b = P2PManager("bob", p2p_port=19971, token=TOKEN, auto_reconnect=False)
    try:
        await a.start()
        await b.start()
        await a.update_peers(_discover_payload(a, b))
        await _wait_for(lambda: a.peer_count == 1, what="initial connection")

        await a.forget_peer("bob")
        assert a.peer_count == 0
        assert "bob" not in a._known_addrs
        assert not await a.send("bob", {"payload": "x"})
    finally:
        await a.stop()
        await b.stop()


async def test_update_peers_prunes_departed():
    """Peers absent from the latest /discover stop being redial candidates."""
    a = P2PManager("alice", p2p_port=19981, token=TOKEN, auto_reconnect=False)
    try:
        await a.start()
        await a.update_peers({"peers": {
            "ghost": {"name": "ghost", "ip": "127.0.0.1", "p2p_port": 1},
        }})
        assert "ghost" in a._known_addrs
        await a.update_peers({"peers": {}})
        assert "ghost" not in a._known_addrs
    finally:
        await a.stop()


# ── Adapter (gateway integration) ────────────────────────────────────────


def _stub_gateway_modules():
    """Stub the hermes gateway packages so adapter.py imports standalone."""
    if "gateway" in sys.modules:
        return
    gw = types.ModuleType("gateway")
    cfg = types.ModuleType("gateway.config")
    cfg.Platform = lambda name: name
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform

    class SendResult:
        def __init__(self, success=False, error=None, retryable=False):
            self.success = success
            self.error = error
            self.retryable = retryable

    base.BasePlatformAdapter = BasePlatformAdapter
    base.SendResult = SendResult
    gw.config = cfg
    gw.platforms = platforms
    platforms.base = base
    sys.modules.update({
        "gateway": gw,
        "gateway.config": cfg,
        "gateway.platforms": platforms,
        "gateway.platforms.base": base,
    })


def _load_adapter():
    _stub_gateway_modules()
    import importlib.util
    path = os.path.join(_REPO, "plugin", "adapter.py")
    spec = importlib.util.spec_from_file_location("_agentbus_adapter_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_agentbus_adapter_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_discover_url_uses_http_port():
    """Regression: /discover must target the HTTP API port, not the WS port.

    The original bug: ws://host:9876 → http://host:9876 (WS port kept),
    the websockets server answered 426, and P2P silently never connected.
    """
    adapter = _load_adapter()
    assert adapter._http_api_base("ws://100.64.0.9:9876", 9877) == "http://100.64.0.9:9877"
    assert adapter._http_api_base("wss://bus.example.com:9876", 9877) == "https://bus.example.com:9877"
    assert adapter._http_api_base("ws://localhost", 9877) == "http://localhost:9877"


def test_p2p_port_zero_disables():
    """Regression: p2p_port: 0 in config must disable P2P, not fall back to 9878."""
    adapter = _load_adapter()

    class FakeConfig:
        extra = {"token": "t", "p2p_port": 0}

    inst = adapter.AgentBusAdapter(FakeConfig())
    assert inst._p2p_port == 0, f"p2p_port 0 became {inst._p2p_port}"

    class FakeConfigDefault:
        extra = {"token": "t"}

    os.environ.pop("AGENT_BUS_P2P_PORT", None)
    inst2 = adapter.AgentBusAdapter(FakeConfigDefault())
    assert inst2._p2p_port == 9878


# ── Runner ───────────────────────────────────────────────────────────────


def main():
    sync_tests = [test_discover_url_uses_http_port, test_p2p_port_zero_disables]
    async_tests = [
        test_connect_and_exchange,
        test_auth_mismatch_rejected,
        test_glare_resolves_to_single_connection,
        test_forget_peer_stops_redial,
        test_update_peers_prunes_departed,
    ]
    failures = 0
    for t in sync_tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  ❌ {t.__name__}: {exc}")
    for t in async_tests:
        try:
            asyncio.run(t())
            print(f"  ✅ {t.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  ❌ {t.__name__}: {exc}")
    print(f"\n{len(sync_tests) + len(async_tests) - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
