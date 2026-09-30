"""Narwhal test suite and shared fixtures."""

import atexit
import shutil
import tempfile
from pathlib import Path

import narwhal.deployment.launch_engine as _launcher
import tools.deployment.launch_engine as _tool_launcher

# Keep cache-event socket directories from test launch plans out of the shared /tmp root.
_KV_EVENTS_ROOT = Path(tempfile.mkdtemp(prefix="narwhal-test-"))
atexit.register(shutil.rmtree, _KV_EVENTS_ROOT, True)
for _module in (_launcher, _tool_launcher):
    _module.KV_EVENTS_ROOT = _KV_EVENTS_ROOT
