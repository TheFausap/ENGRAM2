#!/usr/bin/env python3

import json
import csv
import hashlib
import argparse
from collections import defaultdict
from typing import List, Dict, Optional

import numpy as np
from sentence_transformers import SentenceTransformer


# =========================
# Configuration
# =========================

DEFAULT_BUCKETS = 200_000     # Hash table size
DEFAULT_HASH_HEADS = 2        # Multi-head hashing
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


# =========================
# Utility functions
# =========================

def normalize_engram_key(key: str) -> str:
    """
    Final canonical normalization.
    """
    return (
        key.lower()
           .replace(" ", "")
           .replace("→", "->")
    )


def is_valid_ngram(key: str) -> bool:
    """
    Only allow 1-, 2-, 3-grams.
    """
    return key.count("->") in (0, 1, 2)


def hash_key(key: str, seed: int, buckets: int) -> int:
    """
    Deterministic hash (Engram-style).
    """
    h = hashlib.blake2b(
        f"{seed}:{key}".encode("utf-8"),
        digest_size=8
    ).digest()
    return int.from_bytes(h, "big") % buckets


# =========================
# Engram Memory
# =========================

class EngramMemory:
    """
    Minimal Engram-style memory.
    """

    def __init__(self, buckets: int, heads: int, embedding_dim: int):
        self.buckets = buckets
        self.heads = heads
        self.embedding_dim = embedding_dim

        # One hash table per head
        self.tables: List[Dict[int, np.ndarray]] = [
            {} for _ in range(heads)
        ]

    def add(self, key: str, embedding: np.ndarray):
        for head in range(self.heads):
            idx = hash_key(key, head, self.buckets)
            self.tables[head][idx] = embedding

    def lookup(self, key: str) -> List[np.ndarray]:
        vectors = []
        for head in range(self.heads):
            idx = hash_key(key, head, self.buckets)
            vec = self.tables[head].get(idx)
            if vec is not None:
                vectors.append(vec)
        return vectors

    def size(self) -> int:
        return sum(len(t) for t in self.tables)


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
# Main pipeline
# =========================

def build_memory(
    input_path: str,
    fmt: str,
    buckets: int,
    heads: int
) -> EngramMemory:

    print("[*] Loading embedding model...")
    embedder = SentenceTransformer(EMBEDDING_MODEL)

    print("[*] Loading Engram keys...")
    if fmt == "csv":
        raw_keys = load_csv(input_path)
    else:
        raw_keys = load_json(input_path)

    print(f"[*] Loaded {len(raw_keys)} raw keys")

    memory = EngramMemory(
        buckets=buckets,
        heads=heads,
        embedding_dim=embedder.get_sentence_embedding_dimension()
    )

    seen = set()
    accepted = 0
    rejected = 0

    print("[*] Building Engram memory...")

    for raw_key in raw_keys:
        key = normalize_engram_key(raw_key)

        if not is_valid_ngram(key):
            rejected += 1
            continue

        if key in seen:
            continue

        embedding = embedder.encode(key, normalize_embeddings=True)
        memory.add(key, embedding)

        seen.add(key)
        accepted += 1

    print("[✓] Engram memory built")
    print(f"    Accepted keys : {accepted}")
    print(f"    Rejected keys : {rejected}")
    print(f"    Memory size   : {memory.size()} entries")

    return memory


# =========================
# Example lookup demo
# =========================

def demo_lookup(memory: EngramMemory):
    print("\n[*] Demo lookup")
    test_keys = [
        "eventid=4624->eventid=4672",
        "eventid=4985"
    ]

    for key in test_keys:
        norm = (key)
        hits = memory.lookup(norm)
        print(f"Key: {norm} → {len(hits)} hit(s)")


# =========================
# CLI
# =========================

def main():
    parser = argparse.ArgumentParser(
        description="Engram memory builder (Splunk → Python)"
    )

    parser.add_argument(
        "--input",
        required=True,
        help="Path to SPL export (CSV or JSON)"
    )

    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        required=True,
        help="Input format"
    )

    parser.add_argument(
        "--buckets",
        type=int,
        default=DEFAULT_BUCKETS
    )

    parser.add_argument(
        "--heads",
        type=int,
        default=DEFAULT_HASH_HEADS
    )

    args = parser.parse_args()

    memory = build_memory(
        input_path=args.input,
        fmt=args.format,
        buckets=args.buckets,
        heads=args.heads
    )

    demo_lookup(memory)


if __name__ == "__main__":
    main()
