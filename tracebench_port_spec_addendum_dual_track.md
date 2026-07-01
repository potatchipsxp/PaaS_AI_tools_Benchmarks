# Addendum: Dual-Track (Option A + Option B) — Planned Comparison

**Read this alongside `tracebench_port_spec.md`.** This addendum supersedes the parts of the
original spec that treated Option B (trace-native) as a conditional follow-on. Option B is now
a **planned, first-class track**, run and compared against Option A regardless of Option A's
results. Phase numbering from the original spec is preserved; this addendum only changes
Phase 4, Phase 5's deterministic keys, and Phase 6, and adds one item to the Cleanliness
Contract.

---

## Why both, and what changes structurally

Committing to both up front means **A-vs-B is a designed comparison, not a debugging
reaction.** The consequence: A and B must be built to be comparable from the start. This
implies a specific structure —

**One shared spine, one forked layer, one three-way comparison.**

- **Shared across A and B (build once):** Phases 0, 1, 2, 3, and the ground truth + LLM judge
  in Phase 5. Same cases, same questions, same ground truth, same docs, same judge, same
  models, same everything — because if any of these differ between A and B, the A-vs-B result
  is confounded by that difference instead of by modality (flat logs vs trace structure).
- **Forked (build twice):** only the Phase 4 data-access layer and the Phase 5 *deterministic*
  signal that keys off it.
- **Final comparison (Phase 6):** three-way — PaaS ranking vs Trace-Flat ranking vs
  Trace-Native ranking.

### New Cleanliness Contract entry (add to the table in the original spec)

| Element | Status | Notes |
|---|---|---|
| Everything except the data-access layer, identical between Track A and Track B | **FROZEN** | Same cases, questions, ground truth, docs, judge, models, temps. The ONLY difference between A and B is how `query_logs` reaches the data. If anything else differs, the A-vs-B comparison is confounded. |

**The single controlled variable between A and B is modality.** A sees flat rows; B sees trace
structure. Nothing else may differ. Guard this the way the project guards every other
single-variable comparison.

---

## Revised PHASE 4 — Build BOTH data-access layers

**Precondition:** Phase 3 complete. **Build A first, then B, then verify parity of everything
around them.**

Both tracks read from the SAME underlying data in `data/benchmark_trace_db.sqlite`, which after
this phase contains BOTH representations:
- the flattened `logs` table (for Track A), and
- the original four trace tables Event/Edge/Trace/Operation (for Track B).

Keeping both in one DB file guarantees the two tracks diagnose the exact same incidents from
the exact same source rows — the flattening is just a view of the same events B sees
structurally.

### 4-A. Track A — Flatten into the existing `logs` schema

