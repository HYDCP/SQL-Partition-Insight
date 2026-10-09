"""File/process adapter for log profiling."""
import os
from .runtime import stage_root, write_json, progress
from sql_partition_analysis.ingestion.profile import profile_logs


def main():
    result = profile_logs(os.environ['SQL_ANALYSIS_SOURCE_CSV'],
                          os.environ.get('SQL_ANALYSIS_SOURCE_LABEL'), progress)
    write_json(stage_root() / 'profile.json', result)


if __name__ == '__main__':
    main()
