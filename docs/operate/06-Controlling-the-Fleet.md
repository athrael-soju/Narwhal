---
description: Run engine actions, configuration overlays and AIPerf load jobs against a running fleet from the fleet control service and console, with a private run record per session.
---

# Controlling the fleet

The fleet control service runs operator test actions against a running Narwhal fleet:

- engine pause, resume, stop, start, drain and readmit
- configuration overlays
- cold restarts and baseline restores
- AIPerf load jobs

The service records each action, with its time and effect, in a private run record. The console runs on its own page or inside the **Fleet control** Grafana dashboard, beside the fleet's charts.

The service runs on the router host, listens on loopback, and authenticates API requests with a bearer token. Operators reach the console and Grafana from a workstation through the operator tunnel.

Hooks are the commands, defined in the private configuration, that perform each change, such as stopping an engine or restarting the router. Drains and readmits use the router's lifecycle API instead.

Work happens in sessions. Starting a session copies the baseline fleet configuration. Ending it closes the session's record and leaves the fleet as it is. **Restore baseline** undoes the session's changes.

## Pages

<div class="grid cards" markdown>

-   [Setting up the control service](fleet-control/01-Set-Up-the-Service.md)

    ---

    Write the private configuration, set the token, and start the service.

-   [Opening the console](fleet-control/02-Open-the-Console.md)

    ---

    Reach the console through the tunnel and choose how it authenticates.

-   [Using the console from Grafana](fleet-control/03-Grafana-Dashboard.md)

    ---

    Run the console in the **Fleet control** dashboard and mark actions on the Narwhal Orchestrator charts.

-   [Running a test session](fleet-control/04-Sessions.md)

    ---

    Start and end sessions, read the session strip, and follow the activity log.

-   [Engine actions](fleet-control/05-Engine-Actions.md)

    ---

    Pause, stop, drain and readmit engines.

-   [Load jobs](fleet-control/06-Load-Jobs.md)

    ---

    Run AIPerf workloads against the router and read the results.

-   [Overlays, cold restarts and restores](fleet-control/07-Configuration-Changes.md)

    ---

    Change serving settings mid-session, restart the fleet, or return to the baseline.

-   [Troubleshooting the console](fleet-control/08-Troubleshooting.md)

    ---

    Empty panels, missing chart markers, and engines stuck after a drain or stop.

-   [Configuration reference](fleet-control/09-Configuration-Reference.md)

    ---

    Configuration keys, the hook contract and hook environment.

-   [API reference](fleet-control/10-API-Reference.md)

    ---

    Routes, request fields, status codes and metrics.

-   [Run record](fleet-control/11-Run-Record.md)

    ---

    Session files, the `run.json` schema and journal extracts.

</div>
