"""The response policy: what a status code means for the whole job.

This module is pure — no I/O — so the rules can be read, tested and argued with
in isolation. `client.py` is the only thing that acts on a Verdict.

The table below is the contract:

    200-299  ok         continue; clear the strike counter
    300-399  redirect   follow at most `max_redirects`, each hop is its own
                        gated request (a redirect loop still costs requests)
    404/410  absent     a real answer: record it, move on, never retry today
    400/405  error      our bug, not theirs: record, do not retry
    401/403  blocked    STOP the job, require a human. No automatic resume.
    429      throttle   STOP the job. Honour Retry-After if present.
    503      throttle   STOP the job. Back off substantially.
    500-5xx  retry      limited retries with exponential backoff (configurable
                        to stop instead)
    dropped  drop       treat as throttling, never as "absent". This is the
                        single most expensive bug in a naive crawler: a reset
                        connection recorded as "file does not exist".
"""

from __future__ import annotations

import email.utils
import socket
import ssl
import time
import urllib.error
from dataclasses import dataclass

OK = "ok"
REDIRECT = "redirect"
ABSENT = "absent"
ERROR = "error"
RETRY = "retry"
THROTTLE = "throttle"
BLOCKED = "blocked"
DROP = "drop"
SUSPECT = "suspect"

#: One-line explanations used in logs and reports.
EXPLAIN = {
    OK: "server answered",
    REDIRECT: "redirect",
    ABSENT: "the capture is not there",
    ERROR: "the request was wrong",
    RETRY: "server-side failure, limited retries",
    THROTTLE: "server asked us to slow down",
    BLOCKED: "server refused us — human review required",
    DROP: "connection dropped — throttling, not absence",
    SUSPECT: "200 OK but the body is not the file we asked for",
}


@dataclass(frozen=True)
class Verdict:
    action: str
    halt: bool = False
    review: bool = False
    retryable: bool = False
    wait: float = 0.0
    note: str = ""

    @property
    def explanation(self) -> str:
        base = EXPLAIN.get(self.action, self.action)
        return f"{base}: {self.note}" if self.note else base


def parse_retry_after(value, *, now: float | None = None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date. Clamp to a day."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    seconds: float | None = None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - (now if now is not None else time.time())
    if seconds is None or seconds != seconds:  # NaN guard
        return None
    return max(0.0, min(seconds, 86400.0))


def classify(status: int, *, retry_after=None, stop_on_5xx: bool = False) -> Verdict:
    """Map one HTTP status to a Verdict."""
    if 200 <= status < 300:
        return Verdict(OK)

    if 300 <= status < 400:
        return Verdict(REDIRECT, note=f"HTTP {status}")

    if status in (404, 410):
        return Verdict(ABSENT, note=f"HTTP {status}")

    if status in (401, 403, 407):
        return Verdict(
            BLOCKED,
            halt=True,
            review=True,
            note=f"HTTP {status} — crawling is disabled until a human reviews it",
        )

    if status == 429:
        return Verdict(
            THROTTLE,
            halt=True,
            retryable=True,
            wait=parse_retry_after(retry_after) or 0.0,
            note="HTTP 429 rate limited" + (
                f" (Retry-After: {retry_after})" if retry_after else ""
            ),
        )

    if status == 503:
        return Verdict(
            THROTTLE,
            halt=True,
            retryable=True,
            wait=parse_retry_after(retry_after) or 0.0,
            note="HTTP 503 service unavailable" + (
                f" (Retry-After: {retry_after})" if retry_after else ""
            ),
        )

    if status == 408:
        return Verdict(RETRY, retryable=True, note="HTTP 408 request timeout")

    if 500 <= status < 600:
        return Verdict(
            RETRY,
            halt=stop_on_5xx,
            retryable=True,
            note=f"HTTP {status}",
        )

    # 400/405/406/451/nonsense: our request was wrong or the resource is
    # legally unavailable. Retrying will not help and would just add traffic.
    return Verdict(ERROR, note=f"HTTP {status}")


def classify_exception(exc: BaseException) -> Verdict:
    """A failed connection is throttling until proven otherwise."""
    if isinstance(exc, urllib.error.HTTPError):  # defensive: handled elsewhere
        return classify(exc.code)

    if isinstance(exc, ssl.SSLError):
        return Verdict(DROP, halt=True, retryable=True, note=f"TLS error: {exc}")

    if isinstance(exc, (socket.timeout, TimeoutError)):
        return Verdict(DROP, halt=True, retryable=True, note="request timed out")

    if isinstance(exc, (ConnectionError, socket.gaierror)):
        return Verdict(
            DROP, halt=True, retryable=True, note=f"connection failed: {exc}"
        )

    if isinstance(exc, urllib.error.URLError):
        return Verdict(
            DROP, halt=True, retryable=True, note=f"connection failed: {exc.reason}"
        )

    return Verdict(DROP, halt=True, retryable=True, note=f"{type(exc).__name__}: {exc}")


def cooldown_for(strikes: int, backoff_sequence: tuple[float, ...], floor: float = 0.0) -> float:
    """Exponential cooldown, capped at the last step in the sequence."""
    if not backoff_sequence:
        return max(0.0, floor)
    step = backoff_sequence[min(max(strikes, 1) - 1, len(backoff_sequence) - 1)]
    return max(float(floor), float(step))
