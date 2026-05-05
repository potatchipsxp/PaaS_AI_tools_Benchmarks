#!/usr/bin/env python3
"""
query_sql_agent_v2.py

Improved LangChain SQL agent with a reduced toolkit and richer upfront context.

Key differences from v1 (query_sql_agent.py):
  - Toolkit reduced to 2 tools: query_checker + query.
    The schema discovery tools (list_tables, db_schema) are removed because
    the full schema is provided upfront in the system prompt. Fewer tools =
    smaller action space = fewer ways for small models to go wrong.

  - Full schema with column descriptions is injected into the system prompt.
    The model never needs to ask "what tables exist?" or "what columns are there?"
    This directly addresses the failure mode in v1 where llama3.2 passed column
    names to sql_db_schema instead of table names.

  - Few-shot examples show the exact ReAct format the model must follow,
    including the critical rule that Action Input must be raw SQL (no backticks).

  - Tiered error recovery: after N consecutive errors the agent is re-invoked
    with an escalating instruction — fix syntax → simplify query → give up.
    This prevents the infinite loop behaviour seen in v1.

NOTE: v1 is kept intentionally as a benchmark baseline. It demonstrates how
      smaller models fail on the full 4-tool ReAct chain — useful comparative
      data showing that SOTA interaction patterns require sufficiently capable
      models to execute reliably.

Dependencies:
    pip install langchain-community langchain-ollama langgraph sqlalchemy
"""

import json
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langgraph.prebuilt import create_react_agent
from langchain_community.utilities import SQLDatabase
from langchain_community.tools import QuerySQLDatabaseTool
from langchain_community.tools.sql_database.tool import QuerySQLCheckerTool
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, ToolMessage


# ============================================================================
# CONFIG — edit these before running
# ============================================================================

# --- Database ---
DB_URI = "sqlite:///./data/benchmark_db.sqlite"
# Other examples:
#   DB_URI = "sqlite:///./events.db"
#   DB_URI = "postgresql://user:pass@localhost/mydb"
#   DB_URI = "mysql+pymysql://user:pass@localhost/mydb"

# Limit which tables the agent can query. Keeps the context focused.
INCLUDE_TABLES = ["logs"]
# INCLUDE_TABLES = ["events", "venues", "attendees"]  # example for events DB

# --- LLM ---
LLM_MODEL    = "llama3.2"  # swap to any model in: ollama list, to try: qwen2.5-coder:7b
LLM_TEMP     = 0.0                  # 0 = deterministic, best for SQL generation
LLM_BASE_URL = "http://localhost:11434"

# --- Agent behaviour ---
MAX_ITERATIONS  = 15    # max total ReAct steps per question (recursion limit)
MAX_ROWS        = 20    # agent is instructed to LIMIT results to this many rows

# After this many consecutive tool errors on one question, re-invoke the agent
# with an escalating recovery instruction instead of letting it loop.
ERROR_THRESHOLD = 2

# --- Output ---
VERBOSE     = True
OUTPUT_FILE = "query_results_v2.json"


# ============================================================================
# SCHEMA DESCRIPTION
#
# This is the most important thing to customise per project.
# By providing the full schema here we eliminate the need for list_tables
# and db_schema tools entirely — the model already has everything it needs.
#
# Include:
#   - exact table names (critical — model must not invent table names)
#   - every column with its type and plain-English meaning
#   - example values for columns with a constrained set of values
#   - row count so the model has realistic expectations
#   - foreign key relationships if multiple tables exist
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
# Example SCHEMA_DESCRIPTION for a user events database:
#
# SCHEMA_DESCRIPTION = \"\"\"
# DATABASE: User-created social events platform.
#
# TABLE: events  (N rows)
#   id          INTEGER  — unique event identifier
#   title       TEXT     — event name
#   description TEXT     — freeform description
#   event_date  TEXT     — ISO date e.g. '2024-06-15'
#   venue_id    INTEGER  — foreign key -> venues.id
#   creator_id  INTEGER  — foreign key -> users.id
#   capacity    INTEGER  — max attendees (NULL = unlimited)
#   category    TEXT     — 'party', 'concert', 'sports', 'meetup', etc.
#
# TABLE: venues  (N rows)
#   id          INTEGER  — unique venue identifier
#   name        TEXT     — venue name
#   city        TEXT     — city the venue is in
#   capacity    INTEGER  — maximum venue capacity
#
# TABLE: attendees  (N rows)
#   event_id    INTEGER  — foreign key -> events.id
#   user_id     INTEGER  — foreign key -> users.id
#   rsvp        TEXT     — 'yes', 'no', or 'maybe'
# \"\"\"
# ============================================================================


