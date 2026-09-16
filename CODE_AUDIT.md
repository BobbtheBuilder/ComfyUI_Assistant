# Current checkout code audit

## Scope and evidence

Repository-wide Python syntax/import/reference scans and duplicate-function-body scans;
JavaScript function/constant reference scans; focused review of the frontend controllers,
workflow ownership, provider streaming, routes, persistence, indexing, compatibility and
enrichment. Reviewed `debug.log`, including the two build sessions beginning at
2026-09-16 01:06 and 01:10 UTC. Reviewed tests and ran the offline suites.

This is a static and regression-test audit, not proof that every supported model or custom
node works. No live LLM requests were sent, production databases rebuilt, or running
enrichment jobs restarted. Existing unrelated working-tree changes were preserved.

## Changes made

- Removed unused knowledge writers: `add_alias`, `add_edge`, `add_evidence`, `add_workflow`.
  Repository code, tests and documentation contained no callers. Active manifest ingestion
  and evidence routes use other implementations and remain in place.
- Removed unused frontend `parseClaimsJson`, `storeClaims`, `RESEARCH_SYSTEM`, and
  `MAX_WEB_QUERIES`, plus the test for the retired parser.
- Consolidated SQLite schema-version reading in `storage.schema_version`, retaining the
  existing private import names for callers.
- Consolidated pack-name extraction in `node_catalog.pack_from_module`.
- Summarization now uses the shared provider completion/streaming implementation instead
  of a separate OpenAI/Anthropic HTTP implementation. Its hidden 1,200-token ceiling is gone.
- Planner lookup exhaustion now has one final, tools-disabled synthesis request. Previously,
  it returned null after the last tool result without asking the model to produce a plan.
- Planner execution enforces the advertised tool allowlist and checks cancellation before
  subsequent calls and after responses. Unexpected mutation tools are rejected.

## Requested cap removal

- Removed the first-60-nodes-per-pack enrichment cutoff. Batching remains; it processes all nodes.
- Removed build-packet caps of 80 candidates/exclusions and 15 unknown nodes.
- Removed the 16-schema planning cap, 40-node context subsets, diagnostic node/type caps,
  prompt-node subsets, and validation candidate/report subsets.
- Installed-node search returns all matches by default; an explicitly requested limit still works.
- Removed default tool-result character truncation, the planner's 6,000-character tool-result
  truncation, and unconditional truncation of older tool results before each request.
- Preserved full node descriptions and diagnostic schema fields in these frontend paths.
- New output-token settings default to zero (provider-managed); OpenAI-compatible requests
  omit `max_tokens` in that mode. Explicit positive settings remain effective. Enrichment's
  zero setting no longer inherits the chat's output cap.
- Anthropic requires an explicit output-token limit; zero produces a clear configuration
  error instead of sending an invalid request or inventing another fixed ceiling.

Physical model context windows, optional context compaction, user-requested retrieval limits,
document chunking, log/display excerpts, image sizing and bounded retry/tool-call counts remain.
Those are not removed node/output-token ceilings. Larger packets may still exceed a provider's
actual context capacity. Existing saved positive token settings are not overwritten.

Packs already marked done by the old 60-node implementation are not automatically requeued
by this cleanup. Their omitted nodes need a later targeted requeue. No live DB migration was run.

## Important unresolved findings

### High: model resolution can return unrelated nodes

`knowledge.resolve` searches all entity kinds, then uses bidirectional SQL substring matching
over aliases. Short aliases and SQL wildcard characters can match ordinary words in the request.
There is no model-only constraint for the build-controller caller. This explains how a model
request can become `node:ComfyOrNode` or `node:MiniMaxCoverControl`.

Required follow-up: separate model resolution from node/asset resolution; use literal,
token-aware matching and report ambiguity instead of choosing an unrelated entity. The planner
finalization fix does not solve this independent blocker.

### High: generated requirements can reject valid utilities

`enrich._apply_pack_result` accepts generated model-property restrictions. The evaluator trusts
the target model's properties without using each restriction's confidence as an enforcement gate.
Observed stored rules confuse IMAGE/LATENT/MASK data with model roles. Generic image utilities
can consequently be classified as incompatible with diffusion models.

Required follow-up: typed predicate values, source-backed restrictions, and advisory treatment
of unsupported generated claims. Conflicting explicit edges also need deliberate resolution;
the current evaluator takes the first confirmed edge encountered.

### High: enrichment and retrieval remain disconnected

`kb.node_docs` does not merge enriched purposes/side effects from the knowledge database.
`knowledge.get_build_context` sends compatibility metadata but not those node explanations.
More completed enrichment does not automatically mean the planner receives better node guidance.

### Medium: enrichment freshness and progress are incomplete

`enrich.index_local_facts` fingerprints pack name, repository and node names, not source/schema/
documentation contents. In-place node changes can leave stale enrichment marked complete.
Local properties include value in their primary key, so inserting a changed value does not
necessarily remove the old value. Pack-level retries can repeat successful batches, and a
valid response omitting some requested nodes can still lead to a completed pack status.

### Medium: pack result ownership and provenance need validation

`_apply_pack_result` checks whether a generated node exists, but not whether it belongs to
the pack/batch being enriched. A mistaken node name can replace another node's inferred data.
Generated claims cite the LLM as source rather than an exact supporting passage.

### Medium: enrichment model selection can diverge from recorded provenance

`enrich_pack` records the enrichment-specific model setting, while `_complete` builds the request
from the main configuration without applying that model override. If those settings differ,
the recorded generating model can be wrong.

### Medium: candidate selection and request-time work remain expensive

`_relevant_nodes` includes all role-bearing nodes without task relevance ranking. Removing
truncation preserves coverage but does not improve ranking. Build-time research can run pack
enrichment inline. The worker checks idleness between packs, not between every batch.

## What the logs establish

- The recent failed builds completed provider requests with `finish_reason: tool_calls`.
  Repeated three-call groups match planner call-budget exhaustion. The log does not contain
  final response bodies, so it cannot establish malformed JSON as the cause.
- Older enrichment runs logged coroutine serialization errors and unsupported response-format
  errors. Current `_web_research` awaits the async search via `asyncio.run`; current `_complete`
  uses schema mode with a fallback. Historical entries are not evidence these errors persist.
- Test entries such as `pack:MyPack` / `boom` and schema version 999 are deliberately generated
  by fixtures. Tests currently share the extension debug sink, so raw error totals are misleading.
- Provider log records lack a correlation ID tying enrichment, chat, and planner attempts together.
  Add that before relying on aggregate latency/error counts for diagnosis.

## Validation and limits

Baseline: 124 Python tests and 49 JavaScript tests passed before cleanup.
Final validation: 128 Python tests, 52 JavaScript tests, and all 3 workflow fixture checks pass.
JavaScript module syntax and Git whitespace checks also pass.
Post-change suites cover large packs/packets, all-schema inclusion, untruncated tool data,
provider-managed output, shared summarization, planner finalization, allowlisting and cancellation.
The separate workflow fixture benchmark covers one basic Klein build and two rejection cases.

These checks do not demonstrate successful Qwen Edit 2511 or Flux 2 builds against the user's
live provider. Model resolution and compatibility/retrieval findings above remain blockers.
Backend changes need a later ComfyUI restart; refresh the frontend then. The running enrichment
process continues using its already-loaded code until that restart.
