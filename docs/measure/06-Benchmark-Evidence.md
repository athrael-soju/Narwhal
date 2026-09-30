# Benchmark evidence bundle

Add an `evidence` section to the benchmark plan and the runner will collect what you need to check each point afterward: journal rows, metric samples, file digests, and a list of anything that doesn't add up.

## Requirements

Run the [benchmark runner](05-Benchmark-Runner.md) on a host that can read the router's JSONL journal (the same file passed to `narwhal-serve --journal`) and reach the router and every engine's metrics endpoint.

The journal can already contain older rows. The collector notes the byte offset before the client starts and again after the final drain, and copies only the rows in between into the point's `journal-rows.json`.

## Configure collection

Add an `evidence` object at the top level of the plan, next to `schema` and `points`:

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

Change the metrics URLs to addresses the runner host can reach. Keep the plan, and the whole `runs/` directory, private.

The collector reads the client's records from `{point_dir}/client/requests.jsonl`, with `warmup.json` and `summary.json` beside it. If your client writes them somewhere else, add a `client_records` path to the `evidence` object. The collector fills in `{point_dir}` and `{point_id}` in that path.

The collector takes the `identity` fields as given and doesn't verify them. Check them against the deployment and preflight records before you accept a GPU result.

## What gets collected

Before each point, the collector hashes the fleet and profile files, and it reports if either one changes while the point is running. It also hashes the client's records, warmup, and summary files when they exist. Router state includes `journal_run`, which identifies the router process, so metric samples can be grouped correctly across a restart or a standby takeover.

Each point gets these files under `runs/<run>/<point>/`:

| File                                                                 | Contents                                                                                                                          |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `result.json`                                                        | Readiness, client exit status, drain result, timestamps, and the number of evidence diagnostics                                   |
| `evidence.json`                                                      | Journal offsets, identity, file digests, counts by terminal class and process, counter deltas, the role timeline, and diagnostics |
| `samples.json`                                                       | Timestamped router state, router and engine metrics, and scrape errors across the load and drain                                  |
| `journal-rows.json`                                                  | Every journal row between the two offsets, including terminal rows, process metadata, and events                                  |
| `client/requests.jsonl`, `client/warmup.json`, `client/summary.json` | The client's own files, if it writes inside the point directory                                                                   |
| `client-*.snapshot.*`                                                | Exact private copies of the client files, if the client writes them somewhere else                                                |
| `client.stdout`, `client.stderr`                                     | The client's output                                                                                                               |
| `summary.shareable.json`                                             | A redacted summary for publication (see below)                                                                                    |

`evidence.json` lists which client files were kept, under `retained_client_files`, along with their hashes.

### The shareable summary

`summary.shareable.json` is the only file meant to leave your private storage. It has selected counts, latency and throughput (if the client wrote `summary.json`), an anonymized role history, configuration digests, and any evidence gaps. It leaves out URLs, credentials, private file paths, engine IDs, request failure text, and the raw engine image reference.

It does include the model and revision labels you declared, so read it before publishing in case one of those labels gives away more than you intended. Keep every other file private.

## How the checks work

The collector compares three sources: the client's records, the journal rows, and the router's counters.

**Client against journal.** The collector compares the number of sent requests with the number of terminal journal rows. When both sides have `client_rid`, it also matches requests one to one. Completed counts are compared separately.

**Journal against counters.** For each router process, the terminal classes in the journal are compared with how much the router counters moved between that process's first and last sample. `narwhal_offered_total`, `narwhal_expired_total`, and `narwhal_invalid_requests_total` restart from zero with each process. The served, failed, refused, rejected, and cancelled counters may be restored from before. If the process changed and there isn't a complete pair of samples for it, you'll get a counter diagnostic.

**Role history.** The timeline starts with the role pools seen in the first sample and adds each role flip as it's observed. If `narwhal_flips_total` counts more flips than the timeline saw, the router's bounded state history has dropped a change, and the collector flags a `role_history_gap`.

**Sampling.** Scrape errors are flagged, and so is any gap between samples longer than 2.5 times the configured interval. Each of these diagnostics names the point, and scrape-gap diagnostics include the affected time window.

## When the numbers don't match

Start with `evidence.json`. From there, look at `journal-rows.json`, the client files listed in `retained_client_files`, and the raw `samples.json`. The warmup request sits in its own client file. The collector still counts it when it compares the client with the journal and counters, and reports it separately as `warmup_sent`.

The usual causes are another client using the router, a process restart, a failed scrape, or a client that didn't record every outcome. Keep the failed bundle, fix the cause, and run the point again. The summary is derived from the evidence, so editing it won't close a gap.
