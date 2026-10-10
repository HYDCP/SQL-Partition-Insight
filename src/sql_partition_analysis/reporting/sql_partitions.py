"""Default SQL/partition recognition export from processed run evidence."""
import collections
import json
import os
from pathlib import Path
import tempfile

from sql_partition_analysis.paths import remove_if_exists

OUTPUT_NAME = '逐条SQL分区识别.jsonl'


def export_sql_partitions(profile, analysis, report, output_path):
    """One record per SQL/user context, preserving excluded and failed parses.

    Uses processed results only; never reconnects, rereads logs or reparses SQL.
    Publish atomically so export failures cannot replace a successful file.
    """
    records = profile['sqls']
    ids = {record['id'] for record in records}
    index = {item['id']: item for item in analysis['index']}
    source_rows = sum(record['count'] for record in records)
    if (len(ids) != len(records) or len(index) != len(analysis['index']) or set(index) != ids
            or source_rows != sum(profile['widths'].values())
            or source_rows != report['metrics']['source_rows']):
        raise ValueError('运行目录的 SQL 编号或日志数量不一致，无法可靠导出')

    details = collections.defaultdict(list)
    predicates = collections.defaultdict(list)
    partitions = collections.defaultdict(list)
    unresolved = collections.defaultdict(list)
    occurrences = set()
    for detail in analysis['details']:
        if detail['id'] not in ids:
            raise ValueError('解析明细引用了未知 SQL 编号')
        details[detail['id']].append(detail)
        occurrences.add((detail['id'], detail['occurrence'], detail['table']))
    for predicate in analysis['predicates']:
        key = (predicate['id'], predicate['occurrence'], predicate['table'])
        if key not in occurrences:
            raise ValueError('条件证据引用了未知表引用')
        predicates[key].append(predicate)
    for mapping in report['mapping']:
        key = (mapping['id'], mapping['occurrence'], mapping['table'])
        if key not in occurrences:
            raise ValueError('分区映射引用了未知表引用')
        partitions[key].append({field: mapping[field] for field in
                               ('id', 'table', 'partition', 'role', 'basis', 'occurrence', 'raw_partition')})
    for sid, field, condition, reason in analysis['unresolved']:
        if sid not in ids:
            raise ValueError('待确认字段引用了未知 SQL 编号')
        unresolved[sid].append({'field': field, 'condition': condition, 'reason': reason})
    catalog = report['partition_config']
    non_partitioned = set(catalog.get('non_partitioned_tables', []))
    destination = Path(output_path).resolve() / OUTPUT_NAME
    descriptor, pending_name = tempfile.mkstemp(prefix=destination.name + '.', suffix='.part', dir=destination.parent)
    pending = Path(pending_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            for record in records:
                sid = record['id']
                tables = []
                for detail in details[sid]:
                    table = detail['table']
                    key = (sid, detail['occurrence'], table)
                    recognized = partitions[key]
                    included = bool(recognized)
                    if included:
                        reason = ''
                    elif not detail.get('statistics_eligible', True):
                        reason = '异常执行日志，仅保留SQL证据，不计访问统计'
                    elif table in non_partitioned:
                        reason = '清单标明非分区表'
                    elif table.startswith('[未指定schema].'):
                        reason = '未指定 schema，不自动补全'
                    else:
                        reason = '不在明确分区表清单内'
                    tables.append({
                        'table': table, 'occurrence': detail['occurrence'], 'alias': detail['alias'],
                        'role': detail['role'], 'included': included, 'exclusion_reason': reason,
                        'partition_config': catalog['tables'].get(table),
                        'recognized_partitions': recognized, 'predicates': predicates[key],
                        'all_conditions': detail['all_conditions'], 'status': detail['status'],
                        'notes': detail['notes'],
                    })
                item = {
                    'sql_id': sid, 'user_name': record.get('user_name', ''),
                    'log_count': record['count'], 'first_record': record['first_record'],
                    'first_time': record['first_time'], 'last_time': record['last_time'],
                    'sql': record['sql'], 'parse_status': index[sid]['status'],
                    'parse_notes': index[sid]['notes'],
                    'included_in_statistics': any(table['included'] for table in tables),
                    'tables': tables, 'unresolved': unresolved[sid],
                }
                stream.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')
        pending.replace(destination)
    finally:
        remove_if_exists(pending)
    return destination
