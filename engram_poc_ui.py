import importlib.util
import math
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext, ttk


APP_DIR = Path(__file__).resolve().parent
ENGINE_PATH = APP_DIR / "3chat.research.py"


class EngramPoCUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ENGRAM PoC")
        self.root.geometry("1180x760")
        self.root.minsize(980, 640)

        self.engine = None
        self.turn = 0
        self.busy = False
        self.show_memory = tk.BooleanVar(value=False)
        self.ui_queue: queue.Queue[tuple[str, str]] = queue.Queue()

        self.affect_colors = {
            "curiosity": "#4ea8de",
            "concern": "#ff8c42",
            "calm": "#72b01d",
            "focus": "#7b61ff",
            "amusement": "#f7b801",
            "unease": "#d1495b",
            "trust": "#2a9d8f",
            "warmth": "#e76f51",
            "vulnerability": "#b56576",
        }

        self._build_layout()
        self.append_system("Loading ENGRAM engine...")
        self.root.update_idletasks()
        self._load_engine()
        self._poll_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_layout(self):
        self.root.configure(bg="#15171c")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#15171c")
        style.configure("Side.TFrame", background="#1e222b")
        style.configure("TLabel", background="#1e222b", foreground="#e8edf2")
        style.configure("Muted.TLabel", background="#1e222b", foreground="#9aa4b2")
        style.configure("TButton", padding=(12, 8))
        style.configure("Send.TButton", padding=(18, 10))
        style.configure("Horizontal.TProgressbar", troughcolor="#2b303a", background="#6ccff6")

        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True)

        main = ttk.Frame(outer)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 8), pady=14)

        header = tk.Frame(main, bg="#15171c")
        header.pack(fill=tk.X, pady=(0, 10))

        self.title_label = tk.Label(
            header,
            text="ENGRAM Memory-Augmented Companion",
            bg="#15171c",
            fg="#f4f7fb",
            font=("Helvetica", 20, "bold"),
        )
        self.title_label.pack(side=tk.LEFT)

        self.status_label = tk.Label(
            header,
            text="initializing",
            bg="#15171c",
            fg="#9aa4b2",
            font=("Helvetica", 11),
        )
        self.status_label.pack(side=tk.RIGHT)

        self.chat = scrolledtext.ScrolledText(
            main,
            wrap=tk.WORD,
            bg="#0f1117",
            fg="#e8edf2",
            insertbackground="#e8edf2",
            relief=tk.FLAT,
            padx=18,
            pady=18,
            font=("Helvetica", 13),
        )
        self.chat.pack(fill=tk.BOTH, expand=True)
        self.chat.configure(state=tk.DISABLED)
        self._configure_chat_tags()

        composer = tk.Frame(main, bg="#15171c")
        composer.pack(fill=tk.X, pady=(12, 0))

        self.input_box = tk.Text(
            composer,
            height=4,
            wrap=tk.WORD,
            bg="#1e222b",
            fg="#f4f7fb",
            insertbackground="#f4f7fb",
            relief=tk.FLAT,
            padx=12,
            pady=10,
            font=("Helvetica", 13),
        )
        self.input_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.input_box.bind("<Return>", self._send_on_return)
        self.input_box.bind("<Shift-Return>", self._newline_on_shift_return)

        button_column = tk.Frame(composer, bg="#15171c")
        button_column.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

        self.send_button = ttk.Button(
            button_column,
            text="Send",
            style="Send.TButton",
            command=self.send_message,
        )
        self.send_button.pack(fill=tk.X)

        self.memory_check = ttk.Checkbutton(
            button_column,
            text="Show memory",
            variable=self.show_memory,
        )
        self.memory_check.pack(anchor=tk.W, pady=(10, 0))

        side = ttk.Frame(outer, style="Side.TFrame", width=320)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 14), pady=14)
        side.pack_propagate(False)

        self.identity_label = ttk.Label(side, text="Character", font=("Helvetica", 15, "bold"))
        self.identity_label.pack(anchor=tk.W, padx=14, pady=(14, 2))
        self.identity_detail = ttk.Label(side, text="Loading...", style="Muted.TLabel")
        self.identity_detail.pack(anchor=tk.W, padx=14, pady=(0, 12))

        self.affect_canvas = tk.Canvas(
            side,
            width=292,
            height=230,
            bg="#171b22",
            highlightthickness=0,
        )
        self.affect_canvas.pack(padx=14, pady=(0, 14))

        ttk.Label(side, text="Relationship", font=("Helvetica", 13, "bold")).pack(anchor=tk.W, padx=14)
        self.relationship_frame = ttk.Frame(side, style="Side.TFrame")
        self.relationship_frame.pack(fill=tk.X, padx=14, pady=(6, 14))
        self.relationship_rows = {}
        for name in ("familiarity", "closeness", "repair_need", "shared_humor"):
            self._add_metric_row(self.relationship_frame, name)

        ttk.Label(side, text="Memory", font=("Helvetica", 13, "bold")).pack(anchor=tk.W, padx=14)
        self.memory_stats = ttk.Label(side, text="episodic: -\nsemantic: -\nprocedural: -", style="Muted.TLabel")
        self.memory_stats.pack(anchor=tk.W, padx=14, pady=(6, 14))

        ttk.Label(side, text="Mode", font=("Helvetica", 13, "bold")).pack(anchor=tk.W, padx=14)
        self.mode_label = ttk.Label(side, text="detail: -\nmode: -\ntone: -", style="Muted.TLabel")
        self.mode_label.pack(anchor=tk.W, padx=14, pady=(6, 14))

        utility = ttk.Frame(side, style="Side.TFrame")
        utility.pack(fill=tk.X, side=tk.BOTTOM, padx=14, pady=14)
        ttk.Button(utility, text="Diagnostics", command=self.show_diagnostics).pack(fill=tk.X, pady=(0, 8))
        ttk.Button(utility, text="Save State", command=self.save_state).pack(fill=tk.X)

    def _configure_chat_tags(self):
        self.chat.tag_configure("system", foreground="#f4d35e", spacing1=8, spacing3=8)
        self.chat.tag_configure("user", foreground="#9be7ff", spacing1=12, spacing3=4, lmargin1=12, lmargin2=12)
        self.chat.tag_configure("assistant", foreground="#d8f3dc", spacing1=12, spacing3=8, lmargin1=12, lmargin2=12)
        self.chat.tag_configure("memory", foreground="#c77dff", spacing1=8, spacing3=8, lmargin1=12, lmargin2=12)
        self.chat.tag_configure("label", foreground="#f4f7fb", font=("Helvetica", 11, "bold"))

    def _add_metric_row(self, parent, name: str):
        row = ttk.Frame(parent, style="Side.TFrame")
        row.pack(fill=tk.X, pady=3)
        label = ttk.Label(row, text=name.replace("_", " "), width=15, style="Muted.TLabel")
        label.pack(side=tk.LEFT)
        bar = ttk.Progressbar(row, orient=tk.HORIZONTAL, length=120, mode="determinate", maximum=100)
        bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.relationship_rows[name] = bar

    def _load_engine(self):
        spec = importlib.util.spec_from_file_location("engram_research_engine", ENGINE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load engine from {ENGINE_PATH}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.engine = module

        # Route engine system messages into the GUI instead of Rich terminal panels.
        self.engine.render_system_message = lambda text: self.ui_queue.put(("system", str(text)))

        self.engine.load_history()
        self.engine.load_state()
        raw_prompt = self.engine.load_system_prompt(self.engine.current_prompt_path)
        self.engine.SYSTEM_PROMPT = raw_prompt.replace("{{char}}", self.engine.CHAR_NAME).replace("{{user}}", self.engine.USER_NAME)
        self.engine.GREETING = self.engine.load_greeting(self.engine.current_greeting_path)

        self.turn = len(self.engine.CONVERSATION_HISTORY) // 2
        self.identity_detail.configure(
            text=(
                f"{self.engine.CHAR_NAME} with {self.engine.USER_NAME}\n"
                f"prompt: {self.engine.current_prompt_path}\n"
                f"model: {self.engine.MODEL}"
            )
        )
        self.status_label.configure(text="ready")
        self.append_assistant(self.engine.GREETING or "Ready.")
        self.refresh_state_panel()

    def append_message(self, role: str, text: str):
        self.chat.configure(state=tk.NORMAL)
        labels = {
            "system": "SYSTEM",
            "user": "YOU",
            "assistant": getattr(self.engine, "CHAR_NAME", "ASSISTANT") if self.engine else "ASSISTANT",
            "memory": "MEMORY",
        }
        self.chat.insert(tk.END, f"\n{labels.get(role, role.upper())}\n", "label")
        self.chat.insert(tk.END, f"{text.strip()}\n", role)
        self.chat.see(tk.END)
        self.chat.configure(state=tk.DISABLED)

    def append_system(self, text: str):
        self.append_message("system", text)

    def append_user(self, text: str):
        self.append_message("user", text)

    def append_assistant(self, text: str):
        self.append_message("assistant", text)

    def append_memory(self, text: str):
        self.append_message("memory", text)

    def _send_on_return(self, event):
        if event.state & 0x0001:
            return None
        self.send_message()
        return "break"

    def _newline_on_shift_return(self, event):
        self.input_box.insert(tk.INSERT, "\n")
        return "break"

    def send_message(self):
        if self.busy or self.engine is None:
            return

        text = self.input_box.get("1.0", tk.END).strip()
        if not text:
            return

        self.input_box.delete("1.0", tk.END)
        self.append_user(text)

        if text.startswith("/"):
            self.handle_command(text)
            return

        self.busy = True
        self.send_button.configure(state=tk.DISABLED)
        self.status_label.configure(text="thinking...")
        include_memory_preview = self.show_memory.get()
        thread = threading.Thread(target=self._chat_worker, args=(text, include_memory_preview), daemon=True)
        thread.start()

    def handle_command(self, text: str):
        command = text.strip().lower()
        if command in ("/quit", "/exit"):
            self.on_close()
            return
        if command == "/memory on":
            self.show_memory.set(True)
            self.append_system("Memory preview enabled.")
            return
        if command == "/memory off":
            self.show_memory.set(False)
            self.append_system("Memory preview disabled.")
            return
        if command == "/diagnostics":
            self.show_diagnostics()
            return
        if command == "/save":
            self.save_state()
            return
        self.append_system("This PoC UI supports /memory on, /memory off, /diagnostics, /save, and /quit.")

    def _chat_worker(self, text: str, include_memory_preview: bool):
        try:
            self.engine.update_affect_from_input(text)
            self.engine.update_relationship_from_input(text)

            if include_memory_preview:
                epi = self.engine.recall_episodic(text, self.engine.embed)
                sem = self.engine.recall_semantic(text, self.engine.embed)
                proc = self.engine.recall_procedural(text, self.engine.embed)
                self.ui_queue.put(("memory", self._format_memory_preview(sem, epi, proc)))

            self.turn += 1
            reply = self.engine.chat(text, self.turn)
            self.ui_queue.put(("assistant", reply))
            self.ui_queue.put(("refresh", ""))
        except Exception as exc:
            self.ui_queue.put(("system", f"Error: {exc}"))
        finally:
            self.ui_queue.put(("done", ""))

    def _format_memory_preview(self, sem, epi, proc) -> str:
        sections = []
        if sem:
            sections.append("Semantic\n" + "\n".join(f"- {item}" for item in sem[:5]))
        if epi:
            lines = []
            for item in epi[:5]:
                meta = item.get("meta", {}) or {}
                stamp = meta.get("datestring", "unknown")
                lines.append(f"- [{stamp}] {item.get('text', '')[:220]}")
            sections.append("Episodic\n" + "\n".join(lines))
        if proc:
            sections.append("Procedural\n" + "\n".join(f"- {item[:220]}" for item in proc[:5]))
        return "\n\n".join(sections) if sections else "No relevant memories found."

    def _poll_queue(self):
        while True:
            try:
                role, text = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if role == "assistant":
                self.append_assistant(text)
            elif role == "system":
                self.append_system(text)
            elif role == "memory":
                self.append_memory(text)
            elif role == "refresh":
                self.refresh_state_panel()
            elif role == "done":
                self.busy = False
                self.send_button.configure(state=tk.NORMAL)
                self.status_label.configure(text="ready")

        self.root.after(100, self._poll_queue)

    def refresh_state_panel(self):
        if self.engine is None:
            return

        self._draw_affect_canvas()
        rel = self.engine.RELATIONSHIP.as_dict()
        for name, bar in self.relationship_rows.items():
            bar["value"] = float(rel.get(name, 0.0)) * 100

        self.mode_label.configure(
            text=(
                f"detail: {rel.get('preferred_detail', '-')}\n"
                f"mode: {rel.get('preferred_mode', '-')}\n"
                f"tone: {rel.get('last_user_tone', '-')}"
            )
        )

        try:
            self.memory_stats.configure(
                text=(
                    f"episodic: {self.engine.episodic.count()}\n"
                    f"semantic: {self.engine.semantic.count()}\n"
                    f"procedural: {self.engine.procedural.count()}"
                )
            )
        except Exception as exc:
            self.memory_stats.configure(text=f"memory stats unavailable\n{exc}")

    def _draw_affect_canvas(self):
        canvas = self.affect_canvas
        canvas.delete("all")
        canvas.create_text(146, 18, text="Affect constellation", fill="#e8edf2", font=("Helvetica", 12, "bold"))

        affect = self.engine.AFFECT.as_dict()
        names = list(affect.keys())
        cx, cy = 146, 126
        radius = 76

        for index, name in enumerate(names):
            angle = (math.tau * index / len(names)) - math.pi / 2
            value = float(affect.get(name, 0.0))
            node_radius = 8 + min(value, 1.5) * 8
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius
            color = self.affect_colors.get(name, "#9aa4b2")
            canvas.create_line(cx, cy, x, y, fill="#2b303a")
            canvas.create_oval(
                x - node_radius,
                y - node_radius,
                x + node_radius,
                y + node_radius,
                fill=color,
                outline="#f4f7fb" if value > 0.75 else "",
                width=1,
            )
            canvas.create_text(x, y + node_radius + 12, text=name[:8], fill="#9aa4b2", font=("Helvetica", 8))

        calm = affect.get("calm", 0.0)
        canvas.create_oval(cx - 22, cy - 22, cx + 22, cy + 22, fill="#202632", outline="#2b303a")
        canvas.create_text(cx, cy - 4, text="calm", fill="#e8edf2", font=("Helvetica", 9, "bold"))
        canvas.create_text(cx, cy + 10, text=f"{calm:.2f}", fill="#9aa4b2", font=("Helvetica", 9))

    def show_diagnostics(self):
        if self.engine is None:
            return
        affect = self.engine.AFFECT.as_dict()
        rel = self.engine.RELATIONSHIP.as_dict()
        affect_lines = "\n".join(f"{k}: {v:.2f}" for k, v in affect.items())
        rel_lines = "\n".join(f"{k}: {v}" for k, v in rel.items())
        self.append_system(f"Affect\n{affect_lines}\n\nRelationship\n{rel_lines}")
        self.refresh_state_panel()

    def save_state(self):
        if self.engine is None:
            return
        self.engine.save_state()
        self.engine.save_history()
        self.append_system("State and conversation history saved.")

    def on_close(self):
        if self.engine is not None:
            try:
                self.engine.save_state()
                self.engine.save_history()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    EngramPoCUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
