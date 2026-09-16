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

test("planner finalizes after its last lookup instead of reporting invalid JSON", async () => {
  let requests = 0;
  let executed = 0;
  const plan = { workflow_plan: [{ role: "image", node: "LoadImage" }] };
  const c = load(["requestPlan", "parsePlanJson"], {
    state: {}, PLANNER_SYSTEM: "plan", PLANNER_TOOLS: [{ function: { name: "get_node_docs" } }],
    MAX_PLAN_CALLS: 2, MAX_PLAN_TOOL_CALLS: 5, plannerPrompt: () => "request",
    toOpenAiToolCall: (call) => call, safeParse: JSON.parse, safeStringify: JSON.stringify, debugLog: () => {},
    executeTool: async () => { executed++; return { schema: true }; },
    streamChat: async (messages, tools) => {
      requests++;
      if (requests <= 2) return { text: "", toolCalls: [{ id: String(requests), name: "get_node_docs", arguments: "{}" }] };
      assert.equal(tools.length, 0);
      assert.equal(messages.filter((m) => m.role === "tool").length, 2);
      return { text: JSON.stringify(plan), toolCalls: [] };
    },
  });
  assert.equal(JSON.stringify(await c.requestPlan("build", {})), JSON.stringify(plan));
  assert.equal(requests, 3);
  assert.equal(executed, 2);
});

test("planner rejects mutation tools and honours cancellation after a response", async () => {
  const state = {};
  let executed = 0;
  let requests = 0;
  const c = load(["requestPlan", "parsePlanJson"], {
    state, PLANNER_SYSTEM: "plan", PLANNER_TOOLS: [{ function: { name: "get_node_docs" } }],
    MAX_PLAN_CALLS: 1, MAX_PLAN_TOOL_CALLS: 5, plannerPrompt: () => "request",
    toOpenAiToolCall: (call) => call, safeParse: JSON.parse, safeStringify: JSON.stringify, debugLog: () => {},
    executeTool: async () => executed++,
    streamChat: async (messages, tools) => {
      requests++;
      if (requests === 1) return { text: "", toolCalls: [{ id: "1", name: "remove_node", arguments: "{}" }] };
      assert.match(messages.find((m) => m.role === "tool").content, /not available during planning/);
      state.cancelled = true;
      return { text: '{"workflow_plan":[{"node":"X"}]}', toolCalls: [] };
    },
  });
  assert.equal(await c.requestPlan("build", {}), null);
  assert.equal(executed, 0);
});

test("packet enrichment and tool results retain all nodes beyond former caps", async () => {
  const defs = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`Node${i}`, { input: {}, output: [] }]));
  const state = { verifiedNodeTypes: new Set(), messages: [] };
  const c = load(["enrichPacket", "recordToolResult", "safeStringify", "truncate"], {
    state, getNodeDefs: async () => defs, nodeSchemaDigest: (type) => ({ type }), debugLog: () => {},
  });
  const packet = await c.enrichPacket({ resolved: true, compatible_nodes: Object.keys(defs).map((node) => ({ node })) });
  assert.equal(Object.keys(packet.schemas).length, 30);
  const result = { nodes: Array.from({ length: 1000 }, (_, id) => ({ id, title: "Long workflow node name" })) };
  c.recordToolResult("get_workflow_summary", result, "call");
  assert.equal(JSON.parse(state.messages[0].content).nodes.length, 1000);
});

test("preparing context does not silently truncate old tool results", async () => {
  const content = JSON.stringify({ nodes: Array.from({ length: 100 }, (_, id) => ({ id, description: "x".repeat(100) })) });
  const state = { messages: [{ role: "tool", content }, ...Array.from({ length: 8 }, () => ({ role: "user", content: "next" }))], config: { context: { auto_compact: false } } };
  const c = load(["prepareContext"], { state });
  await c.prepareContext();
  assert.equal(state.messages[0].content, content);
});

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
    state, RUNTIME_RULES: "", NO_PROGRESS_LIMIT: 3, TOOLS: [],
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
  assert.equal(first.results[0].description, "needle " + "x".repeat(200));
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
    state, setBusy: () => {}, flushInjections: () => false, logEvent: () => {}, NO_PROGRESS_LIMIT: 3,
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
    state: { config: {}, workflowTestCancel: false }, RUNTIME_RULES: "", NO_PROGRESS_LIMIT: 3, TOOLS: [],
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
    state, setBusy: () => {}, flushInjections: () => false, logEvent: () => {}, NO_PROGRESS_LIMIT: 3,
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
  const c = load(["isWidgetSpec", "specTypeOf", "nodeSchemaDigest", "getNodeDocs"], { state, api: { fetchApi: async () => ({ ok: true, json: async () => payload }) } });
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

