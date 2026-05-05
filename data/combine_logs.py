#!/usr/bin/env python3
"""
combine_logs.py
---------------
Takes all_incidents.jsonl + hdfs_output.jsonl and produces:

  eval_db.sqlite      — full data including all ground-truth labels
  benchmark_db.sqlite — clean stream, ground-truth columns dropped

All rows get:
  - source_system normalised to "paas_platform"
  - a unique row_uuid for cross-DB evaluation joins

Usage:
    python combine_logs.py hdfs_output.jsonl all_incidents.jsonl \
        --eval-db eval_db.sqlite --benchmark-db benchmark_db.sqlite
"""

import json
import uuid
import sqlite3
import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ── HDFS time window ─────────────────────────────────────────────────────────
WINDOW_START = datetime(2008, 11,  9, 20, 37, 51, tzinfo=timezone.utc)
WINDOW_END   = datetime(2008, 11, 11, 11,  6, 40, tzinfo=timezone.utc)
WINDOW_SECS  = (WINDOW_END - WINDOW_START).total_seconds()

# Anchor fraction per incident — preserves internal timing, moves t0 into window
INCIDENT_ANCHORS = {
    "INC-001": 0.02, "INC-002": 0.06, "INC-003": 0.10,
    "INC-004": 0.14, "INC-005": 0.18, "INC-006": 0.23,
    "INC-007": 0.27, "INC-008": 0.32, "INC-009": 0.37,
    "INC-010": 0.42, "INC-011": 0.46, "INC-012": 0.50,
    "INC-013": 0.54, "INC-014": 0.58, "INC-015": 0.62,
    "INC-016": 0.66, "INC-017": 0.70, "INC-018": 0.74,
    "INC-019": 0.78, "INC-020": 0.81, "INC-021": 0.86,
    "INC-022": 0.88, "INC-023": 0.91, "INC-024": 0.93,
    "INC-025": 0.95,
}


def remap_timestamps(logs: list[dict], anchor_frac: float) -> list[dict]:
    """Shift all timestamps so t0 lands at anchor_frac of the HDFS window."""
    parsed = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) for e in logs]
    t0_orig = min(parsed)
    t0_new  = WINDOW_START + timedelta(seconds=anchor_frac * WINDOW_SECS)
    out = []
    for entry, orig_ts in zip(logs, parsed):
        new_ts = min(t0_new + (orig_ts - t0_orig), WINDOW_END)
        e = dict(entry)
        e["timestamp"] = new_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(e)
    return out


# ── Schema ───────────────────────────────────────────────────────────────────

EVAL_DDL = """
CREATE TABLE IF NOT EXISTS logs (
    row_uuid      TEXT PRIMARY KEY,
    timestamp     TEXT NOT NULL,
    source_system TEXT NOT NULL,
    component     TEXT,
    subcomponent  TEXT,
    level         TEXT,
    node_id       TEXT,
    instance_id   TEXT,
    event_type    TEXT,
    message       TEXT,
    thread_id     INTEGER,
    block_id      TEXT,
    source_file   TEXT,
    -- ground-truth cols (eval_db only)
    incident_id   TEXT,
    root_cause    TEXT,
    severity      TEXT,
    correlation_id TEXT,
    app_guid      TEXT,
    org           TEXT,
    space         TEXT
);
CREATE INDEX IF NOT EXISTS idx_e_ts         ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_e_incident   ON logs(incident_id);
CREATE INDEX IF NOT EXISTS idx_e_source     ON logs(source_system);
"""

BENCHMARK_DDL = """
CREATE TABLE IF NOT EXISTS logs (
    row_uuid      TEXT PRIMARY KEY,
    timestamp     TEXT NOT NULL,
    source_system TEXT NOT NULL,
    component     TEXT,
    subcomponent  TEXT,
    level         TEXT,
    node_id       TEXT,
    instance_id   TEXT,
    event_type    TEXT,
    message       TEXT,
    thread_id     INTEGER,
    block_id      TEXT,
    source_file   TEXT
);
CREATE INDEX IF NOT EXISTS idx_b_ts      ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_b_level   ON logs(level);
CREATE INDEX IF NOT EXISTS idx_b_comp    ON logs(component);
CREATE INDEX IF NOT EXISTS idx_b_source  ON logs(source_system);
"""