# ============================================================================
# FEW-SHOT EXAMPLES
#
# Demonstrates the exact format the model must follow for each tool call.
# Directly addresses the two failure modes observed in v1:
#   1. SQL wrapped in markdown backticks  → correct form shown explicitly
#   2. Column names passed to schema tool → tool removed, non-issue in v2
# ============================================================================

FEW_SHOT_EXAMPLES = """
--- EXAMPLE 1: Simple count ---
Question: How many log entries are there?
Thought: I need to count all rows in the logs table.
Action: sql_db_query_checker
Action Input: SELECT COUNT(*) FROM logs
Observation: The query looks correct.
Action: sql_db_query
Action Input: SELECT COUNT(*) FROM logs
Observation: [(10403,)]
Thought: I now know the final answer.
Final Answer: There are 10,403 log entries in the database.

--- EXAMPLE 2: Filtered aggregation ---
Question: How many ERROR-level events are there per component?
Thought: I need to group by component and filter for ERROR level.
Action: sql_db_query_checker
Action Input: SELECT component, COUNT(*) as error_count FROM logs WHERE level = 'ERROR' GROUP BY component ORDER BY error_count DESC LIMIT 20
Observation: The query looks correct.
Action: sql_db_query
Action Input: SELECT component, COUNT(*) as error_count FROM logs WHERE level = 'ERROR' GROUP BY component ORDER BY error_count DESC LIMIT 20
Observation: [('STORAGE_NODE', 42), ('NAME_NODE', 7)]
Thought: I now know the final answer.
Final Answer: STORAGE_NODE generated 42 ERROR-level events; NAME_NODE generated 7.

--- EXAMPLE 3: Multi-step query ---
Question: What are the most common event types?
Thought: I should group by event_type and count occurrences.
Action: sql_db_query_checker
Action Input: SELECT event_type, COUNT(*) as cnt FROM logs GROUP BY event_type ORDER BY cnt DESC LIMIT 20
Observation: The query looks correct.
Action: sql_db_query
Action Input: SELECT event_type, COUNT(*) as cnt FROM logs GROUP BY event_type ORDER BY cnt DESC LIMIT 20
Observation: [('data_transfer', 5200), ('heartbeat', 3100), ('error', 103)]
Thought: I now know the final answer.
Final Answer: The most common event types are data_transfer (5,200), heartbeat (3,100), and error (103).

--- CRITICAL FORMAT RULES ---
CORRECT:  Action Input: SELECT level, COUNT(*) FROM logs GROUP BY level
WRONG:    Action Input: ```SELECT level, COUNT(*) FROM logs GROUP BY level```
WRONG:    Action Input: "SELECT level, COUNT(*) FROM logs GROUP BY level"

Action Input must always be raw SQL with no backticks and no surrounding quotes.
"""


# ============================================================================
# TIERED RECOVERY INSTRUCTIONS
#
# Injected into the question when consecutive errors are detected.
# Escalates from "fix the syntax" → "simplify" → "give up cleanly".
# ============================================================================

RECOVERY_INSTRUCTIONS = {
    1: (
        "Your last query produced an error. "
        "Fix the specific syntax issue: check for backticks around the SQL, "
        "wrong column names, or invalid syntax. Then try again."
    ),
    2: (
        "You have failed twice. Stop retrying the same approach. "
        "Simplify your query: start with a basic SELECT to confirm the data "
        "exists, then build complexity only after that simpler version works."
    ),
    3: (
        "You have failed three times. Do not attempt another query. "
        "Respond immediately with: Final Answer: I was unable to answer this "
        "question after multiple failed attempts. Briefly describe what you tried."
    ),
}


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

