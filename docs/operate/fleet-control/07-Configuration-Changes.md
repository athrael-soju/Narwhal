---
description: Apply a configuration overlay to a running fleet, cold-restart it, or restore the baseline configuration from the fleet control console.
---

# Overlays, cold restarts and restores

The **Configuration** view shows the session's baseline file and, under **Changed from baseline**, each setting in the current configuration that differs from the baseline.

Overlays, cold restarts and restores are [exclusive actions](04-Sessions.md#exclusive-actions).

## Configuration overlays

An overlay is a partial fleet document that the service merges onto the current fleet configuration as a JSON merge patch (RFC 7386):

- An object merges key by key.
- `null` removes a key, which returns the field to its default.
- Any other value, arrays included, replaces the current value.

An overlay may change the `slo`, `controller`, `serving` and `recovery` sections, with the fields in [Fleet schema](../../configuration/01-Fleet-Schema.md#minimal-fleet-definition), [Serving and role control](../../configuration/02-Serving-and-Role-Control.md) and [Recovery and validation](../../configuration/03-Recovery-and-Validation.md), plus keys that start with `_`.

For example, this overlay adds 10% of the TTFT target to the admission budget:

```json
{"serving": {"admission_margin": 0.1}}
```

### Apply an overlay

Applying an overlay requires the `router_restart` hook.

1. Enter the overlay in the **Configuration** view.
2. Select **Check overlay**. This dry run lists each setting the overlay changes, with its current and new value, or the validation errors.
3. Select **Apply overlay** and confirm.

The service validates the merged configuration as `narwhal config validate` does, saves it in the session directory, and restarts the router with it through the `router_restart` hook.

## Cold restart

**Cold restart** runs the `cold_restart` hook with the current fleet configuration file.

## Restore the baseline

Select **Restore baseline** to return the fleet to the state the session started from. For membership, that state is the set of engines the baseline fleet configuration names, so restore also removes an engine that joined before the session and returns a baseline engine that left before it. Ending a session keeps its changes, so restore before you end it.

Restore baseline is an [exclusive action](04-Sessions.md#exclusive-actions) and undoes the session's changes in this order:

1. If the current configuration differs from the baseline, the service restarts the router with the baseline through the `router_restart` hook and waits for readiness, as in [Router readiness](#router-readiness).
2. For each member engine the session paused, the service runs the `engine_resume` hook. For each member engine it stopped, the service runs the `engine_start` hook.
3. For each member engine the session drained, the service calls the router's readmit.
4. For each engine the baseline configures that is not in the router's membership, the service runs the `engine_start` hook when it is configured, then joins the engine with its baseline entry, with each `${VARIABLE}` endpoint resolved from the service's environment as at session start.
5. For each member engine the baseline does not configure, the service removes it from the router's membership.

For step 4, set each `${VARIABLE}` that a returning engine's endpoints use to the value the router uses, in the service's environment. For an engine of step 4, the `engine_start` hook runs even when the engine's process still runs, so it must succeed for a running engine. The router refuses a returning engine unless its profile store holds a profile for exactly the members and that engine, as in [Joining and removing engines](../08-Joining-and-Removing-Engines.md#profile-the-engine). After a removal, the store keeps the removed engine's profiles until you [update it](../08-Joining-and-Removing-Engines.md#update-the-fleet-file-and-the-profile-store).

Restore baseline does not update the profile store. After step 5 removes an engine, the router refuses a later join of any other engine with `stale profiles for engines outside the proposed set: <iid>` until you update the store without that engine, as in [Update the fleet file and the profile store](../08-Joining-and-Removing-Engines.md#update-the-fleet-file-and-the-profile-store).

After step 1, the service reads the router's `GET /narwhal/engines` and compares it with the baseline to find the engines of steps 4 and 5. With a router that has no such route, it uses the joins and leaves the session recorded.

The run record holds each step as it completes. When a step fails, the run record holds that step too, and the restore fails with its status. Select **Restore baseline** again to continue from the fleet's current state.

## Router readiness

After an overlay, a cold restart or a configuration restore, the service polls the router's `GET /ready` every second until it returns HTTP 200. When `router.timeout_s` passes first, the action fails with HTTP 504.

If the `router_restart` hook succeeds but the router misses that deadline, the overlay still applies to the session.
