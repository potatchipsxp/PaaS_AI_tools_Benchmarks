#!/usr/bin/env python3
"""
evaluate.py

Three-layer evaluation of diagnostic agent runs against the 25 benchmark incidents.

Scores each diagnostic result on three independent dimensions:

  1. DOC RETRIEVAL QUALITY
     For each query_docs call in the tool trace, checks whether the retrieved
     documents include at least one doc whose incident_ids contains the target
     incident. Precision and recall are computed per-incident.

  2. REASONING TRACE QUALITY
     Checks whether the agent called the right mix of tools:
       - Did it call query_logs at least once? (grounded in evidence)
       - Did it call query_docs at least once? (consulted documentation)
       - Did it call query_logs before query_docs? (correct order)
       - Did it avoid calling query_docs only without any log evidence?

  3. DIAGNOSTIC ANSWER QUALITY
     Same signal-matching approach as the existing benchmark_incidents.py:
       - full_credit : all answer_required keywords present (case-insensitive)
       - partial     : at least one answer_partial keyword present
       - miss        : none of the above

Input:  diagnostic_results.json  (output of diagnostic_agent.py)
        benchmark_incidents.py   (ground truth cases)
Output: evaluation_report.json
        prints a summary table to stdout

Usage:
    python diagnostic_agent.py   # generates diagnostic_results.json
    python evaluate.py           # scores the results
"""

import json
import sys
from pathlib import Path
from datetime import datetime


# ============================================================================
# GROUND TRUTH
# Import benchmark cases from the existing benchmark_incidents.py
# ============================================================================

try:
    from benchmark_incidents import BENCHMARK_CASES
except ImportError:
    sys.exit(
        "benchmark_incidents.py not found. "
        "Make sure it is in the same directory."
    )

# Build a lookup dict: incident_id → case
CASES_BY_ID = {c["incident_id"]: c for c in BENCHMARK_CASES}


# ============================================================================
# CONFIG
# ============================================================================

# RESULTS_FILE = "diagnostic_results.json"
RESULTS_FILE = "diagnostic_results__diag-qwen25-coder-7b__sql-qwen25-coder-7b__doc-llama32.json"
REPORT_FILE  = "evaluation_report.json"


# ============================================================================
# SCORING FUNCTIONS
# ============================================================================

def score_doc_retrieval(incident_id, tool_call_trace):
    """
    Score retrieval quality for all query_docs calls in the trace.

    For each query_docs call:
      - relevant = True if any retrieved doc has incident_id in its incident_ids
      - precision = relevant_retrieved / total_retrieved
      - recall    = 1 if at least one relevant doc found, else 0

    Returns a dict with per-call details and aggregate scores.
    """
    doc_calls = [t for t in tool_call_trace if t["tool"] == "query_docs"]

    if not doc_calls:
        return {
            "num_calls": 0,
            "calls":     [],
            "precision": None,
            "recall":    0.0,
            "note":      "no query_docs calls made",
        }

    call_scores = []
    any_relevant_found = False

    for call in doc_calls:
        result = call.get("result", {})
        if isinstance(result, str):
            # doc agent returned a plain string (error case)
            call_scores.append({
                "question":   call["inputs"]["question"],
                "n_retrieved": 0,
                "n_relevant":  0,
                "precision":   0.0,
                "docs":        [],
            })
            continue

        retrieved_docs = result.get("retrieved_docs", [])
        n_retrieved = len(retrieved_docs)
        n_relevant  = sum(
            1 for d in retrieved_docs
            if incident_id in d.get("incident_ids", [])
        )

        if n_relevant > 0:
            any_relevant_found = True

        call_scores.append({
            "question":    call["inputs"]["question"],
            "n_retrieved": n_retrieved,
            "n_relevant":  n_relevant,
            "precision":   n_relevant / n_retrieved if n_retrieved > 0 else 0.0,
            "docs":        [
                {
                    "doc_id":       d["doc_id"],
                    "doc_type":     d["doc_type"],
                    "incident_ids": d["incident_ids"],
                    "relevant":     incident_id in d.get("incident_ids", []),
                    "distance":     d.get("distance"),
                }
                for d in retrieved_docs
            ],
        })

    # Aggregate precision = mean of per-call precisions
    precisions = [c["precision"] for c in call_scores if c["n_retrieved"] > 0]
    avg_precision = sum(precisions) / len(precisions) if precisions else 0.0

    return {
        "num_calls":  len(doc_calls),
        "calls":      call_scores,
        "precision":  round(avg_precision, 3),
        "recall":     1.0 if any_relevant_found else 0.0,
        "note":       "",
    }


