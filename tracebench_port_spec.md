# Build Spec: Porting the PaaS Diagnostic Benchmark to TraceBench

**Purpose of this document.** This is an implementation spec to be executed one step at a
time, each step in its own working session if desired. The goal is to rerun the existing
PaaS diagnostic benchmark against the TraceBench (HDFS distributed-tracing) dataset, so the
two results can be compared. The comparison answers: *do our model-tier conclusions survive
a change of data source, domain, modality, and ground-truth provenance?*

**What "success" means for the experiment (read first).**
The headline metric is **NOT absolute score parity**. Absolute scores are expected to move
(trace reasoning is harder than log keyword-matching). The robustness signal is whether the
**tier ordering and the gaps between tiers are preserved**. Define success as: high rank
correlation (Spearman) of the model ranking across the two datasets, plus preservation of
per-tier gaps. Fix this in writing before running anything.

**Known confound (carry this through to interpretation).**
This port changes domain (Hadoop vs PaaS) AND modality (traces vs flat logs) at once. If
rankings AGREE, that is strong evidence. If they DIVERGE, you will not immediately know which
shift caused it — Phase 6 includes a tiebreaker run to disentangle.

---

## THE CLEANLINESS CONTRACT (frozen — do not violate in any step)

The project's core principle is *never change two things at once*. This port unavoidably
changes the data AND the data-access tool. Therefore everything else is frozen. The ONLY
sanctioned tool-surface change is the `query_logs` backend (Phase 4).

| Element | Status | Notes |
|---|---|---|
| Orchestrator system prompt (`SYSTEM_PROMPT` in `diagnostic_agent.py`) | **FROZEN** | Not one word changes |
| `query_logs` / `query_docs` tool **interfaces** (NL question in → text answer out) | **FROZEN** | Behind-the-tool implementation may change; signature may not |
| Model tiers, exact model IDs, backends | **FROZEN** | Same sweep as PaaS run |
| Temperatures (all = 0.0) | **FROZEN** | |
| LLM-judge rubric (root cause / evidence / fix, 0–2 each) | **FROZEN** | Reference answer changes; rubric does not |
| Retry / recovery / rate-limit logic | **FROZEN** | |
| Scoring *philosophy* (deterministic keyword/retrieval + LLM judge) | **FROZEN** | Specific keywords/signals change; structure does not |
| Ground-truth isolation (separate DB, never shown to agent) | **FROZEN** | |
| `query_logs` SQL backend (what table it queries) | **SANCTIONED CHANGE** | Phase 4 only |
| Data source | **SANCTIONED CHANGE** | The whole point |
| Doc corpus content | **SANCTIONED CHANGE** | Phase 3 |
| Deterministic scoring keywords/signals | **SANCTIONED CHANGE** | Phase 5 |

**Decisions already made in this spec (override now if you disagree):**
1. Primary path is **Option A: flatten traces into the existing `logs` schema.** Trace-native
   (Option B) is a conditional follow-on, not the lead.
2. Doc corpus **matches the PaaS leakage posture** (per-fault docs) for the primary run.
   Category-only docs are a secondary variant.
3. `query_logs` interface stays identical so the orchestrator and its prompt are untouched.

---

## PHASE 0 — Scaffolding and the "what changed" table

**Precondition:** none. Do this first.

**Steps:**
0.1  Create a sibling project directory, e.g. `PaaS_AI_tools_Benchmarks/tracebench_port/`,
     so the trace experiment never overwrites PaaS files. Mirror the layout:
     `data/`, `Results/`, agent files (copied, then modified), `build_*_index.py`.
0.2  Copy (do not move) the current agent files into it: `diagnostic_agent.py`,
     `sql_agent.py`, `doc_agent.py`, `run_benchmark.py`, `build_doc_index.py`,
     `build_log_index.py`, `benchmark_incidents.py`. These are the baseline you will edit.
0.3  Create `CHANGELOG_PORT.md` containing the Cleanliness Contract table above, with a
     "what changed / what stayed" column you fill in as you go. Every subsequent session
     appends to this. This is the audit trail that proves experimental cleanliness.

