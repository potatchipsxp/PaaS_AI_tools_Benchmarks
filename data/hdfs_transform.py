#!/usr/bin/env python3
"""
HDFS Log Transformer
Transforms LogHub HDFS logs into a unified PaaS diagnostic log format (JSONL).

Usage:
    python hdfs_transform.py input.log output.jsonl
    python hdfs_transform.py input.log output.jsonl --use-llm
"""

import re
import json
import argparse
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------------------
# Schema / dataclass
# ---------------------------------------------------------------------------

@dataclass
class DiagnosticLog:
    timestamp: str
    source_system: str
    component: str
    subcomponent: str
    level: str
    node_id: Optional[str]
    instance_id: Optional[str]
    event_type: str
    message: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    def validate(self) -> list[str]:
        """Return list of validation errors (empty = valid)."""
        errors = []
        # timestamp ISO8601 check
        try:
            datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"Invalid timestamp: {self.timestamp}")
        if not self.source_system:
            errors.append("source_system is required")
        if not self.component:
            errors.append("component is required")
        if self.level not in {"TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL"}:
            errors.append(f"Unexpected log level: {self.level}")
        return errors


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Raw log line regex: YYMMDD HHMMSS thread_id LEVEL class: message
LINE_RE = re.compile(
    r"^(?P<date>\d{6})\s+(?P<time>\d{6})\s+(?P<thread>\d+)\s+"
    r"(?P<level>\w+)\s+(?P<class>[\w.$]+):\s*(?P<message>.+)$"
)

# IP address (first match becomes node_id)
IP_RE = re.compile(r"/?((?:\d{1,3}\.){3}\d{1,3}):\d+")

# Block ID
BLOCK_RE = re.compile(r"(blk_-?\d+)")

# Component mapping: class substring → generic component
COMPONENT_MAP = [
    ("DataNode",      "STORAGE_NODE"),
    ("NameNode",      "METADATA_SERVICE"),
    ("FSNamesystem",  "FILESYSTEM_MANAGER"),
    ("BlockManager",  "BLOCK_MANAGER"),
    ("DataTransfer",  "DATA_TRANSFER"),
    ("Client",        "CLIENT"),
    ("LeaseManager",  "LEASE_MANAGER"),
    ("SecondaryNameNode", "SECONDARY_METADATA"),
]

# Event-type classification rules: (regex_pattern, event_type)
EVENT_RULES = [
    (re.compile(r"\b(receiving|received)\s+block\b", re.I),     "data_transfer"),
    (re.compile(r"\b(replicat|replication)\b",        re.I),     "replication"),
    (re.compile(r"\b(delet|remov)\w*\s+block\b",      re.I),     "deletion"),
    (re.compile(r"\b(heartbeat|heart beat)\b",         re.I),     "heartbeat"),
    (re.compile(r"\b(served block|serving block)\b",   re.I),     "block_serve"),
    (re.compile(r"\b(add(ed|ing)?|register)\b.*block\b", re.I),  "block_add"),
    (re.compile(r"\b(exception|error|failed|failure)\b", re.I),  "error"),
    (re.compile(r"\b(connect|disconnect|lost|shutdown)\b", re.I),"connection"),
    (re.compile(r"\b(namenode|checkpoint)\b",          re.I),     "metadata_op"),
    (re.compile(r"\bblock\b",                          re.I),     "block_op"),
]


