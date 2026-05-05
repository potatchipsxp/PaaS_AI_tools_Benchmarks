#!/usr/bin/env python3
"""
query_index.py

Query an existing ChromaDB index built by build_index.py.
Edit the CONFIG section to match what you used when building, then run:

    python query_index.py

The collection name is derived from your config settings, so they must
match what was used during indexing to hit the right collection.
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
CHUNK_METHOD      = "character"
CHUNK_SIZE        = 500
CHUNK_OVERLAP     = 50

LLM_MODEL         = "llama3.2"      # Ollama model for answer generation
N_RESULTS         = 5               # number of chunks to retrieve per query
VERBOSE           = True


# ============================================================================
# COLLECTION NAME HELPER  (same logic as build_index.py)
# ============================================================================

def make_collection_name(base, embedding_model, chunk_size, chunk_overlap,
                         chunk_method, suffix=""):
    model_short = embedding_model.split("/")[-1].replace("-", "_")
    parts = [base, model_short, f"chunk{chunk_size}",
             f"overlap{chunk_overlap}", chunk_method]
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
            "Run build_index.py with the matching config first."
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

def retrieve_chunks(question, collection, embedding_model_name, n_results=N_RESULTS):
    """Embed the question and find the closest chunks in the collection."""
    model = get_embedding_model(embedding_model_name)
    query_embedding = model.encode([question])[0]

    results = collection.query(
        query_embeddings=[query_embedding.tolist()],
        n_results=n_results,
    )

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
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        source = chunk["metadata"].get("filename", "unknown")
        context_parts.append(f"[Source {i}: {source}]\n{chunk['text']}")
    context = "\n\n---\n\n".join(context_parts)

    return f"""You are a helpful assistant answering questions about system log data.

Context information:
{context}

Question: {question}

Provide a clear, accurate answer based only on the context above."""


def generate_answer(prompt, llm_model=LLM_MODEL):
    response = ollama.generate(model=llm_model, prompt=prompt)
    return response["response"]


# ============================================================================
# FULL QUERY PIPELINE
# ============================================================================

def query(question, collection_name, embedding_model=EMBEDDING_MODEL,
          n_results=N_RESULTS, llm_model=LLM_MODEL, verbose=VERBOSE):
    """
    Run the full RAG pipeline for a single question.
    Returns a dict with answer, sources, and metadata.
    """
    if verbose:
        print("\n" + "=" * 70)
        print(f"QUESTION: {question}")
        print(f"Collection: {collection_name}")
        print("=" * 70)

    collection = get_collection(collection_name)
    if collection.count() == 0:
        raise RuntimeError(f"Collection '{collection_name}' is empty.")

    if verbose:
        print(f"\nStep 1: Retrieving {n_results} chunks...")

    chunks = retrieve_chunks(question, collection, embedding_model, n_results)

    if verbose:
        for i, chunk in enumerate(chunks, 1):
            source   = chunk["metadata"].get("filename", "unknown")
            distance = chunk.get("distance") or 0
            print(f"  {i}. {source}  (distance: {distance:.4f})")
        print("\nStep 2: Generating answer...")

    prompt = build_prompt(question, chunks)
    answer = generate_answer(prompt, llm_model)

    if verbose:
        print("\nANSWER:")
        print("-" * 70)
        print(answer)
        print("-" * 70)

    return {
        "question":            question,
        "answer":              answer,
        "collection_name":     collection_name,
        "embedding_model":     embedding_model,
        "llm_model":           llm_model,
        "num_chunks_retrieved": len(chunks),
        "sources":             [c["metadata"] for c in chunks],
    }


def save_results(results, output_file="query_results.json"):
    """Save a list of result dicts to JSON."""
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved results to {output_file}")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    collection_name = make_collection_name(
        BASE_COLLECTION, EMBEDDING_MODEL, CHUNK_SIZE,
        CHUNK_OVERLAP, CHUNK_METHOD, COLLECTION_SUFFIX
    )

    # -------------------------------------------------------------------
    # Edit your questions here
    # -------------------------------------------------------------------
    questions = [
        "What errors appear most frequently in the logs?",
        "Are there any repeated connection failures?",
        "Summarize any critical warnings found in the logs.",
    ]
    # -------------------------------------------------------------------

    all_results = []
    for q in questions:
        result = query(
            question        = q,
            collection_name = collection_name,
            embedding_model = EMBEDDING_MODEL,
            n_results       = N_RESULTS,
            llm_model       = LLM_MODEL,
            verbose         = VERBOSE,
        )
        all_results.append(result)

    save_results(all_results)
