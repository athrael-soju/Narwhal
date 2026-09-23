# Targets and deployment freeze

## 5. Set production SLOs

After accepting the profiles, run light traffic and choose:

```text
slo.ttft_s
slo.tpot_s
```

from the service requirement and the measured latency distribution.

A TPOT target below the measured per-token floor of the engine shape yields zero feasible decode capacity.

Run preflight after changing either SLO:

```bash
narwhal-check --fleet config/fleet.production.json
```

### Pace gate

With at least three successful probes, the pace gate compares each engine with the fleet median.

For smaller fleets, every engine must instead have:

* a saved prefill profile;
* an exact `usage.prompt_tokens` count.

Both paths apply the same maximum slowdown:

```text
1.5x
```

## 6. Freeze the deployment under test

Run:

```bash
narwhal-check --fleet config/fleet.production.json
```

against the final fleet before sending measured traffic.

Assign a deployment identifier before the load test begins.

Bind that identifier to the exact:

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

During the offered-rate sweep, keep the following fixed:

* source revision;
* model;
* runtime;
* profiles;
* router targets;
* workload shape;
* cache policy;
* TTFT target;
* TPOT target.

Change only the offered request rate.

Between rates, drain resident work and transfer leases.

Stop after either:

* candidate attainment fails; or
* the intended operating ceiling has been tested.

Continue with the [synthetic load trial](03-Load-Trial.md).
