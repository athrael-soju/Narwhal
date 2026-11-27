# Start one router of the pair, router-0 or router-1, with the narwhal-serve arguments after
# its own. The routers elect the primary through the Kubernetes Lease. On the first install,
# router-0 finds no handoff at its state path and starts with --first-start-primary: it claims
# the Lease while the Lease is absent or free, and otherwise shadows the other router. Every
# other start is a standby of the other router, as a supervisor restarts a router of the pair.
import json
import os
import sys

name = os.environ["NARWHAL_POD_NAME"]
base, _, index = name.rpartition("-")
peer = f"http://{base}-{1 - int(index)}.{os.environ['NARWHAL_ROUTERS_DOMAIN']}:8000"
arguments = sys.argv[1:]
with open(arguments[arguments.index("--fleet") + 1]) as stream:
    recovery = json.load(stream).get("recovery") or {}
saved = os.path.exists(recovery.get("state_path", "runs/state.json"))
standby = ["--standby-of", peer]
if index == "0" and not saved:
    standby.append("--first-start-primary")
argv = [sys.executable, "-I", "-m", "narwhal.cli", *arguments, "--router-id", name, *standby]
os.execv(sys.executable, argv)  # noqa: S606 - the router replaces this process
