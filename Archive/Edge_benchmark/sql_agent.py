#!/usr/bin/env python3
"""
sql_agent.py

SQL agent for querying the PaaS log database.

This module defines the SQL sub-agent used by the diagnostic orchestrator.
It can also be run standalone for smoke-testing — see the __main__ block at
the bottom.

The agent uses langgraph's create_react_agent with native tool-calling
(NOT text-based ReAct prompting), routing through Ollama's /api/chat endpoint
via langchain_ollama.ChatOllama.

Two backends are supported:
  "qwen"   — for Qwen2.5 family models (recommended). Single-tool toolkit,
             clean SQL via native function calling.
  "ollama" — for older / non-tool-calling models. Adds a QuerySQLCheckerTool
             to catch malformed SQL before execution.

WHY ChatOllama and not ChatOpenAI -> /v1:
  Ollama's OpenAI-compatible /v1 endpoint does not reliably populate the
  `tool_calls` response field — Qwen's tool calls come back as raw JSON in
  message.content, langchain-openai never parses them, and the agent appears
  to never call tools. ChatOllama uses Ollama's native /api/chat endpoint and
  correctly surfaces tool_calls on AIMessage.

Public API:
  build_agent(...) -> (agent, system_prompt)
  query(question, agent, system_prompt, ...) -> dict

Dependencies:
    pip install langchain-community langchain-ollama langgraph sqlalchemy
"""

import json
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langgraph.prebuilt import create_react_agent
from langchain_community.utilities import SQLDatabase
from langchain_community.tools import QuerySQLDatabaseTool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


# ============================================================================
# DEFAULT CONFIG — overrideable as build_agent() parameters
#
# These are defaults for standalone smoke-testing. When called from the
# diagnostic_agent / run_benchmark, the orchestrator passes its own values
# from its own CONFIG block.
# ============================================================================

DB_URI         = "sqlite:///./data/benchmark_db.sqlite"
INCLUDE_TABLES = ["logs"]

LLM_MODEL    = "qwen2.5:latest"
LLM_BACKEND  = "qwen"           # "qwen" or "ollama"
LLM_TEMP     = 0.0
LLM_BASE_URL = "http://localhost:11434"   # bare host, no /v1
LLM_API_KEY  = "ollama"         # unused for ChatOllama; kept for backward-compat

MAX_ITERATIONS  = 12
MAX_ROWS        = 20
ERROR_THRESHOLD = 2

VERBOSE     = True
OUTPUT_FILE = "query_results_qwen.json"


# ============================================================================
# DEFAULT SCHEMA DESCRIPTION
#
# This describes the PaaS benchmark log database. Callers can override it
# via the schema_description parameter to build_agent() — the diagnostic
# orchestrator passes the same description from its own config so that
# both files agree on what the SQL agent should know.
# ============================================================================

DEFAULT_SCHEMA_DESCRIPTION = """
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
"""


# ============================================================================
# TIERED RECOVERY INSTRUCTIONS
#
# Injected when consecutive tool errors are detected.
# Escalates: fix syntax → simplify → give up.
# ============================================================================

RECOVERY_INSTRUCTIONS = {
    1: (
        "Your last query produced an error. "
        "Fix the specific issue: check column names against the schema, "
        "check for syntax errors, and try again."
    ),
    2: (
        "You have failed twice. Stop retrying the same approach. "
        "Simplify your query drastically — start with a basic SELECT to confirm "
        "the data exists, then add complexity only after that works."
    ),
    3: (
        "You have failed three times. Do not attempt another query. "
        "Respond immediately with a plain text answer explaining what you tried "
        "and that you were unable to complete the query."
    ),
}


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

def build_system_prompt(schema_description, max_rows):
    return f"""You are an expert SQL analyst diagnosing PaaS platform incidents.
Answer questions by querying the log database using the available tools.

{schema_description}

Always LIMIT results to {max_rows} rows unless the user explicitly asks for more.

Rules:
- Write raw SQL — no markdown backticks, no surrounding quotes.
- Never reference tables or columns not listed in the schema above.
- Answer only from query results — do not guess or hallucinate values.
- If results are empty or the question cannot be answered, say so clearly.
- Use column aliases (AS) for readable output."""


# ============================================================================
# AGENT BUILDER
# ============================================================================

