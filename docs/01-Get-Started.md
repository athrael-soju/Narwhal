# Get started with a fleet

Open [Deploy a fleet](03-Deploy.md) from your management workstation. Read the supplied private inventory and access configuration, identify the designated router and engine hosts, and open their management shells before installing Narwhal. Check GPUs on the engine hosts named by that inventory.

A fresh source clone contains the software and public guides. The deployment handoff supplies the private inventory, credentials and environment values separately; use their supplied locations from the management workstation when opening remote shells. Keep those values in the private access tooling and ignored host configuration described in Deploy.

If an inventory location, credential source or management route is unavailable, report fleet access as the first blocked gate, with the host, source revision, starting state, commands attempted and error. Continue deployment after that gate is resolved.
