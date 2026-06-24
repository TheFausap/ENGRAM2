# Overview of `3chat.research.py`

This document explains what is implemented in `3chat.research.py` in plain language. The file implements a local, memory-augmented chat assistant: a command-line chatbot that talks through a local language model, remembers past conversations, can load character/world information, can search the web when needed, and keeps a simple internal "affect" state that changes how the character speaks.

## Short Summary

`3chat.research.py` is a prototype for a roleplay or research-style AI companion. It is designed to run on the user's own machine rather than through a hosted chat service.

At a high level, it:

- Talks to a local AI model served by LM Studio, with optional older support for Ollama.
- Stores and retrieves memories using ChromaDB.
- Splits memory into three types: factual knowledge, remembered events, and learned procedures.
- Loads a character prompt and greeting from local files.
- Tracks the current character name and user name.
- Changes its speaking style based on a lightweight internal affect state, such as curiosity, concern, calm, amusement, unease, trust, and focus.
- Can load "lorebooks," which are folders or JSON files containing background information.
- Can search the web using DuckDuckGo when it believes a message needs outside verification.
- Saves conversation history and runtime state between sessions.
- Provides slash commands for changing settings, debugging, viewing memory, loading lorebooks, ingesting documents, and listing learned skills.

## What Kind of Program Is This?

This is a command-line chat application. When the script starts, it loads a character prompt, prints a greeting, then waits for the user to type messages.

The assistant is not just generating replies from the latest user message. Before answering, it gathers context from several places:

- Recent conversation history.
- Long-term memories stored in ChromaDB.
- The active character prompt.
- The current time and day.
- The assistant's internal affect state.
- Optional web search results.
- Optional lorebook content.

It then sends all of that to a local AI model and displays the assistant's reply.

There is also a graphical PoC launcher in `engram_web_poc.py`. It runs a small local Python web server and opens the experience in a browser-style interface: chat transcript, memory preview toggle, affect visualization, relationship state, memory counts, diagnostics-style state panels, and a read-only memory inspector.

To launch the recommended graphical PoC:

`.venv/bin/python engram_web_poc.py`

Then open the local URL printed in the terminal, usually:

`http://127.0.0.1:8765`

There is also an optional Tkinter desktop experiment in `engram_poc_ui.py`, but the project virtual environment on this machine does not currently include Tkinter support.

To recreate the Python environment, install the curated dependencies from:

`requirements.txt`

For example:

`.venv/bin/python -m pip install -r requirements.txt`

The graphical PoC includes:

- A **Help** button that lists supported browser commands and reminds the presenter which extra commands are available in the terminal version.
- A **Memory** selector for episodic, semantic, and procedural memories.
- A **Show** button that displays stored memory entries with partial content, metadata, and the first few vector dimensions.
- A **Knowledge** panel that can fetch a specific URL, ingest a URL into semantic memory, ingest a local PDF path, or select a local text/source-code file for semantic-memory ingestion.
- The text/code picker supports common documentation, data, configuration, web, shell, and programming-language extensions. Files are read locally in the browser, validated by the server, split into chunks, and tagged with their filename and detected language.
- An image-reading control that sends a local PNG/JPEG/WebP/GIF to LM Studio using the OpenAI-style image message format. This requires the currently loaded local model to support image input.
- Markdown rendering for assistant replies, including lists, code blocks, tables, and blockquotes.
- LaTeX math rendering for expressions enclosed in `$...$` or `$$...$$`.
- A **Reset** button that backs up the current state, history, and memory database under `backups/`, then clears active memories and conversation state while keeping the current character, user, prompt, and greeting.

## Main External Components

The file depends on several external tools and libraries:

- **LM Studio**: The main local AI server. The code expects it at `http://localhost:1234/v1`.
- **Ollama**: There is a separate function for Ollama at `http://localhost:11434`, though the main chat path uses LM Studio.
- **ChromaDB**: Stores long-term memories in a local vector database.
- **Sentence Transformers**: Converts text into numerical representations so memories can be searched by meaning, not only by exact words.
- **DuckDuckGo search through `ddgs`**: Used for web lookups.
- **Requests plus an HTML text extractor**: Used to fetch and read a specific URL supplied by the user.
- **pypdf**: Used to extract text from PDF files for ingestion.
- **Pillow**: Used to validate image files and read basic image metadata before sending images to a vision-capable local model.
- **A local `ui.py` module**: Handles formatted output for user messages, assistant messages, memory blocks, and system messages.

