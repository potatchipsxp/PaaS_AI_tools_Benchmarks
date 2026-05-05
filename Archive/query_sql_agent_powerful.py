#!/usr/bin/env python3
"""
query_sql_agent.py

Query a SQL database using a LangChain ReAct SQL agent.

The agent explores the database schema at query time — no pre-built index needed.
It iteratively calls tools (list tables, inspect schema, check query, run query)
until it can answer the question, then synthesizes a natural language response.

This script is intentionally database-agnostic. To use it for a different
project (events, products, whatever), just update the CONFIG and SCHEMA_DESCRIPTION
sections — the agent machinery stays the same.

Edit the CONFIG section, then run:
    python query_sql_agent.py

Dependencies:
    pip install langchain langchain-community langchain-ollama sqlalchemy
"""

import json
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.agent_toolkits.sql.base import create_sql_agent
from langchain_ollama import ChatOllama


# ============================================================================
# CONFIG — edit these before running
# ============================================================================

# --- Database ---
DB_URI = "sqlite:///./data/benchmark_db.sqlite"
# Examples for other databases:
#   DB_URI = "sqlite:///./events.db"
#   DB_URI = "postgresql://user:pass@localhost/mydb"
#   DB_URI = "mysql+pymysql://user:pass@localhost/mydb"

# Which tables to expose to the agent. None = all tables.
# Limiting this to relevant tables improves accuracy and reduces token usage.
INCLUDE_TABLES = ["logs"]
# INCLUDE_TABLES = ["events", "venues", "attendees"]   # example for events DB

# --- LLM ---
LLM_MODEL    = "llama3.2"   # any model available in your Ollama instance
LLM_TEMP     = 0.0          # 0 = deterministic, better for SQL generation
LLM_BASE_URL = "http://localhost:11434"

# --- Agent behaviour ---
MAX_ITERATIONS   = 10       # max ReAct loop steps before giving up
VERBOSE          = True     # True = prints the full ReAct thought/action trace
MAX_ROWS         = 20       # agent is instructed to LIMIT results to this many rows

# --- Output ---
OUTPUT_FILE = "query_results.json"


# ============================================================================
# SCHEMA DESCRIPTION
# Provide a plain-English description of your database for the agent.
# This is the single most important thing to customise per project.
# Good descriptions mention:
#   - what each table contains
#   - the meaning of key columns (especially non-obvious ones)
#   - any domain-specific terminology the LLM might not infer from column names
# ============================================================================

SCHEMA_DESCRIPTION = """
This database contains HDFS distributed filesystem logs from a PaaS platform.

Table: logs
  - row_uuid     : unique identifier for each log entry
  - timestamp    : ISO-8601 datetime (e.g. 2008-11-11T10:57:42Z)
  - source_system: always 'paas_platform' in this dataset
  - component    : the HDFS component that produced the log
                   (e.g. STORAGE_NODE, NAME_NODE, DATA_NODE)
  - subcomponent : the specific Java class within the component
  - level        : log severity — INFO, WARN, or ERROR
  - node_id      : IP address of the node that produced the log
  - instance_id  : instance identifier (may be null)
  - event_type   : semantic category of the event
                   (e.g. data_transfer, block_report, heartbeat, error)
  - message      : the raw log message text
  - thread_id    : OS thread that produced the log
  - block_id     : HDFS block identifier (e.g. blk_-4493569113005607099)
                   Present when the event relates to a specific data block.
  - source_file  : original log file this record came from
"""

