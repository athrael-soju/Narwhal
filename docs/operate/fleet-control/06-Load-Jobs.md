---
description: Run an AIPerf workload against the router from the fleet control console, stop it early, and read its results and job document.
---

# Load jobs

The service runs one AIPerf job at a time against `router.url`. To change the rate or duration, start a new job. Load jobs require the [`load`](09-Configuration-Reference.md#load) configuration section.

<div class="narwhal-panel-row" markdown>

![Load job view with a running job.](../../assets/fleet-control/load-job.png)

</div>

## Choose a workload

The **Workload** list shows each workload's `label`. The hint under the inputs shows the workload's `description`, or a summary of its settings when the workload has no description, and then the input rules for that workload. [Workloads](09-Configuration-Reference.md#workloads) defines the workload kinds and their keys.

## Start a job

In the **Load job** view, select a workload and set its inputs:

| Input            | Field             | Meaning                                                                                                  |
| ---------------- | ----------------- | -------------------------------------------------------------------------------------------------------- |
| Rate (req/s)     | `rate`            | Requests per second                                                                                      |
| Arrival          | `arrival`         | Spacing of requests at the rate. **Random** sends Poisson arrivals, **Steady** spaces requests evenly, and **Bursty** sends gamma arrivals with smoothness 0.5 |
| Concurrency      | `concurrency`     | Maximum requests in flight                                                                               |
| Ramp-up (s)      | `ramp_s`          | Seconds to rise from a low start to the rate and the concurrency                                         |
| Duration (s)     | `duration_s`      | Seconds of load                                                                                          |
| Requests         | `requests`        | Number of requests to send                                                                               |
| Warm-up requests | `warmup_requests` | Requests sent before measurement starts. The results leave them out                                      |

Each workload kind accepts these inputs:

| Input              | `synthetic`, `mixed`, `multi_turn`, `public_dataset`, `prefix_trace` | `timestamped_trace`                                  |
| ------------------ | -------------------------------------------------------------------- | ---------------------------------------------------- |
| `rate`             | Set `rate`, `concurrency` or both                                    | Rejected                                             |
| `arrival`          | Optional. `steady`, `random` or `bursty`, default `random`. Requires `rate` | Rejected                                      |
| `concurrency`      | Set `rate`, `concurrency` or both                                    | Optional. Caps the requests in flight                |
| `ramp_s`           | Optional. Ramps the rate and the concurrency that are set            | Rejected                                             |
| `duration_s`       | Set `duration_s`, `requests` or both                                 | Optional. Shortens the replay                        |
| `requests`         | Set `duration_s`, `requests` or both                                 | Optional. Ends the replay after this many requests   |
| `warmup_requests`  | Optional                                                             | Rejected                                             |

A `timestamped_trace` workload sends each request at its recorded time. With both `duration_s` and `requests` set, the job stops at the first limit it reaches. In a `multi_turn` workload, `rate` counts turns and `concurrency` counts conversations.

## While the job runs

The view locks its inputs, and the session strip shows the job's progress. **Stop job** stops the job's whole AIPerf process group.

## Results

When the job finishes, the view shows:

- completed requests, total requests and errors
- request and output-token throughput, and goodput when `load.goodput` is configured
- TTFT, inter-token latency and request latency at p50, p95 and p99
- AIPerf's exit code and the number of journal lines copied

**Job document** downloads the full job document as `<job>.json`. [Load jobs](10-API-Reference.md#load-jobs) in the API reference lists its fields.

A job fails when its trace file is unreadable, AIPerf fails to start or exits non-zero, or the AIPerf summary export is missing or unreadable.
