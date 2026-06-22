
#!/usr/bin/env python3
"""
Engram Memory Builder with optional FAISS integration

Pipeline stage covered:
Splunk exports -> Python pattern normalization + hashing ->
  (A) O(1) hash-table lookup (pure Engram)
  (B) Optional FAISS similarity search (hybrid mode)

Designed for Windows Security XML Engram keys.
"""

import json
import csv
import argparse
import hashlib
from typing import List, Dict
from collections import defaultdict

import numpy as np

from sentence_transformers import SentenceTransformer

# =========================
# Configuration
# =========================

DEFAULT_BUCKETS = 200_000
DEFAULT_HASH_HEADS = 2
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# =========================
# Utility functions
# =========================

def normalize_engram_key(key: str) -> str:
    return key.lower().replace(" ", "").replace("→", "->")


def is_valid_ngram(key: str) -> bool:
    # allow 1, 2, 3 event sequences only
    return key.count("->") in (0, 1, 2)


def hash_key(key: str, seed: int, buckets: int) -> int:
    h = hashlib.blake2b(
        f"{seed}:{key}".encode("utf-8"),
        digest_size=8
    ).digest()
    return int.from_bytes(h, "big") % buckets

# =========================
# Engram Memory (hash-based)
# =========================

class EngramMemory:
    def __init__(self, buckets: int, heads: int, dim: int):
        self.buckets = buckets
        self.heads = heads
        self.dim = dim
        self.tables: List[Dict[int, np.ndarray]] = [
            {} for _ in range(heads)
        ]

    def add(self, key: str, embedding: np.ndarray):
        for h in range(self.heads):
            idx = hash_key(key, h, self.buckets)
            self.tables[h][idx] = embedding

    def lookup(self, key: str) -> List[np.ndarray]:
        hits = []
        for h in range(self.heads):
            idx = hash_key(key, h, self.buckets)
            vec = self.tables[h].get(idx)
            if vec is not None:
                hits.append(vec)
        return hits

    def size(self) -> int:
        return sum(len(t) for t in self.tables)

from typing import List, Tuple

class EngramNumpySimilarity:
    """
    Simple cosine-similarity search using NumPy.
    Intended as a FAISS replacement for Python 3.12.
    """

    def __init__(self):
        self.keys: List[str] = []
        self.embeddings: List[np.ndarray] = []
        self.matrix: np.ndarray | None = None

    def add(self, key: str, embedding: np.ndarray):
        # embeddings are assumed to be L2-normalized
        self.keys.append(key)
        self.embeddings.append(embedding)

    def build(self):
        if not self.embeddings:
            raise RuntimeError("No embeddings to build similarity matrix")
        self.matrix = np.vstack(self.embeddings)

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5,
        min_score: float = 0.0) -> List[Tuple[str, float]]:
        """
        Returns top-k most similar keys by cosine similarity.
        """
        if self.matrix is None:
            raise RuntimeError("Similarity index not built")

        # cosine similarity because embeddings are normalized
        scores = self.matrix @ query_embedding

        idxs = np.argsort(-scores)[:top_k]

        results = []
        for idx in idxs:
            score = float(scores[idx])
            if score >= min_score:
                results.append((self.keys[idx], score))

        return results

# =========================
# Loaders
# =========================

def load_csv(path: str) -> List[str]:
    keys = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "engram_key" in row:
                keys.append(row["engram_key"])
    return keys


def load_json(path: str) -> List[str]:
    keys = []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
        for row in data:
            if "engram_key" in row:
                keys.append(row["engram_key"])
    return keys

# =========================
# Build pipeline
# =========================

def build_memory(input_path: str, fmt: str, buckets: int, heads: int, use_similarity: bool):
    print("[*] Loading embedding model...")
    embedder = SentenceTransformer(EMBEDDING_MODEL)
    dim = embedder.get_sentence_embedding_dimension()

    print("[*] Loading Engram keys...")
    raw_keys = load_csv(input_path) if fmt == "csv" else load_json(input_path)
    print(f"[*] Loaded {len(raw_keys)} raw keys")

    memory = EngramMemory(buckets=buckets, heads=heads, dim=dim)
    similarity_index = EngramNumpySimilarity() if use_similarity else None

    seen = set()
    accepted = rejected = 0

    print("[*] Building Engram memory...")

    for raw in raw_keys:
        key = normalize_engram_key(raw)
        if not is_valid_ngram(key):
            rejected += 1
            continue
        if key in seen:
            continue

        emb = embedder.encode(key, normalize_embeddings=True)
        memory.add(key, emb)

        if similarity_index:
            similarity_index.add(key, emb)

        seen.add(key)
        accepted += 1

    print("[✓] Memory build complete")
    print(f"    Accepted: {accepted}")
    print(f"    Rejected: {rejected}")
    print(f"    Hash entries: {memory.size()}")
    
    if similarity_index:
        print("[*] Building similarity index")
        similarity_index.build()
        print("[✓] Similarity index build complete")

    return memory, similarity_index, embedder
    
def resolve_pattern(
    memory,
    similarity_index,
    embedder,
    key,
    top_k=5):
    # 1. Hash-based Engram lookup (O(1))
    hits = memory.lookup(key)
    if hits:
        return {
            "type": "exact",
            "vectors": hits
        }

    # 2. Similarity fallback (cold path)
    if similarity_index:
        emb = embedder.encode(key, normalize_embeddings=True)
        sims = similarity_index.search(emb, top_k=top_k)
        return {
            "type": "similar",
            "matches": sims
        }

    # 3. Unknown pattern
    return {
        "type": "unknown"
    }

# =========================
# Demo
# =========================

def demo(memory, similarity_index, embedder):
    print("\n[*] Demo lookup")
    test_key = "eventid=4624->eventid=4672"
    norm = normalize_engram_key(test_key)

    hits = resolve_pattern(memory,similarity_index,embedder,norm)
    print(f"Resolve lookup: {hits}")

# =========================
# CLI
# =========================

def main():
    ap = argparse.ArgumentParser("Engram memory builder with FAISS")
    ap.add_argument("--input", required=True, help="CSV or JSON export from Splunk")
    ap.add_argument("--format", choices=["csv", "json"], required=True)
    ap.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS)
    ap.add_argument("--heads", type=int, default=DEFAULT_HASH_HEADS)
    ap.add_argument("--similarity", action="store_true", help="Enable Cosine similarity index")

    args = ap.parse_args()

    memory, similarity_index, embedder = build_memory(
        args.input,
        args.format,
        args.buckets,
        args.heads,
        args.similarity
    )

    demo(memory, similarity_index, embedder)


if __name__ == "__main__":
    main()
