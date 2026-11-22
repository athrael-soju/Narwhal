---
description: Measure the highest offered request rate a Narwhal fleet sustains within its TTFT and TPOT targets.
---

# Measuring a fleet

A fleet measurement reports the highest tested offered rate that meets the fleet's time to first token (TTFT) and time per output token (TPOT) targets.

## Running a measurement

1. [Measurement contract and profiling](measure/01-Profile.md): router and client latency boundaries, and the idle-fleet latency profile.
2. [Targets and deployment freeze](measure/02-Targets-and-Freeze.md): production TTFT and TPOT targets, and the frozen deployment under test.
3. [Synthetic load trial](measure/03-Load-Trial.md): 200 requests each at 0.5 and 1 request/s through the private router tunnel.
4. [Reconciling and accepting](measure/04-Reconcile-and-Accept.md): the client-to-journal join, throughput and client-limit checks, the post-load KV ring check, and deployment acceptance.

## Other measurements

- [Ordered benchmark points](measure/05-Benchmark-Runner.md): a plan's trial points, with readiness and drain checks per point.
- [Benchmark evidence bundle](measure/06-Benchmark-Evidence.md): journal rows, metrics samples, file digests, and a shareable summary per point.
- [Cache-aware placement trial](measure/08-Cache-Aware-Trial.md): cold-priced and cache-aware arms on one fleet, scored on repeated-prefix and cold-control workloads.
- [Benchmarking the router](measure/09-Router-Benchmark.md): relayed frames per router CPU-second for one router process against simulated engines.
