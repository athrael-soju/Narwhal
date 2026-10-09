---
description: Pause, resume, stop, start, drain and readmit engines from the fleet control console, and find out why an engine action is disabled.
---

# Engine actions

The **Engines** view lists each engine in the baseline fleet configuration with its role, router state, resident requests and last action. Its heading shows the prefill-to-decode split of the engines in service, such as `split 2P:6D`.

<div class="narwhal-panel-row" markdown>

![Engines view with n7 draining.](../../assets/fleet-control/engines-draining.png)

</div>

## What each action does

| Action      | Effect                                                                                                                                                                                                                         |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Pause**   | Runs the `engine_pause` hook                                                                                                                                                                                                   |
| **Resume**  | Runs the `engine_resume` hook                                                                                                                                                                                                  |
| **Stop**    | Runs the `engine_stop` hook                                                                                                                                                                                                    |
| **Start**   | Runs the `engine_start` hook                                                                                                                                                                                                   |
| **Drain**   | Calls the router's [`POST /narwhal/lifecycle/drain`](../../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain). The router stops placing new requests on the engine and waits for its resident requests to finish |
| **Readmit** | Calls the router's [`POST /narwhal/lifecycle/readmit`](../../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) for a drained engine                                                                           |

Each row has a main button for the engine's current state and a **…** menu with the other actions:

| Engine state                 | Main button                                                         |
| ---------------------------- | ------------------------------------------------------------------- |
| Paused in this session       | **Resume**                                                          |
| Stopped in this session      | **Start**                                                           |
| Draining, drained or blocked | **Readmit**                                                         |
| Ejected by the router        | **Readmit**, disabled because the router readmits it after recovery |
| In service                   | **Drain**                                                           |

The console asks for confirmation before **Pause**, **Stop** and **Drain**. A drain uses **Drain deadline (s)** from the session strip when it is set, and the router's default of 300 seconds otherwise. During a drain, a bar under the engine's resident requests shows the drained fraction.

## When an action is disabled

A disabled action shows the reason in its tooltip or under the menu item.

<div class="narwhal-panel-row" markdown>

![Engines view with the n6 action menu open.](../../assets/fleet-control/engine-menu.png)

</div>

- **Pause**, **Resume**, **Stop** and **Start** require their hooks. **Resume** requires a paused engine, and **Start** a stopped or ejected engine.
- **Drain** requires an engine in service. The router runs one drain or readmit at a time.
- **Readmit** requires a drained or blocked engine. When the router requires a restart after the drain, **Readmit** stays disabled until the engine restarts. See [Readmit stays disabled after a drain](08-Troubleshooting.md#readmit-stays-disabled-after-a-drain).
- All engine actions require an active session and are disabled during an [exclusive action](04-Sessions.md#exclusive-actions).

## Paused and stopped engines

Pause and stop run through hooks, outside the router's view, so the console derives the paused and stopped states from the session's run record. A baseline restore or a cold restart clears them.

The **Last action** line under the table shows the latest engine action and the change in the engine's state and resident requests. The run record holds the engine's full router state before and after each action.
