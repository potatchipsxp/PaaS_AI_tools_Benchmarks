#!/usr/bin/env python3
"""
evaluate_trace.py

Phase 5 deterministic evaluator for the TraceBench port. Adapted from
Results/evaluate.py (the PaaS evaluator) -- same three-layer structure and
scoring philosophy (frozen, per CHANGELOG_PORT.md's Cleanliness Contract),
plus one new layer the spec's Phase 5.3 recommends. Shared across BOTH
tracks: which results file you point it at determines which track gets
scored; the one place the scoring logic itself forks is
score_reasoning_trace's evidence-tool check (query_logs for Track A,
query_trace for Track B), detected from each result's
model_config["track"].

Scores each diagnostic result on four independent dimensions:

  1. DOC RETRIEVAL QUALITY
     For each query_docs call, checks whether the retrieved documents
     include at least one doc whose case_ids contains the target case.
     (Renamed from incident_ids -- see Phase 3's build_doc_index.py fix.)

  2. REASONING TRACE QUALITY
     Did the agent call its evidence-retrieval tool (query_logs for Track A,
     query_trace for Track B) and query_docs, in the right order, at a
     tier-appropriate depth?

  3. DIAGNOSTIC ANSWER QUALITY
     Same signal-matching approach as benchmark_cases_trace.py:
       full_credit / partial / miss against answer_required/answer_partial.

  4. LOCALIZATION  (new -- spec 5.3, not present in the PaaS evaluator)
     Did the diagnosis name the real affected_component (e.g. "datanode046")
     from the isolated ground_truth_trace.json? For Tier 0 (normal
     controls, affected_component == []) this is reported as "n/a", not
     scored as a miss -- there's nothing to localize.

Input:  a diagnostic_results__*.json file (output of run_benchmark.py,
        either track)
        benchmark_cases_trace.py (question-side ground truth: tier,
        answer_required/partial)
        data/ground_truth_trace.json (isolated ground truth: affected_component,
        used ONLY here, post-hoc -- never shown to the agent)
Output: evaluation_report__*.json + a summary table to stdout

Usage:
    python evaluate_trace.py Results/diagnostic_results__...__track-Aflat.json
    python evaluate_trace.py track_b/Results/diagnostic_results__...__track-Bnative.json
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

from benchmark_cases_trace import BENCHMARK_CASES_TRACE

CASES_BY_ID = {c["case_id"]: c for c in BENCHMARK_CASES_TRACE}

SCRIPT_DIR = Path(__file__).resolve().parent
GROUND_TRUTH_PATH = SCRIPT_DIR / "data" / "ground_truth_trace.json"
with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
    GROUND_TRUTH_BY_ID = {c["case_id"]: c for c in json.load(f)}


# ============================================================================
# EVIDENCE TOOL NAME PER TRACK
#
# The one place scoring itself forks between tracks -- Track A's evidence
# tool is query_logs (an LLM SQL sub-agent), Track B's is query_trace
# (deterministic, see track_b/query_trace.py). Everything else in this file
# is track-agnostic.
# ============================================================================

EVIDENCE_TOOL_BY_TRACK = {
    "A_flat":   "query_logs",
    "B_native": "query_trace",
}


def _evidence_tool_for(track):
    return EVIDENCE_TOOL_BY_TRACK.get(track, "query_logs")


# ============================================================================
# SCORING FUNCTIONS
# ============================================================================

def score_doc_retrieval(case_id, tool_call_trace):
    """
    Score retrieval quality for all query_docs calls in the trace.

    For each query_docs call:
      - relevant = True if any retrieved doc has case_id in its case_ids
      - precision = relevant_retrieved / total_retrieved
      - recall    = 1 if at least one relevant doc found, else 0
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
            if case_id in d.get("case_ids", [])
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
                    "doc_id":   d["doc_id"],
                    "doc_type": d["doc_type"],
                    "case_ids": d["case_ids"],
                    "relevant": case_id in d.get("case_ids", []),
                    "distance": d.get("distance"),
                }
                for d in retrieved_docs
            ],
        })

    precisions = [c["precision"] for c in call_scores if c["n_retrieved"] > 0]
    avg_precision = sum(precisions) / len(precisions) if precisions else 0.0

    return {
        "num_calls":  len(doc_calls),
        "calls":      call_scores,
        "precision":  round(avg_precision, 3),
        "recall":     1.0 if any_relevant_found else 0.0,
        "note":       "",
    }


