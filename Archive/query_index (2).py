#!/usr/bin/env python3
"""
query_index.py

Query an existing ChromaDB index built by build_index.py.

Edit the CONFIG section to match build_index.py settings, then run:
    python query_index.py

Results are printed and saved to query_results.json.
"""

import json
import chromadb
import ollama
from sentence_transformers import SentenceTransformer


# ============================================================================
# CONFIG — must match the settings used in build_index.py
# ============================================================================

DB_PATH           = "./chroma_db"
BASE_COLLECTION   = "logs"
COLLECTION_SUFFIX = ""

EMBEDDING_MODEL   = "all-MiniLM-L6-v2"
CHUNK_METHOD      = "record"    # "record", "window", or "block"
CHUNK_SIZE        = 1           # only relevant if CHUNK_METHOD = "window"
CHUNK_OVERLAP     = 0           # only relevant if CHUNK_METHOD = "window"

LLM_MODEL         = "llama3.2"  # Ollama model for answer generation
N_RESULTS         = 10          # number of chunks to retrieve per query
VERBOSE           = True


# ============================================================================
# COLLECTION NAME  (must stay in sync with build_index.py)
# ============================================================================

def make_collection_name(base, embedding_model, chunk_method, chunk_size,
                         chunk_overlap, suffix=""):
    model_short = embedding_model.split("/")[-1].replace("-", "_")
    parts = [base, model_short, chunk_method]
    if chunk_method == "window":
        parts += [f"w{chunk_size}", f"o{chunk_overlap}"]
    if suffix:
        parts.append(suffix)
    return "_".join(parts)


# ============================================================================
# CHROMA HELPERS
# ============================================================================

_chroma_client = None

def get_chroma_client(db_path=DB_PATH):
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=db_path)
    return _chroma_client


def get_collection(collection_name):
    client = get_chroma_client()
    try:
        return client.get_collection(name=collection_name)
    except Exception:
        raise RuntimeError(
            f"Collection '{collection_name}' not found. "
            "Run build_index.py with matching config first."
        )


def list_collections(db_path=DB_PATH):
    client = get_chroma_client(db_path)
    return [{"name": c.name, "count": c.count()} for c in client.list_collections()]


# ============================================================================
# EMBEDDING MODEL CACHE
# ============================================================================

_model_cache = {}

def get_embedding_model(model_name):
    if model_name not in _model_cache:
        print(f"  Loading embedding model: {model_name}...")
        _model_cache[model_name] = SentenceTransformer(model_name)
    return _model_cache[model_name]


# ============================================================================
# RETRIEVAL
# ============================================================================

def retrieve_chunks(question, collection, embedding_model_name,
                    n_results=N_RESULTS, where_filter=None):
    """
    Embed the question and return the closest chunks from the collection.

    Args:
        where_filter: optional ChromaDB metadata filter dict, e.g.
                      {"level": "WARN"} or {"component": "STORAGE_NODE"}
    """
    model = get_embedding_model(embedding_model_name)
    query_embedding = model.encode([question])[0]

    kwargs = dict(
        query_embeddings=[query_embedding.tolist()],
        n_results=n_results,
    )
    if where_filter:
        kwargs["where"] = where_filter

    results = collection.query(**kwargs)

    chunks = []
    for i in range(len(results["ids"][0])):
        chunks.append({
            "id":       results["ids"][0][i],
            "text":     results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i] if "distances" in results else None,
        })
    return chunks


# ============================================================================
# GENERATION
# ============================================================================

def build_prompt(question, chunks):
    """
    Build the LLM prompt from retrieved log chunks.
    Each chunk includes its metadata summary so the model knows
    the level, component, timestamp, and block context.
    """
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        meta = chunk["metadata"]
        header = (
            f"[Chunk {i}] "
            f"ts={meta.get('timestamp','')}  "
            f"level={meta.get('level','')}  "
            f"component={meta.get('component','')}  "
            f"event={meta.get('event_type','')}  "
            f"block={meta.get('block_id','')}"
        )
        if meta.get("label"):
            header += f"  label={meta['label']}"
        context_parts.append(f"{header}\n{chunk['text']}")

    context = "\n\n---\n\n".join(context_parts)

    return f"""You are a system reliability engineer analyzing HDFS log data.

The following log chunks were retrieved as most relevant to the question.
Each chunk includes timestamp, log level, component, event type, and block ID.

{context}

Question: {question}

Provide a clear, specific answer based only on the log data above.
If the logs do not contain enough information to answer, say so."""


