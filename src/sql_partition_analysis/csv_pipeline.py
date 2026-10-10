"""CSV input adapter for the shared event analysis pipeline."""
from pathlib import Path

from .batch_pipeline import run_event_analysis
from .ingestion.csv import iter_csv_batches, read_csv_header


def run_csv_analysis(source, catalog_path, output, label=None, workers=None,
                     progress=None, log_format='adb_legacy_33',
                     batch_rows=5000, engine='adb_pg'):
    source = Path(source)
    header = read_csv_header(source)
    batches = iter_csv_batches(source, batch_rows=batch_rows, log_format=log_format)
    return run_event_analysis(batches, {'path': str(source), 'label': label or source.name},
                              header, catalog_path, output, workers=workers,
                              batch_rows=batch_rows, progress=progress, engine=engine)
