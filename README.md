# SQL-Partition-Insight

SQL 分区访问分析工具。读取 CSV 文件或 Doris 日志表中的 SQL 日志，识别表及分区访问证据，统计访问类型、次数、首次/末次访问时间和平均访问间隔，导出报表与逐条识别明细。

项目将**日志来源、日志格式、SQL 执行引擎**分开，以公共分析流程支持后续多引擎扩展。

## 当前支持范围

| 层次 | 已实现 | 扩展状态 |
| --- | --- | --- |
| 日志来源 | CSV；Doris 日志表，通过 MySQL 协议批量读取 | 两种来源共用分析流水线 |
| 日志格式 | 原有 ADB 33 列格式 `adb_legacy_33` | 已定义 `LogFormat` 接口；通用格式尚未实现 |
| SQL 执行引擎 | ADB PostgreSQL：`adb_pg`，兼容别名 `adb` | Doris、Hive 仅预留接口，尚不能分析其 SQL |
| 元数据 | 分区配置 JSON；现有 ADB 元数据 CSV 导入 | 其他引擎的元数据适配尚未实现 |
| 分析结果 | SQL 证据、分区条件与候选、访问统计、遗漏检查 | 不代表数据库实际扫描的物理分区 |

**日志存放在 Doris，不代表日志中的 SQL 由 Doris 执行。** 例如：从 Doris 日志表读取 ADB SQL 时，使用 `source.type=mysql` 和 `engine.type=adb_pg`。

目前一个任务分析一种引擎，只统计分区清单中明确配置的分区表。不自动补全 schema；无法确定的分区保留为未知。选择尚未实现的引擎或日志格式会在读取前报错，不会回退为 ADB 分析。

## 环境与安装

使用 Conda Python 3.7。已有 `sql-partition-py37` 环境时直接激活；新环境可按下面的命令创建：

```bash
conda create -n sql-partition-py37 python=3.7
conda activate sql-partition-py37
python -m pip install -r requirements.txt
```

SQLGlot 版本由 `requirements.txt` 固定，运行前会检查版本是否一致。

## 配置

在项目根目录复制模板：

```bash
cp config/config.example.json config/config.json
```

主要配置项：

| 配置项 | 用途 |
| --- | --- |
| `engine.type` | SQL 执行引擎，默认 `adb_pg` |
| `source.type` | 输入来源：`csv` 或 `mysql` |
| `source.log_format` | 日志格式，默认且目前仅支持 `adb_legacy_33` |
| `source.csv.path` | CSV 文件路径 |
| `source.mysql` | Doris/MySQL 协议连接配置；`table` 为日志表名列表 |
| `source.mysql.batch_rows` | 每批读取行数，默认 5,000 |
| `partition.format` | 分区元数据格式：`config_json` 或 `metadata_csv` |
| `partition.path` | 分区元数据文件路径 |
| `output.directory` | 运行结果的父目录 |

相对路径以配置文件所在目录为基准。旧配置省略 `engine` 和 `source.log_format` 时，继续使用 ADB 分析和旧 33 列格式。

数据库密码可通过环境变量 `SQL_LOG_MYSQL_PASSWORD` 提供，也可在运行时按提示输入；此时配置中的 `password` 保持 `null`。

### 日志输入

目前实际读取仍使用旧 33 列适配器，CSV 第一行为表头，各记录的字段位置保持原有约定：

| 内容 | 列位置（从 1 开始） | 默认数据库列名 |
| --- | --- | --- |
| 日志时间 | 1 | `column1` |
| 用户名 | 3 | `column3` |
| 日志语句类型 | 19 | `column19` |
| SQL 文本 | 22 | `column22` |

数据库默认读取 `column1` 至 `column33`。如使用 `source.mysql.columns` 指定实际列名，必须按旧格式顺序完整列出 33 个不同字段。输入适配器保持已有的 `statement:` / `execute …: ` 前缀处理和时间文本规则。

通用格式和字段映射暂未启用。后续格式通过实现 `LogFormat` 转换为标准 `LogEvent`，不需要改写公共批处理和统计流程。

### 分区元数据

现有 ADB 元数据 CSV 的表头为 `SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD`，可转换为分区配置：

```bash
PYTHONPATH=src python -m sql_partition_analysis.metadata_import /path/to/metadata.csv --output config/partition_config.json
```

也可将 `partition.format` 设为 `metadata_csv`，直接读取该文件。分区定义为 `NULL` 的表不参与统计；只有分区字段、没有物理分区边界时，不会推断出已验证的物理分区访问。

## 运行

