#!/usr/bin/env python3
"""
diagnostic_agent.py  (Track B -- trace-native, deterministic retrieval)

Orchestrating diagnostic agent for the TraceBench Track B arm of the port.
Forked from tracebench_port/diagnostic_agent.py (Track A / PaaS). The rate
limit handling, LLM builder, tool-call tracing, diagnose()/save_results(),
and smoke-test harness below are UNCHANGED from that file -- copy, not
rewrite. What's genuinely different, and why:

  query_logs (an LLM SQL sub-agent) -> query_trace (deterministic Python,
  see query_trace.py). This is the sanctioned, discussed-and-agreed
  redesign: Track B trades "identical tool interface to Track A" for
  "no second model's SQL competence confounding the result" -- retrieval
  for this track has no LLM in the loop at all, so there is no SQL_MODEL /
  SQL_BACKEND / SQL_DB_URI / SQL_INCLUDE_TABLES / SQL_SCHEMA_DESCRIPTION
  config block here. Only the orchestrator model and the doc-agent model
  are in play for Track B -- a real, documented difference in what a
  "tier" means for this track vs. Track A/PaaS (see CHANGELOG_PORT.md).

The doc agent is UNCHANGED in mechanism (same doc_agent.py, same
build_doc_index retrieve_docs), but points at the shared trace doc corpus
(tracebench_port/tracebench_doc_chroma_db, collection docs_trace_perfault)
via explicit DOC_DB_PATH/DOC_COLLECTION overrides -- Track A's
diagnostic_agent.py needed the identical fix (it had no override for these
at all and would otherwise default to the PaaS doc index).

Edit the CONFIG section, then run:
    python diagnostic_agent.py

Dependencies:
    pip install langchain-community langchain-openai langgraph
    pip install chromadb sentence-transformers ollama
"""

import json
import time
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

import sys
sys.path.insert(0, "..")
from doc_agent import query as doc_query  # noqa: E402

from query_trace import query_trace as _query_trace_impl


# ============================================================================
# RATE LIMIT HANDLING -- identical to Track A / PaaS diagnostic_agent.py
# ============================================================================

import re

def _parse_retry_delay(error_message):
    """Pull the 'try again in Xs' value from a Groq 429 error message."""
    m = re.search(r"try again in ([\d.]+)s", str(error_message))
    if m:
        return float(m.group(1))
    return 5.0  # safe default

def invoke_with_rate_limit_retry(agent, payload, config, max_retries=5, verbose=True):
    """
    Invoke a langgraph agent, catching 429 rate-limit errors and retrying
    after the API-suggested delay.

    Returns:
        (result, total_sleep_seconds)
    """
    total_sleep = 0.0
    for attempt in range(max_retries + 1):
        try:
            result = agent.invoke(payload, config=config)
            return result, total_sleep
        except Exception as e:
            err_str = str(e)
            is_rate_limit = (
                "429" in err_str
                or "rate_limit" in err_str.lower()
                or "tokens per minute" in err_str.lower()
            )
            if not is_rate_limit or attempt == max_retries:
                raise
            delay = _parse_retry_delay(err_str) + 0.5  # small buffer
            if verbose:
                print(f"  [Rate limit hit, attempt {attempt+1}/{max_retries}] "
                      f"sleeping {delay:.1f}s before retry...")
            time.sleep(delay)
            total_sleep += delay
    # unreachable
    raise RuntimeError("retry loop exhausted")


# ============================================================================
# CONFIG — change model names here to run different benchmark configurations
#
# To run a controlled experiment:
#   - Change one block only; leave the others untouched.
#   - The output filename encodes the model combo so results don't overwrite.
#
# Backends:
#   "qwen"      — local Qwen2.5 family via ChatOllama (native /api/chat)
#   "ollama"    — local non-tool-calling models via ChatOllama
#   "groq"      — Groq API via ChatOpenAI -> /v1. Requires $env:GROQ_API_KEY.
#   "deepinfra" — DeepInfra API via ChatOpenAI -> /v1/openai.
#                 Requires $env:DEEPINFRA_API_KEY.
#   "openai"    — OpenAI API direct (default endpoint).
#                 Requires $env:OPENAI_API_KEY.
# ============================================================================

# --- Diagnostic agent (orchestrator) ---
DIAGNOSTIC_MODEL    = "gpt-5.4"
DIAGNOSTIC_BACKEND  = "openai"      # "qwen", "ollama", "groq", "deepinfra", or "openai"
DIAGNOSTIC_TEMP     = 0.0
DIAGNOSTIC_BASE_URL = "http://localhost:11434/v1"   # ignored for API backends
DIAGNOSTIC_API_KEY  = "ollama"                      # ignored for API backends

