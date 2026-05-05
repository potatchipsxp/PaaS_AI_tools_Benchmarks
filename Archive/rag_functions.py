#!/usr/bin/env python3
"""
Experimental RAG Pipeline - Multiple Configurations

Supports testing different:
- Embedding models
- Chunking strategies
- Collection configurations

Each configuration gets its own ChromaDB collection.
"""

import os
import glob
from typing import List, Dict, Optional, Tuple
import chromadb
from sentence_transformers import SentenceTransformer
import ollama
import json


# ============================================================================
# CONFIGURATION MANAGEMENT
# ============================================================================

class RAGConfig:
    """Configuration for a RAG experiment."""
    
    def __init__(self,
                 embedding_model: str = "all-MiniLM-L6-v2",
                 chunk_size: int = 500,
                 chunk_overlap: int = 50,
                 chunk_method: str = "character",
                 collection_suffix: str = None):
        """
        Args:
            embedding_model: Sentence transformer model name
            chunk_size: Characters (or tokens/sentences depending on method)
            chunk_overlap: Overlap size
            chunk_method: "character", "sentence", or "paragraph"
            collection_suffix: Optional suffix for collection name
        """
        self.embedding_model = embedding_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunk_method = chunk_method
        self.collection_suffix = collection_suffix
    
    def get_collection_name(self, base_name: str = "logdata") -> str:
        """Generate unique collection name based on config."""
        
        # Shorten model name for readability
        model_short = self.embedding_model.split('/')[-1].replace('-', '_')
        
        # Build collection name
        parts = [
            base_name,
            model_short,
            f"chunk{self.chunk_size}",
            f"overlap{self.chunk_overlap}",
            self.chunk_method
        ]
        
        if self.collection_suffix:
            parts.append(self.collection_suffix)
        
        return "_".join(parts)
    
    def to_dict(self) -> Dict:
        """Convert config to dictionary."""
        return {
            'embedding_model': self.embedding_model,
            'chunk_size': self.chunk_size,
            'chunk_overlap': self.chunk_overlap,
            'chunk_method': self.chunk_method,
            'collection_suffix': self.collection_suffix
        }
    
    def __str__(self) -> str:
        """String representation."""
        return (f"RAGConfig(model={self.embedding_model}, "
                f"chunk_size={self.chunk_size}, "
                f"overlap={self.chunk_overlap}, "
                f"method={self.chunk_method})")


# ============================================================================
# MODEL CACHE (support multiple models)
# ============================================================================

_MODEL_CACHE = {}

def get_embedding_model(model_name: str) -> SentenceTransformer:
    """
    Get or load an embedding model.
    Supports multiple models loaded simultaneously.
    
    Args:
        model_name: Sentence transformer model name
        
    Returns:
        Loaded model
    """
    if model_name not in _MODEL_CACHE:
        print(f"Loading embedding model: {model_name}...")
        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
        print(f"✓ Loaded {model_name}\n")
    
    return _MODEL_CACHE[model_name]


def clear_model_cache():
    """Clear all loaded models from memory."""
    global _MODEL_CACHE
    _MODEL_CACHE = {}
    print("✓ Cleared model cache")


# ============================================================================
# CHROMA CLIENT (support multiple collections)
# ============================================================================

_CHROMA_CLIENT = None

def get_chroma_client(db_path: str = "./chroma_db") -> chromadb.PersistentClient:
    """Get or create ChromaDB client."""
    global _CHROMA_CLIENT
    if _CHROMA_CLIENT is None:
        _CHROMA_CLIENT = chromadb.PersistentClient(path=db_path)
    return _CHROMA_CLIENT


def get_or_create_collection(collection_name: str) -> chromadb.Collection:
    """
    Get or create a specific collection.
    
    Args:
        collection_name: Unique collection name
        
    Returns:
        ChromaDB collection
    """
    client = get_chroma_client()
    
    try:
        collection = client.get_collection(name=collection_name)
        print(f"✓ Loaded existing collection: {collection_name}")
    except:
        collection = client.create_collection(name=collection_name)
        print(f"✓ Created new collection: {collection_name}")
    
    return collection


