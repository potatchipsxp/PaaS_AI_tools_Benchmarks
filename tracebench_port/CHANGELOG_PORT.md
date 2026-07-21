# CHANGELOG_PORT

Audit trail for the PaaS → TraceBench port. Every session: re-read this file first, confirm
the Cleanliness Contract still holds, do one phase, append below, stop.

## Cleanliness Contract (frozen unless noted "SANCTIONED CHANGE")

| Element | Status | Notes |
|---|---|---|
| Orchestrator system prompt (`SYSTEM_PROMPT` in `diagnostic_agent.py`) | FROZEN | Not one word changes |
| `query_logs` / `query_docs` tool **interfaces** (NL question in → text answer out) | FROZEN | Behind-the-tool implementation may change; signature may not |
| Model tiers, exact model IDs, backends | FROZEN | Same sweep as PaaS run |
| Temperatures (all = 0.0) | FROZEN | |
| LLM-judge rubric (root cause / evidence / fix, 0–2 each) | FROZEN | Reference answer changes; rubric does not |
| Retry / recovery / rate-limit logic | FROZEN | |
| Scoring philosophy (deterministic keyword/retrieval + LLM judge) | FROZEN | Specific keywords/signals change; structure does not |
| Ground-truth isolation (separate DB, never shown to agent) | FROZEN | |
| Everything except the data-access layer, identical between Track A and Track B | FROZEN | Same cases, questions, ground truth, docs, judge, models, temps. Only `query_logs`'s backend may differ between tracks. |
| `query_logs` SQL backend (what table it queries) | SANCTIONED CHANGE | Phase 4 only |
| Data source | SANCTIONED CHANGE | The whole point |
| Doc corpus content | SANCTIONED CHANGE | Phase 3 |
| Deterministic scoring keywords/signals | SANCTIONED CHANGE | Phase 5 |

---

## Session Log

### Phase 0 — Scaffolding (this session)

**What changed:**
- Created `tracebench_port/` as a sibling directory to the PaaS project root (same repo, same
  venv/environment — no new git repo, no new venv).
- Copied unmodified (byte-for-byte, no edits yet) from project root:
  - `diagnostic_agent.py`, `sql_agent.py`, `doc_agent.py`, `run_benchmark.py`,
    `build_doc_index.py`, `build_log_index.py`, `benchmark_incidents.py` (per spec Phase 0.2).
  - `generate_doc_corpus.py` (baseline for Phase 3 — spec references it as the JSONL record
    shape to match).
  - `Results/evaluate.py` (baseline for Phase 5 — spec calls `evaluate_trace.py` its "analog").
- Created empty `data/` and `Results/` directories to mirror the PaaS layout.
- Copied `data/.gitignore` (`*.txt`, `HDFS_v1/`) into `tracebench_port/data/.gitignore` so the
  TraceBench raw dump doesn't get committed.

**What did NOT change / was NOT copied:**
- No PaaS data artifacts copied in (no `benchmark_db.sqlite`, `eval_db.sqlite`,
  `doc_corpus.jsonl`, `chroma_db*/`) — these are PaaS-specific outputs; TraceBench gets its own,
  built fresh in Phases 1/3/4.
- No code inside any copied file was touched. Exit check per spec: "No code modified yet."

**Exit check:** directory exists, files copied, changelog seeded. ✅

### Phase 1 — Load + trace_set fix, and Phase 2A/2B/2C(partial)/2D — manifest + verified case selection (this session)

**What changed:**
- Pulled the real TraceBench raw dump (`github.com/mtracer/TraceBench`, 364 `.sql` files,
  3.3GB) into `data/raw_sql/mtracer-TraceBench-44b29e5/` (gitignored, regenerable).
- Fixed two bugs in `load_tracebench.py` found by actually running it to completion for the
  first time:
  1. An `ALTER TABLE Trace ADD COLUMN trace_set` approach broke every subsequent file's bare
     `INSERT INTO Trace VALUES (...)` (no column list, sized to the original 8-column schema),
     silently dropping ~370k Trace rows to 345. Replaced with a separate
     `Trace_Source(TraceID, trace_set)` side table that doesn't touch `Trace`'s schema.
  2. The dump's real PK column is `TaskID`, not `TraceID` — `clean_statement()`'s `\bTask\b`→
     `Trace` rename only touches the standalone table-name token. Also discovered real column
     names throughout: `Event.TaskID`/`Event.TID` (not `TraceID`/`NID`), `Operation.MaxDelay`/
     `MinDelay`/`AverageDelay` (not `*Latency`), `Trace.NumReports` (not `NumEvents`). **Flag
     for Phase 4:** the spec and any future `sql_agent.py` schema description need these real
     names, not the spec's assumed ones.
  3. `Operation`'s `PRIMARY KEY (OpName)` gets stripped by `clean_statement()`'s KEY-clause
     regex along with every other file's dump, so `Operation` accumulates ~19 rows per file
     (6,894 total) rather than one global baseline per OpName — it is NOT deduplicated across
     trace_sets. `select_cases.py` sidesteps this by computing its own latency baselines
     directly from `Event` durations rather than trusting `Operation`.
  - Final load: `Event` 14,777,715 / `Edge` 2,560,426 / `Trace` 370,334 / `Operation` 6,894 /
    364 distinct trace_sets, 0 untagged, 0 load errors.
