"""File/process adapter for report exports."""
import os
from .runtime import stage_root, read_json, progress
from sql_partition_analysis.reporting.export import export_reports


def main():
    root = stage_root()
    export_reports(read_json(root / 'profile.json'), read_json(root / 'analysis.json'),
                   read_json(root / 'aggregate.json'), os.environ['SQL_ANALYSIS_SOURCE_CSV'], root, progress)


if __name__ == '__main__':
    main()
