from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.text import Text
from rich.table import Table
import re

console = Console()

def render_user_message(text: str):
    console.print(
        Panel(
            Text(text, style="bold white"),
            title="You",
            title_align="left",
            border_style="cyan"
        )
    )

def render_assistant_message(text: str):
    # Process LaTeX math enclosed in $ ... $ or $$ ... $$
    try:
        from pylatexenc.latex2text import LatexNodes2Text
        converter = LatexNodes2Text()
        
        def replacer(match):
            math_str = match.group(1)
            try:
                return converter.latex_to_text(math_str)
            except Exception:
                return match.group(0) # fallback on error
                
        # Match $$ ... $$ or $ ... $
        text = re.sub(r'\$\$(.*?)\$\$', replacer, text, flags=re.DOTALL)
        text = re.sub(r'\$(.*?)\$', replacer, text)
        
    except ImportError:
        # Fallbacks if pylatexenc is not installed
        text = text.replace("$\\rightarrow$", "→")
        text = text.replace("\\rightarrow", "→")

    console.print(
        Panel(
            Markdown(text),
            title="Assistant",
            title_align="left",
            border_style="green"
        )
    )

def render_memory_block(title: str, items: list[str]):
    if not items:
        return
    table = Table(show_header=False, box=None, padding=(0,1))
    for item in items:
        table.add_row(f"- {item}")
    console.print(
        Panel(
            table,
            title=title,
            title_align="left",
            border_style="magenta"
        )
    )

def render_system_message(text: str):
    console.print(
        Panel(
            Text(text, style="yellow"),
            title="System",
            border_style="yellow"
        )
    )

