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
  `NumEdges > 0` and found **zero** matching `Edge` rows for any of them (a real data-quality gap
  in the dump itself: only ~59% of `TaskID`s have any `Edge` row at all despite `NumEdges` often
  claiming otherwise). Confirmed this is not a join-key bug — 99.9995% of `Edge` rows *do* match
  a real `TaskID` when one exists, and `FatherNID`/`ChildNID` correctly resolve to real `Event`
  rows with sensible cross-host parent/child relationships (e.g. an RPC client's `newBlockReader`
  as father of a DataNode's `verifiedByClient`). Both schema descriptions tell the agent not to
  trust the count columns and to `COUNT(*)` the real rows instead.
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
