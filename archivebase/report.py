"""Human-readable state: what the gate is doing, what is queued, what happened."""

from __future__ import annotations

import time

from .gate import iso


def _fmt_ts(ts) -> str:
    try:
        return iso(float(ts)) or "—"
    except (TypeError, ValueError):
        return "—"


def status_text(cfg, gate, store, cache=None) -> str:
    st = gate.snapshot()
    pol = cfg.policy
    out: list[str] = []
    out.append(f"archivebase — {cfg.project}")
    out.append(f"  user agent   {cfg.effective_ua}")
    out.append(f"  config       {cfg.path or '(built-in defaults)'}")
    out.append(f"  project data {cfg.project_dir}")
    out.append(f"  gate         {cfg.gate_dir}")
    out.append("")
    out.append("policy")
    out.append(f"  concurrency  {pol.concurrency} (the gate enforces 1 in flight)")
    out.append(f"  gap          {pol.min_gap_seconds:g}s between requests")
    out.append(f"  per day      {pol.max_requests_per_day} requests")
    out.append(f"  per run      {pol.max_requests_per_run} requests / {pol.max_runtime_seconds}s")
    out.append(f"  robots       {cfg.robots}")
    out.append("")

    out.append("gate")
    if st.get("paused"):
        out.append("  PAUSED       an operator pressed pause (arcb resume to clear)")
    if st.get("review_required"):
        out.append(f"  REVIEW       {st.get('review_reason')}")
        out.append(f"               since {_fmt_ts(st.get('review_since'))}")
    blocked = float(st.get("blocked_until") or 0)
    if blocked > time.time():
        out.append(f"  cooling down until {_fmt_ts(blocked)} ({int(blocked - time.time())}s)")
        out.append(f"               {st.get('block_reason')}")
    out.append(f"  strikes      {st.get('strikes') or 0}")
    out.append(f"  today        {st.get('day_count') or 0} / {pol.max_requests_per_day}"
               f" on {st.get('day') or '—'}")
    out.append(f"  total sent   {st.get('attempts') or 0}")
    out.append(f"  last ok      {_fmt_ts(st.get('last_ok_at'))}")
    out.append(f"  last request {st.get('last_url') or '—'}  (HTTP {st.get('last_status') or '—'})")
    out.append("")

    counts = store.queue_counts()
    out.append("queue")
    for key in ("pending", "retry", "ok", "absent", "suspect", "failed", "skipped"):
        if counts.get(key):
            out.append(f"  {key:10s} {counts[key]}")
    out.append(f"  {'total':10s} {counts.get('total', 0)}")
    out.append("")
    out.append(f"  captures held   {store.capture_count()}")
    out.append(f"  requests today  {store.request_count_today()}")
    if cache is not None:
        files, size = cache.size()
        out.append(f"  blob cache      {files} files, {size / 1e6:.1f} MB")
    out.append("")

    events = list(st.get("events") or [])[-8:]
    if events:
        out.append("recent gate events")
        for ev in reversed(events):
            detail = " ".join(
                str(ev[k]) for k in ("url", "note", "wait") if ev.get(k)
            )[:90]
            out.append(f"  {ev.get('at_iso', '')}  {ev.get('kind', ''):10s} {detail}")
        out.append("")

    runs = store.recent_runs(3)
    if runs:
        out.append("recent runs")
        for r in runs:
            out.append(
                f"  {_fmt_ts(r['started'])}  {r['state'] or '?':10s} "
                f"requests={r['requests'] or 0} ok={r['ok'] or 0} "
                f"absent={r['absent'] or 0} failed={r['failed'] or 0}"
                + (f"  ({r['reason']})" if r["reason"] else "")
            )
        out.append("")

    out.append("next")
    if st.get("review_required"):
        out.append("  a block was recorded. Read `arcb log`, then either wait and")
        out.append("  `arcb resume --i-have-reviewed --reason \"...\"` or stop here.")
    elif st.get("paused"):
        out.append("  `arcb resume --reason \"...\"` when you are ready to continue")
    elif blocked > time.time():
        out.append(f"  nothing to do until {_fmt_ts(blocked)}")
    elif counts.get("pending"):
        out.append("  `arcb fetch` — one run, bounded by the policy above")
    else:
        out.append("  queue is empty; `arcb discover --prefix <url>` to look for more")
    return "\n".join(out)


def log_text(store, n: int = 20) -> str:
    rows = store.recent_requests(n)
    if not rows:
        return "no requests logged yet"
    out = [f"{'when':20s} {'status':>6s} {'action':10s} {'ms':>6s}  url"]
    for r in reversed(rows):
        out.append(
            f"{iso(r['ts']) or '—':20s} {str(r['status'] or '—'):>6s} "
            f"{r['action'] or '—':10s} {str(r['elapsed_ms'] or 0):>6s}  {r['url']}"
        )
        if r["note"]:
            out.append(f"{'':20s} {'':>6s} {'':10s} {'':>6s}  ↳ {r['note']}")
    return "\n".join(out)
