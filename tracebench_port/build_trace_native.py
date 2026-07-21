#!/usr/bin/env python3
"""
build_trace_native.py

Phase 4 Track B: load the native Event/Edge/Trace/Operation tables into the
SAME benchmark_trace_db.sqlite that build_trace_logs.py wrote the flat `logs`
table into, scoped to the identical 27 cases' trace_sets -- so Track A and
Track B provably diagnose the same underlying data, differing only in shape
(addendum's "one shared spine, one forked layer").

Deliberate departures from a literal copy of the raw tables (recorded here
and in CHANGELOG_PORT.md, not silently done):

  - Operation is NOT copied as-is. The raw table has ~19 unattributed,
    non-deduplicated rows per OpName per source file (see load_tracebench.py's
    own comments) -- handing that straight to an agent means multiple
    conflicting rows for the same OpName with no way to tell them apart.
    Instead this aggregates one row per OpName from ONLY the Operation_Source
    rows tagged to our 27 scope trace_sets (SUM(Num), MAX(MaxDelay),
    MIN(MinDelay), Num-weighted mean AverageDelay) -- a clean baseline
    consistent with what's actually visible in this DB, not silently
    borrowing stats from out-of-scope trace_sets the agent can't see.

  - Trace_Source / Operation_Source are NEVER copied into this DB. They
    exist purely to carry the trace_set (= fault name) provenance for ground
    truth and would hand the agent the answer directly if exposed.

  - Trace.NumReports / NumEdges are copied as-is (real columns) but the
    Track B schema description explicitly warns the agent not to trust them
    (verified unreliable -- see build_trace_logs.py's docstring).

Run from within tracebench_port/, AFTER build_trace_logs.py.
"""

import json
import sqlite3

RAW_DB_PATH = "data/tracebench_raw.sqlite"
OUT_DB_PATH = "data/benchmark_trace_db.sqlite"
GROUND_TRUTH_PATH = "data/ground_truth_trace.json"


def load_scope_trace_sets():
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        gt = json.load(f)
    return sorted({c["trace_set"] for c in gt})


def build_event_table(raw_cur, out_conn, trace_sets):
    out_conn.execute("DROP TABLE IF EXISTS Event")
    out_conn.execute(
        """
        CREATE TABLE Event (
            TaskID TEXT, TID TEXT, OpName TEXT,
            StartTime INTEGER, EndTime INTEGER,
            HostAddress TEXT, HostName TEXT, Agent TEXT, Description TEXT
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
    return n


def build_edge_table(raw_cur, out_conn, trace_sets):
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
    return len(rows)


def build_trace_table(raw_cur, out_conn, trace_sets):
    out_conn.execute("DROP TABLE IF EXISTS Trace")
    out_conn.execute(
        """
        CREATE TABLE Trace (
            TaskID TEXT, Title TEXT, NumReports INTEGER, NumEdges INTEGER,
            FirstSeen TEXT, LastUpdated TEXT, StartTime INTEGER, EndTime INTEGER
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

    distinct_titles = sorted({r[1] for r in rows if r[1]})
    print(f"  Distinct Trace.Title values ({len(distinct_titles)}): {distinct_titles}")
    return len(rows)


def build_operation_table(raw_cur, out_conn, trace_sets):
    """One deduplicated row per OpName, aggregated ONLY from the scope
    trace_sets' own Operation_Source-tagged rows (see module docstring)."""
    out_conn.execute("DROP TABLE IF EXISTS Operation")
    out_conn.execute(
        """
        CREATE TABLE Operation (
            OpName TEXT PRIMARY KEY, Num INTEGER,
            MaxDelay INTEGER, MinDelay INTEGER, AverageDelay REAL
        )
        """
    )
    placeholders = ",".join("?" * len(trace_sets))
    raw_cur.execute(
        f"""
        SELECT o.OpName,
               SUM(o.Num) AS Num,
               MAX(o.MaxDelay) AS MaxDelay,
               MIN(o.MinDelay) AS MinDelay,
               SUM(o.AverageDelay * o.Num) * 1.0 / SUM(o.Num) AS AverageDelay
        FROM Operation o
        JOIN Operation_Source os ON o.rowid = os.op_rowid
        WHERE os.trace_set IN ({placeholders})
        GROUP BY o.OpName
        """,
        trace_sets,
    )
    rows = raw_cur.fetchall()
    out_conn.executemany("INSERT INTO Operation VALUES (?,?,?,?,?)", rows)
    out_conn.commit()
    print(f"  Operation: {len(rows):,} distinct OpName rows (deduplicated)")
    return len(rows)


def main():
    trace_sets = load_scope_trace_sets()
    print(f"Scope: {len(trace_sets)} trace_sets\n")

    raw_conn = sqlite3.connect(RAW_DB_PATH)
    out_conn = sqlite3.connect(OUT_DB_PATH)
    raw_cur = raw_conn.cursor()

    print("Building Track B native tables in benchmark_trace_db.sqlite...")
    build_event_table(raw_cur, out_conn, trace_sets)
    build_edge_table(raw_cur, out_conn, trace_sets)
    build_trace_table(raw_cur, out_conn, trace_sets)
    build_operation_table(raw_cur, out_conn, trace_sets)

    out_cur = out_conn.cursor()
    out_cur.execute("SELECT COUNT(DISTINCT TaskID) FROM Event")
    print(f"\nDistinct TaskID in Event: {out_cur.fetchone()[0]:,}")
    out_cur.execute("SELECT COUNT(DISTINCT TaskID) FROM Trace")
    print(f"Distinct TaskID in Trace: {out_cur.fetchone()[0]:,}")

    raw_conn.close()
    out_conn.close()
    print(f"\nDone. Track B tables added to {OUT_DB_PATH}")


if __name__ == "__main__":
    main()
