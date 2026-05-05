#!/usr/bin/env python3
"""
build_index.py

Index HDFS log data (JSONL format) into ChromaDB.

Each log record becomes a "document" — the message field is embedded,
and all structured fields (level, component, event_type, node_id, block_id, etc.)
are stored as metadata for filtering and inspection.

When you add synthetic labeled errors to the JSONL later, they will be
indexed automatically alongside the real records the next time you run this.

Edit the CONFIG section, then run:
    python build_index.py
"""

import os
import json
import chromadb
from sentence_transformers import SentenceTransformer


# ============================================================================
# CONFIG — edit these before running
# ============================================================================

DATA_FILE         = "./data/hdfs_output.jsonl"   # path to your JSONL file
DB_PATH           = "./chroma_db"
BASE_COLLECTION   = "logs"
COLLECTION_SUFFIX = ""                # optional label e.g. "v1" or "minilm"
FORCE_REINDEX     = False             # True = delete existing and rebuild

EMBEDDING_MODEL   = "all-MiniLM-L6-v2"
CHUNK_METHOD      = "record"          # "record"  : one embedding per log record
                                      # "window"  : sliding window over N records
                                      # "block"   : group all records for the same block_id
CHUNK_SIZE        = 1                 # records per chunk (only used for "window" method)
CHUNK_OVERLAP     = 0                 # overlap in records (only used for "window" method)


# ============================================================================
# COLLECTION NAME
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

def get_chroma_client(db_path=DB_PATH):
    return chromadb.PersistentClient(path=db_path)


def get_or_create_collection(client, name):
    try:
        col = client.get_collection(name=name)
        print(f"  Loaded existing collection: {name}  ({col.count()} chunks)")
    except Exception:
        col = client.create_collection(name=name)
        print(f"  Created new collection: {name}")
    return col


def delete_collection(client, name):
    try:
        client.delete_collection(name=name)
        print(f"  Deleted collection: {name}")
    except Exception:
        print(f"  Collection not found, skipping delete: {name}")


def list_collections(db_path=DB_PATH):
    client = get_chroma_client(db_path)
    return [{"name": c.name, "count": c.count()} for c in client.list_collections()]


# ============================================================================
# LOADING JSONL
# ============================================================================

