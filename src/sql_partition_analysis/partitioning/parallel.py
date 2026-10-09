"""Transport-independent SQL scheduling and parse-result reconciliation."""
import collections
import os
from pathlib import Path


def split_records(records, count):
    """Greedy longest-first placement balances SQL text cost, not log counts."""
    chunks = [[] for _ in range(count)]
    loads = [0] * count
    for position, record in sorted(enumerate(records),
                                   key=lambda pair: (-len(pair[1]['sql']), pair[0])):
        slot = min(range(count), key=lambda i: (loads[i], i))
        chunks[slot].append((position, record))
        loads[slot] += max(1, len(record['sql']))
    return [[record for _, record in sorted(chunk)] for chunk in chunks]


def merge_results(profile, outputs):
    order = {record['id']: position for position, record in enumerate(profile['sqls'])}
    merged = {}
    for key in ('details', 'predicates', 'index', 'unresolved'):
        rows = [row for output in outputs for row in output[key]]
        # Stable sorting preserves occurrence/predicate order within each SQL,
        # including multiple predicates from the same table occurrence.
        merged[key] = sorted(rows, key=lambda row: order[row[0] if key == 'unresolved' else row['id']])
    if [row['id'] for row in merged['index']] != [row['id'] for row in profile['sqls']]:
        raise ValueError('并行解析结果缺少或重复 SQL 编号，未发布 analysis.json')
    versions = {output['metrics']['sqlglot_version'] for output in outputs}
    if len(versions) != 1:
        raise ValueError('并行解析进程的 SQLGlot 版本不一致')
    categories = collections.defaultdict(set)
    for detail in merged['details']:
        categories[detail['table']].add(detail['category'])
    records = {record['id']: record for record in profile['sqls']}
    status_logs = {}
    for row in merged['index']:
        status_logs[row['status']] = status_logs.get(row['status'], 0) + records[row['id']]['count']
    metrics = {
        'sqlglot_version': versions.pop(),
        'source_rows': sum(profile['widths'].values()),
        'raw_distinct_messages': len({record['sql'] for record in profile['sqls']}),
        'sql_user_contexts': len(profile['sqls']),
        'status_sql_counts': dict(collections.Counter(row['status'] for row in merged['index'])),
        'status_logs': status_logs,
        'tables': len(categories),
        'table_categories': dict(collections.Counter('；'.join(sorted(value)) for value in categories.values())),
        'detail_rows': len(merged['details']),
        'unresolved_fields': len(merged['unresolved']),
        'time_min': min(record['first_time'] for record in profile['sqls']),
        'time_max': max(record['last_time'] for record in profile['sqls']),
    }
    return {'metrics': metrics, **merged}


def cpu_quota(root=Path('/sys/fs/cgroup')):
    """Honor Docker CPU limits under both cgroup v2 and v1."""
    try:
        quota, period = (root / 'cpu.max').read_text().split()
        quota, period = int(quota), int(period)
        if quota > 0 and period > 0:
            return (quota + period - 1) // period
    except (OSError, ValueError):
        pass
    for folder in (root / 'cpu', root / 'cpu,cpuacct', root):
        try:
            quota = int((folder / 'cpu.cfs_quota_us').read_text())
            period = int((folder / 'cpu.cfs_period_us').read_text())
            if quota > 0 and period > 0:
                return (quota + period - 1) // period
        except (OSError, ValueError):
            pass
    return None


def automatic_workers():
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    quota = cpu_quota()
    return max(1, min(count, quota if quota is not None else count, 32))