**Exit check:** directory exists, files copied, changelog seeded. No code modified yet.

---

## PHASE 1 — Acquire and understand TraceBench

**Precondition:** Phase 0 complete.

**Background (verify against the actual dump on load — 2014 dataset):**
- Source: https://mtracer.github.io/TraceBench/ ; GitHub: mtracer/TraceBench.
- 364 MySQL files, >370,000 traces, >180 hours, collected on a real HDFS/IaaS (CloudStack)
  cluster (1 namenode, 50 datanodes, 50 client hosts).
- Four tables: **Event**, **Edge**, **Trace**, **Operation**.
  - `Event`: TraceID, NID, OpName, StartTime, EndTime, HostAddress, HostName, Agent,
    Description (← exceptions/results live here).
  - `Edge`: TraceID, FatherNID, FatherStartTime, ChildNID (← call structure).
  - `Trace`: TraceID, Title, NumEvents, NumEdges, FirstSeen, LastUpdated, StartTime, EndTime.
  - `Operation`: OpName, Num, MaxLatency, MinLatency, AverageLatency (← latency baselines).
  - Implementation aliases to watch for: Task=Trace, TID=NID, Report=Event, Delay=Latency.
- Trace-set naming = `Class_Type_Fault_Workload_Variable` (regex form), e.g.
  `AN_Net_slowDN_w_3` = Abnormal / Network / slowDN fault / write workload / 3 failed datanodes.
- 17 faults in 5 types (Process, Network, Data, System, Bug) — see Phase 2 mapping table.

**Steps:**
1.1  Download the dump. Load into a **local SQLite** file `data/tracebench_raw.sqlite`
     (SQLite, not MySQL, to reuse the existing SQL tooling). Preserve all four tables.
