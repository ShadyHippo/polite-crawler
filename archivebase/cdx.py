"""Discovery: CDX queries that are narrow, paginated and resumable.

The rule this module enforces: **discovery is cheap, retrieval is expensive.**
A CDX query returns a catalogue; it does not entitle you to download the
catalogue. So this stage only fills the queue, and every page of results is
paid for out of the same global request budget as a download.

Politeness rules baked in:

* `matchType=prefix` by default. `domain` scans are refused unless the caller
  says `--broad`, because a domain-wide CDX query is the request that gets
  people blocked.
* Page size is capped (`limit=500` by default) and paging uses the documented
  `showResumeKey=true` cursor, persisted per query, so an interrupted run
  resumes exactly where it stopped instead of re-querying from the top.
* Only 200-status, non-HTML rows are catalogued.
* A CDX answer that is not a CDX answer (an HTML error page) is refused rather
  than parsed as "no results" — the false-absence bug in a different costume.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
FIELDS = "original,timestamp,statuscode,mimetype,length,digest"

#: Mimetypes that are worth cataloguing, and what we call them locally.
MIME_EXT = {
    "application/x-shockwave-flash": "swf",
    "application/futuresplash": "swf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "audio/mpeg": "mp3",
    "video/x-flv": "flv",
    "text/xml": "xml",
    "application/xml": "xml",
    "application/json": "json",
    "application/javascript": "js",
    "text/javascript": "js",
    "text/css": "css",
    "application/pdf": "pdf",
    "application/zip": "zip",
}

#: Rows the catalogue keeps even though they are not an "asset".
PAGE_MIMES = {"text/html", "text/plain"}


@dataclass(frozen=True)
class Query:
    prefix: str
    match_type: str = "prefix"
    collapse: str = "urlkey"
    filters: tuple[str, ...] = ("statuscode:200",)
    mimes: tuple[str, ...] = ()
    limit: int = 500
    from_ts: str = ""
    to_ts: str = ""

    @property
    def key(self) -> str:
        parts = [self.prefix, self.match_type, self.collapse,
                 "|".join(self.filters), "|".join(self.mimes),
                 str(self.limit), self.from_ts, self.to_ts]
        return "cdx::" + "::".join(parts)

    def params(self, resume_key: str = "") -> list[tuple[str, str]]:
        p: list[tuple[str, str]] = [
            ("url", self.prefix),
            ("matchType", self.match_type),
            ("output", "text"),
            ("fl", FIELDS),
            ("limit", str(max(1, min(int(self.limit), 5000)))),
            ("showResumeKey", "true"),
        ]
        if self.collapse:
            p.append(("collapse", self.collapse))
        for f in self.filters:
            p.append(("filter", f))
        for m in self.mimes:
            p.append(("filter", f"mimetype:{m}"))
        if self.from_ts:
            p.append(("from", self.from_ts))
        if self.to_ts:
            p.append(("to", self.to_ts))
        if resume_key:
            p.append(("resumeKey", resume_key))
        return p

    def url(self, resume_key: str = "") -> str:
        return CDX_ENDPOINT + "?" + urllib.parse.urlencode(self.params(resume_key))


@dataclass(frozen=True)
class Row:
    original: str
    timestamp: str
    status: str
    mime: str
    length: str
    digest: str

    @property
    def ext(self) -> str:
        return MIME_EXT.get(self.mime.split(";")[0].strip(), "")

    @property
    def is_asset(self) -> bool:
        return self.mime.split(";")[0].strip() in MIME_EXT


@dataclass
class Page:
    rows: list[Row] = field(default_factory=list)
    resume_key: str = ""
    http: int = 0
    note: str = ""


def parse(text: str) -> tuple[list[Row], str]:
    """Parse a CDX text page into (rows, resume_key)."""
    lines = text.splitlines()
    resume_key = ""

    # The resume key is a lone token on the last line, separated by a blank
    # line. Anything else (an HTML error page, a JSON blob) is not a page.
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx >= 1 and " " not in lines[idx].strip() and len(lines[idx].strip()) >= 8:
        prev = idx - 1
        while prev >= 0 and not lines[prev].strip():
            prev -= 1
        if prev != idx - 1:
            resume_key = lines[idx].strip()
            lines = lines[:idx]

    rows: list[Row] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        if not (parts[0].startswith("http://") or parts[0].startswith("https://")):
            continue
        if not (parts[1].isdigit() and len(parts[1]) == 14):
            continue
        parts += [""] * (6 - len(parts))
        rows.append(Row(*parts[:6]))
    return rows, resume_key


class Discovery:
    """Paginate a CDX query into the local catalogue, resumably."""

    def __init__(self, fetcher, store):
        self.f = fetcher
        self.store = store

    def run(
        self,
        query: Query,
        *,
        max_pages: int = 1,
        enqueue: bool = True,
        priority: int = 0,
        echo=print,
    ) -> dict:
        state = self.store.query_state(query.key) or {}
        resume = state.get("resume_key") or ""
        pages = int(state.get("pages") or 0)
        found = int(state.get("urls_found") or 0)
        added = 0
        page_total = 0

        for _ in range(max(1, max_pages)):
            url = query.url(resume)
            result = self.f.get(url, want="text")
            if not result.ok:
                self.store.save_query_state(
                    query.key, kind="cdx", url=query.prefix, pages=pages,
                    urls_found=found, done=state.get("done", False),
                    resume_key=resume,
                )
                echo(f"  discovery stopped: {result.action} — {result.note}")
                if result.action == "suspect":
                    echo("  (a CDX answer that is an HTML page is a block page, "
                         "not an empty result — refusing to record it as 'none')")
                return {"state": result.action, "rows": page_total, "added": added,
                        "pages": pages, "note": result.note}

            rows, next_key = parse(result.body.decode("utf-8", "replace"))
            head = result.body.lstrip()[:8].lower()
            if not rows and not next_key and (
                head.startswith(b"[") or head.startswith(b"{")
                or head.startswith(b"<?xml") or head.startswith(b"<!doc")
            ):
                # A JSON blob or an HTML/XML page is not a CDX result page.
                # Recording it as "no captures" is the false-absence bug again.
                return {"state": "suspect", "rows": 0, "added": 0, "pages": pages,
                        "note": "CDX did not return a results page (block page or error)"}

            page_total += len(rows)
            pages += 1
            for row in rows:
                self.store.add_url(row.original, scope=query.prefix, source="cdx")
                found += 1
                if enqueue and row.is_asset:
                    if self.store.enqueue(
                        row.original,
                        timestamp=row.timestamp,
                        kind="capture",
                        want=row.ext,
                        priority=priority,
                        note=f"cdx {row.mime}",
                    ):
                        added += 1

            resume = next_key
            self.store.save_query_state(
                query.key, kind="cdx", url=query.prefix, pages=pages,
                urls_found=found, done=not resume, resume_key=resume,
            )
            echo(f"  page {pages}: {len(rows)} rows, {added} queued, "
                 f"{'more' if resume else 'done'}")
            if not resume:
                break

        return {"state": "ok", "rows": page_total, "added": added, "pages": pages,
                "note": "resume key saved" if resume else "complete"}


def guess_want(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext if ext in set(MIME_EXT.values()) | {"html", "htm", "txt", "xml", "json"} else ""
