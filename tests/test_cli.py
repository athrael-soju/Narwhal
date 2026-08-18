"""Every console script is declared, imports and is callable.

A package installed as a PEP 420 namespace portion loses these silently to any
other distribution holding the same top-level name.
"""

from importlib.metadata import entry_points

import pytest

EXPECTED = {
    "narwhal-check": "narwhal.check:main",
    "narwhal-serve": "narwhal.cli:serve",
    "narwhal-profile": "narwhal.probe:main",
    "narwhal-bench": "narwhal.bench:main",
    "narwhal-live-bench": "narwhal.live_bench:main",
    "narwhal-fleet": "narwhal.fleet:main",
    "narwhal-report": "narwhal.report:main",
}


def _scripts():
    return {ep.name: ep for ep in entry_points(group="console_scripts")}


def test_the_declared_scripts_are_exactly_the_expected_seven():
    ours = {name: ep.value for name, ep in _scripts().items() if name.startswith("narwhal-")}
    assert ours == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_script_target_imports_and_is_callable(name):
    ep = _scripts().get(name)
    assert ep is not None, f"{name} is not installed; reinstall with pip install -e ."
    assert callable(ep.load())


def test_the_package_is_a_regular_package_not_a_namespace_portion():
    """A namespace portion has __file__ None and merges with anything else of
    the same name. A regular package owns its directory."""
    import narwhal

    assert narwhal.__file__ is not None
    assert narwhal.__version__