def score_reasoning_trace(case_id, tool_call_trace, tier, track):
    """
    Score the quality of the agent's reasoning process.

    Checks:
      - called_evidence_tool : did the agent call its evidence tool
                                (query_logs for Track A, query_trace for B)?
      - called_docs          : did the agent query the documentation?
      - evidence_before_docs : did it look at evidence before consulting docs?
      - no_doc_only          : did it avoid reaching a conclusion from docs alone?
      - appropriate_depth    : tier-appropriate number of tool calls

    Returns a dict with individual checks and an overall trace_score (0-5).
    """
    evidence_tool = _evidence_tool_for(track)
    tool_sequence = [t["tool"] for t in tool_call_trace]

    called_evidence = evidence_tool in tool_sequence
    called_docs     = "query_docs" in tool_sequence
    n_evidence_calls = tool_sequence.count(evidence_tool)
    n_doc_calls       = tool_sequence.count("query_docs")

    try:
        first_evidence = tool_sequence.index(evidence_tool)
        first_doc      = tool_sequence.index("query_docs")
        evidence_before_docs = first_evidence < first_doc
    except ValueError:
        evidence_before_docs = False  # one or both not called

    no_doc_only = called_evidence

    # Tier-appropriate depth (same thresholds as the PaaS evaluator):
    #   Tier 0/1: 1-4 total tool calls is sufficient
    #   Tier 2: 2-6 tool calls expected
    #   Tier 3: 3-8 tool calls expected
    total_calls = len(tool_call_trace)
    depth_thresholds = {0: (1, 4), 1: (1, 4), 2: (2, 6), 3: (3, 8)}
    min_calls, max_calls = depth_thresholds.get(tier, (1, 8))
    appropriate_depth = min_calls <= total_calls <= max_calls

    score = sum([
        called_evidence,
        called_docs,
        evidence_before_docs,
        no_doc_only,
        appropriate_depth,
    ])

    return {
        "evidence_tool":         evidence_tool,
        "called_evidence_tool":  called_evidence,
        "called_docs":           called_docs,
        "evidence_before_docs":  evidence_before_docs,
        "no_doc_only":           no_doc_only,
        "appropriate_depth":     appropriate_depth,
        "n_evidence_calls":      n_evidence_calls,
        "n_doc_calls":           n_doc_calls,
        "total_calls":           total_calls,
        "trace_score":           score,       # 0-5
        "trace_score_max":       5,
    }


def score_answer(case_id, diagnosis):
    """
    Score the final diagnosis text against ground-truth keyword signals.

    Returns: "full_credit" | "partial" | "miss"
    """
    case = CASES_BY_ID.get(case_id)
    if case is None:
        return "unknown_case"

    diagnosis_lower = diagnosis.lower()

    required = case.get("answer_required", [])
    partial  = case.get("answer_partial", [])

    if required and all(kw.lower() in diagnosis_lower for kw in required):
        return "full_credit"
    if partial and any(kw.lower() in diagnosis_lower for kw in partial):
        return "partial"
    return "miss"


def score_localization(case_id, diagnosis):
    """
    Spec 5.3: did the diagnosis name the real affected_component (e.g. a
    specific datanode)? Reads data/ground_truth_trace.json -- the isolated
    ground truth, never shown to the agent -- post-hoc only.

    Returns: "hit" | "miss" | "n/a" (Tier 0 normal controls have no
    affected_component to localize -- scoring them as a miss would
    penalize a case type that structurally can't have this evidence).
    """
    gt = GROUND_TRUTH_BY_ID.get(case_id)
    if gt is None:
        return "unknown_case"

    affected = gt.get("affected_component", [])
    if not affected:
        return "n/a"

    diagnosis_lower = diagnosis.lower()
    return "hit" if any(a.lower() in diagnosis_lower for a in affected) else "miss"


# ============================================================================
# TIMING AGGREGATION -- identical logic to the PaaS evaluator, generalized
# to read sql_ms_total (Track A) or trace_ms_total (Track B) under one key.
# ============================================================================

def _percentile(values, pct):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _evidence_ms(timing):
    """Track A's timing dict has sql_ms_total; Track B's has trace_ms_total."""
    return timing.get("sql_ms_total", timing.get("trace_ms_total", 0))


