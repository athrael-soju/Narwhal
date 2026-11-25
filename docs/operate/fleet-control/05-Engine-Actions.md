---
description: Pause, resume, stop, start, drain, readmit, join, and remove engines from the fleet control console.
---

# Engine actions

The **Engines** view lists each engine in the router's [membership](../../http-api/08-Engine-Membership.md), or in the baseline fleet configuration when the router does not answer `GET /narwhal/engines`, with its role, router state, resident requests and last action. Its heading shows the prefill-to-decode split of the engines in service, such as `split 2P:6D`.

## Find an engine

The list sorts engines by state, most severe first: ejected and blocked engines, then quarantined, draining, drained and probation engines, then paused and stopped engines, then engines in service. Click a column heading to sort by that column, and click it again to reverse the order.

To narrow the list:

- Click a state above the list, such as **Draining**, to show only engines in that state. **Needs attention** shows every engine that is not in service. Click the state again to show all engines.
- Type part of an engine ID in **Find engine**.
- Choose a role in **All roles**.

**Clear filters** removes all three.

The list shows 20 engines per page. When the fleet has more than 10 engines, a selector offers 10, 20, 50 or 100 engines per page, and **‹** and **›** appear once the list needs more than one page.

Click an engine ID to open its details beside the list: its state, role, resident requests, router lifecycle state and flags, the router's reason when the engine is ejected, and the engine's actions in this session.

<div class="narwhal-panel-row" markdown>

![Engines view filtered to engines that need attention, with n7's details open.](../../assets/fleet-control/engine-details.png#only-light)
![Engines view filtered to engines that need attention, with n7's details open.](../../assets/fleet-control/engine-details-dark.png#only-dark)

</div>

<div class="narwhal-panel-row" markdown>

![Engines view with n7 draining.](../../assets/fleet-control/engines-draining.png#only-light)
![Engines view with n7 draining.](../../assets/fleet-control/engines-draining-dark.png#only-dark)

</div>

## What each action does

| Action      | Effect                                                                                                                                                                                                                         |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Pause**   | Runs the `engine_pause` hook                                                                                                                                                                                                   |
| **Resume**  | Runs the `engine_resume` hook, then waits until the router returns the engine to service                                                                                                                                       |
| **Stop**    | Runs the `engine_stop` hook                                                                                                                                                                                                    |
| **Start**   | Runs the `engine_start` hook, then waits until the router returns the engine to service                                                                                                                                        |
| **Drain**   | Calls the router's [`POST /narwhal/lifecycle/drain`](../../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain). The router stops placing new requests on the engine and waits for its resident requests to finish |
| **Readmit** | Calls the router's [`POST /narwhal/lifecycle/readmit`](../../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) for a drained engine                                                                           |
| **Leave**   | Calls the router's [`DELETE /narwhal/engines/{iid}`](../../http-api/08-Engine-Membership.md#delete-narwhalenginesiid), then waits until the router removes the engine. The engine process keeps running                      |

Each action first reads the engine's entry from the router's `GET /narwhal/engines`, so it fails while the router does not answer. With a router that lacks this route, **Leave** fails, and the console reads the entry for the other actions from the baseline fleet configuration.

Each row has a main button for the engine's current state and a **…** menu with the other actions:

| Engine state                | Main button                                                                         |
| --------------------------- | ----------------------------------------------------------------------------------- |
| Paused in this session      | **Resume**                                                                          |
| Stopped in this session     | **Start**                                                                           |
| Drained or blocked          | **Readmit**                                                                         |
| Draining                    | **Readmit**, disabled until the drain finishes. The tooltip names the state         |
| Ejected by the router       | **Readmit**, disabled. The tooltip gives the router's reason                        |
| In service                  | **Drain**                                                                           |

**Start** and **Resume** wait up to `router.timeout_s`, 600 seconds by default. While they wait, the engine's state reads `starting` or `resuming` with the seconds waited. The action fails if the engine stays ejected or quarantined for the whole wait, the router blocks its readmission, or the engine needs fresh profiles. To fix a failed action, follow [Start fails and the engine stays ejected](08-Troubleshooting.md#start-fails-and-the-engine-stays-ejected).

The console asks for confirmation before **Pause**, **Stop**, **Drain** and **Leave**. A drain or a leave uses **Drain deadline** from the session strip, or the router's default of 300 seconds when that field is empty or 0. **Leave** waits for the removal up to that deadline plus `router.timeout_s`, and fails when the drain passes its deadline with resident requests. The router still removes the engine once they finish. During a drain, a bar under the engine's resident requests shows the drained fraction.

## Join an engine

Before a join, prepare the engine's profiles and calibration as in [Joining and removing engines](../08-Joining-and-Removing-Engines.md#join-an-engine).

Below the engine list, **Available engines** lists each engine of the baseline fleet configuration that is not a member of the router's fleet, such as an engine that left and still runs. **Engine API** and **Sidecar** show whether the engine's health route and its attestation sidecar respond. Both read **starting** when the service ran the engine's `engine_start` hook, from **Start** or **Restore baseline**, less than `router.timeout_s` ago, and the engine or its sidecar has not responded since. The service probes them at most once every 10 seconds, and again after each engine action. To return an engine, select its **Join** and confirm. The service sends the engine's baseline entry to the router's [`POST /narwhal/engines`](../../http-api/08-Engine-Membership.md#post-narwhalengines), with its `${VARIABLE}` references resolved from the service's environment as **Restore baseline** does.

For an engine outside the baseline, **Join engine** takes one engine entry as the fleet file holds it, with literal `url` and `attestation_url` values. Fill in the entry, select **Join engine** and confirm. The service sends the entry to the router's [`POST /narwhal/engines`](../../http-api/08-Engine-Membership.md#post-narwhalengines).

The feedback line under **Join engine** shows the result of either join.

Joins and leaves are [exclusive actions](04-Sessions.md#exclusive-actions).

## When an action is disabled

A disabled action shows the reason in its tooltip or under the menu item.

<div class="narwhal-panel-row" markdown>

![Engines view with the n2 action menu open.](../../assets/fleet-control/engine-menu.png#only-light)
![Engines view with the n2 action menu open.](../../assets/fleet-control/engine-menu-dark.png#only-dark)

</div>

- **Pause**, **Resume**, **Stop** and **Start** require their hooks. **Resume** requires a paused engine, and **Start** a stopped or ejected engine.
- **Drain** requires an engine in service. The router runs one drain or readmit at a time.
- The router readmits an ejected engine itself once its checks pass, so **Readmit** stays disabled for it. The tooltip shows the router's latest lifecycle event for the engine. When that event is `profile_recovery_blocked`, the engine's state reads `needs profiles`.
- **Readmit** requires a drained or blocked engine. When this session drained the engine and the router needs it restarted, **Readmit** stays disabled until **Start** succeeds for that engine. In every other case **Readmit** stays enabled, and the router refuses it until the engine restarts. To restart and readmit the engine, follow [Readmit stays disabled after a drain](08-Troubleshooting.md#readmit-stays-disabled-after-a-drain).
- An available engine's **Join** requires that the router's membership can be read and that the engine and its sidecar respond. While the engine reads **starting**, the tooltip reads `The engine is starting.`
- All engine actions require an active session and are disabled during an [exclusive action](04-Sessions.md#exclusive-actions).

## Paused and stopped engines

The console tracks paused and stopped engines within the session, and a cold restart clears those states. The console works out which engines joined or left by comparing the router's membership with the baseline. **Restore baseline** returns both to the baseline.

The **Last action** line under the table shows the latest engine action and the change in the engine's state and resident requests. The run record holds the engine's full router state before and after each action.