1.2  Write `inspect_tracebench.py`: print row counts per table, distinct OpName values,
     distinct HostName values, and a sample reconstructed trace (one TraceID's Events+Edges).
     Confirm the field names match the list above; record any deviations in CHANGELOG_PORT.md.
1.3  Build the **set manifest**. Parse every trace-set name into a row:
     `{trace_set_name, is_anomalous(bool), fault_category, fault_name, workload, severity_int}`.
     Store as `data/tracebench_manifest.csv`. This manifest IS your ground-truth key.

**Exit check:** `tracebench_raw.sqlite` loaded; manifest CSV built; field names verified.

---

## PHASE 2 — Case selection, fault→tier mapping, ground truth

**Precondition:** Phase 1 complete.

### 2A. Fault → Tier mapping (mirrors the PaaS Tier 1/2/3 structure)

| Tier | Rationale | TraceBench faults |
|---|---|---|
| **1 — single-component, clear functional** | one datanode, unambiguous signature | `killDN`, `suspendDN`, `readOnlyDN` |
| **2 — cross-component / performance degradation** | latency propagation, partial failure | `slowDN`, `slowHDFS`, `disconnectDN` |
| **3 — multi-factor / subtle / real bugs** | silent corruption, combination, real Hadoop bugs | `corruptBlk`, `corruptMeta`, `cutBlk`, `cutMeta`, `lossBlk`, `lossMeta`, Combination sets (`COM_*`, AnarchyApe `AA`), bugs HADOOP-3257 / HADOOP-6502 / HADOOP-7064 |

Notes for the implementer:
- `panicDN` / `deadDN` are System-type functional faults; place in Tier 1 if their trace
  signature is a clean single-node failure, Tier 2 if it propagates. Decide by inspecting one
  example trace each; record the decision.
- Severity (`Variable` column, 1–5 failed datanodes) is an *intra-tier* difficulty gradient,
  not a tier selector. Prefer mid-severity (2–3) cases for the primary set to avoid trivially
  easy (1) or saturated (5) extremes.

### 2B. Case selection

2.1  Target **25–40 cases** total (comparable to the 25 PaaS incidents; keeps runtime/cost
     parity). Stratify across the three tiers and include **normal (non-anomalous) traces** as
     negative controls (~15–20% of cases) — a good diagnostician should be able to say
     "no fault detected," and weaker models may hallucinate faults here.
2.2  For each selected case, pick one anomalous trace (or a small bundle of traces from one
     fault set) as the unit. Record its TraceID(s) and source trace-set name.

### 2C. Incident question authoring (apply anti-leakage discipline)

2.3  For each case write an **operator-style question describing SYMPTOMS, not the fault.**
     The question may reference what an operator would observe (a write request failing, a read
     returning corrupt data, elevated latency on some operations) but must NOT name the injected
     fault, the fault category, or the target datanode. This mirrors the discipline we want on
     the PaaS side and prevents the answer leaking through the prompt.
     - GOOD: "copyFromLocal requests for app data are failing partway through the write
       pipeline; some datanodes seem unreachable. What is the root cause?"
     - BAD: "A disconnectDN fault on 3 datanodes caused write failures. Confirm it."

### 2D. Ground truth (derived mechanically — this is TraceBench's key advantage)

2.4  For each case, derive ground truth **from the injection record, not from imagination**:
     - `root_cause` = the injected fault name (e.g. `slowDN`).
     - `fault_category` = Process/Network/Data/System/Bug.
     - `affected_component` = the targeted datanode HostName(s) (the localization label).
     - `evidence_anchor` = the actual exception text in `Event.Description` and/or the latency
       inflation visible in the trace for this TraceID. Pull the REAL string from the data —
       never write a phantom signal from memory (existing project principle).
     - For normal controls: `root_cause = "none / no fault"`.
2.5  Store ground truth in a **separate** `data/eval_tracebench.sqlite`, NEVER exposed to the
     agent (same isolation as `eval_db.sqlite`). Mirror the `ground_truth.json` structure the
     PaaS judge already consumes so the judge code barely changes.

**Exit check:** `benchmark_cases_trace.py` (analog of `benchmark_incidents.py`) holds the
25–40 cases with questions; `eval_tracebench.sqlite` + `ground_truth_trace.json` hold the
isolated labels; every retrieval signal verified present in actual log/trace text.

---

## PHASE 3 — Documentation corpus for the doc agent

**Precondition:** Phase 2 complete. **Rationale:** the tool's premise is *assisted* diagnosis
against existing docs — the doc channel must survive the port or you're benchmarking a
different (unassisted) tool.

**Steps:**
3.1  Author a per-fault doc set analogous to the PaaS `runbook_/error_ref_/config_` triple,
     but for HDFS trace faults. Source content from **real Hadoop documentation + the TraceBench
     fault descriptions** so docs are real, not invented. Each doc covers: what the fault is,
     how it manifests in traces (latency inflation / structural change / Description exception),
     how to confirm it, and the remediation.
3.2  **Leakage posture (primary run): per-fault docs**, matching the PaaS posture so the two
     benchmarks differ in DATA, not in difficulty design. Use the same JSONL record shape as
     `generate_doc_corpus.py` (doc_id, doc_type, incident_ids→case_ids, components,
     failure_pattern, tier, title, content). The `case_ids` field is the relevance label.
3.3  **Also author a category-only variant** (one doc per fault *category*, not per fault) for a
     secondary run. The gap between per-fault and category-only on the trace data tells you how
     much the PaaS results depend on doc leakage — a valuable side finding.
3.4  Index with `build_doc_index.py` essentially unchanged (it is content-agnostic; only the
     input path and collection name change). Verify with a couple of retrieval smoke queries.

**Exit check:** two doc corpora built (`doc_corpus_trace_perfault.jsonl`,
`doc_corpus_trace_category.jsonl`), both indexed into separate ChromaDB collections; smoke
retrieval returns sane docs.

---

## PHASE 4 — Adapt the tool (the ONE sanctioned tool change)

**Precondition:** Phase 3 complete. **This is the crux. Lead with Option A.**

### Option A — Flatten traces into the existing `logs` schema (PRIMARY)

Goal: maximal architectural parity. The SQL agent runs almost unchanged; only the underlying
table is rebuilt from trace data. Keep the `query_logs` tool interface identical.

4A.1  Write `build_trace_logs.py` that projects `Event` rows (joined to their `Trace`/`Edge`
      context as needed) into a flat `logs` table matching the EXISTING schema in
      `sql_agent.py`'s `DEFAULT_SCHEMA_DESCRIPTION`:

      | Existing `logs` column | Source from TraceBench |
      |---|---|
      | row_uuid | generated unique id |
      | timestamp | `Event.StartTime` (normalize to ISO-8601) |
      | source_system | constant `'hdfs_tracebench'` |
      | component | `Event.OpName` (or `Event.Agent` = code class) — pick one, document it |
      | subcomponent | the other of OpName/Agent |
      | level | derive: ERROR if `Description` contains an exception, else INFO; WARN for elevated-latency events if detectable |
      | node_id | `Event.HostName` (the datanode) |
      | instance_id | `Event.TraceID` (so a query can group a request) |
      | event_type | coarse class of OpName (read/write/rpc/heartbeat) |
      | message | `Event.Description` (the real text — exceptions live here) |
      | thread_id | `Event.NID` |
      | block_id | NULL (or parse from Description if block ids appear) |
      | source_file | source trace-set name (KEEP OUT of agent-visible columns if it leaks the fault — see 4A.3) |

4A.2  Build the flat table into `data/benchmark_trace_db.sqlite` (the clean stream the agent
      queries) — analogous to `benchmark_db.sqlite`. One table named `logs`.
4A.3  **Leakage audit (critical):** the trace-set name encodes the fault. Ensure NO
      agent-visible column (component, message, event_type, source_file) contains the fault
      name, category, or set name. The fault must be *inferable from symptoms*, not *stated*.
      Run audit SQL: `SELECT DISTINCT message FROM logs` and grep for fault names. This is the
      trace-side analog of the HDFS-vocabulary-contamination audit already on the project radar.
4A.4  Point `sql_agent.py`'s `DB_URI` at `benchmark_trace_db.sqlite`. The
      `DEFAULT_SCHEMA_DESCRIPTION` stays valid because the columns match. **No other change to
      `sql_agent.py`.** Record in CHANGELOG_PORT that only DB_URI changed.
4A.5  Smoke-test the SQL agent standalone (its existing `__main__`) against the new DB with a
      few questions ("what errors appear", "which node has the most errors").

**Pro:** closest possible "same tool, different data." **Con:** discards edge/tree structure;
faults whose signature IS structural (a malformed write subtree) may be degraded or invisible.
That limitation is itself a finding — see Option B trigger.

### Option B — Trace-native query tool (CONDITIONAL follow-on)

Trigger Option B **only if** Option A shows the agent is structurally blind to a fault class
(e.g. Tier 3 corruption/combination faults score near zero because the signal is in trace
structure, not in any single Description line).

4B.1  Keep the four tables in `benchmark_trace_db.sqlite`. Either (a) expand the SQL agent's
      schema description to all four tables and let it write joins, or (b) add a small
      `query_trace(trace_id)` helper tool that reconstructs and returns a trace tree.
4B.2  Keep the tool interface NL→text. If you add `query_trace`, it is an *additional* tool;
      note that this weakens the "same tool" claim, so label Option B runs distinctly.
4B.3  Run the same case set through Option B. Compare A vs B: agreement ⇒ flattening lost
      nothing; divergence ⇒ trace structure carries diagnostic signal the flat PaaS-style tool
      cannot see (important for the real PaaS tool's production design).

**Exit check (Phase 4):** Option A flat DB built, leakage-audited, SQL agent smoke-passes with
only `DB_URI` changed. Option B deferred unless triggered.

---

## PHASE 5 — Adapt scoring and the judge

**Precondition:** Phase 4 complete. **Philosophy frozen; specifics change.**

**Steps:**
5.1  **Deterministic layer** (rewrite keywords/signals to trace ground truth, structure
     unchanged):
     - `answer_required` → fault name + affected node must appear (conjunctive — mirrors the
       OpenRCA-style rigor we discussed; avoids the over-permissive OR-keyword inflation).
     - `retrieval_signals` → "did the doc agent surface the correct fault (per-fault run) or
       correct category (category run) doc."
     - For normal controls → required answer is an explicit "no fault" assertion; penalize
       hallucinated faults.
5.2  **LLM judge** (rubric FROZEN — root cause correctness / evidence grounding / fix
     appropriateness, 0–2 each). Only the *reference answer* fed to the judge changes: it now
     comes from `ground_truth_trace.json`. The judge is domain-agnostic by design, so this is a
     near-zero-code change — verify by diffing against the PaaS judge.
5.3  **Add a localization sub-score** (optional but recommended): did the diagnosis name the
     correct datanode? Cheap to compute from `affected_component`; strong tier discriminator and
     directly analogous to OpenRCA's component+datetime element.
5.4  Keep deterministic and judge scores SEPARATE in the output (as now), so you can see which
     layer moves between datasets.

**Exit check:** `evaluate_trace.py` (analog of `Results/evaluate.py`) runs against a sample
results file and emits per-tier deterministic + judge + localization scores.

---

## PHASE 6 — Run, compare on the right axis, tiebreaker

**Precondition:** Phases 0–5 complete.

**Steps:**
6.1  Run the SAME tier sweep used for the PaaS benchmark (same model configs: Tier 1 edge,
     Tier 2a/2b mid, Tier 3 frontier; same model IDs). Output filenames must encode
     **model combo + dataset** so PaaS and trace results accumulate side by side and never
     overwrite (extend the existing `OUTPUT_FILE` sanitizer to append a `__data-tracebench` tag).
6.2  **Primary comparison:** Spearman rank correlation of the model ranking on PaaS vs trace,
     plus per-tier gap preservation. This single number answers the robustness question.
6.3  **Secondary:** where do absolute scores move most? If Tier 1 holds but Tier 3 collapses on
     traces, the fragility localizes to complex-failure reasoning — the "important question to
     dig into."
6.4  **Tiebreaker for the domain×modality confound:** if rankings diverge, run a
     **flattened-trace + per-incident(per-fault) docs** configuration (already your primary) and
     compare against **flattened-trace + category-only docs**. Holding modality fixed and varying
     only leakage isolates whether divergence is about data difficulty vs genuine capability.
     If still ambiguous, the Option A vs Option B comparison isolates modality.

**Exit check:** side-by-side results table; Spearman computed; divergence (if any) attributed
via tiebreaker.

---

## SCOPE & FRAMING (for the eventual brief — keep honest)

- This is an **out-of-domain (Hadoop), out-of-modality (traces), real-fault-labeled
  cross-check** of the PaaS conclusions — NOT a second PaaS benchmark.
- **No-change outcome** ⇒ strong: conclusions survived simultaneous shifts in data source,
  domain, modality, and ground-truth provenance.
- **Divergence outcome** ⇒ informative but must be attributed via the Phase 6 tiebreaker before
  drawing conclusions.
- State plainly that absolute scores are not comparable across datasets; only rankings and gaps
  are.

## SUGGESTED SESSION BOUNDARIES (one chat per line)

1. Phase 0 (scaffold + changelog).
2. Phase 1 (load + manifest).
3. Phase 2A–2B (tier map + case selection).
4. Phase 2C–2D (questions + ground truth).
5. Phase 3 (doc corpora + index).
6. Phase 4 Option A (flatten + leakage audit + SQL smoke).
7. Phase 5 (scoring + judge).
8. Phase 6 (run + compare). Option B and tiebreaker only if triggered.

Each session: open by re-reading CHANGELOG_PORT.md, confirm the Cleanliness Contract still
holds, do the one phase, append what changed, stop.
