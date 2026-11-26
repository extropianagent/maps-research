"""The one place an HTTP client for a provider is built.

Every provider calls `make_client` when no client is injected. It first runs the proxy check, so a
proxy set in the environment is refused unless it is loopback or the owner recorded it. httpx's
default handling of the environment is otherwise kept (certificate variables must keep working).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from .. import policy
from ..budget import Budget

_unchecked = False


@contextmanager
def unchecked_proxy() -> Iterator[None]:
    """For commands that build providers but send nothing (doctor): skips the proxy check."""
    global _unchecked
    previous, _unchecked = _unchecked, True
    try:
        yield
    finally:
        _unchecked = previous


def request_timeout(budget: Budget, own: float) -> float:
    """The timeout of one request: the provider's own, but never beyond what the run's time limit has
    left, and never below one second."""
    return max(1.0, min(own, budget.remaining_elapsed_s()))


def make_client(*, timeout: float) -> httpx.Client:
    if _unchecked:
        return httpx.Client(timeout=timeout)
    policy.check_proxy_env()
    return httpx.Client(timeout=timeout)
