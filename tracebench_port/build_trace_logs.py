#!/usr/bin/env python3
"""
build_trace_logs.py

Phase 4 Track A: flatten TraceBench Event rows into the existing PaaS `logs`
schema (sql_agent.py's DEFAULT_SCHEMA_DESCRIPTION), so the SQL agent can be
pointed at trace data with ZERO change to its tool interface or internals —
only DB_URI and INCLUDE_TABLES differ from the PaaS config.

SCOPE (a deliberate deviation from the spec's literal "keep the four tables"
wording, recorded in CHANGELOG_PORT.md): this flattens only the 27 selected
cases' trace_sets, not the full 370k-trace corpus. Critically, "scope" means
the ENTIRE trace_set each case was drawn from (thousands of sibling traces),
not just the one named TraceID per case. select_cases.py's own fault
verification logic (trace_set_op_host_avgs / find_host_latency_outlier)
compares a datanode's average op latency against its PEERS across the WHOLE
trace_set -- that peer-comparison evidence does not exist if we only flatten
the single TraceID a question names. 27 trace_sets -> ~26,943 traces total
(see ground_truth_trace.json), an order of magnitude smaller than the full
corpus.

VERIFIED FACTS this script relies on (checked directly against the loaded
tracebench_raw.sqlite, not assumed from the spec):
  - Event columns: TaskID, TID, OpName, StartTime, EndTime, HostAddress,
    HostName, Agent, Description. (TaskID/TID are the dump's real names --
    spec assumed TraceID/NID.)
  - TID is a THREAD identifier, not a unique per-event id -- the same TID
    repeats across many events on one client thread. Edge disambiguates a
    specific father event via (FatherNID, FatherStartTime) together.
  - Description is populated on ~100% of rows (0 NULLs) -- ~92.5% are
    "Success: ..." text, ~4.9% contain "Exception". It is NOT an
    error-only field, so ERROR-level detection must look for the substring,
    not for non-nullness.
  - Event.StartTime/EndTime are per-host nanoTime-style ticks, NOT a shared
    wall-clock epoch -- cross-host gaps within the same TaskID are wildly
    implausible as real elapsed time (verified: one sampled TaskID showed a
    ~1.8 HOUR gap between a client's and a namenode's StartTime for what
    should be a millisecond RPC). Only same-row durations (EndTime-StartTime)
    and same-host StartTime ordering are meaningful. `timestamp` below is
    therefore anchored per (TaskID, HostName) to the real Trace.FirstSeen
    date rather than converting the raw tick to a fabricated absolute clock
    reading -- see build_timestamp_anchors().
  - Trace.NumEdges is unreliable metadata: many TaskIDs with NumEdges > 0
    have zero matching Edge rows in the actual dump. Never trust it; if you
    need an edge count, COUNT(*) FROM Edge directly.

Run from within tracebench_port/:
    python build_trace_logs.py
"""

import json
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from select_cases import (
    build_nm_latency_baseline,
    trace_set_op_host_avgs,
    find_host_latency_outlier,
    find_cluster_wide_deviation,
)

RAW_DB_PATH = "data/tracebench_raw.sqlite"
OUT_DB_PATH = "data/benchmark_trace_db.sqlite"
GROUND_TRUTH_PATH = "data/ground_truth_trace.json"

# Same source-file naming discipline as sql_agent.py: never expose the
# trace_set filename to the agent (see LEAKAGE AUDIT below) -- the fault
# name lives in that filename.
SOURCE_SYSTEM = "hdfs_tracebench"


# ============================================================================
# EVENT_TYPE CLASSIFICATION -- built from the FULL verified distinct OpName
# list (71 values), not guessed. See CHANGELOG_PORT.md for the query used.
# ============================================================================

_RPC_PREFIX = "RPC:"
_CLI_PREFIX = "fs -"

