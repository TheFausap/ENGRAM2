
#!/usr/bin/env python3
"""engram_memory_numpy_gate.py

Engram Memory Builder for Splunk-exported Engram keys with:
  - Hash-table (O(1)) lookup (primary, deterministic)
  - Optional NumPy cosine similarity search (secondary, cold-path)
  - Context-aware gating (Engram-style) to suppress irrelevant/colliding memory

Gating rationale (from Engram writeups): use current context representation as Query
and retrieved memory as Key/Value; compute a scalar gate via normalized dot product
and a sigmoid to suppress conflicting memory. citeturn14search71turn14search72

This script approximates the hidden-state Query using the *query embedding*.
That keeps the system model-agnostic until you wire it into an actual LLM runtime.
"""

import json
import csv
import argparse
import hashlib
from typing import List, Dict, Tuple, Optional

import numpy as np
from sentence_transformers import SentenceTransformer

# =========================
# Defaults
# =========================

DEFAULT_BUCKETS = 200_000
DEFAULT_HASH_HEADS = 2
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Gating defaults (tunable)
DEFAULT_GATE_TEMP = 1.0      # higher -> softer; lower -> sharper
DEFAULT_GATE_BIAS = 0.0
DEFAULT_GATE_MIN = 0.15      # below this alpha -> treat as 0
DEFAULT_TOPK_SIM = 8         # similarity candidates if hash misses

# =========================
# Utils
# =========================

def normalize_engram_key(key: str) -> str:
    return key.lower().replace(" ", "").replace("→", "->")


def is_valid_ngram(key: str) -> bool:
    return key.count("->") in (0, 1, 2)


def hash_key(key: str, seed: int, buckets: int) -> int:
    h = hashlib.blake2b(f"{seed}:{key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % buckets


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


# =========================
# Engram Memory (hash)
# =========================

class EngramMemory:
    def __init__(self, buckets: int, heads: int, dim: int):
        self.buckets = buckets
        self.heads = heads
        self.dim = dim
        self.tables: List[Dict[int, np.ndarray]] = [{} for _ in range(heads)]

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


# =========================
# NumPy similarity (optional)
# =========================

class EngramNumpySimilarity:
    """
    Cosine similarity search via matrix multiply.
    Assumes embeddings are L2-normalized.
    """

    def __init__(self):
        self.keys: List[str] = []
        self.embeddings: List[np.ndarray] = []
        self.matrix: Optional[np.ndarray] = None

        # ✅ O(1) key → row index
        self.key_to_row: Dict[str, int] = {}

    def add(self, key: str, embedding: np.ndarray):
        self.key_to_row[key] = len(self.keys)
        self.keys.append(key)
        self.embeddings.append(embedding)

    def build(self):
        if not self.embeddings:
            raise RuntimeError("No embeddings to build similarity index")
        self.matrix = np.vstack(self.embeddings).astype(np.float32)

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5) -> List[Tuple[str, float]]:
        if self.matrix is None:
            raise RuntimeError("Similarity index not built")

        q = query_embedding.astype(np.float32)
        scores = self.matrix @ q
        idxs = np.argsort(-scores)[:top_k]

        return [(self.keys[i], float(scores[i])) for i in idxs]


# =========================
# Context-aware gating
# =========================

class EngramGater:
    """Engram-style scalar gating.

    In the paper/writeups, gating uses normalized dot product between the current
    hidden state (Query) and memory-derived Key, passed through sigmoid to produce
    a scalar gate α. If memory conflicts with context, α tends toward 0. citeturn14search71turn14search72

    Here we approximate Query with the query embedding and Key with each retrieved
    memory embedding (both already L2-normalized).
    """

    def __init__(self, temperature: float = DEFAULT_GATE_TEMP, bias: float = DEFAULT_GATE_BIAS, min_alpha: float = DEFAULT_GATE_MIN):
        self.temperature = float(max(1e-6, temperature))
        self.bias = float(bias)
        self.min_alpha = float(min_alpha)

    def gate(self, query_emb: np.ndarray, mem_embs: List[np.ndarray]) -> Tuple[np.ndarray, List[float]]:
        """Return fused vector and per-embedding gate weights.

        - query_emb: (D,) normalized
        - mem_embs: list of (D,) normalized embeddings

        Fused vector: weighted average of memory vectors.
        """
        if not mem_embs:
            return np.zeros_like(query_emb), []

        M = np.vstack(mem_embs).astype(np.float32)  # (m, D)
        q = query_emb.astype(np.float32)            # (D,)

        # cosine similarity since vectors are normalized
        sims = M @ q  # (m,)

        # scalar gate per memory vector
        # α = sigmoid( sim / temp + bias )
        alphas = sigmoid(sims / self.temperature + self.bias)

        # hard suppression of weak gates
        alphas = np.where(alphas >= self.min_alpha, alphas, 0.0)

        s = float(alphas.sum())
        if s <= 1e-9:
            return np.zeros_like(query_emb), [float(a) for a in alphas]

        fused = (alphas.reshape(-1, 1) * M).sum(axis=0) / s
        # keep normalized for downstream similarity / fusion stability
        norm = np.linalg.norm(fused) + 1e-12
        fused = fused / norm

        return fused.astype(np.float32), [float(a) for a in alphas]


