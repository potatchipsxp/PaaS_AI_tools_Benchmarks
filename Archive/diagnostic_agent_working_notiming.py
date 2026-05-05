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
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

from query_doc_agent import query as doc_query
from build_doc_index import DB_PATH as DOC_DB_PATH, COLLECTION_NAME as DOC_COLLECTION


# ============================================================================
# CONFIG — change model names here to run different benchmark configurations
#
# To run a controlled experiment:
#   - Change one block only; leave the others untouched.
#   - The output filename encodes the model combo so results don't overwrite.
# ============================================================================

# --- Diagnostic agent (orchestrator) ---
DIAGNOSTIC_MODEL    = "qwen2.5:latest"
DIAGNOSTIC_BACKEND  = "qwen"       # "qwen" (ChatOpenAI/v1) or "ollama" (ChatOllama)
DIAGNOSTIC_TEMP     = 0.0
DIAGNOSTIC_BASE_URL = "http://localhost:11434/v1"   # only used for "qwen" backend
DIAGNOSTIC_API_KEY  = "ollama"                      # only used for "qwen" backend

# --- SQL agent ---
SQL_MODEL           = "qwen2.5:latest"
SQL_BACKEND         = "qwen"       # "qwen" (recommended) or "ollama"
SQL_TEMP            = 0.0
SQL_BASE_URL        = "http://localhost:11434/v1"
SQL_API_KEY         = "ollama"
SQL_DB_URI          = "sqlite:///./data/benchmark_db.sqlite"
SQL_MAX_ITER        = 12
SQL_MAX_ROWS        = 20

# --- Documentation agent ---
DOC_MODEL           = "llama3.2"
DOC_BACKEND         = "ollama"     # doc agent always uses ChatOllama (no tool calling needed)
DOC_BASE_URL        = "http://localhost:11434"
DOC_N_RESULTS       = 5            # docs to retrieve per query_docs call

# --- Orchestrator behaviour ---
MAX_TURNS           = 6
VERBOSE             = True

# Output filename encodes the model combo for easy result comparison.
# Override to a fixed name if you prefer.
OUTPUT_FILE = (
    f"diagnostic_results"
    f"__diag-{DIAGNOSTIC_MODEL.replace(':', '-').replace('.', '')}"
    f"__sql-{SQL_MODEL.replace(':', '-').replace('.', '')}"
    f"__doc-{DOC_MODEL.replace(':', '-').replace('.', '')}"
    f".json"
)


# ============================================================================
# SQL AGENT BUILDER
#
# Supports two backends so any Ollama model can be used as the SQL agent:
#   "qwen"   — ChatOpenAI pointed at /v1. Required for models that emit
#              native OpenAI function-calling (Qwen2.5, Mistral-Nemo, etc.)
#   "ollama" — ChatOllama with text-based ReAct. Works with llama3.2 and
#              other models that do not natively support function-calling.
#              Uses 2-tool toolkit (query_checker + query).
# ============================================================================

