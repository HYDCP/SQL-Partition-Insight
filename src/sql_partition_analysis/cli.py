"""Command-line selection; connection and file settings live in JSON."""
import argparse
import os
from pathlib import Path
import sys

from .configuration import load_config
from .paths import DEFAULT_CONFIG
from .pipeline import run


def main():
    parser = argparse.ArgumentParser(description='从 CSV 或 MySQL 一键导出 SQL 表分区访问统计')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--source', choices=('csv', 'mysql'),
                        help='选择本次数据来源；覆盖配置 source.type，不修改配置文件')
    parser.add_argument('--check', action='store_true', help='仅检查本地配置与依赖，不连接数据库')
    args = parser.parse_args()
    try:
        run(args.config, args.check, args.source)
    except Exception as error:
        # Never echo connection settings or an exception containing a password.
        message = str(error)
        try:
            configured = load_config(args.config).get('source', {}).get('mysql', {})
            secrets = [configured.get('password'), os.environ.get(configured.get('password_env', 'SQL_LOG_MYSQL_PASSWORD'))]
            for value in secrets:
                if isinstance(value, str) and value:
                    message = message.replace(value, '***')
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        for value in (v for k, v in os.environ.items() if 'PASSWORD' in k.upper() and v):
            message = message.replace(value, '***')
        print(f'失败：{message}', file=sys.stderr)
        return 1
    return 0
