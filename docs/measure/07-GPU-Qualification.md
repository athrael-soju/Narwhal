---
description: All Kimi-K3 requests at 0.5 and 1 request/s on a Narwhal GPU fleet met the 10 s TTFT and 0.3 s TPOT limits.
---

# Kimi-K3 GPU benchmark qualification

At 0.5 and 1 request/s, all 200 `moonshotai/Kimi-K3` requests at each rate stayed within the 10 s time to first token (TTFT) and 0.3 s time per output token (TPOT) limits.

The private run bundle in `runs/<qualification-run>/` holds the deployment inputs and the raw evidence from each stage.

## Pinned inputs

| Input | Value |
| --- | --- |
| Narwhal router source | `6c6c7da4c879101d4f353da1590aa32ce2bf7c10` |
| Benchmark client (load trial helper) source | `31b0b78e0d8b1438b212ae56b9fbba32832b53a2` |
| Model | `moonshotai/Kimi-K3` |
| Engine | vLLM `0.29.0` |
| Engine image | `sha256:9eacf87e93ecffcb910802d0d0505ef3c9b753a66fb09bec304d72d8dac1dbc2` |
| Accelerator per engine | Eight GPUs, tensor parallelism 8 |
| Checkpoint weights | 96 safetensors shards |
| Sorted `path:sha256` shard manifest SHA-256 | `6cd00d6ba5817a868738202c91b977534668c42d89fce3b317340de88ea9d2ed` |
| Model config SHA-256 | `9710e121a58d03ac92c8d6da287a19541994319afbbe6d6202af001ffd379213` |
| Fleet latency budgets | 10 s TTFT, 0.3 s TPOT |
| Input and output length | 8,192 input tokens, 128 output tokens |
| Sampling | Seed 1729, temperature 0 |
| Prefix caching | Disabled |
| Prompt pool | 28 returned prompt IDs, 20 distinct |
| Profile store SHA-256 | `8a6ab49148a0eed646863b9f4fc3fc82bf0b3584bb535c9cd7d506c957ed3bc6` |
| Qualified fleet SHA-256 | `7a4e4583933bff496d11a608a2544deac48d2d8843402861ce8015568d5e3d72` |
| Benchmark plan SHA-256 | `43a8755d37383687f5ef639c5c16a095f5cc0feaf02b3d0821b1297f319f79b3` |
| Workload SHA-256 | `55885dd1d9b42a4debf7b01230bbb5c917334689e867d5ca08a66edfb73b80e3` |
| Launch check | Checkpoint manifests and the live launch check agree on every shard and the model config hash on every selected host |

| Profile measurement | Value |
| --- | --- |
| Median live prefill, 8,192 tokens | About 0.92 s |
| Decode intercepts | 0.2416 to 0.2426 s/token |

| Timeout measurement | Value | Result |
| --- | :---: | --- |
| Packaged first-token deadline | 2.5 s | Rejected valid KV handoffs between engines |
| Wider-window direct probe first-token latency | 0.348 to 5.274 s | Completed on every permitted path |
| Qualified fleet copy `engine.first_token_timeout_s`, the only changed field | 8.5 s | About 0.6 s of the 10 s TTFT budget remains after prefill |

Full preflight against the same engine containers and profiled process generations that served the benchmark passed all nine gates.

## Procedure

1. Load the private deployment environment.
2. Prepare the pinned package with `tools/deployment/deploy_hosts.py`.
3. Install the pinned package with `tools/deployment/deploy_hosts.py`.
4. Start each engine from its checked launch plan.
5. Capture each engine's live cache and NIXL contract.
6. Start each engine's attestation sidecar.
7. Finalize the fleet on the router host:

    ```bash
    .venv/bin/python tools/deployment/attestation_contract.py finalize-fleet \
      --fleet runs/deployment/fleet.json
    ```

8. Profile the fleet on the router host:

    ```bash
    .venv/bin/narwhal-profile \
      --fleet runs/deployment/fleet.json \
      --limits runs/deployment/profiling-limits.json \
      --prefill-lens 256,1024,4096,8192 \
      --decode-input-lens 512,4096,8192 \
      --decode-concurrency 1,2,4 \
      --decode-tokens 64 \
      --prefill-repeats 3
    ```

9. Run preflight on the router host:

    ```bash
    .venv/bin/narwhal-check --fleet runs/<qualification-run>/fleet-qualified.json
    ```

10. Start the router:

    ```bash
    .venv/bin/narwhal-serve --fleet runs/<qualification-run>/fleet-qualified.json \
      --host 127.0.0.1 --port 8000 \
      --journal runs/<qualification-run>/router-journal.jsonl
    ```

11. Update the benchmark helper in the router-host checkout to the pinned client revision.
12. Prepare the workload:

    ```bash
    .venv/bin/python tools/measurement/load_trial.py prepare \
      --base http://127.0.0.1:8000 \
      --input-tokens 8192 --output-tokens 128 --seed 1729 \
      --out runs/<qualification-run>/workload
    ```

13. Run the plan from the router host:

    ```bash
    .venv/bin/python tools/measurement/benchmark_runner.py \
      --base http://127.0.0.1:8000 \
      --model moonshotai/Kimi-K3 \
      --plan runs/<qualification-run>/benchmark-plan-qualified.json \
      --out runs/<qualification-run>/benchmark
    ```

The private `benchmark-plan-qualified.json` sets:

| Setting | Value |
| --- | --- |
| Points | 0.5 request/s, then 1 request/s |
| Requests per point | 200 |
| Fleet | The qualified fleet copy |
| Client revision | The pinned benchmark client revision |
| Client command | `load_trial.py run` with `--ttft 10 --tpot 0.3 --attainment 0.95 --timeout 180` |
| Runner substitutions | Router URL, model, and point output directory |

## Measured points

- Each point started with a ready, idle router and ended idle.
- The client and journal each logged 201 terminal requests per point: 200 measured requests and one unscored warmup.

| Offered rate | Completed rate including drain | Output throughput including drain | TTFT p50 / p95 / p99 | TPOT p50 / p95 / p99 | Result |
| --- | --- | --- | --- | --- | --- |
| 0.5 request/s | 0.460 request/s | 58.9 tokens/s | 5.258 / 7.298 / 7.407 s | 257.5 / 258.4 / 259.3 ms | 200/200 within limits |
| 1 request/s | 0.840 request/s | 107.5 tokens/s | 5.572 / 7.314 / 7.367 s | 258.7 / 259.9 / 260.5 ms | 200/200 within limits |

| Observation | 0.5 request/s | 1 request/s |
| --- | --- | --- |
| Role allocation | Router shifted capacity toward decode | Router held the allocation from the 0.5 request/s point |
| Collector scrape coverage | Whole run | Whole run |
| Collector client and journal counts | Matched | Matched |
| Collector role timeline | Every role change | Every role change |
| Collector diagnostics | One `counter_missing` for `narwhal_flips_total`, a series the pinned router source first exported at the first role change | Zero |

Grafana dashboard coverage begins partway through the 0.5 request/s point.

A post-load KV ring check with [`narwhal-check`](../cli/Check.md) passed for role-permitted transfers between engines on the drained router.

The private artifact bundle `runs/<qualification-run>/qualification-artifacts.tgz` (SHA-256 `5a3dbb86d0a361637b55014bbf5b03a25ffb72eaffd93716107753978ffb489c`) holds the client records, router journal, per-point evidence, and qualified inputs.
