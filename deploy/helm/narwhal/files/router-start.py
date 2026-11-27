# Start one router of the pair, router-0 or router-1, with the narwhal-serve arguments after
# its own. On the first install, router-0 finds no handoff at its state path and no lease
# file, starts as the primary and claims the free lease. Otherwise the router starts as a
# standby of the other one, as a supervisor restarts a router of the pair. A router-0 that
# started as the primary while the other router held the lease would never take over.
import json
import os
import sys

name = os.environ["NARWHAL_POD_NAME"]
base, _, index = name.rpartition("-")
peer = f"http://{base}-{1 - int(index)}.{os.environ['NARWHAL_ROUTERS_DOMAIN']}:8000"
arguments = sys.argv[1:]
with open(arguments[arguments.index("--fleet") + 1]) as stream:
    recovery = json.load(stream).get("recovery") or {}
lease = arguments[arguments.index("--lease-path") + 1]
saved = os.path.exists(recovery.get("state_path", "runs/state.json")) or os.path.exists(lease)
standby = [] if index == "0" and not saved else ["--standby-of", peer]
argv = [sys.executable, "-I", "-m", "narwhal.cli", *arguments, "--router-id", name, *standby]
os.execv(sys.executable, argv)  # noqa: S606 - the router replaces this process
