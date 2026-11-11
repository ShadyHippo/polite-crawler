"""The one and only network path.

Everything that leaves this machine passes through `Fetcher.get()`. It exists
to make the polite behaviour the *easy* behaviour:

    cfg  = load()                      # policy from archivebase.toml
    with Fetcher(cfg) as f:
        r = f.get(url, want="swf")

What that call guarantees, in order:

1. The cache is checked first. A capture already held is never re-downloaded.
2. robots.txt is consulted (per mode) and the answer is recorded.
3. The global gate reserves the single machine-wide slot. If any process is
   over budget, in cooldown, paused, or blocked, this raises — it does not wait
   quietly and it does not retry.
4. A descriptive User-Agent is sent, `Accept-Encoding: identity` (so a gzip
   body can never be mistaken for the asset), and the body is capped.
5. The response is classified by `policy.classify()`. A 403 stops the job and
   demands a human. A 429/503 stops the job until a recorded time. A dropped
   connection is treated as throttling, never as "the file is not there".
6. A 200 is not trusted on faith: the bytes have to look like the thing asked
   for (`magic.check`), which is what stops an HTML error page being saved as
   `game.swf`.
7. The outcome is settled into the gate and appended to the request log.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import magic, policy, robots as robots_mod
from .cache import Cache
from .config import Config
from .gate import Gate, GateError, Halt, ReviewRequired, host_of
from .store import Store

REDIRECT_CODES = (301, 302, 303, 307, 308)


@dataclass
class FetchResult:
    url: str
    status: int = 0
    action: str = policy.ERROR
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    mime: str = ""
    sniffed: str = ""
    elapsed_ms: int = 0
    attempts: int = 0
    from_cache: bool = False
    sha256: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.action == policy.OK


class BodyTooLarge(Exception):
    """The body exceeded policy.max_bytes. Terminal, not retryable."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are hops, and every hop must go back through the gate."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _headers_dict(msg) -> dict:
    if not msg:
        return {}
    return {k.lower(): v for k, v in msg.items()}


