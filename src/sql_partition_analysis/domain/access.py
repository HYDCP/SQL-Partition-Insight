"""Version 1 contracts for future time-window and partition-lifecycle analysis.

Targets represent SQL evidence, not verified physical partition identities.
Observations retain each source log occurrence, including duplicates. Source
record numbers are local to a run; consumers must not blindly combine reruns.
"""
from dataclasses import dataclass
import hashlib
import json
from typing import Tuple

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AccessTarget:
    table: str
    operation: str
    partition_condition: str
    partition_columns: Tuple[str, ...]
    partition_identity: Tuple

    @property
    def access_id(self):
        # Independent of counts, timestamps, row ordering and presentation IDs.
        identity = [SCHEMA_VERSION, self.table, self.operation,
                    self.partition_identity, self.partition_columns]
        encoded = json.dumps(identity, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def to_record(self):
        return {
            'schema_version': SCHEMA_VERSION,
            'access_id': self.access_id,
            'table': self.table,
            'operation': self.operation,
            'partition_condition': self.partition_condition,
            'partition_identity': self.partition_identity,
            'partition_columns': list(self.partition_columns),
            'partition_scope': 'sql_evidence',
            'physical_partition_id': None,
        }


@dataclass(frozen=True)
class AccessObservation:
    source_record: int
    log_time: str
    sql_id: str
    user_name: str
    access_ids: Tuple[str, ...]

    def to_record(self):
        return {
            'schema_version': SCHEMA_VERSION,
            'source_record': self.source_record,
            # Preserve the source timestamp, including its timezone label.
            'log_time': self.log_time,
            'sql_id': self.sql_id,
            'user_name': self.user_name,
            'access_ids': list(self.access_ids),
        }
