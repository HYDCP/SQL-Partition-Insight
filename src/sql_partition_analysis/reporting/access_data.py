"""Stream the versioned access dataset alongside human-readable CSV exports."""
import gzip
import json

from sql_partition_analysis.domain.access import AccessTarget, AccessObservation, SCHEMA_VERSION

# Adapt existing report roles at the output boundary. Downstream consumers use
# these operation codes, so a Chinese display-label change is not an API change.
OPERATIONS = {
    '查询读取': 'read', 'COPY读取': 'copy_read', 'INSERT写入': 'insert',
    'COPY写入': 'copy_write', 'CTAS写入': 'create_as_select', 'UPDATE更新': 'update',
    'DELETE删除': 'delete', 'TRUNCATE清空': 'truncate', 'DROP删除对象': 'drop',
    'CREATE建表': 'create', 'CREATE建索引': 'create_index', 'ALTER结构变更': 'alter',
    'ANALYZE统计': 'analyze', 'COMMENT注释': 'comment', 'COPY方向未知': 'copy_unknown',
    'ALTER新增分区': 'add_partition', 'ALTER清空分区': 'truncate_partition',
    'ALTER分区维护': 'alter_partition',
}
ACCESS_FILES = ('access_targets.jsonl', 'access_observations.jsonl.gz', 'access_manifest.json')


def write_line(stream, record):
    stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')


class AccessDatasetWriter:
    """Bounded-memory writer; call finish only after source reconciliation.

A dataset is consumable only when its enclosing run_summary.status is complete.
Its min/max observed timestamps are not a claim of continuous log coverage.
    """
    def __init__(self, output, profile, results, catalog):
        self.output = output
        self.profile = profile
        self.groups = {}
        self.targets = {}
        self.observations = 0
        for row in results:
            role = row['role']
            target = AccessTarget(row['table'], OPERATIONS.get(role, 'other:' + role),
                                  row['raw_partition'], tuple(catalog['tables'][row['table']]['partition_columns']),
                                  tuple(row['partition_identity']))
            key = target.access_id
            self.groups[row['group_id']] = key
            self.targets[key] = target

    def __enter__(self):
        with (self.output / ACCESS_FILES[0]).open('w', encoding='utf-8') as stream:
            for key in sorted(self.targets):
                write_line(stream, self.targets[key].to_record())
        self.stream = gzip.open(self.output / ACCESS_FILES[1], 'wt', encoding='utf-8', compresslevel=1)
        return self

    def write(self, record_number, log_time, sql_id, user_name, group_ids):
        keys = tuple(sorted({self.groups[gid] for gid in group_ids}))
        write_line(self.stream, AccessObservation(record_number, log_time, sql_id, user_name, keys).to_record())
        self.observations += 1

    def __exit__(self, *exc):
        self.stream.close()

    def finish(self):
        records = self.profile['sqls']
        manifest = {
            'schema_version': SCHEMA_VERSION,
            'run_id': self.output.name,
            'source': self.profile['source'],
            'source_records': sum(self.profile['widths'].values()),
            'included_records': self.observations,
            'targets': len(self.targets),
            'observed_first': min(row['first_time'] for row in records),
            'observed_last': max(row['last_time'] for row in records),
            'log_coverage': 'unknown',
            'partition_scope': 'sql_evidence',
            'target_file': ACCESS_FILES[0],
            'observation_file': ACCESS_FILES[1],
        }
        (self.output / ACCESS_FILES[2]).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
