#!/usr/bin/env python3
"""
build_doc_index.py

Index the synthesised documentation corpus (doc_corpus.jsonl) into ChromaDB.

Each document becomes one chunk — the content field is embedded, and all
structured fields (doc_id, doc_type, incident_ids, components, failure_pattern,
tier) are stored as metadata for filtering and retrieval evaluation.

The incident_ids metadata field is the ground-truth relevance label: a retrieval
is correct when the returned document's incident_ids contains the incident being
diagnosed.

Edit the CONFIG section, then run:
    python build_doc_index.py

Dependencies:
    pip install chromadb sentence-transformers
"""

import os
import json
import chromadb
from sentence_transformers import SentenceTransformer


# ============================================================================
# CONFIG
# ============================================================================

DATA_FILE       = "./data/doc_corpus.jsonl"
DB_PATH         = "./doc_chroma_db"
COLLECTION_NAME = "docs"
FORCE_REINDEX   = False

# Use the same embedding model as the log index for a fair comparison,
# or swap to a model better suited for longer prose if you want to experiment.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


# ============================================================================
# HELPERS
# ============================================================================

def get_chroma_client(db_path=DB_PATH):
    return chromadb.PersistentClient(path=db_path)


def load_corpus(filepath):
    docs = []
    with open(filepath, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  Warning: skipping malformed line {i}: {e}")
    print(f"  Loaded {len(docs):,} documents from {os.path.basename(filepath)}")
    return docs


def doc_to_text(doc):
    """
    Build the embeddable text for a document.

    Prepends a short header so the embedding captures type and failure
    pattern alongside the content — this helps retrieval distinguish
    a runbook from an error_ref on the same topic.
    """
    header = (
        f"[{doc['doc_type'].upper()}] "
        f"{doc['title']} "
        f"| pattern: {doc['failure_pattern']}"
    )
    return f"{header}\n\n{doc['content']}"


def doc_to_metadata(doc):
    """
    Flatten a doc record into a ChromaDB-compatible metadata dict.
    ChromaDB only accepts str / int / float / bool values — lists must
    be serialised to strings.
    """
    return {
        "doc_id":          doc["doc_id"],
        "doc_type":        doc["doc_type"],
        # Store incident_ids as comma-separated string for ChromaDB
        # Use the helper parse_incident_ids() to recover the list on read.
        "incident_ids":    ",".join(doc["incident_ids"]),
        "components":      ",".join(doc["components"]),
        "failure_pattern": doc["failure_pattern"],
        "tier":            doc["tier"],
        "title":           doc["title"],
    }


# ============================================================================
# INDEX BUILD
# ============================================================================

def build_index(
    data_file=DATA_FILE,
    db_path=DB_PATH,
    collection_name=COLLECTION_NAME,
    embedding_model=EMBEDDING_MODEL,
    force_reindex=FORCE_REINDEX,
):
    print("=" * 70)
    print("BUILD DOC INDEX")
    print(f"  File       : {data_file}")
    print(f"  Collection : {collection_name}")
    print(f"  DB path    : {db_path}")
    print(f"  Model      : {embedding_model}")
    print("=" * 70)

    client = get_chroma_client(db_path)

    # Handle existing collection
    try:
        existing = client.get_collection(name=collection_name)
        if existing.count() > 0:
            if force_reindex:
                print(f"\n  Collection has {existing.count():,} docs — rebuilding")
                client.delete_collection(name=collection_name)
            else:
                print(f"\n  Collection already has {existing.count():,} docs.")
                print("  Set FORCE_REINDEX = True to rebuild.")
                return existing.count()
    except Exception:
        pass

    collection = client.create_collection(name=collection_name)
    print(f"  Created collection: {collection_name}")

    print("\nLoading corpus...")
    docs = load_corpus(data_file)

    print(f"\nGenerating embeddings with {embedding_model}...")
    model = SentenceTransformer(embedding_model)
    texts = [doc_to_text(d) for d in docs]
    embeddings = model.encode(texts, show_progress_bar=True, convert_to_numpy=True)
    print()

    print("Storing in ChromaDB...")
    ids       = [d["doc_id"] for d in docs]
    metadatas = [doc_to_metadata(d) for d in docs]

    # Doc corpus is small enough to add in one batch
    collection.add(
        ids=ids,
        embeddings=embeddings.tolist(),
        documents=texts,
        metadatas=metadatas,
    )

    count = collection.count()
    print(f"\n  Done — {count:,} documents in '{collection_name}'")
    print("=" * 70)
    return count


# ============================================================================
# QUERY HELPERS  (imported by query_doc_agent.py and evaluate.py)
# ============================================================================

_client_cache = {}
_model_cache  = {}


def get_collection(db_path=DB_PATH, collection_name=COLLECTION_NAME):
    key = (db_path, collection_name)
    if key not in _client_cache:
        _client_cache[key] = chromadb.PersistentClient(path=db_path)
    client = _client_cache[key]
    try:
        return client.get_collection(name=collection_name)
    except Exception:
        raise RuntimeError(
            f"Collection '{collection_name}' not found in '{db_path}'. "
            "Run build_doc_index.py first."
        )


def get_embedding_model(model_name=EMBEDDING_MODEL):
    if model_name not in _model_cache:
        _model_cache[model_name] = SentenceTransformer(model_name)
    return _model_cache[model_name]


def parse_incident_ids(metadata):
    """Recover the incident_ids list from the stored comma-separated string."""
    raw = metadata.get("incident_ids", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def retrieve_docs(
    question,
    n_results=5,
    where_filter=None,
    db_path=DB_PATH,
    collection_name=COLLECTION_NAME,
    embedding_model=EMBEDDING_MODEL,
):
    """
    Embed the question and return the closest documents from the corpus.

    Args:
        where_filter: optional ChromaDB metadata filter, e.g.
                      {"doc_type": "runbook"}
                      {"failure_pattern": "connection_pool_exhaustion"}
                      Note: incident_ids filtering requires post-query logic
                      because the field is stored as a comma-separated string.

    Returns:
        list of dicts with keys: doc_id, doc_type, title, content,
        incident_ids (list), failure_pattern, tier, distance
    """
    collection = get_collection(db_path, collection_name)
    model      = get_embedding_model(embedding_model)

    query_embedding = model.encode([question])[0]

    kwargs = dict(
        query_embeddings=[query_embedding.tolist()],
        n_results=n_results,
    )
    if where_filter:
        kwargs["where"] = where_filter

    results = collection.query(**kwargs)

    docs = []
    for i in range(len(results["ids"][0])):
        meta = results["metadatas"][0][i]
        docs.append({
            "doc_id":          meta["doc_id"],
            "doc_type":        meta["doc_type"],
            "title":           meta["title"],
            "content":         results["documents"][0][i],
            "incident_ids":    parse_incident_ids(meta),
            "failure_pattern": meta["failure_pattern"],
            "tier":            meta["tier"],
            "distance":        results["distances"][0][i] if "distances" in results else None,
        })
    return docs


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    build_index()