# ============================================================================
# Example SCHEMA_DESCRIPTION for a user events database:
#
# SCHEMA_DESCRIPTION = """
# This database contains user-created social events.
#
# Table: events
#   - id          : unique event identifier
#   - title       : event name
#   - description : freeform event description
#   - event_date  : ISO date of the event
#   - venue_id    : foreign key into the venues table
#   - creator_id  : foreign key into the users table
#   - capacity    : maximum number of attendees (null = unlimited)
#   - category    : type of event (party, concert, sports, etc.)
#
# Table: venues
#   - id       : unique venue identifier
#   - name     : venue name
#   - city     : city the venue is in
#   - capacity : maximum capacity of the venue
#
# Table: attendees
#   - event_id : foreign key into events
#   - user_id  : foreign key into users
#   - rsvp     : 'yes', 'no', or 'maybe'
# """
# ============================================================================


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
    max_rows=MAX_ROWS,
    max_iterations=MAX_ITERATIONS,
    verbose=VERBOSE,
):
    """
    Build and return a LangChain SQL agent.

    The agent receives a toolkit of four tools:
      - sql_db_list_tables   : discover what tables exist
      - sql_db_schema        : inspect columns + sample rows for specific tables
      - sql_db_query_checker : verify a query before running it
      - sql_db_query         : execute SQL and return results

    It uses these in a ReAct loop: Thought → Action → Observation → repeat.
    """
    db = SQLDatabase.from_uri(
        db_uri,
        include_tables=include_tables,
        sample_rows_in_table_info=3,   # how many sample rows to show the agent per table
    )

    llm = ChatOllama(
        model=llm_model,
        temperature=llm_temp,
        base_url=llm_base_url,
    )

    toolkit = SQLDatabaseToolkit(db=db, llm=llm)

    # System prompt: gives the agent domain context + behavioural guardrails
    system_prefix = f"""You are an expert data analyst with access to a SQL database.

DATABASE CONTEXT:
{schema_description}

RULES:
- Always call sql_db_list_tables first if you are unsure which tables exist.
- Always use sql_db_query_checker to verify your query before executing it.
- Always add LIMIT {max_rows} to queries unless the user explicitly asks for more.
- If a query returns an error, rewrite it and try again — do not give up.
- Answer only from the data returned by queries. Do not guess or hallucinate.
- If the data does not contain enough information to answer, say so clearly.
- Be concise and specific in your final answer."""

    agent = create_sql_agent(
        llm=llm,
        toolkit=toolkit,
        agent_type="zero-shot-react-description",
        prefix=system_prefix,
        verbose=verbose,
        max_iterations=max_iterations,
        handle_parsing_errors=True,
    )

    return agent


# ============================================================================
# QUERY PIPELINE
# ============================================================================

def query(
    question,
    agent=None,
    verbose=VERBOSE,
):
    """
    Run a single natural language question through the SQL agent.

    Args:
        question : the natural language question to ask
        agent    : pre-built agent (built once and reused across queries)
        verbose  : if True, prints the full ReAct trace

    Returns:
        dict with question, answer, and status
    """
    if agent is None:
        agent = build_agent(verbose=verbose)

    if verbose:
        print("\n" + "=" * 70)
        print(f"QUESTION : {question}")
        print("=" * 70)

    try:
        result = agent.invoke({"input": question})
        answer = result.get("output", str(result))
        status = "ok"
    except Exception as e:
        answer = f"Agent error: {e}"
        status = "error"

    if verbose:
        print("\nANSWER:")
        print("-" * 70)
        print(answer)
        print("-" * 70)

    return {
        "question": question,
        "answer":   answer,
        "status":   status,
    }


def save_results(results, output_file=OUTPUT_FILE):
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved {len(results)} result(s) to {output_file}")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":

    # Build the agent once — reused across all queries below
    agent = build_agent()

    # ------------------------------------------------------------------
    # Edit your questions here.
    # ------------------------------------------------------------------
    questions = [
        "What errors or warnings appear in the logs?",
        "Which blocks had the most events? Show the top 5.",
        "Are there any nodes that appear to be generating a disproportionate number of errors?",
    ]
    # ------------------------------------------------------------------

    all_results = []
    for q in questions:
        result = query(question=q, agent=agent)
        all_results.append(result)

    save_results(all_results)
