#!/usr/bin/env python3
"""
query_trace.py

The Track B tool: query_trace(trace_id) -> str. Deterministic, no LLM
involved -- reconstructs one trace's per-host timeline plus its Edge
call-structure, annotated with the SAME peer-latency-outlier signal Track A
gets "for free" via its `level=WARN` column (reusing select_cases.py's own
outlier-detection functions, not a new heuristic), so neither track is
handed more pre-digested evidence than the other.

Reads track_b/data/tracebench_raw_scoped.sqlite (raw, unmodified, scoped to
the 27 cases' trace_sets -- see build_track_b_data.py) plus the precomputed
data/nm_latency_baseline.json (built from the FULL raw corpus, matching
Track A's baseline exactly).

LEAKAGE BOUNDARY: this function's OUTPUT TEXT is the thing that must never
contain a trace_set filename, fault name, or fault category -- NOT the
backing DB's column list (Trace_Source is present in the DB for internal
grouping only and is never read into any string built here). See
audit_query_trace_leakage.py, which greps this function's actual return
value for all 27 cases, not the DB schema.

Public API:
    query_trace(trace_id: str) -> str
"""

import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "..")
from select_cases import (  # noqa: E402
    trace_set_op_host_avgs,
    find_host_latency_outlier,
    find_cluster_wide_deviation,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_HERE, "data", "tracebench_raw_scoped.sqlite")
NM_BASELINE_PATH = os.path.join(_HERE, "data", "nm_latency_baseline.json")

_conn_cache = None
_nm_baseline_cache = None


def _get_conn():
    global _conn_cache
    if _conn_cache is None:
        _conn_cache = sqlite3.connect(DB_PATH)
    return _conn_cache


def _get_nm_baseline():
    global _nm_baseline_cache
    if _nm_baseline_cache is None:
        with open(NM_BASELINE_PATH, encoding="utf-8") as f:
            _nm_baseline_cache = json.load(f)
    return _nm_baseline_cache


def _fmt_duration(ns):
    ms = ns / 1e6
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.1f}ms"


def _compute_warn_flags(cur, trace_set):
    """Same functions, same constants Track A used -- see build_trace_logs.py."""
    rows = trace_set_op_host_avgs(cur, trace_set)
    host_outlier = find_host_latency_outlier(rows)
    cluster_dev = find_cluster_wide_deviation(rows, _get_nm_baseline())
    return {
        "host": (host_outlier[0], host_outlier[1]) if host_outlier else None,
        "cluster": cluster_dev[0] if cluster_dev else None,
    }


def _event_flag(host, opname, description, warn_flags):
    if "Exception" in description:
        return "ERROR"
    if warn_flags.get("host") == (host, opname):
        return "WARN"
    if warn_flags.get("cluster") == opname and host.startswith("datanode"):
        return "WARN"
    return None


def query_trace(trace_id):
    """
    Reconstruct and format one HDFS request's trace: a per-host timeline
    (times relative to that host's own first event in this trace -- see
    module docstring on cross-host time comparability) plus the Edge
    call-structure, with the same latency-outlier annotation Track A's
    `logs.level` provides.
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT trace_set FROM Trace_Source WHERE TraceID=?", (trace_id,))
    row = cur.fetchone()
    if row is None:
        return f"No trace found for instance_id={trace_id}."
    trace_set = row[0]  # internal use only -- never appears in the returned text

    cur.execute(
        "SELECT TID, OpName, StartTime, EndTime, HostName, Description "
        "FROM Event WHERE TaskID=? ORDER BY HostName, StartTime",
        (trace_id,),
    )
    events = cur.fetchall()
    if not events:
        return f"No events found for instance_id={trace_id}."

    warn_flags = _compute_warn_flags(cur, trace_set)

    by_host = defaultdict(list)
    for tid, opname, start, end, host, desc in events:
        by_host[host].append((tid, opname, start, end, desc))

    lines = []
    lines.append(f"=== Trace {trace_id} ===")
    lines.append(f"{len(events)} events across {len(by_host)} hosts: {', '.join(sorted(by_host))}")
    lines.append(
        "(Times below are relative to each host's OWN first event in this "
        "trace -- hosts use independent clocks, so times on different hosts "
        "are NOT directly comparable to each other.)"
    )

    for host in sorted(by_host):
        host_events = by_host[host]
        t0 = min(e[2] for e in host_events)
        lines.append(f"\n--- Host: {host} ---")
        for tid, opname, start, end, desc in host_events:
            dur = _fmt_duration(end - start)
            rel = _fmt_duration(start - t0)
            flag = _event_flag(host, opname, desc, warn_flags)
            flag_str = f" [{flag}]" if flag else ""
            if flag == "ERROR":
                detail = f" -- {desc}"
            else:
                detail = ""
            lines.append(f"  +{rel}  {opname} ({dur}){flag_str}{detail}")

    cur.execute(
        "SELECT FatherNID, FatherStartTime, ChildNID FROM Edge WHERE TraceID=?",
        (trace_id,),
    )
    edges = cur.fetchall()
    if edges:
        event_by_tid_start = {(tid, start): (opname, host) for tid, opname, start, end, host, desc in events}
        event_by_tid = defaultdict(list)
        for tid, opname, start, end, host, desc in events:
            event_by_tid[tid].append((start, opname, host))

        seen = set()
        rel_lines = []
        for father_nid, father_start, child_nid in edges:
            father = event_by_tid_start.get((father_nid, father_start))
            candidates = event_by_tid.get(child_nid, [])
            if not father or not candidates:
                continue
            # TID can repeat (see module docstring / schema description) --
            # prefer a candidate that started at/after the father, else fall
            # back to the earliest candidate overall. Best-effort, not exact.
            after_father = [c for c in candidates if c[0] >= father_start]
            pool = after_father if after_father else candidates
            child_start, child_op, child_host = min(pool, key=lambda c: c[0])
            key = (father[1], father[0], child_host, child_op)
            if key in seen:
                continue
            seen.add(key)
            rel_lines.append(f"  {father[1]}/{father[0]} -> {child_host}/{child_op}")
        if rel_lines:
            lines.append("\nCall structure (parent -> child, from Edge; some relationships may be")
            lines.append("ambiguous where a thread id was reused -- best-effort match shown):")
            lines.extend(rel_lines)

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Smoke-test query_trace standalone.")
    parser.add_argument("trace_id")
    args = parser.parse_args()
    print(query_trace(args.trace_id))
