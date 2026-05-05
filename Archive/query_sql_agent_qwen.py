#!/usr/bin/env python3
"""
query_sql_agent_qwen.py

Qwen-optimized SQL agent using native function/tool calling.

Key differences from query_sql_agent_simple.py (ReAct/Ollama version):
  - Uses ChatOpenAI pointed at Ollama's /v1 endpoint instead of ChatOllama.
    This is the critical fix: ChatOllama does not correctly route Qwen's native
    tool calls through langgraph's execution loop — it returns them as raw JSON
    text in the final message. ChatOpenAI speaks the OpenAI function-calling
    wire protocol natively, which is what Qwen2.5 actually emits, so tool calls
    are intercepted and executed correctly.

  - QuerySQLCheckerTool is dropped from the toolkit. With native tool calling,
    Qwen's structured output produces clean SQL and the checker adds no value.

  - No few-shot ReAct examples needed. System prompt focuses on schema and
    rules only — no format coaching required.

  - Error recovery logic preserved but tuned for Qwen's lower error rate.

Dependencies:
    pip install langchain-community langchain-openai langgraph sqlalchemy

NOTE on ChatOpenAI vs ChatOllama:
    Qwen2.5's native tool calling uses the OpenAI function-calling wire protocol.
    ChatOllama does NOT correctly handle this — it returns the tool call as raw
    JSON text in the final message instead of routing it through langgraph's tool
    execution loop. ChatOpenAI pointed at Ollama's /v1 endpoint uses the correct
    protocol and Qwen executes tools properly.
"""

import json
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langgraph.prebuilt import create_react_agent
from langchain_community.utilities import SQLDatabase
from langchain_community.tools import QuerySQLDatabaseTool
from langchain_core.messages import HumanMessage, ToolMessage


# ============================================================================
# CONFIG — edit these before running
# ============================================================================

# --- Database ---
DB_URI = "sqlite:///./data/benchmark_db.sqlite"

# Limit which tables the agent can query. Keeps context focused.
INCLUDE_TABLES = ["logs"]

# --- LLM ---
# Qwen2.5-coder is recommended — strong SQL + reliable tool calling.
# Other good options: "qwen2.5:7b", "qwen2.5:14b", "qwen2.5-coder:14b"
LLM_MODEL    = "qwen2.5:latest"
LLM_TEMP     = 0.0
# Ollama's OpenAI-compatible endpoint — required for correct tool call routing
LLM_BASE_URL = "http://localhost:11434/v1"
# ChatOpenAI requires an api_key param even for local Ollama; any string works
LLM_API_KEY  = "ollama"

# --- Agent behaviour ---
MAX_ITERATIONS = 12   # Qwen is efficient — rarely needs many steps
MAX_ROWS       = 20

# Consecutive tool errors before triggering recovery re-invocation
ERROR_THRESHOLD = 2

# --- Output ---
VERBOSE     = True
OUTPUT_FILE = "query_results_qwen.json"


# ============================================================================
# SCHEMA DESCRIPTION
#
# Provide full schema upfront so the model never needs to discover it.
# Include exact table/column names, types, example values, and row count.
# ============================================================================

SCHEMA_DESCRIPTION = """
DATABASE: HDFS distributed filesystem logs from a PaaS platform.

TABLE: logs  (10,403 rows)
  row_uuid      TEXT     — unique identifier for each log entry
  timestamp     TEXT     — ISO-8601 datetime e.g. '2008-11-11T10:57:42Z'
  source_system TEXT     — always 'paas_platform' in this dataset
  component     TEXT     — HDFS component e.g. 'STORAGE_NODE', 'NAME_NODE', 'DATA_NODE'
  subcomponent  TEXT     — Java class within the component
  level         TEXT     — log severity: exactly 'INFO', 'WARN', or 'ERROR'
  node_id       TEXT     — IP address of the node e.g. '10.250.14.196'
  instance_id   TEXT     — instance identifier (may be NULL)
  event_type    TEXT     — semantic category e.g. 'data_transfer', 'block_report',
                           'heartbeat', 'error', 'connection_error'
  message       TEXT     — raw log message text
  thread_id     INTEGER  — OS thread that produced the log
  block_id      TEXT     — HDFS block ID e.g. 'blk_-4493569113005607099'
                           NULL when the event is not block-specific
  source_file   TEXT     — original log file this record came from

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
#
# Qwen-optimized: no ReAct format coaching needed.
# Focus on schema, task rules, and output expectations.
# ============================================================================

def build_system_prompt(
    schema_description=SCHEMA_DESCRIPTION,
    max_rows=MAX_ROWS,
):
    return f"""You are an expert SQL analyst. Answer questions by querying a SQLite database using the available tools.

====== DATABASE SCHEMA ======
{schema_description}

====== TOOLS ======
You have one tool:
  sql_db_query — execute a SQL query and return results

Use it to run SELECT queries. Never attempt INSERT, UPDATE, DELETE, or DROP.