_READ_OPS = {
    "bestNode", "chooseDataNode", "OP: try new BlockReader", "OP: new blockSender",
    "readBlock", "newBlockReader", "blockSeekTo", "getFileInfo", "sendBlock",
    "checksumOk", "verifiedByClient", "OP: send block", "getBlockLocations",
    "getListing", "getContentSummary", "getBlockInfo", "getProtocolVersion",
}
_WRITE_OPS = {
    "OP: receive block", "writeBlock", "OP: new BlockReceiver", "receiveBlock",
    "OP: connect next Datanode", "addBlock", "createBlockOutputStream",
    "nextBlockOutputStream", "create", "complete", "delete", "mkdirs", "rename",
    "setPermission", "setOwner", "reportBadBlocks", "abandonBlock", "recoverBlock",
    "nextGenerationStamp", "commitBlockSynchronization", "updateBlock",
    "startBlockRecovery",
}
_ERROR_OPS = {"Exception", "errorReport"}


def classify_event_type(opname):
    if opname.startswith(_RPC_PREFIX):
        return "rpc"
    if opname.startswith(_CLI_PREFIX):
        return "client_command"
    if opname in _ERROR_OPS:
        return "error"
    if opname in _READ_OPS:
        return "read"
    if opname in _WRITE_OPS:
        return "write"
    return "other"


# ============================================================================
# SCOPE: the 27 cases' trace_sets (whole sets, not single TraceIDs)
# ============================================================================

def load_scope_trace_sets():
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        gt = json.load(f)
    trace_sets = sorted({c["trace_set"] for c in gt})
    return trace_sets, gt


# ============================================================================
# TIMESTAMP ANCHORING -- see module docstring for why this isn't a direct
# nanoTime -> epoch conversion.
# ============================================================================