在已激活的 Conda Python 3.7 环境中执行：

```bash
# 只检查本地配置与依赖，不连接数据库、不读取日志
python run_analysis.py --source csv --config config/config.json --check

# 分析 CSV 日志
python run_analysis.py --source csv --config config/config.json

# 通过 MySQL 协议分析 Doris 日志表中的日志
python run_analysis.py --source mysql --config config/config.json
```

`--source` 仅覆盖本次输入来源，不修改配置文件。MySQL 模式按配置逐表读取，两种来源都按批次转换为 `LogEvent`，共用并行解析、统计和导出。

相同 SQL／用户名组合复用解析，每条日志出现仍完整计数，不去重丢弃执行记录。并行数自动根据可用 CPU 确定。

## 输出与统计口径

每次运行在输出目录创建独立子目录，主要产物包括：

| 产物 | 内容 |
| --- | --- |
| `统计结果.zip` | 分区访问统计、SQL 解析明细、日志映射、配置及访问数据集 |
| `逐条SQL分区识别.jsonl` | 每个 SQL／用户名组合的 SQL 正文、识别依据及未纳入统计的原因 |
| `access_targets.jsonl` | 稳定访问目标及其 SQL 分区证据 |
| `access_observations.jsonl.gz` | 逐条日志与访问目标的关联 |
| `分区访问遗漏告警.jsonl`、`分区访问遗漏检查.json` | 遗漏告警及检查覆盖情况 |
| `run_summary.json` | 运行状态、输入格式、执行引擎与统计摘要 |

仅使用 `run_summary.json` 中 `status=complete` 的结果。失败时保留诊断信息，不发布完整结果包。

同一条日志对同一表、同一分区候选、同一访问类型计一次；不同日志记录仍重复计数。平均访问间隔为 `(末次时间 - 首次时间) / (次数 - 1)`，只有一次访问时为空。统计反映日志及 SQL 的证据范围；没有遗漏告警不表示所有 SQL 都已验证，也不能据此认定日志覆盖完整或物理分区未被访问。

## 架构与扩展接口

```text
CSV 读取器 / MySQL 协议读取器
  → LogFormat：日志格式适配（当前为 AdbLegacy33Format）
  → LogEvent：标准日志事件
  → 公共批处理：上下文汇总、并行调度、逐条证据记录
  → EngineAdapter：SQL 分析与分区语义（当前为 AdbPostgresAdapter）
  → AccessEvidence：统一分区访问证据
  → 公共统计、质量检查与报表导出
```

| 模块 | 职责 |
| --- | --- |
| `ingestion/csv.py`、`ingestion/mysql.py` | 读取来源并按批次交付日志 |
| `ingestion/formats/base.py` | 日志格式接口：投影、表头、记录校验与事件转换 |
| `ingestion/formats/adb_legacy_33.py` | 集中处理旧 33 列的位置和前缀规则 |
| `domain/log_event.py`、`ingestion/state.py` | 标准事件与保留全部出现次数的上下文汇总 |
| `batch_pipeline.py` | 公共批处理、进程池、证据记录、聚合及导出 |
| `csv_pipeline.py`、`mysql_pipeline.py` | 将各来源接入公共流程 |
| `engines/base.py`、`engines/__init__.py` | 引擎接口、能力与选择，明确拒绝未实现能力 |
| `engines/adb_pg.py`、`engines/adb_pg_evidence.py` | 现有 ADB 分析及分区条件推导 |
| `analytics/aggregation.py` | 根据访问证据计算统计，不再解析 SQL |
| `reporting/`、`validation/` | 导出与独立遗漏检查；当前语义检查仍面向 ADB |

新增日志格式时，实现 `LogFormat` 并注册；新增执行引擎时，实现引擎语义、元数据适配和对应的独立检查，再启用其能力。现有 PostgreSQL 词法、常量求值与遗漏检查规则不能直接用于 Doris 或 Hive。

`LogEvent` 中的实例、数据库、会话、执行状态等字段预留给后续扩展。当前分析入口拒绝未实现的非默认上下文，避免静默丢弃后误算。混合引擎输入、跨实例汇总及通用日志格式尚未启用。原有分阶段命令和 Python 服务入口继续兼容。

## 验证

```bash
conda activate sql-partition-py37
python -m unittest discover -s tests -q
```

测试覆盖旧格式兼容、CSV/MySQL 等价、跨批重复日志、并行一致性、异常资源释放及未实现能力的拒绝执行。涉及真实 Doris 连接的测试另行按环境运行；普通单元测试不连接数据库。
