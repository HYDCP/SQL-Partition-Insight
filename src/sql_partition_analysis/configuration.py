"""Load user configuration and validate only the selected source."""
import json
from .paths import resolve_path
from .ingestion.mysql import mysql_queries
from .partitioning.catalog import PartitionCatalog

def load_config(config_path, source_type=None):
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    source = config.get('source')
    if not isinstance(source, dict):
        raise ValueError('配置缺少 source 对象')
    if source_type is not None:
        source['type'] = source_type
    return config


def validate(config, base):
    if config.get('version') != 1:
        raise ValueError('配置 version 必须为 1')
    source = config['source']
    if source.get('type') not in ('csv', 'mysql'):
        raise ValueError('source.type 必须是 csv 或 mysql')
    if not isinstance(source.get(source['type']), dict):
        raise ValueError(f"请在配置中填写 source.{source['type']}")
    if source['type'] == 'csv':
        if not source['csv'].get('path'):
            raise ValueError('请填写 source.csv.path')
        if not resolve_path(base, source['csv']['path']).is_file():
            raise ValueError('CSV 输入文件不存在')
    else:
        mysql = source['mysql']
        mysql_queries(mysql)
        if not mysql.get('host') or not mysql.get('user'):
            raise ValueError('请填写 mysql.host 和 mysql.user')
        if mysql.get('password') is not None and not isinstance(mysql['password'], str):
            raise ValueError('mysql.password 必须是字符串或 null，空密码请填写空字符串')
        if 'password_env' in mysql and (not isinstance(mysql['password_env'], str) or not mysql['password_env']):
            raise ValueError('mysql.password_env 必须是非空的环境变量名')
        if not 1 <= int(mysql.get('port', 3306)) <= 65535:
            raise ValueError('MySQL 端口无效')
        if not 1 <= int(mysql.get('batch_rows', 5000)) <= 100000:
            raise ValueError('batch_rows 必须为 1 至 100000')
    partition = config['partition']
    if partition.get('format') not in ('metadata_csv', 'config_json'):
        raise ValueError('partition.format 必须是 metadata_csv 或 config_json')
    if not resolve_path(base, partition['path']).is_file():
        raise ValueError('分区清单文件不存在')
    if partition['format'] == 'config_json':
        PartitionCatalog.from_path(resolve_path(base, partition['path']))
    else:
        from .partitioning.importer import load_metadata
        load_metadata(resolve_path(base, partition['path']))
    output = config.get('output')
    if not isinstance(output, dict) or not isinstance(output.get('directory'), str) or not output['directory'].strip():
        raise ValueError('请填写 output.directory 输出目录')
    target = resolve_path(base, output['directory'])
    if target.exists() and not target.is_dir():
        raise ValueError('output.directory 必须是目录，不能是文件')
    if type(output.get('xlsx', False)) is not bool:
        raise ValueError('output.xlsx 必须是 true 或 false')
