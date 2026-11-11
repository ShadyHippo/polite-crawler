"""The runner: bounded work, resumable, with an exit code that means something.

A run is not a loop that tries to finish everything. It is a *budget*:

  * at most `max_requests_per_run` requests
  * at most `max_runtime_seconds` of wall clock
  * at most one request in flight, machine-wide, at all times

When the budget, the clock or the server stops it, the run stops cleanly,
records why, and leaves every unfinished item `pending`. Nothing is lost and
nothing is guessed. Exit codes:

    0   the queue is empty or the budget ran out normally
    75  stopped temporarily (throttled, paused, another process holds the gate)
    78  stopped permanently until a human reviews a block
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import policy
from .cdx import guess_want
from .gate import (
    BudgetExhausted,
    GateError,
    Halt,
    Paused,
    ReviewRequired,
)
from .store import Store

MAX_ITEM_ATTEMPTS = 3  # across runs; a block does not count as an attempt


@dataclass
class RunSummary:
    run: str = ""
    state: str = "running"
    reason: str = ""
    requests: int = 0
    ok: int = 0
    absent: int = 0
    failed: int = 0
    skipped: int = 0
    items: int = 0
    bytes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.state == "review":
            return 78
        if self.state in ("halted", "paused", "time", "budget", "budget-day"):
            return 75 if self.state in ("halted", "paused", "budget-day") else 0
        return 0

    def line(self) -> str:
        bits = [
            f"run {self.run or '-'}",
            f"{self.state}",
            f"requests={self.requests}",
            f"ok={self.ok}",
            f"absent={self.absent}",
            f"failed={self.failed}",
        ]
        if self.bytes:
            bits.append(f"bytes={self.bytes}")
        if self.reason:
            bits.append(f"({self.reason})")
        return " ".join(bits)


def fetch_pending(
    fetcher,
    *,
    limit: int | None = None,
    runtime: int | None = None,
    echo=print,
    dry_run: bool = False,
) -> RunSummary:
    """Work the queue within budget. Never raises for a normal stop."""
    cfg = fetcher.cfg
    store: Store = fetcher.store
    pol = cfg.policy

    budget = int(pol.max_requests_per_run)
    if limit is not None:
        budget = min(budget, max(0, int(limit)))
    deadline = time.monotonic() + min(
        int(pol.max_runtime_seconds), int(runtime if runtime is not None else pol.max_runtime_seconds)
    )

    summary = RunSummary()

    if dry_run:
        echo("dry run: nothing will be requested")
        for item in store.pending(min(budget, 10)):
            summary.items += 1
            echo(f"  would fetch  {item['url']}  "
                 f"({item['kind']}, want={item['want'] or guess_want(item['url']) or '?'})")
        echo(f"queue: {store.queue_counts()}")
        summary.state = "dry-run"
        return summary

    summary.run = store.start_run("fetch")

    try:
        while True:
            # Count real HTTP requests, including robots.txt and redirect hops.
            summary.requests = store.requests_for_run(summary.run)
            if summary.requests >= budget:
                summary.state = "budget"
                summary.reason = f"request budget for this run ({budget})"
                break
            if time.monotonic() >= deadline:
                summary.state = "time"
                summary.reason = "max_runtime_seconds reached"
                break

            item = store.next_pending()
            if item is None:
                summary.state = "empty"
                summary.reason = "queue is empty"
                break

            item_id = int(item["id"])
            url = str(item["url"])
            want = str(item["want"] or "") or guess_want(url)
            timestamp = str(item["timestamp"] or "")
            attempts = int(item["attempts"] or 0)

            if attempts >= MAX_ITEM_ATTEMPTS:
                store.mark(item_id, "failed", f"gave up after {attempts} attempts")
                summary.failed += 1
                echo(f"  gave up     {url}")
                continue

            summary.items += 1
            echo(f"  [{summary.items}] {url}")

            result = fetcher.get(url, want=want, timestamp=timestamp, run=summary.run)

            if result.ok:
                store.record_capture(
                    url=url,
                    timestamp=timestamp,
                    status=result.status,
                    mime=result.mime,
                    length=len(result.body),
                    digest="",
                    sniffed=result.sniffed,
                    nbytes=len(result.body),
                    sha256=result.sha256,
                    path="",
                )
                store.mark(item_id, "ok", f"{result.sniffed} {len(result.body)}B")
                summary.ok += 1
                summary.bytes += len(result.body)
                echo(f"      ok      {result.sniffed or '?'} {len(result.body)}B")
            elif result.action == policy.ABSENT:
                store.record_capture(
                    url=url,
                    timestamp=timestamp,
                    status=result.status or 404,
                    mime="",
                    length=0,
                    digest="",
                    sniffed="",
                    nbytes=0,
                    sha256="",
                    path="",
                )
                store.mark(item_id, "absent", result.note)
                summary.absent += 1
                echo(f"      absent  HTTP {result.status}")
            elif result.action == "robots":
                store.mark(item_id, "skipped", result.note)
                summary.skipped += 1
                echo(f"      skipped {result.note}")
            elif result.action == policy.SUSPECT:
                store.mark(item_id, "suspect", result.note)
                summary.failed += 1
                echo(f"      suspect {result.note}")
            elif result.action == policy.REDIRECT:
                store.mark(item_id, "pending", result.note)
                summary.failed += 1
                echo(f"      redirect {result.note}")
            else:
                # retryable failure: leave it pending for a later run
                state = "retry" if result.attempts < MAX_ITEM_ATTEMPTS else "failed"
                store.mark(item_id, state, result.note)
                summary.failed += 1
                echo(f"      {result.action:8s}{result.note}")

    except ReviewRequired as exc:
        summary.state = "review"
        summary.reason = str(exc).splitlines()[0]
        echo("")
        echo("STOPPED: manual review required.")
        echo(f"  {exc}")
    except Paused as exc:
        summary.state = "paused"
        summary.reason = str(exc)
        echo(f"STOPPED: {exc}")
    except BudgetExhausted as exc:
        summary.state = "budget-day"
        summary.reason = str(exc)
        echo(f"STOPPED: {exc}")
    except Halt as exc:
        summary.state = "halted"
        summary.reason = str(exc)
        echo(f"STOPPED: {exc}")
    except GateError as exc:
        summary.state = "halted"
        summary.reason = str(exc)
        echo(f"STOPPED: {exc}")
    except KeyboardInterrupt:
        summary.state = "stopped"
        summary.reason = "interrupted"
        echo("")
        echo("stopped by hand; queue state is saved")
    finally:
        summary.requests = store.requests_for_run(summary.run) or summary.requests
        store.finish_run(
            summary.run,
            state=summary.state,
            reason=summary.reason,
            requests=summary.requests,
            ok=summary.ok,
            absent=summary.absent,
            failed=summary.failed,
        )

    echo("")
    echo(summary.line())
    echo(f"queue: {store.queue_counts()}")
    return summary


def export_captures(store: Store, cache, dest: str, *, limit: int = 500) -> int:
    """Materialise cached captures as a plain file tree (host/path)."""
    import os
    import urllib.parse
    from pathlib import Path

    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    rows = store.db.execute(
        "SELECT * FROM captures WHERE sha256 != '' AND path = '' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    written = 0
    for row in rows:
        data = cache.get(row["sha256"])
        if data is None:
            continue
        parts = urllib.parse.urlsplit(row["url"])
        rel = Path(parts.hostname or "unknown") / (
            (parts.path or "/").lstrip("/") or "index.html"
        )
        out = root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.stat().st_size == len(data):
            continue
        tmp = out.with_suffix(out.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, out)
        store.db.execute("UPDATE captures SET path=? WHERE id=?", (str(out), row["id"]))
        written += 1
    store.db.commit()
    return written
