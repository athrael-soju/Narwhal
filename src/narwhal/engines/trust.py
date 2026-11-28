"""Which engine endpoints may receive the engine credential."""

from __future__ import annotations

from collections.abc import Iterable


def endpoint(url: str) -> str:
    """The key an engine base URL is trusted under."""
    return url.rstrip("/")


class EngineTrust:
    """The engine base URLs the router sends its engine credential to.

    On a fleet with an engine contract, an endpoint is trusted once its attestation verifies,
    for the process start it attested, and only until the router sees that process end or
    another start in its place. A fleet without a contract has no attestation to verify, and
    trusts the engines its configuration names for as long as they are members.
    """

    def __init__(self, *, attested: bool, configured: Iterable[str] = ()) -> None:
        self.attested = attested
        self._starts: dict[str, float | None] = {}
        for url in configured:
            self.configure(url)

    def configure(self, url: str) -> None:
        """Trust a member that the fleet's configuration or saved membership names, when the
        fleet has no contract to attest it against."""
        if not self.attested:
            self._starts[endpoint(url)] = None

    def allows(self, url: str) -> bool:
        return endpoint(url) in self._starts

    def start(self, url: str) -> float | None:
        """The process start the endpoint's trust was granted for, or None."""
        return self._starts.get(endpoint(url))

    def grant(self, url: str, process_start: float) -> None:
        """Trust the endpoint after its attestation verified for `process_start`."""
        self._starts[endpoint(url)] = process_start

    def revoke(self, url: str) -> None:
        """Stop trusting an attested endpoint until its attestation verifies again."""
        if self.attested:
            self._starts.pop(endpoint(url), None)

    def observe(self, url: str, process_start: float) -> bool:
        """Revoke the endpoint's trust when it now runs another process; return whether it holds."""
        key = endpoint(url)
        if self.attested and key in self._starts and self._starts[key] != process_start:
            del self._starts[key]
        return key in self._starts

    def forget(self, url: str) -> None:
        """Drop an endpoint that left the fleet, on any fleet."""
        self._starts.pop(endpoint(url), None)
