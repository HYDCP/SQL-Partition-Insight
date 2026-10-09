"""Import the provided catalog export; NULL means a non-partitioned table."""
import argparse
from ..paths import DEFAULT_PARTITIONS
import csv
import hashlib
import json
import re
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers


def parse_definition(raw):
    definition = ' '.join(raw.replace('\\n', '\n').split())
    if definition.upper() == 'NULL':
        return None
    match = re.fullmatch(r'PARTITION\s+BY\s+(RANGE|LIST|HASH)\s*\((.*)\)', definition, re.I)
    if not match:
        raise ValueError(f'Unsupported partition definition: {definition}')
    tree = normalize_identifiers(sqlglot.parse_one('SELECT ' + match[2], read='postgres'), dialect='postgres')
    columns = tree.expressions
    if not columns or any(not isinstance(c, exp.Column) or c.table or c.db or c.catalog for c in columns):
        raise ValueError(f'Partition expressions require manual configuration: {definition}')
    return {'partition_columns': [c.name for c in columns],
            'partition_type': match[1].upper(), 'partition_definition': definition}


def load_metadata(source):
    entries = {}
    definitions = {}
    rows = duplicates = 0
    with source.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['SCHEMA_NAME', 'TAB_NAME', 'TAB_PARTI_FIELD']:
            raise ValueError('Expected SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD header')
        for number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Invalid CSV width at row {number}')
            schema, table, raw = (row[k].strip() for k in reader.fieldnames)
            if not schema or not table:
                raise ValueError(f'Missing schema or table at row {number}')
            # Catalog names are already resolved identifiers, not user names.
            name = schema + '.' + table
            if raw not in definitions:
                definitions[raw] = parse_definition(raw)
            entry = definitions[raw]
            rows += 1
            if name in entries:
                if entries[name] != entry:
                    raise ValueError(f'Conflicting partition definitions for {name} at row {number}')
                duplicates += 1
            entries[name] = entry
    return {'version': 1,
            'source': {'path': str(source.resolve()), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                       'rows': rows, 'unique_tables': len(entries), 'duplicate_rows': duplicates,
                       'physical_partition_boundaries_available': False},
            'tables': {name: entry for name, entry in sorted(entries.items()) if entry is not None},
            'non_partitioned_tables': sorted(name for name, entry in entries.items() if entry is None)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=DEFAULT_PARTITIONS)
    args = parser.parse_args()
    config = load_metadata(args.source)
    args.output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({**config['source'], 'partitioned_tables': len(config['tables']),
                                        'non_partitioned_tables': len(config['non_partitioned_tables'])}, ensure_ascii=False))
