# Router failover and rollback

## Router failover

### Primary router failed

Query the private `/ready` endpoint on both routers.

Exactly one lifecycle document must report:

```text
router.controls_fleet: true
```

The load balancer must select only that router.

Compare the active router with the failed primary:

- the active router's lease epoch must be greater;
- its roles must match the last handoff;
- its cumulative counters must match the last handoff.

Restart the old primary as a standby:

```text
--standby-of <active-router>
```

The recovered process should return HTTP 503 from `/ready` and reject a direct completion request.

If both routers return HTTP 503, use their refusal reasons to determine the next repair. Lease-storage and clock-bound failures must be repaired while preserving lease ownership and fencing so recovery converges on one active primary.

### Handoff is stale or incompatible

A standby reports `no fresh handoff` when the previous lease epoch's handoff has expired or the previous router exited before persisting one.

The router also keeps `/ready` at HTTP 503 when the handoff has an incompatible contract version or incorrect epoch.

Keep client traffic stopped while restoring a compatible router release and state set.

When handoff recovery fails, start one router from its configured opening roles during a maintenance window.

Before admitting traffic, verify that the previous router process is stopped or fenced.

## Router rollback

Remove the target router from the load balancer.

Stop it cleanly where possible so it persists a complete handoff.

Inspect the rollback build's contract support:

```bash
narwhal-check --print-contract-versions
```

Restore configuration, profiles, and a handoff version that the rollback build can read.

For a rollback that starts from configured opening roles and resets cumulative counters, configure:

```yaml
recovery:
  resume: false
```

Before serving traffic, verify that fleet control belongs either to the rollback router or to its fenced HA peer.

Start the rollback build and check:

- health;
- readiness;
- roles;
- cumulative counters;
- one completion request.

Return it to service after those checks pass. Restore its standby after the active router is stable.

Run the [post-recovery drills](../Troubleshoot.md#after-recovery) after the router returns to service.
