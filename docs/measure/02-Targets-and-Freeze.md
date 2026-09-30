# Targets and deployment freeze

## 5. Set production SLOs

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

The `narwhal-check` `slo` gate passes when both targets are feasible against the saved profiles.

### Pace gate

| Successful probes | Each engine passes with |
| --- | --- |
| Three or more | Pace within `1.5x` of the fleet median |
| One or two | A saved prefill profile, exact `usage.prompt_tokens`, and pace within `1.5x` of the profile prediction |

## 6. Freeze the deployment under test

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

Held fixed across the offered-rate sweep: source revision, model, runtime, profiles, router targets, workload shape, cache policy, and both latency targets.

Before the next rate, wait for:

* resident work to finish
* transfer leases to release

Stop the sweep at the first of:

* a run that misses the [trial's attainment target](03-Load-Trial.md#7-run-the-synthetic-deployment-trial)
* a tested rate at the intended operating ceiling

Next: [synthetic load trial](03-Load-Trial.md).
