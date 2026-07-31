"""
One-off enrichment: add affected_component_aliases (IP addresses) to
data/ground_truth_trace.json, computed from real data only -- never
invented. Two sources, in order:

1. For hosts that appear as an Event row's HostName in that case's own
   trace_id, look up their HostAddress directly (scoped per-trace -- the
   same hostname string maps to different IPs across different traces, so
   this must not be a global lookup table).
2. For "dead node" cases where the affected host never appears as an Event
   row (it never completed any operation, so there's nothing to look up),
   extract the IP directly from the case's own evidence_anchor text, which
   already contains the literal exception string quoting the IP:port the
   client failed to reach.

Run once from tracebench_port/.
"""
import json
import re
import sqlite3

GT_PATH = "data/ground_truth_trace.json"
DB_PATH = "data/tracebench_raw.sqlite"

with open(GT_PATH, encoding="utf-8") as f:
    gt = json.load(f)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

IP_RE = re.compile(r"/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\d+)?")

n_enriched = 0
for case in gt:
    affected = case.get("affected_component", [])
    if not affected:
        continue

    tid = case["trace_id"]
    aliases = set()

    for host in affected:
        cur.execute(
            "SELECT DISTINCT HostAddress FROM Event WHERE TaskID=? AND HostName=?",
            (tid, host),
        )
        addrs = [r[0] for r in cur.fetchall()]
        aliases.update(addrs)

    if not aliases:
        # Dead-node case: the affected host never appears as an Event row.
        # Pull the IP straight from the evidence_anchor's own exception text.
        anchor = case.get("evidence_anchor", "") or ""
        aliases.update(IP_RE.findall(anchor))

    if aliases:
        case["affected_component_aliases"] = sorted(aliases)
        n_enriched += 1
    else:
        print(f"WARNING: no alias found for {case['case_id']} ({affected})")

with open(GT_PATH, "w", encoding="utf-8") as f:
    json.dump(gt, f, indent=2)

print(f"Enriched {n_enriched} case(s) with affected_component_aliases.")
