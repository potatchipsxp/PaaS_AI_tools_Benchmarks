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
| Ground truth, the 27 cases/questions, doc corpus, LLM judge rubric — shared across Track A **and** Track B | FROZEN | Superseded the addendum's stricter "everything except data-access, identical between A and B" row below — see the Track B redesign session. This narrower set is what must still match exactly for the two tracks' judge scores to be comparable at all. |
| ~~Everything except the data-access layer, identical between Track A and Track B~~ | **SUPERSEDED** | The addendum's original framing (A and B differ ONLY in `include_tables`/`schema_description`, same tool interface) was deliberately abandoned — see the Track B redesign session below. Track B now uses a different tool (`query_trace`, deterministic, no SQL sub-agent model) by design, trading a clean single-variable A-vs-B comparison for three independently-more-reliable tracks (PaaS / Track A / Track B) triangulated together. Phase 6's interpretation grid will need rewriting to match. |
| `query_logs` SQL backend (what table it queries) | SANCTIONED CHANGE | Phase 4, Track A only |
| Track B's tool interface (`query_trace`, not `query_logs`) | SANCTIONED CHANGE | The one exception to the tool-interface-frozen rule, scoped specifically to Track B — see redesign session |
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

### Phase 3 — Documentation corpus (completed this session)

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

**Indexing (this session, local — no egress block here):**
- Deps already present locally at the pinned versions (`chromadb==1.5.0`,
  `sentence-transformers==5.2.2`); no install needed.
