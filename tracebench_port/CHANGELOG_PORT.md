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