def parse_timestamp(date_str: str, time_str: str) -> str:
    """Convert YYMMDD + HHMMSS to ISO8601 UTC string."""
    dt = datetime.strptime(date_str + time_str, "%y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def map_component(class_name: str) -> str:
    for key, component in COMPONENT_MAP:
        if key in class_name:
            return component
    return "UNKNOWN"


def classify_event(message: str, class_name: str) -> str:
    for pattern, event_type in EVENT_RULES:
        if pattern.search(message):
            return event_type
    return "general"


def extract_node_id(message: str) -> Optional[str]:
    match = IP_RE.search(message)
    return match.group(1) if match else None


def extract_block_id(message: str) -> Optional[str]:
    match = BLOCK_RE.search(message)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# LLM fallback (optional)
# ---------------------------------------------------------------------------

def classify_with_llm(message: str, class_name: str) -> str:
    """
    Use the Anthropic API to classify ambiguous event types.
    Requires ANTHROPIC_API_KEY in the environment.
    Falls back to 'general' on any error.
    """
    try:
        import anthropic  # type: ignore
        client = anthropic.Anthropic()
        prompt = (
            f"Classify the following HDFS log message into exactly one short snake_case event type "
            f"(e.g. data_transfer, replication, deletion, heartbeat, block_op, error, connection, metadata_op, general).\n"
            f"Class: {class_name}\nMessage: {message}\n"
            f"Respond with only the event type, nothing else."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip().lower().replace(" ", "_")
    except Exception as exc:
        logging.warning("LLM classification failed (%s); falling back to 'general'.", exc)
        return "general"


# ---------------------------------------------------------------------------
# Core transformer
# ---------------------------------------------------------------------------

def transform_line(
    raw_line: str,
    source_file: str,
    use_llm: bool = False,
) -> Optional[DiagnosticLog]:
    """Parse one raw HDFS log line and return a DiagnosticLog, or None on failure."""
    raw_line = raw_line.rstrip()
    if not raw_line:
        return None

    match = LINE_RE.match(raw_line)
    if not match:
        return None

    groups = match.groupdict()
    timestamp    = parse_timestamp(groups["date"], groups["time"])
    thread_id    = int(groups["thread"])
    level        = groups["level"].upper()
    class_name   = groups["class"]
    message      = groups["message"].strip()

    component    = map_component(class_name)
    event_type   = classify_event(message, class_name)

    # Use LLM for anything classified as generic 'block_op' or 'general' if flag set
    if use_llm and event_type in ("block_op", "general"):
        event_type = classify_with_llm(message, class_name)

    node_id  = extract_node_id(message)
    block_id = extract_block_id(message)

    metadata: dict = {"thread_id": thread_id, "source_file": source_file}
    if block_id:
        metadata["block_id"] = block_id

    log = DiagnosticLog(
        timestamp    = timestamp,
        source_system= "hdfs",
        component    = component,
        subcomponent = class_name,
        level        = level,
        node_id      = node_id,
        instance_id  = None,
        event_type   = event_type,
        message      = message,
        metadata     = metadata,
    )
    return log


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def process_file(
    input_path: Path,
    output_path: Path,
    use_llm: bool = False,
    report_every: int = 10_000,
) -> dict:
    """Process the input file line-by-line and write JSONL to output."""
    source_file = input_path.name
    stats = {"total": 0, "converted": 0, "skipped": 0, "invalid": 0, "errors": 0}

    with (
        input_path.open("r", encoding="utf-8", errors="replace") as fin,
        output_path.open("w", encoding="utf-8") as fout,
    ):
        for lineno, raw_line in enumerate(fin, start=1):
            stats["total"] += 1

            try:
                log = transform_line(raw_line, source_file, use_llm=use_llm)
            except Exception as exc:
                logging.warning("Line %d parse error: %s", lineno, exc)
                stats["errors"] += 1
                continue

            if log is None:
                stats["skipped"] += 1
                continue

            validation_errors = log.validate()
            if validation_errors:
                logging.warning("Line %d validation: %s", lineno, "; ".join(validation_errors))
                stats["invalid"] += 1
                # Still emit — annotate with warnings
                d = log.to_dict()
                d["_validation_warnings"] = validation_errors
                fout.write(json.dumps(d) + "\n")
            else:
                fout.write(json.dumps(log.to_dict()) + "\n")
                stats["converted"] += 1

            if lineno % report_every == 0:
                logging.info(
                    "Progress: %d lines processed | converted=%d skipped=%d errors=%d",
                    lineno, stats["converted"], stats["skipped"], stats["errors"],
                )

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transform HDFS LogHub logs → unified PaaS diagnostic JSONL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input_file",  type=Path, help="Path to raw HDFS log file")
    parser.add_argument("output_file", type=Path, help="Destination JSONL file")
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use Claude API for ambiguous event classification (requires ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--report-every",
        type=int,
        default=10_000,
        metavar="N",
        help="Print progress every N lines (default: 10000)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    input_path: Path = args.input_file
    output_path: Path = args.output_file

    if not input_path.exists():
        logging.error("Input file not found: %s", input_path)
        sys.exit(1)

    if args.use_llm:
        logging.info("LLM-assisted classification is ENABLED.")

    logging.info("Starting transformation: %s → %s", input_path, output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stats = process_file(
        input_path,
        output_path,
        use_llm=args.use_llm,
        report_every=args.report_every,
    )

    logging.info("Done!")
    logging.info(
        "Summary: total=%d  converted=%d  skipped=%d  invalid=%d  errors=%d",
        stats["total"], stats["converted"], stats["skipped"], stats["invalid"], stats["errors"],
    )
    success_rate = (stats["converted"] / max(stats["total"], 1)) * 100
    logging.info("Conversion rate: %.1f%%", success_rate)

    if stats["errors"] > 0:
        sys.exit(2)   # partial failure exit code


if __name__ == "__main__":
    main()