def _build_sql_agent(
    model=SQL_MODEL,
    backend=SQL_BACKEND,
    temp=SQL_TEMP,
    base_url=SQL_BASE_URL,
    api_key=SQL_API_KEY,
    db_uri=SQL_DB_URI,
    max_iter=SQL_MAX_ITER,
    max_rows=SQL_MAX_ROWS,
):
    """
    Build and return (agent, extract_fn) for the given SQL model config.

    Returns:
        agent       — callable via agent.invoke({"messages": [...]})
        extract_fn  — pulls the final text answer out of the result dict
    """
    from langchain_community.utilities import SQLDatabase
    from langchain_community.tools import QuerySQLDatabaseTool
    from langgraph.prebuilt import create_react_agent as _cra

    db = SQLDatabase.from_uri(db_uri, sample_rows_in_table_info=2)

    schema_description = f"""
DATABASE: PaaS platform logs (Cloud Foundry-compatible).

TABLE: logs
  row_uuid      TEXT  — unique row identifier
  timestamp     TEXT  — ISO-8601 datetime e.g. '2008-11-10T10:30:00Z'
  source_system TEXT  — always 'paas_platform'
  component     TEXT  — e.g. 'ROUTER', 'CELL', 'SCHEDULER', 'MESSAGE_BUS',
                        'APP', 'HEALTH', 'METRICS', 'CONTROLLER', 'AUTOSCALER',
                        'NETWORK', 'BUILD_SERVICE', 'BLOB_STORE', 'SERVICE_BROKER'
  subcomponent  TEXT  — Java class or process name within the component
  level         TEXT  — 'INFO', 'WARN', or 'ERROR'
  node_id       TEXT  — IP address or cell identifier e.g. 'cell-009'
  instance_id   TEXT  — container instance identifier (may be NULL)
  event_type    TEXT  — e.g. 'data_transfer', 'heartbeat', 'error', 'startup'
  message       TEXT  — raw log message text
  thread_id     INTEGER
  block_id      TEXT  — NULL for non-block events
  source_file   TEXT

ONLY ONE TABLE EXISTS: logs. Do not reference any other table name.
Always LIMIT results to {max_rows} rows unless asked for more.
"""

    system_prompt = f"""You are an expert SQL analyst diagnosing PaaS platform incidents.
Answer questions by querying the log database using the available tools.

{schema_description}

Rules:
- Write raw SQL — no markdown backticks, no surrounding quotes.
- Answer only from query results — do not guess or hallucinate.
- If results are empty or the question cannot be answered, say so clearly.
- Use column aliases (AS) for readable output."""

    # Use prompt= with a callable. In modern langgraph (>=0.2.x), the
    # `state_modifier` parameter has been removed; `prompt` is the replacement
    # and accepts str | SystemMessage | Callable. Passing a plain string can
    # fail to trigger bind_tools() in some intermediate versions, so we pass
    # a callable that returns a list[BaseMessage] — the contract for callable
    # prompts in current langgraph. This reliably triggers tool binding so
    # Qwen routes tool calls through langgraph instead of emitting them as
    # raw JSON in the assistant message.
    from langchain_core.messages import SystemMessage

    def _state_mod(state):
        msgs = list(state.get("messages", []))
        if not msgs or not isinstance(msgs[0], SystemMessage):
            return [SystemMessage(content=system_prompt)] + msgs
        return msgs

    if backend == "qwen":
        # Use langchain_ollama.ChatOllama (NOT ChatOpenAI -> /v1).
        # ChatOpenAI talks to Ollama's OpenAI-compatible /v1 endpoint, but that
        # endpoint does NOT reliably populate the OpenAI-format `tool_calls`
        # field in responses — Qwen's tool calls come back as raw JSON in
        # message.content, so langchain-openai never sees a tool call and
        # langgraph never executes one. (bind_tools succeeds in sending the
        # schemas to the model, but the return path is broken.)
        #
        # langchain_ollama.ChatOllama uses Ollama's native /api/chat endpoint
        # and correctly parses tool_calls into AIMessage.tool_calls, which is
        # what langgraph needs to route them through the tool execution loop.
        from langchain_ollama import ChatOllama as _ChatOllama
        # base_url for ChatOllama is the bare host (no /v1 suffix)
        ollama_base = base_url.rstrip("/")
        if ollama_base.endswith("/v1"):
            ollama_base = ollama_base[:-3]
        llm = _ChatOllama(model=model, temperature=temp, base_url=ollama_base)
        tools = [QuerySQLDatabaseTool(db=db)]
        agent = _cra(llm, tools, prompt=_state_mod)

    elif backend == "ollama":
        from langchain_ollama import ChatOllama
        from langchain_community.tools.sql_database.tool import QuerySQLCheckerTool

        llm = ChatOllama(model=model, temperature=temp, base_url=base_url)
        tools = [
            QuerySQLCheckerTool(db=db, llm=llm),
            QuerySQLDatabaseTool(db=db),
        ]
        agent = _cra(llm, tools, prompt=_state_mod)

    else:
        raise ValueError(f"Unknown SQL backend: {backend!r}. Use 'qwen' or 'ollama'.")

    # Sanity check: verify tools were actually bound to the LLM.
    # If this prints None/empty, the agent will silently emit tool calls as
    # JSON text instead of routing them through the tool execution loop.
    try:
        bound_tools = getattr(llm.bind_tools(tools), "kwargs", {}).get("tools")
        if not bound_tools:
            print(f"  WARNING: SQL agent LLM ({model}) has no bound tools — "
                  f"tool calls will not execute.")
    except Exception as e:
        print(f"  WARNING: could not verify tool binding for SQL agent: {e}")

    agent._max_iterations = max_iter

    def extract_fn(result):
        messages = result.get("messages", [])
        return messages[-1].content if messages else str(result)

    return agent, extract_fn


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
        # See note in _build_sql_agent: native ChatOllama, not ChatOpenAI->/v1.
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
    else:
        raise ValueError(
            f"Unknown diagnostic backend: {backend!r}. Use 'qwen' or 'ollama'."
        )


