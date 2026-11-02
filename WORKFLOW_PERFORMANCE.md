# Workflow build performance

## Scope and invariants

The bridge and frontend retain their existing workflow ownership, cancellation,
busy/typing signal, validation and repair semantics. Measurements contain timing,
counts and sizes, never prompts, tool arguments, retrieved text or credentials.
No model request deadlines, persistent schema caches or automatic wiring were added.

`node_catalog.schema_record` reads a fresh, JSON-normalized schema without source
evidence or an evidence fingerprint. Builder lookups and validation use it.
`node_catalog.record` remains the evidence-rich interface for knowledge indexing,
node documentation and experience fingerprints; its fingerprint meaning is unchanged.
Dynamic INPUT_TYPES/GET_NODE_INFO_V1 options are recomputed on each lookup.

The bridge supports `get_node_schemas` in addition to the singular tool. It returns
ordered `{node_type, schema}` or `{node_type, error}` entries, preserving duplicates.
Adjacent read-only calls overlap; submission and unknown tools are barriers.
Tool replies retain call IDs and request order even when reads finish out of order.
Already-running Python thread work cannot be forcibly stopped by asyncio cancellation;
its waiter is cancelled, asyncio tasks are drained and no later phase is started.
The existing executor manages thread resources; no read is silently discarded.

Both search tools accept a nonnegative integer `limit`, default 0 = unlimited.
KB results remain structured until the final tool-message JSON serialization.
The previous hidden 60-node/4-document limits and 24,000-character slicing are gone.

## Measurements

Run `python -m tests.benchmark_workflow_builder --output <report.json>`.
It uses real catalog extraction and validation against fixture nodes, a simulated
provider, and three independent KB reads with 15 ms simulated I/O each. It never
calls a real LLM, generates images or modifies a live canvas. Seven warm samples
follow a first-invocation sample; the first invocation is not a fresh-process cold
start. Reports include every sample and min/max, not only the median.

Baseline Git revision: `ce0a7f6b6269d189918961ce97831b042f75e47c`.
Initial measurements on this machine:

| Workload | Baseline median | Schema-only + complete KB | Concurrent reads |
| --- | ---: | ---: | ---: |
| 16 schema lookups | 4.38 ms | 0.154 ms | 0.159 ms |
| Validate 64 nodes | 35.48 ms | 1.64 ms | See saved report |
| Validate 256 nodes | 142.38 ms | 6.51 ms | 6.56 ms |
| Bridge, independent reads | 54.58 ms | 51.26 ms | 19.11 ms |

Schema extraction and validation gains are well outside observed sample ranges.
The concurrency comparison uses the same complete responses before and after;
its reduction is in the simulated I/O workload, not proven live-model latency.
KB serialization is a correctness fix: result bytes grew from 81,789 to 200,894
because results are complete. Do not describe that as a measured end-to-end win.
Batched lookups measured 18.69 ms versus 19.11 ms for individual concurrent calls:
that small local difference is not a demonstrated speedup. Batching is retained
as a supported tool contract enabling models to request schemas together. The
fixture uses two model rounds in either form; fewer real-model rounds remain unproven.

Raw staged benchmark reports are local under `docs/performance/` (git-ignored).
Use the checked-in benchmark to reproduce results. The 50k-inventory case exercises
the bridge with a large registry; it is not a benchmark of full installed-node search.

## Production observations

With existing debug logging enabled, both paths emit `performance/build.finished`
with a build ID, entry point, outcome, monotonic elapsed milliseconds, phase totals
and counts. The bridge reports inventory, model, schema, validation, KB search,
tool execution and serialization durations, plus model rounds and tool-result bytes.
The frontend reports model/tool/validation/serialization durations, tool-result
characters and fixup passes. Frontend instrumentation is inactive when debug is off.

Nested and concurrent phase totals overlap: do not sum them into elapsed time.
Cancelled builds contain partial totals. To assess user-visible gains, compare
the same real build prompts, provider/model and installed files with debug enabled,
then inspect model time, tool time, model rounds and payload sizes separately.
No live-model speedup is claimed by the offline benchmark.

## Evidence-dependent work

- Persistent schema caching: deferred; fresh options are required and extraction
  is already much cheaper without source inspection.
- Validation-result caching: deferred; no evidence of expensive identical submissions.
- Frontend definitions cache replacement: unnecessary; the existing promise and
  identity cache are retained.
- Fixup progress detection: deferred until real debug reports show repeated
  unchanged repairs. Type-only automatic wiring is outside scope.

## Verification and review

Behavior checks cover fresh independently owned schemas, evidence consumers,
complete KB responses, mixed batch errors, caller-controlled retrieval counts,
concurrent reads and stable result IDs, submission barriers, cancellation during
provider streaming and reads, and frontend build/fixup timing without content leaks.
Final check results and Standards/Spec plus Alibaba review receipts are recorded
in the local implementation tracker under `.scratch/workflow-performance/`.

Final checks: **161 Python tests and 64 frontend tests passed**. Both independent
review findings were fixed and rechecked: insecure-context trace ID creation and
schema/source coherence during registry replacement or removal. Evidence-rich
fixture records and fingerprints also match the original revision exactly.

Alibaba review completed in **host-delegate mode** using its selected files and
rules (5/5 selected files reviewed, none skipped). The native attempt produced no
report and was cancelled after being superseded; two focused Go attempts returned
no final answer, including one with an explicitly increased response allowance.
Those attempts are recorded as unavailable, not passed. No unresolved blocking
findings remain from the completed host-delegate or independent reviews.
