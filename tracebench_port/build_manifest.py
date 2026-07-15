#!/usr/bin/env python3
"""
build_manifest.py
------------------
Phase 1.3 / Phase 2A of the TraceBench port: turn the 364 trace_set labels
recorded in Trace_Source (see load_tracebench.py) into the structured
ground-truth key, `data/tracebench_manifest.csv`.

One row per trace_set (not per TraceID) with: is_anomalous, class,
fault_category, fault_name, workload, severity, tier, n_traces.

Naming convention observed in the actual dump (confirmed by inspection,
richer than the spec's toy example):
  AN_{category}_{fault}_{workload}_{severity}FDN_{clients}C_..._{RT}RT_{WT}WT.sql
  COM_{Mul|Sin}_{category}_{workload}_{run}_{clients}C_..._{RT}RT_{WT}WT.sql
  NM_{CL|DN}_{workload}_[{severity}DN_]{clients}C_..._{RT}RT_{WT}WT.sql

Tier map mirrors tracebench_port_spec.md section 2A. deadDN/panicDN placed
in Tier 1 (not the spec's conditional default) after inspecting real
Event.Description text for both: both show a clean "can't reach exactly
`severity` target datanode(s)" exception (SocketTimeoutException /
NoRouteToHostException respectively) repeated across many independent
client sessions — an unambiguous single/multi-node signature, not a
propagating/degrading one, so they group with killDN/suspendDN rather
than slowDN/disconnectDN.
"""
import csv
import re
import sqlite3

DB_PATH = "data/tracebench_raw.sqlite"
OUT_PATH = "data/tracebench_manifest.csv"

TIER_MAP = {
    "killDN": 1, "suspendDN": 1, "readOnlyDN": 1,
    "deadDN": 1, "panicDN": 1,
    "slowDN": 2, "slowHDFS": 2, "disconnectDN": 2,
    "corruptBlk": 3, "corruptMeta": 3, "cutBlk": 3,
    "cutMeta": 3, "lossBlk": 3, "lossMeta": 3,
}

AN_RE = re.compile(
    r'^AN_(?P<category>[A-Za-z]+)_(?P<fault>[A-Za-z0-9]+)_(?P<workload>[a-z]+)_'
    r'(?:(?P<delay_ms>\d+)ms_)?'
    r'(?:(?P<severity>\d+)FDN_)?'
)
COM_RE = re.compile(
    r'^COM_(?P<kind>Mul|Sin)_(?P<category>[A-Za-z]+)_(?P<workload>[a-z]+)_(?P<run>\d+)_'
)
NM_RE = re.compile(
    r'^NM_(?P<subtype>CL|DN)_(?P<workload>[a-z]+)_'
    r'(?:(?P<severity>\d+)DN_)?'
)


def parse_trace_set(trace_set):
    name = trace_set[:-4] if trace_set.endswith(".sql") else trace_set

    m = AN_RE.match(name)
    if m:
        g = m.groupdict()
        fault = g["fault"]
        tier = TIER_MAP.get(fault)
        return {
            "trace_set": trace_set,
            "is_anomalous": True,
            "class": "AN",
            "fault_category": g["category"],
            "fault_name": fault,
            "workload": g["workload"],
            "severity": int(g["severity"]) if g["severity"] is not None else None,
            "delay_ms": int(g["delay_ms"]) if g["delay_ms"] is not None else None,
            "tier": tier,
            "notes": "" if tier is not None else "UNMAPPED fault_name — check TIER_MAP",
        }

    m = COM_RE.match(name)
    if m:
        g = m.groupdict()
        return {
            "trace_set": trace_set,
            "is_anomalous": True,
            "class": "COM",
            "fault_category": g["category"],
            "fault_name": f"{g['kind']}_{g['category']}",
            "workload": g["workload"],
            "severity": int(g["run"]),  # not a failed-node count for COM sets — see notes
            "delay_ms": None,
            "tier": 3,
            "notes": "severity is the run/trial number, not a failed-datanode count "
                     "(COM sets don't encode an NNFDN token)",
        }

    m = NM_RE.match(name)
    if m:
        g = m.groupdict()
        return {
            "trace_set": trace_set,
            "is_anomalous": False,
            "class": "NM",
            "fault_category": "normal",
            "fault_name": "none / no fault",
            "workload": g["workload"],
            "severity": int(g["severity"]) if g["severity"] is not None else None,
            "delay_ms": None,
            "tier": None,
            "notes": f"subtype={g['subtype']}",
        }

    return {
        "trace_set": trace_set,
        "is_anomalous": None,
        "class": None,
        "fault_category": None,
        "fault_name": None,
        "workload": None,
        "severity": None,
        "delay_ms": None,
        "tier": None,
        "notes": "UNPARSED — filename did not match any known pattern",
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT trace_set, COUNT(*) FROM Trace_Source GROUP BY trace_set ORDER BY trace_set"
    )
    rows = cur.fetchall()
    conn.close()

    fieldnames = [
        "trace_set", "is_anomalous", "class", "fault_category", "fault_name",
        "workload", "severity", "delay_ms", "tier", "n_traces", "notes",
    ]
    manifest = []
    for trace_set, n_traces in rows:
        parsed = parse_trace_set(trace_set)
        parsed["n_traces"] = n_traces
        manifest.append(parsed)

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(manifest)

    print(f"Wrote {len(manifest)} trace_set rows to {OUT_PATH}\n")

    unparsed = [r for r in manifest if r["class"] is None]
    unmapped = [r for r in manifest if r["is_anomalous"] and r["class"] == "AN" and r["tier"] is None]
    if unparsed:
        print(f"WARNING: {len(unparsed)} trace_set(s) failed to parse:")
        for r in unparsed:
            print(f"  {r['trace_set']}")
    if unmapped:
        print(f"WARNING: {len(unmapped)} AN trace_set(s) have an unmapped fault_name:")
        for r in unmapped:
            print(f"  {r['trace_set']} -> fault_name={r['fault_name']}")

    print("\n-- Tier distribution (anomalous sets) --")
    from collections import Counter
    tier_counts = Counter(r["tier"] for r in manifest if r["is_anomalous"])
    for tier in sorted(tier_counts, key=lambda t: (t is None, t)):
        print(f"  Tier {tier}: {tier_counts[tier]} trace_sets")
    print(f"  Normal (non-anomalous) controls: {sum(1 for r in manifest if not r['is_anomalous'])} trace_sets")

    print("\n-- fault_name distribution (AN class) --")
    fault_counts = Counter(r["fault_name"] for r in manifest if r["class"] == "AN")
    for fault, count in sorted(fault_counts.items()):
        print(f"  {fault}: {count}")

    print("\n-- severity distribution (AN class, where present) --")
    sev_counts = Counter(r["severity"] for r in manifest if r["class"] == "AN")
    for sev in sorted(sev_counts, key=lambda s: (s is None, s)):
        print(f"  severity={sev}: {sev_counts[sev]}")


if __name__ == "__main__":
    main()
