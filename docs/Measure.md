# Measure a fleet

Measure a deployed fleet against its time to first token (TTFT) and time per output token (TPOT) targets, and record the highest tested offered rate that still meets both.

## Run a measurement

1. [Measurement contract and profiling](measure/01-Profile.md) defines the latency boundaries for the router and the client, then reuses or creates the idle-fleet latency profile.
2. [Targets and deployment freeze](measure/02-Targets-and-Freeze.md) sets the production TTFT and TPOT targets and freezes the deployment under test.
3. [Synthetic load trial](measure/03-Load-Trial.md) measures 0.5 and 1 request/s with 200 requests each through the private router tunnel.
4. [Reconcile and accept](measure/04-Reconcile-and-Accept.md) joins the client records to the router journal, checks throughput and client limits, runs the post-load KV ring check, and records deployment acceptance.

## Automate benchmark points

- [Ordered benchmark points](measure/05-Benchmark-Runner.md) runs a plan's trial points in order, with readiness and drain checks around each one.
- [Benchmark evidence bundle](measure/06-Benchmark-Evidence.md) collects the journal rows, metrics samples, file digests, and shareable summary for each point.

## Qualification results

- [GPU benchmark qualification](measure/07-GPU-Qualification.md) walks through the pinned inputs, procedure, and measured points of a recorded Kimi-K3 run on AMD Instinct MI355X engines.