def _summarize_timings(timings):
    if not timings:
        return {}

    def _stats(timing_list):
        if not timing_list:
            return None
        totals  = [t["total_seconds"]    for t in timing_list]
        ev_ms   = [_evidence_ms(t)              for t in timing_list]
        doc_ms  = [t.get("doc_ms_total", 0)     for t in timing_list]
        orch_ms = [t.get("orchestrator_ms", 0)  for t in timing_list]
        n_calls = [t.get("n_tool_calls", 0)     for t in timing_list]
        return {
            "n":                  len(timing_list),
            "total_seconds_mean": round(sum(totals) / len(totals), 2),
            "total_seconds_med":  round(_percentile(totals, 50), 2),
            "total_seconds_p95":  round(_percentile(totals, 95), 2),
            "total_seconds_min":  round(min(totals), 2),
            "total_seconds_max":  round(max(totals), 2),
            "evidence_ms_mean":   round(sum(ev_ms) / len(ev_ms), 1),
            "doc_ms_mean":        round(sum(doc_ms) / len(doc_ms), 1),
            "orchestrator_ms_mean": round(sum(orch_ms) / len(orch_ms), 1),
            "n_tool_calls_mean":  round(sum(n_calls) / len(n_calls), 2),
            "orchestrator_share": round(
                sum(orch_ms) / (sum(orch_ms) + sum(ev_ms) + sum(doc_ms)), 3
            ) if (sum(orch_ms) + sum(ev_ms) + sum(doc_ms)) > 0 else 0.0,
        }

    overall = _stats([t for _, t in timings])
    by_tier = {
        f"tier_{tier}": _stats([t for tt, t in timings if tt == tier])
        for tier in sorted({tt for tt, _ in timings})
    }
    return {"overall": overall, "by_tier": by_tier}


# ============================================================================
# MAIN EVALUATION
# ============================================================================

