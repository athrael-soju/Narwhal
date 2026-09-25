# Kimi-K3 GPU benchmark qualification

The private run bundle under `runs/issue44-mi355x-20260923/` records host allocation, endpoint addresses, the full deployment package, launch checks, engine attestations, profile samples, preflight output, and raw benchmark evidence.

## Pinned inputs

| Input | Value |
| --- | --- |
| Narwhal router source | `6c6c7da4c879101d4f353da1590aa32ce2bf7c10` |
| Benchmark client source | `31b0b78e0d8b1438b212ae56b9fbba32832b53a2` |
| Model | `moonshotai/Kimi-K3` |
| Engine | vLLM `0.29.0+rocm100`, image `sha256:9eacf87e93ecffcb910802d0d0505ef3c9b753a66fb09bec304d72d8dac1dbc2` |
| Accelerator per engine | AMD Instinct MI355X, eight GPUs, tensor parallelism 8 |
| Checkpoint weights | 96 safetensors shards; SHA-256 of the sorted `path:sha256` manifest is `6cd00d6ba5817a868738202c91b977534668c42d89fce3b317340de88ea9d2ed` |
| Model config SHA-256 | `9710e121a58d03ac92c8d6da287a19541994319afbbe6d6202af001ffd379213` |
| Fleet latency budgets | 10 s TTFT, 0.3 s TPOT; first-token deadline 8.5 s |
| Workload | 8,192 input tokens, 128 output tokens, seed 1729, temperature 0, prefix caching disabled; 20 distinct IDs in a 28-token pool |
| Profile store SHA-256 | `8a6ab49148a0eed646863b9f4fc3fc82bf0b3584bb535c9cd7d506c957ed3bc6` |
| Qualified fleet SHA-256 | `7a4e4583933bff496d11a608a2544deac48d2d8843402861ce8015568d5e3d72` |
| Benchmark plan SHA-256 | `43a8755d37383687f5ef639c5c16a095f5cc0feaf02b3d0821b1297f319f79b3` |
| Workload SHA-256 | `55885dd1d9b42a4debf7b01230bbb5c917334689e867d5ca08a66edfb73b80e3` |

The earlier full checkpoint manifests agree on all weight shard hashes. The live launch check confirmed every shard's recorded size and the model config hash on every selected host. Keep the per-host manifests and current process records in the private bundle. The workload token pool comes from the returned prompt token IDs; the seed completion's output IDs were all identical, so using those IDs would have produced a repeated-token prompt.

Live profiles measured 8,192-token prefill medians around 0.92 s and decode intercepts from 0.2416 to 0.2426 s/token. The packaged 2.5 s first-token deadline rejected working KV handoffs between engines. Direct probes with a wider observation window completed on every permitted path; first-token latency ranged from 0.348 to 5.274 s. The qualified fleet copy changes only `engine.first_token_timeout_s` to 8.5 s, above the observed maximum and leaving about 0.6 s of the 10 s TTFT budget after the measured prefill if the deadline is exhausted. The full preflight passed its health, contract, generation, model, pace, tokenization, KV transfer, and SLO gates against the same running engine containers and profile generations.

## Procedure

Load the private deployment environment, then prepare and install the pinned source package with `tools/deployment/deploy_hosts.py`. Start each engine from its checked launch plan, capture the live cache and NIXL contract, and start its attestation sidecar. On the router host, finalize the fleet contract and profile the current engine generations:

```bash
.venv/bin/python tools/deployment/attestation_contract.py finalize-fleet \
  --fleet runs/deployment/fleet.json
.venv/bin/narwhal-profile \
  --fleet runs/deployment/fleet.json \
  --limits runs/deployment/profiling-limits.json \
  --prefill-lens 256,1024,4096,8192 \
  --decode-input-lens 512,4096,8192 \
  --decode-concurrency 1,2,4 \
  --decode-tokens 64 \
  --prefill-repeats 3
.venv/bin/narwhal-check --fleet runs/issue44-mi355x-20260923/fleet-qualified.json
```

Start the router with an explicit private journal. While it runs, update the benchmark helper in the router-host checkout to the pinned client revision, then generate one workload reused by both points:

```bash
.venv/bin/narwhal-serve --fleet runs/issue44-mi355x-20260923/fleet-qualified.json \
  --host 127.0.0.1 --port 8000 \
  --journal runs/issue44-mi355x-20260923/router-journal.jsonl
.venv/bin/python tools/measurement/load_trial.py prepare \
  --base http://127.0.0.1:8000 \
  --input-tokens 8192 --output-tokens 128 --seed 1729 \
  --out runs/issue44-mi355x-20260923/workload
```

The private `benchmark-plan-qualified.json` declares ordered 0.5 and 1 request/s points, 200 requests each. It points to the qualified fleet copy and records the pinned benchmark client revision. Both points use `load_trial.py run` with `--ttft 10 --tpot 0.3 --attainment 0.95 --timeout 180`; the runner substitutes the router URL, model, and point output directory. Execute the plan from the router host:

```bash
.venv/bin/python tools/measurement/benchmark_runner.py \
  --base http://127.0.0.1:8000 \
  --model moonshotai/Kimi-K3 \
  --plan runs/issue44-mi355x-20260923/benchmark-plan-qualified.json \
  --out runs/issue44-mi355x-20260923/benchmark
```

## Measured points

Both points completed with a ready router, an idle initial state, and an idle post-point drain. The table excludes one completed warmup request per point; the client and router journal each recorded 201 terminal requests including that warmup.

| Offered rate | Completed rate including drain | Output throughput including drain | TTFT p50 / p95 / p99 | TPOT p50 / p95 / p99 | Result |
| --- | --- | --- | --- | --- | --- |
| 0.5 request/s | 0.460 request/s | 58.9 tokens/s | 5.258 / 7.298 / 7.407 s | 257.5 / 258.4 / 259.3 ms | 200/200 within limits |
| 1 request/s | 0.840 request/s | 107.5 tokens/s | 5.572 / 7.314 / 7.367 s | 258.7 / 259.9 / 260.5 ms | 200/200 within limits |

The router shifted capacity toward decode during the lower-rate point and held the resulting allocation through the higher-rate point. The private evidence retains role history, engine identities, and exact allocation. Both intervals have complete scrape coverage and matching client and journal outcome counts; the state timeline records each role change. Grafana dashboard coverage begins partway through the lower-rate point. That point records one `counter_missing` diagnostic for `narwhal_flips_total`, whose time series first appeared after the first role change. The higher-rate point passed all collector checks. A post-load [KV ring check](../cli/Check.md), covering role-permitted transfers between engines, passed after the second drain.

The retained private artifact bundle is `runs/issue44-mi355x-20260923/qualification-artifacts.tgz` (SHA-256 `5a3dbb86d0a361637b55014bbf5b03a25ffb72eaffd93716107753978ffb489c`). It contains the client records, router journal, per-point evidence, metric samples, profile store and samples, preflight reports, post-load check, workload, and qualified inputs. The run left the deployment and monitoring services running for inspection.
