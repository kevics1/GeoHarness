# Max reasoning and latency diagnosis

Date: 2026-10-17. Scope: read-only inspection of configuration, source, and the latest
saved GeoHarness test session. No provider requests, production map regeneration,
configuration changes, or product source changes were performed for this diagnosis.

## Observed configuration

- Main model: `cbcn/deepseek-v4.1-flash(max)`; agent request extensions are empty.
- Empty-response fallback: `cbcn/deepseek-v4.1-flash(none)`; fallback extensions are empty.
- Review model: `cbcn/deepseek-v4.1-flash(none)`; review extensions explicitly set
  `thinking.type=disabled`.
- The main client forwards the model alias unchanged. The alias alone does not prove
  the router applied max reasoning. No main-model fallback activation was established
  for this particular run. Recovery code can retain a successful fallback across
  subsequent tool turns in the same query.
- All 37 saved review diagnostics report zero reasoning tokens.

## Observed work

Session: `test-f8ffb58c932a/latest.json`, session ID file `session-8277717b368e.json`.
Business workspace: `F:/Desktop/Project/GeoHarness/example/test`.
Native/Python job timestamp window: approximately 42.84 minutes. This is an artifact
window, not a fully instrumented end-to-end request timer.

| Observation | Count / duration |
| --- | --- |
| Human text entries / assistant messages | 1 / 163 |
| Total model-issued tool calls | 172 |
| QGIS calls | 103 |
| QGIS help / spec / preflight / create / inspect | 6 / 38 / 31 / 23 / 5 |
| QGIS calls already using spec_ref or plan_ref | 86 |
| Native create / preflight jobs, all succeeded | 30 / 23 |
| Summed native job wall durations | 529.39 s |
| Python calls / summed job wall durations | 25 / 86.40 s |
| Model-issued review calls / saved review reports | 30 / 37 |
| Summed review diagnostic durations | 408.97 s |
| Review issues / unverified statuses | 33 / 4 |
| Session aggregate input / output tokens | 25,674,662 / 150,775 |

Token totals count repeated request inputs; they are not the size of one context.
Seven additional native rebuilds/reviews occur inside the automatic repair workflow.
The summed durations above are approximate components, not a critical-path profiler.
The remaining approximately 25.8 minutes cannot all be attributed to pure model
inference without per-request telemetry. Network, framework, and orchestration time
are not separated yet.

## Reproduced history defect

`cartography/review_context.py::_existing_receipt` reads `receipt.json` as an inline
receipt. The SQLite-backed receipt now contains only `job_id`, `schema_version`, and
`storage`. A read-only probe found 30 SQLite versions with 30 visual stages, but this
lookup returned zero versions and `build_review_iteration_context` returned zero
previous reviews.

Additionally, `cartography/workflow.py::review_with_budget` reconstructs iteration
context without a supplied receipt, then overwrites the earlier evidence. Therefore
the populated context constructed in `geo_map_review._review_one` can be replaced by
the empty context. This is a confirmed context-loss bug; how much latency it caused
has not been measured. Repeated visual warnings alone do not prove reviewer error.

## Proposed implementation order

1. Make the requested reasoning policy explicit across main, recovery, and review
   requests. Preserve the configured router and credentials; validate supported
   provider fields and show requested versus observed reasoning. Do not silently
   switch to none. Verify exact SDK payloads with mocks; live router compliance
   remains a separate acceptance step.
2. Fix SQLite review-history access using bounded tail reads, and avoid overwriting
   valid context with an empty reconstruction. Bind history to map identity and page.
3. Provide one short build operation using spec_ref plus edits. Freeze inputs and
   execute native validation and export in the same worker where possible. Preserve
   subject, coverage, scale, and roster checks and existing public operations.
4. Give one workflow authority over each repair cycle. Merge independent fixes into
   one candidate; return the final exact spec_ref. Track issue identity and measured
   improvement rather than treating changed pixels as progress. Keep unresolved
   issues visible; do not impose a cumulative version-count limit.
5. Review relevant pages together and reuse verified unchanged-page results. Exact
   image caching alone is insufficient: 35 distinct image digests were observed.
6. Reduce repeated model context using bounded receipts and inspect references, then
   evaluate action-specific schemas. Preserve required tool-call reasoning replay,
   source bindings, and full persisted diagnostic records. TUI line folding alone
   does not reduce API context.
7. Add per-request timing (first token and completion), actual model, requested
   reasoning policy, usage, tool phases, and total task elapsed time. Never log secrets
   or raw private reasoning for timing diagnostics.

## Twenty-minute acceptance target

Use the same fixed task/data and max reasoning for before/after comparisons.
Initial targets, not measured promises: 30-40 assistant requests, 4-8 native builds,
and 6-10 reviewed pages per task. Measure correctness, review completion, latency,
and token use separately; use repeated runs and p50/p95 instead of one lucky run.

Introduce an explicit total task budget, preserving time for final checks and
delivery. At roughly 12-15 minutes, prioritize unresolved correctness failures over
cosmetic iterations. If a hard 20-minute deadline is required, return completed
artifacts and precise unresolved status at the deadline; do not claim unfinished
work passed or interrupt an atomic write without cleanup. External service latency
precludes an unconditional guarantee of complete, fully reviewed results under
20 minutes for arbitrary tasks.

Source aggregate audits: `.scratch/latency-diagnosis-oct17.json` and
`.scratch/latency-review-audit-oct17.json`. Full diagnostics remain in the original
runtime workspace; this report does not copy credentials, raw prompts, or reasoning.
