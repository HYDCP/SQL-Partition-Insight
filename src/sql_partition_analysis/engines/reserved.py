"""Explicit extension points; these engines do not claim SQL support yet."""
from .base import EngineAdapter, UnsupportedEngineError


class ReservedEngineAdapter(EngineAdapter):
    def _unsupported(self):
        raise UnsupportedEngineError(
            "Execution engine %r is reserved but not implemented; "
            "ADB PostgreSQL analysis will not be used as a fallback." % self.name)

    def analyze_profile(self, profile, catalog):
        return self._unsupported()

    def resolve_access_evidence(self, analysis, catalog):
        return self._unsupported()


class DorisAdapter(ReservedEngineAdapter):
    name = "doris"


class HiveAdapter(ReservedEngineAdapter):
    name = "hive"
