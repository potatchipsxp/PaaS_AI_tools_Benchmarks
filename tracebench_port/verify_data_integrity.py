#!/usr/bin/env python3
"""
verify_data_integrity.py
-------------------------
Independent spot-check: re-derive facts straight from the raw TraceBench
.sql dump for a sample of already-selected ground-truth cases, and diff
against tracebench_raw.sqlite / tracebench_manifest.csv / ground_truth_trace.json.

Deliberately does NOT reuse load_tracebench.clean_statement() or
load_sql_file() — those implement the load path this script exists to
check, and reusing them would make the check circular. Only
split_sql_statements() (a pure tokenizer) is imported; tuple-splitting and
field extraction are reimplemented fresh here.

Usage:
  python3 verify_data_integrity.py                # default 4-case sample
  python3 verify_data_integrity.py --case-ids TB-001,TB-013
  python3 verify_data_integrity.py --all           # sweep all admitted cases

Exit code is nonzero if any check fails, so this doubles as a regression
guard after future changes to the loader/manifest/selection scripts.
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict

from load_tracebench import split_sql_statements
from build_manifest import parse_trace_set

SQL_DIR = "data/raw_sql/mtracer-TraceBench-44b29e5"
DB_PATH = "data/tracebench_raw.sqlite"
GROUND_TRUTH_PATH = "data/ground_truth_trace.json"
REJECT_LOG_PATH = "data/tracebench_load_rejects.log"

DEFAULT_SAMPLE = ["TB-013", "TB-018", "TB-007", "TB-021"]  # tier1/2/3/0, one per evidence_type
ERROR_PATTERN = re.compile(
    r"xception|rror|ail|efused|imeout|orrupt|hecksum|\bCRC\b|"
    r"eset by peer|o route to host|not known",
    re.IGNORECASE,
)

results = []  # (case_id, check_name, passed, detail)


def record(case_id, check, passed, detail=""):
    results.append((case_id, check, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {check}" + (f" — {detail}" if detail else ""))


def split_tuples(blob):
    """Minimal, independent '(...),( ...)' splitter — deliberately not
    imported from load_tracebench, to keep this check standalone."""
    tuples, buf, depth = [], [], 0
    in_str = False
    i, n = 0, len(blob)
    while i < n:
        ch = blob[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                buf.append(ch); buf.append(blob[i + 1]); i += 2; continue
            if ch == "'":
                if i + 1 < n and blob[i + 1] == "'":
                    buf.append("''"); i += 2; continue
                in_str = False
            buf.append(ch); i += 1; continue
        if ch == "'":
            in_str = True; buf.append(ch); i += 1; continue
        if ch == "(":
            depth += 1; buf.append(ch); i += 1; continue
        if ch == ")":
            depth -= 1; buf.append(ch); i += 1
            if depth == 0:
                tuples.append("".join(buf)); buf = []
            continue
        if ch == "," and depth == 0:
            i += 1; continue
        buf.append(ch); i += 1
    return tuples


def split_fields(tuple_text):
    """tuple_text includes surrounding parens, e.g. ('a','b',1,2)."""
    inner = tuple_text.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    fields, buf, in_str = [], [], False
    i, n = 0, len(inner)
    while i < n:
        ch = inner[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                buf.append(ch); buf.append(inner[i + 1]); i += 2; continue
            if ch == "'":
                if i + 1 < n and inner[i + 1] == "'":
                    buf.append("'"); i += 2; continue
                in_str = False; i += 1; continue
            buf.append(ch); i += 1; continue
        if ch == "'":
            in_str = True; i += 1; continue
        if ch == "," :
            fields.append("".join(buf)); buf = []; i += 1; continue
        buf.append(ch); i += 1
    fields.append("".join(buf))
    return [f.strip() for f in fields]


def load_raw_inserts(filepath, table_name):
    """Return list of field-tuples for every 'INSERT INTO <table_name>
    VALUES (...)' statement in the raw file, parsed independently."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    statements = split_sql_statements(raw)
    pat = re.compile(rf"^INSERT INTO\s+`?{table_name}`?\s+VALUES\s*(.*)$", re.IGNORECASE | re.DOTALL)
    rows = []
    for stmt in statements:
        m = pat.match(stmt.strip())
        if not m:
            continue
        for t in split_tuples(m.group(1)):
            rows.append(split_fields(t))
    return rows


