import importlib.util
import datetime
import html
import json
import re
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import markdown

try:
    from latex2mathml.converter import convert as latex_to_mathml
except ImportError:
    latex_to_mathml = None

try:
    from pylatexenc.latex2text import LatexNodes2Text
except ImportError:
    LatexNodes2Text = None


APP_DIR = Path(__file__).resolve().parent
ENGINE_PATH = APP_DIR / "3chat.research.py"
DEFAULT_PORT = 8765

HELP_TEXT = """Supported PoC UI actions

Buttons:
- Send: send the current message to the assistant.
- Save: save state and conversation history.
- Reset: back up current state/history/memory/adaptive guidance, then clear them while preserving identity and personality files.
- Help: show this help panel.
- Memory preview: include retrieved memories before each reply.
- Memory > Show: inspect stored memory entries for the selected memory type.
- Knowledge > Fetch URL: read a specific web page or PDF URL without storing it.
- Knowledge > Ingest URL: store extracted page/PDF URL text as semantic memory chunks.
- Knowledge > Ingest PDF: store extracted local PDF text as semantic memory chunks.
- Knowledge > Ingest Text / Code: select a local text or source-code file and store it as semantic memory chunks.
- Knowledge > Read Image: analyze a local PNG/JPEG/WebP/GIF through the current LM Studio model.

Message box shortcuts:
- Enter: send.
- Shift+Enter: add a new line.

Supported slash commands in this web PoC:
- /memory on: enable memory preview.
- /memory off: disable memory preview.
- /diagnostics: show affect and relationship diagnostics.
- /learning: show adaptive rules learned from response validation.
- /learning reset: clear adaptive rules without changing the character prompt.
- /help: show this help panel.
- /save: save state and conversation history.
- /reset: back up current state/history/memory, then clear active memories and conversation state.
- /url fetch <url>: read a specific web page or PDF URL without storing it.
- /url ingest <url>: store extracted page/PDF URL text as semantic memory chunks.
- /pdf ingest <path>: store extracted local PDF text as semantic memory chunks.
- /text ingest <path>: store a local text or source-code file as semantic memory chunks.
- /image read <path> [:: question]: analyze a local image, optionally with a specific question.
- /quit or /exit: save state; stop the server with Ctrl+C in the terminal when finished.

The original terminal app supports additional commands such as /prompt, /greeting, /lorebook, /ingest, /teach, and /skill. Those remain available when running 3chat.research.py directly."""


def render_latex_fragment(source: str, display=False) -> str:
    try:
        if latex_to_mathml is not None:
            return latex_to_mathml(source, display="block" if display else "inline")
        if LatexNodes2Text is not None:
            text = LatexNodes2Text().latex_to_text(source)
            return f"<span class=\"math-fallback\">{html.escape(text)}</span>"
    except Exception:
        pass
    return f"<code>{html.escape(source)}</code>"


def normalize_model_markdown(text: str) -> str:
    """Normalize common local coder-model Markdown variations."""
    text = html.unescape(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = normalize_tool_protocol_markup(text)

    normalized = []
    fence_open = False
    for line in text.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+.-]*)\s*$", line)
        if fence:
            language = fence.group(2).lower()
            if not fence_open:
                normalized.append(f"```{language}" if language else "```")
                fence_open = True
            else:
                normalized.append("```")
                fence_open = False
            continue
        normalized.append(line)

    if fence_open:
        normalized.append("```")
    return "\n".join(normalized)

def normalize_tool_protocol_markup(text: str) -> str:
    """Renderer fallback for leaked DeepSeek tool-call control tokens."""
    tool_tokens = {
        "<｜tool▁calls▁begin｜>": "",
        "<｜tool▁calls▁end｜>": "",
        "<｜tool▁call▁begin｜>": "\n",
        "<｜tool▁call▁end｜>": "\n",
        "<｜tool▁sep｜>": "\n",
        "<|tool_calls_begin|>": "",
        "<|tool_calls_end|>": "",
        "<|tool_call_begin|>": "\n",
        "<|tool_call_end|>": "\n",
        "<|tool_sep|>": "\n",
    }
    if not any(token in text for token in tool_tokens):
        return text

    cleaned = text
    for token, replacement in tool_tokens.items():
        cleaned = cleaned.replace(token, replacement)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    lines = cleaned.splitlines()
    if lines and lines[0].strip().lower() in {"function", "tool", "python"}:
        lines = lines[1:]
    payload = "\n".join(lines).strip()
    if not payload or "```" in payload:
        return payload

    lowered = payload.lower()
    language = "text"
    if any(marker in lowered for marker in (
        "import torch", "torch.", "def ", "self,", "isinstance(", "dtype=torch.",
    )):
        language = "python"
    elif any(marker in lowered for marker in ("const ", "let ", "=>", "console.log(")):
        language = "javascript"
    elif payload.lstrip().startswith(("{", "[")):
        language = "json"

    code_signals = sum(marker in payload for marker in ("\n", "(", ")", ":", "=", "{", "}", ";"))
    if language != "text" or code_signals >= 4:
        return f"```{language}\n{payload}\n```"
    return payload


def render_math_delimiters(text: str) -> str:
    math_placeholders = []
    code_placeholders = []

    def stash_math(rendered: str) -> str:
        token = f"@@ENGRAMMATH{len(math_placeholders)}@@"
        math_placeholders.append((token, rendered))
        return token

    def block_replacer(match):
        return stash_math(render_latex_fragment(match.group(1).strip(), display=True))

    def inline_replacer(match):
        return stash_math(render_latex_fragment(match.group(1).strip(), display=False))

    def code_replacer(match):
        token = f"@@ENGRAMCODE{len(code_placeholders)}@@"
        first_line = match.group(0).splitlines()[0]
        language_match = re.match(r"^```([A-Za-z0-9_+.-]+)", first_line)
        language = language_match.group(1).lower() if language_match else "code"
        code_html = markdown.markdown(
            match.group(0),
            extensions=["fenced_code", "codehilite"],
            extension_configs={
                "codehilite": {
                    "guess_lang": False,
                    "noclasses": False,
                }
            },
            output_format="html5",
        )
        code_html = f'<div class="code-source" data-language="{html.escape(language)}">{code_html}</div>'
        code_placeholders.append((token, code_html))
        return token

    protected = re.sub(
        r"(?ms)^```[^\n]*\n.*?^```\s*$",
        code_replacer,
        text,
    )
    protected = re.sub(r"\$\$(.+?)\$\$", block_replacer, protected, flags=re.DOTALL)
    protected = re.sub(r"(?<!\\)\$(?!\s)(.+?)(?<!\s)(?<!\\)\$", inline_replacer, protected, flags=re.DOTALL)
    # Raw model HTML is text unless generated by the trusted renderers above.
    protected = html.escape(protected, quote=False)

    rendered = markdown.markdown(
        protected,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html5",
    )
    for token, value in math_placeholders:
        rendered = rendered.replace(token, value)
    for token, value in code_placeholders:
        rendered = rendered.replace(f"<p>{token}</p>", value)
        rendered = rendered.replace(token, value)
    return rendered


