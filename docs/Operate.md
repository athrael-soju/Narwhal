---
description: Operate a Narwhal fleet with a warm-standby router pair in a shared lease domain.
---

# Operating Narwhal

Each model fleet runs one router pair in a shared lease domain. The lease holder admits and places requests, and its peer runs as standby.

```text
clients
  |
TLS, authentication, WAF, model routing
  |---------------- router pair A ---------------- fleet A, model A
  |---------------- router pair B ---------------- fleet B, model B
  `---------------- router pair C ---------------- fleet C, model C
```

## Operator tasks

<div class="grid cards" markdown>

-   [Production boundary and router pair](operate/01-Start-Routers.md)

    ---

    Set the production boundary, keep one deployment set, configure the client path, and start a router pair.

-   [Router state and placement monitoring](operate/02-Monitor.md)

    ---

    Interpret router state and monitor placement and control.

-   [Engine restart and process replacement](operate/03-Restart-Engines.md)

    ---

    Restart one engine or an engine wave, activate replacement profiles, and detect process replacement.

-   [Upgrade and rollback](operate/04-Upgrade-and-Validate.md)

    ---

    Upgrade routers across compatible or changed handoff versions and roll back.

-   [Release drills](operate/05-Release-Drills.md)

    ---

    Validate every release with restart, whole-wave, and failover drills.

-   [Controlling the fleet](operate/06-Controlling-the-Fleet.md)

    ---

    Run engine actions, configuration overlays, and load jobs from the control service and its console, and read each session's run record.

-   [Choosing admission, queue and retry settings](operate/07-Admission-Queue-and-Retry-Settings.md)

    ---

    Choose the admission mode, queue, retries, failure quarantine, in-flight limit and decode-gap limit from each default's evidence and client outcomes.

-   [Troubleshooting a fleet](Troubleshoot.md)

    ---

    Diagnose overload, engine failures, or router failures from the first visible symptom.

-   [Measuring a fleet](Measure.md)

    ---

    Profile the fleet, calibrate SLOs, and measure a target workload.

</div>

## Production startup checklist

For a new or replaced production deployment:

1. Assemble one [deployment set](operate/01-Start-Routers.md#2-keeping-one-deployment-set).
2. Confirm state handoff compatibility with `narwhal-check --print-contract-versions`.
3. Configure private control interfaces on the [client path](operate/01-Start-Routers.md#3-configuring-the-client-path).
4. Configure trusted ingress rewriting on the client path.
5. Confirm both router hosts share the required [lease domain](operate/01-Start-Routers.md#4-starting-a-router-pair).
6. Start the intended primary.
7. Start its standby from the same deployment set.
8. Verify `/ready` returns HTTP 200 on the primary.
9. Verify `/ready` returns HTTP 503 on the standby.
10. Verify `/health` reports `status: ok` on the active router.
11. Run the deployment workload through production ingress.
12. Verify dashboard collection.
13. Verify paging thresholds.
14. Run the [engine restart drill](operate/05-Release-Drills.md#11-validating-every-release) for the configured restart policy.
15. Run the router failover drill through the production load balancer.
16. Open client admission.
