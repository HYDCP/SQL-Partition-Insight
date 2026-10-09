"""Run SQL parsing in isolated interpreters scheduled by a thread pool.

Original log counts stay in each profile record. Only SQL/user contexts are
distributed; parsing uses separate processes to avoid Python's CPU-bound GIL.
"""
import argparse
import collections
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


from sql_partition_analysis.paths import subprocess_env, DEFAULT_PARTITIONS


def worker_count(value):
    value = int(value)
    if not 1 <= value <= 32:
        raise argparse.ArgumentTypeError("workers 必须在 1 到 32 之间")
    return value


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


def parse_chunk(folder, profile, records, config_path):
    folder.mkdir()
    shard = dict(profile)
    shard['sqls'] = records
    shard['widths'] = {'33': sum(record['count'] for record in records)}
    # Types are not used by analyze.py; omit the original global totals rather
    # than invent a distribution for contexts with several log statement types.
    shard.pop('types', None)
    (folder / 'profile.json').write_text(json.dumps(shard, ensure_ascii=False), encoding='utf-8')
    env = subprocess_env(SQL_ANALYSIS_OUTPUT_DIR=str(folder),
               SQL_ANALYSIS_PARTITION_CONFIG=str(config_path))
    with (folder / 'worker.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-m', 'sql_partition_analysis.stages.analyze'],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"解析进程失败（退出码 {result.returncode}）：{folder / 'worker.log'}")
    return json.loads((folder / 'analysis.json').read_text(encoding='utf-8'))


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


def run(workers=None):
    workers = automatic_workers() if workers is None else worker_count(workers)
    root = Path(os.environ['SQL_ANALYSIS_OUTPUT_DIR']).resolve()
    config_path = Path(os.environ.get('SQL_ANALYSIS_PARTITION_CONFIG', DEFAULT_PARTITIONS)).resolve()
    profile = json.loads((root / 'profile.json').read_text(encoding='utf-8'))
    records = profile['sqls']
    if not records:
        raise ValueError('输入日志为空，请检查文件或查询时间范围')
    if len({record['id'] for record in records}) != len(records):
        raise ValueError('profile.json 存在重复 SQL 编号')
    workers = min(workers, len(records))
    workers_root = root / '.analysis_workers'
    workers_root.mkdir(exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix='run-', dir=workers_root))
    chunks = split_records(records, workers)
    print(f'PROGRESS 解析 SQL 上下文 {len(records)} 个，并发进程 {workers} 个；原始日志计数保持不变', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(parse_chunk, run_root / f'worker-{i:02d}', profile, chunk, config_path)
                   for i, chunk in enumerate(chunks, 1)]
        outputs = [future.result() for future in futures]
    result = merge_results(profile, outputs)
    # Publish only a complete successful result. Existing results survive a
    # worker failure, and a reader cannot observe a partially written JSON.
    pending = run_root / 'analysis.complete.json'
    pending.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    os.replace(pending, root / 'analysis.json')
    print(json.dumps(result['metrics'], ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=worker_count, default=None,
                        help='内部调试覆盖；默认按可用 CPU 自动选择')
    args = parser.parse_args()
    try:
        run(args.workers)
    except Exception as error:
        parser.exit(1, f'并行解析失败：{error}\n')


if __name__ == '__main__':
    main()