# =========================
# Loaders
# =========================

def load_csv(path: str) -> List[str]:
    keys = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "engram_key" in row and row["engram_key"]:
                keys.append(row["engram_key"])
    return keys


def load_json(path: str) -> List[str]:
    keys = []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
        for row in data:
            if isinstance(row, dict) and "engram_key" in row and row["engram_key"]:
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
    sim_index = EngramNumpySimilarity() if use_similarity else None

    seen = set()
    accepted = rejected = 0

    for raw in raw_keys:
        key = normalize_engram_key(raw)
        if not is_valid_ngram(key):
            rejected += 1
            continue
        if key in seen:
            continue

        emb = embedder.encode(key, normalize_embeddings=True)
        memory.add(key, emb)
        if sim_index:
            sim_index.add(key, emb)

        seen.add(key)
        accepted += 1

    if sim_index:
        sim_index.build()

    print("[✓] Memory build complete")
    print(f"    Accepted: {accepted}")
    print(f"    Rejected: {rejected}")
    print(f"    Hash entries: {memory.size()}")
    if sim_index:
        print(f"    Similarity vectors: {len(sim_index.keys)}")

    return memory, sim_index, embedder


# =========================
# Resolution: hash -> (optional) similarity -> gating
# =========================

def resolve_with_gating(
    key: str,
    memory: EngramMemory,
    sim_index: Optional[EngramNumpySimilarity],
    embedder: SentenceTransformer,
    gater: EngramGater,
    sim_top_k: int = DEFAULT_TOPK_SIM,
):
    """Return a dict describing how memory was applied."""
    norm_key = normalize_engram_key(key)
    q = embedder.encode(norm_key, normalize_embeddings=True)

    hits = memory.lookup(norm_key)
    if hits:
        fused, alphas = gater.gate(q, hits)
        return {
            "key": norm_key,
            "mode": "hash_exact",
            "num_candidates": len(hits),
            "alphas": alphas,
            "fused_vector_norm": float(np.linalg.norm(fused)),
        }

    if sim_index is not None:
        # similarity candidates are keys; retrieve their embeddings from sim_index.matrix by index
        sims = sim_index.search(q, top_k=sim_top_k)
        # convert to embeddings (we can use sim_index.matrix rows for speed)
        # build a small embedding list in the same order
        cand_embs = []
        for cand_key, _score in sims:
            idx = sim_index.key_to_row[cand_key]
            cand_embs.append(sim_index.matrix[idx])

        fused, alphas = gater.gate(q, cand_embs)
        return {
            "key": norm_key,
            "mode": "similarity_fallback",
            "num_candidates": len(cand_embs),
            "top_candidates": sims,
            "alphas": alphas,
            "fused_vector_norm": float(np.linalg.norm(fused)),
        }

    return {
        "key": norm_key,
        "mode": "unknown",
        "num_candidates": 0,
        "alphas": [],
        "fused_vector_norm": 0.0,
    }


# =========================
# Demo
# =========================

def demo(memory, sim_index, embedder, gater):
    tests = [
        "eventid=4624->eventid=4672",
        "eventid=9999->eventid=8888",  # likely unknown
    ]

    print("\n[*] Demo gating resolution")
    for t in tests:
        out = resolve_with_gating(t, memory, sim_index, embedder, gater)
        print(json.dumps(out, indent=2))


# =========================
# CLI
# =========================

def main():
    ap = argparse.ArgumentParser("Engram memory (NumPy similarity + gating)")
    ap.add_argument("--input", required=True, help="CSV/JSON export from Splunk containing 'engram_key'")
    ap.add_argument("--format", choices=["csv", "json"], required=True)
    ap.add_argument("--buckets", type=int, default=DEFAULT_BUCKETS)
    ap.add_argument("--heads", type=int, default=DEFAULT_HASH_HEADS)
    ap.add_argument("--similarity", action="store_true", help="Enable NumPy similarity fallback")

    # gating params
    ap.add_argument("--gate_temp", type=float, default=DEFAULT_GATE_TEMP, help="Sigmoid temperature (lower=sharper)")
    ap.add_argument("--gate_bias", type=float, default=DEFAULT_GATE_BIAS, help="Sigmoid bias")
    ap.add_argument("--gate_min", type=float, default=DEFAULT_GATE_MIN, help="Minimum alpha to keep (else 0)")
    ap.add_argument("--sim_topk", type=int, default=DEFAULT_TOPK_SIM, help="Top-K candidates for similarity fallback")

    args = ap.parse_args()

    memory, sim_index, embedder = build_memory(
        args.input,
        args.format,
        args.buckets,
        args.heads,
        args.similarity,
    )

    gater = EngramGater(temperature=args.gate_temp, bias=args.gate_bias, min_alpha=args.gate_min)

    demo(memory, sim_index, embedder, gater)


if __name__ == "__main__":
    main()
