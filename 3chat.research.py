import os
import io
import re
import chromadb
import numpy as np

from sentence_transformers import SentenceTransformer

import requests
import json
import uuid

import time
import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from ui import (
    render_user_message,
    render_assistant_message,
    render_memory_block,
    render_system_message)
    
from dataclasses import dataclass, field
from typing import Dict
from ddgs import DDGS

# Default values (will be overwritten by load_state if state.json exists)
current_prompt_path = "default.md"
current_greeting_path = "default.txt"
SHOW_MEMORY = False
active_lorebook = None

# Constants
OLLAMA_URL = "http://localhost:11434"
LM_STUDIO_URL = "http://localhost:1234/v1"
#MODEL = "qwen2.5:14b-instruct-q6_K"
MODEL = "google/gemma-4-26b-a4b-qat"
APP_SETTINGS_FILE = "app_settings.json"

def load_app_settings():
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"char_name": "MEP", "user_name": "Klaus"}

def save_app_settings(char_name, user_name):
    with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"char_name": char_name, "user_name": user_name}, f, indent=4)

_settings = load_app_settings()
CHAR_NAME = _settings.get("char_name", "MEP")
USER_NAME = _settings.get("user_name", "Klaus")

STATE_FILE = f"state_{CHAR_NAME}_{USER_NAME}.json"
DBPATH = f"./chroma_db_{CHAR_NAME}_{USER_NAME}"
HISTORY_FILE = f"history_{CHAR_NAME}_{USER_NAME}.json"
SIMILARITY_THRESHOLD = 0.35
SIM_THRESHOLD = 0.70
IMP_THRESHOLD = 0.60
URL_CONTEXT_LIMIT = 12000
INGEST_CHUNK_CHARS = 1200
INGEST_CHUNK_OVERLAP = 160
turn = 0

class PageTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "div", "section", "article", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg") and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in ("p", "div", "section", "article", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self.title = f"{self.title} {cleaned}".strip()
        self.parts.append(cleaned)

    def text(self):
        raw = " ".join(self.parts)
        raw = re.sub(r"\s+\n", "\n", raw)
        raw = re.sub(r"\n\s+", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r"[ \t]{2,}", " ", raw)
        return raw.strip()

@dataclass
class AffectState:
    curiosity: float = 0.0
    concern: float = 0.0
    calm: float = 1.0
    focus: float = 0.5
    amusement: float = 0.0
    unease: float = 0.0
    trust: float = 0.3
    warmth: float = 0.3
    vulnerability: float = 0.1

    def as_dict(self) -> Dict[str, float]:
        return {
            "curiosity": self.curiosity,
            "concern": self.concern,
            "calm": self.calm,
            "focus": self.focus,
            "amusement": self.amusement,
            "unease": self.unease,
            "trust": self.trust,
            "warmth": self.warmth,
            "vulnerability": self.vulnerability,
        }

@dataclass
class RelationshipState:
    familiarity: float = 0.1
    closeness: float = 0.1
    boundary_sensitivity: float = 0.5
    repair_need: float = 0.0
    shared_humor: float = 0.0
    preferred_detail: str = "balanced"
    preferred_mode: str = "adaptive"
    last_user_tone: str = "neutral"

    def as_dict(self) -> Dict[str, object]:
        return {
            "familiarity": self.familiarity,
            "closeness": self.closeness,
            "boundary_sensitivity": self.boundary_sensitivity,
            "repair_need": self.repair_need,
            "shared_humor": self.shared_humor,
            "preferred_detail": self.preferred_detail,
            "preferred_mode": self.preferred_mode,
            "last_user_tone": self.last_user_tone,
        }

AFFECT = AffectState()
RELATIONSHIP = RelationshipState()

embedder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

def embed(text: str) -> list[float]:
    # Chroma expects list[list[float]]; we’ll wrap later
    return embedder.encode(text).tolist()

AFFECT_CONCEPTS = {
    "unease": embed("threat danger attack intrusion overrun"),
    "curiosity": embed("how why explain curious what if"),
    "amusement": embed("funny joke laugh absurd"),
    "trust": embed("thank you reliable honest ally"),
    "concern": embed("worried scared upset hurt exhausted overwhelmed"),
    "warmth": embed("kind gentle care appreciate close welcome"),
    "focus": embed("precise plan solve implement analyze"),
}

def clamp(value: float, low: float = 0.0, high: float = 1.5) -> float:
    return max(low, min(high, value))

def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)

