# Benchmark evidence bundle

Run the [ordered benchmark runner](05-Benchmark-Runner.md) on a host that can read the router's append-only JSONL request journal and reach the router and every engine metrics endpoint. The journal path must be the same file used by `narwhal-serve --journal`. Mount or copy no historical rows into a point: the collector records byte offsets before the client starts and after the final drain, then reads exactly that interval.

Add `evidence` to the benchmark plan:

```json
{
  "journal_path": "runs/deployment/journal.jsonl",
  "fleet_path": "runs/deployment/fleet.json",
  "profiles_path": "profiles/accepted.json",
  "sample_interval_s": 1,
  "engine_metrics_urls": {
    "engine-0": "http://127.0.0.1:8010/metrics",
    "engine-1": "http://127.0.0.1:8011/metrics"
  },
  "identity": {
    "narwhal_revision": "<40-character-commit>",
    "model_id": "<served-model>",
    "benchmark_client_version": "<client-version-or-commit>",
    "engine_image": "<pinned-image-with-digest>",
    "engine_version": "<engine-version>",
    "checkpoint_revision": "<checkpoint-commit>",
    "gpu_shape": "<GPU-model-and-count>",
    "gpu_allocation": "<private-host-and-GPU-map>",
    "initial_role_split": "<prefill/decode assignment>"
  }
}
```

This object belongs at the plan's top level, alongside `schema` and `points`. The metrics URLs in the example must be replaced with addresses reachable from the runner host. Keep the plan and entire `runs/` directory private. The collector computes SHA-256 digests of the fleet and profiles before each point and reports if either file changes during the point. The identity fields are operator-declared; compare them with the deployment and preflight records before accepting a GPU result. The router state exposes `journal_run` so metrics samples can be grouped by process even across restart or standby takeover.

For each point, the runner writes these files under `runs/<run>/<point>/`:

| File | Contents |
| --- | --- |
| `result.json` | Readiness, client exit status, drain result, timestamps, and evidence diagnostic count |
| `evidence.json` | Journal cursors, identity, file digests, counts by terminal class and process run, counter deltas, role timeline, and diagnostics |
| `samples.json` | Timestamped router state, router `/metrics`, engine metrics, and scrape errors from the load and drain interval |
| `journal-rows.json` | All JSONL rows between the two byte offsets, including terminal rows, process metadata, and events |
| `client-rows.json` | Client outcomes, including the load trial's unscored warmup when present |
| `client.stdout`, `client.stderr` | Exact external client output |
| `summary.shareable.json` | Selected counts, latency and throughput if the client wrote `summary.json`, anonymised role history, configuration digests, and evidence gaps |

The shareable summary omits URLs, credentials, private file paths, engine IDs, request failure text, and the raw engine image reference. Inspect it before publishing in case an operator-supplied model or revision label itself contains sensitive data. Keep the other files private.

The collector matches client request IDs to journal `client_rid` when both sides provide them. Otherwise it compares sent-client and terminal-journal totals. It compares completed totals separately. Journal terminal classes are compared with router counter deltas for each `journal_run`; `narwhal_offered_total`, `narwhal_expired_total`, and `narwhal_invalid_requests_total` reset at process start, while served, failed, refused, rejected, and cancelled counters may be restored. No counter is subtracted across a process boundary. A process change without both a baseline and final sample for that run produces a counter diagnostic rather than an invented delta.

The timeline begins with observed role pools and adds newly observed flip records. The collector compares newly observed flips with `narwhal_flips_total` and flags a `role_history_gap` when bounded state history has lost a change. It flags scrape errors and intervals exceeding 2.5 times the declared sample cadence. These diagnostics name the point and, for scrape intervals, the affected time window.

For a mismatch, inspect `evidence.json`, then the bounded `journal-rows.json`, `client-rows.json`, and raw `samples.json`. Check whether unrelated clients used the router, a process restarted, a scrape failed, or the client omitted outcomes. Retain the failed bundle. Repair the cause and run a fresh point; a gap cannot be filled by editing the summary.
