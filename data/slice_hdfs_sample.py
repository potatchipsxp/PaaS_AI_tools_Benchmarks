#!/usr/bin/env python3
"""
slice_hdfs_sample.py
--------------------
Extract a clean, anomaly-free sample from HDFS_v1 (loghub).

Uses anomaly_label.csv to identify normal block IDs, then collects
complete block traces until the target line count is reached.

Usage:
    python slice_hdfs_sample.py HDFS.log anomaly_label.csv hdfs_sample.log
    python slice_hdfs_sample.py HDFS.log anomaly_label.csv hdfs_sample.log --lines 10000
    python slice_hdfs_sample.py HDFS.log anomaly_label.csv hdfs_sample.log --lines 10000 --seed 42

The output is a plain .log file in the same format as the input —
feed it directly into hdfs_transform.py with no further changes.
"""

import re
import csv
import argparse
import logging
import random
import sys
from pathlib import Path
from collections import defaultdict

BLOCK_RE = re.compile(r"blk_-?\d+")


def load_normal_blocks(label_path: Path) -> set:
    """
    Parse anomaly_label.csv  →  set of block IDs that are Normal.

    Expected CSV format (loghub standard):
        BlockId,Label
        blk_-1608999687919862906,Normal
        blk_7503483334202473044,Anomaly
        ...
    """
    normal = set()
    with label_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Column names vary slightly across loghub versions
            block_col  = next((k for k in row if "block" in k.lower() or "id" in k.lower()), None)
            label_col  = next((k for k in row if "label" in k.lower()), None)
            if not block_col or not label_col:
                raise ValueError(f"Cannot find BlockId/Label columns. Got: {list(row.keys())}")
            if row[label_col].strip().lower() == "normal":
                normal.add(row[block_col].strip())
    return normal


def index_blocks(log_path: Path, normal_blocks: set) -> dict:
    """
    Single-pass scan: build a dict of  block_id → [list of raw lines].
    Only keeps lines/blocks that are in normal_blocks.
    Lines with no block ID are attached to the most-recently-seen block
    (covers stack traces / continuation lines).
    """
    logging.info("Scanning %s …", log_path)
    block_lines: dict[str, list] = defaultdict(list)
    current_block = None

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            m = BLOCK_RE.search(raw)
            if m:
                bid = m.group()
                if bid in normal_blocks:
                    current_block = bid
                    block_lines[current_block].append(raw)
                else:
                    current_block = None   # anomalous block — skip
            elif current_block is not None:
                # continuation / stack trace line — attach to current block
                block_lines[current_block].append(raw)

    logging.info("Found %d unique normal blocks in log file.", len(block_lines))
    return block_lines


def sample_blocks(block_lines: dict, target_lines: int, seed: int) -> list:
    """
    Randomly select complete block traces until we hit target_lines.
    Returns list of raw log lines (preserving intra-block order).
    """
    rng = random.Random(seed)
    block_ids = list(block_lines.keys())
    rng.shuffle(block_ids)

    selected_lines = []
    selected_blocks = 0

    for bid in block_ids:
        lines = block_lines[bid]
        selected_lines.extend(lines)
        selected_blocks += 1
        if len(selected_lines) >= target_lines:
            break

    logging.info(
        "Selected %d blocks → %d lines (target was %d).",
        selected_blocks, len(selected_lines), target_lines,
    )
    return selected_lines


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Slice a clean, anomaly-free sample from HDFS_v1 logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("log_file",    type=Path, help="HDFS.log (raw log)")
    parser.add_argument("label_file",  type=Path, help="anomaly_label.csv")
    parser.add_argument("output_file", type=Path, help="Output sample .log file")
    parser.add_argument("--lines",  type=int, default=10_000, help="Target line count (default: 10000)")
    parser.add_argument("--seed",   type=int, default=42,     help="Random seed for reproducibility (default: 42)")
    args = parser.parse_args()

    for p in (args.log_file, args.label_file):
        if not p.exists():
            logging.error("File not found: %s", p)
            sys.exit(1)

    logging.info("Loading normal block IDs from %s …", args.label_file)
    normal_blocks = load_normal_blocks(args.label_file)
    logging.info("%d normal blocks in label file.", len(normal_blocks))

    block_lines = index_blocks(args.log_file, normal_blocks)

    if not block_lines:
        logging.error("No normal blocks found — check that block IDs in the log match the label file.")
        sys.exit(1)

    selected = sample_blocks(block_lines, args.lines, args.seed)

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    with args.output_file.open("w", encoding="utf-8") as f:
        f.writelines(selected)

    logging.info("Written %d lines to %s ✓", len(selected), args.output_file)
    logging.info(
        "Next step:  python hdfs_transform.py %s output.jsonl",
        args.output_file,
    )


if __name__ == "__main__":
    main()
