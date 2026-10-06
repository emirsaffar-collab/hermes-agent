"""provider_latency_circuit_breaker.py — autonomous circuit breaker for degraded LLM providers.

Tracks rolling latencies and error rates per (model, upstream_provider). When a provider's
rolling latency exceeds DEGRADED_LATENCY_THRESHOLD_S (default 20.0s) or incurs consecutive timeouts,
it is temporarily demoted in OpenRouter provider_routing.order for COOLDOWN_SECONDS (default 600s).
Once the cooldown expires, it is given an exploratory probe request to re-evaluate health.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Sequence

logger = logging.getLogger(__name__)

_DEGRADED_LATENCY_THRESHOLD_S = 20.0
_COOLDOWN_SECONDS = 600.0
_WINDOW_SIZE = 5

_lock = threading.Lock()
# (model, provider) -> deque of float durations
_history: dict[tuple[str, str], collections.deque[float]] = {}
# (model, provider) -> demoted_until_timestamp
_demoted_until: dict[tuple[str, str], float] = {}


def record_provider_latency(model: str, provider: str, duration_s: float) -> None:
    if not model or not provider or duration_s <= 0:
        return
    key = (model.lower(), provider.lower())
    now = time.monotonic()
    with _lock:
        if key not in _history:
            _history[key] = collections.deque(maxlen=_WINDOW_SIZE)
        _history[key].append(duration_s)

        # Check if rolling average exceeds threshold
        avg = sum(_history[key]) / len(_history[key])
        if len(_history[key]) >= 2 and avg >= _DEGRADED_LATENCY_THRESHOLD_S:
            until = now + _COOLDOWN_SECONDS
            _demoted_until[key] = until
            logger.warning(
                "provider circuit-breaker: %s @ %s degraded (avg=%.1fs over %d calls); demoting for %.0fs",
                model, provider, avg, len(_history[key]), _COOLDOWN_SECONDS,
            )
        elif key in _demoted_until and duration_s < _DEGRADED_LATENCY_THRESHOLD_S:
            # Recovered early on probe
            del _demoted_until[key]
            logger.info("provider circuit-breaker: %s @ %s recovered (latest=%.1fs)", model, provider, duration_s)


def record_provider_failure(model: str, provider: str) -> None:
    if not model or not provider:
        return
    key = (model.lower(), provider.lower())
    now = time.monotonic()
    with _lock:
        _demoted_until[key] = now + _COOLDOWN_SECONDS
        logger.warning(
            "provider circuit-breaker: %s @ %s recorded failure/timeout; demoting for %.0fs",
            model, provider, _COOLDOWN_SECONDS,
        )


def is_provider_demoted(model: str, provider: str) -> bool:
    if not model or not provider:
        return False
    key = (model.lower(), provider.lower())
    now = time.monotonic()
    with _lock:
        until = _demoted_until.get(key)
        if until is None:
            return False
        if now >= until:
            del _demoted_until[key]
            return False
        return True


def adjust_provider_order(model: str, order: Sequence[str]) -> list[str]:
    """Sort provider order so healthy providers are tried before degraded ones."""
    if not order or not model:
        return list(order)
    healthy = []
    demoted = []
    for p in order:
        if is_provider_demoted(model, p):
            demoted.append(p)
        else:
            healthy.append(p)
    if demoted and healthy:
        logger.debug(
            "provider circuit-breaker adjusted order for %s: healthy=%s, demoted=%s",
            model, healthy, demoted,
        )
        return healthy + demoted
    return list(order)


def reset_circuit_breaker() -> None:
    """Testing helper to clear all in-memory breaker state."""
    with _lock:
        _history.clear()
        _demoted_until.clear()
