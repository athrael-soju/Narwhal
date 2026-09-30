# Upgrade and rollback

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

#### Upgrade the standby

1. Check that both releases share a [handoff version](01-Start-Routers.md#2-keep-one-deployment-set).
2. Record the active router's `ha.epoch` from `/narwhal/state`.
3. Stop the standby.
4. Install the new deployment set on that host.
5. Start the upgraded router as standby.
6. Check that `/health` returns HTTP 200 on the new standby.
7. Check that `/ready` returns HTTP 503 on the new standby.

#### Hand off to the upgraded router

1. Stop the old active router gracefully.
2. Verify the upgraded router's `ha.epoch` exceeds the epoch recorded in [Upgrade the standby](#upgrade-the-standby).
3. Check that it is the only backend returning HTTP 200 from `/ready`.

#### Upgrade the former active router

1. Install the same deployment set on the stopped router's host.
2. Start that router as standby.
3. Wait for its `/ready` to return HTTP 503.

### 10.2 Upgrade across a handoff-version change

Incompatible handoff versions need a maintenance window.

1. Stop ingress.
2. Stop both routers.
3. Install the same deployment set on both hosts.
4. Start the intended primary.
5. Start its standby.
6. Confirm the primary holds the lease.
7. Confirm the primary is the only backend returning HTTP 200 from `/ready`.
8. Restore ingress.

### 10.3 Roll back

1. Remove the router you are rolling back from the load balancer.
2. Stop the new router gracefully.
3. Inspect the rollback build's contract support:

    ```bash
    narwhal-check --print-contract-versions
    ```

4. Restore these as one unit:
    - the code
    - the configuration
    - the profiles
    - the first-token calibration artifact for the live process generations
    - a state handoff the restored build supports
5. If process generations changed, regenerate the profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before preflight.
6. For fresh roles and zeroed counters, set `recovery.resume: false` and drop `--resume`.
7. Confirm exactly one router holds the lease: the rollback router or its fenced peer.
8. Start the rollback build.
9. Check that `/health` reports `status: ok`.
10. Check that `/ready` returns HTTP 200.
11. Check that roles and cumulative counters are present.
12. Send one completion request and confirm it succeeds.
13. Return the router to service.
14. Restore its standby.