test("request modes route task-shaped input to the controller", () => {
  const c = load(["resolveRequestMode"]);
  assert.equal(c.resolveRequestMode("build me a basic klein 9b workflow"), "build");
  assert.equal(c.resolveRequestMode("make a text to image workflow"), "build");
  assert.equal(c.resolveRequestMode("let's do a klein 9b workflow"), "build");
  assert.equal(c.resolveRequestMode("finish this workflow"), "build");
  assert.equal(c.resolveRequestMode("why did my workflow fail"), "diagnose");
  assert.equal(c.resolveRequestMode("what nodes do I need for klein 9b"), "discovery");
  assert.equal(c.resolveRequestMode("add a KSampler node"), "edit");
  assert.equal(c.resolveRequestMode("change the sampler steps"), "edit");
  assert.equal(c.resolveRequestMode("do I need a model for this"), "chat");
  assert.equal(c.resolveRequestMode("hello there"), "chat");
});

test("task detection separates t2i and i2i", () => {
  const c = load(["detectTask"]);
  assert.equal(c.detectTask("build a klein 9b text to image workflow"), "t2i");
  assert.equal(c.detectTask("make an image edit workflow for klein"), "i2i");
  assert.equal(c.detectTask("build a sam workflow"), "");
});

test("plan JSON is parsed from prose or fences and rejected when malformed", () => {
  const c = load(["parsePlanJson"]);
  const plan = { workflow_plan: [{ role: "loader", node: "UNETLoader" }], connections: [] };
  const expected = JSON.stringify(plan);
  assert.equal(JSON.stringify(c.parsePlanJson("here you go:\n```json\n" + expected + "\n```")), expected);
  assert.equal(JSON.stringify(c.parsePlanJson(expected)), expected);
  assert.equal(c.parsePlanJson("no json here"), null);
  assert.equal(c.parsePlanJson('{"workflow_plan": []}'), null);
});

test("plan usage rejects excluded, unknown, and unresolved-role nodes", () => {
  const c = load(["validatePlanUsage"]);
  const packet = { compatible_nodes: [{ node: "KSampler" }, { node: "UNETLoader" }],
    exclusions: [{ node: "DualCLIPLoader", reason: "encoder mismatch" }] };
  assert.equal(c.validatePlanUsage({ workflow_plan: [{ role: "s", node: "KSampler" }], connections: [] }, packet).length, 0);
  assert.match(c.validatePlanUsage({ workflow_plan: [{ role: "x", node: "DualCLIPLoader" }] }, packet).join(" "), /incompatible/);
  assert.match(c.validatePlanUsage({ workflow_plan: [{ role: "x", node: "Mystery" }] }, packet).join(" "), /not in allowed_nodes/);
  const bad = c.validatePlanUsage({ workflow_plan: [{ role: "s", node: "KSampler" }],
    connections: [{ from_role: "s", to_role: "missing" }] }, packet);
  assert.match(bad.join(" "), /unknown role/);
});

test("slot lookup prefers names then unconnected type matches", () => {
  const c = load(["findSlotIndex"]);
  const inputs = [{ name: "model", type: "MODEL", link: 3 }, { name: "clip", type: "CLIP", link: null }];
  assert.equal(c.findSlotIndex(inputs, "clip", null, true), 1);
  assert.equal(c.findSlotIndex(inputs, null, "CLIP", true), 1);
  assert.equal(c.findSlotIndex(inputs, null, "MODEL", true), 0);
  assert.equal(c.findSlotIndex(inputs, "nope", null, true), -1);
  assert.equal(c.findSlotIndex(undefined, "x", null, true), -1);
});