====== RULES ======
- Always LIMIT queries to {max_rows} rows unless the user explicitly asks for more
- Never reference tables or columns not listed in the schema above
- Write raw SQL — no markdown backticks, no quotes wrapping the SQL string
- Answer only from query results — do not guess or hallucinate values
- If results are empty or the question cannot be answered from the data, say so clearly
- Prefer readable output: use column aliases (AS) to make result columns self-explanatory"""


# ============================================================================
# AGENT SETUP
# ============================================================================

def build_agent(
    db_uri=DB_URI,
    include_tables=INCLUDE_TABLES,
    llm_model=LLM_MODEL,
    llm_temp=LLM_TEMP,
    llm_base_url=LLM_BASE_URL,
    llm_api_key=LLM_API_KEY,
    schema_description=SCHEMA_DESCRIPTION,
    max_rows=MAX_ROWS,
    max_iterations=MAX_ITERATIONS,
    verbose=VERBOSE,
):
    """
    Build a langgraph agent using Qwen's native tool-calling via ChatOpenAI.

    WHY ChatOpenAI instead of ChatOllama:
      Qwen2.5 uses the OpenAI function-calling wire protocol for tool use.
      ChatOllama intercepts this and returns the tool call as raw JSON text
      in the assistant message — langgraph never sees it as a tool invocation,
      so the tool never executes and the JSON becomes the "final answer".
      ChatOpenAI pointed at Ollama's /v1 endpoint speaks the same protocol
      natively and the tool call round-trip works correctly.

    Tool: sql_db_query only.
      sql_db_query_checker is omitted — Qwen's structured tool calls produce
      clean SQL, making the checker redundant.
    """
    from langchain_openai import ChatOpenAI

    db = SQLDatabase.from_uri(
        db_uri,
        include_tables=include_tables,
        sample_rows_in_table_info=2,
    )

    llm = ChatOpenAI(
        model=llm_model,
        temperature=llm_temp,
        base_url=llm_base_url,
        api_key=llm_api_key,
    )

    tools = [
        QuerySQLDatabaseTool(db=db),
    ]

    system_prompt = build_system_prompt(
        schema_description=schema_description,
        max_rows=max_rows,
    )

    def _state_mod(state):
        from langchain_core.messages import SystemMessage
        msgs = list(state.get("messages", []))
        if not msgs or not isinstance(msgs[0], SystemMessage):
            return [SystemMessage(content=system_prompt)] + msgs
        return msgs

    agent = create_react_agent(
        llm,
        tools,
        prompt=_state_mod,
    )

    agent._max_iterations = max_iterations

    # Sanity check: verify Qwen actually received tool schemas via bind_tools().
    # If this prints empty, the agent will emit tool calls as raw JSON text
    # in the assistant message instead of executing them.
    try:
        bound_tools = getattr(llm.bind_tools(tools), "kwargs", {}).get("tools")
        if not bound_tools:
            print(f"  WARNING: LLM ({llm_model}) has no bound tools — "
                  f"tool calls will not execute.")
        elif verbose:
            print(f"  Bound {len(bound_tools)} tool(s) to {llm_model}.")
    except Exception as e:
        print(f"  WARNING: could not verify tool binding: {e}")

    return agent, system_prompt


# ============================================================================
# QUERY PIPELINE
# ============================================================================

def _extract_answer(result):
    """Pull the final text answer out of a langgraph result dict."""
    messages = result.get("messages", [])
    if messages:
        return messages[-1].content
    return str(result)


def _count_consecutive_errors(messages):
    """
    Count how many of the most recent ToolMessages contained errors.
    Walks backwards through the flat message list and stops at the first
    clean tool response.
    """
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
    Run a single natural language question through the Qwen SQL agent.

    On first invocation, runs the question normally.
    If the result shows >= error_threshold consecutive tool errors, re-invokes
    with a recovery instruction — escalating based on error count.

    Args:
        question        : natural language question string
        agent           : pre-built langgraph agent (build once, reuse)
        system_prompt   : system prompt string (returned from build_agent)
        error_threshold : consecutive errors before triggering recovery
        verbose         : print question/answer trace to stdout

    Returns:
        dict with question, answer, status, and consecutive_errors count
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

        # Tiered recovery: re-invoke with escalating instruction if errors pile up
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
# MAIN
# ============================================================================

if __name__ == "__main__":

    # Build agent once — reused across all questions
    agent, system_prompt = build_agent()

    # ------------------------------------------------------------------
    # Edit your questions here
    # ------------------------------------------------------------------
    questions = [
        "What errors or warnings appear in the logs?",
        "Which blocks had the most events? Show the top 5.",
        "Are there any nodes generating a disproportionate number of errors?",
    ]
    # ------------------------------------------------------------------

    all_results = []
    for q in questions:
        result = query(question=q, agent=agent, system_prompt=system_prompt)
        all_results.append(result)

    save_results(all_results)
