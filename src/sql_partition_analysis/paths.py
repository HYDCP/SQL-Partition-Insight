"""Project locations and subprocess environment shared by entry points."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / 'src'
DEFAULT_CONFIG = PROJECT_ROOT / 'config' / 'config.json'
DEFAULT_PARTITIONS = PROJECT_ROOT / 'config' / 'partition_config.json'
REPORTING_ROOT = Path(__file__).resolve().parent / 'reporting'

def resolve_path(base, value):
    p = Path(value).expanduser()
    return (base / p).resolve() if not p.is_absolute() else p.resolve()


def remove_if_exists(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def subprocess_env(**overrides):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SOURCE_ROOT) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    env.update(overrides)
    return env