def list_collections() -> List[Dict]:
    """
    List all collections in the database.
    
    Returns:
        List of collection info dicts
    """
    client = get_chroma_client()
    collections = client.list_collections()
    
    result = []
    for col in collections:
        result.append({
            'name': col.name,
            'count': col.count()
        })
    
    return result


def delete_collection(collection_name: str):
    """Delete a collection."""
    client = get_chroma_client()
    try:
        client.delete_collection(name=collection_name)
        print(f"✓ Deleted collection: {collection_name}")
    except:
        print(f"Collection {collection_name} does not exist")


# ============================================================================
# DOCUMENT LOADING (same as before)
# ============================================================================

def load_documents(data_dir: str) -> List[Dict]:
    """Load all text documents from a directory."""
    documents = []
    txt_files = glob.glob(os.path.join(data_dir, "*.txt"))
    
    print(f"Loading documents from {data_dir}...")
    for filepath in txt_files:
        filename = os.path.basename(filepath)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        documents.append({
            'filename': filename,
            'content': content,
            'path': filepath
        })
        print(f"  ✓ Loaded: {filename} ({len(content):,} chars)")
    
    print(f"Loaded {len(documents)} documents\n")
    return documents


# ============================================================================
# CHUNKING STRATEGIES
# ============================================================================

def chunk_by_characters(text: str,
                       chunk_size: int,
                       chunk_overlap: int,
                       metadata: Optional[Dict] = None) -> List[Dict]:
    """Simple character-based chunking."""
    chunks = []
    start = 0
    
    while start < len(text):
        end = start + chunk_size
        chunk_text = text[start:end]
        
        chunks.append({
            'text': chunk_text,
            'metadata': metadata or {},
            'char_start': start,
            'char_end': end
        })
        
        start += chunk_size - chunk_overlap
    
    return chunks


def chunk_by_sentences(text: str,
                      sentences_per_chunk: int,
                      overlap_sentences: int,
                      metadata: Optional[Dict] = None) -> List[Dict]:
    """
    Sentence-aware chunking.
    
    Args:
        text: Text to chunk
        sentences_per_chunk: Number of sentences per chunk
        overlap_sentences: Number of sentences to overlap
        metadata: Optional metadata
        
    Returns:
        List of chunks
    """
    # Simple sentence splitting (could use spaCy for better results)
    # Split on . ! ? followed by space and capital letter
    import re
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    
    chunks = []
    i = 0
    
    while i < len(sentences):
        # Take sentences_per_chunk sentences
        chunk_sentences = sentences[i:i + sentences_per_chunk]
        chunk_text = ' '.join(chunk_sentences)
        
        chunks.append({
            'text': chunk_text,
            'metadata': metadata or {},
            'sentence_start': i,
            'sentence_end': i + len(chunk_sentences)
        })
        
        # Move forward with overlap
        i += sentences_per_chunk - overlap_sentences
    
    return chunks


def chunk_by_paragraphs(text: str,
                       paragraphs_per_chunk: int,
                       overlap_paragraphs: int,
                       metadata: Optional[Dict] = None) -> List[Dict]:
    """
    Paragraph-based chunking.
    
    Args:
        text: Text to chunk
        paragraphs_per_chunk: Paragraphs per chunk
        overlap_paragraphs: Overlap
        metadata: Optional metadata
        
    Returns:
        List of chunks
    """
    # Split on double newlines
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    
    chunks = []
    i = 0
    
    while i < len(paragraphs):
        chunk_paragraphs = paragraphs[i:i + paragraphs_per_chunk]
        chunk_text = '\n\n'.join(chunk_paragraphs)
        
        chunks.append({
            'text': chunk_text,
            'metadata': metadata or {},
            'paragraph_start': i,
            'paragraph_end': i + len(chunk_paragraphs)
        })
        
        i += paragraphs_per_chunk - overlap_paragraphs
    
    return chunks


