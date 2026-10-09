"""MySQL-specific batch analysis, without a CSV input or CSV stage processes."""
import collections
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import gzip
import json
import logging
import multiprocessing

from .analytics.aggregation import aggregate_accesses
from .ingestion.mysql import iter_mysql_batches, mysql_tables
from .ingestion.records import COLUMNS, TIME_COLUMN, TYPE_COLUMN, sql_identity
from .partitioning.analyzer import analyze_profile
from .partitioning.catalog import PartitionCatalog
from .partitioning.parallel import automatic_workers, split_records, merge_results
from .paths import remove_if_exists
from .reporting.export import export_processed_reports


class MysqlLogState:
    """Retain each SQL/user context once and accumulate all log occurrences."""
    def __init__(self, source):
        self.source = source
        self.rows = 0
        self.types = collections.Counter()
        self.contexts = {}

    def add_batch(self, batch):
        new = []
        observations = []
        for row in batch:
            self.rows += 1
            key = sql_identity(row)
            record = self.contexts.get(key)
            if record is None:
                record = {'id': 'SQL{:06d}'.format(len(self.contexts) + 1),
                          'sql': key[0], 'user_name': key[1], 'count': 0,
                          'first_record': self.rows, 'first_time': row[TIME_COLUMN],
                          'last_time': row[TIME_COLUMN], 'types': set()}
                self.contexts[key] = record
                new.append(record)
            record['count'] += 1
            record['types'].add(row[TYPE_COLUMN])
            record['first_time'] = min(record['first_time'], row[TIME_COLUMN])
            record['last_time'] = max(record['last_time'], row[TIME_COLUMN])
            self.types[row[TYPE_COLUMN]] += 1
            observations.append({'source_record': self.rows, 'log_time': row[TIME_COLUMN],
                                 'sql_id': record['id']})
        return [dict(record, types=sorted(record['types'])) for record in new], observations

    def profile(self):
        return {'source': self.source, 'header': COLUMNS,
                'widths': {33: self.rows}, 'types': dict(self.types), 'bad': [],
                'sqls': [dict(record, types=sorted(record['types']))
                         for record in self.contexts.values()]}


def _analyze_records(records, catalog):
    logging.getLogger('sqlglot').setLevel(logging.ERROR)
    return analyze_profile({'sqls': records, 'widths': {33: sum(r['count'] for r in records)}}, catalog)


def _init_parser(config):
    global _worker_catalog
    _worker_catalog = PartitionCatalog(config)


def _parse_worker(records):
    return _analyze_records(records, _worker_catalog)


def run_mysql_analysis(config, base, catalog_path, output, label,
                       connect=None, password=None, workers=None, progress=None):
    """Read, process and discard each batch before fetching the next one.

    Only distinct SQL/user contexts and parse results stay in memory. A compact
    gzip journal retains matched occurrence timestamps for final reconciliation;
    it never contains the original 33-column rows or repeated SQL text.
    """
    workers = automatic_workers() if workers is None else int(workers)
    if not 1 <= workers <= 32:
        raise ValueError('workers 必须在 1 到 32 之间')
    catalog = PartitionCatalog.from_path(catalog_path)
    state = MysqlLogState({'type': 'mysql', 'label': label,
                          'database': config['database'], 'tables': mysql_tables(config)})
    outputs = []
    active_ids = set()
    journal = output / '.mysql_observations.jsonl.gz'
    executor = None
    pool_workers = min(workers, int(config.get('batch_rows', 5000)))

    def report(message):
        if progress:
            progress(message)
        else:
            print('PROGRESS ' + message, flush=True)

    try:
        with closing(iter_mysql_batches(config, base, connect, password)) as batches, \
                gzip.open(journal, 'wt', encoding='utf-8', compresslevel=1) as evidence:
            for batch in batches:
                previous = state.rows
                records, observations = state.add_batch(batch)
                if records:
                    if workers == 1 or len(records) == 1:
                        parsed = [_analyze_records(records, catalog)]
                    else:
                        if executor is None:
                            # Spawn avoids handing a live database socket to children.
                            executor = ProcessPoolExecutor(
                                max_workers=pool_workers,
                                mp_context=multiprocessing.get_context('spawn'),
                                initializer=_init_parser, initargs=(catalog.config,))
                        chunks = split_records(records, min(pool_workers, len(records)))
                        futures = [executor.submit(_parse_worker, chunk) for chunk in chunks]
                        parsed = [future.result() for future in futures]
                    outputs.extend(parsed)
                    for result in parsed:
                        active_ids.update(detail['id'] for detail in result['details']
                                          if catalog.partition_columns(detail['table']))
                for row in observations:
                    if row['sql_id'] in active_ids:
                        evidence.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
                if previous == 0 or state.rows // 100000 != previous // 100000:
                    report('MySQL 已处理 {:,} 条日志；SQL/用户组合 {:,}'.format(state.rows, len(state.contexts)))
                del batch, records, observations
        if executor is not None:
            executor.shutdown(wait=True)
            executor = None
        profile = state.profile()
        analysis = merge_results(profile, outputs)
        aggregate = aggregate_accesses(profile, analysis, catalog)
        for name, value in (('profile', profile), ('analysis', analysis), ('aggregate', aggregate)):
            (output / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        report('MySQL 读取和解析完成；正在核对访问证据并导出统计')
        with gzip.open(journal, 'rt', encoding='utf-8') as evidence:
            export_processed_reports(profile, analysis, aggregate,
                                     (json.loads(line) for line in evidence), output, report)
        return aggregate['metrics']
    finally:
        try:
            if executor is not None:
                executor.shutdown(wait=True)
        finally:
            remove_if_exists(journal)
