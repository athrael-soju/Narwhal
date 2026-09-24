"""Compatibility entry point for packaged Prometheus target generation."""

from narwhal.observability.make_targets import main

if __name__ == "__main__":
    raise SystemExit(main())