def chunk_text(text: str,
              config: RAGConfig,
              metadata: Optional[Dict] = None) -> List[Dict]:
    """
    Chunk text using the specified method in config.
    
    Args:
        text: Text to chunk
        config: RAGConfig specifying chunking strategy
        metadata: Optional metadata
        
    Returns:
        List of chunks
    """
    if config.chunk_method == "character":
        return chunk_by_characters(text, config.chunk_size, 
                                  config.chunk_overlap, metadata)
    
    elif config.chunk_method == "sentence":
        # chunk_size is number of sentences
        return chunk_by_sentences(text, config.chunk_size, 
                                 config.chunk_overlap, metadata)
    
    elif config.chunk_method == "paragraph":
        # chunk_size is number of paragraphs
        return chunk_by_paragraphs(text, config.chunk_size, 
                                   config.chunk_overlap, metadata)
    
    else:
        raise ValueError(f"Unknown chunk method: {config.chunk_method}")


def chunk_documents(documents: List[Dict], config: RAGConfig) -> List[Dict]:
    """Chunk multiple documents using config."""
    print(f"Chunking documents (method: {config.chunk_method})...")
    all_chunks = []
    
    for doc in documents:
        metadata = {
            'filename': doc['filename'],
            'source': doc['path']
        }
        chunks = chunk_text(doc['content'], config, metadata)
        all_chunks.extend(chunks)
    
    print(f"✓ Created {len(all_chunks)} chunks\n")
    return all_chunks


# ============================================================================
# INDEXING WITH CONFIGURATION
# ============================================================================

def index_documents_with_config(data_dir: str,
                               config: RAGConfig,
                               base_collection_name: str = "logdata") -> Tuple[str, int]:
    """
    Index documents with a specific configuration.
    
    Args:
        data_dir: Directory containing documents
        config: RAGConfig specifying how to process
        base_collection_name: Base name for collection
        
    Returns:
        Tuple of (collection_name, num_chunks_indexed)
    """
    print("=" * 70)
    print(f"INDEXING WITH CONFIG: {config}")
    print("=" * 70)
    print()
    
    # Generate collection name
    collection_name = config.get_collection_name(base_collection_name)
    print(f"Collection name: {collection_name}\n")
    
    # Check if already exists
    collection = get_or_create_collection(collection_name)
    existing_count = collection.count()
    
    if existing_count > 0:
        print(f"⚠ Collection already has {existing_count} chunks")
        response = input("Delete and re-index? (y/n): ").strip().lower()
        if response != 'y':
            print("Skipping indexing.")
            return collection_name, existing_count
        delete_collection(collection_name)
        collection = get_or_create_collection(collection_name)
    
    # Load documents
    documents = load_documents(data_dir)
    
    # Chunk documents
    chunks = chunk_documents(documents, config)
    
    # Generate embeddings
    print(f"Generating embeddings with {config.embedding_model}...")
    model = get_embedding_model(config.embedding_model)
    chunk_texts = [chunk['text'] for chunk in chunks]
    embeddings = model.encode(
        chunk_texts,
        show_progress_bar=True,
        convert_to_numpy=True
    )
    print()
    
    # Store in ChromaDB
    print("Storing in ChromaDB...")
    ids = [f"chunk_{i}" for i in range(len(chunks))]
    metadatas = [chunk['metadata'] for chunk in chunks]
    
    # Add config info to metadata
    for metadata in metadatas:
        metadata['config'] = config.to_dict()
    
    batch_size = 100
    for i in range(0, len(chunks), batch_size):
        end_idx = min(i + batch_size, len(chunks))
        
        collection.add(
            ids=ids[i:end_idx],
            embeddings=embeddings[i:end_idx].tolist(),
            documents=chunk_texts[i:end_idx],
            metadatas=metadatas[i:end_idx]
        )
    
    print(f"✓ Indexed {len(chunks)} chunks\n")
    print("=" * 70)
    print(f"INDEXING COMPLETE: {collection_name}")
    print("=" * 70)
    print()
    
    return collection_name, len(chunks)


# ============================================================================
# QUERYING WITH CONFIGURATION
# ============================================================================

