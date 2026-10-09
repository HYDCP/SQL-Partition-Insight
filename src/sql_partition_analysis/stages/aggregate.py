"""File/process adapter for the reusable core service."""
from .runtime import stage_root, read_json, write_json, load_catalog
from sql_partition_analysis.analytics.aggregation import aggregate_accesses


def main():
    root = stage_root()
    result = aggregate_accesses(profile=read_json(root / "profile.json"), analysis=read_json(root / "analysis.json"), catalog=load_catalog())
    write_json(root / "aggregate.json", result)


if __name__ == '__main__':
    main()
