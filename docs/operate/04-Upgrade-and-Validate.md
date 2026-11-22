---
description: Roll out and roll back Narwhal upgrades with compatible state handoff versions.
---

# Upgrade and rollback

Upgrade the standby, hand traffic to it, then upgrade the former active router, so one router admits traffic throughout. This needs both releases to write the same [handoff version](01-Start-Routers.md#keeping-one-deployment-set), because a router reads a handoff only when its `schema_version` equals the version its own build writes. When the versions differ, use the maintenance window below.

## Rolling upgrade with compatible handoff versions

### Upgrading the standby

1. Record the active router's `ha.epoch` from `/narwhal/state`.
2. Stop the standby and install the new deployment set on its host.
3. Start the upgraded router as standby.
4. Check that its `/health` returns HTTP 200 and its `/ready` returns HTTP 503.

### Handing off to the upgraded router

1. Stop the old active router gracefully.
2. Check that the upgraded router's `ha.epoch` exceeds the epoch you recorded in [Upgrading the standby](#upgrading-the-standby).
3. Check that it is the only backend returning HTTP 200 from `/ready`.

### Upgrading the former active router

1. Install the same deployment set on the stopped router's host.
2. Start that router as standby and wait for its `/ready` to return HTTP 503.

## Upgrading across a handoff-version change

A router rejects a handoff whose `schema_version` differs from its own, whether older or newer. A standby that polls such a handoff from the primary ends its shadow loop and cannot take over, and a router that resumes from a saved handoff of another version fails at startup. The upgrade therefore needs a maintenance window.

1. Stop ingress, then stop both routers.
2. Install the same deployment set on both hosts.
3. Start the intended primary, then its standby.
4. Confirm that the primary holds the lease and is the only backend returning HTTP 200 from `/ready`.
5. Restore ingress.

## Rolling back

1. Remove the router you are rolling back from the load balancer, then stop the new router gracefully.
2. Inspect the rollback build's contract support:

    ```bash
    narwhal-check --print-contract-versions
    ```

3. Restore these as one unit:
    - the code
    - the configuration
    - the profiles
    - the first-token calibration artifact for the live process generations
    - a state handoff the restored build supports
4. Run preflight. If its `profile` or `calibration` gate reports a changed process generation, regenerate the profiles and the [first-token calibration](../deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration).
5. To start with fresh roles and zeroed counters, set `recovery.resume: false` and drop `--resume`.
6. Confirm that exactly one router holds the lease: the rollback router or its fenced peer.
7. Start the rollback build and check that `/health` reports `status: ok` and `/ready` returns HTTP 200.
8. Check that roles and cumulative counters are present, then send one completion request and confirm it succeeds.
9. Return the router to service, then restore its standby.
