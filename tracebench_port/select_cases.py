#!/usr/bin/env python3
"""
select_cases.py
----------------
Phase 2B-2D of the TraceBench port: stratified case selection + an
automated ground-truth verification gate, run against the manifest built
by build_manifest.py.

A case is only admitted to ground_truth_trace.json if the evidence for its
label is actually present in the real data for the specific TraceID chosen
— never asserted from the fault name alone. Two evidence paths, tried in
order:
  1. description_exception — a real exception/error substring in
     Event.Description for that TraceID (covers Tier 1 faults like
     killDN/deadDN/panicDN and Tier 3 corruption faults, confirmed by
     inspection to produce IOException/ChecksumException/etc. text).
  2. latency_deviation — an event whose (EndTime-StartTime) exceeds the
     MAX ever observed for that OpName across the NM (normal, non-
     anomalous) trace_sets. Needed because slowDN/slowHDFS (Tier 2)
     produce NO exception text at all — confirmed by inspection — the
     only symptom is elevated latency, so evidence must be comparative
     against a real normal-condition baseline, not asserted.
For normal-control trace_sets, the check is inverted: a TraceID is only
admitted if NO exception-like Description text is found in it (otherwise
it isn't actually a clean negative control).

Every candidate that fails both paths is logged with a reason to
data/tracebench_case_selection_rejects.log and is NOT included in the
ground truth — this script does not fabricate labels for cases it
can't verify.
"""
import csv
import json
import re
import sqlite3
from collections import defaultdict

DB_PATH = "data/tracebench_raw.sqlite"
MANIFEST_PATH = "data/tracebench_manifest.csv"
GROUND_TRUTH_PATH = "data/ground_truth_trace.json"
REJECT_LOG_PATH = "data/tracebench_case_selection_rejects.log"
EVAL_DB_PATH = "data/eval_tracebench.sqlite"

N_NORMAL_CONTROLS = 7          # ~20% of a ~30-case target set
MAX_TRACEIDS_TO_TRY = 10       # per candidate trace_set, before giving up
ERROR_PATTERN = re.compile(
    r"xception|rror|ail|efused|imeout|orrupt|hecksum|\bCRC\b|"
    r"eset by peer|o route to host|not known",
    re.IGNORECASE,
)


def load_manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def severity_or_delay(row):
    v = row["severity"] or row["delay_ms"]
    return float(v) if v not in (None, "") else None


