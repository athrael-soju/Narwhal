"""Start subprocess coverage when the coverage runner selects this directory."""

import os

if os.environ.get("NARWHAL_COVERAGE_ACTIVE") == "1":
    from narwhal_coverage import start

    start()