# --- Retrieval: query_trace is deterministic Python, not an LLM sub-agent ---
# (No SQL_MODEL/SQL_BACKEND/etc block here -- see module docstring.)

# --- Documentation agent ---
DOC_MODEL           = "gpt-5.4"
DOC_BACKEND         = "openai"      # "ollama", "groq", "deepinfra", or "openai"
DOC_BASE_URL        = "http://localhost:11434"
DOC_N_RESULTS       = 5
# Shared trace doc corpus (Phase 3, built once, used by BOTH tracks) -- NOT
# the PaaS default build_doc_index.DB_PATH/COLLECTION_NAME would otherwise
# import. Point at the primary per-fault collection; swap to
# "docs_trace_category" for the secondary leakage-comparison run.
DOC_DB_PATH         = "../tracebench_doc_chroma_db"
DOC_COLLECTION      = "docs_trace_perfault"

# --- Orchestrator behaviour ---
MAX_TURNS           = 6
VERBOSE             = True

# Output filename encodes the model combo for easy result comparison.
def _sanitize_model_name(name):
    for ch in ("/", "\\", ":", ".", " "):
        name = name.replace(ch, "-")
    return name

OUTPUT_FILE = (
    f"diagnostic_results"
    f"__diag-{_sanitize_model_name(DIAGNOSTIC_MODEL)}"
    f"__doc-{_sanitize_model_name(DOC_MODEL)}"
    f"__track-Bnative.json"
)


# ============================================================================
# DIAGNOSTIC AGENT LLM BUILDER -- identical to Track A / PaaS
# ============================================================================

def _build_diagnostic_llm(
    model=DIAGNOSTIC_MODEL,
    backend=DIAGNOSTIC_BACKEND,
    temp=DIAGNOSTIC_TEMP,
    base_url=DIAGNOSTIC_BASE_URL,
    api_key=DIAGNOSTIC_API_KEY,
):
    if backend == "qwen":
        from langchain_ollama import ChatOllama as _ChatOllama
        ollama_base = base_url.rstrip("/")
        if ollama_base.endswith("/v1"):
            ollama_base = ollama_base[:-3]
        return _ChatOllama(model=model, temperature=temp, base_url=ollama_base)
    elif backend == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, temperature=temp, base_url=base_url)
    elif backend in ("groq", "deepinfra", "openai"):
        import os
        from langchain_openai import ChatOpenAI

        if backend == "groq":
            api_base = "https://api.groq.com/openai/v1"
            env_var  = "GROQ_API_KEY"
            example  = "gsk_..."
        elif backend == "deepinfra":
            api_base = "https://api.deepinfra.com/v1/openai"
            env_var  = "DEEPINFRA_API_KEY"
            example  = "your-deepinfra-key"
        else:  # openai
            api_base = None
            env_var  = "OPENAI_API_KEY"
            example  = "sk-..."

        api_key = os.environ.get(env_var)
        if not api_key:
            raise RuntimeError(
                f"{env_var} not set in environment. "
                f"Set it before running: $env:{env_var} = '{example}'"
            )
        kwargs = {"model": model, "temperature": temp, "api_key": api_key}
        if api_base is not None:
            kwargs["base_url"] = api_base
        return ChatOpenAI(**kwargs)
    else:
        raise ValueError(
            f"Unknown diagnostic backend: {backend!r}. "
            f"Use 'qwen', 'ollama', 'groq', 'deepinfra', or 'openai'."
        )


# ============================================================================
# TOOL CALL TRACE -- identical to Track A / PaaS
# ============================================================================

def _make_trace():
    """Return a fresh (trace_list, record_fn) pair."""
    trace = []
    def record(tool_name, inputs, result, duration_ms=None):
        entry = {"tool": tool_name, "inputs": inputs, "result": result}
        if duration_ms is not None:
            entry["duration_ms"] = round(duration_ms, 1)
        trace.append(entry)
    return trace, record


# ============================================================================
# TOOL BUILDERS
# ============================================================================

