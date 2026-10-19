"""Narwhal test suite and shared fixtures."""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

from narwhal.deployment.launch_engine import plan as _launch_plan
from narwhal.observability.tools import METRICS_URL_ENV

# Per-run cache-event socket root for test launch plans.
_KV_EVENTS_ROOT = Path(tempfile.mkdtemp(prefix="narwhal-test-"))
atexit.register(shutil.rmtree, _KV_EVENTS_ROOT, True)
_launch_plan.KV_EVENTS_ROOT = _KV_EVENTS_ROOT

# Tool commands default --metrics-url from the environment; tests that need it set it.
os.environ.pop(METRICS_URL_ENV, None)