(This is the original spec's Option A, unchanged. Summarized here for completeness.)

4A.1  `build_trace_logs.py` projects `Event` rows into the existing flat `logs` schema
      (mapping table in the original spec, Phase 4 Option A). Drops the `Edge` structure.
4A.2  Write the flat `logs` table into `benchmark_trace_db.sqlite`.
4A.3  **Leakage audit** — no agent-visible column may contain the fault name, category, or
      trace-set name. `SELECT DISTINCT message FROM logs` and grep for fault names. (Original
      spec 4A.3 — unchanged, still critical.)
4A.4  Track A agent config: `sql_agent.py` with `DB_URI` → `benchmark_trace_db.sqlite`,
      `INCLUDE_TABLES=["logs"]`, existing `DEFAULT_SCHEMA_DESCRIPTION` unchanged. **Only DB_URI
      and INCLUDE_TABLES differ from the PaaS config.** Record in CHANGELOG_PORT.
4A.5  SQL-agent standalone smoke test against `logs`.

### 4-B. Track B — Trace-native access to the same data

Design rule: **change the data the agent can see, NOT the orchestrator, NOT the tool
interface.** The `query_logs` tool still takes an NL question and returns text. What changes is
the schema the SQL sub-agent is given and the tables it may touch.

4B.1  Load the four trace tables (Event, Edge, Trace, Operation) into the SAME
      `benchmark_trace_db.sqlite` alongside the flat `logs` table (from Phase 1's raw load;
      just don't drop them). Confirm they carry the same TraceIDs the flattened rows came from,
      so A and B are provably diagnosing identical incidents.

4B.2  Provide a trace-native schema description. Create a Track-B variant of the SQL agent's
      `schema_description` (passed as the `schema_description` parameter to `build_agent()` —
      the function already accepts this, so **no edit to `sql_agent.py` internals is needed**).
      The description documents the four tables, their key fields, and the join path to
      reconstruct a trace:
      - Event(TraceID, NID, OpName, StartTime, EndTime, HostName, Agent, Description)
      - Edge(TraceID, FatherNID, FatherStartTime, ChildNID)
      - Trace(TraceID, Title, NumEvents, NumEdges, StartTime, EndTime)
      - Operation(OpName, Num, MaxLatency, MinLatency, AverageLatency)  ← latency baselines
      - Reconstruction hint (from TraceBench docs): within a TraceID, an event F1 is ancestor
        of F2 when F1.StartTime < F2.StartTime AND F1.EndTime > F2.EndTime; Edge links classes
        via FatherNID/FatherStartTime → ChildNID.
      Include the same anti-hallucination rules and row-limit rules the PaaS schema description
      has. This keeps prompt STYLE constant; only the schema CONTENT differs.

4B.3  Choose the trace-access mechanism. Two sub-options — **pick 4B.3a as primary**:
      - **4B.3a (recommended): multi-table SQL.** Set `INCLUDE_TABLES=["Event","Edge","Trace",
        "Operation"]` and let the SQL agent write joins over the four tables. This is the
        smallest possible change that still exposes structure — same tool
        (`QuerySQLDatabaseTool`), same agent, just more tables and a richer schema description.
        The agent can compare an event's latency to `Operation.AverageLatency`, walk Edge to
        find children, and detect structural anomalies via joins. **Strongly preferred because
        it changes ONLY the data surface, holding the tool constant — keeping the A-vs-B
        variable clean.**
      - **4B.3b (only if 4B.3a proves too hard for the models to use): add a `query_trace`
        helper tool** that, given a TraceID, reconstructs and returns the trace tree as text.
        NOTE: this adds a tool, which introduces a second difference between A and B (tool set,
        not just data). If you use 4B.3b, document explicitly that A-vs-B now varies by tool AND
        modality, weakening the clean attribution. Prefer 4B.3a.

4B.4  **Leakage audit for Track B** — same discipline as 4A.3, applied to all four tables.
      In particular, ensure `Event.Description`, `OpName`, `Agent`, `Trace.Title`, and any
      table's contents do NOT contain the injected fault name or the trace-set name. The fault
      must be inferable from latency/structure/exception patterns, never stated. Grep each
      table's free-text fields. This is easy to miss because B exposes more columns than A.

4B.5  Track B agent config: same `build_agent()` call as Track A EXCEPT
      `include_tables=["Event","Edge","Trace","Operation"]` and
      `schema_description=<track B description>`. Everything else — model, backend, temp,
      max_iter, max_rows, recovery logic — identical to Track A. Record in CHANGELOG_PORT the
      exact two-parameter difference.

4B.6  SQL-agent standalone smoke test in trace-native mode: verify the agent can (a) find a
      high-latency event by joining to Operation baselines, (b) walk Edge to find an event's
      children, (c) surface an exception from Description. If it can't do these in smoke
      testing, the models can't use the schema and you should reconsider 4B.3b.

### 4-C. Parity verification (do this before any benchmark run)

4C.1  Confirm A and B point at the same `benchmark_trace_db.sqlite` and the same TraceIDs.
4C.2  Confirm the orchestrator (`diagnostic_agent.py`), its `SYSTEM_PROMPT`, the doc agent, the
      judge, the model configs, and temperatures are byte-identical between the two tracks.
      Diff the two agent-config setups and paste the diff into CHANGELOG_PORT — it should show
      ONLY `include_tables` and `schema_description` differing.
4C.3  Confirm both tracks pass their leakage audits.

**Exit check:** `benchmark_trace_db.sqlite` holds both the flat `logs` table and the four trace
tables over identical TraceIDs; Track A config differs from Track B config by exactly two
parameters; both leakage-audited; both smoke-pass. Diff recorded.

---

## Revised PHASE 5 — Scoring: shared judge, forked deterministic key

**Precondition:** Phase 4 complete.

5.1  **Ground truth: shared, built once** (original spec 2D/5). Both tracks score against the
     same `ground_truth_trace.json` and the same isolated `eval_tracebench.sqlite`. Do NOT
     build separate ground truth per track.

5.2  **LLM judge: shared, identical** (original spec 5.2). Same frozen rubric, same reference
     answers, run identically over both tracks' outputs. The judge does not know or care which
     track produced the diagnosis. This is the primary cross-track scoring instrument BECAUSE
     it is modality-agnostic — it scores the *diagnosis text*, which is directly comparable
     between A and B.

5.3  **Deterministic layer: mostly shared, one forked signal.**
     - `answer_required` (fault name + affected node in the diagnosis) — SHARED, keys off the
       diagnosis text, identical for A and B.
     - Localization sub-score (did it name the right datanode) — SHARED.
     - `retrieval_signals` (did the doc agent surface the right doc) — SHARED (doc agent is
       identical across tracks).
     - **Forked signal — evidence-grounding provenance:** the check for "did the SQL agent
       actually retrieve the supporting evidence" must key off different query shapes. Track A:
       evidence came from `logs` rows. Track B: evidence came from Event/Edge/Operation joins.
       Keep this as a small per-track adapter; keep everything else shared. Mark it clearly in
       code as the ONE track-specific scorer.

5.4  Keep deterministic vs judge vs localization SEPARATE in output (original spec 5.4), and
     additionally tag every result row with its track (`A_flat` / `B_native`) and dataset
     (`tracebench`) so the three-way comparison in Phase 6 can slice cleanly.

**Exit check:** `evaluate_trace.py` scores both tracks against shared ground truth + shared
judge, emits per-tier deterministic/judge/localization scores tagged by track.

---

## Revised PHASE 6 — Three-way comparison

**Precondition:** Phases 0–5 complete for both tracks.

6.1  Run the SAME model tier sweep three times' worth of outputs total:
     - PaaS benchmark (already have, or rerun for freshness).
     - TraceBench Track A (flat).
     - TraceBench Track B (trace-native).
     Output filenames encode model combo + dataset + track, e.g.
     `...__data-tracebench__track-Aflat.json`, `...__track-Bnative.json`. Never overwrite.

6.2  **Three rankings, three pairwise Spearman correlations:**
     - PaaS  vs  Trace-Flat   → does the conclusion survive domain shift, holding modality
       roughly flat (both are log-shaped)?
     - PaaS  vs  Trace-Native → does it survive domain AND modality shift together?
     - Trace-Flat vs Trace-Native → **the clean modality test.** Same domain, same data, same
       everything except flat-vs-structured access. This isolates exactly what flattening
       costs, because it is the only variable.

6.3  **Interpretation grid:**

     | PaaS↔Flat | PaaS↔Native | Flat↔Native | Reading |
     |---|---|---|---|
     | agree | agree | agree | Maximally robust. Conclusions hold across domain, modality, and access shape. Strongest possible claim. |
     | agree | agree | agree, but Native scores higher overall | Rankings robust; trace structure adds diagnostic power (informs the real product: add a trace channel). |
     | agree | disagree | disagree | Modality is the fragility axis, not domain. Flattening changed the conclusion. Dig into which tier/faults moved. |
     | disagree | disagree | agree | Domain is the fragility axis, not modality. HDFS-vs-PaaS matters more than data shape. |
     | disagree | disagree | disagree | Multiple axes matter; conclusions are context-specific. Report as a scoping limitation, not a robustness win. |

6.4  **Per-tier, per-fault breakdown of Flat↔Native.** This is where the flattening-loss
     hypothesis gets tested directly. Expect: Tier 1 (killDN/suspendDN — row-local exception
     signal) ≈ equal A and B. Tier 3 (corrupt/cut/combination — structural/latency signal)
     is where Native may pull ahead if flattening blinded A. If that pattern appears, you have
     direct evidence that a log-only tool is structurally blind to a class of cross-component
     faults — a concrete finding for the real PaaS tool's design.

6.5  **Confound note retained:** PaaS↔Native still varies domain and modality together; the
     Flat↔Native column is what disambiguates. Lead interpretation with the Flat↔Native result
     when explaining any PaaS↔Native divergence.

**Exit check:** three result sets; three Spearman values; interpretation grid filled;
per-tier Flat↔Native table produced.

---

## Updated session boundaries (one chat per line)

1. Phase 0 (scaffold + changelog, incl. new contract row).
2. Phase 1 (load raw — keep ALL four tables, do not drop Edge yet).
3. Phase 2A–2B (tier map + case selection).
4. Phase 2C–2D (questions + shared ground truth).
5. Phase 3 (shared doc corpora + index).
6. Phase 4-A (flatten + leakage audit + smoke).
7. Phase 4-B (trace-native schema + tables + leakage audit + smoke) + 4-C (parity verify).
8. Phase 5 (shared judge + forked deterministic key).
9. Phase 6 (run all three + three-way compare).

Each session: reopen CHANGELOG_PORT.md, confirm the Cleanliness Contract (including the new
A-vs-B parity row) still holds, do the one phase, append what changed, stop.

---

## The two things most likely to silently break the A-vs-B comparison

1. **Accidental divergence between A and B in something other than the data layer.** If a
   model ID, temperature, prompt, or the judge drifts between tracks, Flat↔Native stops being a
   clean modality test. Phase 4-C's recorded diff is the guard — it must show only two
   parameters differing.
2. **Track B leakage through the extra columns.** B exposes Event/Edge/Trace/Operation, far
   more free text than A's single `logs` table. The fault name or trace-set name hiding in
   `Trace.Title` or `Event.Agent` would hand B the answer and make it look spuriously better
   than A. 4B.4 is the guard; do not skip it because A already passed its own audit — they are
   different column sets.