def generate_answer(prompt, llm_model=LLM_MODEL):
    response = ollama.generate(model=llm_model, prompt=prompt)
    return response["response"]


# ============================================================================
# FULL QUERY PIPELINE
# ============================================================================

def query(question, collection_name, embedding_model=EMBEDDING_MODEL,
          n_results=N_RESULTS, llm_model=LLM_MODEL,
          where_filter=None, verbose=VERBOSE):
    """
    Run the full RAG pipeline for a single question.

    Args:
        where_filter: optional dict to pre-filter by metadata before
                      vector search, e.g. {"level": "WARN"}

    Returns:
        dict with question, answer, sources, and config metadata
    """
    if verbose:
        print("\n" + "=" * 70)
        print(f"QUESTION : {question}")
        print(f"Collection: {collection_name}")
        if where_filter:
            print(f"Filter   : {where_filter}")
        print("=" * 70)

    collection = get_collection(collection_name)
    if collection.count() == 0:
        raise RuntimeError(f"Collection '{collection_name}' is empty.")

    if verbose:
        print(f"\nStep 1: Retrieving {n_results} chunks...")

    chunks = retrieve_chunks(question, collection, embedding_model,
                             n_results, where_filter)

    if verbose:
        for i, chunk in enumerate(chunks, 1):
            meta     = chunk["metadata"]
            distance = chunk.get("distance") or 0
            label    = f"  label={meta['label']}" if meta.get("label") else ""
            print(f"  {i:2}. [{meta.get('level','?'):4}] "
                  f"{meta.get('component','?'):20} "
                  f"event={meta.get('event_type','?'):15} "
                  f"dist={distance:.4f}{label}")
        print("\nStep 2: Generating answer...")

    prompt = build_prompt(question, chunks)
    answer = generate_answer(prompt, llm_model)

    if verbose:
        print("\nANSWER:")
        print("-" * 70)
        print(answer)
        print("-" * 70)

    return {
        "question":             question,
        "answer":               answer,
        "collection_name":      collection_name,
        "embedding_model":      embedding_model,
        "llm_model":            llm_model,
        "where_filter":         where_filter,
        "num_chunks_retrieved": len(chunks),
        "sources": [
            {
                "timestamp":  c["metadata"].get("timestamp"),
                "level":      c["metadata"].get("level"),
                "component":  c["metadata"].get("component"),
                "event_type": c["metadata"].get("event_type"),
                "block_id":   c["metadata"].get("block_id"),
                "label":      c["metadata"].get("label"),
                "distance":   c.get("distance"),
            }
            for c in chunks
        ],
    }


def save_results(results, output_file="query_results.json"):
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved {len(results)} result(s) to {output_file}")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    collection_name = make_collection_name(
        BASE_COLLECTION, EMBEDDING_MODEL, CHUNK_METHOD,
        CHUNK_SIZE, CHUNK_OVERLAP, COLLECTION_SUFFIX
    )

    # ------------------------------------------------------------------
    # Edit your questions here.
    # Optionally add a where_filter to narrow retrieval by metadata.
    # ------------------------------------------------------------------
    queries = [
        {
            "question": "What errors or warnings appear in the logs?",
            "where_filter": None,
        },
        {
            "question": "Which blocks had transfer or serving failures?",
            "where_filter": {"level": "WARN"},
        },
        {
            "question": "Describe the typical lifecycle of a block in this dataset.",
            "where_filter": None,
        },
    ]
    # ------------------------------------------------------------------

    all_results = []
    for q in queries:
        result = query(
            question        = q["question"],
            collection_name = collection_name,
            embedding_model = EMBEDDING_MODEL,
            n_results       = N_RESULTS,
            llm_model       = LLM_MODEL,
            where_filter    = q.get("where_filter"),
            verbose         = VERBOSE,
        )
        all_results.append(result)

    save_results(all_results)