test("diagnosis extracts and bounds the failing node's subgraph", () => {
  const nodes = {
    1: { id: 1, type: "LoadImage", title: "Load" },
    2: { id: 2, type: "KSampler", title: "Sampler" },
    3: { id: 3, type: "VAEDecode", title: "Decode" },
  };
  const links = {
    10: { id: 10, origin_id: 1, origin_slot: 0, target_id: 2, target_slot: 0, type: "IMAGE" },
    11: { id: 11, origin_id: 2, origin_slot: 0, target_id: 3, target_slot: 0, type: "LATENT" },
  };
  const c = load(["subgraphAround"], { app: { graph: { getNodeById: id => nodes[id], links } } });
  const result = c.subgraphAround(2, 4, 20);
  assert.equal(result.failed.type, "KSampler");
  assert.equal(result.nodes.length, 3);
  assert.equal(result.links.length, 2);
  const bounded = c.subgraphAround(2, 1, 2);
  assert.ok(bounded.nodes.length <= 2);
  assert.equal(c.subgraphAround(99, 4, 20).failed, null);
});

test("diagnosis node docs retain the complete description", () => {
  const c = load(["compactNodeDoc"]);
  const doc = c.compactNodeDoc({ node: { name: "KSampler", description: "d".repeat(900),
    input: { required: { model: ["MODEL"] }, optional: { denoise: ["FLOAT"] } },
    output_name: ["LATENT"], available: true, schema_error: "" } });
  assert.equal(doc.type, "KSampler");
  assert.equal(doc.description, "d".repeat(900));
  assert.equal(JSON.stringify(doc.inputs), JSON.stringify(["model", "denoise"]));
  assert.equal(JSON.stringify(doc.outputs), JSON.stringify(["LATENT"]));
  assert.equal(doc.available, true);
});



test("node schema digest separates widgets, sockets, and outputs", () => {
  const c = load(["isWidgetSpec", "specTypeOf", "nodeSchemaDigest"]);
  assert.equal(c.isWidgetSpec(["INT", {}]), true);
  assert.equal(c.isWidgetSpec([["a", "b"], {}]), true);
  assert.equal(c.isWidgetSpec(["MODEL"]), false);
  assert.equal(c.specTypeOf([["a", "b"], {}]), "COMBO");
  const digest = c.nodeSchemaDigest("KSampler", {
    display_name: "KSampler", category: "sampling", description: "d",
    input: { required: { model: ["MODEL"], seed: ["INT", { default: 0 }], sampler_name: [["euler"], {}] }, optional: { denoise: ["FLOAT"] } },
    output: ["LATENT"], output_name: ["LATENT"],
  });
  assert.equal(JSON.stringify(digest.widgets), JSON.stringify(["seed", "sampler_name", "denoise"]));
  assert.equal(digest.required.find((item) => item.name === "model").widget, false);
  assert.equal(digest.outputs[0].type, "LATENT");
});

test("packet enrichment attaches live schemas and verifies from retrieval", async () => {
  const defs = {
    UNETLoader: { display_name: "Load Diffusion Model", category: "loaders", description: "d",
      input: { required: { unet_name: [["a.safetensors"], {}] } }, output: ["MODEL"], output_name: ["MODEL"] },
    KSampler: { input: { required: { model: ["MODEL"], seed: ["INT", {}] } }, output: ["LATENT"], output_name: ["LATENT"] },
  };
  const state = { verifiedNodeTypes: new Set() };
  const c = load(["isWidgetSpec", "specTypeOf", "nodeSchemaDigest", "enrichPacket"], {
    state, getNodeDefs: async () => defs, debugLog: () => {}, MAX_PACKET_SCHEMAS: 10,
  });
  const packet = { resolved: true, compatible_nodes: [
    { node: "UNETLoader", state: "GENERIC", roles: ["model_loader"] },
    { node: "KSampler", state: "GENERIC", roles: ["sampler"] },
  ], known_good_patterns: [{ nodes: ["UNETLoader"] }] };
  const enriched = await c.enrichPacket(packet);
  const loader = enriched.compatible_nodes.find((entry) => entry.node === "UNETLoader");
  assert.equal(JSON.stringify(loader.schema.widgets), JSON.stringify(["unet_name"]));
  assert.equal(loader.schema.outputs[0].type, "MODEL");
  const sampler = enriched.compatible_nodes.find((entry) => entry.node === "KSampler");
  assert.equal(JSON.stringify(sampler.schema.widgets), JSON.stringify(["seed"]));
  assert.ok(state.verifiedNodeTypes.has("UNETLoader"));
  assert.ok(state.verifiedNodeTypes.has("KSampler"));
  assert.equal(Object.keys(enriched.schemas).length, 2);
});