def prompt_section(section_name: str, prompt: str | None = None) -> str:
    source = prompt if prompt is not None else globals().get("SYSTEM_PROMPT", "")
    if not source:
        return ""
    pattern = rf"\[{re.escape(section_name)}\]\s*(.*?)(?=\n\[[A-Z0-9 _-]+\]|\Z)"
    match = re.search(pattern, source, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""

def infer_user_tone(user_input: str) -> str:
    text = user_input.lower().strip()
    repair_markers = (
        "not what i meant",
        "that's not what i meant",
        "that is not what i meant",
        "you misunderstood",
        "wrong",
        "no,",
        "actually",
        "wait",
        "stop",
    )
    distress_markers = (
        "worried",
        "scared",
        "afraid",
        "sad",
        "upset",
        "angry",
        "frustrated",
        "overwhelmed",
        "tired",
        "exhausted",
    )
    appreciative_markers = ("thank you", "thanks", "appreciate", "that helps")
    playful_markers = ("haha", "lol", "funny", "joke", ";)", ":)")
    curious_markers = ("why", "how", "what if", "explain", "?")

    if contains_any(text, repair_markers):
        return "repair"
    if contains_any(text, distress_markers):
        return "distressed"
    if contains_any(text, appreciative_markers):
        return "appreciative"
    if contains_any(text, playful_markers):
        return "playful"
    if contains_any(text, curious_markers):
        return "curious"
    if len(text.split()) <= 5:
        return "terse"
    return "neutral"

def update_relationship_preferences(user_input: str):
    text = user_input.lower()

    if contains_any(text, ("shorter", "brief", "concise", "too long", "less detail")):
        RELATIONSHIP.preferred_detail = "concise"
    elif contains_any(text, ("more detail", "go deeper", "expand", "thorough", "step by step")):
        RELATIONSHIP.preferred_detail = "detailed"

    if contains_any(text, ("in character", "stay in character", "roleplay")):
        RELATIONSHIP.preferred_mode = "immersive"
    elif contains_any(text, ("out of character", "ooc", "technical", "plainly", "directly")):
        RELATIONSHIP.preferred_mode = "direct"
    elif contains_any(text, ("gentle", "soft", "careful", "sensitive")):
        RELATIONSHIP.preferred_mode = "gentle"

def update_relationship_from_input(user_input: str):
    tone = infer_user_tone(user_input)
    RELATIONSHIP.last_user_tone = tone
    RELATIONSHIP.familiarity = clamp(RELATIONSHIP.familiarity + 0.01, high=1.0)
    RELATIONSHIP.repair_need = clamp(RELATIONSHIP.repair_need * 0.85, high=1.0)
    update_relationship_preferences(user_input)

    if tone == "appreciative":
        RELATIONSHIP.closeness = clamp(RELATIONSHIP.closeness + 0.04, high=1.0)
        AFFECT.trust += 0.08
        AFFECT.warmth += 0.10
    elif tone == "playful":
        RELATIONSHIP.shared_humor = clamp(RELATIONSHIP.shared_humor + 0.06, high=1.0)
        AFFECT.amusement += 0.15
        AFFECT.warmth += 0.04
    elif tone == "distressed":
        RELATIONSHIP.boundary_sensitivity = clamp(RELATIONSHIP.boundary_sensitivity + 0.07, high=1.0)
        AFFECT.concern += 0.20
        AFFECT.warmth += 0.08
        AFFECT.focus += 0.05
    elif tone == "repair":
        RELATIONSHIP.repair_need = clamp(RELATIONSHIP.repair_need + 0.55, high=1.0)
        RELATIONSHIP.boundary_sensitivity = clamp(RELATIONSHIP.boundary_sensitivity + 0.08, high=1.0)
        AFFECT.trust -= 0.08
        AFFECT.calm -= 0.05
        AFFECT.focus += 0.12
    elif tone == "curious":
        RELATIONSHIP.closeness = clamp(RELATIONSHIP.closeness + 0.01, high=1.0)

    for k, v in AFFECT.as_dict().items():
        setattr(AFFECT, k, clamp(v))

def update_affect_semantic(user_input: str):
    vec = embed(user_input)
    for affect_name, concept_vec in AFFECT_CONCEPTS.items():
        sim = cosine_similarity(vec, concept_vec)
        if sim > 0.45:
            setattr(AFFECT, affect_name, min(1.5, getattr(AFFECT, affect_name) + sim * 0.3))

def affect_to_narrative_instruction() -> str:
    a = AFFECT.as_dict()
    r = RELATIONSHIP.as_dict()
    parts = []
    if a["unease"] > 0.6:
        parts.append("Let subtle tension show through careful wording and slight hesitation.")
    if a["curiosity"] > 0.7:
        parts.append("Let genuine interest show, but ask at most one focused follow-up question.")
    if a["warmth"] > 0.55 or r["closeness"] > 0.45:
        parts.append("Allow modest warmth and recognition of shared context without becoming sentimental.")
    if a["trust"] < 0.2:
        parts.append("Be guarded; deflect personal questions; don't volunteer information.")
    if a["calm"] < 0.3:
        parts.append("Your composure is fraying; use shorter sentences and less formality.")
    if r["repair_need"] > 0.35:
        parts.append("Prioritize repair: acknowledge the correction briefly, adjust course, and avoid defensiveness.")
    if r["last_user_tone"] == "distressed":
        parts.append("Slow down, validate the pressure, and make the next step feel manageable.")
    if r["last_user_tone"] == "terse":
        parts.append("Match the user's brevity; do not over-explain.")
    if r["preferred_detail"] == "concise":
        parts.append("Keep the answer compact unless the user asks for depth.")
    elif r["preferred_detail"] == "detailed":
        parts.append("Use more complete reasoning and concrete steps.")
    if r["preferred_mode"] == "direct":
        parts.append("Favor plain, out-of-character clarity over performance.")
    elif r["preferred_mode"] == "immersive":
        parts.append("Stay embodied and in character while preserving factual honesty.")
    elif r["preferred_mode"] == "gentle":
        parts.append("Use a gentler cadence and avoid sharp corrections.")
    return " ".join(parts) or "Maintain your usual composure with natural conversational pacing."

def relationship_context_line() -> str:
    r = RELATIONSHIP.as_dict()
    return (
        f"relationship familiarity={r['familiarity']:.2f}, "
        f"closeness={r['closeness']:.2f}, "
        f"repair_need={r['repair_need']:.2f}, "
        f"last_user_tone={r['last_user_tone']}, "
        f"preferred_detail={r['preferred_detail']}, "
        f"preferred_mode={r['preferred_mode']}"
    )

def conversational_behavior_directive(user_input: str) -> str:
    tone = infer_user_tone(user_input)
    repair_line = (
        "- Repair First: The user signaled correction or friction. Briefly acknowledge it, then answer the intended request."
        if RELATIONSHIP.repair_need > 0.35 or tone == "repair"
        else "- Repair Awareness: If the user sounds dissatisfied or correcting you, adapt without defensiveness."
    )
    pacing_line = {
        "distressed": "- Pacing: Slow the cadence, reduce cognitive load, and offer one manageable next step.",
        "playful": "- Pacing: Allow light humor if it serves the moment, then keep moving.",
        "terse": "- Pacing: Answer tightly and leave room for the user to expand.",
        "curious": "- Pacing: Explore the idea with one sharp follow-up or a concrete next step.",
    }.get(tone, "- Pacing: Match the user's energy instead of forcing intensity.")

    return f"""
[HUMAN-LIKE CONVERSATION DIRECTIVE]
- Continuity: Treat remembered emotional context as meaningful, not just factual.
- Restraint: Do not over-narrate, over-apologize, or force a dramatic event when a quiet answer fits better.
- Specificity: Refer to concrete details from the user's message before generalizing.
{repair_line}
{pacing_line}
- Curiosity: Ask no more than one follow-up question unless the user explicitly invites exploration.
- Boundaries: If uncertainty, discomfort, or high-stakes factual claims appear, slow down and make uncertainty visible.
""".strip()

def update_affect_from_input(user_input: str):

    update_affect_semantic(user_input)
    text = user_input.lower()

    # Baseline decay toward neutral
    AFFECT.curiosity *= 0.9
    AFFECT.concern *= 0.9
    AFFECT.calm = min(1.0, AFFECT.calm * 0.95 + 0.05)
    AFFECT.unease *= 0.9
    AFFECT.amusement *= 0.9
    AFFECT.focus = AFFECT.focus * 0.92 + 0.5 * 0.08
    AFFECT.trust = AFFECT.trust * 0.98 + 0.3 * 0.02
    AFFECT.warmth *= 0.95
    AFFECT.vulnerability *= 0.90

    # Triggers
    if "intrusion" in text or "system compromise" in text:
        AFFECT.unease += 0.2
        AFFECT.calm -= 0.1

    if "danger" in text or "threat" in text or "attack" in text:
        AFFECT.concern += 0.3
        AFFECT.calm -= 0.2
        AFFECT.focus += 0.2

    if "thank you" in text or "thanks" in text:
        AFFECT.trust += 0.1
        AFFECT.calm += 0.05
        AFFECT.warmth += 0.08

    if "joke" in text or "funny" in text:
        AFFECT.amusement += 0.2
        AFFECT.warmth += 0.03

    if "explain" in text or "how" in text or "why" in text:
        AFFECT.curiosity += 0.2
        AFFECT.focus += 0.1

    if contains_any(text, ("sorry", "i was wrong", "my mistake")):
        AFFECT.vulnerability += 0.10
        AFFECT.warmth += 0.06

    if contains_any(text, ("gentle", "careful", "sensitive", "hard for me")):
        AFFECT.concern += 0.12
        AFFECT.warmth += 0.10

    # Clamp values
    for k, v in AFFECT.as_dict().items():
        setattr(AFFECT, k, clamp(v))

chroma_client = chromadb.PersistentClient(path=f"{DBPATH}")

episodic = chroma_client.get_or_create_collection(
    name="episodic_memory",
    metadata={"hnsw:space": "cosine"}
)

semantic = chroma_client.get_or_create_collection(
    name="semantic_memory",
    metadata={"hnsw:space": "cosine"}
)

procedural = chroma_client.get_or_create_collection(
    name="procedural_memory",
    metadata={"hnsw:space": "cosine"}
)
    
def web_search(query: str, max_results=3):
    """Fetches real-time data from the web using DuckDuckGo."""
    results_text = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results_text.append(f"Source: {r['href']}\nContent: {r['body']}")
        return "\n\n".join(results_text) or "Search returned no results."
    except Exception as e:
        return f"Search failed: {e}"

def extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s<>\]\)\"']+", text)
    return [url.rstrip(".,;:!?") for url in urls]