def score_reasoning_trace(incident_id, tool_call_trace, tier):
    """
    Score the quality of the agent's reasoning process.

    Checks:
      - called_logs     : did the agent query the log database?
      - called_docs     : did the agent query the documentation?
      - logs_before_docs: did it look at logs before consulting docs?
      - no_doc_only     : did it avoid reaching a conclusion from docs alone?
      - appropriate_depth: tier-appropriate number of tool calls

    Returns a dict with individual checks and an overall trace_score (0-4).
    """
    tool_sequence = [t["tool"] for t in tool_call_trace]

    called_logs  = "query_logs" in tool_sequence
    called_docs  = "query_docs" in tool_sequence
    n_log_calls  = tool_sequence.count("query_logs")
    n_doc_calls  = tool_sequence.count("query_docs")

    # logs_before_docs: first query_logs appears before first query_docs
    try:
        first_log = tool_sequence.index("query_logs")
        first_doc = tool_sequence.index("query_docs")
        logs_before_docs = first_log < first_doc
    except ValueError:
        logs_before_docs = False  # one or both not called

    no_doc_only = called_logs  # must have log calls to not be doc-only

    # Tier-appropriate depth:
    #   Tier 1: 1-3 total tool calls is sufficient
    #   Tier 2: 2-5 tool calls expected
    #   Tier 3: 3-6 tool calls expected
    total_calls = len(tool_call_trace)
    depth_thresholds = {1: (1, 4), 2: (2, 6), 3: (3, 8)}
    min_calls, max_calls = depth_thresholds.get(tier, (1, 8))
    appropriate_depth = min_calls <= total_calls <= max_calls

    score = sum([
        called_logs,
        called_docs,
        logs_before_docs,
        no_doc_only,
        appropriate_depth,
    ])

    return {
        "called_logs":       called_logs,
        "called_docs":       called_docs,
        "logs_before_docs":  logs_before_docs,
        "no_doc_only":       no_doc_only,
        "appropriate_depth": appropriate_depth,
        "n_log_calls":       n_log_calls,
        "n_doc_calls":       n_doc_calls,
        "total_calls":       total_calls,
        "trace_score":       score,       # 0–5
        "trace_score_max":   5,
    }


def score_answer(incident_id, diagnosis):
    """
    Score the final diagnosis text against ground-truth keyword signals.
    Mirrors the scoring logic in benchmark_incidents.py.

    Returns: "full_credit" | "partial" | "miss"
    """
    case = CASES_BY_ID.get(incident_id)
    if case is None:
        return "unknown_incident"

    diagnosis_lower = diagnosis.lower()

    required = case.get("answer_required", [])
    partial  = case.get("answer_partial", [])

    if required and all(kw.lower() in diagnosis_lower for kw in required):
        return "full_credit"
    if partial and any(kw.lower() in diagnosis_lower for kw in partial):
        return "partial"
    return "miss"


# ============================================================================
# MAIN EVALUATION
# ============================================================================

