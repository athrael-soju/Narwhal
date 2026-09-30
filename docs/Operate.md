# Narwhal production operations

Each model fleet is served by a router pair that shares one lease domain. Whichever router holds the lease has admission and placement authority, and the other waits on standby until it's needed.

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

Work through this list for any new or replaced production deployment:

1. Assemble one release, fleet configuration, profile store, and evidence set.
2. Check router handoff compatibility with `narwhal-check --print-contract-versions`.
3. Keep the control interfaces private and set up trusted ingress rewriting.
4. Make sure both router hosts share the required lease domain.
5. Start the intended primary.
6. Start its standby from the same deployment set.
7. Check lease ownership with `/ready` and process and fleet state with `/health`.
8. Send the deployment workload through production ingress.
9. Confirm the dashboard is collecting and the paging thresholds are set.
10. Run whichever engine lifecycle procedure your restart policy allows.
11. Run router failover through the production load balancer.
12. Open client admission.

If something goes wrong, [Troubleshoot a fleet](Troubleshoot.md) has the failure procedures. [Measure a fleet](Measure.md) covers production evidence and deployment validation.
