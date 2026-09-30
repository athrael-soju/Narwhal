# Targets and deployment freeze

This page sets the latency targets (SLOs) and freezes the deployment before the load test.

## 5. Set production SLOs

Run light traffic against the fleet and read the TTFT and TPOT distribution. Set `slo.ttft_s` and `slo.tpot_s` from that distribution and the service requirement.

Compare the TPOT target with the engine shape's measured per-token floor. A TPOT target below that floor is unreachable and leaves the fleet with no feasible decode capacity.

After any SLO change, run `narwhal-check` on the edited fleet. It tests the new target against the saved profiles and live handoffs:

```bash
narwhal-check --fleet config/fleet.production.json
```

### Pace gate

The pace gate fails an engine that is more than 1.5x slower than the fleet median. It requires three or more successful probes. An engine with a saved prefill profile is also checked against that profile, with the same 1.5x limit. The profile comparison needs exact `usage.prompt_tokens` from the engine.

With one or two probes there is no median, so only the profile check runs. An engine without a saved profile is skipped.

## 6. Freeze the deployment under test

Before the load test, assign a deployment identifier and record these under it:

- Software: Narwhal release, source revision, distribution digest, engine image digest, and engine launcher.
- Configuration: fleet, router, and engine configuration, the profile files, the sample store, and the attestation documents.
- Checks: preflight output and endpoint captures.
- Run output: deployment-client output, router journal, state snapshots, and metrics.

Record the workstation host, router host, and SSH tunnel mapping under the same identifier.

Across the sweep, only the request rate changes. Hold the source revision, model, runtime, profiles, router targets, workload shape, cache policy, and both SLOs fixed. Between rates, let resident work and outstanding transfer leases drain.

Stop the sweep at the first rate that misses the [trial's attainment target](03-Load-Trial.md#7-run-the-synthetic-deployment-trial) with a valid client schedule, or after the highest planned rate. A miss with an invalid client schedule says nothing about the fleet. The next page explains how to tell the two apart.

Next: [the synthetic load trial](03-Load-Trial.md).
