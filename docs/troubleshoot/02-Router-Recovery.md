# Router failover and rollback

## Router failover

### Primary router failed

1. Query `/ready` and `/narwhal/lifecycle` on both routers.
2. Identify the lease holder: its `/narwhal/lifecycle` reports `router.controls_fleet: true`.
3. Wait for the lease holder's `/ready` to return HTTP 200.
4. Send load-balancer traffic to that router.
5. Check the active router against the last persisted state handoff:
    - Its lease epoch is higher than the failed primary's last epoch.
    - Its roles match the handoff.
    - Its cumulative counters match the handoff.
6. Add `--standby-of <active-router>` (the active router's URL) to the old primary's launch command.
7. Restart the old primary.

The recovered standby:

- returns HTTP 503 from `/ready`.
- rejects direct completion requests.

When both routers return HTTP 503 from `/ready`, read the refusal reasons:

| Refusal reason | Action |
| --- | --- |
| Lease storage or clock bound | Repair the storage or clock, keeping the lease holder and fencing in place. |
| Any other reason | Fix what the reason names. |

### State handoff is stale or incompatible

| Cause | Standby `/ready` |
| --- | --- |
| The previous lease epoch's state handoff expired, or the previous router exited before persisting one | HTTP 503, reason `no fresh handoff` |
| Contract version mismatch or wrong epoch in the state handoff | HTTP 503 |

1. Keep client traffic stopped.
2. Restore a compatible router release.
3. Restore the [deployment set](../operate/01-Start-Routers.md#2-keep-one-deployment-set).

If handoff restoration fails:

1. Open a maintenance window.
2. Set `recovery.resume: false` in the fleet configuration.
3. Drop `--resume` and `--standby-of` from one router's launch command.
4. Start that router from its opening roles.
5. Wait until the old router has stopped or fenced itself.
6. Admit traffic.

## Router rollback

1. Follow [Roll back](../operate/04-Upgrade-and-Validate.md#103-roll-back).
2. Run the drills in [Validate every release](../operate/04-Upgrade-and-Validate.md#11-validate-every-release).
