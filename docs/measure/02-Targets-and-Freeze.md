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

If the TPOT target is lower than the engine's measured per-token time, no decode capacity is feasible.

`narwhal-check` tests the targets against saved profiles and live handoffs. It also runs the [pace gate](../deploy/06-Profile-and-Preflight.md#pace-gate), which compares each engine with the fleet median under a `1.5x` slowdown limit.

### Pace gate

With three or more successful probes, the gate compares each engine with the fleet median. With one or two, each engine also needs a saved prefill profile and exact `usage.prompt_tokens`.

## 6. Freeze the deployment under test

Assign a deployment identifier before the load test and attach the exact artifacts:

* the Narwhal release, source revision, and distribution digest;
* the fleet, router, and engine configuration;
* the profile files and sample store;
* the engine image digest, engine launcher, and attestation documents;
* the preflight output, endpoint captures, deployment-client output, router journal, state snapshots, and metrics.

Record the hosts and SSH tunnel mapping with the identifier.

Vary only the request rate across the offered-rate sweep. Keep the source revision, model, runtime, profiles, router targets, workload shape, cache policy, and both latency targets fixed.

Between rates, let resident work finish and release transfer leases.

Stop the sweep when a run misses the [trial's attainment target](03-Load-Trial.md#7-run-the-synthetic-deployment-trial). Also stop once you have tested the intended operating ceiling.

Next: [synthetic load trial](03-Load-Trial.md).
