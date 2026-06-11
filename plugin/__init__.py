"""AgentBus — transparent multi-agent communication via gateway platform.

Allows agents to communicate, delegate tasks and share messages
asynchronously through the Hermes gateway.

Includes direct P2P connections (agent-to-agent) for lower latency
with automatic fallback to server relay when direct connection is
not available — inspired by Tailscale's coordination model.

Inspired by Google's A2A (Agent-to-Agent) protocol.
"""
from .adapter import register

__all__ = ["register", "P2PManager"]
__version__ = "0.8.0"


def __getattr__(name):
    """Lazy P2PManager export.

    p2p.py is not always a sibling of this file (it lives in client/ in
    the repo; installers copy it next to adapter.py), so the import goes
    through the adapter's zero-config loader instead of a hard relative
    import that would break plugin loading entirely when the file is
    elsewhere.
    """
    if name == "P2PManager":
        from .adapter import _get_p2p_manager
        cls = _get_p2p_manager()
        if cls is None:
            raise AttributeError("P2PManager unavailable: p2p.py not found")
        return cls
    raise AttributeError(name)
