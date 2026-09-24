"""Compatibility entry point for the installed Narwhal observability startup."""

from narwhal.observability.start import main

if __name__ == "__main__":
    raise SystemExit(main())