- `build_manifest.py` (new): parses all 364 trace_set filenames into
  `data/tracebench_manifest.csv` (is_anomalous, class, fault_category, fault_name, workload,
  severity, delay_ms, tier, n_traces). Confirms the real naming convention is richer than the
  spec's toy example (`AN_{cat}_{fault}_{workload}_[{delay}ms_]{severity}FDN_{clients}C_...`,
  plus `COM_{Mul|Sin}_{cat}_{workload}_{run}_...` combination sets and `NM_{CL|DN}_{workload}_...`
  normal controls). All 364 filenames parsed and tier-mapped with zero unparsed/unmapped rows.
  `deadDN`/`panicDN` placed in **Tier 1** (spec left this conditional) after inspecting real
  `Event.Description` text for both: clean `SocketTimeoutException`/`NoRouteToHostException`
  against exactly `severity` target datanode(s), repeated across many independent clients — an
  unambiguous single/multi-node signature, not a propagating one.
- `select_cases.py` (new): stratified selection (one trace_set per distinct anomalous
  fault_name at the lower-middle of its own severity/delay_ms range, generalizing the spec's
  "prefer 2-3" from an assumed 1-5 scale to the real observed 1-50/ms scales; plus 7 normal
  controls) followed by a verification gate that only admits a case if real evidence is found
  for the *specific* TraceID chosen — never asserted from the fault name. Two evidence paths:
  `description_exception` (real Event.Description text) tried first, falling back to
  trace_set-level latency comparison (`host_latency_outlier` vs. peer datanodes in the same
  trace_set, or `cluster_latency_deviation` vs. a real normal-control baseline) for
  slowDN/slowHDFS, which inject delay without producing any exception text (confirmed by
  inspection). Rejected candidates are logged with a reason, not silently dropped or
  force-labeled.
  - Result: 27 cases admitted (5 Tier 1, 3 Tier 2, 12 Tier 3, 7 normal controls), 0 rejected.