def build_system_prompt(
    schema_description=SCHEMA_DESCRIPTION,
    few_shot_examples=FEW_SHOT_EXAMPLES,
    max_rows=MAX_ROWS,
):
    """
    Build the system prompt injected into the langgraph agent.
    langgraph's create_react_agent accepts a plain string for the system
    prompt, which is simpler than the PromptTemplate approach in v1.
    """
    return f"""You are an expert SQL analyst. Answer questions by querying a SQL database.

====== DATABASE SCHEMA ======
{schema_description}

====== YOUR TOOLS ======
You have exactly two tools:
  sql_db_query_checker — verify that a SQL query is correct before running it
  sql_db_query         — execute a SQL query and return results

Always run sql_db_query_checker before sql_db_query.

====== EXAMPLES OF CORRECT TOOL USE ======
{few_shot_examples}

====== RULES ======
- Always LIMIT queries to {max_rows} rows unless explicitly asked for more
- Never reference tables or columns not listed in the schema above
- Action Input must be raw SQL only — no backticks, no quotes around the SQL
- Answer only from query results — do not guess or hallucinate values
- If the data does not contain enough information to answer, say so clearly"""


# ============================================================================
# AGENT SETUP
# ============================================================================

def build_agent(
    db_uri=DB_URI,
    include_tables=INCLUDE_TABLES,
    llm_model=LLM_MODEL,
    llm_temp=LLM_TEMP,
    llm_base_url=LLM_BASE_URL,
    schema_description=SCHEMA_DESCRIPTION,
    few_shot_examples=FEW_SHOT_EXAMPLES,
    max_rows=MAX_ROWS,
    max_iterations=MAX_ITERATIONS,
    verbose=VERBOSE,
):
    """
    Build a langgraph ReAct agent with a 2-tool SQL toolkit.

    Tools provided:
      sql_db_query_checker — LLM-assisted query verification before execution
      sql_db_query         — raw SQL execution against the database

    Tools intentionally omitted vs v1:
      sql_db_list_tables   — redundant: table list is in the system prompt
      sql_db_schema        — redundant: full schema is in the system prompt.
                             Also removed because small models misuse it by
                             passing column names as if they were table names.

    Uses langgraph's create_react_agent, which is the correct stable API for
    this version of the langchain/langgraph ecosystem. The verbose flag enables
    the full step-by-step trace to print as the agent runs.
    """
    db = SQLDatabase.from_uri(
        db_uri,
        include_tables=include_tables,
        sample_rows_in_table_info=2,
    )

    llm = ChatOllama(
        model=llm_model,
        temperature=llm_temp,
        base_url=llm_base_url,
    )

    tools = [
        QuerySQLCheckerTool(db=db, llm=llm),
        QuerySQLDatabaseTool(db=db),
    ]

    system_prompt = build_system_prompt(
        schema_description=schema_description,
        few_shot_examples=few_shot_examples,
        max_rows=max_rows,
    )

    agent = create_react_agent(
        llm,
        tools,
        prompt=system_prompt,
    )

    # Store max_iterations on the agent object so query() can access it
    agent._max_iterations = max_iterations

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
    Count how many of the most recent tool responses contained errors.
    langgraph returns a flat list of messages; tool responses are ToolMessages.
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
    Run a single natural language question through the SQL agent.

    On first invocation, runs the question normally.
    If the result shows >= error_threshold consecutive tool errors, re-invokes
    with a recovery instruction prepended — escalating based on error count.

    Args:
        question        : natural language question
        agent           : pre-built langgraph agent (build once, reuse)
        system_prompt   : system prompt string (returned from build_agent)
        error_threshold : consecutive errors before triggering recovery
        verbose         : print question and answer headers

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

    # Build agent once — reused across all questions below
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