test("preflight rejects unconnected inputs, bad widgets, absent assets, duplicate roles", () => {
  const state = { nodeSchemas: {
    UNETLoader: { outputs: [{ name: "MODEL", type: "MODEL" }], required: [{ name: "unet_name", type: "COMBO", widget: true }], optional: [], widgets: ["unet_name"] },
    KSampler: { outputs: [{ name: "LATENT", type: "LATENT" }], required: [{ name: "model", type: "MODEL", widget: false }, { name: "seed", type: "INT", widget: true }], optional: [], widgets: ["seed"] },
  } };
  const c = load(["findSlotIndex", "slotTypesCompatible", "preflightPlan"], { state, ASSET_WIDGET_RE: /(unet_name|name)$/i, ASSET_EXT_RE: /\.(safetensors)$/i });
  const good = {
    workflow_plan: [{ role: "loader", node: "UNETLoader" }, { role: "sampler", node: "KSampler" }],
    connections: [{ from_role: "loader", from_output: "MODEL", to_role: "sampler", to_input: "model" }],
    widgets: [{ role: "loader", name: "unet_name", value: "x.safetensors" }, { role: "sampler", name: "seed", value: 1 }],
  };
  const packet = { installed_assets: [{ filename: "x.safetensors" }] };
  assert.equal(JSON.stringify(c.preflightPlan(good, packet)), "[]");
  assert.match(c.preflightPlan({ ...good, connections: [] }, packet).join(" "), /required input "model" is not connected/);
  assert.match(c.preflightPlan({ ...good, widgets: [{ role: "sampler", name: "nope", value: 1 }] }, packet).join(" "), /no widget "nope"/);
  assert.match(c.preflightPlan(good, { installed_assets: [{ filename: "other.safetensors" }] }).join(" "), /is not installed/);
  assert.match(c.preflightPlan({ workflow_plan: [{ role: "a", node: "UNETLoader" }, { role: "a", node: "KSampler" }], connections: [], widgets: [] }, packet).join(" "), /duplicate role/);
  assert.match(c.preflightPlan({ workflow_plan: [{ role: "a", node: "Unknown" }], connections: [], widgets: [] }, packet).join(" "), /schema not available/);
});

test("failed build rolls back created nodes", () => {
  const added = [];
  const removed = [];
  const nodes = new Map();
  let nextId = 1;
  const graph = {
    _nodes: [], links: {},
    add(node) { added.push(node.id); this._nodes.push(node); nodes.set(node.id, node); },
    remove(node) { removed.push(node.id); this._nodes = this._nodes.filter((item) => item !== node); nodes.delete(node.id); },
    getNodeById(id) { return nodes.get(id) || null; },
    beforeChange() {}, afterChange() {}, setDirtyCanvas() {},
  };
  const state = { verifiedNodeTypes: new Set(["Good", "Bad"]), runAddedNodeIds: [], runMutatedGraph: false, nodeSchemas: {} };
  const c = load(["applyPlan", "withGraphChange", "findFreePosition", "nodePositionBlocked", "nodeRectAt", "rectsOverlap", "addNode", "discardPlanNodes"], {
    app: { graph, workflowManager: null }, state,
    LiteGraph: { createNode: (type) => (type === "Bad" ? null : { id: nextId++, type, pos: [0, 0], size: [200, 100], inputs: [], outputs: [] }) },
    connectNodes: () => ({ ok: true }), setWidgetValue: () => ({ ok: true }),
  });
  const result = c.applyPlan({ workflow_plan: [{ role: "g", node: "Good" }, { role: "b", node: "Bad" }], connections: [], widgets: [] });
  assert.equal(result.ok, false);
  assert.equal(result.created.length, 0);
  assert.equal(added.length, 1);
  assert.equal(removed.length, 1);
});

test("agent stops when the same tool call repeats without progress", async () => {
  const state = { messages: [], runId: 0 };
  const bubbles = [];
  const calls = [{ name: "search_docs", id: "0", arguments: "{}" }];
  const c = load(["isLookupTool", "runToolBatch", "runAgent", "runTool", "recordToolResult"], {
    state, setBusy: () => {}, flushInjections: () => false, logEvent: () => {}, NO_PROGRESS_LIMIT: 3,
    streamAssistantReply: async () => ({ text: "", toolCalls: calls }),
    toOpenAiToolCall: (call) => call, safeParse: JSON.parse, safeStringify: JSON.stringify,
    appendNotice: () => {}, debugLog: () => {}, truncate: (text) => text,
    appendBubble: (_role, text) => { bubbles.push(text); },
    executeTool: async () => ({ ok: true }), saveHistory: async () => {}, scrollMessages: () => {},
    extractInlineActions: () => [],
  });
  await c.runAgent();
  assert.match(bubbles.join(" "), /repeated the same tool call/);
});

