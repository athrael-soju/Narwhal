# Production Fleet Measurement and Acceptance

Bind each production result to a deployment identifier that records the model, engine build, hardware shape, topology, workload, cache policy, and TTFT/TPOT targets used in the run.

[Gate F](deploy/06-Profile-and-Preflight.md) produces the engine profiles and final preflight for an initial deployment. Attach those artifacts to the load trial while the engine processes and fleet inputs match.

## Measurement sequence

1. [Measurement contract and profiling](measure/01-Profile.md)
2. [Targets and deployment freeze](measure/02-Targets-and-Freeze.md)
3. [Synthetic load trial](measure/03-Load-Trial.md)
4. [Reconcile and accept](measure/04-Reconcile-and-Accept.md)
5. [Ordered benchmark points](measure/05-Benchmark-Runner.md)
6. [Benchmark evidence bundle](measure/06-Benchmark-Evidence.md)
7. [GPU benchmark qualification](measure/07-GPU-Qualification.md)
