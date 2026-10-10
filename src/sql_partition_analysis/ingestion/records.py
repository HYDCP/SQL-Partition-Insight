"""Legacy import compatibility; new code consumes domain.LogEvent."""
from .formats.adb_legacy_33 import (
    FIELD_COUNT, COLUMNS, TIME_COLUMN, USER_COLUMN, TYPE_COLUMN, SQL_COLUMN,
    sql_identity,
)
