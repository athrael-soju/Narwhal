# Operate Narwhal

Each model fleet is served by a router pair that shares one lease domain. The router that holds the lease has admission and placement authority. The other router is on standby.

Clients reach each fleet through one ingress layer and one router pair per model.

```text
clients
  |
TLS, authentication, WAF, model routing
  |---------------- router pair A ---------------- fleet A, model A
  |---------------- router pair B ---------------- fleet B, model B
  `---------------- router pair C ---------------- fleet C, model C
```

## Operator tasks

- [Production boundary and router pair](operate/01-Start-Routers.md)
- [Router state and placement monitoring](operate/02-Monitor.md)
- [Engine restart and process replacement](operate/03-Restart-Engines.md)
- [Upgrade, rollback, and release drills](operate/04-Upgrade-and-Validate.md)

## Production startup checklist

Run these steps for a new or replaced production deployment:

1. Pin one release, fleet configuration, profile store, and evidence set.
2. Verify router handoff compatibility with `narwhal-check --print-contract-versions`.
3. Restrict control interfaces to private networks and configure trusted ingress rewriting.
4. Confirm both router hosts share the same lease domain.
5. Start the intended primary.
6. Start its standby from the same deployment set.
7. Verify lease ownership with `/ready` and process and fleet state with `/health`.
8. Send the deployment workload through production ingress.
9. Confirm the dashboard is collecting and the paging thresholds are set.
10. Run the engine restart procedure your restart policy allows ([Restart engines](operate/03-Restart-Engines.md)).
11. Run router failover through the production load balancer.
12. Open client admission.

For failures, see [Troubleshoot a fleet](Troubleshoot.md). For production evidence and deployment validation, see [Measure a fleet](Measure.md).
