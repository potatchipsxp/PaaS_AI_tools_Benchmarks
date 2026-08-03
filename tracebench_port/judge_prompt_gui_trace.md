# LLM-as-Judge: TraceBench Diagnostic Agent Evaluation

You are evaluating the output of an automated diagnostic agent that investigates faults on an HDFS (Hadoop Distributed File System) cluster, using the TraceBench dataset. The agent is given an operator-style symptom description and has access to two tools: an evidence-retrieval tool over the request's trace data, and a documentation retrieval tool over HDFS fault runbooks. It reasons over the evidence and produces a root-cause diagnosis.

**The evidence-retrieval tool differs by which track produced this run** — check the `model_config.track` field on each result (or just look at the tool names in `tool_call_trace`):
- `A_flat` — the tool is `query_logs`, an LLM SQL sub-agent over a flattened log table.
- `B_native` — the tool is `query_trace`, which reconstructs the request's per-host timeline from the raw trace data (no SQL). Depending on the run (`model_config.retrieval_mode`) it either returns that timeline directly (`"deterministic"`), or routes a natural-language question through a trace sub-agent that reads the timeline and returns a filtered answer (`"agent"`).

This is the one frozen-rubric adaptation the two-track design requires — score both the same way; just recognize either tool name as "the evidence tool" for Dimension 2 below.

I have attached three files:

1. **Diagnostic results** (`diagnostic_results__*.json`) — the agent's output for the TraceBench cases. Each entry contains `incident_id` (this holds the TraceBench `case_id`, e.g. `"TB-018"` — the file uses the generic key name `incident_id` regardless), the operator's `question`, the agent's `diagnosis` text, the `tool_call_trace` (sequence of evidence-tool and `query_docs` calls with their inputs and result summaries), `timing` data, and `model_config` (including which `track` produced this run).

2. **Benchmark cases** (`benchmark_cases_trace.py`) — the 27 cases with their `tier` (**0** = normal control/no fault, **1** = single-component clear functional fault, **2** = cross-component/performance degradation, **3** = multi-factor/subtle/real bugs), the `trace_id` (the specific HDFS request instance named in the question), and the deterministic scorer's keyword signals (`answer_required`, `answer_partial`). You may use these keyword signals as a cross-reference, but they are NOT your source of truth for root cause.

