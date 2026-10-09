---
description: Start and end a fleet control session, read the session strip, know which actions are exclusive, and follow the session's activity log.
---

# Running a test session

A session is one operator test run. Start a session before any other action.

## Start and end a session

**Start session** validates the baseline fleet configuration and copies it into a new session directory.

**End session and restore** asks for confirmation, then:

1. stops a running load job
2. runs the `restore` hook
3. copies the session's [journal extract](11-Run-Record.md#journal-extracts)
4. closes the session

It returns once the restore hook finishes, before the router reports ready. If the restore hook fails, the session stays open, and you can end it again.

## Session strip

The session strip runs along the top of the console.

<div class="narwhal-panel-row" markdown>

![Session strip during a load job and a drain.](../../assets/fleet-control/session-strip.png)

</div>

| Item                   | Shows                                                                                                                         |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Connection             | **Connected**, **Service unreachable** or **Not connected**                                                                   |
| **Admission**          | **Ready** when the router admits requests, otherwise **Not ready** or **Unreachable**. The tooltip holds the router's reason  |
| **Alerts**             | Number of firing Narwhal alerts, red when any has page severity. Requires `prometheus_url`. The tooltip lists the alert names |
| **In progress**        | Actions still running, such as `drain n7`                                                                                     |
| **Session**            | Short session ID. The tooltip holds the start time                                                                            |
| **From session start** | Opens the dashboard with its time range starting at the session start                                                         |
| **Load job**           | The current or last job, its state and its elapsed time                                                                       |
| **Drain deadline (s)** | Deadline for drains started from this tab. Empty uses the router's default                                                    |
| Buttons                | **Start session** or **End session and restore**, and **Forget token**                                                        |

## Exclusive actions

Session start and end, configuration overlays, cold restarts and baseline restores are exclusive. While one runs, the service refuses every other action with `<action> is in progress`.

Engine actions and load jobs can run at the same time, so a drain can run during a load job.

## Activity

The **Activity** view lists the session's actions, newest first, with each action's start time, target, duration and outcome. A drain stays `in progress` until the router finishes it.

<div class="narwhal-panel-row" markdown>

![Activity view during a drain.](../../assets/fleet-control/activity.png)

</div>

**Run record** downloads the session's [run record](11-Run-Record.md) as `run-<session>.json`.