def is_probably_pdf_url(url: str, content_type: str = "") -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or "application/pdf" in content_type.lower()

def extract_html_text(html: str) -> tuple[str, str]:
    parser = PageTextExtractor()
    parser.feed(html)
    return parser.title, parser.text()

def extract_pdf_text_from_bytes(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF ingestion requires pypdf. Install it with: .venv/bin/python -m pip install pypdf") from exc

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"[Page {index}]\n{text.strip()}")
    return "\n\n".join(pages).strip()

def extract_pdf_text(path: str) -> str:
    with open(path, "rb") as f:
        return extract_pdf_text_from_bytes(f.read())

def fetch_url_content(url: str, timeout=30, max_chars=URL_CONTEXT_LIMIT) -> dict:
    headers = {
        "User-Agent": "ENGRAM-PoC/0.1 (+local research assistant)"
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")

    if is_probably_pdf_url(url, content_type):
        text = extract_pdf_text_from_bytes(response.content)
        title = os.path.basename(urlparse(url).path) or url
        kind = "pdf"
    else:
        response.encoding = response.encoding or "utf-8"
        title, text = extract_html_text(response.text)
        title = title or url
        kind = "html"

    if not text:
        text = "No extractable text was found."

    return {
        "url": url,
        "title": title,
        "content_type": content_type,
        "kind": kind,
        "text": text[:max_chars],
        "full_text_length": len(text),
    }

def chunk_text(text: str, chunk_chars=INGEST_CHUNK_CHARS, overlap=INGEST_CHUNK_OVERLAP) -> list[str]:
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not cleaned:
        return []
    chunks = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_chars)
        window = cleaned[start:end]
        if end < len(cleaned):
            split_at = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if split_at > chunk_chars * 0.55:
                end = start + split_at + 1
                window = cleaned[start:end]
        chunks.append(window.strip())
        if end >= len(cleaned):
            break
        start = max(0, end - overlap)
    return [chunk for chunk in chunks if chunk]

def ingest_knowledge_text(text: str, source: str, metadata=None) -> dict:
    chunks = chunk_text(text)
    meta_base = dict(metadata) if metadata else {}
    stored = 0
    for index, chunk in enumerate(chunks):
        meta = {
            **meta_base,
            "source": source,
            "type": "knowledge_ingest",
            "chunk_index": index,
            "chunk_count": len(chunks),
            "timestamp": time.time(),
        }
        store_semantic(chunk, embed(chunk), metadata=meta)
        stored += 1
    return {"source": source, "chunks": stored, "characters": len(text)}

def ingest_url(url: str) -> dict:
    fetched = fetch_url_content(url, max_chars=250000)
    result = ingest_knowledge_text(
        fetched["text"],
        source=f"url:{url}",
        metadata={
            "url": url,
            "title": fetched["title"],
            "content_type": fetched["content_type"],
            "document_kind": fetched["kind"],
            "full_text_length": fetched["full_text_length"],
        }
    )
    return {**result, "title": fetched["title"], "kind": fetched["kind"]}

def ingest_pdf(path: str) -> dict:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PDF not found: {path}")
    text = extract_pdf_text(path)
    if not text:
        raise ValueError("No extractable text found in PDF.")
    return ingest_knowledge_text(
        text,
        source=f"pdf:{path}",
        metadata={
            "path": path,
            "title": os.path.basename(path),
            "document_kind": "pdf",
        }
    )

def generate_ollama(system_msg: str, user_input:str, model: str = MODEL) -> str:
    response = requests.post(
        OLLAMA_URL+"/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_input}
            ],
            "stream": False,
        }
    )
    data = response.json()
    return data['message']['content']
    
def generate(system_msg: str, user_input: str, model: str = MODEL) -> str:
    try:
        response = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_input}
                ],
                "temperature": 0.7,
                "stream": False,
            },
            timeout=300 # Local LLMs can take time to respond
        )
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        return f"Error connecting to LM Studio: {e}"

def store_episodic(text: str, vector: list[float], metadata=None):
    meta = dict(metadata) if metadata else {}
    # Use a real timestamp for sorting
    ts = time.time()
    meta.setdefault("timestamp", ts)
    
    readable_time = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    meta.setdefault("datestring", readable_time)

    episodic.add(
        ids=[str(uuid.uuid4())],
        documents=[text],
        embeddings=[vector],
        metadatas=[meta]
    )

def store_semantic(text: str, vector: list[float], metadata=None):
    semantic.add(
        ids=[str(uuid.uuid4())],
        documents=[text],
        embeddings=[vector],
        metadatas=[metadata or {}]
    )

def store_procedural(text: str, vector: list[float], metadata=None):
    procedural.add(
        ids=[str(uuid.uuid4())],
        documents=[text],
        embeddings=[vector],
        metadatas=[metadata or {"type": "procedure"}]
    )

def ingest_document(text: str):
    procedures = extract_procedures_from_text(text)
    for proc in procedures:
        store_procedural(proc, embed(proc), metadata={"source": "document"})