def evaluate(results_file=RESULTS_FILE, report_file=REPORT_FILE):
    # Load diagnostic results
    results_path = Path(results_file)
    if not results_path.exists():
        sys.exit(f"Results file not found: {results_file}\nRun diagnostic_agent.py first.")

    with open(results_path) as f:
        results = json.load(f)

    print(f"Evaluating {len(results)} diagnostic result(s)...")
    print()

    report_cases = []
    answer_counts = {"full_credit": 0, "partial": 0, "miss": 0, "error": 0}
    retrieval_recalls   = []
    retrieval_precisions = []
    trace_scores        = []

    for result in results:
        incident_id = result.get("incident_id", "UNKNOWN")
        diagnosis   = result.get("diagnosis", "")
        trace       = result.get("tool_call_trace", [])
        status      = result.get("status", "ok")

        case = CASES_BY_ID.get(incident_id, {})
        tier = case.get("tier", 1)

        # Score all three layers
        retrieval_score = score_doc_retrieval(incident_id, trace)
        trace_score     = score_reasoning_trace(incident_id, trace, tier)
        answer_grade    = score_answer(incident_id, diagnosis) if status == "ok" else "error"

        # Accumulate for summary
        if retrieval_score["recall"] is not None:
            retrieval_recalls.append(retrieval_score["recall"])
        if retrieval_score["precision"] is not None:
            retrieval_precisions.append(retrieval_score["precision"])
        trace_scores.append(trace_score["trace_score"])
        answer_counts[answer_grade] = answer_counts.get(answer_grade, 0) + 1

        case_report = {
            "incident_id":      incident_id,
            "tier":             tier,
            "status":           status,
            "answer_grade":     answer_grade,
            "retrieval":        retrieval_score,
            "trace":            trace_score,
            "diagnosis_excerpt": diagnosis[:300] + "..." if len(diagnosis) > 300 else diagnosis,
        }
        report_cases.append(case_report)

        # Print per-incident summary
        recall_str    = f"{retrieval_score['recall']:.1f}" if retrieval_score['recall'] is not None else " — "
        precision_str = f"{retrieval_score['precision']:.2f}" if retrieval_score['precision'] is not None else " — "
        print(
            f"  {incident_id} [T{tier}]  "
            f"answer={answer_grade:12}  "
            f"trace={trace_score['trace_score']}/5  "
            f"ret_recall={recall_str}  "
            f"ret_prec={precision_str}  "
            f"tools={trace_score['total_calls']}"
        )

    # Summary
    n = len(results)
    avg_recall    = sum(retrieval_recalls)    / len(retrieval_recalls)    if retrieval_recalls    else 0
    avg_precision = sum(retrieval_precisions) / len(retrieval_precisions) if retrieval_precisions else 0
    avg_trace     = sum(trace_scores)         / len(trace_scores)         if trace_scores         else 0

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Incidents evaluated : {n}")
    print()
    print("  Answer quality:")
    print(f"    full_credit : {answer_counts['full_credit']:3}  ({100*answer_counts['full_credit']/n:.0f}%)")
    print(f"    partial     : {answer_counts['partial']:3}  ({100*answer_counts['partial']/n:.0f}%)")
    print(f"    miss        : {answer_counts['miss']:3}  ({100*answer_counts['miss']/n:.0f}%)")
    if answer_counts.get("error", 0):
        print(f"    error       : {answer_counts['error']:3}")
    print()
    print("  Retrieval quality:")
    print(f"    avg recall    : {avg_recall:.3f}  (did any retrieved doc match the incident?)")
    print(f"    avg precision : {avg_precision:.3f}  (fraction of retrieved docs that were relevant)")
    print()
    print("  Reasoning trace:")
    print(f"    avg trace score : {avg_trace:.2f} / 5")
    print("=" * 70)

    # Write full report
    report = {
        "evaluated_at": datetime.utcnow().isoformat() + "Z",
        "n_incidents":  n,
        "summary": {
            "answer": answer_counts,
            "retrieval": {
                "avg_recall":    round(avg_recall, 3),
                "avg_precision": round(avg_precision, 3),
            },
            "trace": {
                "avg_score":     round(avg_trace, 2),
                "max_score":     5,
            },
        },
        "cases": report_cases,
    }

    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Full report saved to {report_file}")

    return report


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    evaluate()
