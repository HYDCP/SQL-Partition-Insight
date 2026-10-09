"""One canonical log layout and identity for parsing and reconciliation."""
import re

FIELD_COUNT = 33
COLUMNS = ['column{}'.format(i) for i in range(1, FIELD_COUNT + 1)]
TIME_COLUMN, USER_COLUMN, TYPE_COLUMN, SQL_COLUMN = 0, 2, 18, 21
_PREFIX = re.compile(r'^(?:execute [\s\S]*?: |statement: )')


def sql_identity(row):
    return _PREFIX.sub('', row[SQL_COLUMN], count=1), row[USER_COLUMN]
