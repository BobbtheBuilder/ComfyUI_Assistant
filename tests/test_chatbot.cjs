// Exercise the actual browser functions with isolated API/DOM doubles.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/chatbot.js"), "utf8");

function load(names, overrides = {}) {
  const context = vm.createContext({ TextDecoder, AbortController, DOMException, ...overrides });
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

test("shared stream parser handles split UTF-8 and final line without newline", async () => {
  const bytes = Buffer.from('{"type":"text","text":"café"}\n{"type":"tool_call","name":"test"}');
  const split = bytes.indexOf(Buffer.from("é")) + 1;
  const { reader, response } = responseFor([bytes.subarray(0, split), bytes.subarray(split)]);
  const controller = new AbortController();
  const events = [];
  const c = load(["streamChat"], { api: { fetchApi: async (_url, options) => {
    assert.equal(options.signal, controller.signal);
    return response;
  } } });
  const result = await c.streamChat([], [], event => events.push(event), controller.signal);
  assert.equal(result.text, "café");
  assert.equal(result.toolCalls[0].name, "test");
  assert.equal(events.length, 2);
  assert.ok(reader.cancelled && reader.released);
});

test("stream errors reject and release the reader", async () => {
  const { reader, response } = responseFor([Buffer.from('{"type":"error","error":"failed"}\n')]);
  const c = load(["streamChat"], { api: { fetchApi: async () => response } });
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
    state, RUNTIME_RULES: "", MAX_AGENT_TURNS: 2, TOOLS: [],
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
  const state = {};
  let restored = false;
  let judged = false;
  const c = load(["startWorkflowTestSuite"], {
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
    "layout_workflow", "remember_lesson", "install_custom_node", "future_tool"]) {
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
  const c = load(["isLookupTool", "runToolBatch", "runAgent", "runTool", "recordToolResult"], {
    state, MAX_AGENT_TURNS: 3, setBusy: () => {}, flushInjections: () => false,
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
    state: { config: {}, workflowTestCancel: false }, RUNTIME_RULES: "", MAX_AGENT_TURNS: 3, TOOLS: [],
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
  const c = load(["isLookupTool", "runToolBatch", "runAgent", "recordToolResult"], {
    state, MAX_AGENT_TURNS: 3, setBusy: () => {}, flushInjections: () => false,
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