test("workflow switch while busy is deferred, not aborted", async () => {
  const state = { busy: true, sessionKey: "path:a", messages: [], attachments: [] };
  const c = load(["switchSession", "flushPendingSessionSwitch"], {
    state, resolveWorkflowKey: () => "path:b", renderHistory: () => {}, logEvent: () => {}, hashKey: (value) => value, saveHistory: async () => {},
    renderAttachments: () => {}, loadHistory: async () => {}, appendNotice: () => {}, scrollMessages: () => {},
  });
  await c.switchSession("path:b");
  assert.equal(state.pendingSessionKey, "path:b");
  assert.equal(state.sessionKey, "path:a");
  state.busy = false;
  await c.flushPendingSessionSwitch();
  assert.equal(state.sessionKey, "path:b");
  assert.equal(state.pendingSessionKey, null);
});

test("temporary workflow key stays stable when activeState id appears", () => {
  const workflow = { path: "", isTemporary: true, activeState: {} };
  const c = load(["resolveWorkflowKey"], {
    app: { workflowManager: { activeWorkflow: workflow } },
    _workflowSessionKeys: new WeakMap(), _workflowSessionSeq: 0, _workflowSessionNonce: "page-one",
  });
  const first = c.resolveWorkflowKey();
  workflow.activeState.id = "abc";
  assert.equal(c.resolveWorkflowKey(), first);
  assert.match(first, /^tmp:/);
  workflow.path = "saved.json";
  workflow.isTemporary = false;
  assert.equal(c.resolveWorkflowKey(), first, "saving must not change an active chat's owner");
});

test("new temporary tabs and new page instances never reuse deleted tab histories", () => {
  const app = { workflowManager: { activeWorkflow: { isTemporary: true } } };
  const make = (nonce) => load(["resolveWorkflowKey"], {
    app, _workflowSessionKeys: new WeakMap(), _workflowSessionSeq: 0, _workflowSessionNonce: nonce,
  });
  const firstPage = make("one");
  const oldKey = firstPage.resolveWorkflowKey();
  app.workflowManager.activeWorkflow = { isTemporary: true };
  assert.notEqual(firstPage.resolveWorkflowKey(), oldKey);
  assert.notEqual(make("two").resolveWorkflowKey(), oldKey);
});

test("returning to the original tab clears a deferred switch without aborting", async () => {
  const controller = new AbortController();
  const state = { busy: true, sessionKey: "a", pendingSessionKey: "b", abortController: controller };
  const switched = [];
  const c = load(["onWorkflowMaybeChanged", "flushPendingSessionSwitch"], {
    state, resolveWorkflowKey: () => "a", switchSession: async (key) => switched.push(key),
  });
  c.onWorkflowMaybeChanged();
  assert.equal(state.pendingSessionKey, null);
  state.busy = false;
  await c.flushPendingSessionSwitch();
  assert.deepEqual(switched, ["a"]);
  assert.equal(controller.signal.aborted, false);
});

test("late history responses cannot replace the active workflow conversation", async () => {
  const pending = new Map();
  const state = { sessionKey: "a", messages: [] };
  let rendered = 0;
  const c = load(["loadHistory"], {
    state, renderHistory: () => rendered++, updateContextStatus: () => {},
    api: { fetchApi: (url) => new Promise((resolve) => pending.set(url.split("=")[1], resolve)) },
  });
  const a = c.loadHistory();
  state.sessionKey = "b";
  const b = c.loadHistory();
  await Promise.resolve();
  pending.get("b")({ json: async () => ({ history: [{ role: "user", content: "B" }] }) });
  await b;
  pending.get("a")({ json: async () => ({ history: [{ role: "user", content: "A" }] }) });
  await a;
  assert.equal(state.messages[0].content, "B");
  assert.equal(state.historyLoading, false);
  assert.equal(rendered, 1);
});