- Found and fixed one real bug in `build_doc_index.py` before it would index anything: it still
  read `doc["incident_ids"]` (the PaaS corpus's field name), but
  `generate_doc_corpus_trace.py`'s records use `case_ids` per spec 3.2 ("incident_ids →
  case_ids"). This raised `KeyError: 'incident_ids'` on the first indexing attempt. Renamed
  consistently throughout `build_doc_index.py` — the metadata key, `parse_incident_ids` →
  `parse_case_ids`, and `retrieve_docs()`'s returned dict key all now say `case_ids`. This is
  the one sanctioned tweak to an index script the task anticipated; no other line changed.
  Downstream note for Phase 5: `evaluate_trace.py` will need `case_ids` (not `incident_ids`)
  when it adapts `Results/evaluate.py`'s retrieval-scoring logic.
- Indexed both corpora into `tracebench_doc_chroma_db/` (gitignored — regenerable from the
  committed JSONL corpora, same as `doc_chroma_db/` for PaaS): `docs_trace_perfault` → 64 docs,
  `docs_trace_category` → 6 docs. Matches expected counts exactly.

**Smoke-test retrieval (3 real questions from `benchmark_cases_trace.py`, both collections):**
- **TB-018 (slowDN, Tier 2):** in `docs_trace_perfault`, `runbook_slowDN` is rank 2 (dist
  0.566) and `arch_latency_baselines` (case_ids includes TB-018) rank 3; `error_ref_slowDN` and
  `config_slowDN` land at rank 8 (0.737) and rank 10 (0.820) — inside the top 10 of 64 docs but
  not the top 5. Rank 1 was `runbook_readOnlyDN`, a different but genuinely similar fault
  (also a latency-degradation signature). In `docs_trace_category`, `category_Net` (the correct
  category) is rank 2 behind `category_Sys` — expected, this corpus is deliberately coarse.
  **Partial pass**: the right fault surfaces near the top, but not as cleanly as the original
  "near the top" expectation implied for all three doc types simultaneously.
- **TB-021 (normal control, Tier 0):** in `docs_trace_perfault`, nothing scored confidently —
  best distance ~0.81, versus ~0.54–0.60 for the true-fault matches on TB-018/TB-011. No doc
  claims relevance and the top results are a scattergun of unrelated faults. This is the
  expected behavior for a no-fault case and is **not a bug**.
- **TB-011 (deadDN, Tier 1, added as a third check beyond the two requested):** a real
  retrieval-quality gap. `error_ref_deadDN` (which contains the exact "Connection refused"
  string from the question) is only rank 10; `runbook_deadDN` and `config_deadDN` don't appear
  in the top 20 of 64 docs at all. Rank 1 is `config_readOnlyDN` — topically unrelated (that
  fault is about slow writes, not connection refusal). Inspecting content directly: the
  `config_*` docs are short, structurally-uniform markdown tables (parameter/default/relevance)
  that carry little distinguishing text per fault, so the bi-encoder embeds them into a
  generic cluster that sometimes lands deceptively close to unrelated queries — a known
  weakness of embedding very short/templated documents, not a code defect.

**Assessment:** the pipeline works correctly end-to-end (bug fixed, counts match, no-fault
case correctly shows low confidence). Retrieval precision is uneven across fault types — clean
for some (TB-018, directionally), poor for others (TB-011). **Flagging for Phase 5** rather
than fixing now (would mean rewriting corpus content without a specified quality bar): the
deterministic retrieval-signal design should probably score against the runbook+error_ref+config
triple as a group within a wider n_results (e.g. 10–15), not literal top-5 rank, and/or note that
`config_*` docs' low information density may warrant richer content later if this pattern
recurs across more cases.

**Exit check (Phase 3): MET.** Both corpora indexed at expected counts; smoke-tested against
real benchmark questions; one real code bug found and fixed; retrieval-quality observations
recorded for Phase 5 rather than papered over.

### Phase 4 — Dual-track query_logs (Track A flatten + Track B trace-native), completed this session

**Precondition work:** `tracebench_raw.sqlite` and `data/raw_sql/` are gitignored/regenerable and
weren't present in this session's container. Re-cloned `github.com/mtracer/TraceBench` (public,
plain `git clone` — no auth needed) directly into `data/raw_sql/mtracer-TraceBench-44b29e5/` (the
exact path `load_tracebench.py` expects) and reran it unmodified. Final counts matched the
earlier verified run exactly: Event 14,777,715 / Edge 2,560,426 / Trace 370,334 / Operation 6,894
/ 364 trace_sets, 0 untagged, 0 load errors — confirms the loader is deterministic and the counts
aren't a fluke of the first run.

**Schema facts verified directly against the loaded DB (not assumed from the spec) before writing
any flattening code:**
- `Event` columns: `TaskID, TID, OpName, StartTime, EndTime, HostAddress, HostName, Agent,
  Description`. `Agent` (assumed by the spec) does exist for real. `Description` is populated on
  100% of rows (0 NULL) — ~92.5% are `"Success: ..."` text, ~4.9% contain `"Exception"` — it is
  NOT an error-only field, so ERROR-level detection must look for the substring, not non-nullness.
- **`TID` is a thread identifier, not a unique per-event id** — the same `TID` repeats across
  dozens of events on one client thread (confirmed: a single `TID` matched 4× `RPC:getFileInfo` +
  many repeated `chooseDataNode`/`bestNode`/`newBlockReader` calls, i.e. one thread's retry loop
  across multiple blocks). `Edge.FatherNID`/`ChildNID` reference this same `TID`, disambiguated
  for the father row only by the accompanying `FatherStartTime` (no such disambiguator exists for
  the child row — noted as an honest gap in the Track B schema description, not glossed over).
- **`Event.StartTime`/`EndTime` are per-host, nanoTime-style relative ticks — NOT a shared
  wall-clock epoch.** Verified directly: for one sampled `TaskID`, events on `datanode036` had
  `StartTime` ≈ 4.07e14 while `client024`'s events on the *same TaskID* had `StartTime` ≈ 4.11e15
  — a ~1.8-hour gap if these were a shared clock, which is impossible for what should be a
  millisecond-scale RPC. Only same-row durations (`EndTime - StartTime`) and same-host ordering
  are meaningful; cross-host absolute comparison is not. This directly affects both tracks:
  - Track A's `logs.timestamp` is anchored per (`TaskID`, `HostName`) to `Trace.FirstSeen` (a real
    wall-clock date the tracing backend recorded) plus that host's own relative offset — a
    plausible, correctly-ordered-within-host timestamp, not a fabricated absolute cross-host one.
  - Track B's schema description explicitly warns the agent never to subtract or compare
    `StartTime`/`EndTime` across two different `HostName` values.
  - The spec's ancestor-reconstruction hint (`F1.StartTime < F2.StartTime AND F1.EndTime >
    F2.EndTime` implies containment) is therefore only trustworthy same-host; Track B's schema
    description points the agent at the `Edge` join instead of that heuristic for cross-host
    relationships.
- **`Trace.NumReports`/`NumEdges` are unreliable** — spot-checked several `TaskID`s with
  `NumEdges > 0` and found **zero** matching `Edge` rows for any of them.
  **⚠ CORRECTED in a later session (see "Edge data-loss bug found, fixed, and reloaded" below):**
  this was attributed at the time to "a real data-quality gap in the dump itself: only ~59% of
  `TaskID`s have any `Edge` row at all despite `NumEdges` often claiming otherwise" — that
  attribution was wrong. It was a loader bug (a `split_sql_statements()` quote-parsing heuristic
  silently dropping the first `Edge` INSERT statement in every one of the 364 files). After the
  fix, 0.00% of traces have zero `Edge` rows. The join-key finding immediately below is still
  accurate and was not affected: 99.9995% of the `Edge` rows that *did* load matched a real
  `TaskID`, and `FatherNID`/`ChildNID` correctly resolved to real `Event` rows with sensible
  cross-host parent/child relationships (e.g. an RPC client's `newBlockReader` as father of a
  DataNode's `verifiedByClient`) — the rows that loaded were always trustworthy; there just used
  to be far fewer of them than there should have been. Both schema descriptions still correctly
  tell the agent not to trust the count columns and to `COUNT(*)` the real rows instead — that
  guidance holds regardless of this correction.
- One anomalous `Trace.Title` value (`'AA15130628A163E0'`, 1 row out of 26,943) turned out to
  equal its own `TaskID` — an isolated dump/parsing artifact, confirmed leak-free (doesn't contain
  any fault/category/trace_set token) and too small a fraction to chase further.

**Scope decision (a deliberate deviation from the spec's literal "keep the four tables" wording,
flagged per the spec's own "override now if you disagree" convention):** both tracks are scoped
to the 27 selected cases' **entire trace_sets** (26,943 traces, not just the 27 named TraceIDs),
not the full 370k-trace corpus. This is necessary, not just an optimization: `select_cases.py`'s
own fault-verification logic (`trace_set_op_host_avgs` / `find_host_latency_outlier`) compares a
datanode's average latency against its *peers across the whole trace_set* — that evidence doesn't
exist if only the one named TraceID is flattened. It's also what keeps the DB tractable (1.36M
Event rows instead of 14.7M) and the leakage-audit surface checkable.

**Track A (`build_trace_logs.py`, new) — flatten to the existing `logs` schema:**
- Column mapping: `component`=`OpName`, `subcomponent`=`Agent`, `node_id`=`HostName`,
  `instance_id`=`TaskID`, `thread_id`=`TID`, `message`=`Description` (verbatim — this is where the
  real exception strings and `"Success: ..."` text live).
- `event_type` bucketed into `read`/`write`/`rpc`/`client_command`/`error`/`other` from the full,
  verified list of 71 distinct `OpName` values (not guessed) — no `heartbeat` bucket exists in
  this dataset, unlike the spec's assumed PaaS-style categories, since TraceBench traces
  request/RPC activity, not periodic heartbeats.
- `level`: `ERROR` if `Description` contains `"Exception"`; `WARN` if the event's (`HostName`,
  `OpName`) or `OpName` (cluster-wide) was flagged as a peer-latency outlier for that trace_set —
  reusing `select_cases.py`'s own `build_nm_latency_baseline` / `find_host_latency_outlier` /
  `find_cluster_wide_deviation` functions directly (same `HOST_OUTLIER_RATIO`/
  `CLUSTER_DEVIATION_RATIO`/`MIN_SAMPLES` constants) rather than inventing a second, possibly
  inconsistent heuristic; else `INFO`.
- `source_file`: deliberately **NULL, not the trace_set filename** — the spec's own mapping table
  flags this exact column as a leakage risk, so it's left out entirely rather than smuggled in.
- Result: 1,364,907 rows flattened, 26,941 distinct `instance_id` (2 short of the 26,943 traces in
  scope — negligible, some TaskIDs apparently have zero Event rows; not chased further). Level
  distribution: 8,200 ERROR / 31,285 WARN / 1,325,422 INFO.
- **Leakage audit (`audit_leakage_trace.py`, new)**: greps every distinct value in every
  agent-visible text column against all fault names/categories/trace_set filenames (750 tokens,
  ≥5 chars to avoid noise from short common substrings) — **PASS, 0 hits.**

**Track B (`build_trace_native.py`, new) — trace-native, same underlying data:**
- `Event`/`Edge`/`Trace` copied as-is (real columns), scoped to the same 27 trace_sets.
- `Operation` is **not** copied as-is: the raw table has ~19 unattributed, non-deduplicated rows
  per `OpName` per source file (same issue Phase 2's CHANGELOG already flagged), so this
  aggregates one row per `OpName` (`SUM(Num)`, `MAX(MaxDelay)`, `MIN(MinDelay)`, `Num`-weighted
  mean `AverageDelay`) from only the `Operation_Source` rows tagged to our 27 scope trace_sets —
  62 distinct `OpName` rows, self-consistent with what's actually visible in this DB rather than
  silently borrowing baseline stats from out-of-scope trace_sets the agent can't see.
- `Trace_Source`/`Operation_Source` (the trace_set-provenance side tables) are never copied into
  this DB at all — those are exactly what would hand the agent the fault name directly.
- `trace_sql_schemas.py` (new): `TRACK_A_SCHEMA_DESCRIPTION` / `TRACK_B_SCHEMA_DESCRIPTION`
  strings, written in the same style/rules as `sql_agent.py`'s PaaS description, documenting the
  real column names and every caveat above (cross-host time non-comparability, unreliable count
  columns, `TID` ambiguity). Meant to be pasted into `SQL_SCHEMA_DESCRIPTION`.
- **Leakage audit, run separately from Track A** per the addendum's explicit warning (Track B
  exposes far more free text — `Trace.Title`, `Event.Agent`, etc.) — **PASS, 0 hits**, including
  the anomalous `Trace.Title` row above.

**Orchestrator plumbing (`diagnostic_agent.py`, modified — the one necessary touch to this file):**
`build_tools()` and `build_diagnostic_agent()` previously hardcoded `sql_agent.py`'s own
`INCLUDE_TABLES`/`DEFAULT_SCHEMA_DESCRIPTION` (only `db_uri` was ever overridable). Added
`sql_include_tables`/`sql_schema_description` parameters to both functions, threaded through to
`build_sql_agent(...)`, plus `SQL_INCLUDE_TABLES`/`SQL_SCHEMA_DESCRIPTION` CONFIG constants that
default to `sql_agent.py`'s own PaaS values (imported, not duplicated, so there's one source of
truth) — verified via import that behavior is byte-identical to before unless overridden. Added
commented example blocks showing exactly what to uncomment for each track. This does **not**
touch `SYSTEM_PROMPT` or the `query_logs` tool's signature — same category of change as the
model/backend/temp parameters this file already exposed. Track A vs Track B now differ by
**exactly two parameters**, satisfying addendum 4C.2's parity-diff requirement.

**Smoke test (no LLM backend/API key available in this session, so this validates what a live
test would actually be checking — that the schema and data support the diagnosis, independent of
whether a given model is smart enough to write the SQL):**
- `SQLDatabase.from_uri()` introspects both configs without error.
- TB-018 (slowDN, target `datanode046`): filtering `logs` to just the *named* instance already
  shows 2 WARN rows on `datanode046` out of its 8 read events — the operator doesn't even need to
  reason cluster-wide to spot it. Track B: `Event JOIN Operation` on `TaskID` executes and returns
  real durations against the real baseline (a hand-written query using raw duration DESC isn't
  itself the right diagnostic shape — that needs the same per-op averaging `select_cases.py`
  uses — but this was only checking the join mechanics work, which they do).
- TB-011 (deadDN): filtering to the named instance surfaces the exact real
  `"IOException: java.net.ConnectException: Connection refused..."` string.
- TB-021 (normal control): 100% INFO, 0 WARN/ERROR, for the named instance — clean negative
  control.

**Exit check (Phase 4): MET.** `benchmark_trace_db.sqlite` holds both the flat `logs` table
(Track A) and the four native tables (Track B) over identical scope; both leakage-audited
separately with 0 hits; both schema-level smoke-tested against real questions; Track A vs Track B
config diff is exactly `include_tables` + `schema_description`, recorded above.

### Track B redesign — deterministic query_trace, own subfolder (this session)

**Why:** discussed and agreed with the user that the addendum's original Track B design (SQL
sub-agent over native tables, differing from Track A only in `include_tables`/
`schema_description`) optimized for the wrong thing. The actual research question is *model
power and how it generalizes*, not narrowly "does one fixed SQL-only tool transfer across data
shapes." Reframed as three complementary tests instead of one clean A-vs-B pair:
- **PaaS** (done): native data, native tool. Highest face validity, weakest generalization signal
  alone (single domain).
- **Track A**: TraceBench forced into the PaaS tool/schema shape. Lower face validity (data
  contorted to fit), but highest apples-to-apples reliability — literally nothing but the DB
  changed.
- **Track B** (redesigned here): TraceBench read the way it actually is, with tooling adapted to
  fit the data rather than the other way around. Lower face validity for the PaaS use case
  (different domain *and* different apparatus), but no risk that our own flattening/schema-forcing
  introduced artifacts.

If the model-tier ranking holds across all three, that's a stronger claim than any single clean
pairwise comparison could give — trading the addendum's "isolate exactly one variable" guarantee
for "three independently-trustworthy tests, triangulated." This is a deliberate, discussed
departure from the addendum, not a discovery of a flaw in it — recorded in the Cleanliness
Contract above (the old "identical except data-access layer" row is marked SUPERSEDED, not
deleted, so the reasoning trail stays intact). Phase 6's interpretation grid (which read
Flat↔Native as "the clean modality test") will need rewriting to match — not done yet, flagged for
whoever picks up Phase 6.

**Architecture decided (discussed, not assumed):**
- `query_trace(trace_id)` replaces `query_logs` for Track B. Deterministic Python, not an LLM SQL
  sub-agent — removes a confound the addendum's design had baked in (a weak SQL-sub-agent model
  botching a join would have looked identical to "the orchestrator reasoned poorly"). Consequence:
  there is no `SQL_MODEL`/`SQL_BACKEND` for Track B at all — only the orchestrator and doc-agent
  models are in play, which changes what a "tier" means for this track vs. Track A/PaaS (a tier
  there is diag+sql+doc together; here it's diag+doc). Recorded plainly rather than glossed over.
- New subfolder `track_b/`, mirroring the Phase-0 convention of a sibling directory with its own
  layout — but **not** its own `data/` copy of ground truth/docs/cases: those stay shared at
  `tracebench_port/data/` (referenced via `../data/...`) specifically because the addendum's
  concern about the two tracks silently drifting apart is still valid for THESE elements even
  though the tool interface itself is now allowed to differ — see the updated Cleanliness Contract
  row above.
- `track_b/data/` holds only what's genuinely Track-B-specific: a scoped copy of the RAW tables
  (see below) and `Results/` for Track B's own run output.

**Data (`build_track_b_data.py`, new) — raw, not the Phase-4 SQL tables:**
- Discussed directly with the user: Track B's data should be *closer to the raw TraceBench tables*
  than Phase 4's `build_trace_native.py` output, which was curated specifically for an LLM writing
  SQL against it (deduplicated `Operation`, no `Trace_Source`/`Operation_Source` at all). Since
  `query_trace` is deterministic Python with no SQL access exposed to any model, that curation is
  unnecessary — the leakage boundary only has to be enforced on the tool's *output text*, not on
  what columns technically exist in the backing file.
- Chose to **materialize a scoped copy** (`track_b/data/tracebench_raw_scoped.sqlite`) rather than
  query the 2.9GB `tracebench_raw.sqlite` live at run time — discussed as a deliberate tradeoff:
  a small one-time filtering cost now vs. a runtime dependency on the giant raw file (which would
  also need new indexes to stay fast, same slowness Phase 4 already hit). Keeps `track_b/`
  self-contained, matching why the subfolder was created in the first place.
- Copies `Event`/`Edge`/`Trace`/`Trace_Source`/`Operation`/`Operation_Source` **raw** — real column
  types preserved (`StartTime`/`EndTime` etc. stay `INTEGER`, not coerced to `TEXT`), no
  deduplication, no relabeling — scoped to the same 27 cases' trace_sets Track A uses. Row counts
  match Track A's flattening exactly (1,364,907 Event rows, 26,941 distinct `TaskID`), confirming
  both tracks are working from identical underlying data.
- `Trace_Source`/`Operation_Source` **are** included here (unlike Track A's/the old Track B's
  agent-facing tables) — they carry the `trace_set` (= fault name) mapping `query_trace.py` needs
  *internally* to group peer events for latency-outlier comparison. Safe specifically because no
  model ever gets direct SQL access to this file; the leakage boundary is `query_trace`'s output
  text, audited separately (see below).
- **Parity fix caught before it became a bug**: Track A's WARN-derivation used
  `build_nm_latency_baseline()` against the FULL, unscoped raw corpus (all 364 trace_sets). If
  `query_trace` had computed that same baseline from only the 7 scoped NM trace_sets, its
  cluster-deviation signal would have been measurably noisier than Track A's purely due to an
  architecture choice, not a real track difference. Fixed by precomputing the baseline once from
  the full corpus (matching Track A exactly) and caching it to `data/nm_latency_baseline.json`, so
  `query_trace` never needs to touch the 2.9GB raw file at query time.
- Leakage audit on the raw-copied columns Event/Trace/Operation that could end up in output text
  (not `Trace_Source`/`Operation_Source`, which intentionally hold the trace_set mapping for
  internal use only) — **PASS, 0 hits**.

**`query_trace.py` (new) — the tool itself:**
- Reconstructs a per-host timeline (grouped by host because cross-host `StartTime` isn't
  comparable — see Phase 4's verified finding) plus a best-effort cross-host call-structure section
  from `Edge`. Times shown are relative to each host's own first event in the trace, with an
  explicit note that cross-host times aren't comparable — chosen over Track A's anchored-to-a-real-
  date approach because free text has room to be transparent about this rather than needing to fit
  a schema convention.
- Annotates `[WARN]`/`[ERROR]` using the **same** `select_cases.py` outlier functions and same
  `nm_latency_baseline.json` Track A used — deliberately, so neither track hands the model more
  pre-digested evidence than the other; the comparison should be about model reasoning, not about
  which track's tool does more work for it.
- Smoke-tested against real cases: TB-018's WARN lands on `datanode046` exactly like Track A's flat
  table (`+930.5ms verifiedByClient (1.0ms) [WARN]`, `+14.93s verifiedByClient (0.8ms) [WARN]`);
  TB-011 surfaces the real `ConnectException: Connection refused...` text with `[ERROR]`. The
  call-structure section only renders for traces that actually have `Edge` rows — confirmed only
  3 of the 27 named cases' specific `TaskID`s do (consistent with Phase 4's already-documented
  ~41% zero-edge-rate; not a new bug).
- **Leakage audit on the actual output text** (`audit_query_trace_leakage.py`, new) — calls
  `query_trace()` for all 27 real cases and greps the returned strings (not the DB) against all
  750 fault/category/trace_set tokens — **PASS, 0 hits**.

**`track_b/diagnostic_agent.py` (new, forked from Track A's) — orchestrator wiring:**
- Rate-limit handling, LLM builder, tool-call tracing, `diagnose()`/`save_results()`, and the smoke
  test harness are copied unchanged from Track A's `diagnostic_agent.py` — genuinely identical
  logic, not reimplemented.
- What's different, necessarily: no `SQL_MODEL`/`SQL_BACKEND`/etc. CONFIG block at all (see
  architecture note above); `query_logs` replaced with a `query_trace` tool wrapping
  `query_trace.py`'s deterministic function; `SYSTEM_PROMPT` rewritten to describe the new tool and
  HDFS terminology instead of Cloud Foundry/PaaS — this is the one prompt deviation the redesign
  requires, and it's scoped to Track B only (Track A's/PaaS's `SYSTEM_PROMPT` stays byte-identical
  per the frozen contract).
- Doc agent is mechanically unchanged (same `doc_agent.py`, imported directly rather than copied,
  since it needed no Track-B-specific modification) but points at the shared trace doc corpus
  (`../tracebench_doc_chroma_db`, collection `docs_trace_perfault`) via explicit
  `DOC_DB_PATH`/`DOC_COLLECTION` — importing across the `track_b/` → `tracebench_port/` boundary
  the same way `query_trace.py` already does for `select_cases.py`.
- **Bug found and fixed in Track A's `diagnostic_agent.py` while building this**: it had no
  `DOC_DB_PATH`/`DOC_COLLECTION` override at all — it silently imported `build_doc_index.py`'s own
  PaaS defaults (`./doc_chroma_db`, collection `docs`) and would have queried the wrong doc index
  on an actual run. Fixed the same way `SQL_INCLUDE_TABLES`/`SQL_SCHEMA_DESCRIPTION` already were:
  explicit CONFIG constants defaulting to the PaaS values (imported, not duplicated) with a
  commented block showing what to uncomment for the trace corpus.
- Verified: both files import cleanly; `build_tools()` constructs the right tool set for each
  (`[query_trace, query_docs]` for Track B, confirmed via `tools[i].name`); `query_trace` tool
  invokes correctly end-to-end through the LangChain tool wrapper. Full live orchestrator runs
  (actual LLM tool-calling) not tested — no API key available in this session, same limitation
  noted in Phases 3–4.

**Exit check (Track B redesign): MET** for the pieces buildable without a live LLM backend —
scoped raw data built and leakage-audited (both at the DB-column and the tool-output-text layer),
`query_trace` smoke-tested against real cases with parity-correct WARN/ERROR annotations, orchestrator
forked and wired with the doc-agent/config gap fixed on both tracks. **Not yet done**: an actual
live benchmark run of Track B (needs an API key); Phase 5's forked deterministic evidence-grounding
signal now needs a Track-B-specific adapter keyed off `query_trace` calls instead of SQL query
results (anticipated by the addendum, mechanics not yet written); Phase 6's interpretation grid
needs rewriting for the three-test triangulation framing instead of clean pairwise isolation.

### run_benchmark.py fixes + Phase 5 deterministic evaluator + judge prompt (this session)

**Why now:** both `run_benchmark.py` (still importing PaaS's `benchmark_incidents.py`) and the
deterministic evaluator (`Results/evaluate.py` sat as an untouched Phase-0 copy) were flagged as
blockers in the last status review — nothing downstream is runnable without them.

**`run_benchmark.py` fixes:**
- Track A: swapped `benchmark_incidents.BENCHMARK_CASES` → `benchmark_cases_trace.BENCHMARK_CASES_TRACE`,
  `case["incident_id"]` → `case["case_id"]`, and widened `--tier` from `{1,2,3}` to `{0,1,2,3}` — the
  trace case set has a Tier 0 (7 normal controls) the PaaS incident set never had, so the old parser
  couldn't even select them. Verified: `--help` output correct, case-count-by-tier matches the
  documented 5/3/12/7 split exactly.
- `track_b/run_benchmark.py` (new): forked from Track A's, importing `track_b/diagnostic_agent.py`
  instead and dropping the `SQL_MODEL`/`SQL_BACKEND` print lines (no SQL sub-agent for this track).
- Added `"track": "A_flat"` to Track A's `diagnose()`'s `model_config` dict, mirroring Track B's
  existing `"track": "B_native"` — this is what lets `evaluate_trace.py` (below) auto-detect which
  track a results file came from instead of needing a separate CLI flag.
- Also fixed, per the addendum's explicit Phase 6.1 requirement ("output filenames must encode
  model combo + dataset"): Track A's `OUTPUT_FILE` had no track suffix at all (Track B's already did,
  `__track-Bnative.json`) — added `__track-Aflat` for symmetry and to prevent a same-model-combo PaaS
  run and Track A run from ever looking identical from the filename alone.

**`evaluate_trace.py`** (new, adapted from `Results/evaluate.py` — same three-layer structure and
scoring philosophy, frozen per the Cleanliness Contract; only field names and one signal fork):
- Ground truth source swapped to `benchmark_cases_trace.BENCHMARK_CASES_TRACE`, keyed by `case_id`.
- `score_doc_retrieval`: renamed every `incident_ids` field read to `case_ids`, matching the fix
  already made in `build_doc_index.py` back in Phase 3 (this evaluator hadn't existed yet to catch
  the mismatch at the time).
- `score_reasoning_trace`: the **one deterministic signal that forks by track**, exactly as the
  addendum anticipated — reads `model_config["track"]` and checks for `query_logs` (Track A) or
  `query_trace` (Track B) as "the evidence tool," instead of hardcoding `query_logs`.
- **New**: `score_localization()` — spec 5.3's recommended signal, not present in the PaaS evaluator
  at all. Checks whether the diagnosis names the real `affected_component` from the isolated
  `data/ground_truth_trace.json` (read post-hoc, after the agent has already produced its diagnosis —
  this does not violate the frozen ground-truth-isolation rule, which is about what the *agent* sees
  during diagnosis). Reports `"n/a"` rather than a miss for Tier 0 cases, which structurally have no
  `affected_component` to localize.
- Report/filenames now carry the track explicitly (`report["tracks"]`, per-case `"track"` field,
  output filename derived from the input results filename) so PaaS/Track A/Track B reports never
  collide and can be sliced by track later in Phase 6.

**Real bug found and fixed via testing, not just inspection**: built two synthetic
`diagnostic_results`-shaped files (one per track, matching real `diagnose()` output exactly) to
exercise `evaluate_trace.py` end-to-end before any live run exists. A synthetic *correct* "no fault"
diagnosis for a Tier 0 case scored `miss` — traced it to `benchmark_cases_trace.py`: all 7 Tier 0
cases had `answer_required = ["no fault", "no anomaly", "normal", "no issue", "healthy"]`, and
`score_answer`'s `answer_required` check is conjunctive (`all(...)` — every keyword must appear).
Those five phrases are alternative ways of saying the same one thing, not five independent facts —
under the conjunctive rule, essentially no realistic diagnosis would ever earn `full_credit` on any
of the 7 normal-control cases (26% of the benchmark), regardless of correctness. `benchmark_cases_trace.py`'s
own docstring already flagged these signals as "provisional pending the Phase 5 rewrite the spec
calls for" — fixed now: `answer_required = ["no fault"]` (the single canonical phrase, matching the
question's own framing), the other four moved to `answer_partial`. Re-ran the synthetic test after
the fix — same correct diagnosis now scores `full_credit` as it should. This is exactly the kind of
thing that only surfaces by actually running the scorer against realistic input, not by reading the
code — logged here as the reason the synthetic-test step was worth doing before a real (costly) live
run.

**`judge_prompt_gui_trace.md`** (new, adapted from `Results/judge_prompt_gui.md` at the PaaS root —
discovered this session that the "LLM judge" is a paste-into-chat prompt, not a script, which changes
what "Phase 5's judge adaptation" actually means: careful prompt editing, not code):
- Domain rewrite (HDFS/TraceBench instead of Cloud Foundry/PaaS), tool references updated to name
  both possible evidence tools (`query_logs` for Track A runs, `query_trace` for Track B runs —
  judge is told to check `model_config.track` to know which applies to a given result).
- **Real gap found and fixed**: the original rubric scores against a `root_cause` field in
  `ground_truth.json`. `ground_truth_trace.json` has no such field — the canonical one is
  `fault_name` (confirmed by inspection, not assumed). Rubric text updated to point at the field
  that actually exists.
- Added explicit Tier 0 (normal control) scoring guidance to Dimensions 1 and 3, which the PaaS
  rubric never needed (PaaS's incident set had no "nothing is wrong" case type) — without this a
  judge could plausibly misread a correct "no fault" diagnosis as "vacuous" under the existing 0-2
  definitions.
- Rubric dimensions themselves (root cause / evidence / fix, 0-2 each) and the red-herring concept
  are left structurally unchanged, per the frozen-rubric rule — the red-herring field definition now
  notes explicitly that TraceBench cases weren't authored with that property as a design goal (unlike
  PaaS's incident set), so the judge should apply the existing definition per-case from what it
  actually reads rather than assume most cases have it.
- Output schema: kept the `incident_id` field name (matches what's literally in the results JSON —
  diagnose()'s parameter name, holding the TraceBench `case_id` value), added a `track` field per
  case so `cross_cutting_observations` can note cross-track patterns.
- Not yet done: actually running this prompt against a real results file — needs live results to
  exist first (blocked on an API key, same as everything else downstream).

**Exit check (this session): MET** for everything buildable without a live LLM backend. Both
`run_benchmark.py` entrypoints verified (help output, case-count-by-tier). `evaluate_trace.py`
verified end-to-end against synthetic data for both tracks, including the track-fork logic actually
switching evidence-tool names correctly. One real scoring bug found and fixed via that testing, not
merely by code review. Judge prompt adapted and internally consistent with the current data shape.
**Still not done**: a live benchmark run on either track (no API key available this session, per
Phase 3/4's same limitation); running the judge prompt for real; Phase 6.

### Edge data-loss bug found, fixed, and reloaded (this session)

**How this was found:** the user pushed back on Phase 4's "~41% of traces have zero `Edge` rows —
a genuine data-quality gap in the source dump" claim, correctly pointing out it had only been
checked against our *loaded* `tracebench_raw.sqlite`, never against the original raw `.sql` files
directly. That check was the right call — the claim was wrong.

**What was actually true:** picked the 5 specific `TaskID`s from Phase 4's own investigation that
supposedly had zero `Edge` rows despite `Trace.NumEdges` claiming otherwise, and grepped the raw
`.sql` file's `INSERT INTO Edge` statement text directly (independent of `load_tracebench.py`
entirely). All 5 were present, with occurrence counts exactly matching their claimed `NumEdges`
values (11/35/19/35/23). The data was never missing from the dump — it was being dropped during
our own load.

**Root cause, fully traced:** `split_sql_statements()`'s quote-tracking has a "grammar-aware
lookahead" heuristic — when it hits a closing `'`, it checks whether the next non-whitespace
character is `,`, `)`, or `;` (the only contexts a real data-tuple string ends in); if not, it
assumes the quote is a mid-word apostrophe (e.g. "doesn't") and stays inside the string. This
heuristic was never validated against the mysqldump preamble's own directive lines — e.g.
`/*!40103 SET TIME_ZONE='+00:00' */;`, where the closing quote is followed by `*/;`, matching
none of the three accepted characters. The parser incorrectly concluded this valid,
properly-terminated string was still open, and stayed corrupted until a quote elsewhere happened
to satisfy the old check — silently merging everything in between (including the real `CREATE
TABLE Edge` and the first, larger `INSERT INTO Edge` statement) into one ~1MB blob starting with
`/*`, which the loader's own directive-skip filter then discarded with **no error logged** (the
reject log was empty — this is why "0 errors" across the whole original load didn't catch it).

