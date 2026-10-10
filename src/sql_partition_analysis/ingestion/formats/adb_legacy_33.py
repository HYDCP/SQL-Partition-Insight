"""Compatibility adapter for the original positional ADB log export."""
import re

from ...domain.log_event import LogEvent
from .base import LogFormat

FIELD_COUNT = 33
COLUMNS = ['column{}'.format(i) for i in range(1, FIELD_COUNT + 1)]
TIME_COLUMN, USER_COLUMN, TYPE_COLUMN, SQL_COLUMN = 0, 2, 18, 21
_PREFIX = re.compile(r'^(?:execute [\s\S]*?: |statement: )')


def sql_identity(row):
    return _PREFIX.sub('', row[SQL_COLUMN], count=1), row[USER_COLUMN]


class AdbLegacy33Format(LogFormat):
    name = 'adb_legacy_33'
    columns = COLUMNS

    def validate_header(self, header):
        # Historic CSV headers are informational; records are positional.
        pass

    def validate_projection(self, columns):
        if len(columns) != FIELD_COUNT:
            raise ValueError('mysql.columns 必须依次列出 33 个不同字段，对应 column1 至 column33')

    def validate_row(self, row, source_record, header=None):
        if len(row) != FIELD_COUNT:
            raise ValueError('第 {} 条记录有 {} 列，预期 33 列；请检查导出字段顺序'.format(
                source_record, len(row)))
        if not row[TIME_COLUMN]:
            raise ValueError('第 {} 条记录缺少日志时间：日志时间为空，无法可靠统计'.format(source_record))

    def to_event(self, row, source_record, header=None):
        self.validate_row(row, source_record, header)
        sql, user = sql_identity(row)
        return LogEvent(sql_text=sql, raw_sql=row[SQL_COLUMN],
                        event_time=row[TIME_COLUMN], user_name=user,
                        statement_type=row[TYPE_COLUMN], source_record=source_record,
                        source_width=len(row))
