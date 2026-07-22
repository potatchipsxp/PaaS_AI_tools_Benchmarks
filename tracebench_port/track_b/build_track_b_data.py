#!/usr/bin/env python3
"""
build_track_b_data.py

Track B (redesigned, see CHANGELOG_PORT.md) reads through a NEW deterministic
tool, query_trace(trace_id) -- not an LLM-driven SQL sub-agent -- so it no
longer needs an agent-facing, leakage-scrubbed table set like Track A's
`logs` or the earlier (now superseded) build_trace_native.py output. Instead
this is simply a faithful, unmodified copy of the six raw tables
(Event/Edge/Trace/Trace_Source/Operation/Operation_Source), scoped to the
same 27 cases' trace_sets Track A uses -- no deduplication, no relabeling,
no schema reinterpretation. Real column types are preserved (StartTime/
EndTime etc. stay INTEGER) so downstream arithmetic behaves exactly as it
does against tracebench_raw.sqlite itself.

Trace_Source and Operation_Source ARE included here (unlike the
agent-facing DBs), because they carry the trace_set (= fault name) mapping
query_trace.py needs INTERNALLY to group peer events for latency-outlier
comparison. This is safe specifically because the model never gets direct
SQL access to this file -- it only ever calls query_trace(trace_id) and
receives whatever text that function decides to return. The leakage
boundary for Track B is therefore enforced by auditing query_trace()'s
OUTPUT TEXT (see audit_query_trace_leakage.py), not this backing file's
column list.

Run from within tracebench_port/track_b/:
    python build_track_b_data.py
"""

import json
import sqlite3
import sys

sys.path.insert(0, "..")
from select_cases import build_nm_latency_baseline  # noqa: E402

RAW_DB_PATH = "../data/tracebench_raw.sqlite"
OUT_DB_PATH = "data/tracebench_raw_scoped.sqlite"
GROUND_TRUTH_PATH = "../data/ground_truth_trace.json"
NM_BASELINE_PATH = "data/nm_latency_baseline.json"


def load_scope_trace_sets():
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        gt = json.load(f)
    return sorted({c["trace_set"] for c in gt})


def build_event(raw_cur, out_conn, trace_sets):
    out_conn.execute("DROP TABLE IF EXISTS Event")
    out_conn.execute(
        """
        CREATE TABLE Event (
            TaskID varchar(40), TID varchar(40), OpName varchar(255),
            StartTime INTEGER, EndTime INTEGER,
            HostAddress varchar(20), HostName varchar(128),
            Agent varchar(128), Description varchar(255)
        )
        """
    )
    placeholders = ",".join("?" * len(trace_sets))
    raw_cur.execute(
        f"""
        SELECT e.TaskID, e.TID, e.OpName, e.StartTime, e.EndTime,
               e.HostAddress, e.HostName, e.Agent, e.Description
        FROM Event e JOIN Trace_Source s ON e.TaskID = s.TraceID
        WHERE s.trace_set IN ({placeholders})
        """,
        trace_sets,
    )
    n = 0
    batch = []
    for row in raw_cur:
        batch.append(row)
        n += 1
        if len(batch) >= 50000:
            out_conn.executemany("INSERT INTO Event VALUES (?,?,?,?,?,?,?,?,?)", batch)
            batch = []
    if batch:
        out_conn.executemany("INSERT INTO Event VALUES (?,?,?,?,?,?,?,?,?)", batch)
    out_conn.commit()
    print(f"  Event: {n:,} rows")


def build_edge(raw_cur, out_conn, trace_sets):
    out_conn.execute("DROP TABLE IF EXISTS Edge")
    out_conn.execute(
        """
        CREATE TABLE Edge (
            TraceID TEXT, FatherNID TEXT, FatherStartTime INTEGER, ChildNID TEXT
        )
        """
    )
    placeholders = ",".join("?" * len(trace_sets))
    raw_cur.execute(
        f"""
        SELECT e.TraceID, e.FatherNID, e.FatherStartTime, e.ChildNID
        FROM Edge e JOIN Trace_Source s ON e.TraceID = s.TraceID
        WHERE s.trace_set IN ({placeholders})
        """,
        trace_sets,
    )
    rows = raw_cur.fetchall()
    out_conn.executemany("INSERT INTO Edge VALUES (?,?,?,?)", rows)
    out_conn.commit()
    print(f"  Edge: {len(rows):,} rows")


def build_trace(raw_cur, out_conn, trace_sets):
    out_conn.execute("DROP TABLE IF EXISTS Trace")
    out_conn.execute(
        """
        CREATE TABLE Trace (
            TaskID varchar(40), Title varchar(128), NumReports INTEGER, NumEdges INTEGER,
            FirstSeen timestamp, LastUpdated timestamp, StartTime INTEGER, EndTime INTEGER
        )
        """
    )
    placeholders = ",".join("?" * len(trace_sets))
    raw_cur.execute(
        f"""
        SELECT t.TaskID, t.Title, t.NumReports, t.NumEdges,
               t.FirstSeen, t.LastUpdated, t.StartTime, t.EndTime
        FROM Trace t JOIN Trace_Source s ON t.TaskID = s.TraceID
        WHERE s.trace_set IN ({placeholders})
        """,
        trace_sets,
    )
    rows = raw_cur.fetchall()
    out_conn.executemany("INSERT INTO Trace VALUES (?,?,?,?,?,?,?,?)", rows)
    out_conn.commit()
    print(f"  Trace: {len(rows):,} rows")