EVAL_INSERT = """INSERT INTO logs (
    row_uuid, timestamp, source_system, component, subcomponent, level,
    node_id, instance_id, event_type, message,
    thread_id, block_id, source_file,
    incident_id, root_cause, severity, correlation_id, app_guid, org, space
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

BENCH_INSERT = """INSERT INTO logs (
    row_uuid, timestamp, source_system, component, subcomponent, level,
    node_id, instance_id, event_type, message,
    thread_id, block_id, source_file
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def make_rows(entry: dict, row_id: str) -> tuple:
    """Returns (full_row_20, bench_row_13)."""
    meta = entry.get("metadata", {})
    full = (
        row_id,
        entry.get("timestamp"),
        "paas_platform",                    # ← normalised for all rows
        entry.get("component"),
        entry.get("subcomponent"),
        entry.get("level"),
        entry.get("node_id"),
        entry.get("instance_id"),
        entry.get("event_type"),
        entry.get("message"),
        meta.get("thread_id"),
        meta.get("block_id"),
        meta.get("source_file"),
        meta.get("incident_id"),
        meta.get("root_cause"),
        meta.get("severity"),
        meta.get("correlation_id"),
        meta.get("app_guid"),
        meta.get("org"),
        meta.get("space"),
    )
    return full, full[:13]


def flush(eval_conn, bench_conn, eval_buf, bench_buf):
    eval_conn.executemany(EVAL_INSERT,  eval_buf)
    bench_conn.executemany(BENCH_INSERT, bench_buf)
    eval_conn.commit()
    bench_conn.commit()
    eval_buf.clear()
    bench_buf.clear()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hdfs_jsonl",       type=Path, help="hdfs_output.jsonl")
    parser.add_argument("incidents_jsonl",  type=Path, help="all_incidents.jsonl")
    parser.add_argument("--eval-db",        type=Path, default=Path("eval_db.sqlite"))
    parser.add_argument("--benchmark-db",   type=Path, default=Path("benchmark_db.sqlite"))
    parser.add_argument("--batch",          type=int,  default=500)
    args = parser.parse_args()

    for p in (args.hdfs_jsonl, args.incidents_jsonl):
        if not p.exists():
            logging.error("File not found: %s", p); sys.exit(1)

    for db in (args.eval_db, args.benchmark_db):
        if db.exists():
            db.unlink()

    eval_conn  = sqlite3.connect(args.eval_db)
    bench_conn = sqlite3.connect(args.benchmark_db)
    eval_conn.executescript(EVAL_DDL)
    bench_conn.executescript(BENCHMARK_DDL)

    eval_buf, bench_buf = [], []

    def insert(entry):
        rid = str(uuid.uuid4())
        full, bench = make_rows(entry, rid)
        eval_buf.append(full)
        bench_buf.append(bench)
        if len(eval_buf) >= args.batch:
            flush(eval_conn, bench_conn, eval_buf, bench_buf)

    # ── HDFS logs ─────────────────────────────────────────────────────────────
    logging.info("Loading HDFS logs from %s …", args.hdfs_jsonl)
    hdfs_rows = load_jsonl(args.hdfs_jsonl)
    for e in hdfs_rows:
        insert(e)
    flush(eval_conn, bench_conn, eval_buf, bench_buf)
    logging.info("  %d HDFS entries", len(hdfs_rows))

    # ── Incidents ─────────────────────────────────────────────────────────────
    logging.info("Loading incidents from %s …", args.incidents_jsonl)
    inc_rows = load_jsonl(args.incidents_jsonl)

    # Group by incident_id and remap timestamps per-incident
    from collections import defaultdict
    by_inc = defaultdict(list)
    for e in inc_rows:
        inc_id = e.get("metadata", {}).get("incident_id", "UNKNOWN")
        by_inc[inc_id].append(e)

    total_inc = 0
    for inc_id, logs in sorted(by_inc.items()):
        anchor = INCIDENT_ANCHORS.get(inc_id)
        if anchor is None:
            logging.warning("No anchor defined for %s — skipping", inc_id)
            continue
        remapped = remap_timestamps(logs, anchor)
        for e in remapped:
            insert(e)
        total_inc += len(remapped)
        ts_min = min(e["timestamp"] for e in remapped)
        ts_max = max(e["timestamp"] for e in remapped)
        logging.info("  %s: %d entries  %s → %s", inc_id, len(remapped), ts_min, ts_max)

    flush(eval_conn, bench_conn, eval_buf, bench_buf)
    logging.info("  %d incident entries", total_inc)

    # ── Verify no label leak ──────────────────────────────────────────────────
    bench_cols = {r[1] for r in bench_conn.execute("PRAGMA table_info(logs)").fetchall()}
    leaked = bench_cols & {"incident_id", "root_cause", "severity", "correlation_id", "app_guid", "org", "space"}
    if leaked:
        logging.error("LABEL LEAK in benchmark_db: %s", leaked); sys.exit(1)

    # Verify source_system uniformity
    sources_bench = bench_conn.execute("SELECT DISTINCT source_system FROM logs").fetchall()
    sources_eval  = eval_conn.execute("SELECT DISTINCT source_system FROM logs").fetchall()
    assert sources_bench == [("paas_platform",)], f"Unexpected source values: {sources_bench}"
    assert sources_eval  == [("paas_platform",)], f"Unexpected source values: {sources_eval}"
    logging.info("Verification passed: all rows source_system='paas_platform', no label leak")

    # ── Summary ───────────────────────────────────────────────────────────────
    total     = eval_conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    inc_count = eval_conn.execute("SELECT COUNT(*) FROM logs WHERE incident_id IS NOT NULL").fetchone()[0]
    hdfs_count = total - inc_count

    print(f"""
┌──────────────────────────────────────────────────────────────┐
│                   Benchmark DB summary                       │
├──────────────────────────────────────────────────────────────┤
│  Total rows                  : {total:>6,}                       │
│    Background (HDFS→paas)    : {hdfs_count:>6,}                       │
│    Incident entries          : {inc_count:>6,}                       │
├──────────────────────────────────────────────────────────────┤
│  source_system               : paas_platform (all rows)      │
├──────────────────────────────────────────────────────────────┤
│  eval_db.sqlite              : includes ground-truth labels  │
│  benchmark_db.sqlite         : clean stream, no labels       │
└──────────────────────────────────────────────────────────────┘

Evaluation join (cross-file, by row_uuid):
  ATTACH 'eval_db.sqlite' AS truth;
  SELECT b.timestamp, b.component, b.message,
         t.incident_id, t.root_cause
  FROM logs b
  JOIN truth.logs t USING (row_uuid)
  WHERE t.incident_id = 'INC-008'
  ORDER BY b.timestamp;
""")

    eval_conn.close()
    bench_conn.close()
    logging.info("Done →  %s  |  %s", args.eval_db, args.benchmark_db)


if __name__ == "__main__":
    main()
