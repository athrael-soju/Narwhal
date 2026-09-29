# Kimi-K3 GPU benchmark qualification

Narwhal served `moonshotai/Kimi-K3` on AMD Instinct MI355X engines at 0.5 and 1 request/s. All 200 requests at each rate stayed within the 10 s time to first token (TTFT) and 0.3 s time per output token (TPOT) limits.

A private run bundle holds the deployment inputs and the raw evidence from each stage. The bundle sits in `runs/<qualification-run>/`.

## Pinned inputs

| Input | Value |
| --- | --- |
| Narwhal router source | `6c6c7da4c879101d4f353da1590aa32ce2bf7c10` |
| Benchmark client (load trial helper) source | `31b0b78e0d8b1438b212ae56b9fbba32832b53a2` |
| Model | `moonshotai/Kimi-K3` |
| Engine | vLLM `0.29.0+rocm100`, image `sha256:9eacf87e93ecffcb910802d0d0505ef3c9b753a66fb09bec304d72d8dac1dbc2` |
| Accelerator per engine | AMD Instinct MI355X, eight GPUs, tensor parallelism 8 |
| Checkpoint weights | 96 safetensors shards; SHA-256 of the sorted `path:sha256` manifest is `6cd00d6ba5817a868738202c91b977534668c42d89fce3b317340de88ea9d2ed` |
| Model config SHA-256 | `9710e121a58d03ac92c8d6da287a19541994319afbbe6d6202af001ffd379213` |
| Fleet latency budgets | 10 s TTFT, 0.3 s TPOT; first-token deadline 8.5 s |
| Input and output length | 8,192 input tokens, 128 output tokens |
| Sampling | Seed 1729, temperature 0 |
| Prefix caching | Disabled |
| Prompt pool | 28 returned prompt IDs, 20 distinct |
| Profile store SHA-256 | `8a6ab49148a0eed646863b9f4fc3fc82bf0b3584bb535c9cd7d506c957ed3bc6` |
| Qualified fleet SHA-256 | `7a4e4583933bff496d11a608a2544deac48d2d8843402861ce8015568d5e3d72` |
| Benchmark plan SHA-256 | `43a8755d37383687f5ef639c5c16a095f5cc0feaf02b3d0821b1297f319f79b3` |
| Workload SHA-256 | `55885dd1d9b42a4debf7b01230bbb5c917334689e867d5ca08a66edfb73b80e3` |

The checkpoint manifests and the live launch check agree on every shard and the model config hash on every selected host.

Live prefill for 8,192 tokens took about 0.92 s at the median. Decode intercepts ran 0.2416 to 0.2426 s/token.

The packaged 2.5 s first-token deadline rejected valid KV handoffs between engines. Wider-window direct probes completed on every permitted path, with first-token latency from 0.348 to 5.274 s.

The qualified fleet copy changes only `engine.first_token_timeout_s`, to 8.5 s. That leaves about 0.6 s of the 10 s TTFT budget after prefill.

Full preflight passed all nine gates, run against the same engine containers and profiled process generations that served the benchmark.

## Procedure

1. Load the private deployment environment.
2. Prepare and install the pinned package with `tools/deployment/deploy_hosts.py`.
3. Start each engine from its checked launch plan.
4. Capture each engine's live cache and NIXL contract.
5. Start each engine's attestation sidecar.
6. Finalize, profile, and preflight on the router host:

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
    .venv/bin/narwhal-check --fleet runs/<qualification-run>/fleet-qualified.json
    ```

7. Start the router, then update the benchmark helper in the router-host checkout to the pinned client revision and prepare the workload:

    ```bash
    .venv/bin/narwhal-serve --fleet runs/<qualification-run>/fleet-qualified.json \
      --host 127.0.0.1 --port 8000 \
      --journal runs/<qualification-run>/router-journal.jsonl
    .venv/bin/python tools/measurement/load_trial.py prepare \
      --base http://127.0.0.1:8000 \
      --input-tokens 8192 --output-tokens 128 --seed 1729 \
      --out runs/<qualification-run>/workload
    ```

8. Run the plan from the router host:

    ```bash
    .venv/bin/python tools/measurement/benchmark_runner.py \
      --base http://127.0.0.1:8000 \
      --model moonshotai/Kimi-K3 \
      --plan runs/<qualification-run>/benchmark-plan-qualified.json \
      --out runs/<qualification-run>/benchmark
    ```

`benchmark-plan-qualified.json` (private) sets:

| Setting | Value |
| --- | --- |
| Points | 0.5 and 1 request/s, in order, 200 requests each |
| Fleet | The qualified fleet copy |
| Client revision | The pinned benchmark client revision |
| Client command | `load_trial.py run` with `--ttft 10 --tpot 0.3 --attainment 0.95 --timeout 180` |
| Runner substitutions | Router URL, model, and point output directory |

## Measured points

Each point started with a ready, idle router and ended idle. Each also ran one warmup request, which the table excludes, so the client and journal both logged 201 terminal requests.

| Offered rate | Completed rate including drain | Output throughput including drain | TTFT p50 / p95 / p99 | TPOT p50 / p95 / p99 | Result |
| --- | --- | --- | --- | --- | --- |
| 0.5 request/s | 0.460 request/s | 58.9 tokens/s | 5.258 / 7.298 / 7.407 s | 257.5 / 258.4 / 259.3 ms | 200/200 within limits |
| 1 request/s | 0.840 request/s | 107.5 tokens/s | 5.572 / 7.314 / 7.367 s | 258.7 / 259.9 / 260.5 ms | 200/200 within limits |

The router shifted capacity toward decode during the lower-rate point and held that allocation through the higher-rate point.
| Point | Collector result |
| --- | --- |
| Both | Scrapes covered the whole run, client and journal counts matched, and the timeline shows every role change |
| 0.5 request/s | One `counter_missing` diagnostic for `narwhal_flips_total`; the series appeared only after the first role change |
| 1 request/s | All collector checks passed |

Grafana dashboard coverage begins partway through the lower-rate point.

After the second drain, a post-load KV ring check with [`narwhal-check`](../cli/Check.md) passed for role-permitted transfers between engines.

The private artifact bundle at `runs/<qualification-run>/qualification-artifacts.tgz` has SHA-256 `5a3dbb86d0a361637b55014bbf5b03a25ffb72eaffd93716107753978ffb489c`. It holds the client records, router journal, per-point evidence, and the qualified inputs. The deployment and monitoring services stayed running for inspection.
