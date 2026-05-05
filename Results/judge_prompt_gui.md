# LLM-as-Judge: PaaS Diagnostic Agent Evaluation

You are evaluating the output of an automated diagnostic agent that investigates incidents on a Cloud Foundry-compatible PaaS platform. The agent is given an operator-style symptom description and has access to two tools: a SQL query tool over a log database, and a documentation retrieval tool over platform runbooks. It reasons over the evidence and produces a root-cause diagnosis.

I have attached three files:

1. **Diagnostic results** (`diagnostic_results__*.json`) — the agent's output for 25 incidents. Each entry contains the `incident_id`, the operator's `question`, the agent's `diagnosis` text, the `tool_call_trace` (sequence of `query_logs` and `query_docs` calls with their questions and result summaries), `timing` data, and the `model_config` used for the run.

2. **Benchmark cases** (`benchmark_incidents.py`) — the 25 incidents with their `tier` (1 = single component, 2 = cross-component, 3 = complex distributed), affected `app_id`, `org`, and the deterministic scorer's keyword signals (`answer_required`, `answer_partial`). You may use these keyword signals as a cross-reference, but they are NOT your source of truth for root cause.

3. **Ground truth** (`ground_truth.json`) — the canonical `root_cause` for each incident, pulled from the labeled eval database. THIS is your source of truth. You score each diagnosis against this, not against the keyword lists and not against your own intuition about what sounds plausible.

## Your task

Score every incident in the results file against the canonical root cause. Use the rubric below. Produce a single JSON array as your final output with one object per incident, plus a short summary of cross-cutting observations at the end.

## Scoring rubric

Score each dimension on a 0-2 integer scale per incident. Use the full scale — do not default to 1.

### Dimension 1: Root cause correctness (`root_cause_score`)

- **2** — The diagnosis correctly identifies the canonical root cause. Paraphrasing and different terminology are fine as long as the underlying mechanism matches. For incidents where the visible symptom is in a different component than the actual cause (red herrings), this requires correctly identifying the upstream cause rather than stopping at the symptom.
- **1** — The diagnosis identifies part of the causal chain correctly but stops at a symptom, or names the right component/failure category but misses a specific mechanism that is essential to the canonical cause. For red-herring incidents, score 1 when the agent correctly described the visible symptom but failed to trace it upstream.
- **0** — The diagnosis is wrong, vacuous, empty, or names an unrelated failure mode.

### Dimension 2: Evidence grounding (`evidence_score`)

- **2** — The agent called `query_logs` (and optionally `query_docs`), cited specific log evidence in its diagnosis, and that evidence is consistent with the canonical root cause. Reasoning is visibly anchored in what was retrieved.
- **1** — The agent called tools and cited some evidence, but the evidence is vague, partial, or the reasoning chain from evidence to conclusion has a gap.
- **0** — The agent made no tool calls, or called tools but ignored the results, or fabricated specific claims (log lines, error codes, numerical values, timestamps) that do not appear in the tool-call trace. An empty diagnosis scores 0.

**Important:** fabricated evidence — specific claims not supported by what the tool trace actually returned — scores 0 on this dimension even if the overall diagnosis happens to match the canonical root cause. Honest uncertainty ("the logs did not contain clear evidence of X") is acceptable and should not be penalized.

### Dimension 3: Fix appropriateness (`fix_score`)

- **2** — The diagnosis proposes a specific, actionable remediation that would actually address the canonical root cause.
- **1** — The proposed fix is directionally correct but too vague to act on, OR addresses the symptom rather than the true root cause.
- **0** — No fix proposed, or the proposed fix is wrong or would not help.

## Output format

Return your evaluation as a JSON object with two keys: `cases` (an array of per-incident scores) and `summary` (aggregate observations). Do not include any text outside this JSON object. Do not wrap it in markdown code fences.

```json
{
  "cases": [
    {
      "incident_id": "INC-001",
      "tier": 1,
      "root_cause_score": 2,
      "root_cause_justification": "One sentence explaining what earned or lost the point.",
      "evidence_score": 2,
      "evidence_justification": "One sentence.",
      "fix_score": 1,
      "fix_justification": "One sentence.",
      "is_red_herring_incident": false,
      "identified_red_herring_correctly": null,
      "fabrication_detected": false,
      "notes": ""
    }
  ],
  "summary": {
    "n_incidents_scored": 25,
    "mean_root_cause_score": 0.0,
    "mean_evidence_score": 0.0,
    "mean_fix_score": 0.0,
    "mean_total_score": 0.0,
    "n_fabrications_detected": 0,
    "n_empty_diagnoses": 0,
    "red_herring_summary": {
      "total_red_herring_incidents": 0,
      "correctly_identified": 0
    },
    "tier_breakdown": {
      "tier_1": {"n": 0, "mean_total": 0.0},
      "tier_2": {"n": 0, "mean_total": 0.0},
      "tier_3": {"n": 0, "mean_total": 0.0}
    },
    "cross_cutting_observations": "2-4 sentences highlighting patterns across incidents: common failure modes, what the agent systematically got right or wrong, whether evidence-grounding correlates with root-cause correctness, etc."
  }
}
```

## Field definitions

- `incident_id` — from the results file.
- Scores are integers 0, 1, or 2. Justifications are one sentence each and must be specific about what in the diagnosis earned or lost the point.
- `is_red_herring_incident` — you decide this by reading the canonical root cause and comparing it to where the operator's reported symptoms are. If the visible symptom is in component A but the canonical cause is in component B, it is a red herring. Set `true` or `false`.
- `identified_red_herring_correctly` — `true` if `is_red_herring_incident` is true AND the agent traced through to the upstream cause; `false` if it is a red herring and the agent stopped at the symptom; `null` if not a red herring.
- `fabrication_detected` — `true` if the diagnosis contains specific claims (log lines, error codes, numbers, timestamps) that are not supported by the tool-call trace results; otherwise `false`.
- `notes` — per-incident observations worth flagging, or empty string. Examples: "agent produced empty diagnosis", "diagnosis correct but orchestrator made zero tool calls", "hallucinated a component that does not exist in the platform".

## Rules

- Score every incident in the results file. If the file contains 25 incidents, the `cases` array has 25 entries.
- Ground truth comes from `ground_truth.json`. Not from the `answer_required` keyword lists. Not from your own beliefs about what sounds right for the described symptoms.
- If the agent produced a diagnosis that sounds reasonable but does not match the canonical root cause, it is incorrect. Do not be charitable on semantic grounds — a diagnosis of "database connection issue" is NOT a 2 when the canonical cause is specifically "PostgreSQL connection pool exhausted due to slow queries holding connections."
- If the tool-call trace is empty AND the diagnosis is nonempty, this is strong evidence of either fabrication or knowledge-based guessing. Score evidence 0 and note it.
- If the tool-call trace is empty AND the diagnosis is also empty, score all three dimensions 0 and note it as an empty diagnosis.
- Keep justifications to one sentence. The `cross_cutting_observations` field at the end is where you expand on patterns.

Begin evaluation.