def evaluate(results_file, report_file=None):
    results_path = Path(results_file)
    if not results_path.exists():
        sys.exit(f"Results file not found: {results_file}\nRun run_benchmark.py first.")

    if report_file is None:
        report_file = results_path.with_name(
            results_path.name.replace("diagnostic_results", "evaluation_report")
        )

    with open(results_path) as f:
        results = json.load(f)

    print(f"Evaluating {len(results)} diagnostic result(s)...")
    print()

    report_cases = []
    answer_counts       = {"full_credit": 0, "partial": 0, "miss": 0, "error": 0}
    localization_counts = {"hit": 0, "miss": 0, "n/a": 0}
    retrieval_recalls    = []
    retrieval_precisions = []
    trace_scores         = []
    timings              = []
    tracks_seen           = set()

    for result in results:
        case_id   = result.get("incident_id", "UNKNOWN")  # diagnose()'s param name; holds the case_id value
        diagnosis = result.get("diagnosis", "")
        trace     = result.get("tool_call_trace", [])
        status    = result.get("status", "ok")
        timing    = result.get("timing", {})
        track     = result.get("model_config", {}).get("track", "unknown")
        tracks_seen.add(track)

        case = CASES_BY_ID.get(case_id, {})
        tier = case.get("tier", 1)

        retrieval_score = score_doc_retrieval(case_id, trace)
        trace_score     = score_reasoning_trace(case_id, trace, tier, track)
        answer_grade    = score_answer(case_id, diagnosis) if status == "ok" else "error"
        localization    = score_localization(case_id, diagnosis) if status == "ok" else "error"

        if retrieval_score["recall"] is not None:
            retrieval_recalls.append(retrieval_score["recall"])
        if retrieval_score["precision"] is not None:
            retrieval_precisions.append(retrieval_score["precision"])
        trace_scores.append(trace_score["trace_score"])
        answer_counts[answer_grade] = answer_counts.get(answer_grade, 0) + 1
        if localization in localization_counts:
            localization_counts[localization] += 1
        if timing:
            timings.append((tier, timing))

        case_report = {
            "case_id":          case_id,
            "tier":             tier,
            "track":            track,
            "status":           status,
            "answer_grade":     answer_grade,
            "localization":     localization,
            "retrieval":        retrieval_score,
            "trace":            trace_score,
            "timing":           timing,
            "diagnosis_excerpt": diagnosis[:300] + "..." if len(diagnosis) > 300 else diagnosis,
        }
        report_cases.append(case_report)

        recall_str    = f"{retrieval_score['recall']:.1f}" if retrieval_score['recall'] is not None else " — "
        precision_str = f"{retrieval_score['precision']:.2f}" if retrieval_score['precision'] is not None else " — "
        time_str      = f"{timing['total_seconds']:5.1f}s" if timing else "  — "
        print(
            f"  {case_id} [T{tier}]  "
            f"answer={answer_grade:12}  "
            f"loc={localization:4}  "
            f"trace={trace_score['trace_score']}/5  "
            f"ret_recall={recall_str}  "
            f"ret_prec={precision_str}  "
            f"tools={trace_score['total_calls']:2}  "
            f"time={time_str}"
        )

    n = len(results)
    avg_recall    = sum(retrieval_recalls)    / len(retrieval_recalls)    if retrieval_recalls    else 0
    avg_precision = sum(retrieval_precisions) / len(retrieval_precisions) if retrieval_precisions else 0
    avg_trace     = sum(trace_scores)         / len(trace_scores)         if trace_scores         else 0
    timing_summary = _summarize_timings(timings)

    localizable = n - localization_counts["n/a"]
    loc_rate = localization_counts["hit"] / localizable if localizable else None

    if len(tracks_seen) > 1:
        print(f"\n  WARNING: results file mixes multiple tracks {tracks_seen} — "
              f"evidence-tool checks may be inconsistent across cases.")

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Track               : {', '.join(sorted(tracks_seen))}")
    print(f"  Cases evaluated      : {n}")
    print()
    print("  Answer quality:")
    print(f"    full_credit : {answer_counts['full_credit']:3}  ({100*answer_counts['full_credit']/n:.0f}%)")
    print(f"    partial     : {answer_counts['partial']:3}  ({100*answer_counts['partial']/n:.0f}%)")
    print(f"    miss        : {answer_counts['miss']:3}  ({100*answer_counts['miss']/n:.0f}%)")
    if answer_counts.get("error", 0):
        print(f"    error       : {answer_counts['error']:3}")
    print()
    print("  Localization (named the real affected datanode):")
    print(f"    hit  : {localization_counts['hit']:3}")
    print(f"    miss : {localization_counts['miss']:3}")
    print(f"    n/a  : {localization_counts['n/a']:3}  (Tier 0 normal controls -- nothing to localize)")
    if loc_rate is not None:
        print(f"    hit rate (of localizable cases): {loc_rate:.3f}")
    print()
    print("  Retrieval quality:")
    print(f"    avg recall    : {avg_recall:.3f}  (did any retrieved doc match the case?)")
    print(f"    avg precision : {avg_precision:.3f}  (fraction of retrieved docs that were relevant)")
    print()
    print("  Reasoning trace:")
    print(f"    avg trace score : {avg_trace:.2f} / 5")
    if timing_summary:
        o = timing_summary["overall"]
        print()
        print("  Timing:")
        print(f"    total wall-clock : mean {o['total_seconds_mean']:.1f}s   "
              f"median {o['total_seconds_med']:.1f}s   "
              f"p95 {o['total_seconds_p95']:.1f}s   "
              f"range {o['total_seconds_min']:.1f}s–{o['total_seconds_max']:.1f}s")
        print(f"    mean evidence time : {o['evidence_ms_mean']/1000:.1f}s per case "
              f"(query_logs or query_trace calls)")
        print(f"    mean doc time      : {o['doc_ms_mean']/1000:.1f}s per case")
        print(f"    mean orch time     : {o['orchestrator_ms_mean']/1000:.1f}s per case")
        print(f"    orch share         : {o['orchestrator_share']*100:.0f}% of total tool+orch time")
        if timing_summary.get("by_tier"):
            print()
            print("    by tier (mean total seconds):")
            for tier_key, stats in sorted(timing_summary["by_tier"].items()):
                if stats:
                    print(f"      {tier_key} (n={stats['n']:2}) : "
                          f"{stats['total_seconds_mean']:5.1f}s   "
                          f"tools={stats['n_tool_calls_mean']:.1f}")
    print("=" * 70)

    report = {
        "evaluated_at": datetime.utcnow().isoformat() + "Z",
        "tracks":       sorted(tracks_seen),
        "n_cases":      n,
        "summary": {
            "answer": answer_counts,
            "localization": {
                **localization_counts,
                "hit_rate_of_localizable": round(loc_rate, 3) if loc_rate is not None else None,
            },
            "retrieval": {
                "avg_recall":    round(avg_recall, 3),
                "avg_precision": round(avg_precision, 3),
            },
            "trace": {
                "avg_score":     round(avg_trace, 2),
                "max_score":     5,
            },
            "timing": timing_summary,
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
    ap = argparse.ArgumentParser(description="Score a TraceBench diagnostic_results file (either track).")
    ap.add_argument("results_file", help="path to a diagnostic_results__*.json file")
    ap.add_argument("--output", help="report output path (default: derived from results_file name)")
    args = ap.parse_args()
    evaluate(args.results_file, report_file=args.output)