def query_with_config(question: str,
                     config: RAGConfig,
                     base_collection_name: str = "logdata",
                     n_results: int = 5,
                     llm_model: str = "llama3.2",
                     verbose: bool = True) -> Dict:
    """
    Query using a specific configuration.
    
    Args:
        question: User's question
        config: RAGConfig to use for retrieval
        base_collection_name: Base name for collection
        n_results: Number of chunks to retrieve
        llm_model: Ollama model for generation
        verbose: Print progress
        
    Returns:
        Dict with answer, sources, config info
    """
    collection_name = config.get_collection_name(base_collection_name)
    
    if verbose:
        print("\n" + "=" * 70)
        print(f"QUERY: {question}")
        print(f"Config: {config}")
        print("=" * 70)
        print()
    
    # Get model and collection
    model = get_embedding_model(config.embedding_model)
    collection = get_or_create_collection(collection_name)
    
    if collection.count() == 0:
        return {
            'error': f"Collection {collection_name} is empty. Run indexing first.",
            'config': config.to_dict()
        }
    
    # Embed query
    if verbose:
        print("Step 1: Retrieving relevant chunks...")
    
    query_embedding = model.encode([question])[0]
    
    # Search
    results = collection.query(
        query_embeddings=[query_embedding.tolist()],
        n_results=n_results
    )
    
    # Format results
    chunks = []
    for i in range(len(results['ids'][0])):
        chunks.append({
            'id': results['ids'][0][i],
            'text': results['documents'][0][i],
            'metadata': results['metadatas'][0][i],
            'distance': results['distances'][0][i] if 'distances' in results else None
        })
    
    if verbose:
        print(f"✓ Retrieved {len(chunks)} chunks")
        for i, chunk in enumerate(chunks, 1):
            source = chunk['metadata'].get('filename', 'unknown')
            distance = chunk.get('distance', 0)
            print(f"  {i}. {source} (distance: {distance:.3f})")
        print("\nStep 2: Generating answer...")
    
    # Generate answer
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        source = chunk['metadata'].get('filename', 'unknown')
        context_parts.append(f"[Source {i}: {source}]\n{chunk['text']}")
    
    context = "\n\n---\n\n".join(context_parts)
    
    prompt = f"""You are a helpful assistant answering questions based on provided context.

Context information:
{context}

Question: {question}

Please provide a clear, accurate answer based on the context above."""

    response = ollama.generate(model=llm_model, prompt=prompt)
    
    if verbose:
        print(f"✓ Generated answer\n")
    
    return {
        'answer': response['response'],
        'sources': [chunk['metadata'] for chunk in chunks],
        'config': config.to_dict(),
        'collection_name': collection_name,
        'num_chunks_retrieved': len(chunks)
    }


# ============================================================================
# COMPARISON UTILITIES
# ============================================================================

def compare_configs(question: str,
                   configs: List[RAGConfig],
                   base_collection_name: str = "logdata",
                   n_results: int = 5,
                   llm_model: str = "llama3.2") -> List[Dict]:
    """
    Query the same question across multiple configurations.
    
    Args:
        question: Question to ask
        configs: List of RAGConfig objects
        base_collection_name: Base name for collections
        n_results: Number of chunks to retrieve
        llm_model: LLM model to use
        
    Returns:
        List of results, one per config
    """
    print("\n" + "=" * 70)
    print(f"COMPARING {len(configs)} CONFIGURATIONS")
    print(f"Question: {question}")
    print("=" * 70)
    
    results = []
    
    for i, config in enumerate(configs, 1):
        print(f"\n--- Configuration {i}/{len(configs)} ---")
        result = query_with_config(
            question,
            config,
            base_collection_name,
            n_results,
            llm_model,
            verbose=True
        )
        results.append(result)
        
        if 'answer' in result:
            print(f"\nANSWER:")
            print("-" * 70)
            print(result['answer'][:500])  # Truncate for comparison
            if len(result['answer']) > 500:
                print("... (truncated)")
            print("-" * 70)
    
    return results


def save_comparison_results(results: List[Dict], output_file: str = "comparison_results.json"):
    """Save comparison results to JSON file."""
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\n✓ Saved results to {output_file}")
