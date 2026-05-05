# PaaS Benchmark

A multi-agent RAG system that benchmarks how well LLMs can diagnose Cloud Foundry-style PaaS incidents. Three independent agents collaborate — a SQL log-querying agent, a documentation RAG agent, and an orchestrating diagnostic agent — across 25 synthetic incidents spanning single-component failures (Tier 1) through complex multi-factor cascades (Tier 3).

Results are scored on three dimensions: answer quality, retrieval quality, and reasoning trace quality. Output filenames encode the exact model configuration so runs never overwrite each other and are easy to compare.

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally on `http://localhost:11434`
- The following models pulled in Ollama:

```bash
ollama pull qwen2.5:latest
ollama pull llama3.2
```

---

## Installation

```bash
cd PaaS_Benchmark
pip install -r requirements.txt
```

---

## First-time Setup

The ChromaDB vector indexes must be built before running the benchmark. Run these once (or again if you regenerate the data files):

```bash
# Index the documentation corpus into ChromaDB
python build_doc_index.py

# Index the log database into ChromaDB
python build_log_index.py
```

If the documentation corpus (`data/doc_corpus.jsonl`) does not exist yet:

```bash
python generate_doc_corpus.py
python build_doc_index.py
```

---

## Running the Benchmark

```bash
# Run all 25 incidents
python run_benchmark.py

# Run only Tier 1 incidents (single-component failures)
python run_benchmark.py --tier 1

# Run only the first 5 incidents
python run_benchmark.py --limit 5

# Override the output filename
python run_benchmark.py --output my_results.json
```

Results are written to `Results/` with a filename that encodes the model configuration, e.g.:

```
Results/diagnostic_results__diag-qwen25-latest__sql-qwen25-latest__doc-llama32.json
```

---

## Evaluating Results

```bash
cd Results
python evaluate.py
```

This produces `Results/evaluation_report.json` with aggregate metrics:

| Dimension | What it measures |
|---|---|
| **Answer quality** | Did the diagnosis contain the required root-cause keywords? |
| **Retrieval quality** | Did the doc agent retrieve the relevant documentation? |
| **Trace quality** | Did the agent follow a sound diagnostic reasoning pattern? (0–5 scale) |

---

## Changing Models

All model assignments live at the top of [diagnostic_agent.py](diagnostic_agent.py):

```python
# Orchestrator
DIAGNOSTIC_MODEL    = "qwen2.5:latest"

# SQL / log-querying agent
SQL_MODEL           = "qwen2.5:latest"

# Documentation RAG agent
DOC_MODEL           = "llama3.2"
```

Edit one block, then run `python run_benchmark.py`. The output filename auto-updates to reflect the new config so previous results are preserved.

---

## Smoke Testing Individual Agents

```bash
# Test the SQL agent (queries logs for a hardcoded question)
python sql_agent.py

# Test the documentation agent
python doc_agent.py

# Test the full diagnostic orchestrator on a single incident
python diagnostic_agent.py
```

---

## Project Structure

```
PaaS_Benchmark/
├── run_benchmark.py          # Entry point — runs incidents, saves results
├── benchmark_incidents.py    # The 25 test cases (Tier 1–3)
├── diagnostic_agent.py       # Orchestrator agent + model config
├── sql_agent.py              # Sub-agent: queries logs via SQL
├── doc_agent.py              # Sub-agent: retrieves documentation via RAG
├── build_doc_index.py        # Indexes doc_corpus.jsonl → ChromaDB
├── build_log_index.py        # Indexes log data → ChromaDB
├── generate_doc_corpus.py    # Generates the synthetic documentation corpus
├── data/
│   ├── benchmark_db.sqlite   # PaaS log database (SQLite)
│   ├── doc_corpus.jsonl      # Documentation corpus (78 docs)
│   └── hdfs_output.jsonl     # Raw log data
├── chroma_db/                # ChromaDB index for logs
├── doc_chroma_db/            # ChromaDB index for documentation
└── Results/
    ├── evaluate.py           # Evaluation script
    ├── ground_truth.json     # Per-incident scoring criteria
    ├── evaluation_report.json
    └── diagnostic_results__*.json
```

---

## The 25 Benchmark Incidents

| Tier | Incidents | Example scenarios |
|---|---|---|
| **1** — Single-component | INC-001 – INC-007 | Wrong port binding, disk quota exceeded, JVM OOM, certificate expiry |
| **2** — Cross-component | INC-008 – INC-018 | DB connection pool exhaustion, cell OOM, network policy blocks, NATS saturation |
| **3** — Multi-factor cascade | INC-019 – INC-025 | Circular service dependency, BBS quorum loss, mTLS org-wide expiry |

---

## Agent Architecture

```
Incident question
       ↓
┌─────────────────────────┐
│  Diagnostic Agent       │  ← orchestrates reasoning (Qwen 2.5)
│  (orchestrator)         │
└────────┬────────────────┘
         │ tool calls
    ┌────┴────┐
    ↓         ↓
┌────────┐  ┌────────┐
│  SQL   │  │  Doc   │
│ Agent  │  │ Agent  │
└───┬────┘  └───┬────┘
    ↓            ↓
 SQLite      ChromaDB
 (logs)      (docs)
```

The orchestrator queries logs first for concrete evidence, then retrieves documentation to interpret that evidence, then synthesizes a root-cause diagnosis with supporting evidence and a recommended fix.