class Fetcher:
    def __init__(
        self,
        config: Config,
        *,
        store: Store | None = None,
        gate: Gate | None = None,
        cache: Cache | None = None,
        stream_size: bool = True,
    ):
        self.cfg = config
        self.policy = config.policy
        self.ua = config.effective_ua
        self.stream_size = stream_size

        self.store = store or Store(config.db_path)
        self.cache = cache or Cache(config.blob_dir)
        self.gate = gate or Gate(
            config.gate_dir,
            min_gap=config.policy.min_gap_seconds,
            daily_budget=config.policy.max_requests_per_day,
            backoff=config.policy.backoff_seconds,
            host_gaps=config.host_gaps,
            host_budgets=config.host_budgets,
            pause_path=config.pause_path,
        )
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._robots: dict[str, dict] = {}
        self._robots_path = config.project_dir / "robots.json"
        self._closed = False

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.store.close()

    # -- public API ------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        want: str | None = None,
        timestamp: str = "",
        cache: bool = True,
        allow_html: bool = False,
        extra_headers: dict | None = None,
        run: str = "",
    ) -> FetchResult:
        """Fetch one URL politely. Raises a GateError when the job must stop."""

        # 1. cache ------------------------------------------------------
        if cache:
            row = self.store.have_capture(url, timestamp)
            if row and row["sha256"]:
                data = self.cache.get(row["sha256"])
                if data is not None:
                    return FetchResult(
                        url=url,
                        status=int(row["status"] or 200),
                        action=policy.OK,
                        body=data,
                        mime=row["mime"] or "",
                        sniffed=row["sniffed"] or "",
                        from_cache=True,
                        sha256=row["sha256"],
                        note="served from cache",
                    )
            elif row and row["status"] in (404, 410):
                return FetchResult(
                    url=url,
                    status=int(row["status"]),
                    action=policy.ABSENT,
                    from_cache=True,
                    note="recorded absent",
                )

        # 2. robots / host policy ---------------------------------------
        if not self.cfg.host_enabled(host_of(url)):
            self.store.log_request(url=url, status=0, action="host-disabled",
                                   run=run, note="host disabled in config")
            return FetchResult(url=url, action="robots",
                               note="host disabled in archivebase.toml")

        allowed, why = self._robots_check(url)
        if not allowed:
            res = FetchResult(url=url, action="robots", note=why)
            self.store.log_request(
                url=url, status=0, action="robots", note=why, run=run
            )
            return res

        # 3-7. gated attempts ------------------------------------------
        hops = 0
        target = url
        attempts = 0
        result = FetchResult(url=url)
        while attempts < max(1, int(self.policy.attempts_per_url)):
            attempts += 1
            raw = self._exchange(target, extra_headers)
            result.attempts = attempts
            result.elapsed_ms = raw.elapsed_ms

            if raw.exc is not None:
                verdict = (
                    policy.Verdict(policy.ERROR, note=f"body too large: {raw.exc}")
                    if isinstance(raw.exc, BodyTooLarge)
                    else policy.classify_exception(raw.exc)
                )
                self._settle(raw, verdict, run=run, attempt=attempts, body_len=0)
                if verdict.halt and self.policy.stop_on_drop:
                    snap = self.gate.snapshot()
                    raise Halt(
                        f"{verdict.explanation}\n"
                        "a dropped connection is treated as throttling, not as a "
                        "missing file — the job stopped instead of recording a "
                        "false absence",
                        until=float(snap.get("blocked_until") or 0) or None,
                        reason=verdict.explanation,
                    )
                return FetchResult(
                    url=target,
                    action=verdict.action,
                    attempts=attempts,
                    note=verdict.explanation,
                )

            status = raw.status
            headers = raw.headers
            retry_after = headers.get("retry-after", "")
            verdict = policy.classify(
                status,
                retry_after=retry_after,
                stop_on_5xx=self.policy.stop_on_5xx,
            )

            # A redirect is its own gated hop.
            if verdict.action == policy.REDIRECT and status in REDIRECT_CODES:
                location = headers.get("location", "")
                self._settle(raw, verdict, run=run, attempt=attempts, body_len=len(raw.body))
                if not location:
                    return FetchResult(
                        url=target, status=status, action=policy.ERROR,
                        attempts=attempts, note="redirect without Location",
                    )
                hops += 1
                if hops > self.policy.max_redirects:
                    return FetchResult(
                        url=target, status=status, action=policy.ERROR,
                        attempts=attempts,
                        note=f"more than {self.policy.max_redirects} redirects",
                    )
                target = urllib.parse.urljoin(target, location)
                attempts -= 1  # a redirect is not a failed attempt
                continue

            if verdict.action == policy.OK:
                acceptable, kind, reason = magic.check(
                    want, raw.body, allow_html=allow_html
                )
                if not acceptable:
                    suspect = policy.Verdict(
                        policy.SUSPECT, note=f"200 OK but {reason}"
                    )
                    self._settle(raw, suspect, run=run, attempt=attempts,
                                 body_len=len(raw.body), note=suspect.note)
                    return FetchResult(
                        url=target, status=status, action=policy.SUSPECT,
                        body=raw.body, headers=headers,
                        mime=headers.get("content-type", "").split(";")[0],
                        sniffed=kind, attempts=attempts, note=suspect.note,
                    )

                self._settle(raw, verdict, run=run, attempt=attempts,
                             body_len=len(raw.body))
                sha = self.cache.put(raw.body)
                mime = headers.get("content-type", "").split(";")[0].strip()
                return FetchResult(
                    url=target,
                    status=status,
                    action=policy.OK,
                    body=raw.body,
                    headers=headers,
                    mime=mime,
                    sniffed=kind,
                    elapsed_ms=raw.elapsed_ms,
                    attempts=attempts,
                    sha256=sha,
                )

            # Everything else settles, logs, then either stops or retries.
            self._settle(raw, verdict, run=run, attempt=attempts,
                         body_len=len(raw.body))

            if verdict.review or (status in (401, 403, 407) and self.policy.stop_on_403):
                raise ReviewRequired(
                    f"{verdict.explanation}\n"
                    f"stopped at {target}. No automatic retry, no cooldown-and-go: "
                    "review what the server said, then `arcb resume --reason \"...\" "
                    "--i-have-reviewed`.",
                    reason=verdict.explanation,
                )
            if verdict.halt:
                snap = self.gate.snapshot()
                raise Halt(
                    f"{verdict.explanation}\nstopping the job at {target}",
                    until=float(snap.get("blocked_until") or 0) or None,
                    reason=verdict.explanation,
                )
            if not verdict.retryable:
                return FetchResult(
                    url=target, status=status, action=verdict.action,
                    headers=headers, attempts=attempts, note=verdict.explanation,
                )
            # retryable (5xx with stop_on_5xx off): loop, the gate paces it
            result = FetchResult(
                url=target, status=status, action=verdict.action,
                headers=headers, attempts=attempts, note=verdict.explanation,
            )
            if attempts >= self.policy.attempts_per_url:
                return result

        return result

    # -- internals -------------------------------------------------------
    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "User-Agent": self.ua,
            "Accept": "*/*",
            "Accept-Encoding": "identity",  # a gzip body must never be saved
        }
        if extra:
            h.update(extra)
        return h

    def _http(self, url: str, headers: dict) -> tuple[int, bytes, dict]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(req, timeout=self.policy.timeout_seconds) as r:
                return r.status, self._read_capped(r), _headers_dict(r.headers)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, self._read_capped(exc), _headers_dict(exc.headers)
            finally:
                exc.close()

    def _read_capped(self, stream) -> bytes:
        cap = int(self.policy.max_bytes)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise BodyTooLarge(f"{total} bytes exceeds max_bytes={cap}")
            chunks.append(chunk)
        return b"".join(chunks)

    def _exchange(self, url: str, extra_headers: dict | None = None) -> "_Raw":
        res = self.gate.reserve(url)
        started = time.perf_counter()
        try:
            status, body, headers = self._http(url, self._headers(extra_headers))
        except Exception as exc:  # noqa: BLE001 - classified by policy
            return _Raw(
                res=res,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                exc=exc,
            )
        return _Raw(
            res=res,
            status=status,
            body=body,
            headers=headers,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    def _settle(self, raw: "_Raw", verdict: policy.Verdict, *, run: str,
                attempt: int, body_len: int, note: str = "") -> None:
        actionable = {
            policy.THROTTLE: "throttle",
            policy.DROP: "drop",
            policy.BLOCKED: "blocked",
            policy.ABSENT: "absent",
            policy.OK: "ok",
            policy.RETRY: "retry",
            policy.SUSPECT: "suspect",
            policy.ERROR: "error",
        }.get(verdict.action, "error")
        self.gate.settle(
            raw.res,
            action=actionable,
            status=raw.status,
            wait=verdict.wait,
            reason=verdict.note,
            note=note or verdict.note,
        )
        self.store.log_request(
            url=raw.res.url,
            status=raw.status,
            action=actionable,
            nbytes=body_len,
            elapsed_ms=raw.elapsed_ms,
            attempt=attempt,
            retry_after=str(raw.headers.get("retry-after", "")),
            content_type=str(raw.headers.get("content-type", ""))[:120],
            note=note or verdict.note,
            run=run,
        )

    # -- robots ----------------------------------------------------------
    def _robots_cache_load(self) -> None:
        try:
            self._robots = json.loads(self._robots_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._robots = {}

    def _robots_save(self) -> None:
        try:
            self._robots_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._robots_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._robots, indent=1), encoding="utf-8")
            tmp.replace(self._robots_path)
        except OSError:
            pass

    def _robots_check(self, url: str) -> tuple[bool, str]:
        mode = self.cfg.robots
        if mode == "off":
            return True, "robots disabled"

        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host or not self.cfg.host_enabled(host):
            return True, "host not gated by robots"
        origin = f"{parts.scheme}://{parts.netloc}"
        key = origin

        entry = self._robots.get(key)
        if entry is None and not self._robots:
            self._robots_cache_load()
            entry = self._robots.get(key)

        if entry is None:
            robots_url = origin + "/robots.txt"
            raw = self._exchange(robots_url)
            if raw.exc is not None:
                # Unreachable robots.txt is not "no rules": retry next process,
                # but do not hammer it either.
                self.gate.settle(raw.res, action="absent", status=0)
                self.store.log_request(url=robots_url, status=0, action="robots",
                                       note=f"robots.txt unreachable: {raw.exc}")
                self._robots[key] = {"status": 0, "text": "", "transient": True,
                                     "note": "unreachable", "fetched_at": time.time()}
                return True, "robots.txt unreachable"
            if raw.status >= 400:
                self.gate.settle(raw.res, action="absent", status=raw.status)
                entry = {"status": raw.status, "text": "",
                         "note": f"no robots.txt (HTTP {raw.status})"}
                self.store.log_request(url=robots_url, status=raw.status,
                                       action="robots", note=entry["note"])
            else:
                self.gate.settle(raw.res, action="ok", status=raw.status)
                entry = {"status": raw.status,
                         "text": raw.body.decode("utf-8", "replace")[:200000],
                         "note": "robots.txt read"}
                self.store.log_request(url=robots_url, status=raw.status,
                                       action="robots", nbytes=len(raw.body),
                                       note=entry["note"])
            entry["fetched_at"] = time.time()
            self._robots[key] = entry
            self._robots_save()

        if not entry.get("text"):
            return True, entry.get("note", "no robots.txt")

        allowed, why = robots_mod.allows(entry["text"], self.ua, parts.path or "/")
        if allowed:
            return True, why
        if mode == "enforce":
            return False, f"robots.txt disallows this path ({why})"
        self.store.log_request(url=url, status=0, action="robots-warn",
                               note=f"robots.txt would disallow it ({why})")
        return True, f"robots warns ({why}) but mode=warn"


@dataclass
class _Raw:
    res: object
    status: int = 0
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    exc: Exception | None = None
