"""Coordinate isolated parsing stages and publish complete result bundles."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile

from .configuration import load_config, validate
from .dependencies import check_sqlglot
from .paths import PROJECT_ROOT, REPORTING_ROOT, resolve_path, remove_if_exists, subprocess_env
from .ingestion.mysql import mysql_tables
from .mysql_pipeline import run_mysql_analysis
from .reporting.access_data import ACCESS_FILES
from .reporting.partition_alerts import CHECK_NAME

DELIVERABLES = ['表分区访问统计.csv', 'SQL解析明细.csv', 'SQL原文索引.csv',
                '待确认字段.csv', '逐条日志统计映射.csv.gz', '表分区配置核对.csv',
                'partition_config.json', 'run_summary.json'] + list(ACCESS_FILES)

def node_binary(config, base):
    explicit = config['output'].get('node_binary')
    candidate = str(resolve_path(base, explicit)) if explicit else shutil.which('node')
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
    if not candidate and bundled.is_file():
        candidate = str(bundled)
    if not candidate or not Path(candidate).is_file():
        raise ValueError('未找到 Node.js；配置 output.node_binary，或设 output.xlsx=false')
    check = subprocess.run([candidate, '--input-type=module', '-e',
                            "await import('@oai/artifact-tool');"],
                           cwd=REPORTING_ROOT, capture_output=True, text=True)
    if check.returncode:
        raise ValueError('Excel 依赖 @oai/artifact-tool 不可用；配置现有运行环境或设 output.xlsx=false')
    return candidate


def run(config_path, check_only=False, source_type=None):
    config_path = config_path.resolve()
    base = config_path.parent
    config = load_config(config_path, source_type)
    sqlglot_version = check_sqlglot(PROJECT_ROOT / 'requirements.txt')
    validate(config, base)
    if config['source']['type'] == 'mysql' and importlib.util.find_spec('pymysql') is None:
        raise ValueError('缺少 PyMySQL，请先安装 requirements.txt')
    node = node_binary(config, base) if config['output'].get('xlsx', False) else None
    if check_only:
        print(f"配置与本地依赖检查通过；来源={config['source']['type']}；SQLGlot={sqlglot_version}；未连接 MySQL、未读取数据")
        return None
    output_parent = resolve_path(base, config['output']['directory'])
    output_parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    output = output_parent / stamp
    output.mkdir()
    summary = {'status': 'running', 'source_type': config['source']['type'],
               'started_at': dt.datetime.now().astimezone().isoformat(), 'xlsx': bool(node)}
    def save_summary():
        (output / 'run_summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    save_summary()
    print(f'本次输出目录：{output}', flush=True)
    try:
        partition = config['partition']
        partition_source = resolve_path(base, partition['path'])
        partition_target = output / 'partition_config.json'
        if partition['format'] == 'metadata_csv':
            from .partitioning.importer import load_metadata
            metadata = load_metadata(partition_source)
            partition_target.write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        else:
            shutil.copyfile(partition_source, partition_target)
        source = config['source']
        env = subprocess_env(SQL_ANALYSIS_OUTPUT_DIR=str(output),
                             SQL_ANALYSIS_PARTITION_CONFIG=str(partition_target))
        if source['type'] == 'mysql':
            mysql = source['mysql']
            summary['database_tables'] = [mysql['database'] + '.' + table for table in mysql_tables(mysql)]
            source_label = 'MySQL ' + ', '.join(summary['database_tables'])
            with (output / 'mysql_analysis.log').open('w', encoding='utf-8') as log:
                def mysql_progress(message):
                    line = 'PROGRESS ' + message
                    print(line, flush=True)
                    log.write(line + '\n')
                    log.flush()
                metrics = run_mysql_analysis(mysql, base, partition_target, output, source_label,
                                             progress=mysql_progress)
            summary['read_rows'] = metrics['source_rows']
        else:
            source_path = resolve_path(base, source['csv']['path'])
            source_label = source_path.name
            original_stat = source_path.stat()
            env['SQL_ANALYSIS_SOURCE_CSV'] = str(source_path)
            env['SQL_ANALYSIS_SOURCE_LABEL'] = source_label
            for script in ('profile.py', 'parallel_analyze.py', 'aggregate.py', 'export_data.py'):
                print(f'正在执行 {script}', flush=True)
                command = [sys.executable, '-m', 'sql_partition_analysis.stages.' + script[:-3]]
                with (output / (script + '.log')).open('w', encoding='utf-8') as log:
                    with subprocess.Popen(command, env=env, cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
                        for line in process.stdout:
                            log.write(line)
                            if line.startswith('PROGRESS'):
                                print(line.rstrip(), flush=True)
                        status = process.wait()
                if status:
                    raise RuntimeError(f'{script} 失败，请查看 {output / (script + ".log")}')
            current_stat = source_path.stat()
            if (original_stat.st_size, original_stat.st_mtime_ns) != (current_stat.st_size, current_stat.st_mtime_ns):
                raise RuntimeError('处理期间输入 CSV 发生变化，本次结果不可用，请重跑')
            metrics = json.loads((output / 'aggregate.json').read_text(encoding='utf-8'))['metrics']
        omission_check=json.loads((output / CHECK_NAME).read_text(encoding='utf-8'))
        summary.update(partition_omission_alerts=omission_check['alert_count'],
                       partition_omission_check_file=CHECK_NAME)
        if node:
            print('正在生成 Excel', flush=True)
            with (output / 'build_report.log').open('w', encoding='utf-8') as log:
                result = subprocess.run([node, str(REPORTING_ROOT / 'build_report.mjs')], env=env,
                                        cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f'Excel 导出失败，请查看 {output / "build_report.log"}')
            remove_if_exists(output / '表分区访问统计.xlsx.inspect.ndjson')
        summary.update(status='packaging', source_rows=metrics['source_rows'],
                       tables=metrics['tables'], groups=metrics['stat_groups'],
                       completed_at=dt.datetime.now().astimezone().isoformat())
        save_summary()
        archive_path = output / '统计结果.zip.part'
        complete_summary = dict(summary, status='complete')
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in DELIVERABLES + (['表分区访问统计.xlsx'] if node else []):
                if name == 'run_summary.json':
                    archive.writestr(name, json.dumps(complete_summary, ensure_ascii=False, indent=2))
                else:
                    archive.write(output / name, name)
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('结果压缩包校验失败')
        archive_path.replace(output / '统计结果.zip')
        summary.update(complete_summary)
        save_summary()
        print(f"完成：{metrics['source_rows']:,} 条日志，{metrics['tables']} 张表，{metrics['stat_groups']} 个统计组")
        print(f'结果：{output / "统计结果.zip"}')
        return output
    except Exception:
        summary['status'] = 'failed'
        remove_if_exists(output / '统计结果.zip.part')
        save_summary()
        print(f'本次运行未完成；中间文件保留在 {output}，不可当成完整结果', file=sys.stderr)
        raise