- `.gitignore`: carved `tracebench_port/data/tracebench_manifest.csv` and
  `tracebench_port/data/ground_truth_trace.json` out of the blanket `tracebench_port/data/`
  ignore and committed them — they're the actual durable Phase 2 output (KB-sized) and need to
  survive container recycling between sessions, unlike the multi-GB raw dump/sqlite which stay
  regenerable/gitignored. (This was learned the hard way: a background load was lost mid-run
  when this session's container was reclaimed during an idle period, and had to be rerun.)

**What did NOT change:**
- Nothing outside `tracebench_port/` and this `.gitignore` carve-out. No agent files touched.

**Open items for future sessions:**
- Phase 2C (anti-leakage operator-style questions per case) not yet written — `select_cases.py`
  produces the ground-truth *labels*, not the benchmark question text or
  `benchmark_cases_trace.py`.
- Phase 4's schema description will need the real column names noted above, not the spec's
  assumed `TraceID`/`NID`/`*Latency` names.
- `eval_tracebench.sqlite` stays gitignored/regenerable (mechanical rebuild from
  `ground_truth_trace.json` + `tracebench_raw.sqlite`, both of which are either committed or
  regenerable) — only `select_cases.py` needs to be rerun against a freshly-loaded
  `tracebench_raw.sqlite` to reproduce it.

**Exit check (Phase 1 + 2A/2B/2D):** `tracebench_manifest.csv` and `ground_truth_trace.json`
committed; every retrieval/evidence signal in `ground_truth_trace.json` verified present in
actual Event/Trace data for its specific TraceID (spec's Phase 2 exit check). Phase 2C
(question authoring) remains open.

### Independent verification + Phase 2C + Operation table fix (this session)

**What changed:**
- `verify_data_integrity.py` (new): independently re-parses the raw `.sql` files (reusing only
  `split_sql_statements()`, never the load path under test) and diffs row counts, Trace fields,
  evidence-anchor text, and Edge structure against `tracebench_raw.sqlite` for a sample (or
  `--all`) of admitted cases. Sanity-tested against deliberately corrupted ground truth to
  confirm it actually fails rather than rubber-stamping. **183/183 checks pass across all 27
  cases.**
- Found and fixed a second provenance bug, same root cause as the `Trace_Source` one but a
  different symptom: `Operation`'s `PRIMARY KEY (OpName)` also gets stripped by
  `clean_statement()`, so its 6,894 rows were anonymous (~19 per file, no way to attribute a
  row to a trace_set — not row loss this time, but unusable for the Track B baseline role the
  addendum assigns it). Added `Operation_Source(op_rowid, trace_set)`, tagged by rowid range
  since `OpName` isn't unique per file (unlike `TraceID`). Verified independently: 15 raw
  `Operation` tuples in a sample file match 15 tagged rows.
- Found and fixed a ground-truth accuracy gap: 5 cases (killDN/deadDN/panicDN/disconnectDN/
  suspendDN) are observed from the client side, where the exception names the target datanode
  only as a bare IP, never by HostName — `affected_component` was recording the observing
  client instead. Added an IP→datanode `HostName` resolution map from `Event.HostAddress`.
  Re-ran the pipeline: same 27 cases, same trace_ids, only those 5 `affected_component` values
  changed.
- `benchmark_cases_trace.py` (new) — Phase 2C: one operator-style question per admitted case,
  written from each case's real `evidence_anchor` (never invented), same anti-leakage
  discipline as `benchmark_incidents.py`. **Finding:** `killDN`/`deadDN` share an identical
  `ConnectException` signature, and `panicDN`/`disconnectDN` share an identical
  `NoRouteToHostException` signature, in their admitted cases' evidence — Description text
  alone may not disambiguate these pairs. Directly relevant to the addendum's Track A
  (flat) vs. Track B (trace-native) comparison: if flattening can't tell these apart, that's
  evidence trace structure carries signal a log-only tool can't see.

**Exit check (Phase 2C):** `benchmark_cases_trace.py` holds all 27 questions; every question
verified to embed its own case's `trace_id` and to omit the case's `fault_name`.

### Phase 3 — Documentation corpus (this session, partial)

**What changed:**
- `generate_doc_corpus_trace.py` (new): mirrors `generate_doc_corpus.py`'s structure exactly
  (same JSONL record shape, same runbook/error_ref/config triple pattern). Produces:
  - `data/doc_corpus_trace_perfault.jsonl` — 64 docs: a triple per distinct fault_name (20
    faults) + 4 cross-cutting architecture docs (write pipeline, DataNode liveness/deadNodes,
    checksums & corruption signatures, latency baselines — the last citing the real NM
    baseline averages per OpName computed fresh from `tracebench_raw.sqlite`).
  - `data/doc_corpus_trace_category.jsonl` — 6 docs, one per fault_category, for the
    doc-leakage comparison spec 6.4 calls for.
  - Content discipline: mechanism from standard HDFS/Hadoop architecture; every quoted error
    string/latency figure copied verbatim from `ground_truth_trace.json`'s verified
    `evidence_anchor` for that fault — verified programmatically (no dangling `case_id`
    references, every `error_ref` quote matches its fault's real evidence exactly).
  - The docs for the killDN/deadDN and panicDN/disconnectDN pairs explicitly note their
    shared exception signatures and what the log text alone can and can't distinguish —
    honest about the ambiguity discovered in Phase 2C rather than glossing over it.
- Installed `chromadb`/`sentence-transformers` (already pinned in root `requirements.txt`, just
  not present in this fresh container).
- **Indexing is blocked, not done.** `build_doc_index.py` (unmodified, as spec 3.4 expects)
  needs to download the `all-MiniLM-L6-v2` embedding model from `huggingface.co`, which this
  environment's egress policy denies (confirmed via the proxy status endpoint: a `403` policy
  denial, not a transient failure — `recentRelayFailures` shows repeated `connect_rejected` for
  `huggingface.co:443`). Per the proxy's own guidance this is reported, not routed around.
  Cleaned up the partial/empty `tracebench_doc_chroma_db/` the failed attempt left behind.

**What did NOT happen yet:**
- Both corpora exist and are verified, but neither is indexed into ChromaDB.
- No retrieval smoke test has run (depends on indexing).

**Exit check (Phase 3): NOT MET.** Corpora built and content-verified; indexing and smoke
retrieval blocked on network access to `huggingface.co` (or a pre-cached embedding model) in
whatever environment continues this — check that first before resuming this phase.
