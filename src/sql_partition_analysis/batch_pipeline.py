"""Analyze normalized event batches independently of their input transport."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
import gzip
import json
import logging
import multiprocessing

from .analytics.aggregation import aggregate_accesses
from .engines import get_engine_adapter
from .ingestion.state import LogProfileState
from .partitioning.analyzer import analyze_profile
from .partitioning.catalog import PartitionCatalog
from .partitioning.parallel import automatic_workers, split_records, merge_results
from .paths import remove_if_exists
from .reporting.export import export_processed_reports


def _analyze_records(records, catalog, engine, analyze):
    logging.getLogger('sqlglot').setLevel(logging.ERROR)
    profile = {'sqls': records, 'widths': {'events': sum(r['count'] for r in records)}}
    return analyze(profile, catalog, engine=engine)


def _init_parser(config, engine, analyze):
    global _worker_catalog, _worker_engine, _worker_analyze
    _worker_catalog = PartitionCatalog(config)
    _worker_engine = engine
    _worker_analyze = analyze


def _parse_worker(records):
    return _analyze_records(records, _worker_catalog, _worker_engine, _worker_analyze)


def run_event_analysis(batches, source, header, catalog_path, output,
                       workers=None, batch_rows=5000, progress=None, engine='adb_pg',
                       journal_name='.access_observations.jsonl.gz',
                       analyze=None, export=None, executor_factory=None):
    """Consume each batch before fetching the next; retain every occurrence.

    The stream is closed on success and failure. Only unique SQL contexts and
    their analysis stay in memory; the temporary journal has no raw SQL rows.
    Callback arguments keep the old Python entry points usable by integrations.
    Parallel analysis callbacks must be pickleable for spawned worker processes.
    """
    analyze = analyze or analyze_profile
    export = export or export_processed_reports
    executor_factory = executor_factory or ProcessPoolExecutor
    executor = None
    journal = output / journal_name

    def report(message):
        if progress:
            progress(message)
        else:
            print('PROGRESS ' + message, flush=True)

    try:
        with closing(batches):
            adapter = get_engine_adapter(engine)
            workers = automatic_workers() if workers is None else int(workers)
            if not 1 <= workers <= 32:
                raise ValueError('workers 必须在 1 到 32 之间')
            if not 1 <= int(batch_rows) <= 100000:
                raise ValueError('batch_rows 必须为 1 至 100000')
            catalog = PartitionCatalog.from_path(catalog_path)
            state = LogProfileState(source, header=header)
            outputs, active_ids = [], set()
            pool_workers = min(workers, int(batch_rows))
            with gzip.open(journal, 'wt', encoding='utf-8', compresslevel=1) as evidence:
                for batch in batches:
                    for event in batch:
                        adapter.validate_event(event)
                    previous = state.rows
                    records, observations = state.add_batch(batch)
                    if records:
                        if workers == 1 or len(records) == 1:
                            parsed = [_analyze_records(records, catalog, engine, analyze)]
                        else:
                            if executor is None:
                                executor = executor_factory(
                                    max_workers=pool_workers,
                                    mp_context=multiprocessing.get_context('spawn'),
                                    initializer=_init_parser, initargs=(catalog.config, engine, analyze))
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
                        report('已处理 {:,} 条日志；SQL/用户组合 {:,}'.format(state.rows, len(state.contexts)))
                    del batch, records, observations
            if not state.rows:
                raise ValueError('输入日志为空，请检查文件或查询时间范围')
        if executor is not None:
            executor.shutdown(wait=True)
            executor = None
        profile = state.profile()
        analysis = merge_results(profile, outputs)
        aggregate = aggregate_accesses(profile, analysis, catalog, engine=engine)
        for name, value in (('profile', profile), ('analysis', analysis), ('aggregate', aggregate)):
            (output / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        report('读取和解析完成；正在核对访问证据并导出统计')
        with gzip.open(journal, 'rt', encoding='utf-8') as evidence:
            export(profile, analysis, aggregate, (json.loads(line) for line in evidence), output, report)
        return aggregate['metrics']
    finally:
        try:
            if executor is not None:
                executor.shutdown(wait=True)
        finally:
            remove_if_exists(journal)
