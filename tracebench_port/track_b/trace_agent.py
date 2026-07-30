#!/usr/bin/env python3
"""
trace_agent.py

Trace sub-agent for Track B -- the LLM-mediated retrieval layer, structurally
parallel to sql_agent.py (Track A's SQL sub-agent). See CHANGELOG_PORT.md for
the full discussion; the short version:

Track B's defining principle was never "zero LLM assistance anywhere in
retrieval" -- it's "work with the native trace data format instead of
converting it to SQL." The original all-deterministic query_trace(trace_id)
tool conflated those two things. This agent keeps the first (native format
only -- its one tool still returns exactly query_trace.py's unmodified,
fully deterministic per-host timeline + call-structure reconstruction; that
Python logic has not changed at all) while relaxing the second: an LLM layer
now sits around that deterministic core and does two things a real SQL
sub-agent already does for Track A --

  1. Extract the instance_id from a natural-language question. This is the
     same category of interpretive work sql_agent.py does when it turns a
     vague question into a targeted SQL query -- Track A's orchestrator
     never has to produce an exact literal argument either.
  2. Read the full deterministic reconstruction and answer the actual
     question using only the relevant evidence, instead of handing the
     entire raw dump back to the orchestrator.

Why this matters empirically (this session's forced-injection probe,
CHANGELOG_PORT.md): handed the full raw trace directly, the Edge tier
(qwen2.5:latest) engaged with it reasonably well in 2 of 3 cases, but missed
the one diagnostically decisive line in the third (a verbatim
"Connection refused" exception, buried in an 11K-character undifferentiated
per-host dump) because nothing in that dump signals which lines matter.
Track A's SQL agent never faces this problem -- a WHERE clause already
scopes the result before the orchestrator sees it. This agent restores that
same scoping step for Track B, without ever touching SQL, cross-trace
queries, or fault labels -- it only ever sees one trace at a time, in its
native shape.

Public API:
  build_agent(...) -> (agent, system_prompt)
  query(question, agent, system_prompt, ...) -> dict

Dependencies:
    pip install langchain-community langchain-ollama langgraph
"""

import json
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langgraph.prebuilt import create_react_agent
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from query_trace import query_trace as _query_trace_impl


# ============================================================================
# DEFAULT CONFIG -- overrideable as build_agent() parameters
#
# These are defaults for standalone smoke-testing. When called from
# track_b/diagnostic_agent.py, the orchestrator passes its own values from
# its own CONFIG block (TRACE_MODEL / TRACE_BACKEND / etc).
# ============================================================================

LLM_MODEL    = "qwen2.5:latest"
LLM_BACKEND  = "qwen"           # "qwen" or "ollama"
LLM_TEMP     = 0.0
LLM_BASE_URL = "http://localhost:11434"   # bare host, no /v1
LLM_API_KEY  = "ollama"         # unused for ChatOllama; kept for backward-compat

MAX_ITERATIONS  = 8
ERROR_THRESHOLD = 2

VERBOSE     = True
OUTPUT_FILE = "trace_agent_results.json"


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = """You are an expert HDFS trace analyst. You have ONE tool:

  get_trace(instance_id) -- returns the full per-host timeline and
  cross-host call structure for one HDFS request/instance, exactly as
  recorded (operation names, durations, host names, exception text, and
  any latency-outlier [WARN] annotations).

Your job: given a question about a specific instance, extract the
instance_id (it is always present in the question, e.g. "instance
C4113E01C484F2EB"), call get_trace with that exact string, then answer the
question using ONLY evidence from the returned trace.

Rules:
- Never guess or invent trace evidence -- always call get_trace first.
- Quote the SPECIFIC evidence that answers the question: host name(s),
  operation name(s), duration figures, or exact exception text. Do not just
  describe the trace in general terms -- name the number, the host, the
  exact error message.
- If the trace shows a [WARN] or [ERROR] annotation anywhere, that is a
  strong signal -- address it directly and explicitly. Do not let it get
  lost among unrelated timeline detail.
- Times shown for different hosts are NOT directly comparable to each other
  (independent per-host clocks) -- only compare same-host timing.
- If get_trace returns "No trace found" or "No events found", you likely
  mis-extracted the instance_id -- re-read the question and try again with
  the exact string.
- Keep your answer focused and evidence-grounded: a few sentences citing
  specific facts from the trace, not a full re-narration of the whole
  timeline."""


# ============================================================================
# TIERED RECOVERY INSTRUCTIONS -- same escalation pattern as sql_agent.py
# ============================================================================