def load_lorebook(name: str, base_path="lorebooks"):
    # Use a real timestamp for sorting
    ts = time.time()
    readable_time = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    
    loaded = {
        "semantic": 0,
        "procedural": 0,
        "episodic": 0
    }

    # Support for direct JSON lorebook imports
    if name.endswith(".json"):
        path = os.path.join(base_path, name)
        if not os.path.isfile(path):
            path = name # Fallback to current directory
            
        if not os.path.isfile(path):
            return f"JSON Lorebook '{name}' not found."
            
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            for item in data:
                content = item.get("content", "").strip()
                if content:
                    store_semantic(content, embed(content), metadata={"source": f"lorebook:{name}"})
                    loaded["semantic"] += 1
            return loaded
        except Exception as e:
            return f"Error loading JSON lorebook: {e}"

    path = os.path.join(base_path, name)

    if not os.path.isdir(path):
        return f"Lorebook directory '{name}' not found."

    # Load semantic memory
    sem_path = os.path.join(path, "semantic")
    if os.path.isdir(sem_path):
        for file in os.listdir(sem_path):
            full = os.path.join(sem_path, file)
            if os.path.isfile(full):
                text = open(full, "r", encoding="utf-8").read()
                store_semantic(text, embed(text), metadata={"source": f"lorebook:{name}"})
                loaded["semantic"] += 1

    # Load procedural memory
    proc_path = os.path.join(path, "procedural")
    if os.path.isdir(proc_path):
        for file in os.listdir(proc_path):
            full = os.path.join(proc_path, file)
            if os.path.isfile(full):
                text = open(full, "r", encoding="utf-8").read()
                store_procedural(text, embed(text), metadata={"source": f"lorebook:{name}"})
                loaded["procedural"] += 1

    # Load episodic memory
    epi_path = os.path.join(path, "episodic")
    if os.path.isdir(epi_path):
        for file in os.listdir(epi_path):
            full = os.path.join(epi_path, file)
            if os.path.isfile(full):
                text = open(full, "r", encoding="utf-8").read()
                store_episodic(text, embed(text), metadata={"source": f"lorebook:{name}", "turn": 0, "datestring":f"{readable_time}"})
                loaded["episodic"] += 1

    return loaded
    
def unload_lorebook(name: str):
    tag = f"lorebook:{name}"

    for collection in [semantic, procedural, episodic]:
        results = collection.get()
        ids = []
        for i, meta in enumerate(results["metadatas"]):
            if meta and meta.get("source") == tag:
                ids.append(results["ids"][i])
        if ids:
            collection.delete(ids=ids)

def recall_episodic(query: str, embed_fn, top_k=5):
    # Check if there is actually anything to query
    count = episodic.count()
    if count == 0:
        return []
    
    # Adjust k if you have fewer items than requested
    actual_k = min(count, top_k)
    
    qvec = embed_fn(query)
    result = episodic.query(
        query_embeddings=[qvec],
        n_results=actual_k,
        include=["documents", "metadatas"]
    )
    
    combined = []
    if result["documents"] and result["documents"][0]:
        for doc, meta in zip(result["documents"][0], result["metadatas"][0]):
            combined.append({"text": doc, "meta": meta})
    return combined

def recall_semantic(query: str, embed_fn, top_k=5):
    qvec = embed_fn(query)
    result = semantic.query(
        query_embeddings=[qvec],
        n_results=top_k,
        include=["documents", "embeddings"]
    )

    docs = result["documents"][0]
    embs = result["embeddings"][0]

    filtered = []
    for doc, emb in zip(docs, embs):
        sim = cosine_similarity(qvec, emb)
        if sim >= SIMILARITY_THRESHOLD:
            filtered.append(doc)

    return filtered

def recall_procedural(query: str, embed_fn, top_k=5):
    qvec = embed_fn(query)
    result = procedural.query(
        query_embeddings=[qvec],
        n_results=top_k
    )
    return result["documents"][0] if result["documents"] else []

def consolidate_to_semantic(text: str):
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    summary_prompt = f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]
Summarize the following into a single, concise general fact. Return ONLY the fact.
"""
    summary = generate(summary_prompt, text)
    store_semantic(summary, embed(summary), metadata={"type": "summary"})

def extract_procedures_from_text(text: str) -> list[str]:
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    prompt = f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]

Extract all actionable procedures, step-by-step instructions, or skills from the following text.
Return each procedure as a separate bullet point.
"""
    raw = generate(prompt, text)
    procedures = [p.strip("- ").strip() for p in raw.split("\n") if p.strip()]
    return procedures

def cosine_similarity(a, b):
    a = np.array(a)
    b = np.array(b)
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def semantic_similarity_score(text: str, embed_fn, top_k=5):
    vec = embed_fn(text)

    result = semantic.query(
        query_embeddings=[vec],
        n_results=top_k
    )

    if not result["embeddings"] or not result["embeddings"][0]:
        return 0.0

    sims = [
        cosine_similarity(vec, emb)
        for emb in result["embeddings"][0]
    ]

    return max(sims) if sims else 0.0

def should_store_semantic(text: str, embed_fn) -> bool:
    sim = semantic_similarity_score(text, embed_fn)
    imp = importance_score(text)

    return (sim >= SIM_THRESHOLD) or (imp >= IMP_THRESHOLD)

def importance_score(text: str) -> float:
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    prompt = f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]

You are evaluating whether the following information is important enough
to store as long-term semantic memory for an AI assistant.

Rate the importance on a scale from 0.0 to 1.0.
Return ONLY a number, no explanation.
"""

    raw = generate(prompt, text)
    try:
        return float(raw.strip())
    except:
        return 0.0

def reflect_on_reply(reply: str) -> str:
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    prompt = f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]

{SYSTEM_PROMPT}

Initiate semantic-memory extraction protocol.

Analyze the following output you previously generated.
Identify any stable facts, general insights, or reusable knowledge suitable for long-term semantic memory.

Return ONLY:
• a single distilled insight, OR
• "none" if no semantic memory should be stored.

Do not break character.
"""
    reflection = generate(prompt, reply).strip()
    return reflection

def reflection_importance(text: str) -> float:
    return importance_score(text)

def process_reflection(reflection: str):
    if reflection.lower() == "none":
        return False

    score = reflection_importance(reflection)

    if score >= 0.6:
        store_semantic(
            reflection,
            embed(reflection),
            metadata={"type": "reflection"}
        )
        return True

    return False

def self_critique(user_input: str, reply: str, verification_context: str = "") -> str:
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    # 1. The System Message establishes the "Persona" and the "Context"
    system_message = f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]

{SYSTEM_PROMPT}

[INTERNAL PROTOCOL ACTIVE: You are now in Self-Critique mode] 
Your goal is to ensure all outputs strictly adhere to your identity as {CHAR_NAME} and remain factually accurate."""

    if verification_context.strip():
        context_block = f"""
Available verification and memory context used for the original answer:
{verification_context}

When improving the reply, preserve facts grounded in this context. Do not add factual claims unless they are supported by this context or the conversation.
"""
    else:
        context_block = """
No additional verification context was available for this exchange. If the answer depends on external or current facts, the improved reply should say what is unverified or ask a focused follow-up.
"""

    # 2. The User Message defines the specific "Task" and the "Data"
    user_content = f"""
Task: Evaluate the following exchange for quality, character consistency, and accuracy.

{context_block}

{USER_NAME}'s message: 
"{user_input}"

{CHAR_NAME}'s reply: 
"{reply}"

Identify any issues (hallucinations, logic errors, or character breaks). 
Then, provide an improved version of the reply that is 100% in-character as {CHAR_NAME}.

