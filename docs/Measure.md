# Production fleet measurement and acceptance

These pages take a Narwhal fleet from an idle-latency profile to a recorded, accepted operating rate. Tie every result to a deployment identifier that records the model, engine build, hardware shape, topology, workload, cache policy, and the time to first token (TTFT) and time per output token (TPOT) targets for the run. A number without that record can't be compared with anything else.

The first four pages are the manual procedure. Work through them in order.

1. [Measurement contract and profiling](measure/01-Profile.md)
2. [Targets and deployment freeze](measure/02-Targets-and-Freeze.md)
3. [Synthetic load trial](measure/03-Load-Trial.md)
4. [Reconcile and accept](measure/04-Reconcile-and-Accept.md)

The next two automate the load trial and the evidence collection around it. They're easier to use once you've done the manual steps at least once.

5. [Ordered benchmark points](measure/05-Benchmark-Runner.md)
6. [Benchmark evidence bundle](measure/06-Benchmark-Evidence.md)

The last page is a completed qualification run on MI355X hardware. Use it as a worked example.

7. [GPU benchmark qualification](measure/07-GPU-Qualification.md)
