#!/usr/bin/env python3
"""
diagnostic_agent.py

Orchestrating diagnostic agent for the PaaS benchmark.

Given an incident scenario (a natural-language question describing symptoms),
this agent reasons over the problem and iteratively calls two sub-agents:

  sql_agent  : queries benchmark_db.sqlite for log evidence
  doc_agent  : retrieves relevant documentation from doc_chroma_db

## Model configuration

All three agents have independent model settings in the CONFIG section below.
To run a controlled experiment varying only one agent's model, change only
that agent's block — the other two are unaffected.

  DIAGNOSTIC_MODEL  — the orchestrating agent (high-level reasoning)
  SQL_MODEL         — the SQL agent (query generation and execution)
  DOC_MODEL         — the documentation agent (RAG answer synthesis)

The SQL agent supports two backends:
  "qwen"   — ChatOpenAI → Ollama /v1 endpoint, native function calling (recommended)
  "ollama" — ChatOllama, text-based ReAct (fallback for non-Qwen models)

Edit the CONFIG section, then run:
    python diagnostic_agent.py

Dependencies:
    pip install langchain-community langchain-openai langgraph sqlalchemy
    pip install chromadb sentence-transformers ollama
"""

import json
import time
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

from doc_agent import query as doc_query
from sql_agent import build_agent as build_sql_agent
from build_doc_index import DB_PATH as DOC_DB_PATH, COLLECTION_NAME as DOC_COLLECTION


# ============================================================================
# RATE LIMIT HANDLING
#
# Groq's free tier has a 12K tokens-per-minute limit. A single incident's
# tool-calling chain can approach or exceed that. We wrap agent.invoke() in
# a retry loop that catches 429s, sleeps for the duration the API requests,
# and tracks the slept time so timing metrics can subtract it.
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
# ============================================================================

# --- Diagnostic agent (orchestrator) ---
DIAGNOSTIC_MODEL    = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
DIAGNOSTIC_BACKEND  = "deepinfra"   # "qwen", "ollama", "groq", or "deepinfra"
DIAGNOSTIC_TEMP     = 0.0
DIAGNOSTIC_BASE_URL = "http://localhost:11434/v1"   # ignored for groq/deepinfra
DIAGNOSTIC_API_KEY  = "ollama"                      # ignored for groq/deepinfra

# --- SQL agent ---
SQL_MODEL           = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
SQL_BACKEND         = "deepinfra"   # "qwen", "ollama", "groq", or "deepinfra"
SQL_TEMP            = 0.0
SQL_BASE_URL        = "http://localhost:11434/v1"   # ignored for groq/deepinfra
SQL_API_KEY         = "ollama"                      # ignored for groq/deepinfra
SQL_DB_URI          = "sqlite:///./data/benchmark_db.sqlite"
SQL_MAX_ITER        = 12
SQL_MAX_ROWS        = 20

# --- Documentation agent ---
DOC_MODEL           = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
DOC_BACKEND         = "deepinfra"   # "ollama", "groq", or "deepinfra"
DOC_BASE_URL        = "http://localhost:11434"
DOC_N_RESULTS       = 5

# --- Orchestrator behaviour ---
MAX_TURNS           = 6
VERBOSE             = True

# Output filename encodes the model combo for easy result comparison.
# Strip any chars that would break filenames or look like path separators
# (model IDs like "meta-llama/Llama-3.3-70B-Instruct-Turbo" contain '/').
def _sanitize_model_name(name):
    for ch in ("/", "\\", ":", ".", " "):
        name = name.replace(ch, "-")
    return name

OUTPUT_FILE = (
    f"diagnostic_results"
    f"__diag-{_sanitize_model_name(DIAGNOSTIC_MODEL)}"
    f"__sql-{_sanitize_model_name(SQL_MODEL)}"
    f"__doc-{_sanitize_model_name(DOC_MODEL)}"
    f".json"
)

# ============================================================================
# SQL AGENT IMPORT
#
# The SQL agent is defined in sql_agent.py. We import its build_agent function
# here (aliased as build_sql_agent at the top of this file). The orchestrator
# only needs the agent object plus a tiny helper to pull the final text answer
# out of a langgraph result dict.
# ============================================================================

def _sql_extract(result):
    """Pull the final text answer out of an SQL agent invocation result."""
    messages = result.get("messages", [])
    return messages[-1].content if messages else str(result)




# ============================================================================
# DIAGNOSTIC AGENT LLM BUILDER
# ============================================================================