# ============================================================================
# TOOL CALL TRACE
# ============================================================================

def _make_trace():
    """Return a fresh (trace_list, record_fn) pair."""
    trace = []
    def record(tool_name, inputs, result):
        trace.append({"tool": tool_name, "inputs": inputs, "result": result})
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

    sql_agent, sql_extract = _build_sql_agent(
        model=sql_model,
        backend=sql_backend,
        temp=sql_temp,
        base_url=sql_base_url,
        api_key=sql_api_key,
        db_uri=sql_db_uri,
        max_iter=sql_max_iter,
    )

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
        result = sql_agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={"recursion_limit": sql_max_iter * 3},
        )
        answer = sql_extract(result)
        record("query_logs", {"question": question}, answer)
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
        result = doc_query(
            question=question,
            n_results=doc_n_results,
            llm_model=doc_model,
            db_path=doc_db_path,
            collection_name=doc_collection,
            verbose=False,
        )
        record("query_docs", {"question": question}, {
            "answer":         result["answer"],
            "retrieved_docs": result["retrieved_docs"],
        })
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
        doc_n_results=doc_n_results,
        doc_db_path=doc_db_path,
        doc_collection=doc_collection,
    )

    # See the note in _build_sql_agent: pass prompt= as a callable that
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

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={"recursion_limit": max_iter * 3},
        )
        messages  = result.get("messages", [])
        diagnosis = messages[-1].content if messages else str(result)

    except Exception as e:
        diagnosis = f"Agent error: {e}"
        status    = "error"

    if verbose:
        print(f"\n{'─' * 70}")
        print(f"TOOL CALLS: {len(trace)}")
        for i, call in enumerate(trace, 1):
            q = call["inputs"].get("question", "")[:60]
            print(f"  {i}. {call['tool']}({q!r})")
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
        # Model config recorded in each result for self-contained eval reports
        # Read from module-level constants — diagnose() is config-agnostic
        "model_config": {
            "diagnostic_model":   DIAGNOSTIC_MODEL,
            "diagnostic_backend": DIAGNOSTIC_BACKEND,
            "sql_model":          SQL_MODEL,
            "sql_backend":        SQL_BACKEND,
            "doc_model":          DOC_MODEL,
        },
    }


def save_results(results, output_file=OUTPUT_FILE):
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved {len(results)} result(s) to {output_file}")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":

    print(f"Diagnostic model : {DIAGNOSTIC_MODEL} ({DIAGNOSTIC_BACKEND})")
    print(f"SQL model        : {SQL_MODEL} ({SQL_BACKEND})")
    print(f"Doc model        : {DOC_MODEL} ({DOC_BACKEND})")
    print(f"Output file      : {OUTPUT_FILE}")
    print()

    # Build once — reused across all scenarios in this run.
    agent, tools, trace = build_diagnostic_agent()

    # Sample scenarios.
    # To run all 25, replace with:
    from benchmark_incidents import BENCHMARK_CASES
    scenarios = [{"incident_id": c["incident_id"], "question": c["question"]}
                for c in BENCHMARK_CASES]
    # scenarios = [
    #     {
    #         "incident_id": "INC-008",
    #         "question": (
    #             "payments-api is returning sustained 503s. Metrics show the "
    #             "database connection pool climbing. An autoscaler scale-out fired "
    #             "but the 503s continued even after new instances started. "
    #             "What is the root cause?"
    #         ),
    #     },
    #     {
    #         "incident_id": "INC-016",
    #         "question": (
    #             "Multiple platform components appear to be failing simultaneously: "
    #             "the routing table is stale, cell heartbeats are missing, and Diego "
    #             "is evacuating cells. But the cells themselves show no errors in their "
    #             "own logs. What is causing this?"
    #         ),
    #     },
    #     {
    #         "incident_id": "INC-025",
    #         "question": (
    #             "At a specific time, all inter-service communication on the platform "
    #             "failed simultaneously with TLS errors. Six hours earlier there were "
    #             "CERT WARNING messages in the logs. What happened?"
    #         ),
    #     },
    # ]

    all_results = []
    for scenario in scenarios:
        result = diagnose(
            incident_id=scenario["incident_id"],
            question=scenario["question"],
            agent=agent,
            trace=trace,
            verbose=VERBOSE,
        )
        all_results.append(result)

    save_results(all_results)
