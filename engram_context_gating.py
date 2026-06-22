#!/usr/bin/env python3
"""engram_context_gating.py

Engram-style gating with *real context embeddings*.

Key upgrade in this version:
  - If the provided --context file is JSON, we build a *compressed event narrative*
    (identity-free) and embed THAT text.
  - That context embedding is then used as the gating Query vector.

Context-aware gating idea (Engram): compute a scalar gate via normalized dot
product + sigmoid between a context-derived Query and memory Keys, suppressing
irrelevant/colliding memory. citeturn14search71turn14search72

Embedding sources demonstrated:
  A) Local sentence-transformers embedder (offline)
  B) Ollama embedding API (POST /api/embed) citeturn16search101turn16search103
  C) vLLM OpenAI-compatible embeddings endpoint (/v1/embeddings) citeturn16search96turn16search112

Usage examples:
  # 1) sentence-transformers context embeddings
  python engram_context_gating.py --patterns engram_export.csv --patterns_format csv \
      --context context.json --ctx_source st --st_model all-MiniLM-L6-v2

  # 2) Ollama context embeddings
  python engram_context_gating.py --patterns engram_export.csv --patterns_format csv \
      --context context.json --ctx_source ollama --ollama_model mxbai-embed-large

  # 3) vLLM context embeddings
  python engram_context_gating.py --patterns engram_export.csv --patterns_format csv \
      --context context.json --ctx_source vllm --vllm_base_url http://localhost:8000 \
      --vllm_model <MODEL_ID>

Note:
- This is a wiring demo. In your production pipeline you should load your persisted
  EngramMemory (hash tables + stored embeddings) rather than building it ad-hoc.
"""

import argparse
import csv
import json
import hashlib
from typing import List, Dict, Tuple, Optional

import numpy as np

# Optional deps for remote embedding sources
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    from sentence_transformers import SentenceTransformer
    ST_AVAILABLE = True
except ImportError:
    ST_AVAILABLE = False


# -------------------------
# Basic utilities
# -------------------------

def normalize_engram_key(key: str) -> str:
    return key.lower().replace(" ", "").replace("→", "->")


def is_valid_ngram(key: str) -> bool:
    return key.count("->") in (0, 1, 2)