**Confirmed universal, not a one-off:** wrote a scope-check (structural signature: an anomalously
large statement starting with `/*` or `SET`) and ran it across all 364 files. **364/364 (100%)**
showed the pattern — this directive is standard mysqldump boilerplate present in every file, so
this bug fired on every single load, every time, from the very first session. The near-identical
swallowed-statement size across files (~1,033,6xx characters) indicates it was consistently
losing the same thing each time — the first/larger `Edge` INSERT — while `Event`/`Trace`/
`Operation` (which load later in each file, past the point where a later quote happens to resync
the parser) were structurally protected from this specific bug.

**Fix**: extended the lookahead check to also accept a quote followed by `*/` (after optional
whitespace) as a valid terminator — a narrowly-scoped addition, since real `Event.Description`/
`OpName` content wouldn't plausibly contain a literal `*/` sequence immediately after an
apostrophe. Verified: the sample file that exposed the bug now splits the directive and the real
`Edge` INSERT into separate, correctly-recognized statements. Re-ran the 364-file scope check —
**0/364** show the pattern now.

**Second discovery, a direct byproduct of the fix**: the now-correctly-isolated statements reveal
the dump *does* contain a real `CREATE TABLE Edge` statement — Phase 1's conclusion that it didn't
(via `find_table_names.py`, which ran under the same buggy parser) was itself wrong, for the same
root cause. Its real column names are `TaskID`/`FatherTID`/`FatherStartTime`/`ChildTID` — not the
`TraceID`/`FatherNID`/`ChildNID` names picked when we thought we had to invent the schema
ourselves. Not a functional bug (we control our own SQLite column names, and everything downstream
already uses ours consistently, verified via real sample-tuple joins back in Phase 4) — but worth
recording for anyone cross-referencing the original dump. `FatherTID`/`ChildTID` confirms what we'd
already inferred semantically ("the `TID` of the father/child `Event` row"); we just labeled it
differently. Documented in `load_tracebench.py`'s comments; a full rename was considered and
deferred as cosmetic-only, pending the user's call.

