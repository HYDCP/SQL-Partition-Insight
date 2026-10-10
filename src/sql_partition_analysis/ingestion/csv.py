"""Bounded CSV reader yielding standardized log events."""
import csv
from pathlib import Path

from .formats import get_log_format


def read_csv_header(path):
    csv.field_size_limit(20_000_000)
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return next(csv.reader(stream), [])


def iter_csv_batches(path, batch_rows=5000, log_format='adb_legacy_33'):
    batch_rows = int(batch_rows)
    if batch_rows <= 0:
        raise ValueError('batch_rows 必须为正整数')
    adapter = get_log_format(log_format)
    csv.field_size_limit(20_000_000)
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream)
        header = next(reader, None)
        if header is None:
            raise ValueError('输入日志为空，请检查文件或查询时间范围')
        adapter.validate_header(header)
        batch = []
        records = 0
        for records, row in enumerate(reader, 1):
            batch.append(adapter.to_event(row, records, header))
            if len(batch) == batch_rows:
                yield batch
                batch = []
        if batch:
            yield batch
        if not records:
            raise ValueError('输入日志为空，请检查文件或查询时间范围')
