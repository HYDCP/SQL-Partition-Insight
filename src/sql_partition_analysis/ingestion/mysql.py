"""Read original log records through CSV files or the MySQL protocol."""
import csv
import getpass
import os
import re
import sys

from ..paths import resolve_path, remove_if_exists
from .records import COLUMNS

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
    fields = config.get('columns', COLUMNS)
    if len(fields) != 33 or len(set(fields)) != 33:
        raise ValueError('mysql.columns 必须依次列出 33 个不同字段，对应 column1 至 column33')
    select = 'SELECT ' + ', '.join(map(identifier, fields)) + ' FROM '
    database = identifier(config['database'])
    order = config.get('order_by', [])
    suffix = ' ORDER BY ' + ', '.join(map(identifier, order)) if order else ''
    return [(table, select + database + '.' + identifier(table) + suffix)
            for table in mysql_tables(config)]


def stream_mysql(config, destination, base, connect=None, password=None):
    """Bounded-memory transport; writes all original rows, including duplicates."""
    queries = mysql_queries(config)
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
    partial = destination.with_suffix('.csv.part')
    connection = None
    rows = 0
    try:
        connection = connect(**kwargs)
        cursor = connection.cursor()
        expected = config.get('columns', COLUMNS)
        with partial.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            for table, query in queries:
                print('正在读取 MySQL 日志表：{}.{}'.format(config['database'], table), flush=True)
                cursor.execute(query)
                actual = [column[0] for column in cursor.description]
                if actual != expected:
                    raise ValueError('MySQL 表 {} 返回字段与配置不符，已停止导出'.format(table))
                while True:
                    batch = cursor.fetchmany(int(config.get('batch_rows', 5000)))
                    if not batch:
                        break
                    for row in batch:
                        if len(row) != 33:
                            raise ValueError('MySQL 返回记录不是 33 列')
                        if row[0] is None or not str(row[0]):
                            raise ValueError('日志时间为空，无法可靠统计')
                        writer.writerow(['' if value is None else value for value in row])
                    previous = rows
                    rows += len(batch)
                    if rows // 100000 != previous // 100000:
                        print(f'MySQL 已导出 {rows:,} 行', flush=True)
        if not rows:
            raise ValueError('所选日志表均没有数据')
        partial.replace(destination)
        return rows
    except Exception as error:
        if password and password in str(error):
            raise RuntimeError(str(error).replace(password, '***')) from None
        raise
    finally:
        # Do not drain an unfinished SSCursor after failure; close its connection.
        if connection is not None:
            connection.close()
        remove_if_exists(partial)
