"""Run from any directory without installing the project package."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from sql_partition_analysis.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
