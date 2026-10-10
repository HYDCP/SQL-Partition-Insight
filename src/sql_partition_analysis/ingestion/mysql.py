"""Read bounded batches of original log records through the MySQL protocol."""
from contextlib import closing
import csv
import getpass
import os
import re
import sys

from ..paths import resolve_path, remove_if_exists
from .records import COLUMNS
from .formats import get_log_format


def mysql_columns(config):
    if 'fields' in config:
        raise ValueError('当前仅支持旧 33 列日志格式，尚不支持 fields 映射')
    adapter = get_log_format(config.get('log_format', 'adb_legacy_33'))
    fields = config.get('columns', adapter.columns)
    if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields):
        raise ValueError('mysql.columns 必须列出不同的字段名')
    adapter.validate_projection(fields)
    for field in fields:
        identifier(field)
    adapter.validate_header(fields)
    return fields

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError(f'非法数据库标识符：{value!r}')
    return f'`{value}`'


def mysql_tables(config):
    tables = config.get('table')
    if isinstance(tables, str):
        tables = [tables]
    if not isinstance(tables, list) or not tables:
        raise ValueError('mysql.table 必须是非空表名列表')
    for table in tables:
        identifier(table)
    if len(set(tables)) != len(tables):
        raise ValueError('mysql.table 中有重复表名，请删除重复配置，避免重复读取')
    return tables


def mysql_queries(config):
    if any(key in config for key in ('start_date', 'end_date_exclusive', 'date_column')):
        raise ValueError('日期筛选配置已移除，请删除 start_date/end_date_exclusive/date_column；现在按表清单读取整表')
    fields = mysql_columns(config)
    select = 'SELECT ' + ', '.join(map(identifier, fields)) + ' FROM '
    database = identifier(config['database'])
    order = config.get('order_by', [])
    suffix = ' ORDER BY ' + ', '.join(map(identifier, order)) if order else ''
    return [(table, select + database + '.' + identifier(table) + suffix)
            for table in mysql_tables(config)]


def iter_mysql_batches(config, base, connect=None, password=None):
    """Yield one batch at a time; the caller processes it before the next fetch.

    Closing the iterator also closes an unfinished unbuffered connection.
    Every configured table is read once, including duplicate log records.
    """
    queries = mysql_queries(config)
    adapter = get_log_format(config.get('log_format', 'adb_legacy_33'))
    batch_rows = int(config.get('batch_rows', 5000))
    if batch_rows <= 0:
        raise ValueError('batch_rows 必须为正整数')
    if password is None:
        env_name = config.get('password_env', 'SQL_LOG_MYSQL_PASSWORD')
        password = os.environ.get(env_name)
        if password is None:
            password = config.get('password')
        if password is None:
            if not sys.stdin.isatty():
                raise ValueError(f'请填写 source.mysql.password、设置环境变量 {env_name}，或在交互终端输入密码')
            password = getpass.getpass('MySQL 密码：')
    kwargs = dict(host=config['host'], port=int(config.get('port', 3306)),
                  user=config['user'], password=password, database=config['database'],
                  charset='utf8mb4', connect_timeout=15,
                  read_timeout=int(config.get('read_timeout_seconds', 600)),
                  write_timeout=60, autocommit=True, local_infile=False)
    if config.get('ssl_ca'):
        kwargs.update(ssl_ca=str(resolve_path(base, config['ssl_ca'])),
                      ssl_verify_cert=True, ssl_verify_identity=True)
    if connect is None:
        try:
            import pymysql
        except ImportError:
            raise ValueError('缺少 PyMySQL，请先执行安装依赖命令') from None
        kwargs['cursorclass'] = pymysql.cursors.SSCursor
        connect = pymysql.connect
    connection = None
    cursor = None
    rows = 0
    try:
        connection = connect(**kwargs)
        cursor = connection.cursor()
        expected = mysql_columns(config)
        for table, query in queries:
            print('正在读取 MySQL 日志表：{}.{}'.format(config['database'], table), flush=True)
            cursor.execute(query)
            actual = [column[0] for column in cursor.description]
            if actual != expected:
                raise ValueError('MySQL 表 {} 返回字段与配置不符，已停止读取'.format(table))
            while True:
                batch = cursor.fetchmany(batch_rows)
                if not batch:
                    break
                normalized = []
                for row in batch:
                    if len(row) != len(expected):
                        raise ValueError('MySQL 返回记录不是 {} 列'.format(len(expected)))
                    record = ['' if value is None else str(value) for value in row]
                    adapter.validate_row(record, rows + len(normalized) + 1, expected)
                    normalized.append(record)
                rows += len(normalized)
                yield normalized
                # Release the previous batch before fetching another one.
                del batch, normalized
        if not rows:
            raise ValueError('所选日志表均没有数据')
    except Exception as error:
        if password and password in str(error):
            raise RuntimeError(str(error).replace(password, '***')) from None
        raise
    finally:
        # PyMySQL 1.1.1 cursor/result destructors otherwise try to drain an
        # unfinished stream even after connection.close(). Abort those owned
        # results before closing the socket; never download the remaining rows.
        if connection is not None:
            for result in (getattr(cursor, '_result', None), getattr(connection, '_result', None)):
                if result is not None and hasattr(result, 'unbuffered_active'):
                    result.unbuffered_active = False
                    result.connection = None
            if cursor is not None and hasattr(cursor, 'connection'):
                cursor.connection = None
            connection.close()


def iter_mysql_event_batches(config, base, connect=None, password=None):
    """Adapt rows without changing the historical row transport API."""
    adapter = get_log_format(config.get('log_format', 'adb_legacy_33'))
    header = mysql_columns(config)
    source_record = 0
    with closing(iter_mysql_batches(config, base, connect, password)) as batches:
        for batch in batches:
            events = []
            for row in batch:
                source_record += 1
                events.append(adapter.to_event(row, source_record, header))
            yield events


def stream_mysql(config, destination, base, connect=None, password=None):
    """Explicit CSV export helper; the MySQL analysis pipeline does not use it."""
    partial = destination.with_suffix('.csv.part')
    adapter = get_log_format(config.get('log_format', 'adb_legacy_33'))
    rows = 0
    try:
        with closing(iter_mysql_batches(config, base, connect, password)) as batches, \
                partial.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(adapter.columns)
            for batch in batches:
                writer.writerows(batch)
                rows += len(batch)
        partial.replace(destination)
        return rows
    finally:
        remove_if_exists(partial)
