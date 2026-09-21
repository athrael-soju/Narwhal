"""Save the active process collector before drill termination signals."""

import json
import os
import signal
import sys
from pathlib import Path

import coverage


def checkpoint(signum, frame):
    """Persist the current collector and acknowledge its exact output shard."""
    active = coverage.Coverage.current()
    if active is None:
        raise RuntimeError("coverage checkpoint requested before startup")
    active.save()
    folder = Path(os.environ["NARWHAL_COVERAGE_DIR"])
    marker = {"pid": os.getpid(), "data_file": active.get_data().data_filename()}
    temporary = folder / f"saved-{os.getpid()}.tmp"
    temporary.write_text(json.dumps(marker) + "\n")
    temporary.replace(folder / f"saved-{os.getpid()}.json")


def terminate(signum, frame):
    """Save the forked worker's active collector and deliver the termination signal."""
    checkpoint(signum, frame)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def start():
    """Register coverage and process identity for an opted-in Python interpreter."""
    coverage.process_startup()
    folder = Path(os.environ["NARWHAL_COVERAGE_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    identity = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "context": os.environ["NARWHAL_COVERAGE_CONTEXT"],
        "argv": sys.orig_argv,
    }
    (folder / f"process-{os.getpid()}.json").write_text(json.dumps(identity) + "\n")
    signal.signal(signal.SIGUSR1, checkpoint)
    signal.signal(signal.SIGTERM, terminate)