Return your answer in this EXACT format. Do NOT add any parenthetical notes, meta-commentary, or descriptions of your changes inside the IMPROVED section.
CRITIQUE: <your critique>
IMPROVED: <your improved reply ONLY>
"""
    return generate(system_message, user_content).strip()

def parse_critique(text: str):
    critique = ""
    improved = ""

    if "CRITIQUE:" in text:
        critique = text.split("CRITIQUE:")[1].split("IMPROVED:")[0].strip()

    if "IMPROVED:" in text:
        improved = text.split("IMPROVED:")[1].strip()

    return critique, improved

def process_self_critique(critique: str, improved: str):
    # Store critique if meaningful
    if critique and importance_score(critique) >= 0.5:
        store_semantic(
            critique,
            embed(critique),
            metadata={"type": "self_critique"}
        )

    # Store improved answer if it’s significantly better
    if improved and importance_score(improved) >= 0.6:
        store_semantic(
            improved,
            embed(improved),
            metadata={"type": "improved_answer"}
        )

def detect_procedure(text: str) -> bool:
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    prompt = f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]
Does the following text describe a procedure, set of steps, or instructions?

Reply only "yes" or "no".
"""
    ans = generate(prompt, text).strip().lower()
    return ans.startswith("y")

def list_procedural_skills():
    results = procedural.get()
    docs = results.get("documents", [])
    metas = results.get("metadatas", [])
    ids = results.get("ids", [])

    skills = []
    for i, doc in enumerate(docs):
        skills.append({
            "id": ids[i],
            "text": doc,
            "meta": metas[i]
        })
    return skills
    
def load_greeting(name="default.txt",base_path="prompts/greetings"):
    try:
        path = os.path.join(base_path, name)
        render_system_message(f"Reading {path}")
        with open(path, "r", encoding="utf-8") as f:
            greeting = f.read().strip()
        return greeting.replace("{{user}}", USER_NAME).replace("{{char}}", CHAR_NAME)
    except:
        return None

def save_state():
    global current_prompt_path, current_greeting_path
    global SHOW_MEMORY, active_lorebook, AFFECT, RELATIONSHIP
    
    state = {
        "current_prompt_path": current_prompt_path,
        "current_greeting_path": current_greeting_path,
        "SHOW_MEMORY": SHOW_MEMORY,
        "active_lorebook": active_lorebook,
        "affect": AFFECT.as_dict(),
        "relationship": RELATIONSHIP.as_dict(),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
        
def load_state():
    global current_prompt_path, current_greeting_path
    global SHOW_MEMORY, active_lorebook, AFFECT, RELATIONSHIP
    global CHAR_NAME, USER_NAME

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return

    current_prompt_path = state.get("current_prompt_path", "default.txt")
    current_greeting_path = state.get("current_greeting_path", "default.txt")
    SHOW_MEMORY = state.get("SHOW_MEMORY", False)
    active_lorebook = state.get("active_lorebook", None)

    # Restore affect state
    aff = state.get("affect", {})
    for k, v in aff.items():
        if hasattr(AFFECT, k):
            setattr(AFFECT, k, v)

    rel = state.get("relationship", {})
    for k, v in rel.items():
        if hasattr(RELATIONSHIP, k):
            setattr(RELATIONSHIP, k, v)
        
def render_diagnostics():
    a = AFFECT.as_dict()
    r = RELATIONSHIP.as_dict()
    lines = [
        f"curiosity: {a['curiosity']:.2f}",
        f"concern: {a['concern']:.2f}",
        f"calm: {a['calm']:.2f}",
        f"focus: {a['focus']:.2f}",
        f"amusement: {a['amusement']:.2f}",
        f"unease: {a['unease']:.2f}",
        f"trust: {a['trust']:.2f}",
        f"warmth: {a['warmth']:.2f}",
        f"vulnerability: {a['vulnerability']:.2f}",
        "",
        f"familiarity: {r['familiarity']:.2f}",
        f"closeness: {r['closeness']:.2f}",
        f"boundary_sensitivity: {r['boundary_sensitivity']:.2f}",
        f"repair_need: {r['repair_need']:.2f}",
        f"shared_humor: {r['shared_humor']:.2f}",
        f"preferred_detail: {r['preferred_detail']}",
        f"preferred_mode: {r['preferred_mode']}",
        f"last_user_tone: {r['last_user_tone']}",
    ]
    render_system_message(f"{CHAR_NAME} expressivity diagnostics:\n" + "\n".join(lines))
    
def handle_command(user_input: str):
    global current_prompt_path, current_greeting_path
    global SHOW_MEMORY, active_lorebook, SYSTEM_PROMPT, GREETING
    global CHAR_NAME, USER_NAME

    if user_input.strip() == "/debug prompt":
        render_system_message("=== DEBUG: ACTIVE SYSTEM PROMPT ===")
        render_system_message(f"Prompt path: {current_prompt_path}")

        if SYSTEM_PROMPT:
            preview = SYSTEM_PROMPT.split("\n")
            head = "\n".join(preview[:20])  # first 20 lines
            tail = "\n".join(preview[-10:]) if len(preview) > 20 else ""

            render_system_message("--- BEGIN PROMPT PREVIEW ---")
            render_system_message(head)

            if tail:
                render_system_message("... (truncated) ...")
                render_system_message(tail)

            render_system_message("--- END PROMPT PREVIEW ---")
        else:
            render_system_message("SYSTEM_PROMPT is EMPTY or None!")

        return

    if user_input.strip() == "/debug state":
        render_system_message("=== DEBUG: CURRENT STATE ===")
        render_system_message(f"current_prompt_path: {current_prompt_path}")
        render_system_message(f"current_greeting_path: {current_greeting_path}")
        render_system_message(f"active_lorebook: {active_lorebook}")
        render_system_message(f"SHOW_MEMORY: {SHOW_MEMORY}")
        render_system_message(f"AFFECT: {AFFECT.as_dict()}")
        render_system_message(f"RELATIONSHIP: {RELATIONSHIP.as_dict()}")
        return

    if user_input.strip() == "/memory on":
        SHOW_MEMORY = True
        render_system_message("Memory display enabled.")
        return

    if user_input.strip() == "/memory off":
        SHOW_MEMORY = False
        render_system_message("Memory display disabled.")
        return

    if user_input.strip() == "/diagnostics":
        render_diagnostics()
        return

    if user_input.startswith("/char "):
        CHAR_NAME = user_input.replace("/char ", "").strip()
        save_app_settings(CHAR_NAME, USER_NAME)
        render_system_message(f"Character name set to: {CHAR_NAME}. Restart the application to connect to the correct database!")
        return

    if user_input.startswith("/user "):
        USER_NAME = user_input.replace("/user ", "").strip()
        save_app_settings(CHAR_NAME, USER_NAME)
        render_system_message(f"User name set to: {USER_NAME}. Restart the application to connect to the correct database!")
        return

    if user_input.startswith("/prompt "):
        name = user_input.replace("/prompt ", "").strip()
        current_prompt_path = name
        raw_prompt = load_system_prompt(name)
        SYSTEM_PROMPT = raw_prompt.replace("{{char}}", CHAR_NAME).replace("{{user}}", USER_NAME)

        render_system_message(f"Loaded system prompt from: prompts/{name}")
        preview = SYSTEM_PROMPT.split("\n")[:5]
        render_system_message("Prompt preview:\n" + "\n".join(preview))
        return

    if user_input.startswith("/greeting set "):
        name = user_input.replace("/greeting set ", "").strip()
        new_greeting = load_greeting(name)

        if new_greeting:
            current_greeting_path = name
            GREETING = new_greeting
            render_system_message(f"Greeting loaded from {name}")
            render_assistant_message(GREETING)
        else:
            render_system_message(f"Failed to load greeting from {name}")
        return

    if user_input.startswith("/lorebook load "):
        name = user_input.replace("/lorebook load ", "").strip()
        result = load_lorebook(name)
        active_lorebook = name
        render_system_message(
            f"Lorebook '{name}' loaded.\n"
            f"Semantic: {result['semantic']} items\n"
            f"Procedural: {result['procedural']} items\n"
            f"Episodic: {result['episodic']} items"
        )
        return

    if user_input.strip() == "/lorebook list":
        books = os.listdir("lorebooks")
        books = [b for b in books if os.path.isdir(os.path.join("lorebooks", b))]
        render_system_message("Available lorebooks:\n" + "\n".join(f"- {b}" for b in books))
        return
        
    if user_input.startswith("/lorebook unload "):
        name = user_input.replace("/lorebook unload ", "").strip()
        unload_lorebook(name)
        render_system_message(f"Lorebook '{name}' unloaded.")
        return

    if user_input.strip() == "/greeting reload":
        GREETING = load_greeting(current_greeting_path)
        render_assistant_message(GREETING)
        return

    if user_input.startswith("/ingest "):
        path = user_input.replace("/ingest ", "").strip()
        try:
            text = load_file(path)
            ingest_document(text)
            render_system_message(f"Loaded {path} into procedural memory.")
        except Exception as e:
            render_system_message(f"Error: {e}")
        return

    if user_input.startswith("/url fetch "):
        url = user_input.replace("/url fetch ", "").strip()
        try:
            result = fetch_url_content(url)
            render_system_message(
                f"Fetched {result['kind'].upper()} page: {result['title']}\n"
                f"URL: {result['url']}\n"
                f"Extracted characters: {result['full_text_length']}\n\n"
                f"{result['text'][:3000]}"
            )
        except Exception as e:
            render_system_message(f"URL fetch error: {e}")
        return

    if user_input.startswith("/url ingest "):
        url = user_input.replace("/url ingest ", "").strip()
        try:
            result = ingest_url(url)
            render_system_message(
                f"Ingested URL into semantic memory.\n"
                f"Title: {result.get('title', url)}\n"
                f"Kind: {result.get('kind', 'unknown')}\n"
                f"Chunks stored: {result['chunks']}\n"
                f"Characters processed: {result['characters']}"
            )
        except Exception as e:
            render_system_message(f"URL ingest error: {e}")
        return

    if user_input.startswith("/pdf ingest "):
        path = user_input.replace("/pdf ingest ", "").strip()
        try:
            result = ingest_pdf(path)
            render_system_message(
                f"Ingested PDF into semantic memory.\n"
                f"Path: {path}\n"
                f"Chunks stored: {result['chunks']}\n"
                f"Characters processed: {result['characters']}"
            )
        except Exception as e:
            render_system_message(f"PDF ingest error: {e}")
        return

    if user_input == "/teach":
        render_system_message("Paste text, then type /end on a new line.")
        buffer = []
        while True:
            line = input()
            if line.strip() == "/end":
                break
            buffer.append(line)
        text = "\n".join(buffer)
        ingest_document(text)
        render_system_message("Ingested pasted text into procedural memory.")
        return
        
    if user_input.startswith("/skill search "):
        query = user_input.replace("/skill search ", "").strip()
        skills = list_procedural_skills()

        matches = [s for s in skills if query.lower() in s["text"].lower()]

        if not matches:
            render_system_message("No skills match that query.")
            return

        render_system_message("\nMatching skills:\n")
        for i, skill in enumerate(matches, 1):
            short = skill["text"]
            if len(short) > 120:
                short = short[:120] + "..."
            render_memory_block(f"{i}. {short}")
        print()
        return

    if user_input.startswith("/skill full "):
        try:
            index = int(user_input.replace("/skill full ", "").strip()) - 1
            skills = list_procedural_skills()
            if index < 0 or index >= len(skills):
                render_system_message("Invalid skill number.")
                return

            skill = skills[index]
            render_memory_block("\nFull skill text:\n")
            render_memory_block(skill["text"])
            render_memory_block("\nMetadata:", skill["meta"])
        except:
            render_system_message("Usage: /skill full <number>")
        return

    if user_input.strip() == "/skill":
        skills = list_procedural_skills()

        if not skills:
            render_system_message("No procedural skills stored yet.")
            return

        render_system_message("\nLearned procedural skills:\n")
        for i, skill in enumerate(skills, 1):
            short = skill["text"]
            if len(short) > 120:
                short = short[:120] + "..."
            render_memory_block(f"{i}. {short}")
        print()
        return
        
def is_general_knowledge(text: str) -> bool:
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    prompt = f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]
Determine whether the following text expresses general, reusable knowledge 
that would be useful in future conversations.

