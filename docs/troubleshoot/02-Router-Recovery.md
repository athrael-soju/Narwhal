# Router failover and rollback

## Router failover

### Primary router failed

1. Query `/ready` and `/narwhal/lifecycle` on both routers. One of them should hold the lease and report `router.controls_fleet: true`. As soon as that router's `/ready` returns HTTP 200, point the load balancer at it.

2. Compare the active router with the last persisted handoff before you trust it:

   - Its lease epoch should be higher than the failed primary's last epoch.
   - Its roles should match the handoff.
   - Its cumulative counters should match the handoff.

3. Bring the old primary back as a standby:

   ```text
   --standby-of <active-router>
   ```

   It should return HTTP 503 from `/ready` and refuse a completion request sent to it directly. If it accepts one, it isn't running as a standby.

### Both routers return 503

Each router reports a refusal reason. Use it to find the fault. If the cause is lease storage or clock bounds, repair it. Do not delete the lease or unfence a router to restore traffic, because this can produce two active primaries. Recovery is complete when exactly one router is active.

### Handoff is stale or incompatible

A standby holds `/ready` at HTTP 503 for these reasons:

- `no fresh handoff`: the handoff from the previous lease epoch has expired, or the previous router exited before it wrote one.
- The handoff has an incompatible contract version or the wrong epoch.

Keep client traffic stopped. Restore a router release and a state set that are compatible with each other.

If the handoff cannot be recovered, schedule a maintenance window and start a single router from its configured opening roles. Before you route traffic to it, confirm that the previous router process is stopped or fenced.

## Router rollback

1. Take the target router out of the load balancer.
2. Stop it cleanly if you can, so it writes a complete handoff.
3. Check which contract versions the rollback build supports:

   ```bash
   narwhal-check --print-contract-versions
   ```

4. Restore the configuration, profiles, and a handoff whose contract version the rollback build supports.

   To start from the configured opening roles and reset cumulative counters, set `recovery.resume` to `false` in the fleet config and start the router without `--resume`.

5. Before it serves traffic, confirm that fleet control belongs to the rollback router or to its HA peer.
   <!-- TODO: The original said "its fenced HA peer". A fenced router shouldn't control the fleet, so confirm what this check is meant to verify. -->

6. Start the rollback build and check its health, readiness, roles, cumulative counters, and one completion request. Roles and counters should match the handoff you restored, unless you set `recovery.resume` to `false`.

7. Once those checks pass, put it back in service. Restore its standby after the active router is stable.
   <!-- TODO: Define "stable" (time window, error rate, or specific signal). -->

Then run the [post-recovery drills](../Troubleshoot.md#after-recovery).
