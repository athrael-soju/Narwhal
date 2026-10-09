---
description: Apply a configuration overlay to a running fleet, cold-restart it, or restore the baseline configuration from the fleet control console.
---

# Overlays, cold restarts and restores

The **Configuration** view shows the session's baseline file and, under **Changed from baseline**, each setting in the current configuration that differs from the baseline.

Overlays, cold restarts and restores are [exclusive actions](04-Sessions.md#exclusive-actions). After each one, the service waits for the router to report ready.

## Configuration overlays

An overlay is a partial fleet document that the service merges onto the current fleet configuration as a JSON merge patch (RFC 7386):

- An object merges key by key.
- `null` removes a key, which returns the field to its default.
- Any other value, arrays included, replaces the current value.

An overlay may change the `slo`, `controller`, `serving` and `recovery` sections, plus keys that start with `_`. [Serving and role control](../../configuration/02-Serving-and-Role-Control.md) and [Recovery and validation](../../configuration/03-Recovery-and-Validation.md) define those sections' fields.

For example, this overlay adds 10% of the TTFT target to the admission budget. The value is illustrative:

```json
{"serving": {"admission_margin": 0.1}}
```

### Apply an overlay

Applying an overlay requires the `router_restart` hook.

1. Enter the overlay in the **Configuration** view.
2. Select **Check overlay**. This dry run lists each setting the overlay changes, with its current and new value, or the validation errors.
3. Select **Apply overlay** and confirm.

The service checks the merged document with the fleet configuration loader that `narwhal config validate` runs, writes the merged file to the session directory, and runs the `router_restart` hook with `NARWHAL_CONTROL_FLEET` naming that file.

## Cold restart

**Cold restart** runs the `cold_restart` hook with the current fleet configuration file.

## Restore the baseline

**Restore baseline** runs the `restore` hook and records the baseline as the current configuration. The session stays open.

## Router readiness

After an overlay, cold restart or restore, the service polls the router's `GET /ready` every second until it returns HTTP 200. If `router.timeout_s` passes first, the action fails with HTTP 504.

An overlay applied by a successful `router_restart` hook governs the session even when the readiness wait then fails.
