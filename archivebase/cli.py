"""Command line: `arcb <command>`.

    arcb init                     write archivebase.toml here
    arcb status                   what the gate is doing, what is queued
    arcb doctor                   check the policy is safe to run
    arcb discover --prefix URL    ask CDX for a page of captures (cheap)
    arcb enqueue URL              add one URL to the queue by hand
    arcb fetch                    download what is queued, within budget
    arcb export --to DIR          write cached captures out as files
    arcb pause | resume | log     operator controls
    arcb audit PATH               find HTTP calls that bypass the gate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import audit as audit_mod
from . import report
from .cache import Cache
from .cdx import Discovery, Query
from .client import Fetcher
from .config import Config, ConfigError, load, write_default
from .gate import Gate, GateError, Halt, ReviewRequired
from .runner import export_captures, fetch_pending
from .store import Store


def _gate(cfg: Config) -> Gate:
    return Gate(
        cfg.gate_dir,
        min_gap=cfg.policy.min_gap_seconds,
        daily_budget=cfg.policy.max_requests_per_day,
        backoff=cfg.policy.backoff_seconds,
        host_gaps=cfg.host_gaps,
        host_budgets=cfg.host_budgets,
        pause_path=cfg.pause_path,
    )


# ------------------------------------------------------------------ commands


def cmd_init(args) -> int:
    project = args.project or Path.cwd().name
    contact = args.contact or ""
    try:
        path = write_default(args.path or "archivebase.toml",
                             project=project, contact=contact)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    if not contact:
        print("")
        print("Now set [project].contact to a real reachable URL. The User-Agent")
        print("is built from it, and an administrator who cannot identify you")
        print("has only one option.")
    return 0


def cmd_status(args) -> int:
    cfg = load(args.config)
    gate = _gate(cfg)
    with Store(cfg.db_path) as store:
        print(report.status_text(cfg, gate, store, Cache(cfg.blob_dir)))
    return 0


def cmd_doctor(args) -> int:
    cfg = load(args.config)
    problems: list[str] = []
    warnings: list[str] = []

    print(f"archivebase {cfg.project}")
    print(f"  config     {cfg.path or '(defaults)'}")
    print(f"  user agent {cfg.effective_ua}")

    for label, directory in (("data dir", cfg.project_dir), ("gate dir", cfg.gate_dir)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"  {label:9s} {directory} (writable)")
        except OSError as exc:
            problems.append(f"{label} is not writable: {exc}")

    if cfg.policy.concurrency != 1:
        warnings.append(
            "policy.concurrency is not 1; the gate still allows only one "
            "request in flight, but a parallel worker would just block on it"
        )
    if cfg.policy.min_gap_seconds < 1:
        warnings.append("min_gap_seconds below 1s — that is not polite for an archive")
    if cfg.policy.max_requests_per_day > 1000:
        warnings.append(
            "max_requests_per_day above 1000 — check you have written permission"
        )
    if cfg.policy.attempts_per_url > 3:
        warnings.append("attempts_per_url above 3 — retries are traffic too")
    if cfg.robots == "enforce":
        warnings.append(
            "robots.mode=enforce: archive replay hosts often serve no robots.txt "
            "(web.archive.org answers 404), which is not consent either way"
        )
    if cfg.contact and "example" in cfg.contact:
        warnings.append("project.contact still says example — put a real one")

    gate = _gate(cfg)
    snap = gate.snapshot()
    if snap.get("review_required"):
        problems.append(
            f"crawling disabled pending review: {snap.get('review_reason')}"
        )
    if snap.get("paused"):
        warnings.append("PAUSED file present")
    if not cfg.pause_path.exists() and not snap.get("review_required"):
        print("  gate       clear")

    print("")
    for w in warnings:
        print(f"warn: {w}")
    for p in problems:
        print(f"FAIL: {p}")
    if problems:
        return 1
    print("ready: one request at a time, "
          f"{cfg.policy.min_gap_seconds:g}s apart, "
          f"{cfg.policy.max_requests_per_day}/day")
    return 0


def cmd_pause(args) -> int:
    cfg = load(args.config)
    _gate(cfg).pause(args.reason or "paused by operator")
    print(f"paused. {cfg.pause_path} exists; nothing will be requested.")
    return 0


def cmd_resume(args) -> int:
    cfg = load(args.config)
    gate = _gate(cfg)
    try:
        cleared = gate.resume(
            reason=args.reason,
            clear_block=args.clear_block,
            acknowledge=args.i_have_reviewed,
        )
    except ReviewRequired as exc:
        print(f"refusing to resume: {exc}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Read `arcb log` and the gate events first, then re-run with "
              "--i-have-reviewed --reason \"what I checked\".", file=sys.stderr)
        return 78
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if cleared.get("review"):
        print(f"cleared review: {cleared['review']}")
    if cleared.get("blocked_until"):
        print(f"cleared cooldown (was until {cleared['blocked_until']})")
    if cleared.get("pause"):
        print("cleared pause")
    if cleared.get("cooldown_until"):
        print(f"cooldown still in effect until {cleared['cooldown_until']} "
              f"({cleared['cooldown_seconds']}s) — it is a wait, not a lock")
        print("use --clear-block only if you know the server is fine with you now")
    print(f"ready: {cfg.policy.min_gap_seconds:g}s between requests")
    return 0


def cmd_discover(args) -> int:
    cfg = load(args.config)
    match_type = "domain" if args.broad else "prefix"
    if args.broad and not args.broad_confirmed:
        print("refusing a domain-wide CDX scan without --yes\n"
              "a domain scan is the request most likely to get you blocked; "
              "prefer --prefix with a path", file=sys.stderr)
        return 2
    query = Query(
        prefix=args.prefix,
        match_type=match_type,
        collapse="" if args.no_collapse else "urlkey",
        filters=tuple(["statuscode:200"] + [f for f in (args.filter or [])]),
        mimes=tuple(args.mime or ()),
        limit=args.limit,
        from_ts=args.from_ts or "",
        to_ts=args.to_ts or "",
    )
    print(f"discovery: {query.prefix} ({match_type}, page={query.limit})")
    print(f"  query {query.url('')[:160]}")
    with Fetcher(cfg) as f:
        result = Discovery(f, f.store).run(
            query,
            max_pages=args.pages,
            enqueue=not args.no_enqueue,
            priority=args.priority,
        )
    print(f"  state={result['state']} rows={result['rows']} queued={result['added']} "
          f"pages={result['pages']} ({result['note']})")
    if result["added"]:
        print(f"  next: `arcb fetch --limit {min(result['added'], 5)}` "
              "(fetching is a separate, deliberate step)")
    return 0 if result["state"] == "ok" else 75


def cmd_enqueue(args) -> int:
    cfg = load(args.config)
    added = 0
    with Store(cfg.db_path) as store:
        for url in args.urls:
            store.add_url(url, source="manual")
            if store.enqueue(url, want=args.want or "", priority=args.priority,
                             timestamp=args.timestamp or ""):
                added += 1
        print(f"queued {added} of {len(args.urls)}; {store.queue_counts()}")
    return 0


def cmd_fetch(args) -> int:
    cfg = load(args.config)
    with Fetcher(cfg) as f:
        summary = fetch_pending(
            f,
            limit=args.limit,
            runtime=args.runtime,
            dry_run=args.dry_run,
        )
    if summary.state == "review":
        print("")
        print("Exit 78. This will keep refusing until a human clears it — by design.")
    return summary.exit_code


def cmd_export(args) -> int:
    cfg = load(args.config)
    with Store(cfg.db_path) as store:
        n = export_captures(store, Cache(cfg.blob_dir), args.to, limit=args.limit)
    print(f"wrote {n} file(s) under {args.to}")
    return 0


def cmd_log(args) -> int:
    cfg = load(args.config)
    with Store(cfg.db_path) as store:
        print(report.log_text(store, args.n))
    return 0


def cmd_audit(args) -> int:
    findings = audit_mod.audit_tree(
        args.path,
        skip_paths=tuple(args.skip or ()),
        include_artifacts=bool(getattr(args, "all", False)),
    )
    print(audit_mod.render(findings, root=args.path))
    return 1 if findings else 0


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    # --config has to work on both sides of the subcommand: "arcb --config X
    # status" and "arcb status --config X". The subparser copy suppresses its
    # default so it cannot overwrite a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="path to archivebase.toml")

    ap = argparse.ArgumentParser(
        prog="arcb",
        description="Polite base shell for archival crawling: one global slot, "
                    "one budget, one stop button.",
    )
    ap.add_argument("--config", help="path to archivebase.toml")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="write a starter archivebase.toml")
    p.add_argument("--project", help="project name (default: directory name)")
    p.add_argument("--contact", help="contact URL for the User-Agent")
    p.add_argument("--path", help="where to write (default: ./archivebase.toml)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", parents=[common], help="gate state, queue, recent activity")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", parents=[common], help="check this project is safe to run")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("pause", parents=[common], help="stop everything until resumed")
    p.add_argument("--reason", default="paused by operator")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", parents=[common], help="clear a pause / cooldown / review")
    p.add_argument("--reason", required=True, help="what you checked")
    p.add_argument("--clear-block", action="store_true",
                   help="also clear a cooldown (normally you wait it out)")
    p.add_argument("--i-have-reviewed", action="store_true",
                   help="acknowledge a server block explicitly")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("discover", parents=[common], help="catalogue captures via CDX (no downloads)")
    p.add_argument("--prefix", required=True, help="URL prefix to enumerate")
    p.add_argument("--mime", action="append", help="mimetype filter (repeatable)")
    p.add_argument("--filter", action="append", help="extra CDX filter")
    p.add_argument("--limit", type=int, default=500, help="rows per page (max 5000)")
    p.add_argument("--pages", type=int, default=1, help="pages to fetch this run")
    p.add_argument("--from", dest="from_ts", help="earliest timestamp (YYYYMMDD…)")
    p.add_argument("--to", dest="to_ts", help="latest timestamp")
    p.add_argument("--no-collapse", action="store_true",
                   help="keep every capture instead of one per URL")
    p.add_argument("--no-enqueue", action="store_true",
                   help="catalogue only; queue nothing for download")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--broad", action="store_true",
                   help="matchType=domain (needs --yes)")
    p.add_argument("--yes", dest="broad_confirmed", action="store_true",
                   help="confirm a domain-wide scan")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("enqueue", parents=[common], help="queue URLs by hand, after reviewing them")
    p.add_argument("urls", nargs="+")
    p.add_argument("--want", help="expected extension (swf, png, …)")
    p.add_argument("--timestamp", help="capture timestamp")
    p.add_argument("--priority", type=int, default=0)
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("fetch", parents=[common], help="retrieve queued captures within budget")
    p.add_argument("--limit", type=int, help="requests this run")
    p.add_argument("--runtime", type=int, help="seconds this run")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("export", parents=[common], help="write cached captures out as a file tree")
    p.add_argument("--to", required=True, help="destination directory")
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("log", parents=[common], help="recent requests")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("audit", parents=[common], help="find HTTP calls that bypass the gate")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--skip", action="append", help="relative path to skip")
    p.add_argument("--all", action="store_true",
                   help="also scan preserved artifacts (games/, content/, vendor/)")
    p.set_defaults(func=cmd_audit)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Halt as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return exc.exit_code
    except ReviewRequired as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return exc.exit_code
    except GateError as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
