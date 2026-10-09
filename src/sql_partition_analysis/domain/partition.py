"""A partition candidate keeps presentation separate from its grouping identity."""
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class PartitionCandidate:
    role: str
    condition: str
    basis: str
    identity: Tuple = ()

    @property
    def key(self):
        return self.identity or ('expression', self.condition)
