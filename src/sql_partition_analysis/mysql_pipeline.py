"""MySQL-protocol input adapter; analysis is shared with CSV input."""
from concurrent.futures import ProcessPoolExecutor

from .batch_pipeline import run_event_analysis
from .ingestion.formats import get_log_format
from .ingestion.mysql import iter_mysql_event_batches, mysql_tables
from .ingestion.state import LogProfileState
from .partitioning.analyzer import analyze_profile
from .reporting.export import export_processed_reports


class MysqlLogState(LogProfileState):
    """Compatibility facade for callers still supplying legacy 33-column rows."""
    def __init__(self, source):
        self._format = get_log_format()
        super().__init__(source, header=self._format.columns)

    def add_batch(self, batch):
        start = self.rows
        events = [self._format.to_event(row, start + i) for i, row in enumerate(batch, 1)]
        return super().add_batch(events)


def run_mysql_analysis(config, base, catalog_path, output, label,
                       connect=None, password=None, workers=None, progress=None,
                       engine='adb_pg'):
    batches = iter_mysql_event_batches(config, base, connect=connect, password=password)
    source = {'type': 'mysql', 'label': label, 'database': config['database'],
              'tables': mysql_tables(config)}
    # The legacy profile header describes the logical layout even when the
    # physical SQL column names were configured differently.
    header = get_log_format(config.get('log_format', 'adb_legacy_33')).columns
    return run_event_analysis(
        batches, source, header, catalog_path, output, workers=workers,
        batch_rows=int(config.get('batch_rows', 5000)), progress=progress, engine=engine,
        journal_name='.mysql_observations.jsonl.gz', analyze=analyze_profile,
        export=export_processed_reports, executor_factory=ProcessPoolExecutor)
