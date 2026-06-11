"""AgentBus — repo-root plugin shim.

``hermes plugins install emarrero/agent-bus`` clones this whole repository
into ``~/.hermes/plugins/agent-bus/``. The Hermes loader expects
``plugin.yaml`` + ``__init__.py`` at the plugin root, so this file (and the
``plugin.yaml`` symlink next to it) forward to the real plugin package in
``plugin/``. Everything else (install.sh installs, deployed flat copies)
uses ``plugin/__init__.py`` directly.
"""

try:
    from .plugin import register  # loaded with package context
except ImportError:
    # Executed without package context — load plugin/adapter.py by path
    # (same zero-config strategy as the rest of the project).
    import importlib.util as _ilu
    import os as _os
    import sys as _sys

    _path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                          "plugin", "adapter.py")
    _spec = _ilu.spec_from_file_location("_agentbus_adapter", _path)
    _mod = _ilu.module_from_spec(_spec)
    _sys.modules["_agentbus_adapter"] = _mod
    _spec.loader.exec_module(_mod)
    register = _mod.register

__all__ = ["register", "P2PManager"]
__version__ = "0.8.0"


def __getattr__(name):
    if name == "P2PManager":
        from .plugin import P2PManager  # lazy, via the adapter's loader
        return P2PManager
    raise AttributeError(name)
