"""Checkpoint primary coverage before the HA drill sends its original SIGKILL."""

import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import patch

from tools.drills import ha_failover


def checkpoint_before_kill(process, *, timeout_s=5):
    """Wait for the live primary to acknowledge its saved coverage shard."""
    folder = Path(os.environ["NARWHAL_COVERAGE_DIR"])
    marker = folder / f"saved-{process.pid}.json"
    marker.unlink(missing_ok=True)
    os.kill(process.pid, signal.SIGUSR1)
    deadline = time.monotonic() + timeout_s
    while not marker.exists():
        if process.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError(f"coverage checkpoint failed for HA primary {process.pid}")
        time.sleep(0.005)
    (folder / f"killed-{process.pid}.json").write_bytes(marker.read_bytes())


def main(argv=None):
    """Run the unchanged failure drill with an acknowledged pre-kill checkpoint."""
    original = ha_failover._stop

    def stop(process, *, kill=False):
        if kill and process is not None and process.poll() is None:
            checkpoint_before_kill(process)
        return original(process, kill=kill)

    with patch.object(ha_failover, "_stop", side_effect=stop):
        return ha_failover.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