**Full reload results** — `Event`/`Trace`/`Operation` byte-identical to before the fix (14,777,715
/ 370,334 / 6,894, confirming these three were never touched by this bug, exactly as the
per-file structural analysis predicted): `Edge` went from **2,560,426 → 6,303,153 rows (+146%)**.
The zero-edge-rate claim this whole investigation started from: **0.00%** of traces now have zero
`Edge` rows (was ~41%). It was entirely a loader artifact.

**Re-verification, not just re-running:**
- `verify_data_integrity.py --all`: **189/189** checks pass against the corrected data (up from
  183 in Phase 1 — the check count itself grew slightly since then; all pass regardless).
- Track B's scoped data (`build_track_b_data.py`) rebuilt: `Edge` in-scope rows 245,859 → 587,517.
  **27/27 cases now have real `Edge` data for their specific named instance** (was 3/27) — this
  materially improves `query_trace`'s call-structure section, which previously rendered for only
  3 of the 27 cases and now renders for all of them. Re-smoke-tested (TB-018's call structure now
  populated with real cross-host relationships) and both leakage audits re-run clean (DB columns:
  0 hits; `query_trace()` output text across all 27 cases: 0 hits).
- Track A's `logs` table needs no rebuild — it's built purely from `Event` rows, which are
  byte-identical, so it's necessarily unchanged. Confirmed rather than assumed.
