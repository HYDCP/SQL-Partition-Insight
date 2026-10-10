# ADB-Partition-Insight

从 CSV 或 MySQL 协议数据库读取 SQL 日志，识别表和分区访问，统计访问类型、次数及平均访问间隔，并导出报表和逐条 SQL 识别结果。

只统计分区清单中明确配置的分区表；不自动补全 schema，无法确定的分区保留为未知。结果反映日志中的 SQL 证据，不代表数据库实际扫描的物理分区。

## 安装

使用 Python 3.7，可通过 conda 创建环境：

```bash
conda create -n adb-partition python=3.7
conda activate adb-partition
python -m pip install -r requirements.txt
```

## 配置

在项目根目录复制配置模板：

```bash
cp config/config.example.json config/config.json
```

编辑 `config/config.json`：

| 配置项 | 用途 |
| --- | --- |
| `source.csv.path` | CSV 日志文件路径 |
| `source.mysql` | 数据库连接信息；`table` 为需要读取的日志表名列表 |
| `source.mysql.batch_rows` | MySQL 每批读取行数，默认 5,000 |
| `partition.path` | 分区清单路径 |
| `output.directory` | 结果保存目录 |

相对路径以配置文件所在目录为基准。MySQL 密码可通过环境变量 `SQL_LOG_MYSQL_PASSWORD` 提供，也可在运行时按提示输入；此时配置中的 `password` 保持 `null`。

CSV 须为标准 33 列日志格式，时间、用户名和 SQL 分别在第 1、3、22 列；MySQL 日志表对应 `column1` 至 `column33`。

分区清单可由表头为 `SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD` 的元数据 CSV 转换生成：

```bash
PYTHONPATH=src python -m sql_partition_analysis.metadata_import /path/to/metadata.csv --output config/partition_config.json
```

也可将 `partition.format` 设为 `metadata_csv`，并让 `partition.path` 直接指向元数据 CSV。分区定义为 `NULL` 的表不参与统计。

## 运行

```bash
# 检查配置和依赖
python run_analysis.py --source csv --config config/config.json --check

# 分析 CSV 日志
python run_analysis.py --source csv --config config/config.json

# 分析 MySQL 日志
python run_analysis.py --source mysql --config config/config.json
```

MySQL 模式按批次读取配置中的日志表，重复日志完整计数。并行数自动根据可用 CPU 确定。

每次运行会在输出目录创建独立子目录，主要结果包括：

- `统计结果.zip`：分区访问统计、SQL 解析明细及逐条日志映射等报表。
- `逐条SQL分区识别.jsonl`：每个 SQL／用户名组合的原文、识别依据及未纳入统计的原因。
- `分区访问遗漏告警.jsonl`、`分区访问遗漏检查.json`：遗漏告警及检查覆盖情况；无告警不代表所有 SQL 均已验证。

仅使用 `run_summary.json` 中 `status` 为 `complete` 的运行结果。
