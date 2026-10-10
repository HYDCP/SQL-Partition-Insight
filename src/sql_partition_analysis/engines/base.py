"""Contracts between execution-engine semantics and common analysis services."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from sql_partition_analysis.domain.partition import PartitionCandidate


@dataclass(frozen=True)
class EngineCapabilities:
    analysis: bool = False
    partition_evidence: bool = False


@dataclass(frozen=True)
class AccessEvidence:
    """One selected table occurrence and its inferred partition candidates.

    ``detail`` follows the existing analysis detail schema. Candidates describe
    SQL evidence, not proof of physical scans or successful writes. An adapter
    selects eligible occurrences before common statistics consume this result.
    """

    detail: Dict[str, Any]
    candidates: Tuple[PartitionCandidate, ...]


class EngineAdapter(ABC):
    name = ""
    capabilities = EngineCapabilities()

    def validate_event(self, event):
        """Reject event context that this adapter cannot safely interpret yet.

        Future adapters may override this when their analysis and statistics
        preserve and interpret the additional context end to end.
        """
        engine = event.engine_type
        if isinstance(engine, str):
            engine = engine.strip().lower()
            if engine == "adb":
                engine = "adb_pg"
        if engine != self.name:
            raise UnsupportedEngineError(
                "日志事件执行引擎 {!r} 与任务引擎 {!r} 不匹配；不会回退其他 SQL 方言".format(
                    event.engine_type, self.name))
        unsupported = [field for field in (
            "instance_id", "current_database", "current_schema", "session_id", "query_id"
        ) if getattr(event, field) != ""]
        if event.execution_status != "unknown":
            unsupported.append("execution_status")
        if unsupported:
            raise UnsupportedEngineError(
                "本期不支持日志事件上下文字段：{}；不能忽略上下文后继续统计".format(
                    ", ".join(unsupported)))

    @abstractmethod
    def analyze_profile(self, profile, catalog):
        """Produce details, predicates, index, unresolved fields and metrics."""
        raise NotImplementedError

    @abstractmethod
    def resolve_access_evidence(self, analysis, catalog):
        """Return an iterable of AccessEvidence for configured, eligible tables."""
        raise NotImplementedError


class UnsupportedEngineError(ValueError):
    """An unknown or reserved execution engine cannot analyze SQL yet."""