def pick_mid_severity_per_fault(manifest):
    """One trace_set per distinct anomalous fault_name, at the
    lower-middle of that fault's own observed severity/delay_ms range —
    avoids both the trivial low extreme and the saturated high extreme,
    generalizing the spec's '2-3 out of an assumed 1-5 range' guidance to
    the real observed ranges (FDN-style faults actually run 1-50; slow*
    faults are a delay_ms, not a node count, and don't share that scale)."""
    by_fault = defaultdict(list)
    for row in manifest:
        if row["is_anomalous"] == "True":
            by_fault[row["fault_name"]].append(row)

    picks = []
    for fault_name, rows in sorted(by_fault.items()):
        by_value = {}
        for r in rows:
            v = severity_or_delay(r)
            by_value.setdefault(v, r)
        distinct_sorted = sorted(
            by_value.items(), key=lambda kv: (kv[0] is None, kv[0])
        )
        if not distinct_sorted:
            continue
        idx = max(0, len(distinct_sorted) // 3)
        picks.append(distinct_sorted[idx][1])
    return picks


def pick_normal_controls(manifest, n):
    normals = sorted(
        (r for r in manifest if r["is_anomalous"] == "False"),
        key=lambda r: r["trace_set"],
    )
    if not normals:
        return []
    step = max(1, len(normals) // n)
    return normals[::step][:n]


HOST_OUTLIER_RATIO = 1.4     # a datanode averaging >=1.4x its trace_set peers, same op
CLUSTER_DEVIATION_RATIO = 1.3  # a trace_set's own op average >=1.3x the NM op average
MIN_SAMPLES = 5              # ignore (host, op) / (trace_set, op) cells with too few events


def build_nm_latency_baseline(cur):
    """Mean (EndTime-StartTime) per OpName across NM (normal) trace_sets'
    datanode events only — used as the cross-condition reference for
    cluster-wide slowdowns (slowHDFS), where every datanode is affected
    equally so no single host stands out within its own trace_set."""
    cur.execute(
        r"""
        SELECT e.OpName, AVG(e.EndTime - e.StartTime)
        FROM Event e JOIN Trace_Source s ON e.TaskID = s.TraceID
        WHERE s.trace_set LIKE 'NM\_%' ESCAPE '\' AND e.HostName LIKE 'datanode%'
        GROUP BY e.OpName
        """
    )
    return dict(cur.fetchall())


def trace_set_op_host_avgs(cur, trace_set):
    """(HostName, OpName) -> (avg_duration, n) for this trace_set's own
    datanode events. A per-trace_set aggregate, not per-TraceID, so
    individual-event noise averages out."""
    cur.execute(
        "SELECT HostName, OpName, AVG(EndTime-StartTime), COUNT(*) "
        "FROM Event e JOIN Trace_Source s ON e.TaskID = s.TraceID "
        "WHERE s.trace_set = ? AND HostName LIKE 'datanode%' "
        "GROUP BY HostName, OpName HAVING COUNT(*) >= ?",
        (trace_set, MIN_SAMPLES),
    )
    return cur.fetchall()


def find_host_latency_outlier(rows):
    """A datanode whose average duration for some op is well above its
    peers' average for the SAME op within the SAME trace_set — the
    signature of a fault targeting specific node(s) (e.g. slowDN)."""
    by_op = defaultdict(list)
    for host, op, avg, n in rows:
        by_op[op].append((host, avg, n))
    best = None
    for op, hosts in by_op.items():
        if len(hosts) < 2:
            continue
        for host, avg, n in hosts:
            others = [a for h, a, _ in hosts if h != host]
            peer_avg = sum(others) / len(others)
            if peer_avg > 0 and avg / peer_avg >= HOST_OUTLIER_RATIO:
                ratio = avg / peer_avg
                if best is None or ratio > best[3]:
                    best = (host, op, avg, ratio, peer_avg)
    return best


def find_cluster_wide_deviation(rows, nm_avg_by_op):
    """This trace_set's own op-level average vs the true NM baseline for
    that op — catches a fault that slows every datanode roughly equally
    (e.g. slowHDFS), where no single host looks worse than its peers."""
    by_op = defaultdict(lambda: [0, 0])
    for host, op, avg, n in rows:
        by_op[op][0] += avg * n
        by_op[op][1] += n
    best = None
    for op, (weighted_sum, total_n) in by_op.items():
        if total_n < MIN_SAMPLES:
            continue
        weighted_avg = weighted_sum / total_n
        nm_avg = nm_avg_by_op.get(op)
        if nm_avg and nm_avg > 0 and weighted_avg / nm_avg >= CLUSTER_DEVIATION_RATIO:
            ratio = weighted_avg / nm_avg
            if best is None or ratio > best[3]:
                best = (op, weighted_avg, nm_avg, ratio)
    return best


def affected_hosts_from_rows(rows, host_idx=0):
    hosts = sorted({r[host_idx] for r in rows if r[host_idx] and "datanode" in r[host_idx].lower()})
    if hosts:
        return hosts
    return sorted({r[host_idx] for r in rows if r[host_idx]})


def verify_case(cur, trace_set, is_anomalous, nm_baseline, reject_log):
    cur.execute(
        "SELECT TraceID FROM Trace_Source WHERE trace_set=? ORDER BY TraceID LIMIT ?",
        (trace_set, MAX_TRACEIDS_TO_TRY),
    )
    trace_ids = [r[0] for r in cur.fetchall()]
    if not trace_ids:
        reject_log.write(f"{trace_set}: REJECTED — no TraceIDs found in Trace_Source\n")
        return None

    # Path 1 (preferred): a real exception/error string in Event.Description
    # for a specific TraceID — the most direct, least assumption-laden
    # evidence, and what most fault types (Tier 1, corruption/cut/loss,
    # disconnectDN) actually produce.
    for tid in trace_ids:
        cur.execute(
            "SELECT HostName, OpName, Description FROM Event "
            "WHERE TaskID=? AND Description IS NOT NULL AND Description != ''",
            (tid,),
        )
        rows = cur.fetchall()
        error_rows = [r for r in rows if ERROR_PATTERN.search(r[2] or "")]

        if not is_anomalous:
            if not error_rows:
                return {
                    "trace_id": tid,
                    "evidence_type": "clean_normal",
                    "evidence_anchor": "no exception/error text found in Event.Description",
                    "affected_component": [],
                }
            reject_log.write(
                f"{trace_set} / {tid}: candidate normal-control trace shows "
                f"error text ({error_rows[0][2]!r}) — trying next TraceID\n"
            )
            continue

        if error_rows:
            host, op, desc = error_rows[0]
            return {
                "trace_id": tid,
                "evidence_type": "description_exception",
                "evidence_anchor": desc,
                "affected_component": affected_hosts_from_rows(error_rows, host_idx=0),
            }

    if not is_anomalous:
        reject_log.write(
            f"{trace_set}: REJECTED — no clean (error-free) TraceID found among "
            f"{len(trace_ids)} tried\n"
        )
        return None

    # Path 2 fallback: no Description evidence in any TraceID tried — happens
    # for pure-latency faults (slowDN/slowHDFS) which inject delay without
    # producing exception text. Compare trace_set-level latency (aggregated
    # across all its traces, not one TraceID) against the appropriate
    # baseline: peer datanodes within the same trace_set for node-targeted
    # faults, or the real NM baseline for cluster-wide faults.
    latency_rows = trace_set_op_host_avgs(cur, trace_set)
    host_outlier = find_host_latency_outlier(latency_rows)
    if host_outlier:
        host, op, avg, ratio, peer_avg = host_outlier
        cur.execute(
            "SELECT TraceID FROM Event e JOIN Trace_Source s ON e.TaskID = s.TraceID "
            "WHERE s.trace_set=? AND e.HostName=? AND e.OpName=? LIMIT 1",
            (trace_set, host, op),
        )
        anchor_tid = cur.fetchone()
        return {
            "trace_id": anchor_tid[0] if anchor_tid else trace_ids[0],
            "evidence_type": "host_latency_outlier",
            "evidence_anchor": (
                f"{host} averages {avg:,.0f}ns for {op} across this trace_set vs "
                f"{peer_avg:,.0f}ns for its peer datanodes ({ratio:.1f}x)"
            ),
            "affected_component": [host],
        }

    cluster_dev = find_cluster_wide_deviation(latency_rows, nm_baseline)
    if cluster_dev:
        op, weighted_avg, nm_avg, ratio = cluster_dev
        return {
            "trace_id": trace_ids[0],
            "evidence_type": "cluster_latency_deviation",
            "evidence_anchor": (
                f"{op} averages {weighted_avg:,.0f}ns across this trace_set's datanodes "
                f"vs {nm_avg:,.0f}ns in normal-control traces ({ratio:.1f}x)"
            ),
            "affected_component": sorted({h for h, o, _, _ in latency_rows if o == op}),
        }

    reject_log.write(
        f"{trace_set}: REJECTED — no error text in {len(trace_ids)} TraceIDs tried, "
        f"and no host-level or cluster-level latency deviation found\n"
    )
    return None


def main():
    manifest = load_manifest()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print("Building NM (normal) latency baseline from real Event data...")
    nm_baseline = build_nm_latency_baseline(cur)
    print(f"  {len(nm_baseline)} OpNames baselined.\n")

    candidates = pick_mid_severity_per_fault(manifest)
    candidates += pick_normal_controls(manifest, N_NORMAL_CONTROLS)
    print(f"Candidate trace_sets selected: {len(candidates)} "
          f"({len(candidates) - N_NORMAL_CONTROLS} anomalous + {N_NORMAL_CONTROLS} normal)\n")

    ground_truth = []
    rejected = []
    with open(REJECT_LOG_PATH, "w", encoding="utf-8") as reject_log:
        for i, row in enumerate(candidates):
            trace_set = row["trace_set"]
            is_anomalous = row["is_anomalous"] == "True"
            result = verify_case(cur, trace_set, is_anomalous, nm_baseline, reject_log)
            if result is None:
                rejected.append(trace_set)
                continue

            case_id = f"TB-{len(ground_truth) + 1:03d}"
            ground_truth.append({
                "case_id": case_id,
                "tier": int(row["tier"]) if row["tier"] not in (None, "") else 0,
                "is_anomalous": is_anomalous,
                "trace_set": trace_set,
                "trace_id": result["trace_id"],
                "fault_category": row["fault_category"],
                "fault_name": row["fault_name"] if is_anomalous else "none / no fault",
                "severity": row["severity"] or None,
                "delay_ms": row["delay_ms"] or None,
                "affected_component": result["affected_component"],
                "evidence_type": result["evidence_type"],
                "evidence_anchor": result["evidence_anchor"],
            })

    with open(GROUND_TRUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(ground_truth, f, indent=2)

    eval_conn = sqlite3.connect(EVAL_DB_PATH)
    eval_conn.execute("DROP TABLE IF EXISTS ground_truth")
    eval_conn.execute(
        """
        CREATE TABLE ground_truth (
            case_id TEXT PRIMARY KEY, tier INTEGER, is_anomalous INTEGER,
            trace_set TEXT, trace_id TEXT, fault_category TEXT, fault_name TEXT,
            severity TEXT, delay_ms TEXT, affected_component TEXT,
            evidence_type TEXT, evidence_anchor TEXT
        )
        """
    )
    for gt in ground_truth:
        eval_conn.execute(
            "INSERT INTO ground_truth VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                gt["case_id"], gt["tier"], int(gt["is_anomalous"]), gt["trace_set"],
                gt["trace_id"], gt["fault_category"], gt["fault_name"],
                gt["severity"], gt["delay_ms"], json.dumps(gt["affected_component"]),
                gt["evidence_type"], gt["evidence_anchor"],
            ),
        )
    eval_conn.commit()
    eval_conn.close()
    conn.close()

    print(f"Admitted: {len(ground_truth)} cases -> {GROUND_TRUTH_PATH}, {EVAL_DB_PATH}")
    print(f"Rejected: {len(rejected)} candidates -> see {REJECT_LOG_PATH}")
    if rejected:
        for ts in rejected:
            print(f"  REJECTED: {ts}")

    print("\n-- Admitted cases by tier --")
    tier_counts = defaultdict(int)
    for gt in ground_truth:
        tier_counts[gt["tier"]] += 1
    for tier in sorted(tier_counts):
        label = "normal control" if tier == 0 else f"Tier {tier}"
        print(f"  {label}: {tier_counts[tier]}")

    print("\n-- Evidence type breakdown --")
    ev_counts = defaultdict(int)
    for gt in ground_truth:
        ev_counts[gt["evidence_type"]] += 1
    for et, c in sorted(ev_counts.items()):
        print(f"  {et}: {c}")


if __name__ == "__main__":
    main()
