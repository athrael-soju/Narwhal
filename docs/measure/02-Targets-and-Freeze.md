---
description: Set production TTFT and TPOT SLOs and freeze the Narwhal deployment under test.
---

# Targets and deployment freeze

## 5. Setting production SLOs

| Field        | Target                       |
| ------------ | ---------------------------- |
| `slo.ttft_s` | Time to first token (TTFT)   |
| `slo.tpot_s` | Time per output token (TPOT) |

1. Run light traffic with accepted profiles.
2. Set both targets from the service requirement and the latency distribution measured in step 1.
3. Validate the config:

    ```bash
    narwhal-check --fleet config/fleet.production.json
    ```

A TPOT target below the engine's measured per-token time gives zero feasible decode capacity.

The `narwhal-check` `slo` gate passes each engine when:

| Target | Condition |
| --- | --- |
| `slo.tpot_s` | At or above the profile's token interval at the smallest measured decode cohort |
| `slo.ttft_s` | Above the profile's single-token prefill time |

### Pace gate

| Check | Applies to | Passes with |
| --- | --- | --- |
| Fleet median | Every engine, when three or more probes succeed | Pace within `1.5x` of the fleet median |
| Saved profile | Every engine with a saved prefill profile | Exact `usage.prompt_tokens` and pace within `1.5x` of the profile prediction |

With one or two successful probes, the pace gate requires a saved prefill profile for each engine.

## 6. Freezing the deployment under test

1. Assign a deployment identifier before the load test.
2. Attach the exact artifacts to it:

    | Artifact group | Items |
    | --- | --- |
    | Release | Narwhal release, source revision, distribution digest |
    | Configuration | fleet configuration, router configuration, engine configuration |
    | Engine inputs | profile files, sample store, engine image digest, engine launcher |
    | Deployment evidence | attestation documents, preflight output, endpoint captures, deployment-client output |
    | Run evidence | router journal, state snapshots, metrics |

3. Record the hosts and SSH tunnel mapping with the identifier.

The offered-rate sweep holds these fixed:

- source revision
- model
- runtime
- profiles
- router targets
- workload shape
- cache policy
- both latency targets

Before the next rate, wait for:

- resident work to finish
- transfer leases to release

Stop the sweep at the first of:

- a run that misses the [trial's attainment target](03-Load-Trial.md#7-running-the-synthetic-deployment-trial)
- a tested rate at the intended operating ceiling