- `ground_truth_trace.json`/`tracebench_manifest.csv` needed no regeneration — already
  re-validated by the integrity check above (ground truth's evidence paths use `Event`
  durations/text only, never `Edge`), and the manifest is pure filename parsing plus
  `Trace_Source` row counts (unchanged).

**Also added**: persistent indexes (`Event.TaskID`, `Edge.TraceID`, `Trace_Source.trace_set`) at
the end of `load_tracebench.py`'s load, and applied retroactively to the already-loaded DB. This
is unrelated to correctness — it's a response to repeatedly needing to background slow unindexed
queries (Phase 4's flatten step, Track B's data build, and the diagnostic queries in this very
investigation) — but worth doing now while touching this file.

**Exit check (this session): MET.** Bug root-caused via direct raw-file inspection independent of
the loader under test (same discipline `verify_data_integrity.py` already established), fix
verified both narrowly (sample file) and broadly (364/364 → 0/364), full reload completed,
independent integrity re-verification passed (189/189), both tracks' downstream data confirmed
correct (Track B rebuilt + re-audited, Track A confirmed unaffected). The Phase 4 CHANGELOG entry's
"~41% zero-edge-rate, genuine data-quality gap" claim above should be read as **superseded by this
entry**, not as still-accurate background.

### Four further data checks, requested by the user before proceeding to a live run (this session)

