"""Arrow-style adaptive scheduling for disaggregated LLM inference.

Imports as `narwhal`. The distribution is `narwhal-inference`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("narwhal-inference")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
