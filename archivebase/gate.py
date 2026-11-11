"""The gate: one global slot, one global budget, one global stop button.

Why a file and a lock rather than a token bucket in memory:

  Three scripts each politely limited to one request every 10 seconds still
  produce three requests every 10 seconds. Politeness is a property of the
  *machine*, not of a process, so the state has to live somewhere all of them
  can see and take turns in. Here that is a JSON file guarded by `flock(2)`.

Properties this buys, in order of importance:

1. Single in-flight request, machine-wide. A reservation marks the gate
   busy until the response is settled.
2. A hard daily ceiling. It counts *attempts*, including ones that fail,
   because the server counts them too.
3. A stop that a server can trigger. 403/429/503 set state on disk; every
   process — including one started a second later by cron — refuses to send.
4. A stop a human can trigger. `PAUSED` is a file, so it survives reboots and
   does not depend on any process being alive.
5. Replayable history. Every reservation and outcome lands in `events`.

Nothing here talks to the network. Nothing here sleeps while holding the lock
except the (short) gap wait, which is intentional: it makes the slot strictly
ordered.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

STATE_VERSION = 1
POLL_SECONDS = 0.5
EVENT_KEEP = 80


# ------------------------------------------------------------------ errors


class GateError(RuntimeError):
    """The caller must stop its job. Never catch this to keep going."""

    exit_code = 75

    def __init__(self, message: str, *, until: float | None = None, reason: str = ""):
        super().__init__(message)
        self.until = until
        self.reason = reason

    @property
    def until_iso(self) -> str:
        return iso(self.until) if self.until else ""


class Paused(GateError):
    """An operator pressed pause."""

    exit_code = 75


class Halt(GateError):
    """The server asked us to back off, or a request is stuck in flight."""

    exit_code = 75


class ReviewRequired(GateError):
    """A block happened. Only a human can clear this."""

    exit_code = 78


class BudgetExhausted(GateError):
    """Today's request budget is spent. This is a success, not a failure."""

    exit_code = 75


# --------------------------------------------------------------- utilities


def iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _default_state() -> dict:
    return {
        "version": STATE_VERSION,
        "next_allowed_at": 0.0,
        "blocked_until": 0.0,
        "block_reason": "",
        "review_required": False,
        "review_reason": "",
        "review_since": 0.0,
        "strikes": 0,
        "day": "",
        "day_count": 0,
        "host_day": {},
        "attempts": 0,
        "last_ok_at": 0.0,
        "last_url": "",
        "last_status": 0,
        "in_flight": None,
        "events": [],
    }


@dataclass(frozen=True)
class Reservation:
    """A reserved request slot. Must be settled exactly once."""

    url: str
    host: str
    reserved_at: float
    gap: float
    attempt: int
    pid: int


# -------------------------------------------------------------------- gate


