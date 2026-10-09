"""File/process adapter for the reusable core service."""
from .runtime import stage_root, read_json, write_json, load_catalog
from sql_partition_analysis.partitioning.analyzer import analyze_profile


def main():
    root = stage_root()
    result = analyze_profile(profile=read_json(root / "profile.json"), catalog=load_catalog())
    write_json(root / "analysis.json", result)


if __name__ == '__main__':
    main()
