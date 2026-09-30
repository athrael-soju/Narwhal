# Benchmark evidence bundle

Add an `evidence` section to the benchmark plan and the runner collects journal rows, metric samples, file digests, and a list of diagnostics for each point.

## Requirements

Run the [benchmark runner](05-Benchmark-Runner.md) on a host that reads the router's JSONL journal (the file passed to `narwhal-serve --journal`) and reaches the router and every engine's metrics endpoint.

The journal can hold rows from earlier runs. The collector records the byte offset before the client starts and again after the final drain, and copies only the rows between them into the point's `journal-rows.json`.

## Configure collection

Add an `evidence` key at the top level of the plan, next to `schema` and `points`. The JSON below is the value of that key. Set `engine_metrics_urls` to addresses the runner host can reach.

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

The plan and the `runs/` directory contain URLs and private paths. Keep them private.

The collector reads the client's records from `{point_dir}/client/requests.jsonl`, with `warmup.json` and `summary.json` beside it. `{point_dir}` is the point's output directory, `runs/<run>/<point>/`. If the client writes them elsewhere, add a `client_records` path to the `evidence` object. The collector fills in `{point_dir}` and `{point_id}` in that path.

The collector does not verify the `identity` fields. Check them against the deployment and preflight records before accepting a GPU result.

## What gets collected

Before each point, the collector hashes the fleet and profile files and reports any change to either while the point runs. It also hashes the client's records, warmup, and summary files when they exist.

Each point gets these files under `runs/<run>/<point>/`:

| File                                                                 | Contents                                                                                                                          |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `result.json`                                                        | Readiness, client exit status, drain result, timestamps, and the number of evidence diagnostics                                   |
| `evidence.json`                                                      | Journal offsets, identity, file digests, counts by terminal class and process, counter deltas, the role timeline, and diagnostics |
| `samples.json`                                                       | Timestamped router state, router and engine metrics, and scrape errors across the load and drain                                  |
| `journal-rows.json`                                                  | Every journal row between the two offsets, including terminal rows, process metadata, and events                                  |
| `client/requests.jsonl`, `client/warmup.json`, `client/summary.json` | The client's own files, when it writes inside the point directory                                                                 |
| `client-*.snapshot.*`                                                | Exact private copies of the client files, when the client writes them elsewhere                                                   |
| `client.stdout`, `client.stderr`                                     | The client's output                                                                                                               |
| `summary.shareable.json`                                             | A redacted summary for publication (see below)                                                                                    |

`evidence.json` lists which client files were kept, under `retained_client_files`, along with their hashes.

### The shareable summary

`summary.shareable.json` is the only file meant to leave private storage. It holds selected counts, latency and throughput (when the client wrote `summary.json`), an anonymized role history, configuration digests, and diagnostics. It omits URLs, credentials, private file paths, engine IDs, request failure text, and the raw engine image reference.

It includes the model and revision labels from `identity`. Review them before publishing.

## How the checks work

The collector compares the client's records, the journal rows, and the router's counters.

**Client against journal.** The collector compares the number of sent requests with the number of terminal journal rows. When both sides have `client_rid`, it also matches requests one to one. Completed counts are compared separately. The warmup request counts in these comparisons and in the counter comparison, and is reported as `warmup_sent`.

**Journal against counters.** Router state includes `journal_run`, which identifies the router process, so samples are grouped per process, including after a restart or standby takeover. For each process, the collector compares the journal's terminal classes with the movement of the router counters between that process's first and last sample. `narwhal_offered_total`, `narwhal_expired_total`, and `narwhal_invalid_requests_total` restart from zero with each process. The served, failed, refused, rejected, and cancelled counters can be restored from an earlier process. If a process has no complete pair of samples, the collector emits a counter diagnostic.

**Role history.** The timeline starts with the role pools seen in the first sample and adds each role flip as it is observed. If `narwhal_flips_total` counts more flips than the timeline saw, the router's bounded state history dropped a change, and the collector flags a `role_history_gap`.

**Sampling.** The collector flags scrape errors and any gap between samples longer than 2.5 times the configured interval. Each diagnostic names the point, and scrape-gap diagnostics include the affected time window.

## When the numbers don't match

Start with `evidence.json`. Then check `journal-rows.json`, the client files listed in `retained_client_files`, and the raw `samples.json`.

Common causes are another client using the router, a process restart, a failed scrape, or a client that did not record every outcome. Keep the failed bundle, fix the cause, and rerun the point. The summary derives from the evidence, so a rerun closes the gap.