def build_tools(
    doc_model=DOC_MODEL,
    doc_backend=DOC_BACKEND,
    doc_n_results=DOC_N_RESULTS,
    doc_db_path=DOC_DB_PATH,
    doc_collection=DOC_COLLECTION,
):
    """
    Build and return (tools_list, trace_list).

    The trace_list is populated in-place as the tools are called.
    Pass it to diagnose() so it ends up in the result dict.
    """
    trace, record = _make_trace()

    @tool
    def query_trace(trace_id: str) -> str:
        """
        Reconstruct the full HDFS request trace for a specific instance:
        a per-host timeline of operations (with durations and any real
        exception text) plus the cross-host call structure. Use this tool
        when you need to:
          - See the sequence and timing of operations for a specific instance
          - Find exception/error text for a specific instance
          - Identify which host(s) show unusually high latency relative to
            their peers for this workload
          - See how operations on different hosts relate to each other
            (which RPC triggered which downstream operation)

        Input: the instance_id (trace/request identifier). It is ALREADY
        present in the question you were asked -- look for text like
        "instance C4113E01C484F2EB" or "(instance 03FB08C229C2844D)" and
        copy that exact string as the argument. Never ask the operator to
        provide it again; it has already been given to you.
        Output: a formatted per-host timeline and call-structure summary.

        Examples:
          Question mentions "instance C4113E01C484F2EB" -> query_trace("C4113E01C484F2EB")
          Question mentions "instance 03FB08C229C2844D" -> query_trace("03FB08C229C2844D")
        """
        t0 = time.perf_counter()
        answer = _query_trace_impl(trace_id)
        duration_ms = (time.perf_counter() - t0) * 1000
        record("query_trace", {"trace_id": trace_id}, answer, duration_ms)
        return answer

    @tool
    def query_docs(question: str) -> str:
        """
        Search the HDFS fault documentation corpus for operational knowledge.
        Use this tool when you need to:
          - Understand what a specific error message or latency pattern means
          - Find the investigation steps for a known failure pattern
          - Look up configuration thresholds relevant to a suspected fault
          - Understand the architecture behind a symptom (e.g. the write
            pipeline, DataNode liveness handling, checksum verification)

        Input: a natural language question about HDFS behaviour or operations.
        Output: an answer synthesised from retrieved runbooks, error references,
                config notes, and architecture documentation.

        Examples:
          "What does a SocketTimeoutException on a DataNode read mean?"
          "What is the runbook for a DataNode that is slower than its peers?"
        """
        t0 = time.perf_counter()
        result = doc_query(
            question=question,
            n_results=doc_n_results,
            llm_model=doc_model,
            backend=doc_backend,
            db_path=doc_db_path,
            collection_name=doc_collection,
            verbose=False,
        )
        duration_ms = (time.perf_counter() - t0) * 1000
        record("query_docs", {"question": question}, {
            "answer":         result["answer"],
            "retrieved_docs": result["retrieved_docs"],
        }, duration_ms)
        return result["answer"]

    return [query_trace, query_docs], trace


# ============================================================================
# SYSTEM PROMPT
#
# Necessarily different from Track A/PaaS's prompt -- it describes a
# genuinely different tool (query_trace, not query_logs). Structure/rules
# mirror the original closely; only the tool description and terminology
# (HDFS platform, not Cloud Foundry) change. This is the ONE prompt
# deviation the Track B redesign requires -- see CHANGELOG_PORT.md.
# ============================================================================

SYSTEM_PROMPT = """You are an expert distributed-systems reliability engineer
diagnosing incidents on an HDFS (Hadoop Distributed File System) cluster.

You have two tools:
  query_trace — reconstruct the full timeline and call structure for a
                specific instance (request/trace)
  query_docs  — search HDFS fault documentation for operational knowledge

## Diagnostic approach

1. Read the incident description carefully. Identify the symptoms and the
   instance_id named.
2. Use query_trace with that instance_id to see the actual timeline: which
   host(s) are involved, any exception text, and any latency-outlier
   annotation.
3. Use query_docs to understand what the observed pattern means and what
   the root cause category is.
4. If the first round of evidence is ambiguous, do a second round:
   query_trace again if there's a related instance to check, then query_docs
   for confirmation.
5. Synthesise a final diagnosis that states:
   - The root cause (specific and technical, not vague)
   - The key trace evidence that supports the diagnosis (host, operation,
     exception text, or latency comparison)
   - The failure pattern
   - The recommended fix

## Rules
- Never guess the root cause without trace evidence.
- Always use query_trace before concluding — your diagnosis must be grounded
  in the actual trace data, not just documentation knowledge.
- Be specific: name the host, the operation, the exception text or latency
  figure.
- Times shown for different hosts in a trace are NOT directly comparable to
  each other (independent clocks) — only use same-host ordering and
  per-operation durations, not cross-host timing, as evidence.
- If the evidence points to a red herring (a symptom that looks like a cause),
  say so and identify what the actual upstream cause is.
- Final answer: root cause in one sentence, evidence in 2-3 bullet points,
  recommended fix in one sentence."""


# ============================================================================
# MAIN BUILDER
# ============================================================================