RECOVERY_INSTRUCTIONS = {
    1: (
        "Your last call to get_trace failed or found nothing. Re-read the "
        "question and check you copied the instance_id exactly as it "
        "appears there, then try again."
    ),
    2: (
        "You have failed twice. Stop retrying blindly. Quote the exact "
        "substring from the question that you believe is the instance_id, "
        "then call get_trace with exactly that string."
    ),
    3: (
        "You have failed three times. Do not call get_trace again. Respond "
        "with a plain text answer explaining that the instance_id could not "
        "be resolved."
    ),
}


def build_system_prompt():
    return SYSTEM_PROMPT


# ============================================================================
# AGENT BUILDER
# ============================================================================

def build_agent(
    llm_model=LLM_MODEL,
    backend=LLM_BACKEND,
    llm_temp=LLM_TEMP,
    llm_base_url=LLM_BASE_URL,
    max_iterations=MAX_ITERATIONS,
    verbose=VERBOSE,
):
    """
    Build and return (agent, system_prompt).

    Args:
        backend: "qwen"      -- local Qwen2.5 family via ChatOllama (native
                                 /api/chat, not the /v1 endpoint -- see
                                 sql_agent.py's WHY note; same rationale
                                 applies here).
                 "ollama"     -- other local models via ChatOllama.
                 "groq" / "deepinfra" / "openai" -- API backends via
                                 ChatOpenAI, all of which correctly surface
                                 tool_calls.
    """
    system_prompt = build_system_prompt()

    def _state_mod(state):
        msgs = list(state.get("messages", []))
        if not msgs or not isinstance(msgs[0], SystemMessage):
            return [SystemMessage(content=system_prompt)] + msgs
        return msgs

    @tool
    def get_trace(instance_id: str) -> str:
        """
        Reconstruct the full HDFS request trace for a specific instance_id:
        a per-host timeline of operations (with durations and any real
        exception text) plus the cross-host call structure, annotated with
        latency-outlier [WARN] flags where relevant.

        Input: the exact instance_id string (e.g. "C4113E01C484F2EB").
        Output: the formatted trace, or a "No trace found" /
                "No events found" message if the ID doesn't match anything.
        """
        return _query_trace_impl(instance_id)

    if backend in ("groq", "deepinfra", "openai"):
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
        kwargs = {"model": llm_model, "temperature": llm_temp, "api_key": api_key}
        if api_base is not None:
            kwargs["base_url"] = api_base
        llm = ChatOpenAI(**kwargs)
    else:
        from langchain_ollama import ChatOllama
        ollama_base = llm_base_url.rstrip("/")
        if ollama_base.endswith("/v1"):
            ollama_base = ollama_base[:-3]
        llm = ChatOllama(model=llm_model, temperature=llm_temp, base_url=ollama_base)

    tools = [get_trace]
    agent = create_react_agent(llm, tools, prompt=_state_mod)
    agent._max_iterations = max_iterations

    try:
        bound_tools = getattr(llm.bind_tools(tools), "kwargs", {}).get("tools")
        if not bound_tools:
            print(f"  WARNING: trace agent LLM ({llm_model}) has no bound tools — "
                  f"tool calls will not execute.")
        elif verbose:
            print(f"  Bound {len(bound_tools)} tool(s) to {llm_model}.")
    except Exception as e:
        print(f"  WARNING: could not verify tool binding: {e}")

    return agent, system_prompt


# ============================================================================
# QUERY PIPELINE  (used by smoke test and by diagnostic_agent.py's
# query_trace tool closure)
# ============================================================================

def _extract_answer(result):
    """Pull the final text answer out of a langgraph result dict."""
    messages = result.get("messages", [])
    if messages:
        return messages[-1].content
    return str(result)


def _count_consecutive_errors(messages):
    """Count how many of the most recent ToolMessages indicated failure."""
    count = 0
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            content = str(msg.content).lower()
            if any(w in content for w in ("no trace found", "no events found", "error")):
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
    Run a single natural language question through the trace agent.

    On first invocation, runs the question normally. If consecutive tool
    failures pile up (bad instance_id extraction), re-invokes with a
    tier-escalated recovery instruction -- same pattern as sql_agent.query().
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
            config={"recursion_limit": max_iter * 3},
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
                config={"recursion_limit": max_iter * 3},
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
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("TRACE AGENT SMOKE TEST")
    print("=" * 70)
    print("This is NOT the benchmark. It runs a handful of test questions to")
    print("verify the trace agent is wired up correctly.")
    print("=" * 70)

    agent, system_prompt = build_agent()

    questions = [
        (
            "During a read HDFS workload (instance C4113E01C484F2EB), one "
            "datanode is measurably slower than its peers. What is the "
            "root cause?"
        ),
        (
            "During a read-only HDFS workload (instance 03FB08C229C2844D), "
            "some requests are failing with 'connection refused'. What is "
            "the root cause?"
        ),
    ]

    all_results = []
    for q in questions:
        result = query(question=q, agent=agent, system_prompt=system_prompt)
        all_results.append(result)

    save_results(all_results)
