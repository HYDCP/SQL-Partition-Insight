"""Execution-engine registry, independent of where logs are stored.

Doris as a MySQL-protocol log store does not select the Doris SQL engine.
Reserved engines are discoverable via capabilities but fail during selection.
"""
from .base import AccessEvidence, EngineAdapter, EngineCapabilities, UnsupportedEngineError
from .adb_pg import AdbPostgresAdapter
from .reserved import DorisAdapter, HiveAdapter


_ADAPTERS = {
    "adb_pg": AdbPostgresAdapter(),
    "doris": DorisAdapter(),
    "hive": HiveAdapter(),
}
_ALIASES = {"adb": "adb_pg"}


def engine_capabilities():
    """Return a snapshot of supported and reserved engine capabilities."""
    return {name: adapter.capabilities for name, adapter in _ADAPTERS.items()}


def get_engine_adapter(engine="adb_pg"):
    """Select a supported engine by name, or accept an explicit adapter.

    Explicit adapters support embedding and tests without global registration.
    Invalid and reserved engine names fail immediately, before input is read.
    """
    if isinstance(engine, EngineAdapter):
        adapter = engine
    else:
        if not isinstance(engine, str):
            raise UnsupportedEngineError("Execution engine must be a name or EngineAdapter")
        name = engine.strip().lower()
        name = _ALIASES.get(name, name)
        try:
            adapter = _ADAPTERS[name]
        except KeyError:
            raise UnsupportedEngineError("Unknown execution engine: %r" % engine)
    if not (adapter.capabilities.analysis and adapter.capabilities.partition_evidence):
        raise UnsupportedEngineError(
            "Execution engine %r is reserved but not implemented; "
            "ADB PostgreSQL analysis will not be used as a fallback." % adapter.name)
    return adapter


__all__ = ["AccessEvidence", "EngineAdapter", "EngineCapabilities",
           "UnsupportedEngineError", "engine_capabilities", "get_engine_adapter"]
