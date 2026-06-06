#!/usr/bin/env python3
"""Entry point for the 'agent-bus' command."""
import sys
import os

sys.path.insert(0, os.path.expanduser("~/.hermes"))

from agent_bus.cli import main

if __name__ == "__main__":
    main()
