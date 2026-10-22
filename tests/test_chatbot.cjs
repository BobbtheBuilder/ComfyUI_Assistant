// Exercise the actual browser functions with isolated API/DOM doubles.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/chatbot.js"), "utf8");

function load(names, overrides = {}) {
  const context = vm.createContext({ TextDecoder, AbortController, DOMException, ...overrides });
  if (!overrides.assistantFetchApi) {
    context.assistantFetchApi = (...args) => context.api.fetchApi(...args);
  }
  const snapshotStart = source.search(/^function historyMessageSnapshot\(/m);
  vm.runInContext(source.slice(snapshotStart, source.indexOf("\n}", snapshotStart) + 2), context);
  for (const name of names) {
    const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
    assert.notEqual(start, -1, `Missing function ${name}`);
    vm.runInContext(source.slice(start, source.indexOf("\n}", start) + 2), context);
  }
  return context;
}

function responseFor(chunks) {
  let index = 0;
  const reader = {
    cancelled: false, released: false,
    async read() { return index < chunks.length ? { value: chunks[index++], done: false } : { done: true }; },
    async cancel() { this.cancelled = true; },
    releaseLock() { this.released = true; },
  };
  return { reader, response: { ok: true, body: { getReader: () => reader } } };
}

test("assistant requests use the configured timeout override and preserve cancellation", async () => {
  const controller = new AbortController();
  let receivedOptions;
  let finishRequest;
  let rejectNext = false;
  const pending = new Promise(resolve => { finishRequest = resolve; });
  const c = load(["assistantFetchApi", "limitSetting"], {
    state: { config: { limits: { assistant_fetch_timeout_ms: 0 } } },
    api: { fetchApi(_url, options) {
      receivedOptions = options;
      if (rejectNext) return Promise.reject(new DOMException("Fetch timeout", "TimeoutError"));
      return pending;
    } },
  });
  const request = c.assistantFetchApi("/chatbot/chat", { method: "POST", signal: controller.signal });
  assert.equal(receivedOptions.timeoutMs, null, "null disables ComfyUI's built-in deadline");
  assert.equal(receivedOptions.signal, controller.signal);
  assert.equal(receivedOptions.method, "POST");
  controller.abort();
  assert.equal(receivedOptions.signal.aborted, true);
  finishRequest("complete after a slow initial response");
  assert.equal(await request, "complete after a slow initial response");

  c.state.config.limits.assistant_fetch_timeout_ms = 1800000;
  c.assistantFetchApi("/chatbot/summarize");
  assert.equal(receivedOptions.timeoutMs, 1800000);
  rejectNext = true;
  await assert.rejects(c.assistantFetchApi("/chatbot/context?model=private"), error => {
    assert.match(error.message, /\/chatbot\/context after 1800000 ms/);
    assert.doesNotMatch(error.message, /private/);
    return error.cause?.name === "TimeoutError";
  });
});

test("assistant endpoint calls are centralized through assistantFetchApi", () => {
  const directCalls = source.match(/api\.fetchApi\(/g) || [];
  assert.equal(directCalls.length, 1, "all ComfyUI fetches use assistantFetchApi");
  assert.match(source, /function assistantFetchApi\(path, options = \{\}\)/);
  assert.doesNotMatch(source, /api\.getNodeDefs\(/);
});

test("large installed-node discovery uses the assistant timeout override", async () => {
  const calls = [];
  const defs = { KSampler: {} };
  const c = load(["getNodeDefs", "assistantFetchApi", "limitSetting"], {
    state: { config: { limits: { assistant_fetch_timeout_ms: 0 } } },
    api: { async fetchApi(route, options) {
      calls.push({ route, options });
      return { json: async () => defs };
    } },
  });
  assert.equal(await c.getNodeDefs(), defs);
  assert.equal(await c.getNodeDefs(), defs);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].route, "/object_info");
  assert.equal(calls[0].options.timeoutMs, null);
  assert.equal(calls[0].options.cache, "no-store");
});

test("chat compaction keeps complete tool exchanges and a persisted restorable transcript", async () => {
  const sourceMessages = [
    { role: "user", content: "old 0" },
    { role: "assistant", content: "old 1" },
    { role: "user", content: "old 2" },
    { role: "assistant", content: "old 3" },
    { role: "user", content: "old 4" },
    { role: "assistant", content: "", tool_calls: [{ id: "call-1" }] },
    { role: "tool", tool_call_id: "call-1", content: "tool result" },
    { role: "assistant", content: "tool follow-up" },
    { role: "user", content: "latest question" },
    { role: "assistant", content: "latest answer" },
  ];
  let saved = false;
  let summarized;
  const c = load(["compactContext", "limitSetting", "historyMessageSnapshot"], {
    state: { sessionKey: "workflow-A", historyRevision: 3, messages: sourceMessages,
      config: { context: { keep_last: 4 } } },
    resolveWorkflowKey: () => "workflow-A",
    requestSummary: async messages => { summarized = messages; return "earlier topics"; },
    appendNotice() {}, updateContextStatus() {}, updateCompactionButton() {}, saveHistory: async () => { saved = true; },
  });
  assert.equal(await c.compactContext({ manual: true }), true);
  assert.equal(summarized.length, 5);
  assert.deepEqual(Array.from(c.state.messages.slice(1, 4), message => message.role), ["assistant", "tool", "assistant"]);
  assert.equal(c.state.messages[0].compaction_archive.length, sourceMessages.length);
  assert.equal(c.state.messages[0].compaction_tail_count, 5);
  assert.equal(c.state.messages.at(-2).content, "latest question");
  assert.equal(saved, true);
});

test("compaction discards stale summaries and never overwrites a changed transcript", async () => {
  let finishSummary;
  const pendingSummary = new Promise(resolve => { finishSummary = resolve; });
  const original = Array.from({ length: 10 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", content: `message ${index}` }));
  const c = load(["compactContext", "limitSetting", "historyMessageSnapshot"], {
    state: { sessionKey: "workflow-A", historyRevision: 4, messages: original, config: { context: { keep_last: 4 } } },
    requestSummary: () => pendingSummary,
    appendNotice() {}, updateContextStatus() {}, updateCompactionButton() {}, saveHistory: async () => assert.fail("stale compaction must not save"),
  });
  const pending = c.compactContext({ manual: true });
  c.state.messages.push({ role: "user", content: "new message during summary" });
  finishSummary("summary of the old transcript");
  assert.equal(await pending, false);
  assert.equal(c.state.messages.at(-1).content, "new message during summary");

  let finishOwnerSummary;
  const ownerPending = new Promise(resolve => { finishOwnerSummary = resolve; });
  c.state.messages = original;
  c.state.sessionKey = "workflow-A";
  c.requestSummary = () => ownerPending;
  const staleOwner = c.compactContext({ manual: true });
  c.state.sessionKey = "workflow-B";
  c.state.historyRevision += 1;
  c.state.messages = [{ role: "user", content: "workflow B" }];
  finishOwnerSummary("summary for workflow A");
  assert.equal(await staleOwner, false);
  assert.equal(c.state.messages[0].content, "workflow B");

  let liveWorkflow = "workflow-A";
  let finishLiveSummary;
  const pendingLiveSummary = new Promise(resolve => { finishLiveSummary = resolve; });
  c.state.sessionKey = "workflow-A";
  c.state.historyRevision = 7;
  c.state.messages = original;
  c.resolveWorkflowKey = () => liveWorkflow;
  c.requestSummary = () => pendingLiveSummary;
  const staleLiveOwner = c.compactContext({ manual: true });
  liveWorkflow = "workflow-B";
  finishLiveSummary("summary for inactive workflow A");
  assert.equal(await staleLiveOwner, false);
  assert.equal(c.state.messages, original);
});

test("empty summary leaves the original transcript intact", async () => {
  const messages = Array.from({ length: 10 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", content: `message ${index}` }));
  const c = load(["compactContext", "limitSetting", "historyMessageSnapshot"], {
    state: { sessionKey: "workflow-A", historyRevision: 1, messages, config: { context: { keep_last: 4 } } },
    requestSummary: async () => "", appendNotice() {}, updateContextStatus() {}, updateCompactionButton() {}, saveHistory: async () => {},
  });
  assert.equal(await c.compactContext(), false);
  assert.equal(c.state.messages, messages);
});

test("compaction archive is persisted and can restore the full owner chat", async () => {
  let savedBody;
  const prior = [
    { role: "user", content: "original question" }, { role: "assistant", content: "original answer" },
    { role: "user", content: "retained question" }, { role: "assistant", content: "retained answer" },
  ];
  const current = [
    { role: "system", content: "Summary of earlier conversation", compaction_archive: prior, compaction_tail_count: 2 },
    ...prior.slice(-2), { role: "user", content: "new question" }, { role: "assistant", content: "new answer" },
  ];
  const c = load(["saveHistory", "restorePreviousChat", "compactArchiveMessage", "historyMessageSnapshot"], {
    state: { sessionKey: "workflow-A", historyRevision: 2, messages: current },
    api: { fetchApi: async (_url, options) => { savedBody = JSON.parse(options.body); return { ok: true }; } },
    resolveWorkflowKey: () => "workflow-A", renderHistory() {}, updateContextStatus() {}, updateCompactionButton() {},
    appendNotice() {},
  });
  await c.saveHistory();
  assert.deepEqual(savedBody.messages[0].compaction_archive, prior);
  assert.equal(savedBody.messages[0].compaction_tail_count, 2);
  assert.equal(await c.restorePreviousChat(), true);
  assert.equal(JSON.stringify(c.state.messages), JSON.stringify([...prior, ...current.slice(3)]));
  assert.equal(savedBody.key, "workflow-A");
});

test("Strata uses its default URL while preserving custom server addresses", () => {
  const provider = { value: "strata" };
  const field = { value: "http://127.0.0.1:1234/v1" };
  const c = load(["applyProviderDefaultUrl"], {
    state: { dom: { root: { querySelector: selector => selector === "#ccb-provider" ? provider : field } } },
  });
  c.applyProviderDefaultUrl();
  assert.equal(field.value, "http://p40:8080/v1");
  provider.value = "ollama";
  c.applyProviderDefaultUrl();
  assert.equal(field.value, "http://127.0.0.1:11434/v1");
  provider.value = "strata";
  field.value = "http://custom-host:8080/v1";
  c.applyProviderDefaultUrl();
  assert.equal(field.value, "http://custom-host:8080/v1");
});

test("assistant setup returns without waiting for startup reads", () => {
  let extension;
  let startupReadsStarted = false;
  const c = load(["setupAssistant"], {
    app: { registerExtension(value) { extension = value; } },
    api: { addEventListener() {} },
    window: { addEventListener() {} },
    state: { pendingRuns: new Map(), lastOutputs: [] },
    buildUi() {}, installRunCapture() {}, resolveWorkflowKey: () => "id:workflow",
    startSessionWatcher() {}, setupCanvasCoexistence() {},
    initializeAssistantData() { startupReadsStarted = true; return new Promise(() => {}); },
  });
  assert.equal(typeof c.setupAssistant, "function");
  assert.equal(c.setupAssistant(), undefined);
  assert.equal(startupReadsStarted, true);
  assert.equal(c.state.historyLoading, true);
});

test("background startup keeps history loading until configuration finishes", async () => {
  let finishConfig;
  const configPending = new Promise(resolve => { finishConfig = resolve; });
  const calls = [];
  const c = load(["initializeAssistantData"], {
    loadConfig: () => configPending,
    loadHistory: async () => calls.push("history"),
    loadLessons: async () => calls.push("lessons"),
    loadExperiences: async () => calls.push("experiences"),
    debugLog() {}, setStatus() {},
  });
  const initialization = c.initializeAssistantData();
  assert.deepEqual(calls, []);
  finishConfig();
  await initialization;
  assert.deepEqual(calls.sort(), ["experiences", "history", "lessons"]);
});

test("settings hydration is local and three embedding selectors share discovery", async () => {
  const selections = new Map(["#ccb-embedding-model", "#ccb-memory-embed-model", "#ccb-kb-embed-model"]
    .map(selector => [selector, { value: "", options: [], appendChild(option) { this.options.push(option); }, set innerHTML(_value) { this.options = []; } }]));
  selections.set("#ccb-thinking-level", { value: "", options: [], appendChild(option) { this.options.push(option); }, set innerHTML(_value) { this.options = []; } });
  let requests = 0;
  const c = load(["populateSettings", "setLocalModelOption", "refreshEmbedModels", "populateThinkingLevelMenu", "supportedThinkingLevels"], {
    state: { config: {
      embedding: { model: "embed-main" },
      memory: { embed: { model: "embed-memory" } },
      kb: { embed: { model: "embed-kb" } },
    }, availableModels: [], dom: { root: { querySelector(selector) { return selections.get(selector) || { value: "" }; } } } },
    setValue() {}, setPlaceholder() {}, setChecked() {}, setModelOptions() {},
    updateDebugStatus() {}, updateVisionBadge() {}, updateContextStatus() {},
    el: (_tag, props) => props,
    collectSettings: () => ({ provider: "chat", base_url: "http://provider", embedding: { provider: "embed", base_url: "http://embed", api_key: "secret" } }),
    api: { async fetchApi() { requests += 1; await new Promise(resolve => setTimeout(resolve, 0)); return { json: async () => ({ models: ["one", "two"] }) }; } },
    document: { createElement: () => ({}) },
  });
  c.populateSettings();
  assert.equal(requests, 0, "local hydration must not call providers");
  await Promise.all([
    c.refreshEmbedModels("embed-main", "#ccb-embedding-model"),
    c.refreshEmbedModels("embed-memory", "#ccb-memory-embed-model"),
    c.refreshEmbedModels("embed-kb", "#ccb-kb-embed-model"),
  ]);
  assert.equal(requests, 1);
  assert.equal(selections.get("#ccb-embedding-model").value, "embed-main");
  assert.equal(selections.get("#ccb-memory-embed-model").value, "embed-memory");
  assert.equal(selections.get("#ccb-kb-embed-model").value, "embed-kb");
});

test("late model discovery cannot replace results for the newly selected embedding server", async () => {
  const select = { value: "saved", options: [], appendChild(option) { this.options.push(option); }, set innerHTML(_value) { this.options = []; } };
  let baseUrl = "http://server-a";
  const pending = [];
  const c = load(["refreshEmbedModels"], {
    state: { dom: { root: { querySelector: () => select } } },
    collectSettings: () => ({ embedding: { provider: "embed", base_url: baseUrl } }),
    api: { fetchApi: () => new Promise(resolve => pending.push(models => resolve({ json: async () => ({ models }) }))) },
    document: { createElement: () => ({}) },
  });
  const oldRequest = c.refreshEmbedModels("", "#ccb-kb-embed-model");
  baseUrl = "http://server-b";
  const newRequest = c.refreshEmbedModels("", "#ccb-kb-embed-model");
  pending[1](["model-b"]);
  await newRequest;
  pending[0](["model-a"]);
  await oldRequest;
  assert.ok(select.options.some(option => option.value === "model-b"));
  assert.ok(!select.options.some(option => option.value === "model-a"));
  assert.equal(select.value, "saved");
});

test("lesson and experience list loads do not automatically reindex vectors", async () => {
  const paths = [];
  const c = load(["loadLessons", "loadExperiences"], {
    state: { lessons: [], experience: [], config: { memory: { embed: { enabled: true } }, experience: { enabled: true } }, dom: { root: { querySelector: () => null } } },
    api: { async fetchApi(path) { paths.push(path); return { json: async () => ({ lessons: [], experiences: [], embedding: {} }) }; } },
    updateMemoryStatus() {}, updateExperienceStatus() {}, renderMemoryList() {}, renderExperienceList() {},
  });
  await c.loadLessons();
  await c.loadExperiences();
  assert.deepEqual(paths, ["/chatbot/memory", "/chatbot/experience"]);
});



test("workflow identity stays stable through lazy IDs and saving, and new tabs stay separate", () => {
  const app = { workflowManager: { activeWorkflow: { isTemporary: true } } };
  const make = (nonce) => load(["resolveWorkflowKey"], {
    app, _workflowSessionKeys: new WeakMap(), _workflowSessionNonce: nonce, _workflowSessionSeq: 0,
  });
  const c = make("page-a");
  const first = c.resolveWorkflowKey();
  Object.assign(app.workflowManager.activeWorkflow, { activeState: { id: "later" }, path: "saved.json", isTemporary: false });
  assert.equal(c.resolveWorkflowKey(), first);
  app.workflowManager.activeWorkflow = { isTemporary: true };
  assert.notEqual(c.resolveWorkflowKey(), first);
  assert.notEqual(make("page-b").resolveWorkflowKey(), first);
});

test("modern workflow IDs survive restored objects, reloads, saving and renaming", () => {
  const app = { extensionManager: { workflow: { activeWorkflow: {
    path: "workflows/Unsaved Workflow.json", size: -1, isLoaded: true,
    activeState: { id: "workflow-a" },
  } } }, workflowManager: { activeWorkflow: { activeState: { id: "stale" } } } };
  const make = (nonce) => load(["resolveWorkflowKey"], {
    app, _workflowSessionKeys: new WeakMap(), _workflowSessionNonce: nonce, _workflowSessionSeq: 0,
  });
  const first = make("page-a").resolveWorkflowKey();
  assert.equal(first, "id:workflow-a", "modern store takes priority");
  app.extensionManager.workflow.activeWorkflow = {
    path: "workflows/renamed.json", size: 42, isLoaded: true, activeState: { id: "workflow-a" },
  };
  assert.equal(make("page-b").resolveWorkflowKey(), first);
  app.extensionManager.workflow.activeWorkflow = {
    path: "workflows/Unsaved Workflow.json", size: -1, activeState: { id: "workflow-b" },
  };
  assert.notEqual(make("page-c").resolveWorkflowKey(), first, "reused tab name is not an identity");
});

test("loading workflows wait for identity and can use their own serialized content", () => {
  const workflow = { isLoaded: false, size: -1 };
  const app = { extensionManager: { workflow: { activeWorkflow: workflow } }, graph: { id: "previous-tab" } };
  const c = load(["resolveWorkflowKey"], {
    app, _workflowSessionKeys: new WeakMap(), _workflowSessionNonce: "page", _workflowSessionSeq: 0,
  });
  assert.equal(c.resolveWorkflowKey(), null);
  workflow.content = JSON.stringify({ id: "correct-owner" });
  assert.equal(c.resolveWorkflowKey(), "id:correct-owner");
  workflow.activeState = { id: "changed-later" };
  assert.equal(c.resolveWorkflowKey(), "id:changed-later", "resetting the same object must change its modern owner");
});

test("unassigned startup never reads or writes the shared default chat", async () => {
  const state = { sessionKey: null, messages: [{ content: "unassigned" }] };
  const c = load(["loadHistory", "saveHistory"], {
    state, resolveWorkflowKey: () => null,
    api: { fetchApi: () => assert.fail("No workflow means no history request") },
  });
  await c.loadHistory();
  await c.saveHistory();
  state.sessionKey = "__default__";
  await c.saveHistory();
});

test("draft text and attachments return to their own workflow", async () => {
  const image = { dataUrl: "test-image" };
  const state = {
    sessionKey: "a", messages: [], attachments: [image],
    dom: { input: { value: "draft A", style: {} } },
  };
  const c = load(["switchSession"], {
    state, debugLog: () => {}, hashKey: (x) => x, renderAttachments: () => {}, renderHistory: () => {},
    appendNotice: () => {}, scrollMessages: () => {}, loadHistory: async () => {},
    saveHistory: async () => {},
  });
  await c.switchSession("b");
  assert.equal(state.dom.input.value, "");
  assert.equal(state.attachments.length, 0);
  state.dom.input.value = "draft B";
  await c.switchSession("a");
  assert.equal(state.dom.input.value, "draft A");
  assert.equal(state.attachments[0], image);
  await c.switchSession("b");
  assert.equal(state.dom.input.value, "draft B");
});

test("New clicked before the watcher catches up cannot clear the previous workflow", async () => {
  const state = { sessionKey: "a", messages: [{ content: "A" }] };
  const switched = [];
  const c = load(["startNewChat"], {
    state, resolveWorkflowKey: () => "b", switchSession: async (key) => switched.push(key),
  });
  await c.startNewChat();
  assert.deepEqual(switched, ["b"]);
  assert.equal(state.messages[0].content, "A");
});

test("chat histories round-trip through A-B-A switches and a restored browser session", async () => {
  const stored = new Map();
  const api = { fetchApi: async (url, options) => {
    if (options?.method === "POST") {
      const { key, messages } = JSON.parse(options.body);
      stored.set(key, messages);
      return {};
    }
    const key = decodeURIComponent(url.split("=")[1]);
    return { json: async () => ({ history: stored.get(key) || [] }) };
  } };
  const a = { size: -1, isLoaded: true, activeState: { id: "a" } };
  const b = { size: -1, isLoaded: true, activeState: { id: "b" } };
  const app = { extensionManager: { workflow: { activeWorkflow: a } } };
  const make = (nonce) => {
    const state = { messages: [], attachments: [], dom: { input: { value: "", style: {} } } };
    const c = load(["resolveWorkflowKey", "loadHistory", "saveHistory", "switchSession"], {
      app, api, state, _workflowSessionKeys: new WeakMap(), _workflowSessionNonce: nonce, _workflowSessionSeq: 0,
      debugLog: () => {}, hashKey: (x) => x, renderAttachments: () => {}, renderHistory: () => {},
      updateContextStatus: () => {}, appendNotice: () => {}, scrollMessages: () => {},
    });
    return { c, state };
  };
  const { c, state } = make("first-page");
  await c.switchSession(c.resolveWorkflowKey());
  state.messages.push({ role: "user", content: "belongs to A" });
  app.extensionManager.workflow.activeWorkflow = b;
  await c.switchSession(c.resolveWorkflowKey());
  assert.equal(state.messages.length, 0);
  state.messages.push({ role: "user", content: "belongs to B" });
  app.extensionManager.workflow.activeWorkflow = a;
  await c.switchSession(c.resolveWorkflowKey());
  assert.equal(state.messages[0].content, "belongs to A");
  // The browser restores another object with the same workflow ID, not the old WeakMap.
  app.extensionManager.workflow.activeWorkflow = JSON.parse(JSON.stringify(b));
  const restored = make("next-page");
  await restored.c.loadHistory();
  assert.equal(restored.state.messages[0].content, "belongs to B");
  assert.equal(stored.has("__default__"), false);
});

test("switching tabs during streaming never aborts or changes the chat owner", async () => {
  const controller = new AbortController();
  const state = { busy: true, requestActive: true, sessionKey: "a", abortController: controller };
  let active = "b";
  const switched = [];
  const { response } = responseFor([Buffer.from('{"type":"text","text":"one"}\n'), Buffer.from('{"type":"text","text":"two"}\n')]);
  const c = load(["streamChat", "onWorkflowMaybeChanged", "flushPendingSessionSwitch"], {
    state, resolveWorkflowKey: () => active, api: { fetchApi: async () => response },
    switchSession: async (key) => switched.push(key),
  });
  const result = await c.streamChat([], [], () => c.onWorkflowMaybeChanged(), controller.signal);
  assert.equal(result.text, "onetwo");
  assert.equal(state.sessionKey, "a");
  assert.equal(controller.signal.aborted, false);
  assert.equal(state.pendingSessionKey, "b");
  active = "a";
  c.onWorkflowMaybeChanged();
  assert.equal(state.pendingSessionKey, null);
  state.busy = false;
  await c.flushPendingSessionSwitch();
  assert.equal(switched.length, 0, "repair/preparation lifecycle still owns the chat");
  state.requestActive = false;
  await c.flushPendingSessionSwitch();
  assert.deepEqual(switched, ["a"]);
});

test("late history responses cannot overwrite the new workflow", async () => {
  const state = { sessionKey: "a", messages: [] };
  const requests = new Map();
  const c = load(["loadHistory"], {
    state, renderHistory: () => {}, updateContextStatus: () => {},
    api: { fetchApi: (url) => new Promise((resolve) => requests.set(url.split("=")[1], resolve)) },
  });
  const a = c.loadHistory();
  state.sessionKey = "b";
  const b = c.loadHistory();
  await Promise.resolve();
  requests.get("b")({ json: async () => ({ history: [{ content: "B" }] }) });
  await b;
  requests.get("a")({ json: async () => ({ history: [{ content: "A" }] }) });
  await a;
  assert.equal(state.messages[0].content, "B");
  assert.equal(state.historyLoading, false);
});

test("history saves serialize snapshots so clearing cannot resurrect old messages", async () => {
  const state = { sessionKey: "a", messages: [{ role: "user", content: "old" }] };
  const requests = [];
  const c = load(["saveHistory"], {
    state, api: { fetchApi: (_url, options) => new Promise((resolve) => requests.push({ body: JSON.parse(options.body), resolve })) },
  });
  const old = c.saveHistory();
  state.messages = [];
  const cleared = c.saveHistory();
  await new Promise(setImmediate);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.messages[0].content, "old");
  requests[0].resolve({});
  await old;
  await new Promise(setImmediate);
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].body.messages, []);
  requests[1].resolve({});
  await cleared;
});

test("rapid idle switches save the old owner before changing sessions", async () => {
  const state = { sessionKey: "a", messages: [{ content: "A" }], attachments: [] };
  const saves = [];
  let finish;
  const c = load(["switchSession"], {
    state, debugLog: () => {}, hashKey: (x) => x, renderAttachments: () => {}, renderHistory: () => {},
    appendNotice: () => {}, scrollMessages: () => {}, loadHistory: async () => { state.historyLoading = true; },
    saveHistory: (key) => { saves.push([key, state.messages[0]?.content]); return new Promise((r) => { finish = r; }); },
  });
  const b = c.switchSession("b");
  const d = c.switchSession("d");
  finish();
  await Promise.all([b, d]);
  assert.deepEqual(saves, [["a", "A"]]);
  assert.equal(state.sessionKey, "d");
});

test("wrong-canvas tools and snapshot restoration are blocked without aborting", async () => {
  const state = { runWorkflowKey: "original", abortController: new AbortController() };
  const c = load(["assertRunWorkflow", "executeTool", "withGraphChange", "loadGraphSnapshot"], {
    state, resolveWorkflowKey: () => "new", app: { graph: {} }, searchDocs: async () => ({ ok: true }),
  });
  await assert.rejects(c.executeTool("get_workflow_summary", {}), /original workflow/);
  assert.throws(() => c.withGraphChange(() => assert.fail("must not mutate")), /original workflow/);
  await assert.rejects(c.loadGraphSnapshot({ nodes: [] }), /original workflow/);
  assert.equal((await c.executeTool("search_docs", {})).ok, true);
  assert.equal(state.abortController.signal.aborted, false);
});

test("manual graph changes block stale edits, while moving nodes remains allowed", async () => {
  const node = { id: 1, type: "Example", pos: [0, 0], widgets: [{ value: "original" }] };
  const state = {};
  const c = load(["graphContentStamp", "withGraphChange", "executeTool"], {
    state, safeStringify: JSON.stringify, workflowSummary: () => ({}),
    app: { graph: { _nodes: [node], beforeChange() {}, afterChange() {}, setDirtyCanvas() {} } },
  });
  state.runGraphSnapshot = c.graphContentStamp();
  node.pos = [500, 600];
  c.withGraphChange(() => { node.widgets[0].value = "assistant"; });
  node.widgets[0].value = "user";
  assert.throws(() => c.withGraphChange(() => assert.fail()), /content changed/);
  await c.executeTool("get_workflow_summary", {});
  c.withGraphChange(() => { node.widgets[0].value = "fresh"; });
});

test("new chat and automatic unload respect the whole request lifecycle", async () => {
  const state = { requestActive: true, busy: false, messages: [{ content: "running" }] };
  const c = load(["startNewChat", "unloadLlm"], { state, appendNotice: () => {} });
  c.startNewChat();
  assert.equal(state.messages[0].content, "running");
  assert.equal((await c.unloadLlm(true)).skipped, true);
});

test("search_docs exposes every knowledge-base source", () => {
  const start = source.indexOf("const TOOLS = [");
  assert.notEqual(start, -1, "Missing TOOLS");
  const end = source.indexOf("\n];", start);
  assert.notEqual(end, -1, "Missing end of TOOLS");
  const context = vm.createContext({});
  vm.runInContext(source.slice(start, end + 3) + "\nglobalThis.__tools = TOOLS;", context);
  const tool = context.__tools.find((entry) => entry.function.name === "search_docs");
  assert.ok(tool, "Missing search_docs tool");
  assert.deepEqual(
    Array.from(tool.function.parameters.properties.source.enum),
    ["pack", "node", "official", "example", "registry", "model"],
  );
});

test("streamChat forwards reasoning without polluting text", async () => {
  const bytes = Buffer.from(
    '{"type":"reasoning","text":"hmm"}\n{"type":"text","text":"Hi"}\n{"type":"reasoning","text":" ok"}\n',
  );
  const { response } = responseFor([bytes]);
  const events = [];
  const c = load(["streamChat", "assistantFetchApi", "limitSetting"], {
    state: { config: { limits: { assistant_fetch_timeout_ms: 0 } } },
    api: { fetchApi: async () => response },
  });
  const result = await c.streamChat([], [], event => events.push(event));
  assert.equal(result.text, "Hi");
  assert.equal(result.reasoning, "hmm ok");
  assert.deepEqual(events.map(event => event.type), ["reasoning", "text", "reasoning"]);
});

test("streamChat sends the request's captured thinking level", async () => {
  const { response } = responseFor([Buffer.from('{"type":"text","text":"ok"}\n')]);
  let requestBody;
  const c = load(["streamChat", "assistantFetchApi", "limitSetting"], {
    state: { config: { limits: { assistant_fetch_timeout_ms: 0 }, thinking: { level: "high" } } },
    api: { fetchApi: async (_url, options) => { requestBody = JSON.parse(options.body); return response; } },
  });
  const requestSnapshot = { level: "low" };
  c.state.config.thinking.level = "high";
  await c.streamChat([], [], null, null, requestSnapshot);
  assert.deepEqual(requestBody.thinking, { level: "low" });
});

test("thinking menu persistence posts only the thinking level", async () => {
  let requestBody;
  const c = load(["saveThinkingLevel"], {
    state: { config: { provider: "strata", temperature: 0.7, thinking: { enabled: false, level: "auto" } },
      dom: { thinkingLevel: { value: "low" } } },
    assistantFetchApi: async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return { ok: true, json: async () => ({ config: { thinking: { enabled: false, level: "low" } } }) };
    },
    setStatus() {},
  });
  await c.saveThinkingLevel();
  assert.deepEqual(requestBody, { thinking: { level: "low" } });
  assert.equal(c.state.config.temperature, 0.7);
  assert.equal(c.state.config.thinking.enabled, false);
  assert.equal(c.state.config.thinking.level, "low");
});

test("shared stream parser handles split UTF-8 and final line without newline", async () => {
  const bytes = Buffer.from('{"type":"text","text":"café"}\n{"type":"tool_call","name":"test"}');
  const split = bytes.indexOf(Buffer.from("é")) + 1;
  const { reader, response } = responseFor([bytes.subarray(0, split), bytes.subarray(split)]);
  const controller = new AbortController();
  const events = [];
  const c = load(["streamChat", "assistantFetchApi", "limitSetting"], {
    state: { config: { limits: { assistant_fetch_timeout_ms: 0 } } },
    api: { fetchApi: async (_url, options) => {
    assert.equal(options.signal, controller.signal);
    return response;
    } },
  });
  const result = await c.streamChat([], [], event => events.push(event), controller.signal);
  assert.equal(result.text, "café");
  assert.equal(result.toolCalls[0].name, "test");
  assert.equal(events.length, 2);
  assert.ok(reader.cancelled && reader.released);
});

test("stream errors reject and release the reader", async () => {
  const { reader, response } = responseFor([Buffer.from('{"type":"error","error":"failed"}\n')]);
  const c = load(["streamChat", "assistantFetchApi", "limitSetting"], {
    state: { config: { limits: { assistant_fetch_timeout_ms: 0 } } },
    api: { fetchApi: async () => response },
  });
  await assert.rejects(c.streamChat([], []), /failed/);
  assert.ok(reader.cancelled && reader.released);
});

test("failed chat removes empty typing bubble", async () => {
  const bubble = { removed: false, typing: false,
    remove() { this.removed = true; },
    classList: { add() { bubble.typing = true; }, remove() { bubble.typing = false; } },
  };
  const c = load(["streamAssistantReply"], {
    state: {}, TOOLS: [], appendBubble: () => bubble, buildMessages: () => [], debugLog: () => {},
    streamChat: async () => { throw new Error("network failed"); },
  });
  await assert.rejects(c.streamAssistantReply(), /network failed/);
  assert.ok(bubble.removed);
  assert.equal(bubble.typing, false);
});

test("stopping tests aborts the active request and resolves pending dialogs", () => {
  const state = { testAbortController: new AbortController(), pendingWorkflowTests: true,
    buildTargetResolver(value) { state.target = value; }, pendingCard(value) { state.card = value; } };
  const c = load(["cancelWorkflowTests"], { state, appendNotice: () => {} });
  c.cancelWorkflowTests();
  assert.ok(state.workflowTestCancel && state.testAbortController.signal.aborted);
  assert.equal(state.pendingWorkflowTests, false);
  assert.equal(state.target, "");
  assert.equal(state.card, false);
});

test("cancelled task never executes returned tool calls", async () => {
  let calls = 0;
  const state = { config: {}, workflowTestCancel: false, testAbortController: new AbortController() };
  const c = load(["runTaskAgent"], {
    state, RUNTIME_RULES: "", TOOLS: [],
    refreshRelevantLessons: async () => {}, lessonsContext: () => "",
    streamChat: async (_messages, _tools, _callback, signal) => {
      assert.equal(signal, state.testAbortController.signal);
      state.workflowTestCancel = true;
      return { text: "", toolCalls: [{ name: "remove_node" }] };
    }, executeTool: async () => { calls++; },
  });
  await c.runTaskAgent({ instruction: "test", label: "test" });
  assert.equal(calls, 0);
});

test("cancelled suite restores workflow without asking for another verdict", async () => {
  const state = { sessionKey: "test" };
  let restored = false;
  let judged = false;
  const c = load(["startWorkflowTestSuite"], {
    resolveWorkflowKey: () => "test", flushPendingSessionSwitch: async () => {},
    state, WORKFLOW_MODIFICATIONS: [], setTestBusy: () => {}, selectTab: () => {},
    snapshotGraph: () => ({ original: true }), clearGraph: async () => {}, appendNotice: () => {},
    appendTestRow: () => ({ remove() {} }), askBuildTarget: async () => "test",
    runTaskAgent: async () => { state.workflowTestCancel = true; return {}; },
    showTaskResult: async () => {}, judgeDialog: async () => { judged = true; },
    loadGraphSnapshot: async snapshot => { restored = snapshot.original; }, refreshRelevantLessons: async () => {},
  });
  await c.startWorkflowTestSuite();
  assert.ok(restored);
  assert.equal(judged, false);
  assert.equal(state.workflowTestRunning, false);
});

test("lookups overlap, retain result order, and wait before mutations", async () => {
  const c = load(["isLookupTool", "runToolBatch"]);
  const started = [];
  const recorded = [];
  const finish = {};
  const calls = ["search_docs", "get_node_docs", "add_node", "get_node_details"].map(name => ({ name }));
  const pending = c.runToolBatch(calls, call => {
    started.push(call.name);
    return new Promise(resolve => { finish[call.name] = resolve; });
  }, (call, result) => recorded.push([call.name, result]), () => false);
  assert.deepEqual(started, ["search_docs", "get_node_docs"]);
  finish.get_node_docs("second");
  await new Promise(setImmediate);
  assert.equal(started.length, 2);
  finish.search_docs("first");
  await new Promise(setImmediate);
  assert.deepEqual(recorded, [["search_docs", "first"], ["get_node_docs", "second"]]);
  assert.deepEqual(started, ["search_docs", "get_node_docs", "add_node"]);
  finish.add_node("edit");
  await new Promise(setImmediate);
  assert.equal(started[3], "get_node_details");
  finish.get_node_details("after edit");
  await pending;
  assert.equal(recorded.length, 4);
});

test("lookups are capped at four and failures preserve other results", async () => {
  const c = load(["isLookupTool", "runToolBatch"]);
  const started = [];
  const recorded = [];
  const finish = [];
  const calls = Array.from({ length: 7 }, (_, id) => ({ name: "search_docs", id }));
  const pending = c.runToolBatch(calls, call => {
    started.push(call.id);
    if (call.id === 1) throw new Error("lookup failed");
    return new Promise(resolve => { finish[call.id] = resolve; });
  }, (call, result) => recorded.push([call.id, result]), () => false);
  assert.deepEqual(started, [0, 1, 2, 3]);
  for (const id of [0, 2, 3]) finish[id](id);
  await new Promise(setImmediate);
  assert.deepEqual(started, [0, 1, 2, 3, 4, 5, 6]);
  for (const id of [4, 5, 6]) finish[id](id);
  await pending;
  assert.equal(recorded[1][1].error, "lookup failed");
  assert.deepEqual(recorded.map(([id]) => id), [0, 1, 2, 3, 4, 5, 6]);
});

test("cancellation discards late results and stops queued edits", async () => {
  const c = load(["isLookupTool", "runToolBatch"]);
  let cancelled = false;
  let finish;
  const started = [];
  const records = [];
  const pending = c.runToolBatch([{ name: "search_docs" }, { name: "remove_node" }], call => {
    started.push(call.name);
    return new Promise(resolve => { finish = resolve; });
  }, (...args) => records.push(args), () => cancelled);
  cancelled = true;
  finish("late result");
  await pending;
  assert.deepEqual(started, ["search_docs"]);
  assert.deepEqual(records, []);
});

test("unknown tools form a barrier and mutations are never classified as lookups", () => {
  const c = load(["isLookupTool"]);
  for (const name of ["add_node", "remove_node", "apply_workflow_edits", "set_widget_value",
    "connect_nodes", "disconnect_link", "move_node", "set_prompt", "highlight_nodes",
    "layout_workflow", "remember_lesson", "suggest_node_pack", "future_tool"]) {
    assert.equal(c.isLookupTool(name), false, name);
  }
});

test("node searches reuse indexed text and preserve matching, order, and output isolation", async () => {
  let reads = 0;
  const defs = {
    First: { display_name: "Alpha", category: "Images", get description() { reads++; return "needle " + "x".repeat(200); } },
    Second: { display_name: "Beta", category: "Images", description: "needle two" },
  };
  const c = load(["searchInstalledNodes"], { state: {}, getNodeDefs: async () => defs });
  const first = await c.searchInstalledNodes("NEEDLE", 1);
  assert.equal(first.count, 1);
  assert.equal(first.results[0].type, "First");
  assert.equal(first.results[0].description.length, 160);
  const initialReads = reads;
  first.results[0].display_name = "caller mutation";
  const second = await c.searchInstalledNodes("images");
  assert.deepEqual(Array.from(second.results, item => item.type), ["First", "Second"]);
  assert.equal(second.results[0].display_name, "Alpha");
  assert.equal(reads, initialReads);
  assert.equal((await c.searchInstalledNodes("missing")).count, 0);
  assert.equal((await c.searchInstalledNodes("")).count, 2);
});

test("replacement node definitions rebuild the search cache", async () => {
  let defs = { Old: { description: "old node" } };
  const c = load(["searchInstalledNodes"], { state: {}, getNodeDefs: async () => defs });
  assert.equal((await c.searchInstalledNodes("old")).count, 1);
  defs = { New: { description: "new node" } };
  assert.equal((await c.searchInstalledNodes("old")).count, 0);
  assert.equal((await c.searchInstalledNodes("new")).results[0].type, "New");
});

test("normal agent uses concurrent lookups and keeps tool messages ordered", async () => {
  const finish = {};
  const started = [];
  const state = { messages: [], runId: 0 };
  let turn = 0;
  const calls = ["search_docs", "get_node_docs"].map((name, id) => ({ name, id: String(id), arguments: "{}" }));
  const c = load(["limitSetting", "isLookupTool", "runToolBatch", "runAgent", "runTool", "recordToolResult"], {
    state, setBusy: () => {}, flushInjections: () => false,
    streamAssistantReply: async () => turn++ ? { text: "done", toolCalls: [] } : { text: "", toolCalls: calls },
    toOpenAiToolCall: call => call, safeParse: JSON.parse, safeStringify: JSON.stringify,
    appendNotice: () => ({}), debugLog: () => {}, truncate: text => text,
    executeTool: name => { started.push(name); return new Promise(resolve => { finish[name] = resolve; }); },
    extractInlineActions: () => [], saveHistory: async () => {}, scrollMessages: () => {},
    appendBubble: (_role, text) => { throw new Error(text); },
  });
  const pending = c.runAgent();
  await new Promise(setImmediate);
  assert.deepEqual(started, ["search_docs", "get_node_docs"]);
  finish.get_node_docs("second");
  finish.search_docs("first");
  await pending;
  assert.deepEqual(state.messages.filter(item => item.role === "tool").map(item => [item.tool_call_id, item.content]),
    [["0", '"first"'], ["1", '"second"']]);
});

test("workflow task agent overlaps lookups and records results in call order", async () => {
  const finish = {};
  const started = [];
  let turn = 0;
  let recorded;
  const calls = ["search_docs", "get_node_docs"].map((name, id) => ({ name, id: String(id), arguments: "{}" }));
  const c = load(["isLookupTool", "runToolBatch", "runTaskAgent"], {
    state: { config: {}, workflowTestCancel: false }, RUNTIME_RULES: "", TOOLS: [],
    refreshRelevantLessons: async () => {}, lessonsContext: () => "", toOpenAiToolCall: call => call,
    streamChat: async messages => {
      if (!turn++) return { text: "", toolCalls: calls };
      recorded = messages.filter(item => item.role === "tool");
      return { text: "done", toolCalls: [] };
    },
    safeParse: JSON.parse, safeStringify: JSON.stringify, appendNotice: () => {}, truncate: text => text,
    executeTool: name => { started.push(name); return new Promise(resolve => { finish[name] = resolve; }); },
  });
  const pending = c.runTaskAgent({ instruction: "test", label: "test" });
  await new Promise(setImmediate);
  assert.deepEqual(started, ["search_docs", "get_node_docs"]);
  finish.get_node_docs("second");
  finish.search_docs("first");
  const result = await pending;
  assert.equal(result.text, "done");
  assert.deepEqual(Array.from(recorded, item => [item.tool_call_id, item.content]), [["0", '"first"'], ["1", '"second"']]);
});

test("inline tool actions also use ordered concurrent lookups", async () => {
  const finish = {};
  const started = [];
  const state = { messages: [], runId: 0 };
  let turn = 0;
  const c = load(["limitSetting", "isLookupTool", "runToolBatch", "runAgent", "recordToolResult"], {
    state, setBusy: () => {}, flushInjections: () => false,
    streamAssistantReply: async () => ({ text: turn++ ? "done" : "actions", toolCalls: [] }),
    extractInlineActions: text => text === "actions" ? [{ name: "search_docs", arguments: { query: "a" } },
      { name: "get_node_docs", arguments: { type: "b" } }] : [],
    runTool: (name, args) => { started.push([name, args]); return new Promise(resolve => { finish[name] = resolve; }); },
    safeStringify: JSON.stringify, saveHistory: async () => {}, scrollMessages: () => {},
    appendBubble: (_role, text) => { throw new Error(text); },
  });
  const pending = c.runAgent();
  await new Promise(setImmediate);
  assert.deepEqual(started.map(([name]) => name), ["search_docs", "get_node_docs"]);
  assert.equal(started[0][1].query, "a");
  finish.get_node_docs("second");
  finish.search_docs("first");
  await pending;
  assert.deepEqual(state.messages.filter(item => item.role === "user").map(item => item.content),
    ['Tool result for search_docs: "first"', 'Tool result for get_node_docs: "second"']);
});

test("node creation and batches require an inspected exact type before any mutation", async () => {
  const c = load(["addNode", "applyWorkflowEdits"], { state: {} });
  assert.equal(c.addNode("Uninspected").requires_node_docs, "Uninspected");
  const result = await c.applyWorkflowEdits([{ op: "remove_node", id: 1 }, { op: "add_node", type: "Uninspected" }]);
  assert.equal(result.applied, 0);
  assert.equal(result.requires_node_docs[0], "Uninspected");
});

test("only complete available exact node records unlock creation", async () => {
  const state = {};
  let payload = { verified: true, node: { name: "Exact", available: true, schema_error: "" } };
  const c = load(["getNodeDocs"], { state, api: { fetchApi: async () => ({ ok: true, json: async () => payload }) } });
  await c.getNodeDocs("Exact");
  assert.ok(state.verifiedNodeTypes.has("Exact"));
  payload = { verified: false, node: { name: "Exact", available: true, schema_error: "missing dependency" } };
  await c.getNodeDocs("Exact");
  assert.equal(state.verifiedNodeTypes.has("Exact"), false);
  payload = { verified: true, node: { name: "Other", available: true } };
  await c.getNodeDocs("Exact");
  assert.equal(state.verifiedNodeTypes.has("Exact"), false);
});

test("node records remain complete in model tool results", () => {
  const state = { messages: [] };
  const c = load(["recordToolResult", "safeStringify", "truncate"], { state });
  const result = { node: { description: "x".repeat(25000), input: { required: { image: ["IMAGE"] } } } };
  c.recordToolResult("get_node_docs", result, "call-1");
  assert.deepEqual(JSON.parse(state.messages[0].content), result);
});

test("rejected connections do not report a successful edit", () => {
  const sourceNode = { outputs: [{}], connect: () => null };
  const targetNode = { inputs: [{}] };
  const state = {};
  const c = load(["connectNodes"], { state, app: { graph: { getNodeById: id => id === 1 ? sourceNode : targetNode } },
    withGraphChange: fn => fn() });
  assert.match(c.connectNodes(1, 0, 2, 0).error, /Connection rejected/);
  assert.equal(state.runMutatedGraph, undefined);
});


test("run capture freezes workflow and fragment before queue response", async () => {
  let finish;
  let owner = "a";
  const state = { sessionKey: "a", runNodeOwner: "a", pendingRuns: new Map(), runTaskText: "task A" };
  const fragment = { nodes: [{ id: 1, type: "A" }] };
  const api = { queuePrompt: () => new Promise(resolve => { finish = resolve; }) };
  const c = load(["captureRunContext", "trackRunSubmit", "installRunCapture"], {
    state, api, resolveWorkflowKey: () => owner, experienceFragment: () => fragment,
    experienceModels: () => [], lastUserTask: () => "fallback",
  });
  c.installRunCapture();
  const pending = api.queuePrompt();
  owner = "b";
  state.sessionKey = "b";
  fragment.nodes[0].type = "B";
  finish({ prompt_id: "p" });
  await pending;
  const entry = state.pendingRuns.get("p");
  assert.equal(entry.workflow_key, "a");
  assert.equal(entry.fragment.nodes[0].type, "A");
  assert.equal(c.captureRunContext(), null, "old node IDs cannot belong to the new workflow");
});

test("background run approval never appears in another workflow chat", async () => {
  const c = load(["askRunApproval"], {
    state: { sessionKey: "b" }, resolveWorkflowKey: () => "b",
    appendChatCard: () => assert.fail("Wrong workflow must not receive approval card"),
  });
  await c.askRunApproval(1, { workflow_key: "a" });
});

test("coverage summary is compact and expanding it exposes every missing node", async () => {
  const status = { textContent: "" };
  const content = { textContent: "" };
  const details = { open: false, querySelector: () => content };
  const missing = Array.from({ length: 631 }, (_, i) => `Node${i}`);
  const c = load(["refreshKbStatus"], {
    state: { dom: { root: { querySelector: selector => selector === "#ccb-kb-status" ? status :
      selector === "#ccb-kb-coverage" ? details : null }, panel: { classList: { contains: () => false } } } },
    api: { fetchApi: async () => ({ json: async () => ({ status: {
      coverage: { registry_available: true, registered: 4589, indexed: 3958, missing },
    } }) }) },
    clearTimeout: () => {}, setTimeout: () => {},
  });
  await c.refreshKbStatus();
  assert.match(status.textContent, /missing: 631/);
  assert.equal(status.textContent.includes("Node630"), false);
  details.open = true;
  details.ontoggle();
  for (const name of missing) assert.ok(content.textContent.includes(name));
});

test("provider choices and URL defaults contain Strata only once", () => {
  const markup = source.slice(source.indexOf('function buildUi()'), source.indexOf('function wireUi()'));
  const provider = markup.match(/<select id="ccb-provider">([\s\S]*?)<\/select>/)[1];
  const values = [...provider.matchAll(/value="([^"]+)"/g)].map(match => match[1]);
  assert.equal(values.length, new Set(values).size);
  assert.equal(values.filter(value => value === "strata").length, 1);
});

test("modern empty store never borrows legacy workflow and modern drafts wait for an ID", () => {
  const app = { extensionManager: { workflow: { activeWorkflow: null } },
    workflowManager: { activeWorkflow: { activeState: { id: "stale" } } } };
  const c = load(["resolveWorkflowKey"], { app, _workflowSessionKeys: new WeakMap(), _workflowSessionSeq: 0, _workflowSessionNonce: "test" });
  assert.equal(c.resolveWorkflowKey(), null);
  app.extensionManager.workflow.activeWorkflow = { isLoaded: true, isTemporary: true };
  assert.equal(c.resolveWorkflowKey(), null);
  app.extensionManager.workflow.activeWorkflow.activeState = { id: "ready" };
  assert.equal(c.resolveWorkflowKey(), "id:ready");
});

test("a reused workflow object cannot authorize a request for its previous ID", () => {
  const workflow = { activeState: { id: "a" } };
  const state = { runWorkflowKey: "id:a" };
  const c = load(["resolveWorkflowKey", "assertRunWorkflow"], {
    state, app: { extensionManager: { workflow: { activeWorkflow: workflow } } },
    _workflowSessionKeys: new WeakMap(), _workflowSessionSeq: 0, _workflowSessionNonce: "test",
  });
  c.assertRunWorkflow();
  workflow.activeState = { id: "b" };
  assert.throws(() => c.assertRunWorkflow(), /original workflow/);
});

test("failed or malformed history reads cannot erase saved history and can retry", async () => {
  for (const response of [{ ok: false, status: 500 }, { json: async () => ({ error: "failed" }) }]) {
    const state = { sessionKey: "a", messages: [] };
    const fetchApi = async (_url, options) => {
      assert.notEqual(options?.method, "POST", "failed read must never be persisted");
      return response;
    };
    const api = { fetchApi };
    const c = load(["loadHistory", "saveHistory"], { state, api,
      appendNotice() {}, renderHistory() {}, updateContextStatus() {} });
    await c.loadHistory();
    assert.equal(state.historyLoadFailed, true);
    await c.saveHistory();
    api.fetchApi = async () => ({ json: async () => ({ history: [{ content: "saved A" }] }) });
    await c.loadHistory();
    assert.equal(state.historyLoadFailed, false);
    assert.equal(state.messages[0].content, "saved A");
  }
});

test("a startup history read cannot replace a transcript after a request starts", async () => {
  let finish;
  const state = { sessionKey: "a", messages: [] };
  const c = load(["loadHistory"], {
    state, api: { fetchApi: () => new Promise(resolve => { finish = resolve; }) },
    renderHistory: () => assert.fail("stale startup data must not render"), updateContextStatus() {}, appendNotice() {},
  });
  const loading = c.loadHistory();
  await Promise.resolve();
  state.requestActive = true;
  state.messages = [{ role: "user", content: "active request" }];
  finish({ json: async () => ({ history: [{ role: "assistant", content: "stale startup history" }] }) });
  await loading;
  assert.equal(state.messages[0].content, "active request");
  assert.equal(state.historyLoading, false);
});

test("failed writes remain available when switching A-B-A", async () => {
  const state = { sessionKey: "a", messages: [{ role: "user", content: "unsaved A" }], attachments: [],
    dom: { input: { value: "", style: {} } } };
  const c = load(["loadHistory", "saveHistory", "switchSession"], {
    state,
    api: { fetchApi: async (url, options) => options?.method === "POST"
      ? { ok: false, status: 500 }
      : { ok: true, json: async () => ({ history: [] }) } },
    debugLog() {}, hashKey: x => x, renderHistory() {}, renderAttachments() {}, updateContextStatus() {},
    appendNotice() {}, scrollMessages() {},
  });
  await c.saveHistory();
  assert.ok(state.unsavedHistories.has("a"));
  await c.switchSession("b");
  state.messages = [{ role: "user", content: "B" }];
  await c.switchSession("a");
  assert.equal(state.messages[0].content, "unsaved A");
  assert.equal(state.historyLoadFailed, false);
});

test("closing all tabs clears the chat and rejects a late history response", async () => {
  let finish;
  const state = { sessionKey: "a", messages: [], attachments: [], dom: { input: { value: "", style: {} } } };
  const c = load(["loadHistory", "saveHistory", "switchSession"], {
    state, api: { fetchApi: () => new Promise(resolve => { finish = resolve; }) },
    debugLog() {}, hashKey: x => x, renderHistory() {}, renderAttachments() {}, updateContextStatus() {},
    appendNotice() {}, scrollMessages() {},
  });
  const reading = c.loadHistory();
  await Promise.resolve();
  await c.switchSession(null);
  finish({ json: async () => ({ history: [{ content: "old A" }] }) });
  await reading;
  assert.equal(state.sessionKey, null);
  assert.equal(state.messages.length, 0);
  assert.equal(state.historyLoading, false);
});

test("delayed attachments stay with the source draft across a tab switch", async () => {
  let finish;
  let renders = 0;
  const original = [];
  const state = { sessionKey: "a", attachments: original };
  const c = load(["addAttachment"], { state, urlToDataUrl: () => new Promise(resolve => { finish = resolve; }),
    renderAttachments: () => renders++, appendNotice() {} });
  const attaching = c.addAttachment({ url: "image", name: "A image" });
  state.sessionKey = "b";
  state.attachments = [];
  finish("encoded-image");
  await attaching;
  assert.equal(original[0].dataUrl, "encoded-image");
  assert.equal(state.attachments.length, 0);
  assert.equal(renders, 0);
});

test("snapshot restoration targets the existing workflow instead of creating a tab", async () => {
  const workflow = { activeState: { id: "a" } };
  let args;
  const c = load(["loadGraphSnapshot"], { state: { runWorkflowKey: "a" }, assertRunWorkflow() {},
    app: { extensionManager: { workflow: { activeWorkflow: workflow } }, loadGraphData: async (...values) => { args = values; } } });
  const snapshot = { nodes: [] };
  await c.loadGraphSnapshot(snapshot);
  assert.equal(args[0], snapshot);
  assert.equal(args[3], workflow);
});

test("test suite switches chat owner before it can clear a different workflow", async () => {
  const switched = [];
  const c = load(["startWorkflowTestSuite"], { state: { sessionKey: "a" }, resolveWorkflowKey: () => "b",
    switchSession: async key => switched.push(key), clearGraph: () => assert.fail("must not clear yet") });
  await c.startWorkflowTestSuite();
  assert.deepEqual(switched, ["b"]);
});
