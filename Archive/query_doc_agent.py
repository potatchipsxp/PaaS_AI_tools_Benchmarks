#!/usr/bin/env python3
"""
query_doc_agent.py

Documentation RAG agent — answers questions about platform behaviour
by retrieving from the synthesised doc corpus (doc_corpus.jsonl indexed
by build_doc_index.py).

This agent is one of two sub-agents called by diagnostic_agent.py.
It can also be run standalone for testing.

Edit the CONFIG section, then run:
    python query_doc_agent.py

Dependencies:
    pip install chromadb sentence-transformers ollama
"""

import json
import ollama

from build_doc_index import retrieve_docs, DB_PATH, COLLECTION_NAME, EMBEDDING_MODEL


# ============================================================================
# CONFIG
# ============================================================================

LLM_MODEL  = "llama3.2"   # swap to qwen2.5-coder:7b etc. as desired
N_RESULTS  = 5             # docs to retrieve per query
VERBOSE    = True
OUTPUT_FILE = "doc_query_results.json"


# ============================================================================
# GENERATION
# ============================================================================

def build_doc_prompt(question, docs):
    """
    Build the LLM prompt from retrieved documentation chunks.
    Each doc is presented with its type and title so the model knows
    whether it is reading a runbook, an error reference, or a config note.
    """
    context_parts = []
    for i, doc in enumerate(docs, 1):
        header = (
            f"[Doc {i}] "
            f"type={doc['doc_type']}  "
            f"pattern={doc['failure_pattern']}  "
            f"tier={doc['tier']}"
        )
        context_parts.append(f"{header}\n### {doc['title']}\n{doc['content']}")

    context = "\n\n---\n\n".join(context_parts)

    return f"""You are a platform reliability engineer with access to internal documentation.

The following documentation sections were retrieved as most relevant to the question.
Each section is labelled with its type (runbook, error_ref, config, or architecture).

{context}

Question: {question}

Answer clearly and specifically based only on the documentation above.
If the documentation does not contain enough information to answer, say so explicitly.
When citing specific values (thresholds, timeouts, command syntax), quote them exactly."""


def generate_doc_answer(prompt, llm_model=LLM_MODEL):
    response = ollama.generate(model=llm_model, prompt=prompt)
    return response["response"]


# ============================================================================
# FULL QUERY PIPELINE
# ============================================================================

def query(
    question,
    n_results=N_RESULTS,
    llm_model=LLM_MODEL,
    where_filter=None,
    db_path=DB_PATH,
    collection_name=COLLECTION_NAME,
    embedding_model=EMBEDDING_MODEL,
    verbose=VERBOSE,
):
    """
    Run the full documentation RAG pipeline for a single question.

    Args:
        where_filter: optional ChromaDB metadata filter, e.g.
                      {"doc_type": "runbook"}
                      {"failure_pattern": "connection_pool_exhaustion"}

    Returns:
        dict with:
          question          : the input question
          answer            : LLM-generated answer
          retrieved_docs    : list of retrieved doc metadata (for eval)
          llm_model         : model used
          n_results         : number of docs retrieved
    """
    if verbose:
        print("\n" + "=" * 70)
        print(f"QUESTION : {question}")
        if where_filter:
            print(f"Filter   : {where_filter}")
        print("=" * 70)

    docs = retrieve_docs(
        question=question,
        n_results=n_results,
        where_filter=where_filter,
        db_path=db_path,
        collection_name=collection_name,
        embedding_model=embedding_model,
    )

    if verbose:
        print(f"\nStep 1: Retrieved {len(docs)} docs")
        for i, doc in enumerate(docs, 1):
            dist = f"{doc['distance']:.4f}" if doc["distance"] is not None else "?"
            print(f"  {i:2}. [{doc['doc_type']:12}] {doc['title'][:50]}  "
                  f"dist={dist}  incidents={doc['incident_ids']}")
        print("\nStep 2: Generating answer...")

    prompt = build_doc_prompt(question, docs)
    answer = generate_doc_answer(prompt, llm_model)

    if verbose:
        print("\nANSWER:")
        print("-" * 70)
        print(answer)
        print("-" * 70)

    return {
        "question":    question,
        "answer":      answer,
        "llm_model":   llm_model,
        "n_results":   len(docs),
        "where_filter": where_filter,
        "retrieved_docs": [
            {
                "doc_id":          d["doc_id"],
                "doc_type":        d["doc_type"],
                "title":           d["title"],
                "incident_ids":    d["incident_ids"],
                "failure_pattern": d["failure_pattern"],
                "tier":            d["tier"],
                "distance":        d["distance"],
            }
            for d in docs
        ],
    }


def save_results(results, output_file=OUTPUT_FILE):
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved {len(results)} result(s) to {output_file}")


# ============================================================================
# MAIN — standalone test queries
# ============================================================================

if __name__ == "__main__":

    test_queries = [
        {
            "question": "What does POOL EXHAUSTED mean and what should I do when I see it?",
            "where_filter": None,
        },
        {
            "question": "How do I investigate a situation where all instances on a cell crash simultaneously?",
            "where_filter": None,
        },
        {
            "question": "What is the Gorouter cache TTL and why does it cause 502s after a blue-green deploy?",
            "where_filter": None,
        },
        {
            "question": "What steps should I follow when NATS message rate spikes far above normal?",
            "where_filter": {"doc_type": "runbook"},
        },
        {
            "question": "What configuration value must be larger than instance startup time?",
            "where_filter": {"doc_type": "config"},
        },
    ]

    all_results = []
    for q in test_queries:
        result = query(
            question=q["question"],
            where_filter=q.get("where_filter"),
            verbose=VERBOSE,
        )
        all_results.append(result)

    save_results(all_results)