**Why:** having just found one bug the hard way, the user asked what else could be checked before
spending real API money. Four checks, all independent of the loader/DB (same discipline that found
the Edge bug):

1. **Full-corpus reconciliation** (`reconcile_full_corpus.py`, new) — `verify_data_integrity.py`
   only ever checked the 27 *selected* cases; the other 337 trace_sets had never been independently
   verified at all, and both tracks' shared statistics (Track A's WARN baseline, Track B's cached
   `nm_latency_baseline.json`) draw from the *entire* corpus, not just the 27 cases. Reused only
   `split_sql_statements()`/`split_value_tuples()` (text-analysis utilities) — never the DB or the
   loading path — to independently count every raw tuple in every one of the 364 files and compare
   against the loaded DB's row counts. **Result: exact match on all four tables, corpus-wide** —
   Event 14,777,715/14,777,715, Trace 370,334/370,334, Edge 6,303,153/6,303,153, Operation
   6,894/6,894. Not one row anywhere in the whole corpus is unaccounted for.
2. **File footers** — checked whether the closing `/*!...*/;` directive block (that restores what
   the preamble changed) has the same vulnerability precondition (a quoted string literal). It
   doesn't: every footer directive references a `@OLD_*`-style session variable, never a quoted
   value — confirmed across all 364 files (0 contain a quote character in their footer block). The
   bug's precondition structurally cannot occur there.
