#!/usr/bin/env python3
"""
Example: Running RAG Experiments

This script demonstrates how to test different configurations:
- Different embedding models
- Different chunking strategies
- Compare results
"""

from rag_experiments import (
    RAGConfig,
    index_documents_with_config,
    query_with_config,
    compare_configs,
    list_collections,
    save_comparison_results
)


def experiment_1_different_models():
    """
    Experiment 1: Compare different embedding models.
    Same chunking, different embeddings.
    """
    print("\n" + "=" * 70)
    print("EXPERIMENT 1: Different Embedding Models")
    print("=" * 70)
    
    configs = [
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",  # Small, fast (384 dims)
            chunk_size=500,
            chunk_overlap=50,
            chunk_method="character"
        ),
        RAGConfig(
            embedding_model="all-mpnet-base-v2",  # Better quality (768 dims)
            chunk_size=500,
            chunk_overlap=50,
            chunk_method="character"
        ),
        RAGConfig(
            embedding_model="multi-qa-MiniLM-L6-cos-v1",  # Optimized for Q&A
            chunk_size=500,
            chunk_overlap=50,
            chunk_method="character"
        ),
    ]
    
    # Index with each config
    print("\n--- INDEXING PHASE ---\n")
    for config in configs:
        index_documents_with_config("./data", config)
    
    # Test a query
    print("\n--- QUERY PHASE ---\n")
    question = "When was Abraham Lincoln born?"
    
    results = compare_configs(question, configs)
    save_comparison_results(results, "exp1_models.json")


def experiment_2_different_chunk_sizes():
    """
    Experiment 2: Compare different chunk sizes.
    Same model, different chunk sizes.
    """
    print("\n" + "=" * 70)
    print("EXPERIMENT 2: Different Chunk Sizes")
    print("=" * 70)
    
    configs = [
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",
            chunk_size=300,  # Small chunks
            chunk_overlap=30,
            chunk_method="character"
        ),
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",
            chunk_size=500,  # Medium chunks
            chunk_overlap=50,
            chunk_method="character"
        ),
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",
            chunk_size=1000,  # Large chunks
            chunk_overlap=100,
            chunk_method="character"
        ),
    ]
    
    # Index
    print("\n--- INDEXING PHASE ---\n")
    for config in configs:
        index_documents_with_config("./data", config)
    
    # Test queries
    print("\n--- QUERY PHASE ---\n")
    questions = [
        "When was Abraham Lincoln born?",  # Simple fact
        "What were Theodore Roosevelt's major accomplishments?"  # Complex
    ]
    
    for question in questions:
        results = compare_configs(question, configs)
        filename = f"exp2_chunks_{question[:20].replace(' ', '_')}.json"
        save_comparison_results(results, filename)


def experiment_3_different_chunk_methods():
    """
    Experiment 3: Compare chunking methods.
    Character vs sentence vs paragraph.
    """
    print("\n" + "=" * 70)
    print("EXPERIMENT 3: Different Chunking Methods")
    print("=" * 70)
    
    configs = [
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",
            chunk_size=500,  # 500 characters
            chunk_overlap=50,
            chunk_method="character"
        ),
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",
            chunk_size=5,  # 5 sentences
            chunk_overlap=1,
            chunk_method="sentence"
        ),
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",
            chunk_size=2,  # 2 paragraphs
            chunk_overlap=0,
            chunk_method="paragraph"
        ),
    ]
    
    # Index
    print("\n--- INDEXING PHASE ---\n")
    for config in configs:
        index_documents_with_config("./data", config)
    
    # Test
    print("\n--- QUERY PHASE ---\n")
    question = "What were the circumstances of JFK's assassination?"
    
    results = compare_configs(question, configs)
    save_comparison_results(results, "exp3_methods.json")


def experiment_4_combined_variations():
    """
    Experiment 4: Test combinations of different settings.
    """
    print("\n" + "=" * 70)
    print("EXPERIMENT 4: Combined Variations")
    print("=" * 70)
    
    configs = [
        # Best for speed
        RAGConfig(
            embedding_model="all-MiniLM-L6-v2",
            chunk_size=300,
            chunk_overlap=30,
            chunk_method="character",
            collection_suffix="fast"
        ),
        # Best for quality
        RAGConfig(
            embedding_model="all-mpnet-base-v2",
            chunk_size=5,
            chunk_overlap=1,
            chunk_method="sentence",
            collection_suffix="quality"
        ),
        # Balanced
        RAGConfig(
            embedding_model="multi-qa-MiniLM-L6-cos-v1",
            chunk_size=500,
            chunk_overlap=50,
            chunk_method="character",
            collection_suffix="balanced"
        ),
    ]
    
    # Index
    print("\n--- INDEXING PHASE ---\n")
    for config in configs:
        index_documents_with_config("./data", config)
    
    # Test multiple questions
    print("\n--- QUERY PHASE ---\n")
    questions = [
        "When was Abraham Lincoln born?",
        "Compare the presidencies of FDR and Reagan",
        "What wars did Theodore Roosevelt participate in?"
    ]
    
    all_results = {}
    for question in questions:
        print(f"\n\nTesting: {question}")
        results = compare_configs(question, configs)
        all_results[question] = results
    
    save_comparison_results(all_results, "exp4_combined.json")


def show_all_collections():
    """Show all indexed collections."""
    print("\n" + "=" * 70)
    print("EXISTING COLLECTIONS")
    print("=" * 70)
    
    collections = list_collections()
    
    if not collections:
        print("No collections found.")
    else:
        for col in collections:
            print(f"\n{col['name']}")
            print(f"  Chunks: {col['count']}")


def quick_test():
    """
    Quick test: Index one config and test it.
    Good for verifying everything works.
    """
    print("\n" + "=" * 70)
    print("QUICK TEST")
    print("=" * 70)
    
    # Simple config
    config = RAGConfig(
        embedding_model="all-MiniLM-L6-v2",
        chunk_size=500,
        chunk_overlap=50,
        chunk_method="character"
    )
    
    # Index
    print("\n--- INDEXING ---\n")
    index_documents_with_config("./data", config)
    
    # Query
    print("\n--- QUERYING ---\n")
    result = query_with_config(
        "When was Abraham Lincoln born?",
        config,
        verbose=True
    )
    
    if 'answer' in result:
        print(f"\nANSWER:")
        print("-" * 70)
        print(result['answer'])
        print("-" * 70)


def main():
    """Main menu."""
    print("\n" + "=" * 70)
    print("RAG EXPERIMENTATION SUITE")
    print("=" * 70)
    print("""
Choose an experiment:
    
    0. Quick test (verify setup works)
    1. Compare embedding models
    2. Compare chunk sizes
    3. Compare chunking methods
    4. Combined variations (comprehensive)
    5. Show all existing collections
    q. Quit
    """)
    
    choice = input("Select experiment (0-5, q): ").strip()
    
    if choice == '0':
        quick_test()
    elif choice == '1':
        experiment_1_different_models()
    elif choice == '2':
        experiment_2_different_chunk_sizes()
    elif choice == '3':
        experiment_3_different_chunk_methods()
    elif choice == '4':
        experiment_4_combined_variations()
    elif choice == '5':
        show_all_collections()
    elif choice.lower() == 'q':
        print("Goodbye!")
    else:
        print("Invalid choice")


if __name__ == "__main__":
    main()
