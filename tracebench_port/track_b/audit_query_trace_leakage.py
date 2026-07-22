#!/usr/bin/env python3
"""
audit_query_trace_leakage.py

Leakage audit for the redesigned Track B: since query_trace() is a
deterministic function (not raw SQL access), the thing that must never leak
a fault name/category/trace_set is its OUTPUT TEXT, not the backing DB's
column list (which deliberately includes Trace_Source -- see
build_track_b_data.py). Calls query_trace() for all 27 real cases and greps
the actual returned string.
"""

import json
import os
import sys

sys.path.insert(0, "..")
from query_trace import query_trace  # noqa: E402


def load_leak_tokens():
    # audit_leakage_trace.py's paths are relative to tracebench_port/, not
    # track_b/ -- chdir just for this call, query_trace() itself is
    # cwd-independent (uses absolute paths, see its module docstring).
    cwd = os.getcwd()
    os.chdir("..")
    try:
        from audit_leakage_trace import load_leak_tokens as _load
        return _load()
    finally:
        os.chdir(cwd)


def main():
    tokens = load_leak_tokens()
    print(f"Auditing query_trace() output for {27} cases against {len(tokens)} tokens...\n")

    with open("../data/ground_truth_trace.json", encoding="utf-8") as f:
        gt = json.load(f)

    all_hits = []
    for c in gt:
        text = query_trace(c["trace_id"])
        text_lower = text.lower()
        for tok in tokens:
            if tok.lower() in text_lower:
                all_hits.append((c["case_id"], c["trace_id"], tok))

    if all_hits:
        print("LEAK HITS:")
        for h in all_hits:
            print(" ", h)
    print(f"\n{'FAIL' if all_hits else 'PASS'}: {len(all_hits)} total leak hits across 27 cases' query_trace() output.")
    return len(all_hits)


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
