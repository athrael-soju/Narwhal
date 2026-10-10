---
description: Set production TTFT and TPOT SLOs and freeze the Narwhal deployment under test.
---

# Targets and deployment freeze

## Setting production SLOs

`slo.ttft_s` sets the time to first token (TTFT) target, and `slo.tpot_s` sets the time per output token (TPOT) target.

1. Run light traffic with accepted profiles.
2. Set both targets from the service requirement and the latency distribution measured in step 1.
3. Validate the config:

    ```bash
    narwhal-check --fleet config/fleet.production.json
    ```

A TPOT target below the engine's measured per-token time gives zero feasible decode capacity.

The `narwhal-check` `slo` gate passes each engine when `slo.tpot_s` is at or above the profile's token interval at the smallest measured decode cohort, and `slo.ttft_s` is above the profile's single-token prefill time.

The `narwhal-check` [pace gate](../deploy/06-Profile-and-Preflight.md#pace-gate) compares each engine's prefill pace with the fleet median, its saved profile, or both.

## Freezing the deployment under test

1. Assign a deployment identifier before the load test.
2. Attach the exact artifacts to it:

    - the Narwhal release, source revision, and distribution digest
    - the fleet, router, and engine configurations
    - the profile files, sample store, engine image digest, and engine launcher
    - the attestation documents, preflight output, endpoint captures, and deployment-client output
    - the router journal, state snapshots, and metrics

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

- a run that misses the [trial's attainment target](03-Load-Trial.md#running-the-synthetic-deployment-trial)
- a tested rate at the intended operating ceiling
