"""Polite HTTP client + disk cache shared by every sg-legal-help source adapter.

Crawl strategy
--------------
A single ``httpx.Client`` with a connection pool, a jittered base delay before
each request, a 3-attempt exponential-backoff retry on transport errors and 5xx
(4xx surfaces immediately — a blocked route is a dropped route, never retried),
and a circuit breaker that raises :class:`CircuitBreakerOpen` after 5
consecutive failures so a sick source ends the run instead of hammering it.

Retry waits are floored at the base delay: a retry is still a request, so the
gap between requests must never dip below the configured politeness delay.

The honest ``ZeekerBot/1.0`` UA is sent on every request — no browser
impersonation. LawGoWhere serves no robots.txt (404), so the default delay is a
conservative 2s; override per-source with ``SGLEGALHELP_DELAY_SECONDS`` /
``SGLEGALHELP_JITTER_SECONDS`` (never set below a source's declared crawl-delay).

Disk cache
----------
``cache_write`` does atomic tmp-file + ``os.replace`` writes under ``.cache/``
(gitignored) so a crash mid-write can't leave a half-written file behind.

Env knobs
---------
- ``SGLEGALHELP_DELAY_SECONDS`` / ``SGLEGALHELP_JITTER_SECONDS`` — pacing.
- ``SGLEGALHELP_NO_DELAY=1`` — skip pacing + backoff sleeps. Tests against
  ``httpx.MockTransport`` / cached fixtures ONLY; never the live site.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any, Dict, Optional

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_none,
)

USER_AGENT = "ZeekerBot/1.0 (+https://data.zeeker.sg)"

REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 3

# LawGoWhere has no robots.txt (404) — use a conservative default.
DELAY_SECONDS = float(os.environ.get("SGLEGALHELP_DELAY_SECONDS", "2.0"))
JITTER_SECONDS = float(os.environ.get("SGLEGALHELP_JITTER_SECONDS", "1.0"))

MAX_CONSECUTIVE_FAILURES = 5

_RETRYABLE_EXCEPTIONS = (httpx.TransportError, httpx.HTTPStatusError)


def _no_delay() -> bool:
    return os.environ.get("SGLEGALHELP_NO_DELAY") == "1"


class CircuitBreakerOpen(Exception):
    """Too many consecutive request failures — stop hitting the source."""


def cache_write(path, content: str) -> None:
    """Atomically write ``content`` to ``path``, creating parent dirs."""
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class PoliteClient:
    """Rate-limited, retrying, circuit-breakered HTTP client.

    Every ``get`` applies the jittered polite delay, retries transient failures
    with backoff floored at the delay, and feeds a consecutive-failure circuit
    breaker. A success resets the counter; the failure that reaches
    ``max_consecutive_failures`` raises :class:`CircuitBreakerOpen`.
    """

    def __init__(
        self,
        *,
        delay_seconds: float = DELAY_SECONDS,
        jitter_seconds: float = JITTER_SECONDS,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.delay_seconds = delay_seconds
        self.jitter_seconds = jitter_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0
        self._client = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            follow_redirects=True,
            transport=transport,
        )
        self._retrying = Retrying(
            stop=stop_after_attempt(MAX_RETRIES),
            wait=(
                wait_none()
                if _no_delay()
                else wait_exponential(
                    multiplier=2,
                    min=max(1.0, self.delay_seconds),
                    max=max(10.0, self.delay_seconds * 2),
                )
            ),
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            reraise=True,
        )

    def _polite_sleep(self) -> None:
        if _no_delay():
            return
        delay = self.delay_seconds + random.uniform(0, self.jitter_seconds)
        time.sleep(max(0.1, delay))

    def _request_once(
        self, url: str, params: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]
    ) -> httpx.Response:
        response = self._client.get(url, params=params, headers=headers)
        # Raise (→ retry) on 5xx; return 4xx so it surfaces without retry.
        if 500 <= response.status_code < 600:
            response.raise_for_status()
        return response

    def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """GET ``url`` politely. Raises :class:`CircuitBreakerOpen` when tripped."""
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise CircuitBreakerOpen(
                f"{self.consecutive_failures} consecutive failures — refusing new requests"
            )
        self._polite_sleep()
        try:
            response = self._retrying(self._request_once, url, params, headers)
            response.raise_for_status()
        except Exception as exc:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.max_consecutive_failures:
                raise CircuitBreakerOpen(
                    f"{self.consecutive_failures} consecutive failures "
                    f"(last: {type(exc).__name__}: {exc})"
                ) from exc
            raise
        self.consecutive_failures = 0
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