3. **Regression check on the fix's own original purpose** — the heuristic exists to keep genuine
   unescaped apostrophes in content (e.g. "doesn't") as literal text rather than false statement
   terminators. Found this scenario actually occurring in real data (`"...length 67108864 don't
   match block blk_-4458271377988461949_1332..."` in `Event.Description`) and confirmed it loads
   fully intact post-fix, not truncated at the apostrophe — the fix didn't regress the case it was
   originally protecting.
4. **Negative check** — does the fix's new acceptance pattern (`'` followed by optional whitespace
   then `*/`) ever occur outside the two known preamble directives, where it could cause a *new*
   false-positive early termination? Counted every occurrence of `'\s*\*/` across all 364 files:
   **728 total, exactly 364 × 2** — the two known directives per file, and nothing else, anywhere.
   Closes the door on the fix introducing a new bug in the opposite direction.

**Exit check: MET, unambiguously.** All four checks came back clean with no follow-up required.
Between this and the Edge bug fix's own re-verification, the raw data layer (`tracebench_raw.sqlite`)
now has: exact corpus-wide reconciliation, 189/189 independent per-case integrity checks, a
confirmed-uncorrupted footer, and both directions of the parser fix's correctness (didn't break the
original case, didn't introduce a new false-positive) explicitly checked rather than assumed.

### First live run — small sanity check, Track A, 2 models (this session)

**Setup:** 3 hand-picked cases (TB-018/slowDN, TB-011/deadDN, TB-021/normal control — chosen for
signal diversity, not the first 3 in list order) through Track A, with `gpt-5.4`/`openai` and
`meta-llama/Llama-3.3-70B-Instruct-Turbo`/`deepinfra` (the user specified Instruct Turbo over
`llama-3.3-70b-versatile`/`groq`, which "didn't work last time" in the original PaaS run). Reused
the existing API keys from `API_Keys.txt`. This is the first time any live model has touched any
part of this port — everything before this was mechanics-level (direct function calls, hand-written
queries) precisely because no API key was available in any prior session.

**Two real bugs found, both fixed immediately, not worked around:**
- A `UnicodeEncodeError` crashed the very first run — `diagnostic_agent.py`'s verbose output prints
  a `─` (U+2500) divider, and the ad-hoc script's redirected stdout defaulted to Windows' `cp1252`
  codepage, which can't encode it. Fixed at the invocation level (`PYTHONIOENCODING=utf-8`), not in
  project code — this is an artifact of how output was captured for this sanity check, not a defect
  in `diagnostic_agent.py` itself.
- Every single case failed with `Agent error: 'incident_ids'` on the first real attempt, for BOTH
  models. Root cause: `doc_agent.py` (unmodified since its Phase 0 copy from the PaaS original)
  still read `doc['incident_ids']` in two places — the verbose retrieval log line and the
  `retrieved_docs` dict `query_docs`'s tool wrapper returns — but `build_doc_index.py`'s
  `retrieve_docs()` has returned `case_ids` since Phase 3's rename. This is exactly the kind of gap
  that only a live run could catch: every prior test called `retrieve_docs()` directly or checked
  schema/mechanics, never exercised `doc_agent.query()` (the wrapper `query_docs` actually calls)
  end-to-end. Fixed both occurrences; confirmed no other live-code file has the same gap
  (`generate_doc_corpus.py` and `Results/evaluate.py` correctly still say `incident_ids` — they're
  the untouched PaaS baselines, not part of the trace pipeline).

**Results, after both fixes, `evaluate_trace.py` run on both:**

| | `gpt-5.4` | `Llama-3.3-70B-Instruct-Turbo` |
|---|---|---|
| Answer quality | 3/3 full_credit | 2/3 full_credit, 1 error |
| Localization hit rate | 1/2 localizable cases | 0/2 localizable cases |
| Avg trace score | 4.00/5 | 3.33/5 (dragged down by the 0/5 error case) |
| Mean wall-clock/case | 24.8s | 167.8s |

- **`gpt-5.4`**: all 3 correct, including an exact match on TB-018's fault type AND specific
  datanode (`"datanode046 was a single slow DataNode (slowDN)"` — ground truth: `slowDN`,
  `datanode046`), citing the real `verifiedByClient` WARN evidence with real timestamps. TB-021
  (normal control) correctly said "no fault... I checked the full log set... none were present" —
  the Tier 0 scoring fix from the Phase 5 session is doing its job on a real diagnosis, not just the
  synthetic test case used to find the bug.
