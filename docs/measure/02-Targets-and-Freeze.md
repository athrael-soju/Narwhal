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

`narwhal-check` runs the [pace gate](../deploy/06-Profile-and-Preflight.md#pace-gate) and tests the targets against saved profiles and live handoffs.

### Pace gate

| Successful probes | Each engine passes with |
| --- | --- |
| Three or more | Pace within `1.5x` of the fleet median |
| One or two | A saved prefill profile, exact `usage.prompt_tokens`, and pace within `1.5x` of the profile prediction |

## 6. Freeze the deployment under test

1. Assign a deployment identifier before the load test.
2. Attach the exact artifacts to it:
    * the Narwhal release, source revision, and distribution digest;
    * the fleet, router, and engine configuration;
    * the profile files and sample store;
    * the engine image digest, engine launcher, and attestation documents;
    * the preflight output, endpoint captures, deployment-client output, router journal, state snapshots, and metrics.
3. Record the hosts and SSH tunnel mapping with the identifier.

Vary only the request rate across the offered-rate sweep. Keep the source revision, model, runtime, profiles, router targets, workload shape, cache policy, and both latency targets fixed.

Between rates, wait for resident work to finish and transfer leases to release.

Stop the sweep at the first of:

* a run that misses the [trial's attainment target](03-Load-Trial.md#7-run-the-synthetic-deployment-trial)
* a tested rate at the intended operating ceiling

Next: [synthetic load trial](03-Load-Trial.md).
