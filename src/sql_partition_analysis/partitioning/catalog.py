"""Explicit partition inventory, loaded independently for each analysis run."""
import json
from pathlib import Path


class PartitionCatalog:
    def __init__(self, config):
        import copy
        self.config = copy.deepcopy(config)
        if self.config.get('version') != 1 or not isinstance(self.config.get('tables'), dict):
            raise ValueError('Invalid partition configuration version or tables mapping')
        for table, entry in self.config['tables'].items():
            if not isinstance(table, str) or not table.strip() or not isinstance(entry, dict):
                raise ValueError('Each partitioned table must have a name and configuration object')
            columns = entry.get('partition_columns')
            if not isinstance(columns, list) or not columns or any(not isinstance(c, str) or not c.strip() for c in columns):
                raise ValueError('A configured partitioned table must have a nonempty partition_columns list')
        if set(self.config.get('non_partitioned_tables', [])).intersection(self.config['tables']):
            raise ValueError('A table cannot be both partitioned and non-partitioned')
        self.fields = frozenset(c for entry in self.config['tables'].values() for c in entry['partition_columns'])

    @classmethod
    def from_path(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def partition_columns(self, table):
        return self.config['tables'].get(table, {}).get('partition_columns', [])

    def configured_columns(self):
        return self.fields
