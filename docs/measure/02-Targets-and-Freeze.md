# Targets and deployment freeze

With accepted profiles in place, you can pick latency targets and then lock everything else down, so the load test only varies one thing: the offered rate.

## 5. Set production SLOs

Run light traffic against the fleet and look at the latency distribution. Set `slo.ttft_s` and `slo.tpot_s` from that distribution and the service requirement.

Check the TPOT target against the engine shape's measured per-token floor. A target below that floor leaves the fleet with zero feasible decode capacity.

After any SLO change, run `narwhal-check` on the edited fleet. It tests the new target against the saved profiles and live handoffs:

```bash
narwhal-check --fleet config/fleet.production.json
```

### Pace gate

One of the checks looks for an engine that's much slower than the rest. With three or more successful probes, it compares each engine with the fleet median and fails any engine more than 1.5x slower. An engine with a saved prefill profile is also checked against that profile, with the same 1.5x limit. That check needs exact `usage.prompt_tokens` from the engine. With only one or two probes there's no useful median, so the profile check is the only one, and an engine without a saved profile is skipped.

## 6. Freeze the deployment under test

Before the load test, assign a deployment identifier and attach everything needed to reproduce or audit the run:

- **Software:** Narwhal release, source revision, distribution digest, engine image digest, and engine launcher.
- **Configuration:** fleet, router, and engine configuration, the profile files, the sample store, and the attestation documents.
- **Checks:** preflight output and endpoint captures.
- **Run output:** deployment-client output, router journal, state snapshots, and metrics.

Record the workstation host, router host, and SSH tunnel mapping under the same identifier.

Across the sweep, only the request rate changes. Hold the source revision, model, runtime, profiles, router targets, workload shape, cache policy, and both latency targets fixed. Between rates, let resident work and outstanding transfer leases drain.

Stop the sweep at the first rate that misses the [trial's attainment target](03-Load-Trial.md#7-run-the-synthetic-deployment-trial) with a valid client schedule, or once you've tested the highest rate you intend to run at. A miss with an invalid client schedule tells you about the client rather than the fleet. The next page explains how to tell the two apart.

Next: [the synthetic load trial](03-Load-Trial.md).
