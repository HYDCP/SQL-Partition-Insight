"""Shared paths and isolated stage invocation for the source layout."""
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'src'))
from sql_partition_analysis.paths import subprocess_env

CLI = PROJECT_ROOT / 'run_analysis.py'

def stage_command(name):
    return [sys.executable, '-m', 'sql_partition_analysis.stages.' + name.replace('.py', '')]