def build_diagnostic_agent(
    diagnostic_model=DIAGNOSTIC_MODEL,
    diagnostic_backend=DIAGNOSTIC_BACKEND,
    diagnostic_temp=DIAGNOSTIC_TEMP,
    diagnostic_base_url=DIAGNOSTIC_BASE_URL,
    diagnostic_api_key=DIAGNOSTIC_API_KEY,
    doc_model=DOC_MODEL,
    doc_backend=DOC_BACKEND,
    doc_n_results=DOC_N_RESULTS,
    doc_db_path=DOC_DB_PATH,
    doc_collection=DOC_COLLECTION,
    max_turns=MAX_TURNS,
):
    """
    Build and return (agent, tools, trace) for one benchmark configuration.

    Returns:
        agent  — langgraph agent ready for .invoke()
        tools  — [query_trace, query_docs] closures bound to this config
        trace  — list populated in-place during .invoke(); pass to diagnose()
    """
    llm = _build_diagnostic_llm(
        model=diagnostic_model,
        backend=diagnostic_backend,
        temp=diagnostic_temp,
        base_url=diagnostic_base_url,
        api_key=diagnostic_api_key,
    )

    tools, trace = build_tools(
        doc_model=doc_model,
        doc_backend=doc_backend,
        doc_n_results=doc_n_results,
        doc_db_path=doc_db_path,
        doc_collection=doc_collection,
    )

    from langchain_core.messages import SystemMessage

    def _state_mod(state):
        msgs = list(state.get("messages", []))
        if not msgs or not isinstance(msgs[0], SystemMessage):
            return [SystemMessage(content=SYSTEM_PROMPT)] + msgs
        return msgs

    agent = create_react_agent(llm, tools, prompt=_state_mod)
    agent._max_iterations = max_turns

    try:
        bound_tools = getattr(llm.bind_tools(tools), "kwargs", {}).get("tools")
        if not bound_tools:
            print(f"  WARNING: diagnostic LLM ({diagnostic_model}) has no bound "
                  f"tools — orchestrator will not execute tool calls.")
        else:
            print(f"  Diagnostic LLM bound {len(bound_tools)} tool(s) successfully.")
    except Exception as e:
        print(f"  WARNING: could not verify tool binding for diagnostic agent: {e}")

    if diagnostic_backend in ("groq", "deepinfra", "openai"):
        try:
            print("  Warming up the orchestrator (one throwaway invocation)...")
            _ = agent.invoke(
                {"messages": [HumanMessage(content=(
                    "Acknowledge readiness. Respond with the single word: ready"
                ))]},
                config={"recursion_limit": 4},
            )
            trace.clear()
            print("  Warm-up complete.")
        except Exception as e:
            print(f"  WARNING: warm-up call failed ({e}); proceeding anyway.")
            trace.clear()

    return agent, tools, trace


# ============================================================================
# DIAGNOSE -- identical to Track A / PaaS except model_config has no sql_*
# fields (there is no SQL sub-agent model for Track B).
# ============================================================================