class Gate:
    def __init__(
        self,
        gate_dir: str | Path,
        *,
        min_gap: float = 10.0,
        daily_budget: int = 500,
        backoff: tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0),
        host_gaps: dict[str, float] | None = None,
        host_budgets: dict[str, int] | None = None,
        stale_after: float = 600.0,
        max_wait: float | None = None,
        pause_path: str | Path | None = None,
    ):
        self.dir = Path(gate_dir).expanduser()
        self.path = self.dir / "gate.json"
        self.lock_path = self.dir / "gate.lock"
        self.pause_path = Path(pause_path) if pause_path else self.dir / "PAUSED"
        self.min_gap = max(0.0, float(min_gap))
        self.daily_budget = max(0, int(daily_budget))
        self.backoff = tuple(float(x) for x in backoff) or (60.0,)
        self.host_gaps = {k.lower(): float(v) for k, v in (host_gaps or {}).items()}
        self.host_budgets = {k.lower(): int(v) for k, v in (host_budgets or {}).items()}
        self.stale_after = max(30.0, float(stale_after))
        self.max_wait = float(max_wait if max_wait is not None else max(60.0, 4 * self.min_gap))

    # -- plumbing --------------------------------------------------------
    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _default_state()
        if not isinstance(raw, dict):
            return _default_state()
        st = _default_state()
        st.update(raw)
        if not isinstance(st.get("events"), list):
            st["events"] = []
        return st

    def _write(self, st: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        st["events"] = st.get("events", [])[-EVENT_KEEP:]
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[dict]:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            st = self._read()
            yield st
            self._write(st)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # -- querying --------------------------------------------------------
    def paused(self) -> bool:
        return self.pause_path.exists()

    def snapshot(self) -> dict:
        st = self._read()
        st["paused"] = self.paused()
        st["state_path"] = str(self.path)
        return st

    def gap_for(self, host: str) -> float:
        pattern = self._pattern_for(host)
        if pattern and pattern in self.host_gaps:
            return max(self.host_gaps[pattern], 0.0)
        return self.min_gap

    def budget_for(self, host: str) -> int:
        pattern = self._pattern_for(host)
        if pattern and pattern in self.host_budgets:
            return max(0, self.host_budgets[pattern])
        return 0

    def _pattern_for(self, host: str) -> str | None:
        """Longest matching host rule, so web.archive.org beats archive.org."""
        best: str | None = None
        for pattern in set(self.host_gaps) | set(self.host_budgets):
            base = pattern.lstrip("*.")
            if host == base or host.endswith("." + base):
                if best is None or len(base) > len(best.lstrip("*.")):
                    best = pattern
        return best

    @staticmethod
    def _roll_day(st: dict) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if st.get("day") != today:
            st["day"] = today
            st["day_count"] = 0
            st["host_day"] = {}

    def _event(self, st: dict, kind: str, **fields) -> None:
        ev = {"at": time.time(), "at_iso": iso(time.time()), "kind": kind}
        ev.update({k: v for k, v in fields.items() if v not in (None, "")})
        st.setdefault("events", []).append(ev)

    # -- reserving -------------------------------------------------------
    def reserve(self, url: str, *, attempt: int = 1) -> Reservation:
        """Take the single global slot, waiting politely for the gap.

        Raises Paused / ReviewRequired / Halt / BudgetExhausted — all of which
        mean 'stop the job', not 'try again in a tight loop'.
        """
        host = host_of(url)
        if self.paused():
            raise Paused(
                f"{self.pause_path} exists — un-pause with `arcb resume`",
                reason="paused",
            )

        started = time.monotonic()
        while True:
            wait: float = 0.0
            with self._tx() as st:
                now = time.time()

                if st.get("review_required"):
                    raise ReviewRequired(
                        "crawling is disabled pending manual review: "
                        f"{st.get('review_reason') or 'a block was recorded'}\n"
                        "review the evidence, then `arcb resume --reason \"...\"`",
                        reason=str(st.get("review_reason") or ""),
                    )

                if float(st.get("blocked_until") or 0) > now:
                    until = float(st["blocked_until"])
                    raise Halt(
                        f"backing off until {iso(until)} "
                        f"({int(until - now)}s): {st.get('block_reason') or 'throttled'}",
                        until=until,
                        reason=str(st.get("block_reason") or "throttled"),
                    )

                self._roll_day(st)
                if self.daily_budget and int(st.get("day_count") or 0) >= self.daily_budget:
                    raise BudgetExhausted(
                        f"daily budget spent ({self.daily_budget} requests on "
                        f"{st.get('day')}); resume tomorrow or raise the ceiling "
                        "deliberately",
                        reason="daily budget",
                    )
                host_budget = self.budget_for(host)
                host_day = st.get("host_day") or {}
                if host_budget and int(host_day.get(host) or 0) >= host_budget:
                    raise BudgetExhausted(
                        f"daily budget for {host} spent ({host_budget} requests on "
                        f"{st.get('day')})",
                        reason=f"host budget {host}",
                    )

                inflight = st.get("in_flight")
                fresh = (
                    isinstance(inflight, dict)
                    and now - float(inflight.get("since") or 0) < self.stale_after
                )
                if fresh:
                    # Another process owns the slot. Poll rather than block the
                    # lock; the owner clears it when it settles.
                    pass
                else:
                    if inflight:
                        self._event(
                            st,
                            "stale-in-flight-cleared",
                            url=str(inflight.get("url", ""))[:300],
                            age=int(now - float(inflight.get("since") or now)),
                        )
                        st["in_flight"] = None

                    gap = self.gap_for(host)
                    if float(st.get("next_allowed_at") or 0) > now:
                        wait = float(st["next_allowed_at"]) - now
                    else:
                        st["next_allowed_at"] = now + gap
                        st["in_flight"] = {
                            "since": now,
                            "pid": os.getpid(),
                            "url": url[:300],
                        }
                        st["day_count"] = int(st.get("day_count") or 0) + 1
                        host_day = st.setdefault("host_day", {})
                        host_day[host] = int(host_day.get(host) or 0) + 1
                        st["attempts"] = int(st.get("attempts") or 0) + 1
                        st["last_reserve_at"] = now
                        return Reservation(
                            url=url,
                            host=host,
                            reserved_at=now,
                            gap=gap,
                            attempt=attempt,
                            pid=os.getpid(),
                        )

            elapsed = time.monotonic() - started
            if elapsed > self.max_wait:
                raise Halt(
                    "the gate has been busy for "
                    f"{int(elapsed)}s; stopping instead of queueing behind it",
                    reason="gate busy",
                )
            time.sleep(max(0.05, min(wait or POLL_SECONDS, POLL_SECONDS)))

    # -- settling --------------------------------------------------------
    def settle(
        self,
        res: Reservation | None,
        *,
        action: str,
        status: int = 0,
        wait: float = 0.0,
        reason: str = "",
        note: str = "",
    ) -> None:
        """Record what the server said. Always call this, even on failure."""
        with self._tx() as st:
            now = time.time()
            st["in_flight"] = None
            if res is not None:
                st["last_url"] = res.url
                st["last_status"] = int(status or 0)

            if action == "ok":
                st["strikes"] = 0
                st["last_ok_at"] = now
            elif action == "absent":
                st["strikes"] = 0
            elif action in ("throttle", "drop"):
                st["strikes"] = int(st.get("strikes") or 0) + 1
                step = self.backoff[min(st["strikes"] - 1, len(self.backoff) - 1)]
                hold = max(float(wait or 0.0), step)
                st["blocked_until"] = max(float(st.get("blocked_until") or 0), now + hold)
                st["next_allowed_at"] = max(
                    float(st.get("next_allowed_at") or 0), now + hold
                )
                st["block_reason"] = f"{action} {status or ''}: {reason}".strip()
            elif action in ("blocked", "review"):
                st["review_required"] = True
                st["review_since"] = now
                st["review_reason"] = f"{reason or action} (status {status or '?'})"
                st["blocked_until"] = max(
                    float(st.get("blocked_until") or 0), now + 3600
                )
                st["strikes"] = int(st.get("strikes") or 0) + 1
            elif action in ("retry", "error", "suspect", "redirect"):
                st["strikes"] = int(st.get("strikes") or 0) + (
                    1 if action in ("retry", "error") else 0
                )
            else:  # unknown action: be conservative, do not resume anything
                st["block_reason"] = f"unknown action {action!r}"

            self._event(
                st,
                action,
                url=(res.url if res else "")[:300],
                status=status or None,
                wait=round(float(wait or 0.0), 1) or None,
                note=(note or reason)[:300] or None,
            )

    # -- operator controls ----------------------------------------------
    def pause(self, reason: str = "paused by operator") -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pause_path.write_text(reason + "\n", encoding="utf-8")
        with self._tx() as st:
            self._event(st, "pause", note=reason)

    def resume(
        self,
        *,
        reason: str,
        clear_block: bool = False,
        acknowledge: bool = False,
    ) -> dict:
        """Clear a stop. A review stop needs an explicit acknowledgement."""
        if not reason.strip():
            raise ValueError("resume needs --reason: record what you checked")
        cleared: dict = {}
        with self._tx() as st:
            if st.get("review_required") and not acknowledge:
                raise ReviewRequired(
                    "this stop came from a server block; re-run with "
                    "--i-have-reviewed to record that you looked at it",
                    reason=str(st.get("review_reason") or ""),
                )
            cleared["review"] = st.get("review_reason") or ""
            st["review_required"] = False
            st["review_reason"] = ""
            st["review_since"] = 0.0
            st["strikes"] = 0
            if clear_block:
                cleared["blocked_until"] = iso(float(st.get("blocked_until") or 0))
                st["blocked_until"] = 0.0
                st["block_reason"] = ""
                st["next_allowed_at"] = 0.0
            st["in_flight"] = None
            self._event(st, "resume", note=reason[:300], clear_block=clear_block)
            remaining = float(st.get("blocked_until") or 0) - time.time()
            if remaining > 0:
                cleared["cooldown_until"] = iso(float(st["blocked_until"]))
                cleared["cooldown_seconds"] = int(remaining)
        if self.pause_path.exists():
            self.pause_path.unlink()
            cleared["pause"] = True
        return cleared
