# <hw>-<model>

*Paths below are preset-relative (`scripts/...` means this preset's `scripts/`).*

| field | value |
| --- | --- |
| hardware | <accelerator, node count, fabric> |
| model | <served name> |
| engine | <serving build; **record the exact image digest once boot-validated**> |
| parallelism | <TP/EP per node> |
| context | <max-model-len> |
| connector | nixl (`kv_role: kv_both`) |
| dialect | vllm |
| SLOs | **unmeasured until profiled** - calibrate at 2x light-load p99, then record here |
| known blockers | <engine-version caveats that apply to your build; see the compatibility notes in the repository's docs/Deploy.md> |

A preset is *measured* when all eight `narwhal-check` gates pass on its hardware and the SLOs above come from its own profile. Until then, say so here. An unmeasured preset that says so is useful, and a silently unmeasured one is a trap.