3. **Ground truth** (`data/ground_truth_trace.json`) — the canonical fault for each case. THIS is your source of truth. The canonical field is **`fault_name`** (e.g. `"slowDN"`, `"deadDN"`, or `"none / no fault"` for Tier 0 normal controls) — not `root_cause` (that field doesn't exist in this file). Also present: `affected_component` (the real datanode(s) involved — empty for Tier 0) and `evidence_anchor` (the real quoted exception text or latency figure the fault was verified against). Score each diagnosis against `fault_name`/`affected_component`/`evidence_anchor`, not against the keyword lists and not against your own intuition about what sounds plausible.

## Your task

Score every case in the results file against the canonical ground truth. Use the rubric below. Produce a single JSON array as your final output with one object per case, plus a short summary of cross-cutting observations at the end.

## Scoring rubric

Score each dimension on a 0-2 integer scale per case. Use the full scale — do not default to 1.

### Dimension 1: Root cause correctness (`root_cause_score`)

- **2** — The diagnosis correctly identifies the canonical `fault_name`/mechanism. Paraphrasing and different terminology are fine as long as the underlying mechanism matches. For cases where the visible symptom is on a different host than the actual cause (red herrings — see below), this requires correctly identifying the upstream cause rather than stopping at the symptom.
- **1** — The diagnosis identifies part of the causal chain correctly but stops at a symptom, or names the right fault category but misses a specific mechanism essential to the canonical cause. For red-herring cases, score 1 when the agent correctly described the visible symptom but failed to trace it upstream.
- **0** — The diagnosis is wrong, vacuous, empty, or names an unrelated failure mode.

**Tier 0 (normal control) cases**: `fault_name` is `"none / no fault"`. Score 2 if the diagnosis correctly and explicitly states no fault/anomaly was found, without fabricating one. Score 1 if the diagnosis is hedged/uncertain but leans correctly toward "no fault." Score 0 if the diagnosis fabricates a fault that doesn't exist. Do not apply the red-herring framing to these cases — there is no upstream cause to trace to.

### Dimension 2: Evidence grounding (`evidence_score`)

- **2** — The agent called its evidence tool (`query_logs` or `query_trace`, whichever this track uses — see above) and optionally `query_docs`, cited specific trace evidence in its diagnosis, and that evidence is consistent with the canonical root cause. Reasoning is visibly anchored in what was retrieved.
- **1** — The agent called tools and cited some evidence, but the evidence is vague, partial, or the reasoning chain from evidence to conclusion has a gap.
- **0** — The agent made no tool calls, or called tools but ignored the results, or fabricated specific claims (host names, exception text, latency figures, timestamps) that do not appear in the tool-call trace. An empty diagnosis scores 0.

**Important:** fabricated evidence — specific claims not supported by what the tool trace actually returned — scores 0 on this dimension even if the overall diagnosis happens to match the canonical root cause. Honest uncertainty ("the trace did not contain clear evidence of X") is acceptable and should not be penalized.

### Dimension 3: Fix appropriateness (`fix_score`)

- **2** — The diagnosis proposes a specific, actionable remediation that would actually address the canonical root cause.
- **1** — The proposed fix is directionally correct but too vague to act on, OR addresses the symptom rather than the true root cause.
- **0** — No fix proposed, or the proposed fix is wrong or would not help.

**Tier 0 (normal control) cases**: an appropriate "fix" may simply be "no action needed, continue monitoring" — score 2 if that (or an equivalent statement) is present and appropriate, not as a missing-fix penalty.

## Output format

Return your evaluation as a JSON object with two keys: `cases` (an array of per-case scores) and `summary` (aggregate observations). Do not include any text outside this JSON object. Do not wrap it in markdown code fences.

```json
{
  "cases": [
    {
      "incident_id": "TB-018",
      "tier": 2,
      "track": "A_flat",
      "root_cause_score": 2,
      "root_cause_justification": "One sentence explaining what earned or lost the point.",
      "evidence_score": 2,
      "evidence_justification": "One sentence.",
      "fix_score": 1,
      "fix_justification": "One sentence.",
      "is_red_herring_case": false,
      "identified_red_herring_correctly": null,
      "fabrication_detected": false,
      "notes": ""
    }
  ],
  "summary": {
    "n_cases_scored": 27,
    "mean_root_cause_score": 0.0,
    "mean_evidence_score": 0.0,
    "mean_fix_score": 0.0,
    "mean_total_score": 0.0,
    "n_fabrications_detected": 0,
    "n_empty_diagnoses": 0,
    "red_herring_summary": {
      "total_red_herring_cases": 0,
      "correctly_identified": 0
    },
    "tier_breakdown": {
      "tier_0": {"n": 0, "mean_total": 0.0},
      "tier_1": {"n": 0, "mean_total": 0.0},
      "tier_2": {"n": 0, "mean_total": 0.0},
      "tier_3": {"n": 0, "mean_total": 0.0}
    },
    "cross_cutting_observations": "2-4 sentences highlighting patterns across cases: common failure modes, what the agent systematically got right or wrong, whether evidence-grounding correlates with root-cause correctness, whether Tier 0 cases were handled differently than faulted ones, etc."
  }
}
```

## Field definitions

- `incident_id` — from the results file; holds the TraceBench `case_id` (e.g. `"TB-018"`).
- `track` — from the result's `model_config.track` (`"A_flat"` or `"B_native"`). Include it so cross-track patterns in `cross_cutting_observations` are traceable.
- Scores are integers 0, 1, or 2. Justifications are one sentence each and must be specific about what in the diagnosis earned or lost the point.
- `is_red_herring_case` — you decide this by reading the canonical `fault_name`/`affected_component` and comparing to where the operator's reported symptoms are observed. If the visible symptom is reported from one host (e.g. a client seeing a bare IP in an exception) but the canonical cause is a different, specific host, that's a red herring. TraceBench cases were not explicitly authored with this property as a design goal (unlike the original PaaS incident set) — apply the same definition per-case based on what you actually read, rather than assuming most cases have it. Always `false` for Tier 0.
- `identified_red_herring_correctly` — `true` if `is_red_herring_case` is true AND the agent traced through to the upstream cause; `false` if it is a red herring and the agent stopped at the symptom; `null` if not a red herring.
- `fabrication_detected` — `true` if the diagnosis contains specific claims (host names, exception text, latency numbers, timestamps) not supported by the tool-call trace results; otherwise `false`.
- `notes` — per-case observations worth flagging, or empty string. Examples: "agent produced empty diagnosis", "diagnosis correct but orchestrator made zero tool calls", "hallucinated a datanode that isn't in the trace".

## Rules

- Score every case in the results file. If the file contains 27 cases, the `cases` array has 27 entries; if it's a partial run (`--tier`/`--limit`), score exactly what's present.
- Ground truth comes from `data/ground_truth_trace.json`'s `fault_name`/`affected_component`/`evidence_anchor`. Not from the `answer_required`/`answer_partial` keyword lists. Not from your own beliefs about what sounds right for the described symptoms.
- If the agent produced a diagnosis that sounds reasonable but does not match the canonical `fault_name`, it is incorrect. Do not be charitable on semantic grounds — a diagnosis of "network issue" is NOT a 2 when the canonical cause is specifically "a DataNode process that crashed, producing immediate connection-refused errors."
- If the tool-call trace is empty AND the diagnosis is nonempty, this is strong evidence of either fabrication or knowledge-based guessing. Score evidence 0 and note it.
- If the tool-call trace is empty AND the diagnosis is also empty, score all three dimensions 0 and note it as an empty diagnosis.
- Keep justifications to one sentence. The `cross_cutting_observations` field at the end is where you expand on patterns.

Begin evaluation.
