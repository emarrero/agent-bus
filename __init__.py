"""AgentBus — transparent multi-agent communication via gateway platform.

Allows agents to communicate, delegate tasks and share messages
asynchronously through the Hermes gateway.

Includes direct P2P connections (agent-to-agent) for lower latency
with automatic fallback to server relay when direct connection is
not available — inspired by Tailscale's coordination model.

Inspired by Google's A2A (Agent-to-Agent) protocol.
"""
from .adapter import register
from .p2p import P2PManager

__all__ = ["register", "P2PManager"]
__version__ = "0.6.0"
