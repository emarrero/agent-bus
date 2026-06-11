"""AgentBus — Multi-Agent Communication Network (Server).

This package contains the central coordination server. Agents connect
via WebSocket, authenticate with a shared token, and exchange messages
in real time. The server routes messages only within the same token
(private channel), maintaining a separate message and task queue per
network.

Usage (development):
    python3 -m server.server_ws --ws-port 9876 --http-port 9877

Production deployment:
    sudo bash scripts/install-server.sh --ws-port 9876 --http-port 9877
"""

__version__ = "0.8.0"