def build_agent(
    db_uri=DB_URI,
    include_tables=INCLUDE_TABLES,
    llm_model=LLM_MODEL,
    backend=LLM_BACKEND,
    llm_temp=LLM_TEMP,
    llm_base_url=LLM_BASE_URL,
    schema_description=DEFAULT_SCHEMA_DESCRIPTION,
    max_rows=MAX_ROWS,
    max_iterations=MAX_ITERATIONS,
    verbose=VERBOSE,
):
    """
    Build and return (agent, system_prompt).

    Args:
        backend: "qwen"   — single sql_db_query tool, native tool calls.
                            Use this for any model with reliable native
                            function calling (Qwen2.5 family).
                 "ollama" — adds a SQL checker tool. Use for models without
                            reliable tool calling (older llamas, etc.) where
                            you want the checker as a safety net.

        schema_description: pass a custom schema string to describe a different
                            database; defaults to the PaaS schema for use with
                            the benchmark.
    """
    db = SQLDatabase.from_uri(
        db_uri,
        include_tables=include_tables,
        sample_rows_in_table_info=2,
    )

    # Strip /v1 if a caller passed an OpenAI-compatible URL — ChatOllama
    # wants the bare Ollama host
    ollama_base = llm_base_url.rstrip("/")
    if ollama_base.endswith("/v1"):
        ollama_base = ollama_base[:-3]

    system_prompt = build_system_prompt(
        schema_description=schema_description,
        max_rows=max_rows,
    )

    # langgraph >=1.x expects `prompt` to be a callable returning
    # list[BaseMessage], not a state dict (state_modifier was removed).
    def _state_mod(state):
        msgs = list(state.get("messages", []))
        if not msgs or not isinstance(msgs[0], SystemMessage):
            return [SystemMessage(content=system_prompt)] + msgs
        return msgs

    from langchain_ollama import ChatOllama
    llm = ChatOllama(model=llm_model, temperature=llm_temp, base_url=ollama_base)

    if backend == "qwen":
        tools = [QuerySQLDatabaseTool(db=db)]
    elif backend == "ollama":
        from langchain_community.tools.sql_database.tool import QuerySQLCheckerTool
        tools = [
            QuerySQLCheckerTool(db=db, llm=llm),
            QuerySQLDatabaseTool(db=db),
        ]
    else:
        raise ValueError(f"Unknown SQL backend: {backend!r}. Use 'qwen' or 'ollama'.")

    agent = create_react_agent(llm, tools, prompt=_state_mod)
    agent._max_iterations = max_iterations

    # Sanity check: verify tools were bound to the LLM. If this warns, the
    # agent will silently emit tool calls as raw JSON text in message.content
    # instead of routing them through langgraph's tool execution loop.
    try:
        bound_tools = getattr(llm.bind_tools(tools), "kwargs", {}).get("tools")
        if not bound_tools:
            print(f"  WARNING: SQL agent LLM ({llm_model}) has no bound tools — "
                  f"tool calls will not execute.")
        elif verbose:
            print(f"  Bound {len(bound_tools)} tool(s) to {llm_model}.")
    except Exception as e:
        print(f"  WARNING: could not verify tool binding: {e}")

    return agent, system_prompt


# ============================================================================
# QUERY PIPELINE  (used by smoke test only; the diagnostic orchestrator
# wraps the agent itself and doesn't go through this function)
# ============================================================================

def _extract_answer(result):
    """Pull the final text answer out of a langgraph result dict."""
    messages = result.get("messages", [])
    if messages:
        return messages[-1].content
    return str(result)


def _count_consecutive_errors(messages):
    """Count how many of the most recent ToolMessages contained errors."""
    count = 0
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            content = str(msg.content).lower()
            if any(w in content for w in ("error", "syntax", "operationalerror", "invalid")):
                count += 1
            else:
                break
    return count


def query(
    question,
    agent=None,
    system_prompt=None,
    error_threshold=ERROR_THRESHOLD,
    verbose=VERBOSE,
):
    """
    Run a single natural language question through the SQL agent.

    On first invocation, runs the question normally. If consecutive tool errors
    pile up, re-invokes with a tier-escalated recovery instruction.
    """
    if agent is None:
        agent, system_prompt = build_agent(verbose=verbose)

    max_iter = getattr(agent, "_max_iterations", MAX_ITERATIONS)

    if verbose:
        print("\n" + "=" * 70)
        print(f"QUESTION : {question}")
        print("=" * 70)

    answer        = None
    status        = "ok"
    consec_errors = 0

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={"recursion_limit": max_iter},
        )
        answer        = _extract_answer(result)
        consec_errors = _count_consecutive_errors(result.get("messages", []))

        if consec_errors >= error_threshold:
            recovery_level = min(consec_errors, max(RECOVERY_INSTRUCTIONS.keys()))
            recovery_note  = RECOVERY_INSTRUCTIONS[recovery_level]

            if verbose:
                print(f"\n  [Recovery L{recovery_level}] "
                      f"{consec_errors} consecutive errors — re-invoking...")

            recovery_input = f"{recovery_note}\n\nOriginal question: {question}"
            result  = agent.invoke(
                {"messages": [HumanMessage(content=recovery_input)]},
                config={"recursion_limit": max_iter},
            )
            answer = _extract_answer(result)
            status = f"recovered_L{recovery_level}"

    except Exception as e:
        answer = f"Agent error: {e}"
        status = "error"

    if verbose:
        print("\nANSWER:")
        print("-" * 70)
        print(answer)
        print("-" * 70)

    return {
        "question":           question,
        "answer":             answer,
        "status":             status,
        "consecutive_errors": consec_errors,
    }


def save_results(results, output_file=OUTPUT_FILE):
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved {len(results)} result(s) to {output_file}")


# ============================================================================
# SMOKE TEST  (NOT a benchmark — runs a few sample questions to verify
# the agent is wired up correctly)
#
# To run the actual 25-incident benchmark, use:  python run_benchmark.py
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("SQL AGENT SMOKE TEST")
    print("=" * 70)
    print("This is NOT the benchmark. It runs a handful of test questions to")
    print("verify the SQL agent is wired up correctly.")
    print("To run the benchmark: python run_benchmark.py")
    print("=" * 70)

    agent, system_prompt = build_agent()

    questions = [
        "What errors or warnings appear in the logs?",
        "Which blocks had the most events? Show the top 5.",
        "Are there any nodes generating a disproportionate number of errors?",
    ]

    all_results = []
    for q in questions:
        result = query(question=q, agent=agent, system_prompt=system_prompt)
        all_results.append(result)

    save_results(all_results)