## Character and User Settings

The script has two names that shape the experience:

- `CHAR_NAME`: the assistant or character name.
- `USER_NAME`: the user's name.

By default, these come from `app_settings.json`. If that file does not exist, the script uses:

- Character: `MEP`
- User: `Klaus`

These names are also used to create separate files and databases. For example, different character/user combinations get different memory databases and history files.

The script creates or uses files with names like:

- `app_settings.json`
- `state_<character>_<user>.json`
- `history_<character>_<user>.json`
- `chroma_db_<character>_<user>/`

This means each character/user pairing can have its own memory and conversation history.

## The Three Memory Types

The program uses three separate ChromaDB collections. Each one is meant for a different kind of memory.

### 1. Episodic Memory

Episodic memory stores events or conversation moments. In everyday terms, this is like a diary.

Examples of what it stores:

- A user message and the assistant's reply.
- A log of the assistant's affect state.
- Replies that are not considered reusable facts or procedures.

Each episodic memory can include metadata such as:

- Time of storage.
- Readable date string.
- Conversation turn number.
- Source, such as `conversation`, `assistant_reply`, or `affect_layer`.

### 2. Semantic Memory

Semantic memory stores reusable knowledge or general facts. In everyday terms, this is like the assistant's notebook of things it may want to remember later.

Examples:

- A distilled insight from a conversation.
- A general fact extracted from a reply.
- Information loaded from a lorebook.
- Self-critique notes if they are considered important.

The code asks the local AI model to help decide whether something is important enough to store as semantic memory.

### 3. Procedural Memory

Procedural memory stores instructions, skills, or step-by-step knowledge. In everyday terms, this is like a recipe book or instruction manual.

Examples:

- Procedures extracted from a document.
- Assistant replies that look like instructions.
- Skills loaded from a lorebook.

The user can later list and search these learned skills with slash commands.

## How Memory Recall Works

When the user sends a normal message, the script searches memory before generating the reply.

It does this by converting the user's message into an embedding, which is a numerical representation of meaning. It then asks ChromaDB for nearby memories.

The assistant retrieves:

- Up to 5 episodic memories.
- Up to 5 semantic memories.
- Up to 5 procedural memories.

These memories are placed into the hidden context sent to the AI model. The user normally does not see this unless memory display is turned on with `/memory on`.

Newer stored memories also include emotional and relationship metadata. This means the database can record not just what was said, but also whether the moment involved repair, distress, appreciation, high trust, high concern, or an explicit request to remember something.

## Internal Affect State

The file implements a small emotional or behavioral state called `AFFECT`.

It tracks:

- `curiosity`
- `concern`
- `calm`
- `focus`
- `amusement`
- `unease`
- `trust`
- `warmth`
- `vulnerability`

This does not mean the program actually feels those things. It is a set of numbers used to influence the assistant's style.

For example:

- If the user mentions danger, threat, attack, or breach, concern and unease may rise.
- If the user says thanks, trust and calm may rise.
- If the user asks how or why, curiosity and focus may rise.
- If the user mentions jokes or funny things, amusement may rise.

The script also compares the user's message against small concept phrases, such as "threat danger attack breach overrun" or "funny joke laugh absurd," to adjust affect semantically.

Before generating a reply, the affect state is translated into a short speaking instruction. For example, if unease is high, the assistant may be told to speak with subtle tension. If curiosity is high, it may be told to show interest and ask follow-up questions.

## Relationship and Expressivity State

The script also keeps a separate relationship state. This gives the assistant a better memory of conversational tone and preferred interaction style.

It tracks:

- Familiarity.
- Closeness.
- Boundary sensitivity.
- Whether conversational repair is needed.
- Shared humor.
- Preferred level of detail.
- Preferred mode, such as direct, immersive, gentle, or adaptive.
- The user's most recent tone.

