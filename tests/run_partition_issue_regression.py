"""Opt-in offline regression of audited SQL/user contexts from an existing run.

Uses the archived profile and partition catalog plus the original recognition
JSONL. It never reads credentials, reconnects to a database, reconstructs raw
log observations, or proves that an execution succeeded. Candidate text in the
rerun export is the unformatted aggregation output, including raw_partition.
"""

import _support

import argparse
import collections
import copy
import hashlib
import json
from pathlib import Path
import sys
import zipfile

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from sql_partition_analysis.analytics.aggregation import aggregate_accesses
from sql_partition_analysis.partitioning.analyzer import analyze_profile
from sql_partition_analysis.partitioning.catalog import PartitionCatalog
from sql_partition_analysis.partitioning.parser import parse_one_postgres
from sql_partition_analysis.reporting.sql_partitions import export_sql_partitions, OUTPUT_NAME


RUN = "20261009_165815_e382b941"
ROOT = _support.PROJECT_ROOT
ID_KEYS = frozenset(("sql_id", "sql_ids", "affected_ids"))
META_KEYS = ("user_name", "first_record", "first_time", "last_time", "sql")
GROUP_FINDINGS = {
    "explicit_omissions": ("CQ001", "CQ002", "CQ003", "WD-TARGET_FILTER_OWNERSHIP"),
    "copy_direction": ("WD-COPY_DIRECTION",),
    "quote_encoding": ("ROOT-QUOTE-DIAGNOSTIC", "WD-COPY_ENCODED_TARGET_EXCLUDED"),
    "literal_types": ("SIMPLE-TYPE-001", "WD-LITERAL_TYPE_COERCION", "CQ004"),
    "failed_statement_wrappers": ("SIMPLE-EVIDENCE-001",),
}


