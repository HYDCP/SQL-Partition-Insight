"""Publish independent omission diagnostics beside the normal run outputs."""
import json
import os
from pathlib import Path
import tempfile

from sql_partition_analysis.paths import remove_if_exists


ALERTS_NAME = '分区访问遗漏告警.jsonl'
CHECK_NAME = '分区访问遗漏检查.json'


def _publish(path, rows, jsonl):
    descriptor, pending_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.part', dir=path.parent)
    pending = Path(pending_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            if jsonl:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
            else:
                stream.write(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
        pending.replace(path)
    finally:
        remove_if_exists(pending)


def export_partition_omissions(profile, report, output_path, progress=None, check_result=None):
    """Check raw SQL independently; diagnostics never modify access results.

    An alert is not an exception: the caller still publishes its usual reports.
    An empty alert file is published too, with a coverage summary explaining
    which contexts and evidence the deliberately limited checker could inspect.
    """
    from sql_partition_analysis.validation.partition_omissions import check_partition_omissions

    def check_progress(done, total):
        progress('独立分区遗漏检查已核对 {:,}/{:,} 个 SQL/用户组合'.format(done, total))

    result = check_result if check_result is not None else check_partition_omissions(
        profile, report, progress=check_progress if progress else None)
    root = Path(output_path).resolve()
    alerts = result['alerts']
    summary = {key: value for key, value in result.items() if key != 'alerts'}
    summary['alerts_file'] = ALERTS_NAME
    summary['alert_count'] = len(alerts)
    _publish(root / ALERTS_NAME, alerts, True)
    _publish(root / CHECK_NAME, summary, False)
    if alerts:
        examples = ', '.join(list(dict.fromkeys(row['sql_id'] for row in alerts))[:5])
        message = ('WARNING 分区访问遗漏检查发现 {} 项告警（SQL：{}）；'
                   '分析继续，详情：{}').format(len(alerts), examples, root / ALERTS_NAME)
        if progress:
            progress(message)
        else:
            print(message, flush=True)
    return summary
