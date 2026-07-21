#!/usr/bin/env python3
"""
audit_leakage_trace.py

Leakage audit for Phase 4's agent-visible tables (spec 4A.3 / addendum 4B.4):
confirm no fault name, fault category, or trace_set filename token appears in
any column the SQL agent can see.

Checks Track A (`logs`) and, once present, Track B's native tables in
benchmark_trace_db.sqlite. Run after build_trace_logs.py / build_trace_native.py.
"""

import csv
import json
import re
import sqlite3

DB_PATH = "data/benchmark_trace_db.sqlite"
MANIFEST_PATH = "data/tracebench_manifest.csv"
GROUND_TRUTH_PATH = "data/ground_truth_trace.json"


def load_leak_tokens():
    tokens = set()
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["fault_name"] and row["fault_name"] != "none":
                tokens.add(row["fault_name"])
            if row["fault_category"]:
                tokens.add(row["fault_category"])
            tokens.add(row["trace_set"])
            tokens.add(row["trace_set"].replace(".sql", ""))
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        for c in json.load(f):
            tokens.add(c["trace_set"])
            tokens.add(c["trace_set"].replace(".sql", ""))
            if c.get("fault_name"):
                tokens.add(c["fault_name"])
    # Drop tokens too short/common to be meaningful greps (avoid false positives
    # on e.g. a bare "Data" or "Sys" matching unrelated substrings).
    tokens = {t for t in tokens if t and len(t) >= 5}
    return sorted(tokens)


def audit_table(conn, table, text_columns, tokens):
    cur = conn.cursor()
    hits = []
    for col in text_columns:
        cur.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL")
        values = [row[0] for row in cur.fetchall()]
        for val in values:
            for tok in tokens:
                if tok.lower() in str(val).lower():
                    hits.append((table, col, tok, val[:120]))
    return hits


def main():
    tokens = load_leak_tokens()
    print(f"Auditing against {len(tokens)} fault/category/trace_set tokens...\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}

    all_hits = []

    if "logs" in tables:
        print("=== Track A: logs ===")
        hits = audit_table(
            conn, "logs",
            ["component", "subcomponent", "message", "node_id", "event_type", "source_file"],
            tokens,
        )
        all_hits += hits
        print(f"  {len(hits)} hits" if hits else "  clean")
        for h in hits[:20]:
            print("  LEAK:", h)

    for table, cols in (
        ("Event", ["OpName", "Agent", "Description", "HostName"]),
        ("Trace", ["Title"]),
        ("Edge", []),
        ("Operation", ["OpName"]),
    ):
        if table in tables and cols:
            print(f"\n=== Track B: {table} ===")
            hits = audit_table(conn, table, cols, tokens)
            all_hits += hits
            print(f"  {len(hits)} hits" if hits else "  clean")
            for h in hits[:20]:
                print("  LEAK:", h)

    conn.close()

    print(f"\n{'FAIL' if all_hits else 'PASS'}: {len(all_hits)} total leak hits across all audited tables/columns.")
    return len(all_hits)


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