def render_model_text(text: str) -> str:
    return render_math_delimiters(normalize_model_markdown(text))


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ENGRAM PoC</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #111318;
      --panel: #1b2029;
      --panel-2: #232936;
      --text: #edf2f7;
      --muted: #9aa4b2;
      --line: #343b49;
      --user: #7dd3fc;
      --assistant: #bbf7d0;
      --memory: #d8b4fe;
      --accent: #6ccff6;
      --danger: #fb7185;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }

    .app {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 16px;
      height: 100vh;
      padding: 16px;
    }

    .main, .side {
      min-height: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }

    .main {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
    }

    h1 {
      margin: 0;
      font-size: 20px;
      letter-spacing: 0;
    }

    .subtitle {
      color: var(--muted);
      font-size: 13px;
      margin-top: 3px;
    }

    .status {
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .transcript {
      overflow: auto;
      padding: 18px;
    }

    .message {
      max-width: 920px;
      margin: 0 0 14px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #151922;
      white-space: pre-wrap;
      line-height: 1.45;
    }

    .message .label {
      display: block;
      margin-bottom: 7px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .message.user { border-color: rgba(125, 211, 252, 0.45); }
    .message.user .label { color: var(--user); }
    .message.assistant { border-color: rgba(187, 247, 208, 0.45); }
    .message.assistant .label { color: var(--assistant); }
    .message.memory { border-color: rgba(216, 180, 254, 0.45); }
    .message.memory .label { color: var(--memory); }
    .message.system { border-color: rgba(244, 211, 94, 0.45); }

    .message .rendered {
      white-space: normal;
    }

    .message .rendered p {
      margin: 0 0 0.8em;
    }

    .message .rendered p:last-child {
      margin-bottom: 0;
    }

    .message .rendered ul, .message .rendered ol {
      margin: 0.3em 0 0.8em 1.2em;
      padding: 0;
    }

    .message .rendered code {
      padding: 0.1em 0.3em;
      border-radius: 4px;
      background: #0c1017;
      color: #f8fafc;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }

    .message .rendered .code-shell {
      position: relative;
      overflow: hidden;
      margin: 0.7em 0;
      border: 1px solid #343c4b;
      border-radius: 8px;
      background: #0c1017;
    }

    .message .rendered .code-toolbar {
      display: flex;
      min-height: 34px;
      align-items: center;
      justify-content: space-between;
      padding: 0 8px 0 12px;
      border-bottom: 1px solid #343c4b;
      color: #9ca3af;
      background: #151a23;
      font-size: 11px;
      text-transform: uppercase;
    }

    .message .rendered .copy-code {
      width: 30px;
      height: 28px;
      padding: 0;
      border: 0;
      background: transparent;
      color: #cbd5e1;
      font-size: 15px;
      cursor: pointer;
    }

    .message .rendered .copy-code:hover {
      color: #ffffff;
      background: #252c38;
    }

    .message .rendered pre {
      overflow: auto;
      margin: 0;
      padding: 14px;
      background: #0c1017;
      line-height: 1.5;
      tab-size: 4;
      white-space: pre;
    }

    .message .rendered pre code {
      display: block;
      padding: 0;
      border-radius: 0;
      background: transparent;
      white-space: inherit;
    }

    .codehilite .hll { background-color: #283342; }
    .codehilite .c, .codehilite .c1, .codehilite .cm { color: #7f8c98; }
    .codehilite .k, .codehilite .kc, .codehilite .kd, .codehilite .kn { color: #c792ea; }
    .codehilite .s, .codehilite .s1, .codehilite .s2 { color: #c3e88d; }
    .codehilite .mi, .codehilite .mf { color: #f78c6c; }
    .codehilite .n, .codehilite .p { color: #d6deeb; }
    .codehilite .nf, .codehilite .nc { color: #82aaff; }
    .codehilite .o { color: #89ddff; }
    .codehilite .nb, .codehilite .bp { color: #ffcb6b; }
    }

    .message .rendered blockquote {
      margin: 0.4em 0 0.8em;
      padding-left: 12px;
      border-left: 3px solid var(--line);
      color: var(--muted);
    }

    .message .rendered table {
      width: 100%;
      border-collapse: collapse;
      margin: 0.4em 0 0.8em;
    }

    .message .rendered th, .message .rendered td {
      border: 1px solid var(--line);
      padding: 6px 8px;
      text-align: left;
    }

    math {
      color: #f4f7fb;
      font-size: 1.05em;
    }

    .math-fallback {
      color: #f4f7fb;
      font-family: ui-serif, Georgia, serif;
    }

    .composer {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      padding: 14px;
      border-top: 1px solid var(--line);
    }

    textarea {
      width: 100%;
      min-height: 86px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #11151d;
      color: var(--text);
      font: inherit;
      line-height: 1.4;
    }

    textarea:focus {
      outline: 2px solid rgba(108, 207, 246, 0.35);
      border-color: var(--accent);
    }

    .actions {
      display: flex;
      flex-direction: column;
      gap: 10px;
      min-width: 150px;
    }

    button {
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 14px;
      color: var(--text);
      background: var(--panel-2);
      font-weight: 700;
      cursor: pointer;
    }

    button.primary {
      background: #17556b;
      border-color: #2b8bb0;
    }

    button.danger {
      color: #ffe4e6;
      background: #6f1d2a;
      border-color: #a33a4c;
    }

    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }

    .button-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }

    label.toggle {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }

    .side {
      overflow: auto;
      padding: 16px;
    }

    .card {
      margin-bottom: 14px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #171c25;
    }

    .card h2 {
      margin: 0 0 10px;
      font-size: 14px;
      letter-spacing: 0;
    }

    .identity {
      color: var(--muted);
      line-height: 1.45;
      font-size: 13px;
      white-space: pre-line;
    }

    canvas {
      display: block;
      width: 100%;
      height: 240px;
      border-radius: 8px;
      background: #11151d;
    }

    .metric {
      margin: 9px 0;
    }

    .metric-top {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 5px;
      color: var(--muted);
      font-size: 12px;
    }

    .bar {
      height: 8px;
      overflow: hidden;
      border-radius: 8px;
      background: #2a303b;
    }

    .fill {
      height: 100%;
      width: 0;
      border-radius: 8px;
      background: var(--accent);
      transition: width 180ms ease;
    }

    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 8px;
    }

    .stat {
      padding: 10px;
      border-radius: 8px;
      background: var(--panel-2);
      text-align: center;
    }

    .stat b {
      display: block;
      font-size: 20px;
      margin-bottom: 2px;
    }

    .stat span {
      color: var(--muted);
      font-size: 11px;
    }

    .mode {
      color: var(--muted);
      line-height: 1.55;
      font-size: 13px;
      white-space: pre-line;
    }

    .memory-controls {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-top: 10px;
    }

    select, input[type="text"], input[type="file"] {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 10px;
      color: var(--text);
      background: var(--panel-2);
      font: inherit;
    }

    input[type="text"] {
      width: 100%;
    }

    .knowledge-form {
      display: grid;
      gap: 8px;
    }

    .knowledge-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }

    .memory-list {
      max-height: 340px;
      overflow: auto;
      margin-top: 10px;
      padding-right: 2px;
    }

    .memory-item {
      margin-bottom: 10px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #11151d;
    }

    .memory-item-title {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 7px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }

    .memory-content {
      color: var(--text);
      line-height: 1.4;
      font-size: 12px;
      white-space: pre-wrap;
    }

    .memory-meta, .memory-vector {
      margin-top: 7px;
      color: var(--muted);
      line-height: 1.4;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 11px;
      overflow-wrap: anywhere;
    }

    @media (max-width: 980px) {
      .app {
        grid-template-columns: 1fr;
        height: auto;
        min-height: 100vh;
      }

      .side {
        order: -1;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <section class="main">
      <header>
        <div>
          <h1>ENGRAM Memory-Augmented Companion</h1>
          <div class="subtitle" id="subtitle">Loading engine...</div>
        </div>
        <div class="status" id="status">starting</div>
      </header>

      <main class="transcript" id="transcript"></main>

      <section class="composer">
        <textarea id="message" placeholder="Type a message. Enter sends, Shift+Enter adds a line."></textarea>
        <div class="actions">
          <button class="primary" id="send">Send</button>
          <div class="button-row">
            <button id="save">Save</button>
            <button id="help">Help</button>
          </div>
          <button class="danger" id="reset">Reset</button>
          <label class="toggle"><input type="checkbox" id="memoryToggle"> Memory preview</label>
        </div>
      </section>
    </section>

    <aside class="side">
      <div class="card">
        <h2>Identity</h2>
        <div class="identity" id="identity">Loading...</div>
      </div>

      <div class="card">
        <h2>Affect</h2>
        <canvas id="affectCanvas" width="310" height="240"></canvas>
      </div>

      <div class="card">
        <h2>Relationship</h2>
        <div id="relationship"></div>
      </div>

      <div class="card">
        <h2>Memory</h2>
        <div class="grid">
          <div class="stat"><b id="episodicCount">-</b><span>episodic</span></div>
          <div class="stat"><b id="semanticCount">-</b><span>semantic</span></div>
          <div class="stat"><b id="proceduralCount">-</b><span>procedural</span></div>
        </div>
        <div class="memory-controls">
          <select id="memoryKind">
            <option value="episodic">Episodic</option>
            <option value="semantic">Semantic</option>
            <option value="procedural">Procedural</option>
          </select>
          <button id="loadMemory">Show</button>
        </div>
        <div class="memory-list" id="memoryList"></div>
      </div>

      <div class="card">
        <h2>Knowledge</h2>
        <div class="knowledge-form">
          <input type="text" id="urlInput" placeholder="https://example.com/page-or.pdf">
          <div class="knowledge-actions">
            <button id="fetchUrl">Fetch URL</button>
            <button id="ingestUrl">Ingest URL</button>
          </div>
          <input type="text" id="pdfPathInput" placeholder="/absolute/path/to/document.pdf">
          <button id="ingestPdf">Ingest PDF</button>
          <input type="file" id="textFileInput" accept=".txt,.md,.rst,.log,.csv,.tsv,.py,.pyw,.ipynb,.js,.jsx,.mjs,.cjs,.ts,.tsx,.java,.kt,.kts,.go,.rs,.rb,.php,.c,.h,.cc,.cpp,.cxx,.hpp,.cs,.swift,.scala,.sh,.bash,.zsh,.fish,.ps1,.html,.htm,.css,.scss,.sass,.less,.json,.jsonl,.yaml,.yml,.toml,.ini,.cfg,.conf,.sql,.xml,.graphql,.gql,.lua,.r,.m">
          <button id="ingestTextFile">Ingest Text / Code</button>
          <input type="text" id="imagePathInput" placeholder="/absolute/path/to/graph.png">
          <input type="text" id="imageQuestionInput" placeholder="Optional image question">
          <button id="readImage">Read Image</button>
        </div>
      </div>

      <div class="card">
        <h2>Mode</h2>
        <div class="mode" id="mode">-</div>
      </div>
    </aside>
  </div>

  <script>
    const transcript = document.getElementById("transcript");
    const message = document.getElementById("message");
    const send = document.getElementById("send");
    const save = document.getElementById("save");
    const help = document.getElementById("help");
    const reset = document.getElementById("reset");
    const loadMemory = document.getElementById("loadMemory");
    const memoryKind = document.getElementById("memoryKind");
    const memoryList = document.getElementById("memoryList");
    const urlInput = document.getElementById("urlInput");
    const pdfPathInput = document.getElementById("pdfPathInput");
    const textFileInput = document.getElementById("textFileInput");
    const imagePathInput = document.getElementById("imagePathInput");
    const imageQuestionInput = document.getElementById("imageQuestionInput");
    const fetchUrlButton = document.getElementById("fetchUrl");
    const ingestUrlButton = document.getElementById("ingestUrl");
    const ingestPdfButton = document.getElementById("ingestPdf");
    const ingestTextFileButton = document.getElementById("ingestTextFile");
    const readImageButton = document.getElementById("readImage");
    const statusEl = document.getElementById("status");
    const memoryToggle = document.getElementById("memoryToggle");

    const colors = {
      curiosity: "#4ea8de",
      concern: "#ff8c42",
      calm: "#72b01d",
      focus: "#7b61ff",
      amusement: "#f7b801",
      unease: "#d1495b",
      trust: "#2a9d8f",
      warmth: "#e76f51",
      vulnerability: "#b56576"
    };

    function addMessage(role, text, htmlContent = null) {
      const node = document.createElement("article");
      node.className = `message ${role}`;
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = role === "assistant" ? window.characterName || "Assistant" : role;
      node.appendChild(label);
      if (htmlContent) {
        const rendered = document.createElement("div");
        rendered.className = "rendered";
        rendered.innerHTML = htmlContent;
        decorateCodeBlocks(rendered);
        node.appendChild(rendered);
      } else {
        node.appendChild(document.createTextNode(text));
      }
      transcript.appendChild(node);
      transcript.scrollTop = transcript.scrollHeight;
    }

    function decorateCodeBlocks(container) {
      container.querySelectorAll("pre").forEach((pre) => {
        if (pre.parentElement?.classList.contains("code-shell")) return;
        const code = pre.querySelector("code");
        const languageClass = [...(code?.classList || [])].find((name) => name.startsWith("language-"));
        const language = pre.closest(".code-source")?.dataset.language
          || (languageClass ? languageClass.slice("language-".length) : "code");

        const shell = document.createElement("div");
        shell.className = "code-shell";
        const toolbar = document.createElement("div");
        toolbar.className = "code-toolbar";
        const label = document.createElement("span");
        label.textContent = language;
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "copy-code";
        copy.title = "Copy code";
        copy.setAttribute("aria-label", "Copy code");
        copy.textContent = "⧉";
        copy.addEventListener("click", async () => {
          await navigator.clipboard.writeText(code?.textContent || pre.textContent || "");
          copy.textContent = "✓";
          window.setTimeout(() => { copy.textContent = "⧉"; }, 1200);
        });

        toolbar.append(label, copy);
        pre.replaceWith(shell);
        shell.append(toolbar, pre);
      });
    }

    function setBusy(busy) {
      send.disabled = busy;
      message.disabled = busy;
      statusEl.textContent = busy ? "thinking..." : "ready";
    }

    function pct(value, max = 1) {
      return Math.max(0, Math.min(100, (Number(value || 0) / max) * 100));
    }

    function renderMetric(container, name, value) {
      const wrap = document.createElement("div");
      wrap.className = "metric";
      wrap.innerHTML = `
        <div class="metric-top"><span>${name.replaceAll("_", " ")}</span><span>${Number(value || 0).toFixed(2)}</span></div>
        <div class="bar"><div class="fill" style="width:${pct(value)}%"></div></div>
      `;
      container.appendChild(wrap);
    }

    function renderAffect(affect) {
      const canvas = document.getElementById("affectCanvas");
      const ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      const names = Object.keys(affect || {});
      const cx = canvas.width / 2;
      const cy = 126;
      const orbit = 78;

      ctx.fillStyle = "#e8edf2";
      ctx.font = "bold 13px system-ui";
      ctx.textAlign = "center";
      ctx.fillText("Affect constellation", cx, 24);

      names.forEach((name, index) => {
        const angle = (Math.PI * 2 * index / names.length) - Math.PI / 2;
        const value = Number(affect[name] || 0);
        const x = cx + Math.cos(angle) * orbit;
        const y = cy + Math.sin(angle) * orbit;
        const r = 7 + Math.min(value, 1.5) * 8;

        ctx.strokeStyle = "#2b303a";
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(x, y);
        ctx.stroke();

        ctx.fillStyle = colors[name] || "#9aa4b2";
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();

        if (value > 0.75) {
          ctx.strokeStyle = "#f4f7fb";
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }

        ctx.fillStyle = "#9aa4b2";
        ctx.font = "10px system-ui";
        ctx.fillText(name.slice(0, 9), x, y + r + 13);
      });

      ctx.fillStyle = "#202632";
      ctx.beginPath();
      ctx.arc(cx, cy, 25, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#e8edf2";
      ctx.font = "bold 10px system-ui";
      ctx.fillText("calm", cx, cy - 3);
      ctx.fillStyle = "#9aa4b2";
      ctx.font = "10px system-ui";
      ctx.fillText(Number(affect.calm || 0).toFixed(2), cx, cy + 12);
    }

    function renderState(state) {
      window.characterName = state.character || "Assistant";
      document.getElementById("subtitle").textContent = `${state.character} with ${state.user}`;
      document.getElementById("identity").textContent = `character: ${state.character}\nuser: ${state.user}\nprompt: ${state.prompt}\nbackend: ${state.backend || "-"}\nmodel: ${state.model}`;

      renderAffect(state.affect || {});

      const relationship = document.getElementById("relationship");
      relationship.innerHTML = "";
      ["familiarity", "closeness", "boundary_sensitivity", "repair_need", "shared_humor"].forEach((name) => {
        renderMetric(relationship, name, state.relationship?.[name] || 0);
      });

      document.getElementById("episodicCount").textContent = state.memory?.episodic ?? "-";
      document.getElementById("semanticCount").textContent = state.memory?.semantic ?? "-";
      document.getElementById("proceduralCount").textContent = state.memory?.procedural ?? "-";

      document.getElementById("mode").textContent =
        `detail: ${state.relationship?.preferred_detail || "-"}\n` +
        `mode: ${state.relationship?.preferred_mode || "-"}\n` +
        `last tone: ${state.relationship?.last_user_tone || "-"}\n` +
        `adaptive rules: ${(state.adaptive?.corrective || 0) + (state.adaptive?.reinforcement || 0)}\n` +
        `last quality: ${state.adaptive?.last_validation?.score ?? "-"}`;
    }

    function formatMeta(meta) {
      const entries = Object.entries(meta || {});
      if (!entries.length) return "metadata: none";
      return entries
        .slice(0, 8)
        .map(([key, value]) => `${key}: ${String(value)}`)
        .join(" | ");
    }

    function renderMemoryList(payload) {
      memoryList.innerHTML = "";
      if (!payload.items || payload.items.length === 0) {
        const empty = document.createElement("div");
        empty.className = "memory-item";
        empty.textContent = "No stored entries in this memory collection.";
        memoryList.appendChild(empty);
        return;
      }

      payload.items.forEach((item, index) => {
        const node = document.createElement("article");
        node.className = "memory-item";

        const title = document.createElement("div");
        title.className = "memory-item-title";
        title.innerHTML = `<span>${payload.kind} #${index + 1}</span><span>${item.id || ""}</span>`;
        node.appendChild(title);

        const content = document.createElement("div");
        content.className = "memory-content";
        content.textContent = item.content || "";
        node.appendChild(content);

        const meta = document.createElement("div");
        meta.className = "memory-meta";
        meta.textContent = formatMeta(item.metadata);
        node.appendChild(meta);

        const vector = document.createElement("div");
        vector.className = "memory-vector";
        vector.textContent = item.vector_preview
          ? `vector[0:${item.vector_preview.length}] = [${item.vector_preview.join(", ")}]`
          : "vector: unavailable";
        node.appendChild(vector);

        memoryList.appendChild(node);
      });
    }

    async function fetchMemory() {
      loadMemory.disabled = true;
      memoryList.innerHTML = "<div class='memory-item'>Loading memory entries...</div>";
      try {
        const response = await fetch(`/api/memory?kind=${encodeURIComponent(memoryKind.value)}&limit=8`);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Memory request failed");
        renderMemoryList(payload);
      } catch (error) {
        memoryList.innerHTML = "";
        addMessage("system", String(error));
      } finally {
        loadMemory.disabled = false;
      }
    }

    async function fetchHelp() {
      try {
        const response = await fetch("/api/help");
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Help request failed");
        addMessage("system", payload.help);
      } catch (error) {
        addMessage("system", String(error));
      }
    }

    async function postJson(url, body) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Request failed");
      return payload;
    }

    async function fetchSpecificUrl(url = urlInput.value.trim()) {
      if (!url) {
        addMessage("system", "Enter a URL first.");
        return;
      }
      setBusy(true);
      try {
        const payload = await postJson("/api/url/fetch", { url });
        addMessage("memory", `Fetched ${payload.kind.toUpperCase()}: ${payload.title}\nURL: ${payload.url}\nExtracted characters: ${payload.full_text_length}\n\n${payload.text}`);
      } catch (error) {
        addMessage("system", String(error));
      } finally {
        setBusy(false);
      }
    }

    async function ingestSpecificUrl(url = urlInput.value.trim()) {
      if (!url) {
        addMessage("system", "Enter a URL first.");
        return;
      }
      setBusy(true);
      try {
        const payload = await postJson("/api/url/ingest", { url });
        addMessage("system", `Ingested URL into semantic memory.\nTitle: ${payload.title || url}\nKind: ${payload.kind}\nChunks stored: ${payload.chunks}\nCharacters processed: ${payload.characters}`);
        renderState(payload.state);
        fetchMemory();
      } catch (error) {
        addMessage("system", String(error));
      } finally {
        setBusy(false);
      }
    }

    async function ingestPdfPath(path = pdfPathInput.value.trim()) {
      if (!path) {
        addMessage("system", "Enter a local PDF path first.");
        return;
      }
      setBusy(true);
      try {
        const payload = await postJson("/api/pdf/ingest", { path });
        addMessage("system", `Ingested PDF into semantic memory.\nPath: ${payload.path}\nChunks stored: ${payload.chunks}\nCharacters processed: ${payload.characters}`);
        renderState(payload.state);
        fetchMemory();
      } catch (error) {
        addMessage("system", String(error));
      } finally {
        setBusy(false);
      }
    }

    async function ingestSelectedTextFile() {
      const file = textFileInput.files?.[0];
      if (!file) {
        addMessage("system", "Select a text or source-code file first.");
        return;
      }
      if (file.size > 5_000_000) {
        addMessage("system", "The selected file is too large. The limit is 5 MB.");
        return;
      }

      setBusy(true);
      try {
        const content = await file.text();
        const payload = await postJson("/api/text/ingest", {
          filename: file.name,
          content,
        });
        addMessage(
          "system",
          `Ingested text/code file into semantic memory.\nFile: ${payload.filename}\nLanguage: ${payload.language}\nChunks stored: ${payload.chunks}\nCharacters processed: ${payload.characters}`
        );
        textFileInput.value = "";
        renderState(payload.state);
        fetchMemory();
      } catch (error) {
        addMessage("system", String(error));
      } finally {
        setBusy(false);
      }
    }

    async function readImagePath(path = imagePathInput.value.trim(), question = imageQuestionInput.value.trim()) {
      if (!path) {
        addMessage("system", "Enter a local image path first.");
        return;
      }
      setBusy(true);
      try {
        const payload = await postJson("/api/image/read", { path, question });
        const meta = payload.metadata || {};
        addMessage(
          "assistant",
          payload.analysis,
          payload.analysis_html || null
        );
        addMessage(
          "system",
          `Image metadata: ${meta.filename || path} | ${meta.width || "?"}x${meta.height || "?"} | ${meta.format || "unknown"}`
        );
      } catch (error) {
        addMessage("system", String(error));
      } finally {
        setBusy(false);
      }
    }

    async function resetEverything() {
      const confirmed = window.confirm(
        "Back up and clear conversation history, state, memories, and learned quality guidance? Character, user, prompt, and greeting settings will be kept."
      );
      if (!confirmed) return;

      setBusy(true);
      try {
        const response = await fetch("/api/reset", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Reset failed");
        transcript.innerHTML = "";
        addMessage("system", `Reset complete. Backup written to ${payload.backup_path}`);
        if (payload.state?.greeting) addMessage("assistant", payload.state.greeting, payload.state.greeting_html || null);
        renderState(payload.state);
        fetchMemory();
      } catch (error) {
        addMessage("system", String(error));
      } finally {
        setBusy(false);
        message.focus();
      }
    }

    function diagnosticsText(state) {
      const affect = Object.entries(state.affect || {})
        .map(([key, value]) => `${key}: ${Number(value || 0).toFixed(2)}`)
        .join("\n");
      const relationship = Object.entries(state.relationship || {})
        .map(([key, value]) => `${key}: ${value}`)
        .join("\n");
      const adaptiveRules = (state.adaptive?.rules || [])
        .map((entry) => `- [${entry.kind}] ${entry.rule} (${entry.hits || 1}x)`)
        .join("\n");
      const validation = state.adaptive?.last_validation || {};
      return `Affect\n${affect}\n\nRelationship\n${relationship}\n\nAdaptive quality guidance\nLast validation: ${validation.score ?? "-"} (${validation.verdict || "unknown"})\n${adaptiveRules || "No learned rules yet."}`;
    }

    async function handleLocalCommand(text) {
      const command = text.trim().toLowerCase();
      if (command === "/memory on") {
        memoryToggle.checked = true;
        addMessage("system", "Memory preview enabled.");
        return true;
      }
      if (command === "/memory off") {
        memoryToggle.checked = false;
        addMessage("system", "Memory preview disabled.");
        return true;
      }
      if (command === "/help") {
        await fetchHelp();
        return true;
      }
      if (command === "/diagnostics") {
        const response = await fetch("/api/state");
        const state = await response.json();
        addMessage("system", diagnosticsText(state));
        renderState(state);
        return true;
      }
      if (command === "/learning") {
        const response = await fetch("/api/state");
        const state = await response.json();
        const rules = (state.adaptive?.rules || [])
          .map((entry) => `- [${entry.kind}] ${entry.rule} (${entry.hits || 1}x)`)
          .join("\n");
        addMessage("system", rules
          ? `Adaptive quality guidance\n${rules}`
          : "Adaptive quality guidance has not learned any rules yet.");
        return true;
      }
      if (command === "/learning reset") {
        const payload = await postJson("/api/learning/reset", {});
        addMessage("system", "Adaptive quality guidance reset. The character prompt was not changed.");
        renderState(payload.state);
        return true;
      }
      if (command === "/save" || command === "/quit" || command === "/exit") {
        await fetch("/api/save", { method: "POST" });
        addMessage("system", command === "/save"
          ? "State and conversation history saved."
          : "State saved. Stop the local server with Ctrl+C in the terminal when finished.");
        return true;
      }
      if (command === "/reset") {
        await resetEverything();
        return true;
      }
      if (command.startsWith("/url fetch ")) {
        await fetchSpecificUrl(text.replace(/^\/url fetch\s+/i, "").trim());
        return true;
      }
      if (command.startsWith("/url ingest ")) {
        await ingestSpecificUrl(text.replace(/^\/url ingest\s+/i, "").trim());
        return true;
      }
      if (command.startsWith("/pdf ingest ")) {
        await ingestPdfPath(text.replace(/^\/pdf ingest\s+/i, "").trim());
        return true;
      }
      if (command.startsWith("/text ingest ")) {
        addMessage("system", "Use the Text / Code file picker in the Knowledge panel for browser ingestion. The path-based /text command is available in the terminal app.");
        return true;
      }
      if (command.startsWith("/image read ")) {
        const raw = text.replace(/^\/image read\s+/i, "").trim();
        const parts = raw.split(/\s+::\s+/);
        await readImagePath(parts[0]?.trim() || "", parts.slice(1).join(" :: ").trim());
        return true;
      }
      if (command.startsWith("/")) {
        addMessage("system", "This browser PoC supports /memory on, /memory off, /diagnostics, /learning, /learning reset, /help, /save, /reset, /url fetch <url>, /url ingest <url>, /pdf ingest <path>, /text ingest <path>, /image read <path> [:: question], /quit, and /exit. Use the Text / Code file picker for browser ingestion and the terminal version for prompt, lorebook, teach, and skill commands.");
        return true;
      }
      return false;
    }

    async function fetchState() {
      const response = await fetch("/api/state");
      const state = await response.json();
      renderState(state);
      if (state.greeting) {
        addMessage("assistant", state.greeting, state.greeting_html || null);
      }
    }

    async function sendMessage() {
      const text = message.value.trim();
      if (!text) return;

      message.value = "";
      addMessage("user", text);

      if (await handleLocalCommand(text)) {
        message.focus();
        return;
      }

      setBusy(true);

      try {
        const response = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, show_memory: memoryToggle.checked })
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Request failed");
        if (payload.memory_preview) addMessage("memory", payload.memory_preview);
        addMessage("assistant", payload.reply, payload.reply_html || null);
        renderState(payload.state);
      } catch (error) {
        addMessage("system", String(error));
      } finally {
        setBusy(false);
        message.focus();
      }
    }

    send.addEventListener("click", sendMessage);
    save.addEventListener("click", async () => {
      await fetch("/api/save", { method: "POST" });
      addMessage("system", "State and conversation history saved.");
    });
    help.addEventListener("click", fetchHelp);
    reset.addEventListener("click", resetEverything);
    loadMemory.addEventListener("click", fetchMemory);
    memoryKind.addEventListener("change", fetchMemory);
    fetchUrlButton.addEventListener("click", () => fetchSpecificUrl());
    ingestUrlButton.addEventListener("click", () => ingestSpecificUrl());
    ingestPdfButton.addEventListener("click", () => ingestPdfPath());
    ingestTextFileButton.addEventListener("click", ingestSelectedTextFile);
    readImageButton.addEventListener("click", () => readImagePath());

    message.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    fetchState().then(() => {
      statusEl.textContent = "ready";
      fetchMemory();
      message.focus();
    }).catch((error) => {
      statusEl.textContent = "error";
      addMessage("system", String(error));
    });
  </script>
</body>
</html>
"""


class EngineBridge:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine = self._load_engine()
        self.turn = 0
        self._initialize_engine()

    def _load_engine(self):
        spec = importlib.util.spec_from_file_location("engram_research_engine", ENGINE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load engine from {ENGINE_PATH}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.render_system_message = lambda text: None
        return module

    def _initialize_engine(self):
        self.engine.load_history()
        self.engine.load_state()
        raw_prompt = self.engine.load_system_prompt(self.engine.current_prompt_path)
        self.engine.SYSTEM_PROMPT = raw_prompt.replace("{{char}}", self.engine.CHAR_NAME).replace("{{user}}", self.engine.USER_NAME)
        self.engine.GREETING = self.engine.load_greeting(self.engine.current_greeting_path)
        try:
            self.engine.resolve_served_model()
        except Exception:
            # Keep the UI available so configuration errors can be diagnosed there.
            pass
        self.turn = len(self.engine.CONVERSATION_HISTORY) // 2

    def state(self, include_greeting=False):
        memory = {
            "episodic": self.engine.episodic.count(),
            "semantic": self.engine.semantic.count(),
            "procedural": self.engine.procedural.count(),
        }
        state = {
            "character": self.engine.CHAR_NAME,
            "user": self.engine.USER_NAME,
            "prompt": self.engine.current_prompt_path,
            "model": self.engine.MODEL,
            "backend": self.engine.URL,
            "affect": self.engine.AFFECT.as_dict(),
            "relationship": self.engine.RELATIONSHIP.as_dict(),
            "adaptive": self.engine.adaptive_guidance_summary(),
            "memory": memory,
        }
        if include_greeting:
            state["greeting"] = self.engine.GREETING
            state["greeting_html"] = render_model_text(self.engine.GREETING or "")
        return state

    def chat(self, message: str, show_memory=False):
        with self.lock:
            self.engine.update_affect_from_input(message)
            self.engine.update_relationship_from_input(message)

            memory_preview = ""
            if show_memory:
                epi = self.engine.recall_episodic(message, self.engine.embed)
                sem = self.engine.recall_semantic(message, self.engine.embed)
                proc = self.engine.recall_procedural(message, self.engine.embed)
                memory_preview = self._format_memory_preview(sem, epi, proc)

            self.turn += 1
            reply = self.engine.chat(message, self.turn)
            return {
                "reply": reply,
                "reply_html": render_model_text(reply),
                "memory_preview": memory_preview,
                "state": self.state(),
            }

    def save(self):
        with self.lock:
            self.engine.save_state()
            self.engine.save_history()

    def reset_learning(self):
        with self.lock:
            self.engine.reset_adaptive_guidance()
            return {"state": self.state()}

    def fetch_url(self, url: str):
        with self.lock:
            return self.engine.fetch_url_content(url)

    def ingest_url(self, url: str):
        with self.lock:
            result = self.engine.ingest_url(url)
            return {**result, "state": self.state()}

    def ingest_pdf(self, path: str):
        with self.lock:
            result = self.engine.ingest_pdf(path)
            return {**result, "path": path, "state": self.state()}

    def ingest_text(self, filename: str, content: str):
        with self.lock:
            result = self.engine.ingest_text_content(content, filename)
            return {**result, "state": self.state()}

    def read_image(self, path: str, question: str = ""):
        with self.lock:
            result = self.engine.analyze_image(path, question)
            return {
                **result,
                "analysis_html": render_model_text(result.get("analysis", "")),
            }

    def reset_all(self):
        with self.lock:
            backup_dir = self._create_backup()

            for collection in (self.engine.episodic, self.engine.semantic, self.engine.procedural):
                ids = collection.get().get("ids", [])
                if ids:
                    collection.delete(ids=ids)

            preserved_prompt = self.engine.current_prompt_path
            preserved_greeting = self.engine.current_greeting_path

            self.engine.CONVERSATION_HISTORY = []
            self.engine.AFFECT = self.engine.AffectState()
            self.engine.RELATIONSHIP = self.engine.RelationshipState()
            self.engine.reset_adaptive_guidance()
            self.engine.current_prompt_path = preserved_prompt
            self.engine.current_greeting_path = preserved_greeting
            self.engine.SHOW_MEMORY = False
            self.engine.active_lorebook = None
            self.turn = 0

            raw_prompt = self.engine.load_system_prompt(self.engine.current_prompt_path)
            self.engine.SYSTEM_PROMPT = raw_prompt.replace("{{char}}", self.engine.CHAR_NAME).replace("{{user}}", self.engine.USER_NAME)
            self.engine.GREETING = self.engine.load_greeting(self.engine.current_greeting_path)

            self.engine.save_state()
            self.engine.save_history()

            return {
                "backup_path": str(backup_dir),
                "state": self.state(include_greeting=True),
            }

    def _create_backup(self):
        self.engine.save_state()
        self.engine.save_history()

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_char = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in self.engine.CHAR_NAME)
        safe_user = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in self.engine.USER_NAME)
        backup_dir = APP_DIR / "backups" / f"reset_{timestamp}_{safe_char}_{safe_user}"
        backup_dir.mkdir(parents=True, exist_ok=False)

        files_to_backup = [
            APP_DIR / self.engine.STATE_FILE,
            APP_DIR / self.engine.HISTORY_FILE,
            APP_DIR / self.engine.APP_SETTINGS_FILE,
            APP_DIR / self.engine.ADAPTIVE_PROMPT_FILE,
        ]
        for path in files_to_backup:
            if path.exists():
                shutil.copy2(path, backup_dir / path.name)

        db_path = APP_DIR / self.engine.DBPATH
        if db_path.exists():
            shutil.copytree(db_path, backup_dir / db_path.name)

        manifest = {
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "character": self.engine.CHAR_NAME,
            "user": self.engine.USER_NAME,
            "preserved_prompt": self.engine.current_prompt_path,
            "preserved_greeting": self.engine.current_greeting_path,
            "backed_up": [path.name for path in files_to_backup if path.exists()],
            "database_backup": db_path.name if db_path.exists() else None,
        }
        with open(backup_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        return backup_dir

    def memory_items(self, kind: str, limit: int = 8):
        collections = {
            "episodic": self.engine.episodic,
            "semantic": self.engine.semantic,
            "procedural": self.engine.procedural,
        }
        if kind not in collections:
            raise ValueError("Unknown memory collection.")

        limit = max(1, min(limit, 50))
        collection = collections[kind]
        count = collection.count()
        if count == 0:
            return {"kind": kind, "count": 0, "items": []}

        result = collection.get(include=["documents", "metadatas", "embeddings"])
        ids = result.get("ids", [])
        docs = result.get("documents", [])
        metas = result.get("metadatas", [])
        embeddings = result.get("embeddings", [])

        rows = []
        for index, item_id in enumerate(ids):
            meta = metas[index] if index < len(metas) and metas[index] else {}
            doc = docs[index] if index < len(docs) and docs[index] else ""
            embedding = embeddings[index] if index < len(embeddings) else []
            rows.append({
                "id": item_id,
                "content": doc,
                "metadata": meta,
                "embedding": embedding,
                "timestamp": meta.get("timestamp", 0.0) if isinstance(meta, dict) else 0.0,
            })

        rows.sort(key=lambda row: row["timestamp"], reverse=True)
        items = []
        for row in rows[:limit]:
            vector = row["embedding"]
            if vector is None:
                vector = []
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            vector_preview = [round(float(value), 4) for value in list(vector)[:12]]
            content = row["content"]
            if len(content) > 420:
                content = content[:420].rstrip() + "..."
            items.append({
                "id": row["id"],
                "content": content,
                "metadata": row["metadata"],
                "vector_preview": vector_preview,
                "vector_dimensions": len(vector),
            })

        return {"kind": kind, "count": count, "items": items}

    def _format_memory_preview(self, sem, epi, proc) -> str:
        sections = []
        if sem:
            sections.append("Semantic\n" + "\n".join(f"- {item}" for item in sem[:5]))
        if epi:
            lines = []
            for item in epi[:5]:
                meta = item.get("meta", {}) or {}
                stamp = meta.get("datestring", "unknown")
                text = item.get("text", "")
                lines.append(f"- [{stamp}] {text[:240]}")
            sections.append("Episodic\n" + "\n".join(lines))
        if proc:
            sections.append("Procedural\n" + "\n".join(f"- {item[:240]}" for item in proc[:5]))
        return "\n\n".join(sections) if sections else "No relevant memories found."


class Handler(BaseHTTPRequestHandler):
    bridge: EngineBridge | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(HTML)
            return
        if path == "/api/state":
            self._send_json(self.bridge.state(include_greeting=True))
            return
        if path == "/api/memory":
            params = parse_qs(parsed.query)
            kind = params.get("kind", ["episodic"])[0]
            try:
                limit = int(params.get("limit", ["8"])[0])
            except ValueError:
                limit = 8
            try:
                self._send_json(self.bridge.memory_items(kind, limit=limit))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if path == "/api/help":
            self._send_json({"help": HELP_TEXT})
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/chat":
            self._handle_chat()
            return
        if path == "/api/save":
            self.bridge.save()
            self._send_json({"ok": True})
            return
        if path == "/api/url/fetch":
            self._handle_url_fetch()
            return
        if path == "/api/url/ingest":
            self._handle_url_ingest()
            return
        if path == "/api/pdf/ingest":
            self._handle_pdf_ingest()
            return
        if path == "/api/text/ingest":
            self._handle_text_ingest()
            return
        if path == "/api/image/read":
            self._handle_image_read()
            return
        if path == "/api/reset":
            try:
                self._send_json(self.bridge.reset_all())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return
        if path == "/api/learning/reset":
            try:
                self._send_json(self.bridge.reset_learning())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=500)
            return
        self.send_error(404)

    def _handle_chat(self):
        try:
            payload = self._read_json()
            message = str(payload.get("message", "")).strip()
            if not message:
                self._send_json({"error": "Message is empty."}, status=400)
                return
            result = self.bridge.chat(message, show_memory=bool(payload.get("show_memory")))
            self._send_json(result)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_url_fetch(self):
        try:
            payload = self._read_json()
            url = str(payload.get("url", "")).strip()
            if not url:
                self._send_json({"error": "URL is empty."}, status=400)
                return
            self._send_json(self.bridge.fetch_url(url))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_url_ingest(self):
        try:
            payload = self._read_json()
            url = str(payload.get("url", "")).strip()
            if not url:
                self._send_json({"error": "URL is empty."}, status=400)
                return
            self._send_json(self.bridge.ingest_url(url))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_pdf_ingest(self):
        try:
            payload = self._read_json()
            path = str(payload.get("path", "")).strip()
            if not path:
                self._send_json({"error": "PDF path is empty."}, status=400)
                return
            self._send_json(self.bridge.ingest_pdf(path))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_text_ingest(self):
        try:
            payload = self._read_json()
            filename = str(payload.get("filename", "")).strip()
            content = str(payload.get("content", ""))
            if not filename:
                self._send_json({"error": "Text/code filename is empty."}, status=400)
                return
            if not content.strip():
                self._send_json({"error": "Text/code file content is empty."}, status=400)
                return
            self._send_json(self.bridge.ingest_text(filename, content))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _handle_image_read(self):
        try:
            payload = self._read_json()
            path = str(payload.get("path", "")).strip()
            question = str(payload.get("question", "")).strip()
            if not path:
                self._send_json({"error": "Image path is empty."}, status=400)
                return
            self._send_json(self.bridge.read_image(path, question))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _send_html(self, html: str):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"[ENGRAM PoC] {self.address_string()} - {fmt % args}")


def find_port(start=DEFAULT_PORT, attempts=20):
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No available local port found.")


def main():
    print("Loading ENGRAM engine. This can take a moment while embeddings initialize.")
    Handler.bridge = EngineBridge()
    port = find_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ENGRAM PoC UI running at http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        Handler.bridge.save()
        print("\nState saved. Goodbye.")


if __name__ == "__main__":
    main()
