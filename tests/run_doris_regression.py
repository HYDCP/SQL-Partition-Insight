"""Opt-in local Doris regression; does not run during unittest discovery.

Example: python tests/run_doris_regression.py --database test_logs --table logs
Reads the selected existing table and creates/removes its own isolated fixture
database. Passwords are kept in the process environment, never output configs.
"""
import _support

import argparse
from collections import Counter
import datetime as dt
import gc
import gzip
import json
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import patch
import zipfile

import pymysql

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.ingestion.mysql import COLUMNS, identifier
from sql_partition_analysis.ingestion.records import sql_identity
from sql_partition_analysis.mysql_pipeline import run_mysql_analysis
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.pipeline import DELIVERABLES, run


def plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def check(value, message):
    if not value:
        raise AssertionError(message)


def verify_outputs(output, oracle, catalog_path):
    profile = load(output / 'profile.json')
    actual = {(r['sql'], r['user_name']): {
        key: r[key] for key in ('count', 'first_time', 'last_time', 'types')}
        for r in profile['sqls']}
    check(actual == oracle, 'SQL/user counts, types or timestamp ranges differ from oracle')
    catalog = PartitionCatalog.from_path(catalog_path)
    expected_analysis = analyze_profile(profile, catalog)
    check(load(output / 'analysis.json') == plain(expected_analysis), 'Batch parsing differs from serial core')
    expected = aggregate_accesses(profile, expected_analysis, catalog)
    check(load(output / 'aggregate.json') == plain(expected), 'Aggregate differs from serial core')
    summary = load(output / 'run_summary.json')
    check(summary['status'] == 'complete', 'Run not complete')
    check(summary['source_rows'] == sum(r['count'] for r in oracle.values()), 'Source row count differs')
    for name in ('源日志.csv', '源日志.csv.part', '.mysql_observations.jsonl.gz',
                 'profile.py.log', 'parallel_analyze.py.log', 'aggregate.py.log',
                 'export_data.py.log', '.analysis_workers'):
        check(not (output / name).exists(), 'Unexpected CSV input/stage/spool: ' + name)
    targets = {r['access_id'] for r in map(json.loads, (output / 'access_targets.jsonl').read_text().splitlines())}
    counts = Counter()
    previous = 0
    with gzip.open(output / 'access_observations.jsonl.gz', 'rt', encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            check(previous < row['source_record'] <= summary['source_rows'], 'Invalid source record order')
            previous = row['source_record']
            check(set(row['access_ids']).issubset(targets), 'Observation references unknown target')
            counts[row['sql_id']] += 1
    included = {sid for result in expected['results'] for sid in result['ids']}
    check(counts == Counter({r['id']: r['count'] for r in profile['sqls'] if r['id'] in included}),
          'Occurrence counts differ from SQL index')
    manifest = load(output / 'access_manifest.json')
    check(manifest['included_records'] == sum(counts.values()), 'Manifest occurrence count differs')
    with zipfile.ZipFile(output / '统计结果.zip') as archive:
        check(archive.testzip() is None, 'Result archive corrupted')
        check(set(archive.namelist()) == set(DELIVERABLES), 'Archive deliverables differ')
    return {'source_rows': summary['source_rows'], 'contexts': len(profile['sqls']),
            'groups': summary['groups'], 'observations': sum(counts.values()),
            'disk_bytes': sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),
            'output': str(output), 'checks': 'passed'}


