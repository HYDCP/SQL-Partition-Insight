"""Runtime dependency checks use the project's pinned requirements as authority."""
import importlib.util
import re


def check_sqlglot(requirements_path):
    pins = []
    for line in requirements_path.read_text(encoding='utf-8-sig').splitlines():
        requirement = line.split('#', 1)[0].strip()
        if not re.match(r'^sqlglot\b', requirement, re.IGNORECASE):
            continue
        match = re.fullmatch(r'sqlglot\s*==\s*([0-9A-Za-z][0-9A-Za-z.!+_-]*)',
                             requirement, re.IGNORECASE)
        if match is None:
            raise ValueError('requirements.txt 中 SQLGlot 必须使用 sqlglot==具体版本号 固定版本')
        pins.append(match[1])
    if len(pins) != 1:
        raise ValueError('requirements.txt 中必须且只能声明一项 sqlglot==具体版本号')
    if importlib.util.find_spec('sqlglot') is None:
        raise ValueError('缺少 SQLGlot，请使用当前 Python 执行 python -m pip install -r requirements.txt')
    import sqlglot
    expected, installed = pins[0], sqlglot.__version__
    if installed != expected:
        raise ValueError(
            'SQLGlot 版本不匹配：{} 要求 sqlglot=={}，当前 Python 实际安装 {}。'
            '修改依赖文件不会自动安装，请执行 python -m pip install -r requirements.txt'
            .format(requirements_path, expected, installed))
    return installed