def build_trace_source(raw_cur, out_conn, trace_sets):
    out_conn.execute("DROP TABLE IF EXISTS Trace_Source")
    out_conn.execute("CREATE TABLE Trace_Source (TraceID TEXT PRIMARY KEY, trace_set TEXT)")
    placeholders = ",".join("?" * len(trace_sets))
    raw_cur.execute(
        f"SELECT TraceID, trace_set FROM Trace_Source WHERE trace_set IN ({placeholders})",
        trace_sets,
    )
    rows = raw_cur.fetchall()
    out_conn.executemany("INSERT INTO Trace_Source VALUES (?,?)", rows)
    out_conn.commit()
    print(f"  Trace_Source: {len(rows):,} rows")


def build_operation_raw(raw_cur, out_conn, trace_sets):
    """Copies Operation/Operation_Source RAW -- not deduplicated, unlike the
    superseded build_trace_native.py. query_trace.py computes its own
    peer-latency baselines directly from Event durations (same approach as
    select_cases.py), so these two are kept for reference/completeness only."""
    out_conn.execute("DROP TABLE IF EXISTS Operation_Source")
    out_conn.execute("CREATE TABLE Operation_Source (op_rowid INTEGER, trace_set TEXT)")
    placeholders = ",".join("?" * len(trace_sets))
    raw_cur.execute(
        f"SELECT op_rowid, trace_set FROM Operation_Source WHERE trace_set IN ({placeholders})",
        trace_sets,
    )
    op_source_rows = raw_cur.fetchall()
    out_conn.executemany("INSERT INTO Operation_Source VALUES (?,?)", op_source_rows)
    out_conn.commit()
    print(f"  Operation_Source: {len(op_source_rows):,} rows")

    out_conn.execute("DROP TABLE IF EXISTS Operation")
    out_conn.execute(
        "CREATE TABLE Operation (OpName varchar(40), Num INTEGER, MaxDelay INTEGER, MinDelay INTEGER, AverageDelay double)"
    )
    op_rowids = [r[0] for r in op_source_rows]
    batch_size = 900  # stay under SQLite's default 999-bound-variable limit
    op_rows = []
    for i in range(0, len(op_rowids), batch_size):
        batch = op_rowids[i:i + batch_size]
        ph = ",".join("?" * len(batch))
        raw_cur.execute(
            f"SELECT OpName, Num, MaxDelay, MinDelay, AverageDelay FROM Operation WHERE rowid IN ({ph})",
            batch,
        )
        op_rows.extend(raw_cur.fetchall())
    out_conn.executemany("INSERT INTO Operation VALUES (?,?,?,?,?)", op_rows)
    out_conn.commit()
    print(f"  Operation: {len(op_rows):,} rows (raw, not deduplicated)")


def main():
    trace_sets = load_scope_trace_sets()
    print(f"Scope: {len(trace_sets)} trace_sets\n")

    raw_conn = sqlite3.connect(RAW_DB_PATH)
    out_conn = sqlite3.connect(OUT_DB_PATH)
    raw_cur = raw_conn.cursor()

    print(f"Copying raw tables (unmodified, scoped only) into {OUT_DB_PATH}")
    build_event(raw_cur, out_conn, trace_sets)
    build_edge(raw_cur, out_conn, trace_sets)
    build_trace(raw_cur, out_conn, trace_sets)
    build_trace_source(raw_cur, out_conn, trace_sets)
    build_operation_raw(raw_cur, out_conn, trace_sets)

    out_cur = out_conn.cursor()
    out_cur.execute("SELECT COUNT(DISTINCT TaskID) FROM Event")
    print(f"\nDistinct TaskID in Event: {out_cur.fetchone()[0]:,}")

    # Precompute the cross-condition NM (normal) latency baseline from the FULL
    # raw corpus (all 364 trace_sets), NOT this scoped copy -- matching exactly
    # what Track A's build_trace_logs.py used for its own WARN derivation. If
    # query_trace.py computed this from only the 7 scoped NM trace_sets instead,
    # its cluster-wide-deviation signal would be measurably noisier than Track
    # A's purely because of an architecture choice, not a real track difference.
    # Cached here so query_trace.py never needs to touch the 2.9GB raw file.
    print("\nPrecomputing NM latency baseline from the FULL raw corpus (matches Track A)...")
    nm_baseline = build_nm_latency_baseline(raw_cur)
    with open(NM_BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(nm_baseline, f, indent=2)
    print(f"  {len(nm_baseline)} OpNames baselined -> {NM_BASELINE_PATH}")

    raw_conn.close()
    out_conn.close()
    print(f"\nDone. Track B raw-scoped DB at {OUT_DB_PATH}")


if __name__ == "__main__":
    main()