def _build_diagnostic_llm(
    model=DIAGNOSTIC_MODEL,
    backend=DIAGNOSTIC_BACKEND,
    temp=DIAGNOSTIC_TEMP,
    base_url=DIAGNOSTIC_BASE_URL,
    api_key=DIAGNOSTIC_API_KEY,
):
    if backend == "qwen":
        # Same rationale as in sql_agent.py: native ChatOllama, not ChatOpenAI->/v1.
        # The /v1 endpoint does not return tool_calls in the structured field,
        # so the orchestrator never sees its sub-agents being called.
        from langchain_ollama import ChatOllama as _ChatOllama
        ollama_base = base_url.rstrip("/")
        if ollama_base.endswith("/v1"):
            ollama_base = ollama_base[:-3]
        return _ChatOllama(model=model, temperature=temp, base_url=ollama_base)
    elif backend == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, temperature=temp, base_url=base_url)
    elif backend in ("groq", "deepinfra"):
        # OpenAI-compatible API backends. Both correctly surface tool_calls,
        # so ChatOpenAI works. They differ only in base_url and env var name.
        import os
        from langchain_openai import ChatOpenAI

        if backend == "groq":
            api_base = "https://api.groq.com/openai/v1"
            env_var  = "GROQ_API_KEY"
            example  = "gsk_..."
        else:  # deepinfra
            api_base = "https://api.deepinfra.com/v1/openai"
            env_var  = "DEEPINFRA_API_KEY"
            example  = "your-deepinfra-key"

        api_key = os.environ.get(env_var)
        if not api_key:
            raise RuntimeError(
                f"{env_var} not set in environment. "
                f"Set it before running: $env:{env_var} = '{example}'"
            )
        return ChatOpenAI(
            model=model,
            temperature=temp,
            base_url=api_base,
            api_key=api_key,
        )
    else:
        raise ValueError(
            f"Unknown diagnostic backend: {backend!r}. "
            f"Use 'qwen', 'ollama', 'groq', or 'deepinfra'."
        )


# ============================================================================
# TOOL CALL TRACE
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
#
# Tools are constructed as closures that capture specific sub-agent instances.
# build_tools() can be called multiple times with different configs — each
# call produces a fully isolated pair of tools with no shared state.
# ============================================================================

