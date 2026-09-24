# Targets and deployment freeze

## 5. Set production SLOs

Set `slo.ttft_s` and `slo.tpot_s` from the service requirement and measured profile samples. The subsequent workload trial supplies client-path latency distributions for acceptance or a revised target.

A TPOT target below the measured per-token floor of the engine shape yields zero feasible decode capacity.

Use the final targets in the preflight at deployment freeze below. Changing either target keeps the measured engine profiles valid while the engine generations and workload domain stay the same.

### Pace gate

With at least three successful probes, the pace gate compares each engine with the fleet median under a `1.5x` slowdown limit. For one or two successful probes, it requires a saved prefill profile and exact `usage.prompt_tokens` for each engine to apply the same limit.

## 6. Freeze the deployment under test

For a standalone measurement, run the full preflight against the final fleet before sending measured traffic:

```bash
narwhal-check --fleet config/fleet.production.json
```

The initial deployment uses the [Gate F preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) when the engine processes, profile path, fleet document, and targets match. After changing an input, run preflight against its final value.

Before the load test, assign a deployment identifier and attach the exact:

* Narwhal release;
* source revision;
* distribution digest;
* fleet configuration;
* profile files;
* sample store;
* engine image digest;
* engine launcher;
* attestation documents;
* router configuration;
* engine configuration;
* preflight output;
* endpoint captures;
* deployment-client output;
* router journal;
* state snapshots;
* metrics.

Record the workstation host, router host, and SSH tunnel mapping under the same identifier.

Across the offered-rate sweep, vary the request rate while holding these inputs fixed:

* source revision;
* model;
* runtime;
* profiles;
* router targets;
* workload shape;
* cache policy;
* TTFT target;
* TPOT target.

Between rates, drain resident work and transfer leases.

End the sweep at the first candidate attainment miss or after testing the intended operating ceiling.

Continue with the [synthetic load trial](03-Load-Trial.md).
