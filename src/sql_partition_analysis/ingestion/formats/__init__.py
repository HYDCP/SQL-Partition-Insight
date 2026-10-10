"""Input formats are independent of SQL execution engines and transports."""
from .adb_legacy_33 import AdbLegacy33Format
from .base import LogFormat


def get_log_format(name='adb_legacy_33'):
    if name == 'adb_legacy_33':
        return AdbLegacy33Format()
    raise ValueError('不支持的日志格式：{!r}'.format(name))