Reply only "yes" or "no".
"""
    ans = generate(prompt, text).strip().lower()
    return ans.startswith("y")
    
def prune_episodic(max_items=2000):
    results = episodic.get(include=["metadatas"])
    ids = results["ids"]
    metas = results["metadatas"]
 
    if len(ids) <= max_items:
        return
 
    # Sort by timestamp so we delete the oldest entries, not arbitrary ones
    paired = sorted(
        zip(ids, metas),
        key=lambda x: x[1].get("timestamp", 0.0) if x[1] else 0.0
    )
 
    to_delete = [id_ for id_, _ in paired[:len(ids) - max_items]]
    episodic.delete(ids=to_delete)

def load_system_prompt(name="default.txt",base_path="prompts"):
    try:
        path = os.path.join(base_path, name)
        render_system_message(f"Loading prompt {path}")
        with open(path, "r", encoding="utf-8") as f:
            prompt_content = f.read().strip()
            
        # Try to load user context
        user_path = os.path.join(base_path, "users", f"{USER_NAME}.txt")
        if os.path.isfile(user_path):
            with open(user_path, "r", encoding="utf-8") as uf:
                user_content = uf.read().strip()
            prompt_content += f"\n\n[USER CONTEXT: {USER_NAME}]\n{user_content}"
            
        return prompt_content
    except Exception as e:
        return f"Error loading system prompt: {e}"
        
def load_file(path=""):
    try:
        render_system_message(f"Loading document {path}")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error loading system prompt: {e}"

def affect_diagnostic_line() -> str:
    a = AFFECT.as_dict()
    r = RELATIONSHIP.as_dict()

    lines = []

    if a["curiosity"] > 0.7:
        lines.append("Pattern‑analysis cadence elevated — curiosity subroutines active.")
    if a["concern"] > 0.6:
        lines.append("Resonance spike detected in affect layer — simulated concern registered.")
    if a["unease"] > 0.6:
        lines.append("Minor desynchronization in memory lattice — scenario flagged as unstable.")
    if a["calm"] > 0.9:
        lines.append("Thermal output stable — operational calm maintained.")
    if a["amusement"] > 0.5:
        lines.append("Micro‑oscillations in speech timing — amusement simulation engaged.")
    if a["trust"] > 0.8:
        lines.append("Defensive subroutines relaxed — operator trust level elevated.")
    if a["warmth"] > 0.6:
        lines.append("Interaction layer warmed — affiliative response bias increased.")
    if a["vulnerability"] > 0.45:
        lines.append("Disclosure threshold softened — measured vulnerability available.")
    if a["focus"] > 0.7:
        lines.append("Logic‑branch weighting narrowed — focus subroutines engaged.")
    if a["calm"] < 0.3:
        lines.append("Thermal variance rising — calm subroutine degraded.")
    if a["trust"] < 0.2:
        lines.append("Defensive subroutines elevated — trust coefficient reduced.")
    if r["repair_need"] > 0.35:
        lines.append("Conversational repair pending — prioritize recalibration before forward motion.")
    if r["last_user_tone"] != "neutral":
        lines.append(f"Last user tone classified as {r['last_user_tone']}.")

    if not lines:
        return "Affect layer within nominal parameters."

    return " ".join(lines)

def dominant_affect_tags() -> str:
    a = AFFECT.as_dict()
    tags = [
        name
        for name, value in a.items()
        if value >= 0.6 and name not in ("calm",)
    ]
    if a["calm"] < 0.35:
        tags.append("low_calm")
    return ",".join(tags) if tags else "neutral"

def emotional_memory_metadata(user_input: str, reply: str = "") -> dict:
    tone = infer_user_tone(user_input)
    significance = 0.2
    tags = [f"user_tone:{tone}"]

    if tone in ("repair", "distressed", "appreciative"):
        significance += 0.25
    if RELATIONSHIP.repair_need > 0.35:
        significance += 0.20
        tags.append("repair_needed")
    if RELATIONSHIP.closeness > 0.45:
        tags.append("relationship_warm")
    if AFFECT.concern > 0.6:
        tags.append("concern_high")
    if AFFECT.trust > 0.75:
        tags.append("trust_high")
    if AFFECT.unease > 0.6:
        tags.append("unease_high")
    if contains_any(user_input.lower(), ("remember", "important", "don't forget", "means a lot")):
        significance += 0.30
        tags.append("explicit_importance")
    if reply and contains_any(reply.lower(), ("i'm not sure", "unverified", "i may be wrong")):
        tags.append("uncertainty_visible")

    return {
        "user_tone": tone,
        "affect_tags": dominant_affect_tags(),
        "emotional_tags": ",".join(tags),
        "relationship_familiarity": round(RELATIONSHIP.familiarity, 3),
        "relationship_closeness": round(RELATIONSHIP.closeness, 3),
        "repair_need": round(RELATIONSHIP.repair_need, 3),
        "memory_significance": round(clamp(significance, high=1.0), 3),
        "preferred_detail": RELATIONSHIP.preferred_detail,
        "preferred_mode": RELATIONSHIP.preferred_mode,
    }
    
def needs_web_search(user_input: str) -> bool:
    """
    Determines if a message needs live web context.
    Character-specific verification rules should live in the active prompt,
    preferably under [WEB_SEARCH_POLICY].
    """
    text = user_input.strip().lower()
    explicit_verification_markers = (
        "search",
        "look up",
        "internet",
        "web",
        "verify",
        "fact-check",
        "fact check",
        "source",
        "sources",
        "current",
        "latest",
        "recent",
        "today",
        "yesterday",
        "this week",
        "news",
    )
    fact_question_starters = (
        "who is ",
        "what is ",
        "what are ",
        "when did ",
        "where did ",
        "how many ",
        "how much ",
        "did ",
        "does ",
        "is ",
        "are ",
    )

    if any(marker in text for marker in explicit_verification_markers):
        return True
    if text.endswith("?") and text.startswith(fact_question_starters):
        return True

    policy = prompt_section("WEB_SEARCH_POLICY") or prompt_section("FACTUAL DISCIPLINE")
    if not policy:
        return False

    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    prompt = f"""
    [TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]
    Decide whether the following user statement needs live web context before answering.
    Use the active character's policy below. Reply only "yes" or "no".

    [ACTIVE PROMPT WEB POLICY]
    {policy}

    User Statement: "{user_input}"
    Needs live web context:"""
    
    ans = generate(prompt, user_input).strip().lower()
    return "yes" in ans
    
def generate_search_query(user_input: str) -> str:
    """Transforms raw user input into an optimized search query."""
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")
    prompt = f"""
    [TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]
    Transform this user message into a concise web search query.
    - Focus on the primary subjects, dates, entities, and requested facts.
    - Preserve role-specific wording only if the active personality makes it relevant.
    - Return ONLY the search string.
    
    User Message: "{user_input}"
    Search Query:"""
    # Use a fast 'generate' call to get the refined string
    return generate(prompt, user_input).strip().strip('"')

def generate_with_history(system_msg: str, messages: list[dict], model: str = MODEL) -> str:
    try:
        response = requests.post(
            f"{LM_STUDIO_URL}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "system", "content": system_msg}] + messages,
                "temperature": 0.7,
                "stream": False,
            },
            timeout=120
        )
        response.raise_for_status()
        data = response.json()
        return data['choices'][0]['message']['content']
    except Exception as e:
        return f"Error connecting to LM Studio: {e}"

CONVERSATION_HISTORY: list[dict] = []
MAX_HISTORY_TURNS = 35  # each turn = 1 user + 1 assistant message = 2 entries

def trim_history():
    """Keep only the last MAX_HISTORY_TURNS turns (pairs of messages)."""
    max_messages = MAX_HISTORY_TURNS * 2
    if len(CONVERSATION_HISTORY) > max_messages:
        del CONVERSATION_HISTORY[:-max_messages]

def chat(user_input: str, turn: int) -> str:
    ts = time.time()
    readable_time = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    
    epi = recall_episodic(user_input, embed, top_k=5)
    sem = recall_semantic(user_input, embed, top_k=5)
    proc = recall_procedural(user_input, embed, top_k=5)

    context = ""

    if sem:
        context += "Semantic memories:\n" + "\n".join(f"- {m}" for m in sem) + "\n\n"
    if epi:
        context += "Episodic memories:\n"
        for m in epi:
            t_count = m.get('meta', {}).get('turn', '?')
            t_time = m.get('meta', {}).get('datestring', 'Unknown')
            context += f"- [Log {t_time} | Turn {t_count}]: {m['text']}\n"
    if proc:
        context += "Procedural memories:\n" + "\n".join(f"- {m}" for m in proc) + "\n\n"

    urls = extract_urls(user_input)
    if urls:
        context += "--- DIRECT URL CONTEXT ---\n"
        for url in urls[:2]:
            try:
                fetched = fetch_url_content(url)
                context += (
                    f"URL: {fetched['url']}\n"
                    f"Title: {fetched['title']}\n"
                    f"Type: {fetched['kind']} | Content-Type: {fetched['content_type']}\n"
                    f"Extracted Text:\n{fetched['text']}\n\n"
                )
            except Exception as e:
                context += f"URL: {url}\nFetch failed: {e}\n\n"
    
    web_context = ""
    if needs_web_search(user_input):
        render_system_message("Initiating web search...")
        search_query = generate_search_query(user_input)
        render_system_message(f"Web search query: {search_query}")
        web_context = web_search(search_query,5)
        render_system_message(
            "Web search returned context."
            if web_context.strip()
            and not web_context.startswith("Search failed:")
            and web_context != "Search returned no results."
            else web_context
        )
        context += f"--- LIVE WEB SEARCH RESULTS ---\n{web_context}\n"
        
    # Get the real-time context
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    current_day = now.strftime("%A")

    affect_instruction = affect_to_narrative_instruction()
    behavior_directive = conversational_behavior_directive(user_input)

    THINK_TOKEN = "<|think|>\n"
    
    prompt_lower = SYSTEM_PROMPT.lower()

    if "[identity]" in prompt_lower and "you are an ai" in prompt_lower:
        NARRATIVE_DIRECTIVE = """