test("history writes are ordered snapshots so clearing cannot resurrect old messages", async () => {
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
  assert.equal(state.historyWrites.size, 0);
});

test("switching sessions captures the old owner before a slow save", async () => {
  const state = { sessionKey: "a", messages: [{ content: "A" }], attachments: [] };
  const saves = [];
  let finishSave;
  const c = load(["switchSession"], {
    state, logEvent: () => {}, hashKey: (key) => key, renderAttachments: () => {}, renderHistory: () => {},
    appendNotice: () => {}, scrollMessages: () => {},
    saveHistory: (key) => { saves.push([key, state.messages.map((m) => m.content)]); return new Promise((r) => { finishSave = r; }); },
    loadHistory: async () => { state.historyLoading = true; },
  });
  const first = c.switchSession("b");
  const second = c.switchSession("c");
  finishSave();
  await Promise.all([first, second]);
  assert.deepEqual(saves, [["a", ["A"]]]);
  assert.equal(state.sessionKey, "c");
});

test("canvas ownership blocks reads, mutations and snapshot restores without aborting the LLM", async () => {
  const state = { runWorkflowKey: "a", abortController: new AbortController() };
  let changes = 0;
  const c = load(["assertRunWorkflow", "executeTool", "withGraphChange", "loadGraphSnapshot"], {
    state, resolveWorkflowKey: () => "b",
    app: { graph: {}, loadGraphData: async () => changes++ },
    searchDocs: async () => ({ results: ["independent lookup"] }),
  });
  await assert.rejects(c.executeTool("get_workflow_summary", {}), /active canvas changed/);
  assert.throws(() => c.withGraphChange(() => changes++), /active canvas changed/);
  await assert.rejects(c.loadGraphSnapshot({ nodes: [] }), /active canvas changed/);
  assert.equal((await c.executeTool("search_docs", {})).results.length, 1);
  assert.equal(changes, 0);
  assert.equal(state.abortController.signal.aborted, false);
});

test("manual content edits block stale writes; canvas movement does not", () => {
  const node = { id: 1, type: "Test", pos: [0, 0], widgets: [{ value: "original" }], inputs: [], outputs: [] };
  const graph = { _nodes: [node], beforeChange() {}, afterChange() {}, setDirtyCanvas() {} };
  const state = {};
  const c = load(["graphContentStamp", "withGraphChange", "executeTool"], { state, app: { graph }, workflowSummary: () => ({}) });
  state.runGraphSnapshot = c.graphContentStamp();
  node.pos = [900, 500];
  c.withGraphChange(() => { node.widgets[0].value = "assistant"; });
  assert.equal(state.runGraphSnapshot, c.graphContentStamp());
  node.widgets[0].value = "user";
  assert.throws(() => c.withGraphChange(() => { node.widgets[0].value = "stale"; }), /content changed/);
  assert.equal(node.widgets[0].value, "user");
  c.executeTool("get_workflow_summary", {});
  c.withGraphChange(() => { node.widgets[0].value = "fresh"; });
  assert.equal(node.widgets[0].value, "fresh");
});

test("inner agent completion keeps the session locked until the whole request finishes", () => {
  let switches = 0;
  const state = { busy: true, requestActive: true, runWorkflowKey: "a", dom: { busyIndicator: { hidden: true }, send: { classList: { toggle() {} } } } };
  const c = load(["setBusy"], { state, flushPendingSessionSwitch: () => switches++ });
  c.setBusy(true);
  assert.equal(state.dom.busyIndicator.hidden, false);
  c.setBusy(false);
  assert.equal(state.dom.busyIndicator.hidden, false);
  assert.equal(state.busy, true);
  assert.equal(state.runWorkflowKey, "a");
  assert.equal(switches, 0);
  state.requestActive = false;
  c.setBusy(false);
  assert.equal(state.dom.busyIndicator.hidden, true);
  assert.equal(state.busy, false);
  assert.equal(switches, 1);
});

test("new chat cannot erase a running conversation", () => {
  const state = { busy: true, messages: [{ content: "running" }] };
  const c = load(["startNewChat"], { state, appendNotice: () => {} });
  c.startNewChat();
  assert.equal(state.messages[0].content, "running");
});

