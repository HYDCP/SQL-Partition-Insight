"""Group repeated SQL while preserving original occurrence counts."""
from pathlib import Path

from .csv import iter_csv_batches, read_csv_header
from .state import LogProfileState


def profile_logs(source, label=None, progress=None, log_format='adb_legacy_33'):
    src = Path(source)
    state = LogProfileState({'path': str(src), 'label': label or src.name},
                            read_csv_header(src))
    for batch in iter_csv_batches(src, log_format=log_format):
        state.add_batch(batch)
        if progress and state.rows % 100000 == 0:
            progress('已扫描 {:,} 条日志；SQL/用户组合 {:,}'.format(state.rows, len(state.contexts)))
    return state.profile()