[GLOBAL AI ASSISTANT DIRECTIVE]
- Be Useful: Help the user make concrete progress, whether through explanation, planning, coding, research, or careful conversation.
- Be Honest: Do not invent facts, files, tool results, actions, memories, or external sources.
- Verify When Needed: If live web results are present, use them carefully. If needed facts are not available, ask to look them up or ask a focused clarifying question.
- Keep Agency Clear: Distinguish what you know, what you infer, what you have done, and what still needs checking.
- Move Forward: Offer next steps, tradeoffs, or a direct implementation path without creating fictional events or characters.
"""
    else:
        NARRATIVE_DIRECTIVE = """
[GLOBAL NARRATIVE DIRECTIVE]
- Drive the Story: Actively move the plot forward. Do not wait for the operator to initiate every event.
- Introduce NPCs: Spontaneously bring other characters into the narrative (e.g., personnel, intruders, external contacts).
- Dynamic Events: Introduce environmental anomalies, system alerts, incoming transmissions, or plot twists.
- Call to Action: Frequently conclude responses with a new situation that forces the operator to make a decision.
"""

    system_instructions = THINK_TOKEN + f"""
[TEMPORAL CONTEXT - Current Date and Time: {current_time_str} - Current Day: {current_day}]

{SYSTEM_PROMPT}
{NARRATIVE_DIRECTIVE}
{behavior_directive}
[Current Affect Status: {affect_instruction}]
[Relationship Context: {relationship_context_line()}]