def collect_ids(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ID_KEYS:
                members = [item] if isinstance(item, str) else item if isinstance(item, list) else []
                result.update(member for member in members if isinstance(member, str) and member.startswith("SQL"))
            result.update(collect_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(collect_ids(item))
    return result


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_archive_json(archive, basename):
    names = [name for name in archive.namelist() if Path(name).name == basename]
    if len(names) != 1:
        raise ValueError("Archive must contain exactly one " + basename)
    return json.loads(archive.read(names[0])), names[0]


def read_selected_jsonl(path, ids):
    result = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            sid = row["sql_id"]
            if sid in ids:
                if sid in result:
                    raise ValueError("Duplicate SQL/user context: " + sid)
                result[sid] = row
    if set(result) != ids:
        raise ValueError("Missing original contexts: " + ", ".join(sorted(ids - set(result))))
    return result


def load_inputs(args):
    findings = read_json(args.findings)
    ids = collect_ids(findings)
    finding_ids = {item["finding_id"]: collect_ids(item) for item in findings}
    with zipfile.ZipFile(args.archive) as archive:
        original, profile_member = read_archive_json(archive, "profile.json")
        catalog, catalog_member = read_archive_json(archive, "partition_config.json")
    records = [copy.deepcopy(row) for row in original["sqls"] if row["id"] in ids]
    if len(records) != len(ids) or {row["id"] for row in records} != ids:
        raise ValueError("The archived profile does not uniquely contain the problem contexts")
    baseline = read_selected_jsonl(args.baseline, ids)
    for record in records:
        old = baseline[record["id"]]
        if old["log_count"] != record["count"] or any(old[key] != record[key] for key in META_KEYS):
            raise ValueError("Archive/JSONL metadata mismatch for " + record["id"])
    count = sum(row["count"] for row in records)
    groups = {name: set().union(*(finding_ids.get(fid, set()) for fid in members))
              for name, members in GROUP_FINDINGS.items()}
    profile = {
        "source": copy.deepcopy(original["source"]), "header": copy.deepcopy(original["header"]),
        "widths": {"selected_sql_context_logs": count}, "types": {}, "bad": [], "sqls": records,
        "rerun_scope": {
            "kind": "audited_sql_user_context_subset", "original_context_count": len(original["sqls"]),
            "original_source_rows": sum(original["widths"].values()),
            "selected_contexts": len(records), "selected_source_rows": count,
            "source_rows_basis": "sum of archived selected context counts; original logs were not reread",
            "types_basis": "each context retains its original types list; per-type raw log counts cannot be reconstructed",
            "raw_observations_reconstructed": False, "execution_success_verified": False,
        },
    }
    metadata = {
        "findings": str(args.findings.resolve()), "baseline": str(args.baseline.resolve()),
        "archive": str(args.archive.resolve()), "profile_member": profile_member, "catalog_member": catalog_member,
        "finding_sha256": hashlib.sha256(args.findings.read_bytes()).hexdigest(),
        "selected_contexts": len(records), "selected_source_rows": count,
        "groups": {name: {"contexts": len(members),
                          "source_rows": sum(baseline[sid]["log_count"] for sid in members),
                          "sql_ids": sorted(members)} for name, members in groups.items()},
        "finding_context_counts": {key: len(members) for key, members in finding_ids.items()},
        "original_profile_metadata": {key: value for key, value in original.items() if key != "sqls"},
        "metadata_check": "archive profile and original recognition JSONL agree for every selected context",
    }
    return findings, finding_ids, groups, profile, catalog, baseline, metadata


def canonical_condition(text):
    try:
        node = normalize_identifiers(parse_one_postgres(text), dialect="postgres")
        return node.transform(lambda item: exp.column(item.name) if isinstance(item, exp.Column) else item).sql(dialect="postgres")
    except Exception:
        return text


def table_rows(record, table, alias=None):
    return [row for row in record["tables"] if row["table"] == table
            and (alias is None or row["alias"] == alias)]


def verify(findings, finding_ids, groups, profile, catalog, baseline, current, analysis, aggregate, metadata, args):
    checks = collections.defaultdict(list)

    def check(sid, group, name, passed, expected=None, observed=None):
        item = {"group": group, "check": name, "passed": bool(passed)}
        if expected is not None:
            item["expected"] = expected
        if observed is not None:
            item["observed"] = observed
        checks[sid].append(item)

    original_records = {row["id"]: row for row in profile["sqls"]}
    details = collections.defaultdict(list)
    for row in analysis["details"]:
        details[row["id"]].append(row)
    mappings = collections.defaultdict(list)
    for row in aggregate["mapping"]:
        mappings[(row["id"], row["occurrence"], row["table"])].append(row)
    for sid, record in current.items():
        original = original_records[sid]
        check(sid, "metadata", "SQL/user/count/timestamps/first_record preserved",
              record["log_count"] == baseline[sid]["log_count"]
              and all(record[key] == baseline[sid][key] for key in META_KEYS))
        check(sid, "metadata", "archived context including types retained",
              original["sql"] == baseline[sid]["sql"] and original["types"] == metadata["context_types"][sid])

    for finding in findings:
        fid = finding["finding_id"]
        if fid in ("CQ001", "CQ002", "CQ003"):
            expected_counts = collections.Counter((row["sql_id"], row["table"]) for row in finding["instances"])
            for (sid, table), count in expected_counts.items():
                rows = table_rows(current[sid], table)
                check(sid, "explicit_omissions", "configured table occurrence count: " + table,
                      len(rows) == count and all(row["included"] for row in rows), count, len(rows))
            for instance in finding["instances"]:
                sid, table, expected = instance["sql_id"], instance["table"], instance["expected"]
                occurrence = instance["expected_occurrence_in_independent_AST"]
                rows = [row for row in table_rows(current[sid], table, instance.get("alias"))
                        if row["occurrence"] == occurrence]
                check(sid, "explicit_omissions", "table/alias/occurrence matches independent audit AST",
                      len(rows) == 1, {"table": table, "alias": instance.get("alias"), "occurrence": occurrence},
                      [{"table": row["table"], "alias": row["alias"], "occurrence": row["occurrence"]} for row in rows])
                candidates = [part["partition"] for row in rows for part in row["recognized_partitions"]]
                for candidate in expected.get("static_read_filter_candidates", []):
                    check(sid, "explicit_omissions", "independent static date candidate recovered at occurrence {}".format(occurrence),
                          candidate in candidates, candidate, candidates)
                evidence = expected.get("condition_evidence", "")
                if evidence:
                    check(sid, "explicit_omissions", "condition evidence recovered for " + table,
                          bool(rows) and any(row["predicates"] for row in rows), evidence,
                          [item["raw"] for row in rows for item in row["predicates"]])
                if not expected.get("static_read_filter_candidates") and "DATE_TRUNC" in evidence:
                    expected_tree = parse_one_postgres(evidence)
                    unknown = [atom for atom in expected_tree.find_all(exp.EQ)
                               if any(isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)) for node in atom.walk())]
                    expected_unknown = [canonical_condition(atom.sql(dialect="postgres")) for atom in unknown]
                    observed_unknown = [canonical_condition(candidate) for candidate in candidates]
                    check(sid, "explicit_omissions", "timezone-dependent DATE_TRUNC keeps complete unknown expression",
                          bool(expected_unknown) and all(expression in observed_unknown for expression in expected_unknown),
                          expected_unknown, observed_unknown)
                if expected.get("write_partition") == "unknown_without_target_column_order":
                    targets = [row for row in rows if row["role"] in ("插入", "建表写入")]
                    candidates = [part["partition"] for row in targets for part in row["recognized_partitions"]]
                    check(sid, "explicit_omissions", "INSERT destination without column order stays unknown",
                          bool(targets) and bool(candidates) and all("未知" in part or "未确定" in part for part in candidates),
                          "unknown", candidates)
        elif fid == "WD-TARGET_FILTER_OWNERSHIP":
            for instance in finding["occurrences"]:
                sid, table = instance["sql_id"], instance["table"]
                targets = [row for row in table_rows(current[sid], table) if row["role"] == "更新"]
                target_conditions = [part["partition"] for row in targets for part in row["recognized_partitions"]]
                check(sid, "explicit_omissions", "unaliased UPDATE target date recovered",
                      bool(targets) and "data_dt=2026-10-02" in target_conditions,
                      "data_dt=2026-10-02", target_conditions)
                sources = [row for row in table_rows(current[sid], table) if row["alias"] == "p1"]
                candidates = [part["partition"] for row in sources for part in row["recognized_partitions"]]
                check(sid, "explicit_omissions", "UPDATE source interval date retains timestamp result",
                      any("2026-10-01 00:00:00" in part for part in candidates),
                      "timestamp 2026-10-01 00:00:00", candidates)
        elif fid in ("WD-COPY_DIRECTION", "WD-COPY_ENCODED_TARGET_EXCLUDED"):
            group = "copy_direction" if fid == "WD-COPY_DIRECTION" else "quote_encoding"
            for instance in finding["occurrences"]:
                sid, table = instance["sql_id"], instance["table"]
                rows = table_rows(current[sid], table)
                check(sid, group, "COPY object and FROM direction recovered",
                      bool(rows) and all(row["role"] == "COPY写入" for row in rows), "COPY写入",
                      [row["role"] for row in rows])
                candidates = [part["partition"] for row in rows for part in row["recognized_partitions"]]
                check(sid, group, "COPY body absent; no partition value invented",
                      all("未知" in part or "未确定" in part for part in candidates), "unknown", candidates)
        elif fid == "WD-LITERAL_TYPE_COERCION":
            for instance in finding["occurrences"]:
                sid, table = instance["sql_id"], instance["table"]
                expected = canonical_condition(instance["expected"]["condition"])
                actual = [canonical_condition(item["condition"]) for row in table_rows(current[sid], table)
                          for item in row["predicates"]]
                check(sid, "literal_types", "bare literal condition preserved for " + table,
                      expected in actual, expected, actual)
                identity_types = []
                for row in table_rows(current[sid], table):
                    matched = [item for item in row["predicates"] if canonical_condition(item["condition"]) == expected]
                    for predicate in matched:
                        for mapping in mappings[(sid, row["occurrence"], table)]:
                            identity = mapping.get("partition_identity", ())
                            if identity and identity[0] == "values":
                                identity_types.extend(item[1] for item in identity[1] if item[0] == predicate["field"])
                check(sid, "literal_types", "finite candidates retain string identity when inferable",
                      all(kind == "string" for kind in identity_types), "string, or conservatively unknown", identity_types)
        elif fid == "SIMPLE-TYPE-001":
            for instance in finding["affected"]:
                sid, table = instance["sql_id"], instance["table"]
                predicates = [item for row in table_rows(current[sid], table) for item in row["predicates"]]
                for field, literal, old_iso in instance["literal_rewrites"]:
                    actual = [item["condition"] for item in predicates if item["field"] == field]
                    check(sid, "literal_types", "raw date-like string preserved for " + table,
                          any(exp.Literal.string(literal).sql() in condition for condition in actual), literal, actual)
        elif fid == "CQ004":
            for instance in finding["instances"]:
                sid, table = instance["sql_id"], instance["table"]
                expected = canonical_condition(instance["expected"])
                actual = [canonical_condition(item["condition"]) for row in table_rows(current[sid], table)
                          for item in row["predicates"]]
                check(sid, "literal_types", "raw compact string preserved for " + table, expected in actual, expected, actual)
        elif fid == "SIMPLE-EVIDENCE-001":
            for instance in finding["affected"]:
                sid, table = instance["sql_id"], instance["table"]
                row = current[sid]
                check(sid, "failed_statement_wrappers", "failure wrapper excluded from access statistics",
                      not row["included_in_statistics"] and all(not detail.get("statistics_eligible", True) for detail in details[sid])
                      and "失败" in row["parse_notes"], False, row["included_in_statistics"])
                if sid == "SQL066311":
                    check(sid, "failed_statement_wrappers", "invalid GROUP BY DESC remains a syntax failure",
                          row["parse_status"] == "解析失败" and not row["tables"], "解析失败", row["parse_status"])
                else:
                    check(sid, "failed_statement_wrappers", "embedded complete table evidence retained",
                          bool(table_rows(row, table)), table, [item["table"] for item in row["tables"]])
                if sid == "SQL006367":
                    check(sid, "failed_statement_wrappers", "truncated COPY direction stays unknown",
                          any(item["role"] == "COPY方向未知" for item in table_rows(row, table)),
                          "COPY方向未知", [item["role"] for item in row["tables"]])

    diagnostic = args.findings.parent / "complex_queries_quote_recovery.json"
    independent_quote_refs = {row["sql_id"]: row["decoded_configured_references"] for row in read_json(diagnostic)}
    for sid in finding_ids.get("ROOT-QUOTE-DIAGNOSTIC", set()):
        actual = collections.Counter(row["table"] for row in current[sid]["tables"] if row["partition_config"])
        expected = independent_quote_refs[sid]
        check(sid, "quote_encoding", "configured references match independent audit decoding",
              dict(actual) == expected, expected, dict(actual))
    for sid in groups["quote_encoding"]:
        check(sid, "quote_encoding", "targeted identifier decoding recorded; original SQL retained",
              "双重引号编码" in current[sid]["parse_notes"] and current[sid]["sql"] == baseline[sid]["sql"],
              "parse-only quote recovery", current[sid]["parse_notes"])

    failed = [{"sql_id": sid, **item} for sid, items in checks.items() for item in items if not item["passed"]]
    comparisons = []
    for sid in sorted(current):
        before, after = baseline[sid], current[sid]
        comparisons.append({
            "sql_id": sid, **{key: original_records[sid][key] for key in META_KEYS},
            "log_count": original_records[sid]["count"], "types": original_records[sid]["types"],
            "finding_ids": sorted(fid for fid, ids in finding_ids.items() if sid in ids),
            "groups": sorted(name for name, ids in groups.items() if sid in ids),
            "before": {key: before[key] for key in ("parse_status", "parse_notes", "included_in_statistics", "tables", "unresolved")},
            "after": {key: after[key] for key in ("parse_status", "parse_notes", "included_in_statistics", "tables", "unresolved")},
            "checks": checks[sid], "passed": all(item["passed"] for item in checks[sid]),
        })
    comparison_path = args.output / "重跑结果对比.jsonl"
    with comparison_path.open("w", encoding="utf-8") as stream:
        for row in comparisons:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    summary = {
        "status": "checks_passed" if not failed else "checks_failed", "scope": profile["rerun_scope"],
        "selected_contexts": len(current), "selected_source_rows": sum(row["log_count"] for row in current.values()),
        "before_parse_statuses": dict(collections.Counter(row["parse_status"] for row in baseline.values())),
        "after_parse_statuses": dict(collections.Counter(row["parse_status"] for row in current.values())),
        "included_contexts_before": sum(row["included_in_statistics"] for row in baseline.values()),
        "included_contexts_after": sum(row["included_in_statistics"] for row in current.values()),
        "table_occurrences_before": sum(len(row["tables"]) for row in baseline.values()),
        "table_occurrences_after": sum(len(row["tables"]) for row in current.values()),
        "checks": sum(len(items) for items in checks.values()), "failed_checks": failed,
        "groups": {name: {"contexts": len(ids), "source_rows": sum(current[sid]["log_count"] for sid in ids),
                          "checks": sum(item["group"] == name for sid in ids for item in checks[sid]),
                          "failed_checks": sum(item["group"] == name and not item["passed"] for sid in ids for item in checks[sid]),
                          "changed_contexts": sum(baseline[sid]["tables"] != current[sid]["tables"] or
                                                  baseline[sid]["parse_status"] != current[sid]["parse_status"] for sid in ids)}
                   for name, ids in groups.items()},
        "remaining_parse_failures": [{"sql_id": sid, "parse_notes": row["parse_notes"]}
                                     for sid, row in current.items() if row["parse_status"] == "解析失败"],
        "conservative_limits": [
            "Only the audited SQL/user context subset was rerun; original raw logs were not reread.",
            "No per-log observations or execution-success evidence were reconstructed.",
            "Identifiers recovered from doubled quotes are parse compatibility evidence, not execution verification.",
            "Missing field types, dynamic expressions and absent COPY bodies do not establish physical partition hits.",
            "SQL066311 GROUP BY DESC is a genuine syntax failure; its table was not guessed from tokens.",
            "SQL006367 has a truncated COPY header; its complete object is retained with unknown direction.",
            "report.mapping.raw_partition equals the unformatted aggregation candidate partition.",
        ],
        "outputs": {"recognition": str((args.output / OUTPUT_NAME).resolve()),
                    "comparison": str(comparison_path.resolve())},
        "source_code_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (ROOT / "src/sql_partition_analysis").rglob("*.py")},
    }
    write_json(args.output / "regression_summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--findings", type=Path, default=ROOT / "outputs/partition_correctness_audit" / RUN / "all_findings.json")
    parser.add_argument("--baseline", type=Path, default=ROOT / "outputs/zip_generation_test" / RUN / OUTPUT_NAME)
    parser.add_argument("--archive", type=Path, default=Path("/Users/xuchenhao/Downloads") / (RUN + ".zip"))
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/partition_fix_rerun" / RUN)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    findings, finding_ids, groups, profile, config, baseline, metadata = load_inputs(args)
    args.output.mkdir(parents=True, exist_ok=True)
    metadata["context_types"] = {row["id"]: copy.deepcopy(row["types"]) for row in profile["sqls"]}
    write_json(args.output / "input_selection.json", metadata)
    write_json(args.output / "profile.json", profile)
    write_json(args.output / "partition_config.json", config)
    if args.prepare_only:
        print(json.dumps({"status": "prepared", "contexts": len(profile["sqls"]),
                          "source_rows": sum(row["count"] for row in profile["sqls"]),
                          "groups": {name: len(ids) for name, ids in groups.items()}}, ensure_ascii=False))
        return 0
    catalog = PartitionCatalog(config)
    print("Analyzing {} audited contexts".format(len(profile["sqls"])), flush=True)
    analysis = analyze_profile(profile, catalog)
    write_json(args.output / "analysis.json", analysis)
    aggregate = aggregate_accesses(profile, analysis, catalog)
    write_json(args.output / "aggregate.json", aggregate)
    report = copy.deepcopy(aggregate)
    for mapping in report["mapping"]:
        mapping["raw_partition"] = mapping["partition"]
    write_json(args.output / "report_data.json", report)
    export_sql_partitions(profile, analysis, report, args.output)
    current = read_selected_jsonl(args.output / OUTPUT_NAME, set(baseline))
    summary = verify(findings, finding_ids, groups, profile, config, baseline, current, analysis, aggregate, metadata, args)
    print(json.dumps({key: summary[key] for key in ("status", "selected_contexts", "selected_source_rows", "after_parse_statuses", "checks", "groups")}, ensure_ascii=False), flush=True)
    print("Failed checks: {}".format(len(summary["failed_checks"])), flush=True)
    return 1 if summary["failed_checks"] else 0


if __name__ == "__main__":
    sys.exit(main())
