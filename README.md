# ComfyUI Assistent

A floating, draggable chat assistant embedded in the ComfyUI graph editor.

## Features

- Collapsible, draggable chat button and panel.
- Cancel button while the model is responding.
- **New chat** button next to Send; each workflow keeps its own chat session, and sessions
  switch automatically when you change workflows.
- Reads the active workflow and the nodes installed in this ComfyUI.
- Adds, removes, connects, disconnects, repositions nodes and sets widget values.
- Sees input and generated images (vision models) and can write prompts into the correct
  positive/negative text encoder, then critique or refine a prompt against the output.
- Knows the current canvas selection (including multiple nodes) when something is selected, and
  can highlight the nodes it refers to when explaining how something works.
- Arranges the nodes it adds in a readable left-to-right layout, and after building runs a
  follow-up pass to connect missing required inputs (then reports anything still unconnected).
  Bot edits are undoable and mark the workflow as modified.
- Automatic context compaction: when the conversation approaches the model's context window, older
  turns are summarized so local models don't overflow. There is no fixed message cap; use the
  **Compact chat** quick action to compact on demand.
- Remembers corrections and preferences across sessions: when you correct the bot or state a
  preference it saves a short rule and applies relevant rules in future sessions. Manage them under
  **Settings → Memory** (add/edit/delete/pin/clear).
- Local knowledge base (SQLite FTS5) of every installed pack's markdown plus the official
  ComfyUI documentation; exposed to the model via `search_docs` and `get_node_docs`.
- Web search for finding and recommending custom node packs.
- Installs custom node packs with `git clone` (plus `requirements.txt`) after confirmation.
- Providers: LM Studio, Ollama, any OpenAI-compatible endpoint, and Anthropic.
- Settings and chat history persist across restarts.

## Setup

1. Restart ComfyUI after installing this node.
2. Click the chat bubble, open the **Settings** tab.
3. Pick a provider, set the base URL and API key, then choose a model from the dropdown
   (populated automatically; **Refresh** re-fetches the list), and **Save**.
4. Optionally set a web search provider (Tavily, Brave, or SerpAPI) and its API key.

Default base URLs:

| Provider | URL |
| --- | --- |
| LM Studio | `http://127.0.0.1:1234/v1` |
| Ollama | `http://127.0.0.1:11434/v1` |
| OpenAI-compatible | `https://api.openai.com/v1` |
| Anthropic | `https://api.anthropic.com` |

## Notes

- API keys are stored in `chatbot_config.json` in this folder and are never sent to the browser.
- Chat history is stored per workflow in `chatbot_history.json` (keyed by workflow path or id);
  attached image data is not persisted, only a count. The file is gitignored.
- Images require a vision-capable model. Vision support is detected from the provider (LM Studio
  `type: vlm`, Ollama `capabilities`, else a name heuristic) and shown in **Settings → Vision**.
  Attach images with the paperclip (**Input image(s)**, **Last output**, **Choose file**), or use
  the **Critique output vs prompt** / **Write prompt from image** quick actions. *Always include*
  toggles are off by default. Images are downscaled to the configured max dimension (default 1024,
  JPEG q0.85) before sending.
- **Selection awareness** (Settings, on by default): when you have nodes selected, the current
  selection is included with your message so "this"/"these"/"the selected node(s)" works. With no
  selection, nothing is added and the model reasons from the workflow as usual. **Highlight nodes
  the model refers to** (Settings, on by default) lets the model select nodes on the canvas via the
  `highlight_nodes` tool while explaining how they work.
- **Context compaction** (Settings, on by default): the context window is auto-detected (LM Studio
  `loaded_context_length`, Ollama `context_length`; override under **Context window**). When the
  conversation gets close to the limit, older turns are summarized so local models don't overflow.
  **Keep last messages verbatim** controls how many recent messages stay untouched. Use the
  **Compact chat** quick action to compact on demand.
- **Auto-arrange added nodes after building** (Settings, on by default): after a build the bot lays
  out the nodes it added in left-to-right execution order, placing them to the right of your
  existing graph so your layout is untouched. **Check connections after building** (Settings, on by
  default): the bot validates required inputs, runs one follow-up pass to connect anything missing,
  and warns you if problems remain. Tools: `layout_workflow` (scope `added`/`all`) and
  `validate_workflow`.
- The knowledge base is stored in `kb_index.sqlite` and `kb_cache/` (gitignored). It is built in
  the background on first start, updated incrementally on later starts, and re-indexed after a git
  install. The official docs (`docs.comfy.org/llms-full.txt`) are fetched once and refreshed when
  stale. Use **Knowledge Base → Rebuild index / Sync official docs** in settings to force it.
- Git installs run `git clone --depth 1 <url>` into `custom_nodes/<name>` and then
  `python -m pip install -r requirements.txt` with the ComfyUI interpreter. ComfyUI must be
  restarted before the new nodes are available.
- Some local models do not support native tool calling. Turn off **Native tool calling** in
  settings; the model will then emit fenced JSON action blocks that the panel executes.