- **`Llama-3.3-70B-Instruct-Turbo`**: failed TB-018 outright — emitted the tool call as literal text
  (`<function=query_logs{"question": "..."}</function>`) instead of a real structured tool call,
  exhausted the existing `malformed_tool_call` retry logic (already built into `diagnostic_agent.py`
  specifically for this DeepInfra-model failure mode), and gave up with zero tool calls recorded.
  Not a new bug — the known, already-mitigated-but-not-eliminated failure mode this model class
  exhibits. Got the other 2 cases right. Also markedly slower (167.8s mean vs. 24.8s) — worth
  factoring into cost/time expectations for a full sweep with this model.
- **Localization scoring nuance found on real output, not anticipated in Phase 5**: both models
  correctly diagnosed TB-011 (deadDN) but named the datanode by its **IP address**
  (`10.107.100.58`, which really is in the raw evidence text) rather than the resolved hostname
  `datanode001` that `score_localization()` checks for — both scored `loc=miss` despite being
  factually correct about *which* node. This isn't a bug in the diagnosis or the scorer, but a real
  gap in what the scorer currently checks for; worth deciding whether `score_localization()` should
  also accept the IP as an alternate match before running a fuller sweep, since this pattern will
  recur for every IP-only-observed case (the same 5 cases Phase 2's affected_component-resolution
  fix already flagged as client-observed-by-IP).

**Exit check: MET** for what a sanity check is meant to do — confirmed live tool-calling actually
works end-to-end on real data with real models (not just mechanics), found and fixed one real
project bug (`doc_agent.py`) and one environment artifact (encoding), and surfaced one real scoring
design question (IP-vs-hostname localization) worth resolving before a fuller sweep. Track B, the
full 27-case sweep, and the wider multi-model comparison are all still open — this was deliberately
small and cheap by design.

### Third model: the "Edge" tier (local, free) — the most substantively important result yet

**Setup:** same 3 cases, Track A, but the historical "Edge" tier config from `Archive/Edge_benchmark/`
(`qwen2.5:latest` for diagnostic+SQL, `llama3.2` for doc, all via local Ollama — `qwen`/`ollama`
backends, no API key, no cost). Ollama was installed but not running in this environment; started it
(`ollama serve`) and confirmed both required models were already cached locally (no download needed).
Noted for the record: this machine's GPU (GTX 1080) isn't supported by the installed Ollama build's
compiled CUDA architectures, so inference ran on CPU.

**User's framing, worth stating plainly since it shapes how to read everything below**: this result
is not a problem to work around — demonstrating exactly this kind of capability/cost tradeoff (a
weak local model fabricating evidence, taking far longer) *is what this benchmark exists to measure*.
The Edge tier stays in the sweep.

**The most important finding of this port so far**: the SQL sub-agent (`qwen2.5:latest`) fabricated
an entire fake table of log rows for TB-018, wholesale — not paraphrased-but-wrong, invented from
nothing:
```
| Success: BlockReader[...dst=C4113E01C484F2EB...]        | 2023-11-15T14:30:00.000Z |
| WARN: ...Slow read operation took 1.5 seconds            | 2023-11-15T14:30:02.000Z |
| ERROR: ...IOException: java.io.EOFException               | 2023-11-15T...           |
```
Verified point-by-point that none of it is real:
- Every real timestamp in this dataset is anchored to 2013-11-04 (confirmed repeatedly since Phase
  4). `2023-11-15` — a different decade — appears nowhere in the actual data.
- The block-ID format (`BP-567984234-...-blk_1077_...`) doesn't match this dataset's real format
  (`blk_-4458271377988461949_1332`) — it reads as a generic HDFS-block-ID pattern recalled from
  training data, not a query result.
- `dst=C4113E01C484F2EB` treats the trace/instance ID as if it were a datanode hostname; real
  `node_id` values in this schema look like `datanode046`.
- TB-018's ground truth (`slowDN`) is a pure latency-outlier signal — `evidence_type:
  host_latency_outlier` in `ground_truth_trace.json`, no exception text at all. An `EOFException`
  claim isn't just imprecise here, it describes a mechanism this fault's evidence structurally
  cannot contain.

**The deterministic scorer completely missed it** — `evaluate_trace.py` gave TB-018 `full_credit`,
because the question text itself primes "slow"/"datanode" and the fabricated diagnosis happens to
contain both words. This is a real, concrete demonstration of exactly why the frozen LLM-judge
rubric's evidence-grounding dimension exists and explicitly overrides surface keyword matching:
*"fabricated evidence... scores 0 on this dimension even if the overall diagnosis happens to match
the canonical root cause."* Without the judge layer, this run would be indistinguishable from
`gpt-5.4`'s correct, real-evidence-grounded TB-018 diagnosis in the deterministic report alone — the
two layers are doing genuinely different, complementary jobs, not redundant ones.

**Scores** (`evaluate_trace.py`): 3/3 `full_credit` (misleading per above), `loc` 0/2 hit,
avg trace score 2.67/5 (lower than both API models — `query_docs` was never called in any of the 3
cases, unlike the other two models, which called it at least once per case).

**Timing — a second, independently real finding**: mean 677.9s/case, up to 1474.2s (24.6 minutes)
for TB-021 alone (4 tool calls, one of which was the model outputting raw SQL text instead of a
clean final answer — visible mid-task confusion, not just slowness). At this rate a full 27-case
Track A sweep with this model would run into multiple hours, on this hardware. That cost, not just
the fabrication, is itself part of the finding this benchmark is designed to surface.

**Not yet resolved, carried forward rather than fixed silently**: the IP-vs-hostname localization
gap from the previous two models applies here too (TB-011 loc=miss for the same reason). The Edge
tier's own TB-011 diagnosis was also less precise than the other two models' (correctly identified
"connection refused" but stayed at "network or service issue" rather than pinpointing a single dead
DataNode process) and slightly garbled the instance ID in its own prose.

**Artifacts preserved, not treated as throwaway this time**: unlike the first sanity check (whose raw
JSON was left uncommitted as disposable), this run's result is genuine evidentiary content the
CHANGELOG's claims above should be checkable against — committed at
`Results/sanity_check/sanity__edge-qwen25-llama32__track-Aflat.json` and
`Results/sanity_check/eval__edge.json`, along with the `gpt-5.4` and `Llama-3.3-70B-Instruct-Turbo`
runs from the same session.

**Exit check: MET, and this is the clearest evidence yet that the project's two-layer scoring design
(deterministic + frozen LLM judge) is load-bearing, not redundant** — the deterministic layer alone
would have silently certified a fabricated diagnosis as fully correct.