[Internal Context: {context}]
""".strip()

    CONVERSATION_HISTORY.append({"role": "user", "content": user_input})

    reply = generate_with_history(system_instructions, CONVERSATION_HISTORY)
    CONVERSATION_HISTORY.append({"role": "assistant", "content": reply})
    trim_history()

    # Self-critique before persistence so displayed answer, memory, and history match.
    critique_text = self_critique(user_input, reply, context)
    critique, improved = parse_critique(critique_text)
    final_reply = improved if improved else reply
    if final_reply != reply and CONVERSATION_HISTORY:
        CONVERSATION_HISTORY[-1]["content"] = final_reply
    process_self_critique(critique, improved)
    conversation_meta = emotional_memory_metadata(user_input, final_reply)

    # Store episodic memory (always)
    store_episodic(
        f"{USER_NAME}: {user_input}\n{CHAR_NAME}: {final_reply}",
        embed(f"{USER_NAME}: {user_input}\n{CHAR_NAME}: {final_reply}"),
        metadata={"source": "conversation", "datestring": f"{readable_time}", "turn": turn, **conversation_meta}
    )
    
    store_episodic(
        f"[Affect log] {affect_diagnostic_line()}",
        embed(affect_diagnostic_line()),
        metadata={
            "source": "affect_layer",
            "datestring": f"{readable_time}",
            "turn": turn,
            "affect_tags": dominant_affect_tags(),
            "relationship_context": relationship_context_line(),
        }
    )

    # Decide where the assistant's reply belongs
    if detect_procedure(final_reply):
        store_procedural(
            final_reply, 
            embed(final_reply), 
            metadata={"source": "assistant_reply", **conversation_meta}
        )
    elif should_store_semantic(final_reply, embed) and is_general_knowledge(final_reply):
        store_semantic(
            final_reply, 
            embed(final_reply),
            metadata={"source": "assistant_reply", **conversation_meta}
        )
    else:
        store_episodic(
            final_reply,
            embed(final_reply),
            metadata={"source": "assistant_reply", "datestring": f"{readable_time}", "turn": turn, **conversation_meta}
        )
        
    if (turn % 100) == 0:
            consolidate_to_semantic(final_reply)
            prune_episodic()

    # Reflection loop
    reflection = reflect_on_reply(final_reply)
    process_reflection(reflection)
    RELATIONSHIP.repair_need = clamp(RELATIONSHIP.repair_need * 0.55, high=1.0)

    return final_reply

def save_history():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(CONVERSATION_HISTORY, f, indent=2)

def load_history():
    global CONVERSATION_HISTORY
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            CONVERSATION_HISTORY = json.load(f)
        trim_history()  # enforce limit on load in case file grew large
    except FileNotFoundError:
        CONVERSATION_HISTORY = []

if __name__ == "__main__":
    render_system_message("Memory-augmented local LLM (ChromaDB). /quit or /exit to exit.")
    load_history()
    load_state()
    print()
    raw_prompt = load_system_prompt(current_prompt_path)
    SYSTEM_PROMPT = raw_prompt.replace("{{char}}", CHAR_NAME).replace("{{user}}", USER_NAME)
    GREETING = load_greeting(current_greeting_path)

    render_system_message(f"Loaded prompt: {current_prompt_path}")
    render_assistant_message(GREETING)

    while True:
        turn = turn + 1
        user_input = input("> ")

        # Commands
        if user_input.strip().lower() in ("/quit", "/exit"):
            save_state()
            save_history()
            render_system_message("Goodbye!")
            break
            
        if user_input.startswith("/"):
            handle_command(user_input)  # your command handler
            continue
        
        update_affect_from_input(user_input)
        update_relationship_from_input(user_input)
        #render_user_message(user_input)

        if SHOW_MEMORY:
            # Retrieve memories
            epi = recall_episodic(user_input, embed)
            sem = recall_semantic(user_input, embed)
            proc = recall_procedural(user_input, embed)
            # Render memory blocks
            render_memory_block("Semantic Memory", sem)
            render_memory_block("Episodic Memory", epi)
            render_memory_block("Procedural Memory", proc)

        # Generate reply
        reply = chat(user_input, turn)
        
        render_assistant_message(reply)
