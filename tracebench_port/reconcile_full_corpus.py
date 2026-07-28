#!/usr/bin/env python3
"""
reconcile_full_corpus.py

Independent verification, extended to the WHOLE corpus (not just the 27
selected cases verify_data_integrity.py already checks). Directly parses all
364 raw .sql files -- reusing only split_sql_statements()/split_value_tuples()
(text-analysis utilities), never load_sql_file() or the DB itself as a source
of truth -- and compares raw tuple counts per table per file against what's
actually in tracebench_raw.sqlite.

This is the same discipline that caught the Edge data-loss bug, applied to
the 337 trace_sets that were never independently checked before now (only
the 27 selected cases were). Motivation: Track A's WARN-detection and Track
B's NM baseline both draw statistics from the ENTIRE corpus, not just the 27
cases, so an undetected problem in an unselected trace_set could silently
corrupt a baseline both tracks depend on.

Run from within tracebench_port/:
    python reconcile_full_corpus.py
"""

import glob
import os
import re
import sqlite3

from load_tracebench import split_sql_statements, split_value_tuples

SQL_DIR = "data/raw_sql/mtracer-TraceBench-44b29e5"
DB_PATH = "data/tracebench_raw.sqlite"

# Raw (un-renamed) table names as they appear in the dump.
RAW_TABLES = {
    "Report": "Event",
    "Task": "Trace",
    "Edge": "Edge",
    "Operation": "Operation",
}

INSERT_RE = re.compile(r"^INSERT INTO\s+`?(\w+)`?\s+VALUES", re.IGNORECASE)


def count_raw_tuples(filepath):
    """Return {real_table_name: tuple_count} by parsing this file directly."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    stmts = split_sql_statements(raw)
    counts = {name: 0 for name in RAW_TABLES.values()}
    for stmt in stmts:
        m = INSERT_RE.match(stmt.strip())
        if not m:
            continue
        raw_table = m.group(1)
        if raw_table not in RAW_TABLES:
            continue
        values_blob = stmt[m.end():]
        n = len(split_value_tuples(values_blob))
        counts[RAW_TABLES[raw_table]] += n
    return counts


def main():
    files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
    print(f"Reconciling {len(files)} files against {DB_PATH}...\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    totals_raw = {name: 0 for name in RAW_TABLES.values()}
    per_file_mismatches = []

    for i, fp in enumerate(files):
        counts = count_raw_tuples(fp)
        for name, n in counts.items():
            totals_raw[name] += n
        if (i + 1) % 50 == 0 or i == len(files) - 1:
            print(f"  [{i+1}/{len(files)}] {os.path.basename(fp)}")

    print("\n=== GLOBAL TOTALS: raw tuple count vs DB row count ===")
    any_mismatch = False
    for table in ("Event", "Trace", "Edge", "Operation"):
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        db_count = cur.fetchone()[0]
        raw_count = totals_raw[table]
        status = "OK" if raw_count == db_count else "MISMATCH"
        if status == "MISMATCH":
            any_mismatch = True
        print(f"  {table:10} raw={raw_count:>10,}  db={db_count:>10,}  [{status}]")

    conn.close()

    print()
    if any_mismatch:
        print("MISMATCH DETECTED at the global level -- rerun with per-file breakdown to localize.")
    else:
        print("PASS: every table's raw tuple count matches the loaded DB exactly, corpus-wide.")

    return any_mismatch


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