def hash_key(key: str, seed: int, buckets: int) -> int:
    h = hashlib.blake2b(f"{seed}:{key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % buckets


def l2_normalize(x: np.ndarray) -> np.ndarray:
    x = x.astype(np.float32)
    n = np.linalg.norm(x) + 1e-12
    return x / n


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


# -------------------------
# Minimal Engram memory
# -------------------------

class EngramMemory:
    def __init__(self, buckets: int = 200_000, heads: int = 2):
        self.buckets = buckets
        self.heads = heads
        self.tables: List[Dict[int, np.ndarray]] = [{} for _ in range(heads)]

    def add(self, key: str, emb: np.ndarray):
        for h in range(self.heads):
            idx = hash_key(key, h, self.buckets)
            self.tables[h][idx] = emb

    def lookup(self, key: str) -> List[np.ndarray]:
        hits = []
        for h in range(self.heads):
            idx = hash_key(key, h, self.buckets)
            v = self.tables[h].get(idx)
            if v is not None:
                hits.append(v)
        return hits


# -------------------------
# Engram-style gater
# -------------------------

class EngramGater:
    def __init__(self, temperature: float = 0.7, bias: float = 0.0, min_alpha: float = 0.25):
        self.temperature = float(max(1e-6, temperature))
        self.bias = float(bias)
        self.min_alpha = float(min_alpha)

    def gate(self, query_emb: np.ndarray, mem_embs: List[np.ndarray]) -> Tuple[np.ndarray, List[float]]:
        if not mem_embs:
            return np.zeros_like(query_emb), []

        M = np.vstack(mem_embs).astype(np.float32)    # (m, d)
        q = query_emb.astype(np.float32)              # (d,)

        # cosine similarities if normalized
        sims = M @ q
        alphas = sigmoid(sims / self.temperature + self.bias)
        alphas = np.where(alphas >= self.min_alpha, alphas, 0.0)

        s = float(alphas.sum())
        if s <= 1e-9:
            return np.zeros_like(q), [float(a) for a in alphas]

        fused = (alphas.reshape(-1, 1) * M).sum(axis=0) / s
        fused = l2_normalize(fused)
        return fused, [float(a) for a in alphas]


# -------------------------
# Context embedding sources
# -------------------------

def embed_with_sentence_transformers(text: str, model: str) -> np.ndarray:
    if not ST_AVAILABLE:
        raise RuntimeError("sentence-transformers not installed")
    embedder = SentenceTransformer(model)
    emb = embedder.encode(text, normalize_embeddings=True)
    return np.asarray(emb, dtype=np.float32)


def embed_with_ollama(text: str, model: str, host: str) -> np.ndarray:
    """Use Ollama's recommended embedding endpoint: POST /api/embed. citeturn16search101turn16search103"""
    if not HTTPX_AVAILABLE:
        raise RuntimeError("httpx not installed")

    url = host.rstrip("/") + "/api/embed"
    payload = {"model": model, "input": text}
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    emb = np.asarray(data["embeddings"][0], dtype=np.float32)
    return l2_normalize(emb)


def embed_with_vllm_openai(text: str, model: str, base_url: str, api_key: str = "EMPTY") -> np.ndarray:
    """Use vLLM OpenAI-compatible embeddings endpoint via OpenAI client. citeturn16search96turn16search112"""
    if not OPENAI_AVAILABLE:
        raise RuntimeError("openai python client not installed")

    client = OpenAI(base_url=base_url.rstrip("/") + "/v1", api_key=api_key)
    resp = client.embeddings.create(input=[text], model=model)
    emb = np.asarray(resp.data[0].embedding, dtype=np.float32)
    return l2_normalize(emb)


# -------------------------
# Compressed event narrative
# -------------------------

def build_compressed_event_narrative(window: dict) -> str:
    """Build an identity-free, compressed narrative for a time window.

    Expected (flexible) JSON fields:
      - time_window: str
      - top_sequences: list[{key|engram_key, count}]
      - rare_sequences: list[{key|engram_key, count}]
      - event_distribution: dict[eventid -> count]
      - host_roles: dict[role -> count]

    This narrative is designed to be embedding-friendly.
    """

    parts: List[str] = []

    if isinstance(window, dict) and window.get("time_window"):
        parts.append(f"Analysis window: {window['time_window']}.")

    host_roles = window.get("host_roles") if isinstance(window, dict) else None
    if isinstance(host_roles, dict) and host_roles:
        role_desc = ", ".join(f"{count} {role.replace('_', ' ')}" for role, count in host_roles.items())
        parts.append(f"Observed environment includes {role_desc}.")

    top_seqs = window.get("top_sequences") if isinstance(window, dict) else None
    if isinstance(top_seqs, list) and top_seqs:
        parts.append("Dominant event flows were observed.")
        for seq in top_seqs[:5]:
            k = seq.get("key") or seq.get("engram_key")
            c = seq.get("count")
            if k is None:
                continue
            if c is None:
                parts.append(f"The sequence {k} occurred frequently.")
            else:
                parts.append(f"The sequence {k} occurred frequently ({c} times).")

    rare_seqs = window.get("rare_sequences") if isinstance(window, dict) else None
    if isinstance(rare_seqs, list) and rare_seqs:
        parts.append("Rare or unusual behavior was detected.")
        for seq in rare_seqs[:5]:
            k = seq.get("key") or seq.get("engram_key")
            c = seq.get("count")
            if k is None:
                continue
            if c is None:
                parts.append(f"The sequence {k} appeared rarely.")
            else:
                parts.append(f"The sequence {k} appeared only {c} time.")

    event_dist = window.get("event_distribution") if isinstance(window, dict) else None
    if isinstance(event_dist, dict) and event_dist:
        dominant = sorted(event_dist.items(), key=lambda x: x[1], reverse=True)[:3]
        parts.append(
            "Most common event types were " + ", ".join(f"event {eid}" for eid, _ in dominant) + "."
        )

    # fallback if JSON didn't contain expected structure
    if not parts:
        return json.dumps(window, ensure_ascii=False)

    return " ".join(parts)


# -------------------------
# Context loader
# -------------------------

def load_context_text(path: str) -> str:
    """Accept either:
    - plain text file, OR
    - JSON summary file -> compressed event narrative
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()

    # Try JSON first
    try:
        obj = json.loads(raw)
    except Exception:
        return raw

    # If JSON is a list, wrap as dict
    if isinstance(obj, list):
        obj = {"top_sequences": obj}

    return build_compressed_event_narrative(obj)


# -------------------------
# Load pattern keys
# -------------------------

def load_keys_csv(path: str) -> List[str]:
    keys = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            k = row.get("engram_key")
            if k:
                keys.append(k)
    return keys


def load_keys_json(path: str) -> List[str]:
    keys = []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for row in data:
        if isinstance(row, dict) and row.get("engram_key"):
            keys.append(row["engram_key"])
    return keys


# -------------------------
# Demo pipeline
# -------------------------

def main():
    ap = argparse.ArgumentParser("Engram gating with compressed event narrative context")
    ap.add_argument("--patterns", required=True, help="CSV/JSON with engram_key")
    ap.add_argument("--patterns_format", choices=["csv", "json"], required=True)

    ap.add_argument("--context", required=True, help="Path to context text or JSON summary")

    # context embedding source
    ap.add_argument("--ctx_source", choices=["st", "ollama", "vllm"], default="st")

    # sentence-transformers
    ap.add_argument("--st_model", default="all-MiniLM-L6-v2")

    # ollama
    ap.add_argument("--ollama_host", default="http://localhost:11434")
    ap.add_argument("--ollama_model", default="mxbai-embed-large")

    # vllm
    ap.add_argument("--vllm_base_url", default="http://localhost:8000")
    ap.add_argument("--vllm_model", default=None, help="Model id served by vLLM")
    ap.add_argument("--vllm_api_key", default="EMPTY")

    # gating params
    ap.add_argument("--gate_temp", type=float, default=0.7)
    ap.add_argument("--gate_bias", type=float, default=0.0)
    ap.add_argument("--gate_min", type=float, default=0.25)

    args = ap.parse_args()

    # 1) load / build narrative context text
    ctx_text = load_context_text(args.context)

    # 2) compute context embedding
    if args.ctx_source == "st":
        ctx_emb = embed_with_sentence_transformers(ctx_text, args.st_model)
    elif args.ctx_source == "ollama":
        ctx_emb = embed_with_ollama(ctx_text, args.ollama_model, args.ollama_host)
    else:
        if args.vllm_model is None:
            raise RuntimeError("--vllm_model is required when --ctx_source vllm")
        ctx_emb = embed_with_vllm_openai(ctx_text, args.vllm_model, args.vllm_base_url, args.vllm_api_key)

    ctx_emb = l2_normalize(ctx_emb)

    # 3) load patterns and build a tiny demo memory (replace with your persisted memory)
    raw_keys = load_keys_csv(args.patterns) if args.patterns_format == "csv" else load_keys_json(args.patterns)
    keys = []
    for rk in raw_keys:
        k = normalize_engram_key(rk)
        if is_valid_ngram(k):
            keys.append(k)

    if not ST_AVAILABLE:
        raise RuntimeError("sentence-transformers is required in this demo to build memory embeddings")

    embedder = SentenceTransformer(args.st_model)
    mem = EngramMemory(buckets=200_000, heads=2)

    for k in set(keys[:5000]):
        emb = np.asarray(embedder.encode(k, normalize_embeddings=True), dtype=np.float32)
        mem.add(k, emb)

    gater = EngramGater(temperature=args.gate_temp, bias=args.gate_bias, min_alpha=args.gate_min)

    # 4) show gating with *context embedding* (the key change)
    print("\n=== Compressed event narrative (truncated) ===")
    print((ctx_text[:900] + "...") if len(ctx_text) > 900 else ctx_text)

    print("\n=== Gating results (hash hits) ===")
    for test in keys[:10]:
        hits = mem.lookup(test)
        fused, alphas = gater.gate(ctx_emb, hits)  # <-- real context embedding used as Query
        print(json.dumps({
            "pattern": test,
            "hits": len(hits),
            "alphas": alphas,
            "fused_norm": float(np.linalg.norm(fused)),
        }, indent=2))


if __name__ == "__main__":
    main()