def database_oracle(connection, database, tables):
    """Let Doris independently compute counts/time bounds, without raw export."""
    contexts = {}
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute('SELECT column22, column3, column19, COUNT(*), MIN(column1), MAX(column1) '
                           'FROM ' + identifier(database) + '.' + identifier(table) +
                           ' GROUP BY column22, column3, column19')
            for sql, user, kind, count, first, last in cursor.fetchall():
                row = [''] * 33
                row[21], row[2] = sql or '', user or ''
                key = sql_identity(row)
                record = contexts.setdefault(key, {'count': 0, 'first_time': first, 'last_time': last, 'types': set()})
                record['count'] += count
                record['first_time'] = min(record['first_time'], first)
                record['last_time'] = max(record['last_time'], last)
                record['types'].add(kind or '')
    return {key: dict(record, types=sorted(record['types'])) for key, record in contexts.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=_support.PROJECT_ROOT / 'config/config.json')
    parser.add_argument('--database', required=True)
    parser.add_argument('--table', action='append', required=True)
    args = parser.parse_args()
    original = load(args.config)
    mysql = original['source']['mysql']
    check(mysql['host'] in ('127.0.0.1', 'localhost', '::1'), 'This regression is restricted to local Doris')
    secret = os.environ.get(mysql.get('password_env', 'SQL_LOG_MYSQL_PASSWORD'), mysql.get('password')) or ''
    password_env = 'ADB_DORIS_REGRESSION_PASSWORD'
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    root = _support.PROJECT_ROOT / 'outputs' / 'doris_streaming_regression' / stamp
    root.mkdir(parents=True)
    fixture_database = 'codex_mysql_streaming_' + stamp
    connection = pymysql.connect(host=mysql['host'], port=int(mysql.get('port', 3306)),
                                 user=mysql['user'], password=secret, charset='utf8mb4',
                                 connect_timeout=15, read_timeout=120, autocommit=True)
    report = {'status': 'running', 'root': str(root), 'cases': {}}
    created = False
    unraisable = []
    old_hook = getattr(sys, 'unraisablehook', None)
    if old_hook is not None:
        def capture_unraisable(error):
            unraisable.append(type(error.exc_value).__name__)
            old_hook(error)
        sys.unraisablehook = capture_unraisable

    def config_for(name, database, tables, catalog, batch_rows, order_by=None):
        folder = root / name
        folder.mkdir()
        settings = {key: value for key, value in mysql.items()
                    if key not in ('password', 'database', 'table', 'password_env', 'order_by')}
        settings.update(database=database, table=tables, batch_rows=batch_rows,
                        password=None, password_env=password_env)
        if order_by:
            settings['order_by'] = order_by
        config = {'version': 1, 'source': {'type': 'mysql', 'mysql': settings},
                  'partition': dict(catalog), 'output': {'directory': str(folder / 'runs'), 'xlsx': False}}
        path = folder / 'config.json'
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    def failed_case(name, path, action=None):
        try:
            if action:
                with patch('sql_partition_analysis.pipeline.run_mysql_analysis', side_effect=action):
                    run(path)
            else:
                run(path)
        except Exception as error:
            expected = {
                'missing_later_table': (pymysql.err.OperationalError, 'missing'),
                'null_time': (ValueError, '日志时间为空'),
                'all_empty': (ValueError, '所选日志表均没有数据'),
                'killed_connection': (pymysql.err.OperationalError, 'Lost connection'),
            }[name]
            check(isinstance(error, expected[0]) and expected[1] in str(error),
                  name + ' failed for an unrelated reason: ' + type(error).__name__)
            outputs = list((path.parent / 'runs').iterdir())
            check(len(outputs) == 1, 'Expected one failed output directory')
            output = outputs[0]
            check(load(output / 'run_summary.json')['status'] == 'failed', 'Failure status not recorded')
            for filename in ('统计结果.zip', 'access_manifest.json', '.mysql_observations.jsonl.gz', '源日志.csv'):
                check(not (output / filename).exists(), 'Failure published/leaked ' + filename)
            report['cases'][name] = {'checks': 'passed', 'expected_error': type(error).__name__, 'output': str(output)}
        else:
            raise AssertionError(name + ' unexpectedly succeeded')

    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT @@version_comment')
            report['engine'] = cursor.fetchone()[0]
        partition = dict(original['partition'])
        partition_path = Path(partition['path'])
        if not partition_path.is_absolute():
            partition_path = args.config.resolve().parent / partition_path
        partition['path'] = str(partition_path)
        if partition['format'] == 'metadata_csv':
            raise ValueError('Real regression expects a config_json catalog')
        with patch.dict(os.environ, {password_env: secret}):
            oracle = database_oracle(connection, args.database, args.table)
            path = config_for('existing_table', args.database, args.table, partition, 257)
            output = run(path)
            report['cases']['existing_table'] = verify_outputs(output, oracle, partition_path)
            print('REAL REGRESSION existing_table passed', flush=True)

            fixture_catalog = root / 'fixture_partitions.json'
            fixture_catalog.write_text(json.dumps({'version': 1, 'tables': {
                's.events': {'partition_columns': ['data_dt']}, 's.target': {'partition_columns': ['data_dt']}}}), encoding='utf-8')
            catalog = {'format': 'config_json', 'path': str(fixture_catalog)}
            with connection.cursor() as cursor:
                cursor.execute('CREATE DATABASE ' + identifier(fixture_database))
                created = True
                for table in ('day1', 'empty', 'day2', 'bad_time'):
                    columns = ', '.join(identifier(c) + ' TEXT NULL' for c in COLUMNS)
                    cursor.execute('CREATE TABLE ' + identifier(fixture_database) + '.' + identifier(table) +
                                   ' (record_id INT NOT NULL, ' + columns + ') '
                                   'DUPLICATE KEY(record_id) DISTRIBUTED BY HASH(record_id) BUCKETS 1 '
                                   'PROPERTIES ("replication_num"="1")')
                read_sql = "SELECT * FROM s.events WHERE data_dt='2026-09-01'"
                rows = [(read_sql, 'alice', 50, 'SELECT'), (read_sql, 'bob', 10, 'SELECT'),
                        (read_sql, 'alice', 20, 'FETCH'), ('SELECT * FROM s.plain', 'alice', 12, 'SELECT'),
                        ('connection authorized', 'alice', 0, ''),
                        ("SELECT * FROM s.target WHERE data_dt IN ('2026-09-01','2026-09-02')", 'bob', 30, 'SELECT'),
                        (read_sql, 'alice', 5, 'SELECT'), (read_sql, 'bob', 45, 'FETCH'),
                        (read_sql, 'alice', 5, 'SELECT')]
                for table, statements in (('day1', rows[:6]), ('day2', rows[6:]), ('bad_time', rows[:2])):
                    data = []
                    for number, (sql, user, second, kind) in enumerate(statements, 1):
                        row = [''] * 33
                        row[0] = '2026-10-01 00:00:{:02d} CST'.format(second)
                        row[2], row[18], row[21] = user, kind, 'statement: ' + sql
                        if table == 'bad_time' and number == 2:
                            row[0] = None
                        data.append([number] + row)
                    cursor.executemany('INSERT INTO ' + identifier(fixture_database) + '.' + identifier(table) +
                                       ' VALUES (' + ','.join(['%s'] * 34) + ')', data)
            tables = ['day1', 'empty', 'day2']
            oracle = database_oracle(connection, fixture_database, tables)
            for batch_rows in (1, 2):
                name = 'multitable_batch' + str(batch_rows)
                path = config_for(name, fixture_database, tables, catalog, batch_rows, ['record_id'])
                output = run(path)
                report['cases'][name] = verify_outputs(output, oracle, fixture_catalog)
            failed_case('missing_later_table', config_for('missing_later_table', fixture_database,
                        ['day1', 'missing'], catalog, 1, ['record_id']))
            failed_case('null_time', config_for('null_time', fixture_database, ['bad_time'], catalog, 1, ['record_id']))
            failed_case('all_empty', config_for('all_empty', fixture_database, ['empty'], catalog, 1, ['record_id']))

            live = []
            killed = []
            def interrupt_stream(settings, base, catalog_path, output, label, progress=None):
                def connect(**kwargs):
                    kwargs['cursorclass'] = pymysql.cursors.SSCursor
                    current = pymysql.connect(**kwargs)
                    live.append(current)
                    return current
                def on_progress(message):
                    if not killed and message.startswith('MySQL 已处理'):
                        with connection.cursor() as cursor:
                            cursor.execute('KILL ' + str(live[0].thread_id()))
                        killed.append(True)
                    if progress:
                        progress(message)
                return run_mysql_analysis(settings, base, catalog_path, output, label,
                                          connect=connect, password=secret, workers=1, progress=on_progress)
            failed_case('killed_connection', config_for('killed_connection', args.database, args.table,
                        partition, 17), interrupt_stream)
            check(killed and live and not live[0].open, 'Interrupted connection was not closed')
        gc.collect()
        check(not unraisable, 'Driver cleanup emitted unraisable exceptions')
        report['driver_cleanup_errors'] = unraisable
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['failure_type'] = type(error).__name__
        raise
    finally:
        if created:
            with connection.cursor() as cursor:
                cursor.execute('DROP DATABASE ' + identifier(fixture_database))
            report['fixture_database_removed'] = True
        connection.close()
        if old_hook is not None:
            sys.unraisablehook = old_hook
        (root / 'regression_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REAL REGRESSION SUMMARY ' + str(root / 'regression_summary.json'), flush=True)


if __name__ == '__main__':
    main()
