# Presets - one directory per (hardware, model) pair

A preset packages everything a fleet needs that the scheduler core deliberately
does not know: the fleet config and the engine-launch scripts for that
accelerator (plus any container scripts the site needs), and a manifest naming
the image, parallelism, context length, measured SLOs, connector and known
blockers. Nothing under `src/` names a GPU or a model. The preset is where
that knowledge lives.

Presets ship with the repository, because what they record - the launch
recipe, the blockers, the numbers a fleet was measured at - is worth more to
the next operator than it costs to carry. `presets/mi355x-kimi-k3/` is the
reference preset the study's numbers were measured on, and
`presets/b200-kimi-k3/` is its CUDA sibling. Neither carries an address a
reader could dial: `fleet.json` holds placeholders, and the launch scripts
take the node's addresses from the environment.

What stays local is anything that names a site. Standalone scripts that
hard-code a fabric address, a storage path, a container name or a jump host
live on the nodes they belong to and are kept out of the preset. Keep that
line when you add your own: publish the shape, keep the addresses.

## Adding a (hardware, model) pair

1. Copy `presets/_template/` to `presets/<hw>-<model>/` (hyphenated,
   lowercase, model normalized the way its serving name reads). If you
   copy an existing preset instead, take its *structure*: another
   fleet's scripts carry that fleet's site values (container names,
   storage paths, jump hosts), and those values do not transfer.
2. Edit `fleet.json`: engine URLs, `model`, `connector`, `dialect`. Leave the
   SLOs until step 5.
3. Adapt `scripts/` to the accelerator's launch recipe: container image,
   device plumbing, the transfer fabric's environment.
4. Launch the engines, then run the standard sequence:
   `narwhal-profile` → `narwhal-check` (all eight gates are vendor-neutral;
   `produce`/`consume` are the transport's acceptance test). On a build with
   no exact-count route, the profiler sizes prompts off the character ratio
   and says so.
5. Calibrate the SLOs from the profile (twice light-load p99, per
   [Benchmarking](../docs/Benchmarking.md)) and record them in the
   manifest.
6. Serve through the unmodified router.

Then benchmark and record: the run table carries a column per preset, all
produced by the same trace and seed methodology.

`narwhal-check --preset <name>` resolves `presets/<name>/fleet.json` from the
checkout root, so the acceptance gate runs against the preset's own record.
Against a live fleet, point the tools at the working config instead:
`narwhal-check --fleet runs/fleet.local.json`. Keep that file under `runs/` -
`narwhal-fleet deploy` wipes every other tracked path on the node and
preserves `runs/` and `.env*`, so a working config anywhere else is gone on
the next deploy. The preset records the fleet as measured, and the local
config is what it runs today.

Validated pairs are listed in
[Supported Hardware and Models](../docs/Supported-Hardware-and-Models.md);
each row links its shipped configuration.
