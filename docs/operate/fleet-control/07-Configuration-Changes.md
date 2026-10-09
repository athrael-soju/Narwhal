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

An overlay may change the `slo`, `controller`, `serving` and `recovery` sections, plus keys that start with `_`. [Serving and role control](../../configuration/02-Serving-and-Role-Control.md) and [Recovery and validation](../../configuration/03-Recovery-and-Validation.md) define those sections' fields.

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

**Restore baseline** undoes the session's changes and leaves everything else running:

- If the configuration differs from the baseline, the service restarts the router with the baseline through the `router_restart` hook.
- It resumes each paused engine, starts each stopped engine and readmits each drained engine.

A session that changed nothing restores nothing. The session stays open.

## Router readiness

After an overlay, a cold restart or a configuration restore, the service polls the router's `GET /ready` every second until it returns HTTP 200. When `router.timeout_s` passes first, the action fails with HTTP 504.

If the `router_restart` hook succeeds but the router misses that deadline, the overlay still applies to the session.