def build_timestamp_anchors(cur, trace_sets):
    """(TaskID, HostName) -> (anchor_datetime, anchor_ns).

    anchor_datetime comes from Trace.FirstSeen (a real wall-clock date the
    tracing backend recorded). anchor_ns is this (TaskID, HostName)'s own
    minimum StartTime, so every event on that host within that trace gets a
    plausible, correctly-ORDERED offset from a real date -- never a false
    cross-host simultaneity claim.
    """
    placeholders = ",".join("?" * len(trace_sets))
    cur.execute(
        f"""
        SELECT e.TaskID, e.HostName, MIN(e.StartTime)
        FROM Event e JOIN Trace_Source s ON e.TaskID = s.TraceID
        WHERE s.trace_set IN ({placeholders})
        GROUP BY e.TaskID, e.HostName
        """,
        trace_sets,
    )
    host_anchor_ns = {(taskid, host): min_st for taskid, host, min_st in cur.fetchall()}

    cur.execute(
        f"""
        SELECT t.TaskID, t.FirstSeen
        FROM Trace t JOIN Trace_Source s ON t.TaskID = s.TraceID
        WHERE s.trace_set IN ({placeholders})
        """,
        trace_sets,
    )
    first_seen = {}
    for taskid, fs in cur.fetchall():
        try:
            first_seen[taskid] = datetime.strptime(fs, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            first_seen[taskid] = datetime(2013, 11, 4)  # dataset's real collection period

    return host_anchor_ns, first_seen


def compute_timestamp(taskid, host, start_time_ns, host_anchor_ns, first_seen):
    anchor_ns = host_anchor_ns.get((taskid, host), start_time_ns)
    offset = first_seen.get(taskid, datetime(2013, 11, 4)) + timedelta(
        seconds=(start_time_ns - anchor_ns) / 1e9
    )
    return offset.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ============================================================================
# WARN DERIVATION -- reuses select_cases.py's own peer-latency outlier logic
# (same constants: HOST_OUTLIER_RATIO, CLUSTER_DEVIATION_RATIO, MIN_SAMPLES)
# rather than inventing a second, possibly-inconsistent heuristic.
# ============================================================================

def build_warn_flags(cur, trace_sets, nm_baseline):
    """trace_set -> {"host": (HostName, OpName) or None,
                     "cluster": OpName or None}"""
    flags = {}
    for ts in trace_sets:
        rows = trace_set_op_host_avgs(cur, ts)
        host_outlier = find_host_latency_outlier(rows)
        cluster_dev = find_cluster_wide_deviation(rows, nm_baseline)
        flags[ts] = {
            "host": (host_outlier[0], host_outlier[1]) if host_outlier else None,
            "cluster": cluster_dev[0] if cluster_dev else None,
        }
    return flags


def compute_level(description, host, opname, trace_set, warn_flags):
    if "Exception" in description:
        return "ERROR"
    ts_flags = warn_flags.get(trace_set, {})
    if ts_flags.get("host") == (host, opname):
        return "WARN"
    if ts_flags.get("cluster") == opname and host.startswith("datanode"):
        return "WARN"
    return "INFO"


# ============================================================================
# BUILD
# ============================================================================

def build_logs_table(raw_conn, out_conn, trace_sets):
    out_conn.execute("DROP TABLE IF EXISTS logs")
    out_conn.execute(
        """
        CREATE TABLE logs (
            row_uuid      TEXT PRIMARY KEY,
            timestamp     TEXT,
            source_system TEXT,
            component     TEXT,
            subcomponent  TEXT,
            level         TEXT,
            node_id       TEXT,
            instance_id   TEXT,
            event_type    TEXT,
            message       TEXT,
            thread_id     TEXT,
            block_id      TEXT,
            source_file   TEXT
        )
        """
    )

    cur = raw_conn.cursor()

    print("Building NM latency baseline...")
    nm_baseline = build_nm_latency_baseline(cur)

    print("Computing per-trace_set WARN flags (host-outlier / cluster-deviation)...")
    warn_flags = build_warn_flags(cur, trace_sets, nm_baseline)
    for ts, flags in warn_flags.items():
        print(f"  {ts}: host={flags['host']} cluster={flags['cluster']}")

    print("Building timestamp anchors...")
    host_anchor_ns, first_seen = build_timestamp_anchors(cur, trace_sets)

    placeholders = ",".join("?" * len(trace_sets))
    cur.execute(
        f"""
        SELECT e.TaskID, e.TID, e.OpName, e.StartTime, e.EndTime,
               e.HostAddress, e.HostName, e.Agent, e.Description, s.trace_set
        FROM Event e JOIN Trace_Source s ON e.TaskID = s.TraceID
        WHERE s.trace_set IN ({placeholders})
        """,
        trace_sets,
    )

    rows_out = []
    n = 0
    for taskid, tid, opname, start_ns, end_ns, host_addr, host, agent, desc, trace_set in cur:
        n += 1
        level = compute_level(desc, host, opname, trace_set, warn_flags)
        rows_out.append((
            uuid.uuid4().hex,
            compute_timestamp(taskid, host, start_ns, host_anchor_ns, first_seen),
            SOURCE_SYSTEM,
            opname,
            agent,
            level,
            host,
            taskid,
            classify_event_type(opname),
            desc,
            tid,
            None,   # block_id -- not parsed out; already present in message text
            None,   # source_file -- deliberately NOT the trace_set filename (leakage)
        ))
        if len(rows_out) >= 50000:
            out_conn.executemany(
                "INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_out
            )
            rows_out = []

    if rows_out:
        out_conn.executemany(
            "INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_out
        )
    out_conn.commit()
    print(f"\nFlattened {n:,} Event rows into logs.")
    return n


# ============================================================================
# MAIN
# ============================================================================

def main():
    trace_sets, gt = load_scope_trace_sets()
    print(f"Scope: {len(trace_sets)} trace_sets from {len(gt)} cases")

    raw_conn = sqlite3.connect(RAW_DB_PATH)
    out_conn = sqlite3.connect(OUT_DB_PATH)

    n = build_logs_table(raw_conn, out_conn, trace_sets)

    cur = out_conn.cursor()
    cur.execute("SELECT level, COUNT(*) FROM logs GROUP BY level")
    print("\nLevel distribution:")
    for level, count in cur.fetchall():
        print(f"  {level}: {count:,}")

    cur.execute("SELECT COUNT(DISTINCT instance_id) FROM logs")
    print(f"\nDistinct instance_id (TaskID) values: {cur.fetchone()[0]:,}")

    raw_conn.close()
    out_conn.close()
    print(f"\nDone. DB at {OUT_DB_PATH}")


if __name__ == "__main__":
    main()
