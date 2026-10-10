# ADB-Partition-Insight

从 CSV 或 MySQL 协议数据库读取 SQL 日志，统计表分区的访问类型、次数和平均访问间隔。只统计分区清单内明确有分区的表，不补 schema；重复日志保留，无法确定的分区标注未知。

## 安装

使用 Python 3.7，安装固定版本依赖：

```bash
python -m pip install -r requirements.txt
```

版本统一以 `requirements.txt` 中的固定版本为准；修改版本后需重新执行上述安装命令。`--check` 会核对并显示实际安装的 SQLGlot 版本。

## 配置与运行

首次运行先创建本地配置，再编辑 `config/config.json`：

```bash
cp config/config.example.json config/config.json
```

- `source.csv.path`：输入 CSV 路径。
- `source.mysql`：数据库连接信息；`table` 填日志表名列表，按整表读取并合并统计。
- `source.mysql.batch_rows`：每批读取行数，默认 5,000，允许 1 至 100,000。
- `partition.path`：分区清单路径。
- `output.directory`：结果保存目录。

相对路径以配置文件所在目录为基准。MySQL 密码可填写在 `password` 中；为 `null` 时读取 `SQL_LOG_MYSQL_PASSWORD` 环境变量或提示输入。并行数自动根据可用 CPU 确定。

```bash
python run_analysis.py --source csv --config config/config.json
python run_analysis.py --source mysql --config config/config.json
```

追加 `--check` 可只检查配置和依赖。每次运行在输出目录下生成独立子目录，包含 CSV 和 `统计结果.zip`。

CSV 使用标准 33 列日志格式，时间、用户名、SQL 分别在第 1、3、22 列；MySQL 按 `column1` 至 `column33` 读取。

MySQL 使用独立的批次处理链路：流式读取一批日志后立即累计次数、时间并解析首次出现的 SQL/用户组合，处理完再读取下一批；不生成 `源日志.csv`，不调用 CSV 的扫描或阶段脚本。所有配置表只读取一遍，跨批次和跨表的重复日志仍完整计数。SQL 解析、分区统计规则和报表格式由两种模式共享。

MySQL 在内存中保存不同 SQL/用户组合及其解析结果；原始 33 列数据仅保留当前批次。为生成逐条映射并核对统计，临时压缩保存命中目标表的记录编号、时间和 SQL 编号，导出完成或失败时清理。最终逐条映射和访问证据仍会随命中日志条数增长；内存则随不同 SQL/用户组合数增长。

另输出 `access_targets.jsonl`（访问对象）、`access_observations.jsonl.gz`（逐条访问时间及对象关联）、`access_manifest.json`（数据版本及来源），供后续热点和未访问天数分析复用。仅使用 `run_summary.json` 中 `status=complete` 的结果；分区条件尚不等同于物理分区，日志连续覆盖情况标为未知。

CSV 和 MySQL 每次分析默认在本次运行目录生成 `逐条SQL分区识别.jsonl`。每行是一个 SQL/日志用户名组合，包含 SQL 正文、出现次数、解析状态、表引用、识别出的分区条件和判断依据，也保留未纳入统计的 SQL 及排除原因。重复日志通过 `log_count` 累计，分区条件仍表示 SQL 证据。

该文件复用本次解析结果，不额外读取源日志或重新解析。JSONL 单独保存在运行目录，不加入 `统计结果.zip`；仅使用 `run_summary.json` 中 `status=complete` 的运行结果。

解析兼容 Greenplum 建表尾部的 `DISTRIBUTED BY / RANDOMLY / REPLICATED` 和 `values` 别名，按语法位置处理，保留注释及字符串内容。日志中的完整标识符双重引号编码仅在原文解析失败时尝试兼容还原，并记录依据；JSONL 中仍保存原始 SQL。COPY 正文缺失时只保留明确的对象和读写方向，分区值保持未知。

常量求值保留 SQL 类型：未声明类型的日期形字符串不自动改写日期；明确的 DATE、无时区 TIMESTAMP 和受支持的转换格式按其语义求值，时间部分不会丢失。依赖字段类型、排序规则、DateStyle 或 TimeZone 的表达式保留完整条件，不据此判定分区冲突。异常执行消息中的 SQL 也保留识别证据，但不计入访问统计。

每次运行还会独立检查原始 SQL 中可确定的分区条件或操作对象，核对最终访问映射和统计组。检查逻辑不调用主提取器的预处理、字段归属、常量求值或聚合函数。发现遗漏时在控制台输出概要，并生成 `分区访问遗漏告警.jsonl`，包含原 SQL、表引用、预期分区证据、访问类型和实际结果；分析继续运行。

独立检查覆盖本地 WHERE 中单字段等值和有限 IN、显式目标列 INSERT VALUES、静态 UPDATE 赋值及 ALTER 明确分区对象；支持可证明的 INTEGER/NUMERIC/TEXT 类型转换和无来源单行 SELECT/VALUES 的 UPDATE 元组赋值。依赖会话设置、整数溢出、精度或舍入的表达式，以及无法证明行数的子查询，保留为未确定值。

`分区访问遗漏检查.json` 记录检查覆盖范围、跳过原因和告警数量。检查只覆盖能够独立证明的语法子集，空告警不代表所有 SQL 都已验证；动态条件、归属不明的复杂查询、清单外对象、异常执行消息和缺少正文的 COPY 等不会补猜取值。两个诊断文件单独保存在运行目录；`统计结果.zip` 内的 `run_summary.json` 也会记录告警数量。

## 分区清单

原始清单（如 `query_result.txt`）需为 CSV 格式，表头为 `SCHEMA_NAME,TAB_NAME,TAB_PARTI_FIELD`；分区定义为 `NULL` 的表不参与统计。

在项目根目录执行以下命令，将清单转换为 JSON（输入路径按实际位置替换）：

```bash
PYTHONPATH=src python -m sql_partition_analysis.metadata_import /path/to/query_result.txt --output config/partition_config.json
```

生成的 JSON 对应 `partition.format: "config_json"`。也可直接读取原始清单，分析时自动转换：

```json
"partition": {
  "format": "metadata_csv",
  "path": "/path/to/query_result.txt"
}
```

## 项目结构

代码位于 `src/sql_partition_analysis/`：`ingestion/` 读取日志，`partitioning/` 管理分区清单和 SQL 解析，`domain/` 定义访问数据格式，`analytics/` 负责统计，`reporting/` 负责导出。`pipeline.py` 选择来源并发布结果；`mysql_pipeline.py` 编排 MySQL 批次处理，`stages/` 适配 CSV 的命令行阶段。

后续热点与清理建议放在 `analytics/`，复用访问数据；清理执行应作为独立模块接入。当前不执行分区删除。

输入数据、分区清单、本地连接配置和运行结果均不纳入 Git，仅提交通用配置模板。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py'
```
