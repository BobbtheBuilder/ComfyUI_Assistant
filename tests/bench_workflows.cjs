// Deterministic workflow benchmark: exercises validate -> preflight -> apply with a mocked graph.
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

const SCHEMAS = {
  UNETLoader: { outputs: [{ name: "MODEL", type: "MODEL" }], required: [{ name: "unet_name", type: "COMBO", widget: true }], optional: [], widgets: ["unet_name"] },
  CLIPLoader: { outputs: [{ name: "CLIP", type: "CLIP" }], required: [{ name: "clip_name", type: "COMBO", widget: true }, { name: "type", type: "COMBO", widget: true }], optional: [], widgets: ["clip_name", "type"] },
  VAELoader: { outputs: [{ name: "VAE", type: "VAE" }], required: [{ name: "vae_name", type: "COMBO", widget: true }], optional: [], widgets: ["vae_name"] },
  CLIPTextEncode: { outputs: [{ name: "CONDITIONING", type: "CONDITIONING" }], required: [{ name: "clip", type: "CLIP", widget: false }, { name: "text", type: "STRING", widget: true }], optional: [], widgets: ["text"] },
  EmptyFlux2LatentImage: { outputs: [{ name: "LATENT", type: "LATENT" }], required: [{ name: "width", type: "INT", widget: true }, { name: "height", type: "INT", widget: true }], optional: [], widgets: ["width", "height"] },
  KSampler: { outputs: [{ name: "LATENT", type: "LATENT" }], required: [{ name: "model", type: "MODEL", widget: false }, { name: "positive", type: "CONDITIONING", widget: false }, { name: "negative", type: "CONDITIONING", widget: false }, { name: "latent_image", type: "LATENT", widget: false }, { name: "seed", type: "INT", widget: true }], optional: [], widgets: ["seed"] },
  VAEDecode: { outputs: [{ name: "IMAGE", type: "IMAGE" }], required: [{ name: "samples", type: "LATENT", widget: false }, { name: "vae", type: "VAE", widget: false }], optional: [], widgets: [] },
  SaveImage: { outputs: [], required: [{ name: "images", type: "IMAGE", widget: false }], optional: [], widgets: ["filename_prefix"] },
};

const PACKET = {
  resolved: true,
  target_model: { id: "flux2.klein.9b" },
  compatible_nodes: ["UNETLoader", "CLIPLoader", "VAELoader", "CLIPTextEncode", "EmptyFlux2LatentImage", "KSampler", "VAEDecode", "SaveImage"]
    .map((node) => ({ node, state: "GENERIC", roles: [] })),
  exclusions: [],
  installed_assets: [
    { filename: "flux2-klein-9b-fp8.safetensors" },
    { filename: "qwen_3_4b.safetensors" },
    { filename: "flux2-vae.safetensors" },
  ],
  known_good_patterns: [],
};

const KLEIN_T2I = {
  target_model: "flux2.klein.9b", task: "t2i", pattern_id: "klein9b_basic_t2i",
  workflow_plan: [
    { role: "model_loader", node: "UNETLoader" },
    { role: "text_encoder_loader", node: "CLIPLoader" },
    { role: "vae_loader", node: "VAELoader" },
    { role: "positive", node: "CLIPTextEncode" },
    { role: "negative", node: "CLIPTextEncode" },
    { role: "latent", node: "EmptyFlux2LatentImage" },
    { role: "sampler", node: "KSampler" },
    { role: "decoder", node: "VAEDecode" },
    { role: "save", node: "SaveImage" },
  ],
  connections: [
    { from_role: "model_loader", from_output: "MODEL", to_role: "sampler", to_input: "model" },
    { from_role: "text_encoder_loader", from_output: "CLIP", to_role: "positive", to_input: "clip" },
    { from_role: "text_encoder_loader", from_output: "CLIP", to_role: "negative", to_input: "clip" },
    { from_role: "positive", from_output: "CONDITIONING", to_role: "sampler", to_input: "positive" },
    { from_role: "negative", from_output: "CONDITIONING", to_role: "sampler", to_input: "negative" },
    { from_role: "latent", from_output: "LATENT", to_role: "sampler", to_input: "latent_image" },
    { from_role: "sampler", from_output: "LATENT", to_role: "decoder", to_input: "samples" },
    { from_role: "vae_loader", from_output: "VAE", to_role: "decoder", to_input: "vae" },
    { from_role: "decoder", from_output: "IMAGE", to_role: "save", to_input: "images" },
  ],
  widgets: [
    { role: "model_loader", name: "unet_name", value: "flux2-klein-9b-fp8.safetensors" },
    { role: "text_encoder_loader", name: "clip_name", value: "qwen_3_4b.safetensors" },
    { role: "vae_loader", name: "vae_name", value: "flux2-vae.safetensors" },
    { role: "sampler", name: "seed", value: 42 },
  ],
};

