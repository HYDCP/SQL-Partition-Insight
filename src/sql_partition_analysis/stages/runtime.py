"""File and environment boundaries used only by process adapters."""
import json
import logging
import os
from pathlib import Path
from sql_partition_analysis.paths import DEFAULT_PARTITIONS
from sql_partition_analysis.partitioning.catalog import PartitionCatalog


def stage_root():
    logging.getLogger('sqlglot').setLevel(logging.ERROR)
    return Path(os.environ['SQL_ANALYSIS_OUTPUT_DIR'])


def load_catalog():
    return PartitionCatalog.from_path(os.environ.get('SQL_ANALYSIS_PARTITION_CONFIG', DEFAULT_PARTITIONS))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def progress(message):
    print('PROGRESS ' + message, flush=True)
