import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const RUNTIME_RULES =
  "Runtime rule: you can only see images that are attached to a message and reported as [N image(s) attached]. " +
  "If the user asks about an image and none is attached, say you cannot see it and ask them to attach it. " +
  "Never invent, infer, or guess image contents from the workflow. " +
  "When the user corrects a mistake or states a lasting preference, call remember_lesson with a short, general rule.";
const IMAGE_MENTION_RE = /\b(image|images|picture|pictures|photo|photos|render|rendered|output|generated|result|visual|screenshot|frame)\b/i;
const OUTPUT_MENTION_RE = /\b(output|outputs|generated|result|final|rendered)\b/i;
const INPUT_MENTION_RE = /\b(input|inputs|source|reference|loadimage|loaded)\b/i;
const CORRECTION_RE =
  /\b(no[,!. ]|nope|that'?s (wrong|not right|incorrect)|that is (wrong|not right)|not like that|don'?t |do not |stop doing|actually|you (were|are) wrong|you should have|i said|from now on|remember (that|this)|never (use|do)|always (use|do))\b/i;

const styleLink = document.createElement("link");
styleLink.rel = "stylesheet";
styleLink.href = new URL("./chatbot.css", import.meta.url).href;
document.head.appendChild(styleLink);

const TOOLS = [
  {
    type: "function",
    function: {
      name: "get_workflow_summary",
      description:
        "Return a compact summary of the active workflow: node ids, types, titles, modes, links, and groups. Call this before editing the graph.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "get_node_details",
      description: "Return widgets, inputs, outputs, position, and mode for one node by id.",
      parameters: {
        type: "object",
        properties: { id: { type: ["integer", "string"], description: "Node id" } },
        required: ["id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "search_installed_nodes",
      description:
        "Search the node types installed in this ComfyUI. Use this before adding a node to confirm the exact type name.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Name, category, or keyword" },
          limit: { type: "integer", description: "Max results, default 25" },
        },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "add_node",
      description: "Add a node of an installed type to the active workflow.",
      parameters: {
        type: "object",
        properties: {
          type: { type: "string", description: "Exact node type name" },
          x: { type: "number", description: "Optional canvas x" },
          y: { type: "number", description: "Optional canvas y" },
          title: { type: "string", description: "Optional custom title" },
          near: { type: ["integer", "string"], description: "Optional node id to place this node next to" },
        },
        required: ["type"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "remove_node",
      description: "Remove a node from the active workflow. Asks the user to confirm.",
      parameters: {
        type: "object",
        properties: { id: { type: ["integer", "string"], description: "Node id" } },
        required: ["id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "connect_nodes",
      description: "Connect a source node output slot to a target node input slot.",
      parameters: {
        type: "object",
        properties: {
          source_id: { type: ["integer", "string"] },
          source_slot: { type: "integer", description: "Output slot index" },
          target_id: { type: ["integer", "string"] },
          target_slot: { type: "integer", description: "Input slot index" },
        },
        required: ["source_id", "source_slot", "target_id", "target_slot"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "disconnect_link",
      description: "Remove a link by its id.",
      parameters: {
        type: "object",
        properties: { link_id: { type: ["integer", "string"] } },
        required: ["link_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "set_widget_value",
      description: "Set a widget value on a node (text, seed, steps, model name, etc.).",
      parameters: {
        type: "object",
        properties: {
          id: { type: ["integer", "string"] },
          name: { type: "string", description: "Widget name" },
          value: { description: "New value" },
        },
        required: ["id", "name", "value"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "move_node",
      description: "Move a node to a canvas position.",
      parameters: {
        type: "object",
        properties: {
          id: { type: ["integer", "string"] },
          x: { type: "number" },
          y: { type: "number" },
        },
        required: ["id", "x", "y"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "web_search",
      description: "Search the web. Use to find custom nodes, documentation, or current information.",
      parameters: {
        type: "object",
        properties: { query: { type: "string" }, count: { type: "integer" } },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "search_docs",
      description:
        "Search the local knowledge base of installed custom-node documentation and the official ComfyUI docs. Use this to learn what nodes do and how to use them.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string", description: "Keywords, node name, or question" },
          limit: { type: "integer", description: "Max results, default 6" },
          source: { type: "string", enum: ["pack", "official", "node"], description: "Optional source filter" },
        },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "get_node_docs",
      description:
        "Get documentation for a specific installed node type: its description plus its pack README/docs and official built-in docs.",
      parameters: {
        type: "object",
        properties: {
          type: { type: "string", description: "Exact node type name" },
          limit: { type: "integer", description: "Max results, default 8" },
        },
        required: ["type"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "list_prompt_nodes",
      description:
        "List the text/prompt nodes in the active workflow with their role (positive/negative) by tracing from samplers. Call this before inserting a prompt.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "set_prompt",
      description:
        "Insert prompt text into the correct text encoder. target 'positive' or 'negative' auto-traces from the sampler; use 'node:<id>' to force a specific node.",
      parameters: {
        type: "object",
        properties: {
          text: { type: "string", description: "The prompt text to insert" },
          target: { type: "string", description: "'positive', 'negative', or 'node:<id>'" },
        },
        required: ["text"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "get_selection",
      description:
        "Return the nodes currently selected on the canvas (may be multiple). Use this when the user refers to 'this', 'these', or 'the selected node(s)'.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "highlight_nodes",
      description:
        "Select (highlight) nodes on the canvas so the user can see them. Call this with the node ids you are referring to when explaining how something works.",
      parameters: {
        type: "object",
        properties: {
          ids: {
            type: "array",
            items: { type: ["integer", "string"] },
            description: "Node ids to highlight",
          },
          center: { type: "boolean", description: "Center the view on a single node (default true)" },
          add: { type: "boolean", description: "Add to the current selection instead of replacing it" },
        },
        required: ["ids"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "layout_workflow",
      description:
        "Arrange nodes readably in left-to-right execution order. scope 'added' (default) arranges only the nodes you added, keeping the rest of the layout; 'all' arranges the whole graph.",
      parameters: {
        type: "object",
        properties: {
          scope: { type: "string", enum: ["added", "all"], description: "Which nodes to arrange" },
        },
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "validate_workflow",
      description:
        "Check the workflow for unconnected required inputs, dangling links, and orphan nodes. Returns candidate source nodes so you can connect them. Call after building or rewiring.",
      parameters: { type: "object", properties: {}, required: [] },
    },
  },
  {
    type: "function",
    function: {
      name: "remember_lesson",
      description:
        "Save a short, general rule to remember across sessions so a mistake is not repeated. Use when the user corrects you or states a lasting preference.",
      parameters: {
        type: "object",
        properties: {
          text: { type: "string", description: "The rule to remember, e.g. 'Always connect the VAE before the decoder.'" },
          tags: { type: "string", description: "Optional comma-separated keywords" },
          pinned: { type: "boolean", description: "Always include this lesson (default false)" },
        },
        required: ["text"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "search_memory",
      description: "Search saved lessons from past corrections and preferences.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string" },
          limit: { type: "integer", description: "Max results, default 8" },
        },
        required: ["query"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "install_custom_node",
      description:
        "Install an uninstalled custom node pack by cloning its git repository into custom_nodes. The user must confirm. Requires a full git repository URL.",
      parameters: {
        type: "object",
        properties: {
          repo_url: { type: "string", description: "Full git URL, e.g. https://github.com/user/repo" },
          name: { type: "string", description: "Optional target folder name" },
        },
        required: ["repo_url"],
      },
    },
  },
];

const state = {
  config: null,
  messages: [],
  busy: false,
  nodeDefsPromise: null,
  abortController: null,
  cancelled: false,
  pendingConfirm: null,
  kbTimer: null,
  availableModels: [],
  attachments: [],
  visionSupported: null,
  lastOutputs: [],
  sessionKey: null,
  runId: 0,
  botHighlightedIds: [],
  runAddedNodeIds: [],
  runMutatedGraph: false,
  runArranged: false,
  fixPasses: 0,
  contextWindow: null,
  relevantLessons: [],
  lessons: [],
  correctionHint: false,
  debugBuffer: [],
  dom: {},
};

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(child);
  }
  return node;
}

function buildUi() {
  const root = el("div", { id: "ccb-root" });
  root.innerHTML = `
    <button class="ccb-fab" title="ComfyUI Assistant (drag to move)">&#128172;</button>
    <div class="ccb-panel">
      <div class="ccb-header">
        <span class="ccb-title">ComfyUI Assistant</span>
        <div class="ccb-tabs">
          <button class="ccb-tab ccb-active" data-tab="chat">Chat</button>
          <button class="ccb-tab" data-tab="settings">Settings</button>
        </div>
        <button class="ccb-icon-btn" data-action="new" title="New chat">&#43;</button>
        <button class="ccb-icon-btn" data-action="close" title="Hide">&#10005;</button>
      </div>
      <div class="ccb-progress ccb-top" id="ccb-kb-progress"><div class="ccb-progress-bar" id="ccb-kb-bar"></div></div>
      <div class="ccb-body">
        <div class="ccb-view ccb-chat-view ccb-active">
          <div class="ccb-messages"></div>
          <div class="ccb-attachments" id="ccb-attachments"></div>
          <div class="ccb-composer">
            <button class="ccb-attach-btn" id="ccb-attach" title="Attach image">&#128206;</button>
            <textarea class="ccb-input" rows="1" placeholder="Ask about your workflow..."></textarea>
            <button class="ccb-new-btn" id="ccb-new" title="Start a new chat for this workflow">New</button>
            <button class="ccb-send">Send</button>
          </div>
          <div class="ccb-attach-menu" id="ccb-attach-menu">
            <button data-attach="inputs">Input image(s)</button>
            <button data-attach="output">Last output</button>
            <button data-attach="file">Choose file&#8230;</button>
          </div>
          <input type="file" id="ccb-file" accept="image/*" multiple hidden />
        </div>
        <div class="ccb-view ccb-settings-view">
          <div class="ccb-settings">
            <div class="ccb-field">
              <label>Provider</label>
              <select id="ccb-provider">
                <option value="lmstudio">LM Studio</option>
                <option value="ollama">Ollama</option>
                <option value="openai">OpenAI-compatible</option>
                <option value="anthropic">Anthropic</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Base URL</label>
              <input id="ccb-base-url" type="text" placeholder="http://127.0.0.1:1234/v1" />
            </div>
            <div class="ccb-field">
              <label>API key</label>
              <input id="ccb-api-key" type="password" placeholder="not set" />
            </div>
            <div class="ccb-field">
              <label>Model</label>
              <div class="ccb-row">
                <div class="ccb-field">
                  <select id="ccb-model"></select>
                </div>
                <button class="ccb-btn" id="ccb-refresh-models">Refresh</button>
              </div>
            </div>
            <div class="ccb-row">
              <div class="ccb-field">
                <label>Temperature</label>
                <input id="ccb-temperature" type="number" step="0.1" min="0" max="2" />
              </div>
              <div class="ccb-field">
                <label>Max tokens</label>
                <input id="ccb-max-tokens" type="number" step="128" min="128" />
              </div>
            </div>
            <div class="ccb-field">
              <label>Native tool calling</label>
              <select id="ccb-native-tools">
                <option value="true">Enabled</option>
                <option value="false">Disabled (use inline JSON actions)</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Vision</label>
              <div class="ccb-status" id="ccb-vision">Vision support: unknown</div>
            </div>
            <div class="ccb-field">
              <label>Selection awareness (tell the model what you selected)</label>
              <select id="ccb-selection-enabled">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Highlight nodes the model refers to</label>
              <select id="ccb-highlight-enabled">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Auto-arrange added nodes after building</label>
              <select id="ccb-layout-auto">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Check connections after building</label>
              <select id="ccb-validate-auto">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Context window (tokens, 0 = auto-detect)</label>
              <input id="ccb-context-window" type="number" min="0" step="1024" />
            </div>
            <div class="ccb-field">
              <label>Auto-compact when over budget</label>
              <select id="ccb-context-auto">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Keep last messages verbatim</label>
              <input id="ccb-context-keep" type="number" min="4" max="60" />
            </div>
            <div class="ccb-kb-status" id="ccb-context-status">Context: -</div>
            <div class="ccb-field">
              <label>Use saved lessons</label>
              <select id="ccb-memory-enabled">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Auto-detect corrections</label>
              <select id="ccb-memory-autodetect">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Lessons injected per message</label>
              <input id="ccb-memory-limit" type="number" min="1" max="30" />
            </div>
            <div class="ccb-field">
              <label>Memory</label>
              <div class="ccb-row">
                <input id="ccb-memory-new" type="text" placeholder="Add a lesson..." />
                <button class="ccb-btn" id="ccb-memory-add">Add</button>
              </div>
            </div>
            <div class="ccb-row">
              <button class="ccb-btn" id="ccb-memory-refresh">Refresh lessons</button>
              <button class="ccb-btn ccb-danger" id="ccb-memory-clear">Clear all</button>
            </div>
            <div class="ccb-memory-list" id="ccb-memory-list"></div>
            <div class="ccb-field">
              <label>Always include last output image</label>
              <select id="ccb-images-output">
                <option value="false">No</option>
                <option value="true">Yes</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Always include input images</label>
              <select id="ccb-images-inputs">
                <option value="false">No</option>
                <option value="true">Yes</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Auto-attach images when I mention them</label>
              <select id="ccb-images-mention">
                <option value="true">Yes</option>
                <option value="false">No</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Max image dimension (px)</label>
              <input id="ccb-images-max" type="number" min="256" max="4096" step="64" />
            </div>
            <div class="ccb-field">
              <label>System prompt</label>
              <textarea id="ccb-system-prompt" rows="5"></textarea>
            </div>
            <div class="ccb-field">
              <label>Web search provider</label>
              <select id="ccb-websearch-provider">
                <option value="tavily">Tavily</option>
                <option value="brave">Brave</option>
                <option value="serpapi">SerpAPI</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Web search API key</label>
              <input id="ccb-websearch-key" type="password" placeholder="not set" />
            </div>
            <div class="ccb-field">
              <label>Max search results</label>
              <input id="ccb-websearch-count" type="number" min="1" max="20" />
            </div>
            <div class="ccb-field">
              <label>Knowledge base</label>
              <select id="ccb-kb-enabled">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Official docs auto-update</label>
              <select id="ccb-kb-auto-official">
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </div>
            <div class="ccb-field">
              <label>Official docs refresh (days)</label>
              <input id="ccb-kb-refresh-days" type="number" min="1" max="365" />
            </div>
            <div class="ccb-row">
              <button class="ccb-btn" id="ccb-kb-rebuild">Rebuild index</button>
              <button class="ccb-btn" id="ccb-kb-sync">Sync official docs</button>
            </div>
            <div class="ccb-kb-status" id="ccb-kb-status">Knowledge base: loading...</div>
            <div class="ccb-field">
              <label>Debug</label>
              <label class="ccb-checkbox">
                <input type="checkbox" id="ccb-debug-enabled" />
                Enable debug logging (no personal data is recorded)
              </label>
            </div>
            <div class="ccb-row">
              <button class="ccb-btn" id="ccb-debug-copy">Copy report</button>
              <button class="ccb-btn" id="ccb-debug-download">Download report</button>
              <button class="ccb-btn" id="ccb-debug-clear">Clear log</button>
            </div>
            <div class="ccb-kb-status" id="ccb-debug-status">Debug: off</div>
            <div class="ccb-row">
              <button class="ccb-btn ccb-primary" id="ccb-save">Save</button>
              <button class="ccb-btn" id="ccb-test">Test connection</button>
            </div>
            <div class="ccb-status" id="ccb-status"></div>
          </div>
        </div>
      </div>
    </div>
    <div class="ccb-modal"><div class="ccb-dialog"></div></div>
  `;
  document.body.appendChild(root);

  state.dom = {
    root,
    fab: root.querySelector(".ccb-fab"),
    panel: root.querySelector(".ccb-panel"),
    header: root.querySelector(".ccb-header"),
    messages: root.querySelector(".ccb-messages"),
    input: root.querySelector(".ccb-input"),
    send: root.querySelector(".ccb-send"),
    attachments: root.querySelector("#ccb-attachments"),
    attachMenu: root.querySelector("#ccb-attach-menu"),
    modal: root.querySelector(".ccb-modal"),
    dialog: root.querySelector(".ccb-dialog"),
    status: root.querySelector("#ccb-status"),
  };
  wireUi();
}

function wireUi() {
  const { fab, panel, header, input, send, root } = state.dom;

  fab.addEventListener("click", (event) => {
    if (fab.dataset.dragged === "1") {
      event.preventDefault();
      return;
    }
    togglePanel(!panel.classList.contains("ccb-open"));
  });

  root.querySelector('[data-action="close"]').addEventListener("click", () => togglePanel(false));
  root.querySelector('[data-action="new"]').addEventListener("click", () => startNewChat());

  for (const tab of root.querySelectorAll(".ccb-tab")) {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  }

  send.addEventListener("click", () => {
    if (state.busy) cancelAgent();
    else submitInput();
  });
  root.querySelector("#ccb-new").addEventListener("click", () => startNewChat());
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!state.busy) submitInput();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  });

  root.querySelector("#ccb-provider").addEventListener("change", () => {
    applyProviderDefaultUrl();
    refreshModels();
  });
  root.querySelector("#ccb-base-url").addEventListener("change", () => refreshModels());
  root.querySelector("#ccb-refresh-models").addEventListener("click", () => refreshModels());
  root.querySelector("#ccb-save").addEventListener("click", () => saveSettings());
  root.querySelector("#ccb-test").addEventListener("click", () => refreshModels(true));
  root.querySelector("#ccb-kb-rebuild").addEventListener("click", () => rebuildKb(false));
  root.querySelector("#ccb-kb-sync").addEventListener("click", () => rebuildKb(true));
  root.querySelector("#ccb-memory-add").addEventListener("click", async () => {
    const input = root.querySelector("#ccb-memory-new");
    const text = input.value.trim();
    if (!text) return;
    try {
      await rememberLesson(text);
      input.value = "";
    } catch (error) {
      appendNotice(error.message);
    }
  });
  root.querySelector("#ccb-memory-refresh").addEventListener("click", () => loadLessons());
  root.querySelector("#ccb-memory-clear").addEventListener("click", () =>
    updateMemory({ action: "clear" }).catch((error) => appendNotice(error.message)),
  );
  root.querySelector("#ccb-debug-enabled").addEventListener("change", () => {
    updateDebugStatus();
    saveSettings();
  });
  root.querySelector("#ccb-debug-copy").addEventListener("click", () => copyDebugReport());
  root.querySelector("#ccb-debug-download").addEventListener("click", () => downloadDebugReport());
  root.querySelector("#ccb-debug-clear").addEventListener("click", () => clearDebugLog());
  root.querySelector("#ccb-model").addEventListener("change", () => {
    detectVision();
    detectContextWindow();
  });

  const attachMenu = state.dom.attachMenu;
  root.querySelector("#ccb-attach").addEventListener("click", (event) => {
    event.stopPropagation();
    attachMenu.classList.toggle("ccb-open");
  });
  document.addEventListener("click", () => attachMenu.classList.remove("ccb-open"));
  attachMenu.addEventListener("click", (event) => event.stopPropagation());
  root.querySelector('#ccb-attach-menu [data-attach="inputs"]').addEventListener("click", attachInputImages);
  root.querySelector('#ccb-attach-menu [data-attach="output"]').addEventListener("click", attachLastOutput);
  root.querySelector('#ccb-attach-menu [data-attach="file"]').addEventListener("click", () => {
    attachMenu.classList.remove("ccb-open");
    root.querySelector("#ccb-file").click();
  });
  root.querySelector("#ccb-file").addEventListener("change", async (event) => {
    for (const file of Array.from(event.target.files || [])) {
      const objectUrl = URL.createObjectURL(file);
      await addAttachment({ name: file.name, url: objectUrl, source: "file" });
      URL.revokeObjectURL(objectUrl);
    }
    event.target.value = "";
  });

  makeDraggable(fab, fab);
  makeDraggable(panel, header);
  restorePosition(fab, "ccb-fab-pos", { right: "24px", bottom: "96px" });
  restorePosition(panel, "ccb-panel-pos", null);
}

function selectTab(name) {
  state.dom.root.querySelectorAll(".ccb-tab").forEach((tab) => {
    tab.classList.toggle("ccb-active", tab.dataset.tab === name);
  });
  state.dom.root.querySelector(".ccb-chat-view").classList.toggle("ccb-active", name === "chat");
  state.dom.root.querySelector(".ccb-settings-view").classList.toggle("ccb-active", name === "settings");
  if (name === "settings") refreshModels();
}

function togglePanel(open) {
  const { panel, fab } = state.dom;
  panel.classList.toggle("ccb-open", open);
  fab.classList.toggle("ccb-hidden", open);
  if (open) {
    if (!localStorage.getItem("ccb-panel-pos")) {
      placePanelNearFab();
    }
    if (!state.messages.length && !state.dom.messages.dataset.welcomed) {
      state.dom.messages.dataset.welcomed = "1";
      appendNotice("Hi! Ask me to inspect your workflow, add or remove nodes, or find custom nodes.");
    }
    scrollMessages();
    state.dom.input.focus();
    refreshKbStatus();
  } else {
    clearTimeout(state.kbTimer);
  }
}

function placePanelNearFab() {
  const { panel, fab } = state.dom;
  const gap = 12;
  let left = fab.offsetLeft - panel.offsetWidth - gap;
  if (left < gap) left = fab.offsetLeft;
  let top = fab.offsetTop + fab.offsetHeight - panel.offsetHeight;
  top = Math.max(gap, Math.min(top, window.innerHeight - panel.offsetHeight - gap));
  left = Math.max(gap, Math.min(left, window.innerWidth - panel.offsetWidth - gap));
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
  panel.style.right = "auto";
  panel.style.bottom = "auto";
}

function makeDraggable(element, handle) {
  let moved = false;
  handle.addEventListener("pointerdown", (event) => {
    const clickedButton = event.target.closest("button");
    if (event.button !== 0 || (handle !== element && clickedButton)) return;
    moved = false;
    const startX = event.clientX;
    const startY = event.clientY;
    const rect = element.getBoundingClientRect();
    const originLeft = rect.left;
    const originTop = rect.top;
    let pendingX = 0;
    let pendingY = 0;
    let frame = 0;
    handle.setPointerCapture(event.pointerId);

    const applyTransform = () => {
      frame = 0;
      element.style.transform = `translate3d(${pendingX}px, ${pendingY}px, 0)`;
    };
    const onMove = (moveEvent) => {
      pendingX = moveEvent.clientX - startX;
      pendingY = moveEvent.clientY - startY;
      if (!moved && (Math.abs(pendingX) > 4 || Math.abs(pendingY) > 4)) moved = true;
      if (!frame) frame = requestAnimationFrame(applyTransform);
    };
    const finish = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      if (frame) {
        cancelAnimationFrame(frame);
        frame = 0;
      }
      if (moved) {
        element.style.left = `${originLeft + pendingX}px`;
        element.style.top = `${originTop + pendingY}px`;
        element.style.right = "auto";
        element.style.bottom = "auto";
        persistPosition(element);
      }
      element.style.transform = "";
      if (handle === element) {
        element.dataset.dragged = moved ? "1" : "0";
        setTimeout(() => {
          element.dataset.dragged = "0";
        }, 0);
      }
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
  });
}

function persistPosition(element) {
  const key = element.classList.contains("ccb-fab") ? "ccb-fab-pos" : "ccb-panel-pos";
  localStorage.setItem(key, JSON.stringify({ left: element.offsetLeft, top: element.offsetTop }));
}

function restorePosition(element, key, fallback) {
  const raw = localStorage.getItem(key);
  if (raw) {
    try {
      const pos = JSON.parse(raw);
      element.style.left = `${pos.left}px`;
      element.style.top = `${pos.top}px`;
      element.style.right = "auto";
      element.style.bottom = "auto";
      return;
    } catch {
      localStorage.removeItem(key);
    }
  }
  if (fallback) {
    for (const [prop, value] of Object.entries(fallback)) {
      element.style[prop] = value;
    }
  }
}

async function getNodeDefs() {
  if (!state.nodeDefsPromise) {
    state.nodeDefsPromise = api.getNodeDefs().catch((error) => {
      state.nodeDefsPromise = null;
      throw error;
    });
  }
  return state.nodeDefsPromise;
}

function nodeSummary(node) {
  return { id: node.id, type: node.type, title: node.title || node.type, mode: node.mode };
}

function workflowSummary() {
  const graph = app.graph;
  const nodes = (graph._nodes || []).map(nodeSummary);
  const links = Object.values(graph.links || {}).map((link) => ({
    id: link.id,
    origin_id: link.origin_id,
    origin_slot: link.origin_slot,
    target_id: link.target_id,
    target_slot: link.target_slot,
    type: link.type,
  }));
  const groups = (graph._groups || []).map((group) => ({
    title: group.title,
    node_ids: (group.nodes || []).map((node) => (node && typeof node === "object" ? node.id : node)),
  }));
  return { node_count: nodes.length, link_count: links.length, nodes, links, groups };
}

function nodeDetails(id) {
  const node = app.graph.getNodeById(Number(id));
  if (!node) return { error: `No node with id ${id}` };
  return {
    id: node.id,
    type: node.type,
    title: node.title,
    mode: node.mode,
    pos: [node.pos[0], node.pos[1]],
    size: [node.size[0], node.size[1]],
    widgets: (node.widgets || []).map((widget) => ({ name: widget.name, value: widget.value })),
    inputs: (node.inputs || []).map((input, index) => ({
      slot: index,
      name: input.name,
      type: input.type,
      connected: input.link != null,
    })),
    outputs: (node.outputs || []).map((output, index) => ({
      slot: index,
      name: output.name,
      type: output.type,
      links: output.links || [],
    })),
  };
}

async function searchInstalledNodes(query, limit = 25) {
  const defs = await getNodeDefs();
  const needle = String(query || "").toLowerCase();
  const results = [];
  for (const [type, def] of Object.entries(defs)) {
    const haystack = `${type} ${def.display_name || ""} ${def.category || ""} ${def.description || ""}`.toLowerCase();
    if (!needle || haystack.includes(needle)) {
      results.push({
        type,
        display_name: def.display_name || type,
        category: def.category || "",
        description: String(def.description || "").slice(0, 160),
      });
      if (results.length >= limit) break;
    }
  }
  return { count: results.length, results };
}

function withGraphChange(mutate) {
  const graph = app.graph;
  const tracker = app.workflowManager?.activeWorkflow?.changeTracker;
  if (tracker?.beforeChange) tracker.beforeChange();
  else graph.beforeChange?.();
  try {
    return mutate();
  } finally {
    if (tracker?.afterChange) tracker.afterChange();
    else graph.afterChange?.();
    graph.setDirtyCanvas(true, true);
  }
}

function nodeRectAt(pos, size) {
  return { x: pos[0], y: pos[1], w: size?.[0] || 200, h: size?.[1] || 100 };
}

function rectsOverlap(a, b, pad = 40) {
  return a.x < b.x + b.w + pad && a.x + a.w + pad > b.x && a.y < b.y + b.h + pad && a.y + a.h + pad > b.y;
}

function nodePositionBlocked(pos, size) {
  const rect = nodeRectAt(pos, size);
  return (app.graph._nodes || []).some((node) => rectsOverlap(rect, nodeRectAt(node.pos, node.size)));
}

function findFreePosition(size) {
  const nodes = app.graph._nodes || [];
  let maxRight = 0;
  let minTop = 120;
  for (const node of nodes) {
    const rect = nodeRectAt(node.pos, node.size);
    maxRight = Math.max(maxRight, rect.x + rect.w);
    minTop = Math.min(minTop, rect.y);
  }
  const width = size?.[0] || 200;
  const height = size?.[1] || 100;
  const startX = nodes.length ? maxRight + 120 : 120;
  const startY = nodes.length ? minTop : 120;
  for (let col = 0; col < 30; col += 1) {
    for (let row = 0; row < 30; row += 1) {
      const pos = [startX + col * (width + 80), startY + row * (height + 60)];
      if (!nodePositionBlocked(pos, size)) return pos;
    }
  }
  return [startX, startY];
}

function addNode(type, x, y, title, near) {
  const node = globalThis.LiteGraph.createNode(type);
  if (!node) {
    return { error: `Unknown node type "${type}". Call search_installed_nodes first.` };
  }
  const nearNode = near != null ? app.graph.getNodeById(Number(near)) : null;
  let pos = null;
  if (Number.isFinite(x) && Number.isFinite(y)) pos = [x, y];
  else if (nearNode) pos = [nearNode.pos[0] + (nearNode.size?.[0] || 200) + 80, nearNode.pos[1]];
  else pos = findFreePosition(node.size);
  if (!pos || nodePositionBlocked(pos, node.size)) pos = findFreePosition(node.size);
  node.pos = pos;
  withGraphChange(() => app.graph.add(node));
  if (title) node.title = title;
  state.runMutatedGraph = true;
  state.runAddedNodeIds.push(node.id);
  return { ok: true, id: node.id, type: node.type, title: node.title, pos: [node.pos[0], node.pos[1]] };
}

async function removeNode(id) {
  const node = app.graph.getNodeById(Number(id));
  if (!node) return { error: `No node with id ${id}` };
  const confirmed = await confirmDialog(`Remove "${node.title || node.type}" (id ${node.id}) from the workflow?`);
  if (!confirmed) return { cancelled: true, message: "User declined to remove the node." };
  const info = nodeSummary(node);
  withGraphChange(() => app.graph.remove(node));
  state.runMutatedGraph = true;
  return { ok: true, removed: info };
}

function connectNodes(sourceId, sourceSlot, targetId, targetSlot) {
  const source = app.graph.getNodeById(Number(sourceId));
  const target = app.graph.getNodeById(Number(targetId));
  if (!source || !target) return { error: "Source or target node not found." };
  if (!source.outputs?.[sourceSlot]) return { error: `Source ${sourceId} has no output slot ${sourceSlot}.` };
  if (!target.inputs?.[targetSlot]) return { error: `Target ${targetId} has no input slot ${targetSlot}.` };
  withGraphChange(() => source.connect(Number(sourceSlot), target, Number(targetSlot)));
  state.runMutatedGraph = true;
  return { ok: true };
}

function disconnectLink(linkId) {
  const link = (app.graph.links || {})[linkId];
  if (!link) return { error: `No link with id ${linkId}` };
  withGraphChange(() => app.graph.removeLink(linkId));
  state.runMutatedGraph = true;
  return { ok: true };
}

function setWidgetValue(id, name, value) {
  const node = app.graph.getNodeById(Number(id));
  if (!node) return { error: `No node with id ${id}` };
  const widget = (node.widgets || []).find((item) => item.name === name);
  if (!widget) {
    return { error: `Node ${id} has no widget "${name}".`, widgets: (node.widgets || []).map((item) => item.name) };
  }
  withGraphChange(() => {
    widget.value = value;
    if (typeof widget.callback === "function") {
      widget.callback(value, app.canvas, node, [0, 0], null);
    }
  });
  return { ok: true, name, value };
}

function moveNode(id, x, y) {
  const node = app.graph.getNodeById(Number(id));
  if (!node) return { error: `No node with id ${id}` };
  withGraphChange(() => {
    node.pos = [Number(x), Number(y)];
  });
  return { ok: true, id, pos: [node.pos[0], node.pos[1]] };
}

const TEXT_WIDGET_NAMES = ["text", "prompt", "positive", "negative", "string", "value"];

function textWidget(node) {
  return (node.widgets || []).find(
    (widget) => TEXT_WIDGET_NAMES.includes(widget.name) && typeof widget.value === "string",
  );
}

function findUpstreamTextEncoder(graph, sampler, inputName) {
  const input = (sampler.inputs || []).find((item) => item.name === inputName);
  if (!input || input.link == null) return null;
  const link = (graph.links || {})[input.link];
  if (!link) return null;
  const seen = new Set();
  const queue = [link.origin_id];
  while (queue.length) {
    const id = queue.shift();
    if (id == null || seen.has(id)) continue;
    seen.add(id);
    const node = graph.getNodeById(id);
    if (!node) continue;
    if (textWidget(node)) return node;
    for (const nodeInput of node.inputs || []) {
      if (nodeInput.link != null) {
        const upstream = (graph.links || {})[nodeInput.link];
        if (upstream) queue.push(upstream.origin_id);
      }
    }
  }
  return null;
}

function listPromptNodes() {
  const graph = app.graph;
  const results = [];
  const used = new Set();
  const samplers = (graph._nodes || []).filter(
    (node) => (node.inputs || []).some((i) => i.name === "positive") && (node.inputs || []).some((i) => i.name === "negative"),
  );
  for (const sampler of samplers) {
    for (const role of ["positive", "negative"]) {
      const encoder = findUpstreamTextEncoder(graph, sampler, role);
      if (!encoder || used.has(`${role}:${encoder.id}`)) continue;
      used.add(`${role}:${encoder.id}`);
      const widget = textWidget(encoder);
      results.push({
        id: encoder.id,
        title: encoder.title || encoder.type,
        role,
        sampler: sampler.title || sampler.type,
        current_text: widget ? String(widget.value).slice(0, 400) : "",
      });
    }
  }
  if (!results.length) {
    for (const node of (graph._nodes || []).filter(textWidget).slice(0, 8)) {
      const widget = textWidget(node);
      results.push({
        id: node.id,
        title: node.title || node.type,
        role: "unknown",
        sampler: "",
        current_text: widget ? String(widget.value).slice(0, 400) : "",
      });
    }
  }
  return { count: results.length, nodes: results };
}

function setPrompt(text, target) {
  if (!text) return { error: "Missing prompt text." };
  const graph = app.graph;
  let node = null;
  const targetText = String(target || "");
  if (targetText.startsWith("node:")) {
    node = graph.getNodeById(Number(targetText.slice(5)));
  } else if (targetText === "positive" || targetText === "negative") {
    for (const sampler of (graph._nodes || []).filter((n) => (n.inputs || []).some((i) => i.name === "positive"))) {
      const encoder = findUpstreamTextEncoder(graph, sampler, targetText);
      if (encoder) {
        node = encoder;
        break;
      }
    }
  } else {
    const selected = getSelectedNodes().filter(textWidget);
    if (selected.length === 1) node = selected[0];
  }
  if (!node) {
    const encoders = (graph._nodes || []).filter(textWidget);
    if (encoders.length === 1) node = encoders[0];
  }
  if (!node) return { error: `Could not find a ${targetText || "prompt"} input. Call list_prompt_nodes.` };
  const widget = textWidget(node);
  if (!widget) return { error: `Node ${node.id} has no text widget.` };
  withGraphChange(() => {
    widget.value = text;
    if (typeof widget.callback === "function") {
      widget.callback(text, app.canvas, node, [0, 0], null);
    }
  });
  return { ok: true, node_id: node.id, title: node.title || node.type, target: targetText || "auto" };
}

function getSelectedNodes() {
  const canvas = app.canvas;
  const nodes = [];
  const seen = new Set();
  const addNode = (node) => {
    if (!node || node.id == null || !node.type || seen.has(node.id)) return;
    seen.add(node.id);
    nodes.push(node);
  };
  for (const node of Object.values(canvas?.selected_nodes || {})) {
    addNode(node);
  }
  const items = canvas?.selectedItems;
  if (items && typeof items[Symbol.iterator] === "function") {
    for (const item of items) {
      if (item && Array.isArray(item.nodes)) {
        for (const member of item.nodes) addNode(member);
      } else {
        addNode(item?.node ?? item);
      }
    }
  }
  return nodes;
}

function getSelection() {
  const nodes = getSelectedNodes();
  return {
    count: nodes.length,
    nodes: nodes.map((node) => ({
      id: node.id,
      type: node.type,
      title: node.title || node.type,
      mode: node.mode,
      pos: [node.pos[0], node.pos[1]],
    })),
  };
}

function highlightNodes(ids, center = true, add = false) {
  const graph = app.graph;
  const requested = Array.isArray(ids) ? ids : ids != null ? [ids] : [];
  const nodes = [];
  const unknown = [];
  for (const id of requested) {
    const node = graph.getNodeById(Number(id));
    if (node) nodes.push(node);
    else unknown.push(id);
  }
  if (!nodes.length) return { error: "No valid node ids to highlight.", unknown };
  const canvas = app.canvas;
  if (!add) canvas.deselectAllNodes?.();
  for (const node of nodes) {
    canvas.selectNode(node, true);
  }
  if (center && nodes.length === 1) {
    canvas.centerOnNode?.(nodes[0]);
  }
  canvas.setDirty(true, true);
  state.botHighlightedIds = nodes.map((node) => node.id);
  return {
    ok: true,
    highlighted: nodes.map((node) => ({ id: node.id, type: node.type, title: node.title || node.type })),
    unknown,
  };
}

function centerOnNodes(nodes) {
  const canvas = app.canvas;
  if (!canvas || !nodes.length) return;
  if (nodes.length === 1) {
    canvas.centerOnNode?.(nodes[0]);
    return;
  }
  try {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of nodes) {
      minX = Math.min(minX, node.pos[0]);
      minY = Math.min(minY, node.pos[1]);
      maxX = Math.max(maxX, node.pos[0] + (node.size?.[0] || 200));
      maxY = Math.max(maxY, node.pos[1] + (node.size?.[1] || 100));
    }
    const ds = canvas.ds;
    if (ds?.offset && canvas.canvas) {
      ds.offset[0] = canvas.canvas.width / (2 * ds.scale) - (minX + maxX) / 2;
      ds.offset[1] = canvas.canvas.height / (2 * ds.scale) - (minY + maxY) / 2;
      canvas.setDirty(true, true);
    }
  } catch {
    /* centering is best-effort */
  }
}

function layoutWorkflow(scope = "added") {
  const graph = app.graph;
  if (!graph || !graph.arrange) return { error: "Graph layout is unavailable." };
  const addedIds = new Set(state.runAddedNodeIds.map(String));
  const useAdded = scope !== "all" && addedIds.size > 0 && addedIds.size < (graph._nodes || []).length;
  const saved = new Map();
  if (useAdded) {
    for (const node of graph._nodes || []) {
      if (!addedIds.has(String(node.id))) saved.set(String(node.id), [node.pos[0], node.pos[1]]);
    }
  }
  withGraphChange(() => {
    graph.arrange(100);
    if (!saved.size) return;
    for (const node of graph._nodes || []) {
      const pos = saved.get(String(node.id));
      if (pos) node.pos = [pos[0], pos[1]];
    }
    const added = (graph._nodes || []).filter((node) => addedIds.has(String(node.id)));
    const existing = (graph._nodes || []).filter((node) => !addedIds.has(String(node.id)));
    if (added.length && existing.length) {
      let right = -Infinity;
      for (const node of existing) right = Math.max(right, node.pos[0] + (node.size?.[0] || 200));
      let left = Infinity;
      for (const node of added) left = Math.min(left, node.pos[0]);
      const shift = right + 120 - left;
      if (shift > 0) {
        for (const node of added) node.pos[0] += shift;
      }
    }
  });
  state.runArranged = true;
  centerOnNodes((graph._nodes || []).filter((node) => (useAdded ? addedIds.has(String(node.id)) : true)));
  debugLog("graph", "layout", { scope: useAdded ? "added" : "all", node_count: (graph._nodes || []).length });
  return { ok: true, scope: useAdded ? "added" : "all", node_count: (graph._nodes || []).length };
}

async function validateWorkflow() {
  const defs = await getNodeDefs();
  const graph = app.graph;
  const unconnected = [];
  const dangling = [];
  const orphans = [];
  const outputs = [];
  for (const node of graph._nodes || []) {
    (node.outputs || []).forEach((output, slot) => {
      outputs.push({
        id: node.id,
        type: node.type,
        title: node.title || node.type,
        name: output.name,
        slot,
        outputType: output.type,
      });
    });
  }
  for (const node of graph._nodes || []) {
    const def = defs[node.type];
    const required = def?.input?.required || {};
    const widgetNames = new Set((node.widgets || []).map((widget) => widget.name));
    const inputByName = new Map((node.inputs || []).map((input) => [input.name, input]));
    for (const [name, spec] of Object.entries(required)) {
      const slot = inputByName.get(name);
      const linked = slot && slot.link != null && (graph.links || {})[slot.link];
      if (linked || widgetNames.has(name)) continue;
      const typeSpec = Array.isArray(spec) ? spec[0] : spec;
      const wanted = Array.isArray(typeSpec) ? typeSpec : [typeSpec];
      const candidates = outputs
        .filter((output) => output.id !== node.id && (wanted.includes(output.outputType) || output.outputType === "*" || wanted.includes("*")))
        .slice(0, 6)
        .map((output) => ({
          id: output.id,
          type: output.type,
          title: output.title,
          output: output.name,
          output_slot: output.slot,
          output_type: output.outputType,
        }));
      unconnected.push({
        id: node.id,
        type: node.type,
        title: node.title || node.type,
        input: name,
        input_type: wanted.join("|"),
        candidates,
      });
    }
    for (const input of node.inputs || []) {
      if (input.link != null && !(graph.links || {})[input.link]) {
        dangling.push({ id: node.id, type: node.type, title: node.title || node.type, input: input.name, link: input.link });
      }
    }
    const noIn = (node.inputs || []).every((input) => input.link == null);
    const noOut = (node.outputs || []).every((output) => !(output.links || []).length);
    if (noIn && noOut && ((node.inputs || []).length || (node.outputs || []).length)) {
      orphans.push({ id: node.id, type: node.type, title: node.title || node.type });
    }
  }
  const report = {
    ok: unconnected.length === 0 && dangling.length === 0,
    unconnected_required: unconnected,
    dangling_links: dangling,
    orphans,
    counts: { unconnected: unconnected.length, dangling: dangling.length, orphans: orphans.length },
  };
  debugLog("graph", "validate", { ok: report.ok, ...report.counts });
  return report;
}function validationMessage(report) {
  const parts = [];
  if (report.unconnected_required.length) {
    parts.push("Unconnected required inputs:");
    for (const item of report.unconnected_required) {
      const candidates = item.candidates
        .map((candidate) => `${candidate.type}#${candidate.id}.${candidate.output}`)
        .join(", ");
      parts.push(`- node ${item.id} (${item.type}) input "${item.input}" (${item.input_type})${candidates ? `; candidates: ${candidates}` : ""}`);
    }
  }
  if (report.dangling_links.length) {
    parts.push("Dangling links (link target missing):");
    for (const item of report.dangling_links) {
      parts.push(`- node ${item.id} (${item.type}) input "${item.input}" link ${item.link}`);
    }
  }
  if (report.orphans.length) {
    parts.push(`Orphan nodes (no connections): ${report.orphans.map((item) => `${item.type}#${item.id}`).join(", ")}`);
  }
  parts.push("Fix the unconnected inputs with connect_nodes, then call validate_workflow again.");
  return parts.join("\n");
}

function imageUrl({ filename, subfolder, type }) {
  const params = new URLSearchParams({ filename, type: type || "output" });
  if (subfolder) params.set("subfolder", subfolder);
  return api.apiURL(`/view?${params.toString()}`);
}

function collectInputImages() {
  const results = [];
  for (const node of app.graph._nodes || []) {
    const widget = (node.widgets || []).find((w) => w.name === "image" || w.name === "image_path");
    if (!widget || typeof widget.value !== "string" || !widget.value) continue;
    let url = node.imgs?.[0]?.src;
    if (!url) {
      const match = widget.value.match(/^(.*?)\s*\[(input|output|temp)\]$/);
      const path = match ? match[1] : widget.value;
      const type = match ? match[2] : "input";
      const slash = path.lastIndexOf("/");
      const subfolder = slash >= 0 ? path.slice(0, slash) : "";
      const filename = slash >= 0 ? path.slice(slash + 1) : path;
      url = imageUrl({ filename, subfolder, type });
    }
    results.push({ name: `${node.title || node.type}: ${widget.value}`, url });
  }
  return results;
}

async function lastOutputImage() {
  if (state.lastOutputs.length) return state.lastOutputs[state.lastOutputs.length - 1];
  try {
    const response = await api.fetchApi("/history");
    const history = await response.json();
    const entries = Object.values(history || {});
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const images = [];
      for (const nodeOut of Object.values(entries[index]?.outputs || {})) {
        for (const image of nodeOut?.images || []) images.push(image);
      }
      if (images.length) {
        const image = images[images.length - 1];
        return { filename: image.filename, subfolder: image.subfolder || "", type: image.type || "output", url: imageUrl(image) };
      }
    }
  } catch {
    /* ignore */
  }
  return null;
}

function loadImageElement(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.crossOrigin = "anonymous";
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("could not load image"));
    image.src = url;
  });
}

async function urlToDataUrl(url, maxDimension) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const objectUrl = URL.createObjectURL(await response.blob());
  try {
    const image = await loadImageElement(objectUrl);
    const scale = Math.min(1, maxDimension / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.85);
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

function renderAttachments() {
  const container = state.dom.attachments;
  if (!container) return;
  container.innerHTML = "";
  container.classList.toggle("ccb-active", state.attachments.length > 0);
  for (const item of state.attachments) {
    const chip = el("div", { class: "ccb-thumb" });
    chip.appendChild(el("img", { src: item.dataUrl, alt: item.name }));
    const remove = el("button", { class: "ccb-thumb-remove", text: "\u00d7", title: "Remove" });
    remove.addEventListener("click", () => {
      state.attachments = state.attachments.filter((attachment) => attachment.id !== item.id);
      renderAttachments();
    });
    chip.appendChild(remove);
    container.appendChild(chip);
  }
}

async function addAttachment(entry) {
  if (state.visionSupported === false) {
    appendNotice("Not attached: the selected model is not vision-capable.");
    return;
  }
  try {
    const maxDimension = state.config?.images?.max_dimension || 1024;
    const dataUrl = entry.dataUrl || (await urlToDataUrl(entry.url, maxDimension));
    state.attachments.push({
      id: `${Date.now()}_${Math.random().toString(36).slice(2)}`,
      name: entry.name,
      dataUrl,
      source: entry.source,
    });
    renderAttachments();
  } catch (error) {
    appendNotice(`Attach failed: ${error.message}`);
  }
}

async function attachInputImages() {
  state.dom.attachMenu.classList.remove("ccb-open");
  const inputs = collectInputImages();
  if (!inputs.length) {
    appendNotice("No input images found in the workflow.");
    return;
  }
  for (const entry of inputs) {
    await addAttachment({ name: entry.name, url: entry.url, source: "input" });
  }
}

async function attachLastOutput() {
  state.dom.attachMenu.classList.remove("ccb-open");
  const last = await lastOutputImage();
  if (!last) {
    appendNotice("No generated image found yet. Run the workflow first.");
    return;
  }
  await addAttachment({ name: `output: ${last.filename}`, url: last.url, source: "output" });
}

async function gatherAutoImages() {
  const config = state.config?.images || {};
  const entries = [];
  if (config.always_output) {
    const last = await lastOutputImage();
    if (last) entries.push(last);
  }
  if (config.always_inputs) {
    entries.push(...collectInputImages());
  }
  const maxDimension = config.max_dimension || 1024;
  const dataUrls = [];
  for (const entry of entries) {
    try {
      dataUrls.push(await urlToDataUrl(entry.url, maxDimension));
    } catch {
      /* ignore unreachable images */
    }
  }
  return dataUrls;
}

async function gatherMentionedImages(text) {
  const config = state.config?.images || {};
  const maxDimension = config.max_dimension || 1024;
  const entries = [];
  const wantsOutput = OUTPUT_MENTION_RE.test(text);
  const wantsInput = INPUT_MENTION_RE.test(text) || !wantsOutput;
  if (wantsInput) entries.push(...collectInputImages());
  if (wantsOutput || !entries.length) {
    const last = await lastOutputImage();
    if (last) entries.push(last);
  }
  const seen = new Set();
  const dataUrls = [];
  for (const entry of entries) {
    if (seen.has(entry.url)) continue;
    seen.add(entry.url);
    try {
      dataUrls.push(await urlToDataUrl(entry.url, maxDimension));
    } catch {
      /* ignore unreachable images */
    }
    if (dataUrls.length >= 4) break;
  }
  return dataUrls;
}

async function webSearch(query, count) {
  const response = await api.fetchApi("/chatbot/websearch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, count }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return { results: payload.results };
}

async function searchDocs(query, limit, source) {
  const response = await api.fetchApi("/chatbot/docs/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, limit, source }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return { results: payload.results };
}

async function getNodeDocs(type, limit) {
  const response = await api.fetchApi("/chatbot/docs/node", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type, limit }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

async function installCustomNode(repoUrl, name) {
  if (!repoUrl) return { error: "Missing repo_url." };
  const confirmed = await confirmDialog(
    `Clone this repository into custom_nodes and install its requirements?`,
    `Repository: ${repoUrl}\nFolder: ${name || "derived from URL"}\n\nComfyUI must be restarted after install.`,
    "Install",
  );
  if (!confirmed) return { cancelled: true, message: "User declined the installation." };
  const response = await api.fetchApi("/chatbot/install_git", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: repoUrl, name, run_pip: true }),
  });
  const payload = await response.json();
  if (payload.ok) {
    api.fetchApi("/chatbot/kb/rebuild", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }).catch(() => {});
  }
  return payload;
}

async function executeTool(name, args) {
  switch (name) {
    case "get_workflow_summary":
      return workflowSummary();
    case "get_node_details":
      return nodeDetails(args.id);
    case "search_installed_nodes":
      return searchInstalledNodes(args.query, args.limit);
    case "add_node":
      return addNode(args.type, args.x, args.y, args.title, args.near);
    case "remove_node":
      return removeNode(args.id);
    case "connect_nodes":
      return connectNodes(args.source_id, args.source_slot, args.target_id, args.target_slot);
    case "disconnect_link":
      return disconnectLink(args.link_id);
    case "set_widget_value":
      return setWidgetValue(args.id, args.name, args.value);
    case "move_node":
      return moveNode(args.id, args.x, args.y);
    case "list_prompt_nodes":
      return listPromptNodes();
    case "set_prompt":
      return setPrompt(args.text, args.target);
    case "get_selection":
      return getSelection();
    case "highlight_nodes":
      if (state.config?.highlight?.enabled === false) {
        return { error: "Highlighting is disabled in settings." };
      }
      return highlightNodes(args.ids, args.center !== false, args.add === true);
    case "layout_workflow":
      return layoutWorkflow(args.scope);
    case "validate_workflow":
      return validateWorkflow();
    case "remember_lesson":
      return rememberLesson(args.text, args.tags, args.pinned === true);
    case "search_memory":
      return searchMemory(args.query, args.limit);
    case "web_search":
      return webSearch(args.query, args.count);
    case "search_docs":
      return searchDocs(args.query, args.limit, args.source);
    case "get_node_docs":
      return getNodeDocs(args.type, args.limit);
    case "install_custom_node":
      return installCustomNode(args.repo_url, args.name);
    default:
      return { error: `Unknown tool: ${name}` };
  }
}

async function submitInput() {
  const text = state.dom.input.value.trim();
  if (!text || state.busy) return;
  state.dom.input.value = "";
  state.dom.input.style.height = "auto";

  const config = state.config?.images || {};
  const manual = state.attachments.map((attachment) => attachment.dataUrl);
  const images = [...manual];
  if (state.visionSupported !== false) {
    let auto = [];
    if (!manual.length) {
      if (config.always_output || config.always_inputs) {
        auto = await gatherAutoImages();
      } else if (config.auto_on_mention !== false && IMAGE_MENTION_RE.test(text)) {
        auto = await gatherMentionedImages(text);
        if (auto.length) appendNotice(`Attached ${auto.length} workflow image(s) automatically.`);
      }
    }
    for (const url of auto) {
      if (!images.includes(url)) images.push(url);
    }
  } else if (manual.length || config.always_output || config.always_inputs || IMAGE_MENTION_RE.test(text)) {
    appendNotice("Images not sent: the selected model does not support vision.");
  }

  const displayText = images.length ? `${text}\n[${images.length} image(s) attached]` : text;
  appendBubble("user", displayText);
  state.messages.push({ role: "user", content: text, images });
  state.attachments = [];
  renderAttachments();
  scrollMessages();
  state.runAddedNodeIds = [];
  state.runMutatedGraph = false;
  state.runArranged = false;
  state.fixPasses = 0;
  state.correctionHint = state.config?.memory?.auto_detect !== false && CORRECTION_RE.test(text);
  await refreshRelevantLessons(text);
  await prepareContext();
  runAgentWithFixups();
}

function setBusy(busy) {
  state.busy = busy;
  const send = state.dom.send;
  send.classList.toggle("ccb-cancel", busy);
  send.textContent = busy ? "Cancel" : "Send";
}

function cancelAgent() {
  if (!state.busy) return;
  state.cancelled = true;
  if (state.pendingConfirm) {
    state.pendingConfirm(false);
  }
  if (state.abortController) {
    state.abortController.abort();
  }
}

async function runAgent() {
  const runId = (state.runId += 1);
  state.cancelled = false;
  state.runArranged = false;
  setBusy(true);
  try {
    while (true) {
      const { text, toolCalls } = await streamAssistantReply();
      if (state.cancelled || state.runId !== runId) return;
      if (!toolCalls.length) {
        const inline = extractInlineActions(text);
        if (inline.length) {
          state.messages.push({ role: "assistant", content: text });
          for (const action of inline) {
            if (state.cancelled || state.runId !== runId) return;
            await runTool(action.name, action.arguments, null);
          }
          continue;
        }
        if (String(text || "").trim()) {
          state.messages.push({ role: "assistant", content: text });
        }
        break;
      }
      state.messages.push({
        role: "assistant",
        content: text,
        tool_calls: toolCalls.map(toOpenAiToolCall),
      });
      for (const call of toolCalls) {
        if (state.cancelled || state.runId !== runId) return;
        await runTool(call.name, safeParse(call.arguments), call.id);
      }
    }
    if (state.cancelled) {
      appendNotice("Cancelled.");
    }
  } catch (error) {
    if (state.runId !== runId) return;
    if (state.cancelled || error?.name === "AbortError") {
      appendNotice("Cancelled.");
    } else {
      appendBubble("error", String(error?.message || error));
    }
  } finally {
    if (state.runId === runId) {
      state.abortController = null;
      setBusy(false);
      await saveHistory();
      scrollMessages();
    }
  }
}

function autoLayoutIfNeeded() {
  if (state.config?.layout?.auto_after_build === false) return;
  if (!state.runAddedNodeIds.length || state.runArranged) return;
  layoutWorkflow("added");
}

async function runAgentWithFixups() {
  try {
    await runAgent();
    state.correctionHint = false;
    if (state.cancelled) return;
    autoLayoutIfNeeded();
    if (state.config?.validate?.auto_fix === false || !state.runMutatedGraph) return;
    const maxPasses = Math.max(0, Math.min(2, Number(state.config?.validate?.max_passes ?? 1)));
    for (let pass = 0; pass < maxPasses; pass += 1) {
      const report = await validateWorkflow();
      if (report.ok) return;
      state.fixPasses += 1;
      appendNotice(
        `Follow-up pass: ${report.counts.unconnected} unconnected input(s), ${report.counts.dangling} dangling link(s).`,
      );
      state.messages.push({ role: "user", content: validationMessage(report), synthetic: true });
      await saveHistory();
      await runAgent();
      if (state.cancelled) return;
      autoLayoutIfNeeded();
    }
    const finalReport = await validateWorkflow();
    if (!finalReport.ok) {
      const names = finalReport.unconnected_required
        .slice(0, 8)
        .map((item) => `${item.type}#${item.id} .${item.input}`);
      appendBubble(
        "error",
        `Still unconnected after the fix pass: ${names.join(", ") || "dangling links"}${
          finalReport.counts.unconnected > 8 ? "\u2026" : ""
        }`,
      );
    }
  } catch (error) {
    appendBubble("error", String(error?.message || error));
  }
}

async function runTool(name, args, toolCallId) {
  const notice = appendNotice(`\u25b6 ${name} ${truncate(safeStringify(args), 200)}`);
  debugLog("tool", name, { arg_keys: Object.keys(args || {}) });
  let result;
  try {
    result = await executeTool(name, args || {});
  } catch (error) {
    result = { error: String(error?.message || error) };
  }
  notice.textContent = `${result && result.error ? "\u2716" : "\u2714"} ${name}: ${truncate(safeStringify(result), 600)}`;
  const content = safeStringify(result);
  if (toolCallId) {
    state.messages.push({ role: "tool", tool_call_id: toolCallId, content });
  } else {
    state.messages.push({ role: "user", content: `Tool result for ${name}: ${content}` });
  }
  return result;
}

async function streamAssistantReply() {
  state.abortController = new AbortController();
  const bubble = appendBubble("assistant", "");
  bubble.classList.add("ccb-typing");
  let text = "";
  const toolCalls = [];
  const body = {
    messages: buildMessages(),
    tools: TOOLS,
  };
  const response = await api.fetchApi("/chatbot/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: state.abortController.signal,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Chat request failed (${response.status}): ${detail.slice(0, 300)}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      if (event.type === "text") {
        text += event.text;
        bubble.textContent = text;
        scrollMessages();
      } else if (event.type === "tool_call") {
        toolCalls.push(event);
      } else if (event.type === "error") {
        bubble.classList.remove("ccb-typing");
        debugLog("chat", "error", { error: event.error });
        throw new Error(event.error);
      }
    }
  }
  bubble.classList.remove("ccb-typing");
  if (!text && !toolCalls.length) bubble.remove();
  return { text, toolCalls };
}

function selectionContext() {
  if (state.config?.selection?.enabled === false) return "";
  const nodes = getSelectedNodes();
  if (!nodes.length) return "";
  const botIds = state.botHighlightedIds.map(String).sort().join(",");
  const selectedIds = nodes.map((node) => String(node.id)).sort().join(",");
  if (botIds && selectedIds === botIds) return "";
  const parts = nodes.map((node) => `node ${node.id} (${node.type}) "${node.title || node.type}"`);
  return `Current canvas selection (${nodes.length}): ${parts.join("; ")}. If the user refers to "this", "these", or "the selected node(s)", act on these.`;
}

function buildMessages() {
  const context = selectionContext();
  const lessons = lessonsContext();
  const hint = state.correctionHint
    ? "The user's latest message sounds like a correction. If they are correcting a mistake, call remember_lesson with a short general rule before continuing."
    : "";
  const system = `${state.config?.system_prompt || ""}\n\n${RUNTIME_RULES}${context ? `\n\n${context}` : ""}${
    lessons ? `\n\n${lessons}` : ""
  }${hint ? `\n\n${hint}` : ""}`;
  const history = [];
  let started = false;
  for (const message of state.messages) {
    if (!started) {
      if (message.role === "system") {
        history.push(message);
        continue;
      }
      if (message.role !== "user") continue;
      started = true;
    }
    history.push(message);
  }
  let lastUserIndex = -1;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    if (history[index].role === "user") {
      lastUserIndex = index;
      break;
    }
  }
  const wire = history.map((message, index) => {
    if (message.role === "assistant" && !message.tool_calls?.length && !String(message.content || "").trim()) {
      return null;
    }
    if (message.role === "user" && index === lastUserIndex && message.images?.length && state.visionSupported !== false) {
      const content = [
        { type: "text", text: `${message.content || ""}\n[${message.images.length} image(s) attached]` },
      ];
      for (const url of message.images) content.push({ type: "image_url", image_url: { url } });
      return { role: "user", content };
    }
    const copy = { role: message.role, content: message.content };
    if (message.tool_calls) copy.tool_calls = message.tool_calls;
    if (message.tool_call_id) copy.tool_call_id = message.tool_call_id;
    return copy;
  });
  return [{ role: "system", content: system }, ...wire.filter(Boolean)];
}

function toOpenAiToolCall(call) {
  return {
    id: call.id || `call_${Math.random().toString(36).slice(2)}`,
    type: "function",
    function: { name: call.name, arguments: call.arguments || "{}" },
  };
}

function safeParse(text) {
  if (!text) return {};
  if (typeof text === "object") return text;
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

function extractInlineActions(text) {
  const actions = [];
  const regex = /```(?:json)?\s*([\s\S]*?)```/g;
  let match;
  while ((match = regex.exec(text)) !== null) {
    let parsed;
    try {
      parsed = JSON.parse(match[1].trim());
    } catch {
      continue;
    }
    for (const item of Array.isArray(parsed) ? parsed : [parsed]) {
      const name = item.tool || item.action || item.name;
      if (name) actions.push({ name, arguments: item.arguments || item.args || item.params || {} });
    }
  }
  return actions;
}

function appendBubble(role, text) {
  const bubble = el("div", { class: `ccb-msg ccb-${role}` });
  bubble.textContent = text;
  state.dom.messages.appendChild(bubble);
  scrollMessages();
  return bubble;
}

function appendNotice(text) {
  const notice = el("div", { class: "ccb-tool" });
  notice.textContent = text;
  state.dom.messages.appendChild(notice);
  scrollMessages();
  return notice;
}

function scrollMessages() {
  const container = state.dom.messages;
  container.scrollTop = container.scrollHeight;
}

function truncate(text, length) {
  const value = String(text);
  return value.length > length ? `${value.slice(0, length)}\u2026` : value;
}

const DEBUG_LIMIT = 300;

function hashKey(value) {
  const text = String(value || "");
  let hash = 5381;
  for (let index = 0; index < text.length; index += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(index)) >>> 0;
  }
  return hash.toString(16);
}

function debugEnabled() {
  const box = state.dom.root?.querySelector("#ccb-debug-enabled");
  if (box) return box.checked;
  return state.config?.debug?.enabled === true;
}

function debugLog(category, event, data) {
  if (!debugEnabled()) return;
  try {
    state.debugBuffer.push({
      ts: new Date().toISOString(),
      category: String(category),
      event: String(event),
      data: data || {},
    });
    if (state.debugBuffer.length > DEBUG_LIMIT) {
      state.debugBuffer = state.debugBuffer.slice(-DEBUG_LIMIT);
    }
    updateDebugStatus();
  } catch {
    /* ignore */
  }
}

function clientReport() {
  return {
    user_agent: navigator.userAgent,
    session_key_hash: hashKey(state.sessionKey || ""),
    message_count: state.messages.length,
    vision: state.visionSupported,
    context_window: state.contextWindow,
    attachments: state.attachments.length,
    events: state.debugBuffer.slice(-DEBUG_LIMIT),
  };
}

function updateDebugStatus() {
  const element = state.dom.root?.querySelector("#ccb-debug-status");
  if (!element) return;
  element.textContent = `Debug: ${debugEnabled() ? "on" : "off"} \u00b7 ${state.debugBuffer.length} client event(s) buffered`;
}

async function fetchDebugReport() {
  const response = await api.fetchApi("/chatbot/debug/report", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client: clientReport() }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload.text || JSON.stringify(payload, null, 2);
}

async function copyDebugReport() {
  try {
    const text = await fetchDebugReport();
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    setStatus("Debug report copied to clipboard.");
  } catch (error) {
    setStatus(`Copy failed: ${error.message}`);
  }
}

async function downloadDebugReport() {
  try {
    const text = await fetchDebugReport();
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "comfyui-assistent-debug.txt";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus("Debug report downloaded.");
  } catch (error) {
    setStatus(`Download failed: ${error.message}`);
  }
}

async function clearDebugLog() {
  state.debugBuffer = [];
  try {
    await api.fetchApi("/chatbot/debug/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  } catch {
    /* best effort */
  }
  updateDebugStatus();
  setStatus("Debug log cleared.");
}

function safeStringify(value, maxLength = 20000) {
  const seen = new WeakSet();
  let text;
  try {
    text = JSON.stringify(value, (key, item) => {
      if (typeof item === "function") return undefined;
      if (item && typeof item === "object") {
        if (seen.has(item)) return "[circular]";
        seen.add(item);
      }
      return item;
    });
  } catch (error) {
    text = JSON.stringify({ error: `serialize failed: ${String(error?.message || error)}` });
  }
  if (text === undefined) text = "null";
  return truncate(text, maxLength);
}

function confirmDialog(title, detail, confirmLabel = "Confirm") {
  return new Promise((resolve) => {
    const dialog = state.dom.dialog;
    dialog.innerHTML = "";
    dialog.appendChild(el("h3", { text: title }));
    if (detail) dialog.appendChild(el("pre", { text: detail }));
    const actions = el("div", { class: "ccb-card-actions" });
    const cancel = el("button", { class: "ccb-btn", text: "Cancel" });
    const confirm = el("button", { class: `ccb-btn ${confirmLabel === "Install" ? "ccb-primary" : "ccb-danger"}`, text: confirmLabel });
    actions.append(cancel, confirm);
    dialog.appendChild(actions);
    state.dom.modal.classList.add("ccb-open");
    const close = (value) => {
      state.dom.modal.classList.remove("ccb-open");
      state.pendingConfirm = null;
      resolve(value);
    };
    state.pendingConfirm = close;
    cancel.addEventListener("click", () => close(false));
    confirm.addEventListener("click", () => close(true));
  });
}

function startNewChat() {
  state.messages = [];
  state.dom.messages.innerHTML = "";
  state.dom.messages.dataset.welcomed = "1";
  appendNotice("New chat started.");
  saveHistory();
}

async function loadConfig() {
  const response = await api.fetchApi("/chatbot/config");
  const payload = await response.json();
  state.config = payload.config || {};
  populateSettings();
}

function populateSettings() {
  const config = state.config || {};
  const websearch = config.websearch || {};
  const kb = config.kb || {};
  const images = config.images || {};
  const selection = config.selection || {};
  const highlight = config.highlight || {};
  setValue("#ccb-provider", config.provider || "lmstudio");
  setValue("#ccb-base-url", config.base_url || "");
  setPlaceholder("#ccb-api-key", config.api_key_configured ? "\u2022\u2022\u2022 configured" : "not set");
  setModelOptions(state.availableModels, config.model || "");
  setValue("#ccb-temperature", config.temperature ?? 0.7);
  setValue("#ccb-max-tokens", config.max_tokens ?? 2048);
  setValue("#ccb-native-tools", String(config.use_native_tools !== false));
  setValue("#ccb-images-output", String(images.always_output === true));
  setValue("#ccb-images-inputs", String(images.always_inputs === true));
  setValue("#ccb-images-mention", String(images.auto_on_mention !== false));
  setValue("#ccb-images-max", images.max_dimension ?? 1024);
  setValue("#ccb-selection-enabled", String(selection.enabled !== false));
  setValue("#ccb-highlight-enabled", String(highlight.enabled !== false));
  setValue("#ccb-layout-auto", String(config.layout?.auto_after_build !== false));
  setValue("#ccb-validate-auto", String(config.validate?.auto_fix !== false));
  setValue("#ccb-context-window", config.context?.window ?? 0);
  setValue("#ccb-context-auto", String(config.context?.auto_compact !== false));
  setValue("#ccb-context-keep", config.context?.keep_last ?? 12);
  setValue("#ccb-memory-enabled", String(config.memory?.enabled !== false));
  setValue("#ccb-memory-autodetect", String(config.memory?.auto_detect !== false));
  setValue("#ccb-memory-limit", config.memory?.inject_limit ?? 8);
  setValue("#ccb-system-prompt", config.system_prompt || "");
  setValue("#ccb-websearch-provider", websearch.provider || "tavily");
  setPlaceholder("#ccb-websearch-key", websearch.api_key_configured ? "\u2022\u2022\u2022 configured" : "not set");
  setValue("#ccb-websearch-count", websearch.max_results ?? 5);
  setValue("#ccb-kb-enabled", String(kb.enabled !== false));
  setValue("#ccb-kb-auto-official", String(kb.auto_official !== false));
  setValue("#ccb-kb-refresh-days", kb.refresh_days ?? 7);
  setChecked("#ccb-debug-enabled", config.debug?.enabled === true);
  updateDebugStatus();
  updateVisionBadge();
  updateContextStatus();
  refreshKbStatus();
}

function setValue(selector, value) {
  const element = state.dom.root.querySelector(selector);
  if (element) element.value = value;
}

function setPlaceholder(selector, value) {
  const element = state.dom.root.querySelector(selector);
  if (element) element.placeholder = value;
}

function setChecked(selector, value) {
  const element = state.dom.root.querySelector(selector);
  if (element) element.checked = value === true;
}

function isChecked(selector) {
  return state.dom.root.querySelector(selector)?.checked === true;
}

function applyProviderDefaultUrl() {
  const provider = state.dom.root.querySelector("#ccb-provider").value;
  const defaults = {
    lmstudio: "http://127.0.0.1:1234/v1",
    ollama: "http://127.0.0.1:11434/v1",
    openai: "https://api.openai.com/v1",
    anthropic: "https://api.anthropic.com",
  };
  const field = state.dom.root.querySelector("#ccb-base-url");
  if (defaults[provider] && (!field.value || Object.values(defaults).includes(field.value))) {
    field.value = defaults[provider];
  }
}

function collectSettings() {
  const get = (selector) => state.dom.root.querySelector(selector)?.value ?? "";
  const payload = {
    provider: get("#ccb-provider"),
    base_url: get("#ccb-base-url"),
    model: get("#ccb-model"),
    temperature: Number(get("#ccb-temperature")) || 0,
    max_tokens: Number(get("#ccb-max-tokens")) || 2048,
    use_native_tools: get("#ccb-native-tools") === "true",
    system_prompt: get("#ccb-system-prompt"),
    websearch: {
      provider: get("#ccb-websearch-provider"),
      max_results: Number(get("#ccb-websearch-count")) || 5,
    },
    kb: {
      enabled: get("#ccb-kb-enabled") === "true",
      auto_official: get("#ccb-kb-auto-official") === "true",
      refresh_days: Number(get("#ccb-kb-refresh-days")) || 7,
    },
    images: {
      always_output: get("#ccb-images-output") === "true",
      always_inputs: get("#ccb-images-inputs") === "true",
      auto_on_mention: get("#ccb-images-mention") === "true",
      max_dimension: Number(get("#ccb-images-max")) || 1024,
    },
    selection: {
      enabled: get("#ccb-selection-enabled") === "true",
    },
    highlight: {
      enabled: get("#ccb-highlight-enabled") === "true",
    },
    layout: {
      auto_after_build: get("#ccb-layout-auto") === "true",
    },
    validate: {
      auto_fix: get("#ccb-validate-auto") === "true",
      max_passes: 1,
    },
    context: {
      window: Number(get("#ccb-context-window")) || 0,
      auto_compact: get("#ccb-context-auto") === "true",
      keep_last: Number(get("#ccb-context-keep")) || 12,
    },
    memory: {
      enabled: get("#ccb-memory-enabled") === "true",
      auto_detect: get("#ccb-memory-autodetect") === "true",
      inject_limit: Number(get("#ccb-memory-limit")) || 8,
    },
    debug: {
      enabled: isChecked("#ccb-debug-enabled"),
    },
  };
  const apiKey = get("#ccb-api-key");
  if (apiKey) payload.api_key = apiKey;
  const websearchKey = get("#ccb-websearch-key");
  if (websearchKey) payload.websearch.api_key = websearchKey;
  return payload;
}

async function saveSettings() {
  setStatus("Saving...");
  try {
    const response = await api.fetchApi("/chatbot/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.config = payload.config;
    populateSettings();
    setStatus("Saved.");
  } catch (error) {
    setStatus(`Save failed: ${error.message}`);
  }
}

function setModelOptions(models, selected) {
  const select = state.dom.root.querySelector("#ccb-model");
  if (!select) return;
  const values = [];
  const add = (value) => {
    const text = String(value || "").trim();
    if (text && !values.includes(text)) values.push(text);
  };
  if (selected) add(selected);
  for (const model of models || []) add(model);
  select.innerHTML = "";
  if (!values.length) {
    select.appendChild(el("option", { value: "", text: "(no models \u2014 click Refresh)" }));
    return;
  }
  for (const value of values) {
    select.appendChild(el("option", { value, text: value }));
  }
  select.value = values.includes(selected) ? selected : values[0];
}

async function refreshModels(isTest = false) {
  const select = state.dom.root.querySelector("#ccb-model");
  const current = select?.value || state.config?.model || "";
  setStatus(isTest ? "Testing connection..." : "Loading models...");
  try {
    const response = await api.fetchApi("/chatbot/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.availableModels = payload.models || [];
    setModelOptions(state.availableModels, current);
    setStatus(`Connected. ${state.availableModels.length} model(s) available.`);
    detectVision();
    detectContextWindow();
  } catch (error) {
    setModelOptions(current ? [current] : state.availableModels, current);
    setStatus(`Connection failed: ${error.message}`);
  }
}

function updateVisionBadge() {
  const element = state.dom.root.querySelector("#ccb-vision");
  if (!element) return;
  const label = state.visionSupported === true ? "yes" : state.visionSupported === false ? "no" : "unknown";
  element.textContent = `Vision support: ${label}`;
}

async function detectVision() {
  const model = state.dom.root.querySelector("#ccb-model")?.value || "";
  if (!model) {
    state.visionSupported = null;
    updateVisionBadge();
    return;
  }
  try {
    const response = await api.fetchApi("/chatbot/vision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...collectSettings(), model }),
    });
    const payload = await response.json();
    state.visionSupported = payload.vision ?? null;
  } catch {
    state.visionSupported = null;
  }
  updateVisionBadge();
  debugLog("vision", "detected", { vision: state.visionSupported });
}

async function refreshKbStatus() {
  const element = state.dom.root.querySelector("#ccb-kb-status");
  if (!element) return;
  try {
    const response = await api.fetchApi("/chatbot/kb/status");
    const payload = await response.json();
    const kb = payload.status || {};
    const bar = state.dom.root.querySelector("#ccb-kb-bar");
    const progress = state.dom.root.querySelector("#ccb-kb-progress");
    const done = kb.progress_done ?? 0;
    const total = kb.progress_total ?? 0;
    const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    if (progress && bar) {
      progress.classList.toggle("ccb-active", Boolean(kb.building));
      progress.classList.toggle("ccb-indeterminate", Boolean(kb.building) && total <= 0);
      bar.style.width = total > 0 ? `${percent}%` : "";
    }
    const parts = [];
    if (kb.building) {
      const detail = total > 0 ? `${done}/${total}` : "starting";
      parts.push(`${kb.phase || "building"} (${detail})`);
    } else {
      parts.push(kb.phase === "Failed" ? "failed" : "idle");
    }
    parts.push(`${kb.chunks ?? 0} chunks`);
    parts.push(`${kb.files ?? 0} files`);
    parts.push(`official: ${kb.official_fetched_iso || "never"}`);
    if (kb.error) parts.push(`error: ${kb.error}`);
    element.textContent = `Knowledge base: ${parts.join(" \u00b7 ")}`;
    const panelOpen = state.dom.panel.classList.contains("ccb-open");
    clearTimeout(state.kbTimer);
    if (kb.building) {
      state.kbTimer = setTimeout(refreshKbStatus, 1000);
    } else if (panelOpen) {
      state.kbTimer = setTimeout(refreshKbStatus, 5000);
    }
  } catch (error) {
    element.textContent = `Knowledge base: status failed (${error.message})`;
  }
}

async function rebuildKb(syncOfficial) {
  const element = state.dom.root.querySelector("#ccb-kb-status");
  if (element) {
    element.textContent = syncOfficial ? "Knowledge base: syncing official docs\u2026" : "Knowledge base: rebuilding\u2026";
  }
  try {
    const route = syncOfficial ? "/chatbot/kb/sync_official" : "/chatbot/kb/rebuild";
    await api.fetchApi(route, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings().kb),
    });
    clearTimeout(state.kbTimer);
    state.kbTimer = setTimeout(refreshKbStatus, 800);
  } catch (error) {
    if (element) element.textContent = `Knowledge base: ${error.message}`;
  }
}

function setStatus(text) {
  state.dom.status.textContent = text;
}

function resolveWorkflowKey() {
  const workflow = app.workflowManager?.activeWorkflow || app.extensionManager?.workflow?.activeWorkflow;
  if (!workflow) return null;
  const path = String(workflow.path ?? "").trim();
  const isTemporary = workflow.isTemporary === true || workflow.size === -1;
  if (path && !isTemporary) return `path:${path}`;
  const id = String(workflow.activeState?.id ?? workflow.id ?? workflow.key ?? "").trim();
  if (id) return `id:${id}`;
  return path ? `path:${path}` : null;
}

function renderHistory() {
  state.dom.messages.innerHTML = "";
  for (const message of state.messages) {
    if (message.synthetic) continue;
    if ((message.role === "user" || message.role === "assistant") && typeof message.content === "string" && message.content) {
      const suffix = message.image_count ? `\n[${message.image_count} image(s)]` : "";
      appendBubble(message.role, message.content + suffix);
    }
  }
}

async function loadHistory() {
  const key = state.sessionKey || resolveWorkflowKey() || "__default__";
  state.sessionKey = key;
  try {
    const response = await api.fetchApi(`/chatbot/history?key=${encodeURIComponent(key)}`);
    const payload = await response.json();
    state.messages = Array.isArray(payload.history) ? payload.history : [];
  } catch {
    state.messages = [];
  }
  renderHistory();
  updateContextStatus();
}

async function switchSession(newKey) {
  if (!newKey || newKey === state.sessionKey) return;
  debugLog("session", "switch", { key_hash: hashKey(newKey) });
  const previousKey = state.sessionKey;
  state.sessionKey = newKey;
  if (state.busy) {
    state.runId += 1;
    state.cancelled = true;
    if (state.pendingConfirm) state.pendingConfirm(false);
    if (state.abortController) state.abortController.abort();
    setBusy(false);
  }
  await saveHistory(previousKey);
  state.messages = [];
  state.attachments = [];
  renderAttachments();
  await loadHistory();
  if (!state.messages.length) {
    appendNotice("New chat session for this workflow.");
  }
  scrollMessages();
}

function onWorkflowMaybeChanged(event) {
  const detailId = String(event?.detail?.id ?? "").trim();
  const key = resolveWorkflowKey() || (detailId ? `id:${detailId}` : null);
  if (key && key !== state.sessionKey) switchSession(key);
}

function startSessionWatcher() {
  try {
    api.addEventListener("graphChanged", onWorkflowMaybeChanged);
  } catch {
    /* older frontends lack graphChanged; the poll below covers it */
  }
  setInterval(onWorkflowMaybeChanged, 1500);
}

async function saveHistory(keyOverride) {
  const key = keyOverride || state.sessionKey || resolveWorkflowKey() || "__default__";
  const messages = state.messages.map((message) => {
    const copy = { role: message.role, content: message.content };
    if (message.tool_calls) copy.tool_calls = message.tool_calls;
    if (message.tool_call_id) copy.tool_call_id = message.tool_call_id;
    if (message.images?.length) copy.image_count = message.images.length;
    if (message.synthetic) copy.synthetic = true;
    return copy;
  });
  try {
    await api.fetchApi("/chatbot/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, messages }),
    });
  } catch {
    /* history persistence is best-effort */
  }
}

function estimateTokens(text) {
  return Math.ceil(String(text || "").length / 4);
}

function messagesTokens(messages) {
  let total = 0;
  for (const message of messages) {
    if (typeof message.content === "string") {
      total += estimateTokens(message.content);
    } else if (Array.isArray(message.content)) {
      for (const part of message.content) {
        if (part?.type === "text") total += estimateTokens(part.text);
        else if (part?.type === "image_url") total += 1000;
      }
    }
    if (message.tool_calls) total += estimateTokens(safeStringify(message.tool_calls));
  }
  return total;
}

function contextBudget() {
  const window = Number(state.contextWindow || state.config?.context?.window || 0);
  if (!window) return 0;
  const maxTokens = Number(state.config?.max_tokens) || 2048;
  const reserve = Math.min(maxTokens, Math.floor(window / 2));
  return Math.max(1000, window - reserve - 512);
}

function updateContextStatus() {
  const element = state.dom.root?.querySelector("#ccb-context-status");
  if (!element) return;
  const used = messagesTokens(buildMessages());
  const window = Number(state.contextWindow || state.config?.context?.window || 0);
  const windowText = window ? `${Math.round(window / 1000)}k` : "unknown";
  element.textContent = `Context: ~${used.toLocaleString()} tokens used of ${windowText}`;
}

function truncateOldToolResults() {
  const keepRecent = 6;
  for (let index = 0; index < state.messages.length - keepRecent; index += 1) {
    const message = state.messages[index];
    if (message.role === "tool" && typeof message.content === "string" && message.content.length > 2000) {
      message.content = truncate(message.content, 2000);
    }
  }
}

function transcriptForSummary(messages) {
  const lines = messages.map((message) => {
    let content = typeof message.content === "string" ? message.content : "[images]";
    if (message.tool_calls) {
      content += ` [called: ${message.tool_calls.map((call) => call.function?.name).join(", ")}]`;
    }
    return `${message.role.toUpperCase()}: ${truncate(content, 1500)}`;
  });
  return truncate(lines.join("\n"), 24000);
}

async function requestSummary(messages) {
  const response = await api.fetchApi("/chatbot/summarize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: transcriptForSummary(messages) }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload.summary || "";
}

async function compactContext() {
  const keepLast = Math.max(4, Number(state.config?.context?.keep_last) || 12);
  if (state.messages.length <= keepLast + 2) return false;
  let splitIndex = state.messages.length - keepLast;
  while (splitIndex > 0 && state.messages[splitIndex]?.role === "tool") splitIndex -= 1;
  while (
    splitIndex > 0 &&
    state.messages[splitIndex - 1]?.role === "assistant" &&
    state.messages[splitIndex - 1]?.tool_calls?.length
  ) {
    splitIndex -= 1;
  }
  const older = state.messages.slice(0, splitIndex);
  if (!older.length) return false;
  const summary = await requestSummary(older);
  if (!summary) return false;
  state.messages = [
    { role: "system", content: `Summary of earlier conversation:\n${summary}` },
    ...state.messages.slice(splitIndex),
  ];
  appendNotice("Compacted the earlier conversation to fit the model context.");
  updateContextStatus();
  await saveHistory();
  return true;
}

async function prepareContext() {
  truncateOldToolResults();
  if (state.config?.context?.auto_compact === false) return;
  const budget = contextBudget();
  if (!budget) return;
  if (messagesTokens(buildMessages()) <= budget) return;
  try {
    const compacted = await compactContext();
    debugLog("context", "compact", { compacted, budget });
  } catch (error) {
    debugLog("context", "compact_error", { error: String(error?.message || error) });
    appendNotice(`Compaction failed: ${error.message}`);
  }
}

async function detectContextWindow() {
  const model = state.dom.root.querySelector("#ccb-model")?.value || "";
  if (!model) {
    state.contextWindow = null;
    updateContextStatus();
    return;
  }
  try {
    const response = await api.fetchApi("/chatbot/context", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...collectSettings(), model }),
    });
    const payload = await response.json();
    state.contextWindow = payload.window ?? null;
  } catch {
    state.contextWindow = null;
  }
  updateContextStatus();
  debugLog("context", "window", { window: state.contextWindow });
}

async function loadLessons() {
  try {
    const response = await api.fetchApi("/chatbot/memory");
    const payload = await response.json();
    state.lessons = Array.isArray(payload.lessons) ? payload.lessons : [];
  } catch {
    state.lessons = [];
  }
  renderMemoryList();
}

async function refreshRelevantLessons(query) {
  if (state.config?.memory?.enabled === false) {
    state.relevantLessons = [];
    return;
  }
  const limit = Number(state.config?.memory?.inject_limit) || 8;
  try {
    const response = await api.fetchApi("/chatbot/memory/relevant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query || "", limit }),
    });
    const payload = await response.json();
    state.relevantLessons = Array.isArray(payload.lessons) ? payload.lessons : [];
  } catch {
    state.relevantLessons = [];
  }
}

async function rememberLesson(text, tags, pinned) {
  if (!text) return { error: "Missing lesson text." };
  const response = await api.fetchApi("/chatbot/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "add", text, tags: tags || "", source: "assistant", pinned: pinned === true }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  state.lessons = payload.lessons || [];
  renderMemoryList();
  await refreshRelevantLessons(text);
  return { ok: true, remembered: text };
}

async function searchMemory(query, limit) {
  const response = await api.fetchApi("/chatbot/memory/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, limit }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return { lessons: (payload.lessons || []).map((lesson) => lesson.text) };
}

async function updateMemory(payload) {
  const response = await api.fetchApi("/chatbot/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  state.lessons = result.lessons || [];
  renderMemoryList();
}

function renderMemoryList() {
  const container = state.dom.root.querySelector("#ccb-memory-list");
  if (!container) return;
  container.innerHTML = "";
  if (!state.lessons.length) {
    container.appendChild(el("div", { class: "ccb-kb-status", text: "No lessons saved yet." }));
    return;
  }
  for (const lesson of state.lessons) {
    const row = el("div", { class: "ccb-lesson" });
    const text = el("div", { class: "ccb-lesson-text", text: lesson.text });
    const meta = el("div", { class: "ccb-lesson-meta", text: `${lesson.pinned ? "pinned \u00b7 " : ""}${lesson.source || "user"}` });
    const actions = el("div", { class: "ccb-lesson-actions" });
    const enabled = el("button", { class: "ccb-btn", text: lesson.enabled ? "Disable" : "Enable" });
    enabled.addEventListener("click", () => updateMemory({ action: "update", id: lesson.id, enabled: !lesson.enabled }).catch((error) => appendNotice(error.message)));
    const pin = el("button", { class: "ccb-btn", text: lesson.pinned ? "Unpin" : "Pin" });
    pin.addEventListener("click", () => updateMemory({ action: "update", id: lesson.id, pinned: !lesson.pinned }).catch((error) => appendNotice(error.message)));
    const edit = el("button", { class: "ccb-btn", text: "Edit" });
    edit.addEventListener("click", () => {
      const next = window.prompt("Edit lesson", lesson.text);
      if (next == null) return;
      updateMemory({ action: "update", id: lesson.id, text: next }).catch((error) => appendNotice(error.message));
    });
    const remove = el("button", { class: "ccb-btn ccb-danger", text: "Delete" });
    remove.addEventListener("click", () => updateMemory({ action: "delete", id: lesson.id }).catch((error) => appendNotice(error.message)));
    actions.append(enabled, pin, edit, remove);
    row.append(text, meta, actions);
    container.appendChild(row);
  }
}

function lessonsContext() {
  if (state.config?.memory?.enabled === false) return "";
  const lessons = state.relevantLessons;
  if (!lessons.length) return "";
  let total = 0;
  const lines = [];
  for (const lesson of lessons) {
    const line = `- ${lesson.text}${lesson.pinned ? " (always apply)" : ""}`;
    total += line.length;
    if (total > 2000) break;
    lines.push(line);
  }
  if (!lines.length) return "";
  return `Lessons learned from past corrections and preferences (follow these):\n${lines.join("\n")}`;
}

app.registerExtension({
  name: "ComfyUI.Assistant",
  async setup() {
    buildUi();
    window.addEventListener("error", (event) => {
      debugLog("ui", "error", { message: String(event.message || ""), line: event.lineno });
    });
    window.addEventListener("unhandledrejection", (event) => {
      debugLog("ui", "unhandled_rejection", { reason: String(event.reason?.message || event.reason || "") });
    });
    api.addEventListener("executed", ({ detail }) => {
      const images = detail?.output?.images;
      if (!Array.isArray(images) || !images.length) return;
      for (const image of images) {
        state.lastOutputs.push({
          filename: image.filename,
          subfolder: image.subfolder || "",
          type: image.type || "output",
          url: imageUrl(image),
          ts: Date.now(),
        });
      }
      if (state.lastOutputs.length > 20) {
        state.lastOutputs = state.lastOutputs.slice(-20);
      }
    });
    state.sessionKey = resolveWorkflowKey() || "__default__";
    startSessionWatcher();
    try {
      await loadConfig();
      await loadHistory();
      await loadLessons();
      await refreshRelevantLessons("");
      refreshModels();
    } catch (error) {
      setStatus(`Initialization failed: ${error.message}`);
    }
  },
});