function runPlan(plan, packet) {
  const nodes = new Map();
  let nextId = 1;
  const added = [];
  const graph = {
    _nodes: [], links: {},
    add(node) { added.push(node.type); this._nodes.push(node); nodes.set(node.id, node); },
    remove(node) { this._nodes = this._nodes.filter((item) => item !== node); nodes.delete(node.id); },
    getNodeById(id) { return nodes.get(id) || null; },
    beforeChange() {}, afterChange() {}, setDirtyCanvas() {},
  };
  const createNode = (type) => {
    const schema = SCHEMAS[type] || { required: [], optional: [], outputs: [], widgets: [] };
    return {
      id: nextId++, type, pos: [0, 0], size: [200, 100],
      inputs: [...(schema.required || []), ...(schema.optional || [])].map((item) => ({ name: item.name, type: item.type, link: null })),
      outputs: (schema.outputs || []).map((item) => ({ name: item.name, type: item.type, links: [] })),
      widgets: (schema.widgets || []).map((name) => ({ name, value: null })),
    };
  };
  const state = { nodeSchemas: SCHEMAS, verifiedNodeTypes: new Set(Object.keys(SCHEMAS)), runAddedNodeIds: [], runMutatedGraph: false };
  const c = load([
    "validatePlanUsage", "preflightPlan", "isWidgetSpec", "specTypeOf", "nodeSchemaDigest",
    "applyPlan", "withGraphChange", "findFreePosition", "nodePositionBlocked", "nodeRectAt",
    "rectsOverlap", "addNode", "discardPlanNodes", "findSlotIndex", "slotTypesCompatible",
  ], {
    app: { graph, workflowManager: null }, state, LiteGraph: { createNode }, debugLog: () => {},
    connectNodes: () => ({ ok: true }), setWidgetValue: () => ({ ok: true }),
    ASSET_WIDGET_RE: /(unet_name|name)$/i, ASSET_EXT_RE: /\.(safetensors)$/i,
  });
  const usage = c.validatePlanUsage(plan, PACKET);
  const preflight = usage.length ? [] : c.preflightPlan(plan, PACKET);
  const errors = [...usage, ...preflight];
  const applied = errors.length ? { ok: false, created: [] } : c.applyPlan(plan);
  return { added, applied, errors };
}

test("bench: klein t2i basic builds deterministically", () => {
  const started = Date.now();
  const result = runPlan(KLEIN_T2I, PACKET);
  const ms = Date.now() - started;
  assert.equal(JSON.stringify(result.errors), "[]");
  assert.equal(result.applied.ok, true);
  assert.equal(result.added.length, 9);
  assert.ok(result.added.includes("UNETLoader") && result.added.includes("SaveImage"));
  console.log(`  bench klein_t2i_basic: ${ms} ms, ${result.added.length} nodes`);
});

test("bench: missing required conditioning is rejected before mutation", () => {
  const started = Date.now();
  const plan = { ...KLEIN_T2I, connections: KLEIN_T2I.connections.filter((link) => link.to_input !== "negative") };
  const result = runPlan(plan, PACKET);
  const ms = Date.now() - started;
  assert.equal(result.applied.ok, false);
  assert.equal(result.added.length, 0);
  assert.match(result.errors.join(" "), /required input "negative" is not connected/);
  console.log(`  bench klein_t2i_missing_negative: ${ms} ms, rejected`);
});

test("bench: uninstalled asset is rejected before mutation", () => {
  const started = Date.now();
  const plan = { ...KLEIN_T2I, widgets: KLEIN_T2I.widgets.map((widget) =>
    widget.name === "unet_name" ? { ...widget, value: "missing-model.safetensors" } : widget) };
  const result = runPlan(plan, PACKET);
  const ms = Date.now() - started;
  assert.equal(result.applied.ok, false);
  assert.equal(result.added.length, 0);
  assert.match(result.errors.join(" "), /is not installed/);
  console.log(`  bench klein_t2i_missing_asset: ${ms} ms, rejected`);
});