The assistant detects simple signals in the user's message, such as appreciation, distress, playfulness, correction, or brevity. These signals influence pacing and tone. For example, if the user corrects the assistant, the next reply is guided to briefly acknowledge the correction and adjust course. If the user sounds overwhelmed, the assistant is guided to slow down and offer one manageable next step.

The relationship state is saved in the same state file as the affect layer, so it can persist between sessions.

## Character Prompts and Greetings

The assistant's identity comes from prompt files.

The system prompt is loaded from:

`prompts/<prompt name>`

The greeting is loaded from:

`prompts/greetings/<greeting name>`

Both can include placeholders:

- `{{char}}`
- `{{user}}`

These are replaced with the current character and user names.

The script can also add extra user-specific context from:

`prompts/users/<USER_NAME>.txt`

If that file exists, its contents are appended to the character prompt.

## Lorebook Support

The program can load lorebooks. A lorebook is background information that can be added to memory.

Two formats are supported.

### Folder-Based Lorebooks

A lorebook folder can contain these subfolders:

- `semantic`
- `procedural`
- `episodic`

Files inside those folders are loaded into the matching memory type.

For example:

- Files in `lorebooks/my_world/semantic/` become semantic memories.
- Files in `lorebooks/my_world/procedural/` become procedural memories.
- Files in `lorebooks/my_world/episodic/` become episodic memories.

### JSON Lorebooks

The script can also load a `.json` lorebook. It expects the JSON file to contain items with a `content` field. Each `content` value is stored as semantic memory.

Lorebook entries are tagged with metadata so they can later be unloaded.

## Web Search

The assistant can decide to search the web before answering.

It asks the local AI model whether the user's message needs outside verification. Web search may be triggered when the input:

- Names a company, public figure, or organization.
- Mentions an event, date, or breach.
- Makes a factual claim that could be checked against news.
- Discusses legal, financial, or political developments.

If search is needed, the script:

1. Asks the local model to turn the user message into a better search query.
2. Searches DuckDuckGo.
3. Adds the search results into the assistant's hidden context.

This gives the assistant some live information, though the results are only as reliable as the search snippets returned.

## Conversation Flow

For an ordinary user message, the program follows roughly this sequence:

1. The user types a message.
2. The script updates the affect state.
3. It retrieves relevant memories.
4. It decides whether web search is needed.
5. It builds a large hidden system message containing:
   - current date and time,
   - character prompt,
   - narrative directive,
   - human-like conversation directive,
   - affect instruction,
   - relationship context,
   - remembered context,
   - optional web search results.
6. It sends the current conversation history to LM Studio.
7. It receives the assistant reply.
8. It stores the exchange in episodic memory.
9. It stores an affect log.
10. It tags the stored memory with emotional and relationship metadata.
11. It decides whether the reply should also become procedural or semantic memory.
12. It asks the model to reflect on the reply and possibly store a distilled insight.
13. It asks the model to self-critique the reply and possibly produce an improved version.
14. It returns the improved version if one exists; otherwise it returns the original reply.

This means each normal chat message may involve several local AI calls, not just one.

## Conversation History

The script keeps recent chat history in memory while running.

It keeps up to 35 turns. One turn means one user message plus one assistant message. So the maximum in-memory history is about 70 messages.

When the user exits with `/quit` or `/exit`, the history is saved to:

`history_<character>_<user>.json`

When the script starts again, it loads that history back.

## Saved Runtime State

The script saves state to:

`state_<character>_<user>.json`

Saved state includes:

- Current prompt file.
- Current greeting file.
- Whether memory display is on.
- Active lorebook.
- Current affect values.
- Current relationship and expressivity values.

This lets the assistant resume with similar settings later.

## Slash Commands

The script implements several commands. These are typed instead of a normal message.

### Debug Commands

`/debug prompt`

Shows the currently loaded prompt path and a preview of the prompt.

`/debug state`

Shows current state, including prompt, greeting, lorebook, memory display setting, and affect values.

`/diagnostics`

Shows the current affect values.

### Memory Display

`/memory on`

Shows retrieved semantic, episodic, and procedural memories before each reply.

`/memory off`

Turns that display off.

### Character and User Names

`/char <name>`