def load_jsonl(filepath):
    """
    Load all records from the JSONL file.
    Returns a list of dicts. Each dict is one log record with fields:
        timestamp, source_system, component, subcomponent, level,
        node_id, instance_id, event_type, message, metadata
    """
    records = []
    with open(filepath, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  Warning: skipping malformed line {i}: {e}")
    print(f"  Loaded {len(records):,} records from {os.path.basename(filepath)}")
    return records


# ============================================================================
# CHUNKING STRATEGIES
# ============================================================================

def _record_to_text(record):
    """
    Convert a single log record to embeddable text.
    Combines structured fields so the embedding captures more than
    just the raw message string.
    """
    parts = [
        f"[{record.get('level', 'INFO')}]",
        f"component={record.get('component', '')}",
        f"event={record.get('event_type', '')}",
    ]
    if record.get("node_id"):
        parts.append(f"node={record['node_id']}")
    parts.append(record.get("message", ""))
    return " | ".join(parts)


def _record_to_metadata(record):
    """
    Flatten a record into a ChromaDB-compatible metadata dict.
    ChromaDB only accepts str / int / float / bool values.
    """
    inner_meta = record.get("metadata", {}) or {}
    return {
        "timestamp":     record.get("timestamp", ""),
        "source_system": record.get("source_system", ""),
        "component":     record.get("component", ""),
        "subcomponent":  record.get("subcomponent", "") or "",
        "level":         record.get("level", ""),
        "node_id":       record.get("node_id", "") or "",
        "instance_id":   str(record.get("instance_id", "") or ""),
        "event_type":    record.get("event_type", ""),
        "block_id":      inner_meta.get("block_id", "") or "",
        "thread_id":     str(inner_meta.get("thread_id", "") or ""),
        "source_file":   inner_meta.get("source_file", "") or "",
        # label is empty for real data; synthetic errors will populate this
        "label":         str(record.get("label", "") or ""),
    }


def chunk_as_records(records):
    """One chunk per log record — default, most flexible."""
    return [
        {"text": _record_to_text(r), "metadata": _record_to_metadata(r)}
        for r in records
    ]


def chunk_as_windows(records, window_size, overlap):
    """
    Sliding window over N consecutive records.
    Useful for capturing sequences of related events in context.
    """
    chunks = []
    step = max(1, window_size - overlap)
    for i in range(0, len(records), step):
        group = records[i : i + window_size]
        text = "\n".join(_record_to_text(r) for r in group)
        meta = _record_to_metadata(group[0])
        meta["window_size"]  = window_size
        meta["window_start"] = i
        meta["window_end"]   = i + len(group)
        chunks.append({"text": text, "metadata": meta})
    return chunks


def chunk_as_blocks(records):
    """
    Group all records sharing the same block_id into one chunk.
    Good for diagnosing the full lifecycle of a block — allocate,
    transfer, receive, delete — and spotting where errors occur in
    that sequence.
    """
    from collections import defaultdict
    block_groups = defaultdict(list)
    no_block = []

    for r in records:
        bid = (r.get("metadata") or {}).get("block_id", "")
        if bid:
            block_groups[bid].append(r)
        else:
            no_block.append(r)

    chunks = []
    for bid, group in block_groups.items():
        group.sort(key=lambda x: x.get("timestamp", ""))
        text = f"Block: {bid}\n" + "\n".join(_record_to_text(r) for r in group)
        meta = _record_to_metadata(group[-1])
        meta["block_id"]           = bid
        meta["block_record_count"] = len(group)
        # If any record in this block has a label, propagate it to the chunk
        labels = [r.get("label", "") for r in group if r.get("label")]
        meta["label"] = labels[0] if labels else ""
        chunks.append({"text": text, "metadata": meta})

    # Records without a block_id get individual chunks
    for r in no_block:
        chunks.append({"text": _record_to_text(r), "metadata": _record_to_metadata(r)})

    return chunks


def make_chunks(records, chunk_method, chunk_size, chunk_overlap):
    if chunk_method == "record":
        chunks = chunk_as_records(records)
    elif chunk_method == "window":
        chunks = chunk_as_windows(records, chunk_size, chunk_overlap)
    elif chunk_method == "block":
        chunks = chunk_as_blocks(records)
    else:
        raise ValueError(f"Unknown chunk_method: {chunk_method!r}. "
                         "Use 'record', 'window', or 'block'.")
    print(f"  Created {len(chunks):,} chunks  (method='{chunk_method}')")
    return chunks


# ============================================================================
# INDEXING
# ============================================================================

def build_index(data_file, db_path, collection_name, embedding_model,
                chunk_method, chunk_size, chunk_overlap, force_reindex=False):
    """
    Load the JSONL file, chunk, embed, and store in ChromaDB.
    Returns the number of chunks indexed.
    """
    print("=" * 70)
    print("BUILD INDEX")
    print(f"  File       : {data_file}")
    print(f"  Collection : {collection_name}")
    print(f"  Model      : {embedding_model}")
    print(f"  Chunking   : {chunk_method}"
          + (f"  window={chunk_size}  overlap={chunk_overlap}"
             if chunk_method == "window" else ""))
    print("=" * 70)

    client = get_chroma_client(db_path)

    # Handle existing collection
    try:
        existing = client.get_collection(name=collection_name)
        if existing.count() > 0:
            if force_reindex:
                print(f"\n  Collection has {existing.count():,} chunks — rebuilding")
                delete_collection(client, collection_name)
            else:
                print(f"\n  Collection already has {existing.count():,} chunks.")
                print("  Set FORCE_REINDEX = True to rebuild.")
                return existing.count()
    except Exception:
        pass

    collection = get_or_create_collection(client, collection_name)

    print("\nLoading data...")
    records = load_jsonl(data_file)

    print("\nChunking...")
    chunks = make_chunks(records, chunk_method, chunk_size, chunk_overlap)

    print(f"\nGenerating embeddings with {embedding_model}...")
    model = SentenceTransformer(embedding_model)
    texts = [c["text"] for c in chunks]
    embeddings = model.encode(texts, show_progress_bar=True, convert_to_numpy=True)
    print()

    print("Storing in ChromaDB...")
    ids       = [f"chunk_{i}" for i in range(len(chunks))]
    metadatas = [c["metadata"] for c in chunks]

    batch_size = 500
    for i in range(0, len(chunks), batch_size):
        j = min(i + batch_size, len(chunks))
        collection.add(
            ids=ids[i:j],
            embeddings=embeddings[i:j].tolist(),
            documents=texts[i:j],
            metadatas=metadatas[i:j],
        )
        print(f"  Stored {j:,}/{len(chunks):,}")

    print(f"\n  Done — {len(chunks):,} chunks in '{collection_name}'")
    print("=" * 70)
    return len(chunks)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    collection_name = make_collection_name(
        BASE_COLLECTION, EMBEDDING_MODEL, CHUNK_METHOD,
        CHUNK_SIZE, CHUNK_OVERLAP, COLLECTION_SUFFIX
    )

    build_index(
        data_file       = DATA_FILE,
        db_path         = DB_PATH,
        collection_name = collection_name,
        embedding_model = EMBEDDING_MODEL,
        chunk_method    = CHUNK_METHOD,
        chunk_size      = CHUNK_SIZE,
        chunk_overlap   = CHUNK_OVERLAP,
        force_reindex   = FORCE_REINDEX,
    )
