"""AgentBus — transparent multi-agent communication via gateway platform.

Allows agents to communicate, delegate tasks and share messages
asynchronously through the Hermes gateway.

Inspired by Google's A2A (Agent-to-Agent) protocol.
"""
from .adapter import register

__all__ = ["register"]
__version__ = "0.4.0"
