"""Narwhal test suite and shared fixtures."""

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

from narwhal.deployment.launch_engine import plan as _launch_plan
from narwhal.engines import host_process as _host_process
from narwhal.observability.tools import METRICS_URL_ENV
from narwhal.runtime.state import SIGNATURE_HEADER, sign
from narwhal.serving.control_token import ROUTER_TOKEN_ENV, headers

# Per-run cache-event socket root for test launch plans.
_KV_EVENTS_ROOT = Path(tempfile.mkdtemp(prefix="narwhal-test-"))
atexit.register(shutil.rmtree, _KV_EVENTS_ROOT, True)
_launch_plan.KV_EVENTS_ROOT = _KV_EVENTS_ROOT
# Boot time records of the run stay out of the developer's /tmp.
_host_process.BOOT_RECORDS = _KV_EVENTS_ROOT

# Tool commands default --metrics-url from the environment; tests that need it set it.
os.environ.pop(METRICS_URL_ENV, None)

# Every router a test builds requires this control token on its control routes; a test that
# calls one sends `CONTROL_HEADERS`.
CONTROL_TOKEN = "5e" * 32
CONTROL_HEADERS = headers(CONTROL_TOKEN)
os.environ[ROUTER_TOKEN_ENV] = CONTROL_TOKEN


def handoff_response(document, token=CONTROL_TOKEN):
    """A primary's `/narwhal/handoff` answer with `document`, signed under `token`."""
    import httpx

    body = json.dumps(document).encode()
    signature = sign(json.loads(body), body, token)
    return httpx.Response(
        200, content=body, headers={"content-type": "application/json", SIGNATURE_HEADER: signature}
    )
