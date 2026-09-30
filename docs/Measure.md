# Measure a fleet

A fleet measurement reports the highest tested offered rate that meets the fleet's time to first token (TTFT) and time per output token (TPOT) targets.

## Run a measurement

1. [Measurement contract and profiling](measure/01-Profile.md): router and client latency boundaries, and the idle-fleet latency profile.
2. [Targets and deployment freeze](measure/02-Targets-and-Freeze.md): production TTFT and TPOT targets, and the frozen deployment under test.
3. [Synthetic load trial](measure/03-Load-Trial.md): 200 requests each at 0.5 and 1 request/s through the private router tunnel.
4. [Reconcile and accept](measure/04-Reconcile-and-Accept.md): the client-to-journal join, throughput and client-limit checks, the post-load KV ring check, and deployment acceptance.

## Automate benchmark points

- [Ordered benchmark points](measure/05-Benchmark-Runner.md): a plan's trial points in order, with readiness and drain checks around each point.
- [Benchmark evidence bundle](measure/06-Benchmark-Evidence.md): journal rows, metrics samples, file digests, and a shareable summary for each point.

## Qualification results

- [GPU benchmark qualification](measure/07-GPU-Qualification.md): pinned inputs, procedure, and measured points from a recorded Kimi-K3 run on AMD Instinct MI355X engines.
