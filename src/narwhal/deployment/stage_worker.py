"""Keep one helper tree attached to an isolated Linux subreaper until release."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    """Run the helper while keeping forked workers attached after their leader exits."""
    result = Path(sys.argv[1])
    release = Path(sys.argv[2])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "could not enable isolated helper reaping")
    # A caught signal resets to the default when the helper execs. The supervisor
    # stays alive through TERM so descendants retain an ownership anchor.
    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    child = subprocess.Popen(sys.argv[3:])
    code = child.wait()
    temporary = result.with_suffix(".pending")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump({"returncode": code}, output)
    temporary.replace(result)
    while not release.exists():
        # Reap adopted workers that finish while the controller decides whether
        # to clean the tree or retain the successful native engine generation.
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        time.sleep(0.01)


if __name__ == "__main__":
    main()