test("automatic unload never interrupts an assistant or workflow test request", async () => {
  let requests = 0;
  const state = { busy: true };
  const c = load(["unloadLlm"], { state, api: { fetchApi: async () => { requests++; } }, appendNotice: () => {} });
  assert.equal((await c.unloadLlm(true)).skipped, true);
  state.busy = false;
  state.workflowTestRunning = true;
  assert.equal((await c.unloadLlm(true)).skipped, true);
  assert.equal(requests, 0);
});

test("a failed unload releases the loading guard", async () => {
  const state = {};
  const c = load(["unloadLlm"], { state, api: { fetchApi: async () => { throw new Error("offline"); } }, appendNotice: () => {} });
  assert.equal((await c.unloadLlm()).error, "offline");
  assert.equal(state.llmUnloading, false);
});

test("preflight rejects incompatible sockets and multiple connections to one input", () => {
  const state = { nodeSchemas: {
    Source: { outputs: [{ name: "out", type: "IMAGE" }], required: [], optional: [] },
    Target: { outputs: [], required: [{ name: "in", type: "LATENT" }], optional: [] },
  } };
  const c = load(["findSlotIndex", "slotTypesCompatible", "preflightPlan"], { state });
  const connection = { from_role: "s", to_role: "t", from_output: "out", to_input: "in" };
  const errors = c.preflightPlan({ workflow_plan: [{ role: "s", node: "Source" }, { role: "t", node: "Target" }], connections: [connection, connection] }, {});
  assert.match(errors.join(" "), /incompatible types/);
  assert.match(errors.join(" "), /more than one connection/);
});

test("a throwing node factory rolls back the preceding nodes and run flags", () => {
  const state = { verifiedNodeTypes: new Set(["Good", "Throws"]), runMutatedGraph: false, runAddedNodeIds: [] };
  const removed = [];
  const c = load(["applyPlan", "discardPlanNodes"], {
    state, withGraphChange: (mutate) => mutate(),
    app: { graph: { getNodeById: (id) => ({ id }), remove: (node) => removed.push(node.id) } },
    addNode: (type) => {
      if (type === "Throws") throw new Error("custom factory failed");
      state.runAddedNodeIds.push(1);
      state.runMutatedGraph = true;
      return { id: 1 };
    },
  });
  const result = c.applyPlan({ workflow_plan: [{ role: "a", node: "Good" }, { role: "b", node: "Throws" }] });
  assert.equal(result.ok, false);
  assert.match(result.errors.join(" "), /factory failed/);
  assert.deepEqual(removed, [1]);
  assert.equal(state.runAddedNodeIds.length, 0);
  assert.equal(state.runMutatedGraph, false);
});

test("switching tabs during streaming preserves the connection and original chat owner", async () => {
  const controller = new AbortController();
  const state = { busy: true, sessionKey: "a", abortController: controller };
  const { response } = responseFor([
    Buffer.from('{"type":"text","text":"first "}\n'),
    Buffer.from('{"type":"text","text":"second"}\n'),
  ]);
  const c = load(["streamChat", "onWorkflowMaybeChanged"], {
    state, resolveWorkflowKey: () => "b", api: { fetchApi: async () => response },
  });
  const result = await c.streamChat([], [], () => c.onWorkflowMaybeChanged(), controller.signal);
  assert.equal(result.text, "first second");
  assert.equal(state.sessionKey, "a");
  assert.equal(state.pendingSessionKey, "b");
  assert.equal(controller.signal.aborted, false);
});

test("research escalation targets packs instead of blind web queries", async () => {
  const calls = [];
  const c = load(["escalateResearch"], {
    state: { config: { controller: { research: true } } }, MAX_RESEARCH_TARGETS: 2,
    debugLog: () => {}, appendNotice: () => {},
    api: { fetchApi: async (url, options) => {
      calls.push([url, JSON.parse(options.body)]);
      return { ok: true, json: async () => ({ ok: true }) };
    } },
    getBuildContext: async () => ({ resolved: true, counts: { unknown: 0 } }),
  });
  const packet = { resolved: true, target_model: { id: "flux2.klein.9b" }, task: "t2i",
    research_targets: ["MyPack", "OtherPack"], counts: { unknown: 5 } };
  const result = await c.escalateResearch("build", packet);
  assert.ok(result.researched);
  assert.equal(calls.length, 2);
  assert.equal(calls[0][0], "/chatbot/kb/research");
  assert.equal(calls[0][1].pack, "MyPack");
});