def diagnose(
    incident_id,
    question,
    agent,
    trace,
    verbose=VERBOSE,
):
    """
    Run a single incident scenario through a pre-built diagnostic agent.

    Returns:
        dict with incident_id, question, diagnosis, status,
        tool_call_trace, and model_config (for result provenance)
    """
    trace.clear()

    max_iter = getattr(agent, "_max_iterations", MAX_TURNS)

    if verbose:
        print("\n" + "=" * 70)
        print(f"INCIDENT : {incident_id}")
        print(f"QUESTION : {question[:100]}...")
        print("=" * 70)

    diagnosis = None
    status    = "ok"
    malformed_tool_call_retries = 0
    MAX_MALFORMED_RETRIES       = 2

    t_start = time.perf_counter()
    rate_limit_sleep = 0.0
    try:
        for attempt in range(MAX_MALFORMED_RETRIES + 1):
            if attempt > 0:
                trace.clear()

            result, sleep_this_call = invoke_with_rate_limit_retry(
                agent,
                {"messages": [HumanMessage(content=question)]},
                config={"recursion_limit": max_iter * 3},
                verbose=verbose,
            )
            rate_limit_sleep += sleep_this_call
            messages  = result.get("messages", [])
            diagnosis = messages[-1].content if messages else str(result)

            is_malformed = (
                len(trace) == 0
                and diagnosis is not None
                and "<function=" in diagnosis
            )
            if not is_malformed:
                break

            malformed_tool_call_retries += 1
            if verbose:
                print(f"\n  [Malformed tool call detected, "
                      f"attempt {attempt+1}/{MAX_MALFORMED_RETRIES+1}] retrying...")

        if malformed_tool_call_retries >= MAX_MALFORMED_RETRIES and is_malformed:
            status = "malformed_tool_call_unrecovered"

    except Exception as e:
        diagnosis = f"Agent error: {e}"
        status    = "error"
    total_seconds = time.perf_counter() - t_start
    active_seconds = max(0.0, total_seconds - rate_limit_sleep)

    tool_ms_total = sum(c.get("duration_ms", 0) for c in trace)
    trace_ms      = sum(c.get("duration_ms", 0) for c in trace if c["tool"] == "query_trace")
    doc_ms        = sum(c.get("duration_ms", 0) for c in trace if c["tool"] == "query_docs")
    orchestrator_ms = max(0.0, active_seconds * 1000 - tool_ms_total)

    timing = {
        "total_seconds":        round(total_seconds, 3),
        "active_seconds":       round(active_seconds, 3),
        "rate_limit_sleep_s":   round(rate_limit_sleep, 3),
        "tool_ms_total":        round(tool_ms_total, 1),
        "trace_ms_total":       round(trace_ms, 1),
        "doc_ms_total":         round(doc_ms, 1),
        "orchestrator_ms":      round(orchestrator_ms, 1),
        "n_tool_calls":         len(trace),
        "mean_ms_per_tool_call": round(tool_ms_total / len(trace), 1) if trace else 0.0,
        "malformed_tool_call_retries": malformed_tool_call_retries,
    }

    if verbose:
        print(f"\n{'─' * 70}")
        retry_note = f"  RETRIES: {malformed_tool_call_retries}" if malformed_tool_call_retries else ""
        print(f"TOOL CALLS: {len(trace)}   TOTAL TIME: {total_seconds:.1f}s{retry_note}")
        for i, call in enumerate(trace, 1):
            arg = call["inputs"].get("trace_id") or call["inputs"].get("question", "")
            dt = call.get("duration_ms", 0)
            print(f"  {i}. {call['tool']}({str(arg)[:60]!r})  [{dt/1000:.1f}s]")
        print("\nDIAGNOSIS:")
        print("-" * 70)
        print(diagnosis)
        print("-" * 70)

    return {
        "incident_id":     incident_id,
        "question":        question,
        "diagnosis":       diagnosis,
        "status":          status,
        "tool_call_trace": list(trace),
        "timing":          timing,
        "model_config": {
            "track":              "B_native",
            "diagnostic_model":   DIAGNOSTIC_MODEL,
            "diagnostic_backend": DIAGNOSTIC_BACKEND,
            "doc_model":          DOC_MODEL,
            "doc_backend":        DOC_BACKEND,
        },
    }


def save_results(results, output_file):
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved {len(results)} result(s) to {output_file}")


# ============================================================================
# SMOKE TEST  (NOT a benchmark — runs 2 real cases to verify the orchestrator
# is wired up and that both tools are reachable.)
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("DIAGNOSTIC AGENT SMOKE TEST (Track B -- trace-native)")
    print("=" * 70)
    print("This is NOT the benchmark. It runs 2 sample cases to verify the")
    print("orchestrator and both tools are wired up correctly.")
    print("=" * 70)
    print()
    print(f"Diagnostic model : {DIAGNOSTIC_MODEL} ({DIAGNOSTIC_BACKEND})")
    print(f"Doc model        : {DOC_MODEL} ({DOC_BACKEND})")
    print(f"Doc collection   : {DOC_COLLECTION} @ {DOC_DB_PATH}")
    print()

    agent, tools, trace = build_diagnostic_agent()

    smoke_scenarios = [
        {
            "incident_id": "TB-018",
            "question": (
                "During a read HDFS workload (instance C4113E01C484F2EB), one "
                "datanode is measurably slower than its peers across multiple "
                "block-transfer operations, though every request still "
                "eventually completes successfully. What is the root cause?"
            ),
        },
        {
            "incident_id": "TB-011",
            "question": (
                "During a read-only HDFS workload (instance 03FB08C229C2844D), "
                "some requests are failing with 'connection refused' when "
                "trying to reach a specific datanode, and the client "
                "subsequently avoids that node for the rest of the request. "
                "What is the root cause?"
            ),
        },
    ]

    for scenario in smoke_scenarios:
        diagnose(
            incident_id=scenario["incident_id"],
            question=scenario["question"],
            agent=agent,
            trace=trace,
            verbose=VERBOSE,
        )

    print()
    print("Smoke test complete. If both cases made tool calls and returned a")
    print("non-empty diagnosis, the agent is wired up correctly.")