Changes the saved character name in `app_settings.json`. The script says to restart afterward because the database and filenames are based on the name loaded at startup.

`/user <name>`

Changes the saved user name in `app_settings.json`. A restart is also needed for the same reason.

### Prompt and Greeting

`/prompt <filename>`

Loads a new prompt from the `prompts` folder.

`/greeting set <filename>`

Loads a greeting from `prompts/greetings`.

`/greeting reload`

Reloads the current greeting and prints it again.

### Lorebooks

`/lorebook load <name>`

Loads a lorebook from the `lorebooks` folder, or loads a JSON lorebook.

`/lorebook list`

Lists available lorebook folders.

`/lorebook unload <name>`

Removes memory entries tagged as coming from that lorebook.

### Document and Skill Ingestion

`/ingest <path>`

Reads a text file and extracts procedures from it into procedural memory.

`/teach`

Lets the user paste text directly into the terminal. The user finishes by typing `/end` on a new line. The pasted text is then processed for procedures.

`/skill`

Lists stored procedural skills.

`/skill search <query>`

Searches stored procedural skills by plain text matching.

`/skill full <number>`

Shows the full text of a stored skill.

### Exit

`/quit` or `/exit`

Saves state and history, then exits.

## Narrative Behavior

The script includes a built-in "global narrative directive." This tells the assistant to actively move a story forward, introduce other characters, add dynamic events, and end replies with decisions for the user.

This suggests the assistant is meant for an interactive narrative, roleplay, simulation, or investigation experience rather than a neutral question-answer chatbot.

## Self-Reflection and Self-Critique

After generating a reply, the script performs two extra internal checks.

### Reflection

It asks the model to analyze its own reply and extract one stable insight, if there is one. If the insight is important enough, it is stored as semantic memory.

### Self-Critique

It asks the model to review the exchange for:

- Quality.
- Character consistency.
- Accuracy.
- Hallucinations.
- Logic errors.
- Character breaks.

The model is asked to return both a critique and an improved answer. The critique and improved answer may be stored in semantic memory if considered important. The user receives the improved answer if one was produced.

## Data Created by the Program

During use, the script can create or update:

- `app_settings.json`
- `state_<character>_<user>.json`
- `history_<character>_<user>.json`
- `chroma_db_<character>_<user>/`

It can also read from:

- `prompts/`
- `prompts/greetings/`
- `prompts/users/`
- `lorebooks/`
- Any text file passed to `/ingest`

## Important Limitations and Things to Watch

This file is a research prototype, so a few rough edges are visible.

1. **It depends heavily on local services.**

   LM Studio must be running and serving a compatible model at `http://localhost:1234/v1`. If it is not running, replies will fail with a connection error.

2. **Each normal message can trigger many model calls.**

   A single user message may call the model for the main answer, web-search decision, search-query generation, procedure detection, importance scoring, general-knowledge classification, reflection, and self-critique. This can make the assistant slow.

3. **Some commands assume folders exist.**

   For example, `/lorebook list` assumes there is a `lorebooks` folder.

4. **Some error paths may not be handled safely.**

   For example, if lorebook loading returns an error string, the command handler still expects a dictionary with counts.

5. **The memory storage rules are experimental.**

   The assistant asks the model to decide what is important, general, procedural, or worth remembering. These decisions may be inconsistent.

6. **Web search uses snippets.**

   The search tool stores source URLs and short content snippets, not full verified articles. The assistant may still need careful source checking for serious research.

7. **Changing character or user names requires restart.**

   The command updates settings, but database paths and filenames are created near startup. The program correctly tells the user to restart.

8. **The assistant can store a lot of memory over time.**

   Episodic memory is pruned every 100 turns, with a default maximum of 2000 items. Other memory types are not similarly pruned in this file.

## In Plain English

This file builds a local AI character that can remember, learn from documents, look up outside information, and adjust its tone based on the conversation. It is not just a simple chatbot. It is closer to a small experimental assistant engine with memory, personality, self-review, and story-driving behavior.

The most important idea is that the assistant is designed to carry context forward. It remembers conversations, stores useful facts, learns procedures, loads fictional or background knowledge, and uses all of that to shape future replies.