def count_rejects_for_file(filename, table_hint):
    try:
        with open(REJECT_LOG_PATH, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return 0
    return len(re.findall(rf"{re.escape(filename)}.*?(?:ERROR|ROW ERROR)", text))


def check_case(cur, case, conn):
    case_id = case["case_id"]
    trace_set = case["trace_set"]
    trace_id = case["trace_id"]
    filepath = f"{SQL_DIR}/{trace_set}"
    print(f"\n=== {case_id} — {trace_set} (trace_id={trace_id}) ===")

    # 1. Raw Task/Report/Edge rows, parsed independently from the file.
    try:
        raw_task_rows = load_raw_inserts(filepath, "Task")
        raw_report_rows = load_raw_inserts(filepath, "Report")
        raw_edge_rows = load_raw_inserts(filepath, "Edge")
    except FileNotFoundError:
        record(case_id, "raw file exists", False, f"not found: {filepath}")
        return

    raw_task_count = len(raw_task_rows)
    raw_report_for_trace = [r for r in raw_report_rows if r[0] == trace_id]

    # 2. Row-count cross-check against Trace_Source / Event.
    cur.execute("SELECT COUNT(*) FROM Trace_Source WHERE trace_set=?", (trace_set,))
    db_trace_count = cur.fetchone()[0]
    reject_count = count_rejects_for_file(trace_set, "Task")
    expected = raw_task_count - reject_count
    record(
        case_id, "Trace row count (raw vs DB, adjusted for logged rejects)",
        db_trace_count == expected,
        f"raw={raw_task_count} rejects={reject_count} expected={expected} db={db_trace_count}",
    )

    cur.execute("SELECT COUNT(*) FROM Event WHERE TaskID=?", (trace_id,))
    db_event_count = cur.fetchone()[0]
    record(
        case_id, "Event row count for this TraceID (raw vs DB)",
        db_event_count == len(raw_report_for_trace),
        f"raw={len(raw_report_for_trace)} db={db_event_count}",
    )

    # 3. Field-by-field Trace row check + trace_set tagging.
    raw_task_row = next((r for r in raw_task_rows if r[0] == trace_id), None)
    cur.execute("SELECT TaskID, Title, NumReports, NumEdges, StartTime, EndTime FROM Trace WHERE TaskID=?", (trace_id,))
    db_task_row = cur.fetchone()
    if raw_task_row and db_task_row:
        raw_cmp = (
            raw_task_row[0], raw_task_row[1], int(raw_task_row[2]), int(raw_task_row[3]),
            int(raw_task_row[6]), int(raw_task_row[7]),
        )
        record(case_id, "Trace field-by-field match (Title/NumReports/NumEdges/Start/End)",
               raw_cmp == db_task_row, f"raw={raw_cmp} db={db_task_row}")
    else:
        record(case_id, "Trace field-by-field match", False, "row missing in raw or DB")

    cur.execute("SELECT trace_set FROM Trace_Source WHERE TraceID=?", (trace_id,))
    row = cur.fetchone()
    record(case_id, "Trace_Source tagging matches source file read",
           bool(row) and row[0] == trace_set, f"db_trace_set={row[0] if row else None}")

    # 4. Evidence verbatim / recomputation check.
    ev_type = case["evidence_type"]
    if ev_type == "description_exception":
        anchor = case["evidence_anchor"]
        found = any(anchor in (r[8] or "") for r in raw_report_for_trace)
        record(case_id, "evidence_anchor is a verbatim substring of a raw Description field",
               found, "" if found else f"anchor={anchor!r} not found in {len(raw_report_for_trace)} raw rows")
    elif ev_type in ("host_latency_outlier", "cluster_latency_deviation"):
        m = re.search(r"^(\S+.*?) averages ([\d,]+)ns for (.+?) across", case["evidence_anchor"])
        if m:
            host_or_op = m.group(1)
            claimed_avg = int(m.group(2).replace(",", ""))
            op = m.group(3)
            # recompute directly from raw rows for this trace_set (host_latency case: host_or_op is a host)
            raw_rows_for_op_host = [
                r for r in raw_report_rows
                if r[2] == op and r[6] == host_or_op
            ]
            if raw_rows_for_op_host:
                durs = [int(r[4]) - int(r[3]) for r in raw_rows_for_op_host]
                recomputed_avg = sum(durs) / len(durs)
                close = abs(recomputed_avg - claimed_avg) / max(claimed_avg, 1) < 0.05
                record(case_id, "latency evidence recomputes from raw file within 5%",
                       close, f"claimed={claimed_avg:,.0f} recomputed={recomputed_avg:,.0f} n={len(durs)}")
            else:
                record(case_id, "latency evidence recomputes from raw file", False,
                       f"no raw rows found for host/op {host_or_op!r}/{op!r} (evidence_anchor format may differ)")
        else:
            record(case_id, "latency evidence_anchor parseable", False, case["evidence_anchor"])
    elif ev_type == "clean_normal":
        error_hits = [r for r in raw_report_for_trace if ERROR_PATTERN.search(r[8] or "")]
        record(case_id, "no error text in raw Description fields (independent negative check)",
               len(error_hits) == 0, f"found {len(error_hits)} matches" if error_hits else "")

    # 5. Edge sanity check — column order (TraceID, FatherNID, FatherStartTime, ChildNID).
    if raw_edge_rows:
        sample = raw_edge_rows[0]
        cur.execute(
            "SELECT TraceID, FatherNID, FatherStartTime, ChildNID FROM Edge "
            "WHERE TraceID=? AND FatherNID=? LIMIT 1",
            (sample[0], sample[1]),
        )
        db_edge = cur.fetchone()
        record(case_id, "Edge column order sanity (sample raw tuple found in DB)",
               db_edge is not None, f"raw_sample={tuple(sample)} db={db_edge}")

    # 6. Manifest parse regression check.
    parsed = parse_trace_set(trace_set)
    manifest_ok = (
        parsed["fault_name"] == case["fault_name"]
        and str(parsed["tier"] or 0) == str(case["tier"])
        and parsed["is_anomalous"] == case["is_anomalous"]
    )
    record(case_id, "build_manifest.parse_trace_set() matches ground_truth fields",
           manifest_ok, f"parsed={parsed}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-ids", help="comma-separated case_ids, e.g. TB-001,TB-013")
    ap.add_argument("--all", action="store_true", help="run every admitted case")
    args = ap.parse_args()

    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        ground_truth = json.load(f)
    by_id = {c["case_id"]: c for c in ground_truth}

    if args.all:
        sample_ids = list(by_id.keys())
    elif args.case_ids:
        sample_ids = [c.strip() for c in args.case_ids.split(",")]
    else:
        sample_ids = DEFAULT_SAMPLE

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for cid in sample_ids:
        case = by_id.get(cid)
        if not case:
            print(f"\n=== {cid} === \n  [FAIL] unknown case_id")
            results.append((cid, "case_id exists in ground_truth_trace.json", False, ""))
            continue
        check_case(cur, case, conn)

    conn.close()

    total = len(results)
    passed = sum(1 for r in results if r[2])
    print(f"\n{'=' * 60}\n{passed}/{total} checks passed across {len(sample_ids)} case(s)")
    failures = [r for r in results if not r[2]]
    if failures:
        print("\nFAILURES:")
        for case_id, check, _, detail in failures:
            print(f"  {case_id}: {check} — {detail}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