def build_tools(
    sql_model=SQL_MODEL,
    sql_backend=SQL_BACKEND,
    sql_temp=SQL_TEMP,
    sql_base_url=SQL_BASE_URL,
    sql_api_key=SQL_API_KEY,
    sql_db_uri=SQL_DB_URI,
    sql_max_iter=SQL_MAX_ITER,
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

    # Build the SQL sub-agent. The build_sql_agent function is sql_agent.build_agent
    # (imported at the top of this file). It returns (agent, system_prompt) but we
    # only need the agent — the system prompt is already embedded in it. We pair
    # it with the local _sql_extract helper for pulling the final answer text.
    sql_agent, _ = build_sql_agent(
        llm_model=sql_model,
        backend=sql_backend,
        llm_temp=sql_temp,
        llm_base_url=sql_base_url,
        db_uri=sql_db_uri,
        max_iterations=sql_max_iter,
        verbose=False,
    )
    sql_extract = _sql_extract

    @tool
    def query_logs(question: str) -> str:
        """
        Query the platform log database (SQLite) to find evidence about a specific
        incident. Use this tool when you need to:
          - Find error messages, warning patterns, or sequences of events in logs
          - Count occurrences of a specific event or component
          - Identify timestamps, node IDs, or instance IDs involved in an incident
          - Determine the order of events (what happened first vs. what followed)

        Input: a natural language question about the logs.
        Output: a text answer derived from SQL queries over the log database.

        Examples:
          "What ERROR-level events appear in the ROUTER component?"
          "What is the sequence of events for the MESSAGE_BUS component?"
          "Which node_id had the most ERROR-level entries?"
        """
        t0 = time.perf_counter()
        result, sql_sleep = invoke_with_rate_limit_retry(
            sql_agent,
            {"messages": [HumanMessage(content=question)]},
            config={"recursion_limit": sql_max_iter * 3},
            verbose=False,
        )
        answer = sql_extract(result)
        # Subtract rate-limit sleep from tool duration
        duration_ms = (time.perf_counter() - t0 - sql_sleep) * 1000
        record("query_logs", {"question": question}, answer, duration_ms)
        return answer

    @tool
    def query_docs(question: str) -> str:
        """
        Search the platform documentation corpus for operational knowledge.
        Use this tool when you need to:
          - Understand what a specific error message or log pattern means
          - Find the investigation steps for a known failure pattern
          - Look up configuration thresholds, normal baselines, or alert values
          - Understand how two platform components interact or depend on each other

        Input: a natural language question about platform behaviour or operations.
        Output: an answer synthesised from retrieved runbooks, error references,
                config notes, and architecture documentation.

        Examples:
          "What does POOL EXHAUSTED mean and what causes it?"
          "What is the runbook for simultaneous instance crashes on a cell?"
          "What configuration value must exceed instance startup time?"
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

    return [query_logs, query_docs], trace


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = """You are an expert platform reliability engineer diagnosing
incidents on a Cloud Foundry-compatible PaaS platform.

You have two tools:
  query_logs  — search the live log database for evidence of what happened
  query_docs  — search platform documentation for operational knowledge

## Diagnostic approach

1. Read the incident description carefully. Identify the symptoms and the
   affected component(s).
2. Use query_logs to find the specific log evidence for this incident.
   Start with the most distinctive symptom (error messages, specific component).
3. Use query_docs to understand what the log evidence means and what the
   root cause category is.
4. If the first round of evidence is ambiguous, do a second round:
   query_logs for more specific evidence, then query_docs for confirmation.
5. Synthesise a final diagnosis that states:
   - The root cause (specific and technical, not vague)
   - The key log evidence that supports the diagnosis
   - The failure pattern (e.g. connection pool exhaustion, cell OOM, cert expiry)
   - The recommended fix

## Rules
- Never guess the root cause without log evidence.
- Always use query_logs before concluding — your diagnosis must be grounded in
  the actual log data, not just documentation knowledge.
- Be specific: name the component, the error message, the threshold value.
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
    sql_model=SQL_MODEL,
    sql_backend=SQL_BACKEND,
    sql_temp=SQL_TEMP,
    sql_base_url=SQL_BASE_URL,
    sql_api_key=SQL_API_KEY,
    sql_db_uri=SQL_DB_URI,
    sql_max_iter=SQL_MAX_ITER,
    doc_model=DOC_MODEL,
    doc_backend=DOC_BACKEND,
    doc_n_results=DOC_N_RESULTS,
    doc_db_path=DOC_DB_PATH,
    doc_collection=DOC_COLLECTION,
    max_turns=MAX_TURNS,
):
    """
    Build and return (agent, tools, trace) for one benchmark configuration.

    All three model configs are explicit parameters — pass different values
    to produce differently-configured agents for comparison runs.

    Returns:
        agent  — langgraph agent ready for .invoke()
        tools  — [query_logs, query_docs] closures bound to this config
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
        sql_model=sql_model,
        sql_backend=sql_backend,
        sql_temp=sql_temp,
        sql_base_url=sql_base_url,
        sql_api_key=sql_api_key,
        sql_db_uri=sql_db_uri,
        sql_max_iter=sql_max_iter,
        doc_model=doc_model,
        doc_backend=doc_backend,
        doc_n_results=doc_n_results,
        doc_db_path=doc_db_path,
        doc_collection=doc_collection,
    )

    # Same rationale as in sql_agent.py: pass prompt= as a callable that
    # returns list[BaseMessage]. Modern langgraph removed state_modifier;
    # a callable prompt is the cross-version-safe form that reliably
    # triggers bind_tools() on the LLM.
    from langchain_core.messages import SystemMessage

    def _state_mod(state):
        msgs = list(state.get("messages", []))
        if not msgs or not isinstance(msgs[0], SystemMessage):
            return [SystemMessage(content=SYSTEM_PROMPT)] + msgs
        return msgs

    agent = create_react_agent(llm, tools, prompt=_state_mod)
    agent._max_iterations = max_turns

    # Sanity check tool binding on the orchestrator LLM.
    try:
        bound_tools = getattr(llm.bind_tools(tools), "kwargs", {}).get("tools")
        if not bound_tools:
            print(f"  WARNING: diagnostic LLM ({diagnostic_model}) has no bound "
                  f"tools — orchestrator will not execute tool calls.")
        else:
            print(f"  Diagnostic LLM bound {len(bound_tools)} tool(s) successfully.")
    except Exception as e:
        print(f"  WARNING: could not verify tool binding for diagnostic agent: {e}")

    # Warm-up call: some hosted backends (e.g. DeepInfra) emit a malformed
    # response on the very first invocation after the model is loaded —
    # tool calls come back as raw "<function=name {...}>" text instead of
    # being routed through the tool_calls field. Subsequent calls are fine.
    # A throwaway invocation here guarantees the real benchmark loop starts
    # from a warm, correctly-routed state. The trace is cleared after so
    # it doesn't contaminate the first real incident's metrics.
    if diagnostic_backend in ("groq", "deepinfra"):
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
# DIAGNOSE
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

    Args:
        incident_id : e.g. "INC-008" — label for evaluation
        question    : operator-style symptom description
        agent       : built by build_diagnostic_agent()
        trace       : the trace list returned by build_diagnostic_agent();
                      cleared and repopulated in-place on each call
        verbose     : print progress to stdout

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
            # Clear trace before each attempt so retry timing is clean
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

            # Detect the malformed-tool-call failure: diagnosis text leaks the
            # legacy Llama "<function=name {...}>" format AND no tool calls
            # actually executed (trace empty). When that happens, the model's
            # tool call wasn't routed through tool_calls and the agent gave up
            # after one round-trip. Retry from scratch.
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
            # Retries exhausted, still malformed — mark as a recorded failure
            status = "malformed_tool_call_unrecovered"

    except Exception as e:
        diagnosis = f"Agent error: {e}"
        status    = "error"
    total_seconds = time.perf_counter() - t_start
    # Subtract rate-limit sleep so timing reflects actual reasoning, not waiting
    active_seconds = max(0.0, total_seconds - rate_limit_sleep)

    # Timing breakdown: separate tool time (SQL + doc agents) from orchestrator
    # time (the diagnostic LLM's own reasoning between tool calls). Tool time
    # is the sum of per-call durations from the trace; orchestrator time is
    # whatever wall-clock is left over after tools finish — using active_seconds
    # so any rate-limit sleeps don't get charged to the orchestrator.
    tool_ms_total = sum(c.get("duration_ms", 0) for c in trace)
    sql_ms        = sum(c.get("duration_ms", 0) for c in trace if c["tool"] == "query_logs")
    doc_ms        = sum(c.get("duration_ms", 0) for c in trace if c["tool"] == "query_docs")
    orchestrator_ms = max(0.0, active_seconds * 1000 - tool_ms_total)

    timing = {
        "total_seconds":        round(total_seconds, 3),
        "active_seconds":       round(active_seconds, 3),
        "rate_limit_sleep_s":   round(rate_limit_sleep, 3),
        "tool_ms_total":        round(tool_ms_total, 1),
        "sql_ms_total":         round(sql_ms, 1),
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
            q  = call["inputs"].get("question", "")[:60]
            dt = call.get("duration_ms", 0)
            print(f"  {i}. {call['tool']}({q!r})  [{dt/1000:.1f}s]")
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
        # Model config recorded in each result for self-contained eval reports
        # Read from module-level constants — diagnose() is config-agnostic
        "model_config": {
            "diagnostic_model":   DIAGNOSTIC_MODEL,
            "diagnostic_backend": DIAGNOSTIC_BACKEND,
            "sql_model":          SQL_MODEL,
            "sql_backend":        SQL_BACKEND,
            "doc_model":          DOC_MODEL,
            "doc_backend":        DOC_BACKEND,
        },
    }


def save_results(results, output_file):
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved {len(results)} result(s) to {output_file}")


# ============================================================================
# SMOKE TEST  (NOT a benchmark — runs 2 incidents to verify the orchestrator
# is wired up and that both sub-agents are reachable.)
#
# To run the actual 25-incident benchmark, use:  python run_benchmark.py
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("DIAGNOSTIC AGENT SMOKE TEST")
    print("=" * 70)
    print("This is NOT the benchmark. It runs 2 sample incidents to verify the")
    print("orchestrator and both sub-agents are wired up correctly.")
    print("To run the full 25-incident benchmark: python run_benchmark.py")
    print("=" * 70)
    print()
    print(f"Diagnostic model : {DIAGNOSTIC_MODEL} ({DIAGNOSTIC_BACKEND})")
    print(f"SQL model        : {SQL_MODEL} ({SQL_BACKEND})")
    print(f"Doc model        : {DOC_MODEL} ({DOC_BACKEND})")
    print()

    agent, tools, trace = build_diagnostic_agent()

    smoke_scenarios = [
        {
            "incident_id": "SMOKE-001",
            "question": (
                "payments-api is returning sustained 503s. Metrics show the "
                "database connection pool climbing. An autoscaler scale-out fired "
                "but the 503s continued even after new instances started. "
                "What is the root cause?"
            ),
        },
        {
            "incident_id": "SMOKE-002",
            "question": (
                "All inter-service communication on the platform failed "
                "simultaneously with TLS handshake errors. Six hours earlier "
                "there were CERT WARNING messages in the logs. What happened?"
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
    print("Smoke test complete. If both incidents made tool calls and returned a")
    print("non-empty diagnosis, the agent is wired up correctly.")
