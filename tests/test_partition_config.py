"""End-to-end checks for explicit per-table partition configuration.

Run: python -m unittest discover -s tests -p test_partition_config.py -v
"""

import _support

import json
import csv
from pathlib import Path
import subprocess
import tempfile
import unittest



class PartitionConfigurationTests(unittest.TestCase):
    def run_pipeline(self, tables, statements, user_names=None, non_partitioned_tables=()):
        """Each tuple represents an SQL body and its original log occurrences."""
        with tempfile.TemporaryDirectory(prefix="partition-config-test-") as tmp:
            folder = Path(tmp)
            config = {"version": 1, "tables": tables,
                      "non_partitioned_tables": list(non_partitioned_tables)}
            (folder / "partition_config.json").write_text(json.dumps(config), encoding="utf-8")
            sqls = [
                {"id": f"SQL{i:06d}", "sql": sql, "count": count,
                 "first_time": first, "last_time": last}
                for i, (sql, count, first, last) in enumerate(statements, 1)
            ]
            if user_names is not None:
                self.assertEqual(len(sqls), len(user_names))
                for record, user_name in zip(sqls, user_names):
                    record["user_name"] = user_name
            profile = {"sqls": sqls, "widths": {"test": sum(s["count"] for s in sqls)}}
            (folder / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
            for script in ("analyze.py", "aggregate.py"):
                result = subprocess.run(
                    _support.stage_command(script), cwd=folder,
                    env=_support.subprocess_env(SQL_ANALYSIS_OUTPUT_DIR=str(folder),
                         SQL_ANALYSIS_PARTITION_CONFIG=str(folder / "partition_config.json")),
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, f"{script}\n{result.stdout}\n{result.stderr}")
            return (
                json.loads((folder / "analysis.json").read_text(encoding="utf-8")),
                json.loads((folder / "aggregate.json").read_text(encoding="utf-8")),
            )

    @staticmethod
    def record(sql, count=3, first="2026-10-01 00:00:00", last="2026-10-01 00:00:20"):
        return sql, count, first, last

    def test_same_column_on_two_tables_uses_each_tables_configuration(self):
        analysis, aggregate = self.run_pipeline(
            {"s.partitioned": {"partition_columns": ["data_dt"]}},
            [self.record("SELECT * FROM s.partitioned p JOIN s.ordinary o ON p.id = o.id "
                         "WHERE p.data_dt = '2026-09-01' AND o.data_dt = '2026-09-02'")],
            non_partitioned_tables=["s.ordinary"],
        )
        self.assertEqual({p["table"] for p in analysis["predicates"]}, {"s.partitioned"})
        by_table = {r["table"]: r for r in aggregate["results"]}
        self.assertEqual(by_table["s.partitioned"]["partition"], "data_dt=2026-09-01")
        self.assertEqual(set(by_table), {"s.partitioned"})
        self.assertEqual({r["count"] for r in by_table.values()}, {3})

    def test_configured_in_set_expands_with_original_log_counts(self):
        analysis, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["row_date"]}},
            [self.record("SELECT * FROM s.events WHERE row_date IN ('2026-09-01', '2026-09-02') "
                         "AND data_dt = '2026-09-03'", 4,
                         last="2026-10-01 00:00:30")],
        )
        self.assertEqual({p["field"] for p in analysis["predicates"]}, {"row_date"})
        rows = aggregate["results"]
        self.assertEqual({r["partition"] for r in rows},
                         {"row_date=2026-09-01", "row_date=2026-09-02"})
        self.assertEqual({r["count"] for r in rows}, {4})
        self.assertEqual({r["mean_seconds"] for r in rows}, {10})
        self.assertEqual(aggregate["metrics"]["access_log_count"], 4)

    def test_explicit_alter_does_not_override_configuration(self):
        analysis, aggregate = self.run_pipeline({}, [self.record(
            "ALTER TABLE s.events ADD PARTITION p20260901 "
            "START ('2026-09-01') END ('2026-09-02')"
        )], non_partitioned_tables=["s.events"])
        self.assertEqual(analysis["predicates"], [])
        self.assertEqual(aggregate["results"], [])
        self.assertEqual(aggregate["metrics"]["access_log_count"], 0)

    def test_projection_alias_resolves_to_configured_base_column(self):
        analysis, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["data_dt"]}},
            [self.record("SELECT * FROM (SELECT data_dt AS business_key FROM s.events) q "
                         "WHERE q.business_key = '2026-09-01'")],
        )
        self.assertEqual({(p["table"], p["field"]) for p in analysis["predicates"]},
                         {("s.events", "data_dt")})
        self.assertEqual(len(aggregate["results"]), 1)
        self.assertEqual(aggregate["results"][0]["partition"], "data_dt=2026-09-01")

    def test_optional_quotes_merge_visits_and_match_partition_config(self):
        analysis, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["data_dt"]}},
            [self.record('SELECT * FROM "s"."events" WHERE data_dt = \'2026-09-01\'', 3),
             self.record("SELECT * FROM S.EVENTS WHERE data_dt = '2026-09-01'", 2,
                         "2026-10-01 00:00:30", "2026-10-01 00:00:40"),
             self.record('SELECT * FROM "s"."Events"', 1)],
            non_partitioned_tables=["s.Events"],
        )
        rows = {r["table"]: r for r in aggregate["results"]}
        self.assertEqual(set(rows), {"s.events"})
        self.assertEqual(rows["s.events"]["partition"], "data_dt=2026-09-01")
        self.assertEqual(rows["s.events"]["count"], 5)
        self.assertEqual(rows["s.events"]["mean_seconds"], 10)

    def test_cte_is_not_treated_as_a_physical_table(self):
        analysis, aggregate = self.run_pipeline({"shared.customers": {"partition_columns": ["data_dt"]}}, [self.record(
            "WITH recent AS (SELECT * FROM events) "
            "SELECT * FROM recent r JOIN shared.customers c ON r.id = c.id"
        )], user_names=["rdm"])
        self.assertEqual({r["table"] for r in analysis["details"]},
                         {"[未指定schema].events", "shared.customers"})
        self.assertEqual({r["table"] for r in aggregate["results"]}, {"shared.customers"})

    def test_unqualified_and_explicit_references_remain_separate(self):
        analysis, aggregate = self.run_pipeline({"rdm.events": {"partition_columns": ["data_dt"]}}, [self.record(
            "SELECT * FROM events a JOIN rdm.events b ON a.id = b.id", 3
        )], user_names=["rdm"])
        self.assertEqual({r["table"] for r in analysis["details"]},
                         {"[未指定schema].events", "rdm.events"})
        rows = {r["table"]: r for r in aggregate["results"]}
        self.assertEqual(set(rows), {"rdm.events"})
        self.assertEqual((rows["rdm.events"]["count"], rows["rdm.events"]["mean_seconds"]), (3, 10))
        self.assertEqual(aggregate["metrics"]["access_log_count"], 3)

    def test_profile_groups_identical_sql_by_actual_log_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'outputs' / 'sql_partition_analysis'
            folder.mkdir(parents=True)
            with (Path(tmp) / '湖仓adb服务器执行日志样例.csv').open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['header'])
                for user, second in [('rdm', '00'), ('cdm', '05'), ('rdm', '10')]:
                    row = [''] * 33
                    row[0] = '2026-10-01 00:00:' + second
                    row[2], row[18], row[21] = user, 'SELECT', 'statement: SELECT * FROM events'
                    writer.writerow(row)
            subprocess.run(_support.stage_command('profile'), check=True, capture_output=True,
                           env=_support.subprocess_env(SQL_ANALYSIS_OUTPUT_DIR=str(folder),
                           SQL_ANALYSIS_SOURCE_CSV=str(Path(tmp) / '湖仓adb服务器执行日志样例.csv')))
            result = json.loads((folder / 'profile.json').read_text())['sqls']
            users = {s['user_name']: s for s in result}
            self.assertEqual(set(users), {'rdm', 'cdm'})
            self.assertEqual(users['rdm']['count'], 2)
            self.assertEqual(users['cdm']['count'], 1)
            self.assertEqual(users['rdm']['last_time'], '2026-10-01 00:00:10')

    def test_union_writes_expand_distinct_values_once_per_log(self):
        _, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["data_dt"]}},
            [self.record("INSERT INTO s.events(data_dt) "
                         "SELECT DATE '2026-09-01' UNION ALL "
                         "SELECT DATE '2026-09-02' UNION ALL "
                         "SELECT DATE '2026-09-01'", 4)],
        )
        self.assertEqual({r['partition'] for r in aggregate['results']},
                         {'data_dt=2026-09-01', 'data_dt=2026-09-02'})
        self.assertEqual({r['count'] for r in aggregate['results']}, {4})

    def test_insert_branches_preserve_composite_key_pairs(self):
        _, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["data_dt", "region"]}},
            [self.record("INSERT INTO s.events(data_dt,region) "
                         "SELECT DATE '2026-09-01','a' UNION ALL "
                         "SELECT DATE '2026-09-02','b'")],
        )
        self.assertEqual({r['partition'] for r in aggregate['results']},
                         {'data_dt=2026-09-01 AND region=a',
                          'data_dt=2026-09-02 AND region=b'})

    def test_insert_values_are_alternative_writes(self):
        _, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["data_dt"]}},
            [self.record("INSERT INTO s.events(data_dt) VALUES "
                         "(DATE '2026-09-01'),(DATE '2026-09-02'),(DATE '2026-09-01')")],
        )
        self.assertEqual({r['partition'] for r in aggregate['results']},
                         {'data_dt=2026-09-01', 'data_dt=2026-09-02'})
        self.assertEqual({r['count'] for r in aggregate['results']}, {3})

    def test_except_is_not_expanded_as_union_writes(self):
        _, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["data_dt"]}},
            [self.record("INSERT INTO s.events(data_dt) "
                         "SELECT DATE '2026-09-01' EXCEPT SELECT DATE '2026-09-01'")],
        )
        self.assertEqual(len(aggregate['results']), 1)
        self.assertIn('分区值未知', aggregate['results'][0]['partition'])

    def test_union_preserves_unresolved_source_field(self):
        _, aggregate = self.run_pipeline(
            {"s.events": {"partition_columns": ["data_dt"]}},
            [self.record("INSERT INTO s.events(data_dt) "
                         "SELECT t.data_dt FROM s.source t UNION ALL "
                         "SELECT t.data_dt FROM s.source t")],
        )
        self.assertEqual(len(aggregate['results']), 1)
        self.assertEqual(aggregate['results'][0]['partition'], 'data_dt := t.data_dt')


if __name__ == "__main__":
    unittest.main()
