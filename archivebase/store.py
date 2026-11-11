"""SQLite store: the catalog, the queue, the request log, the resume keys.

One database per project. Nothing in here is clever; it exists so that every
decision the crawler made is still answerable a week later:

  * what did we discover, from where, and when
  * which captures did we actually hold, and did the bytes match the name
  * which queue item is pending / done / blocked, and why
  * what were the last N HTTP requests, with status and headers
  * where an interrupted CDX paging run got to
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    id        INTEGER PRIMARY KEY,
    url       TEXT NOT NULL UNIQUE,
    host      TEXT,
    scope     TEXT,
    source    TEXT,
    first_seen REAL,
    last_seen  REAL
);

CREATE TABLE IF NOT EXISTS captures (
    id        INTEGER PRIMARY KEY,
    url       TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    status    INTEGER,
    mime      TEXT,
    length    INTEGER,
    digest    TEXT,
    sniffed   TEXT,
    nbytes    INTEGER,
    sha256    TEXT,
    fetched_at REAL,
    path      TEXT,
    UNIQUE(url, timestamp)
);
CREATE INDEX IF NOT EXISTS captures_url ON captures(url);

CREATE TABLE IF NOT EXISTS queue (
    id       INTEGER PRIMARY KEY,
    url      TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL DEFAULT 'capture',
    want     TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    state    TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    note     TEXT NOT NULL DEFAULT '',
    created  REAL,
    updated  REAL,
    UNIQUE(url, timestamp, kind)
);
CREATE INDEX IF NOT EXISTS queue_state ON queue(state, priority DESC, id);

CREATE TABLE IF NOT EXISTS requests (
    id        INTEGER PRIMARY KEY,
    ts        REAL,
    url       TEXT,
    host      TEXT,
    status    INTEGER,
    action    TEXT,
    nbytes    INTEGER,
    elapsed_ms INTEGER,
    attempt   INTEGER,
    retry_after TEXT,
    content_type TEXT,
    note      TEXT,
    run       TEXT
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts DESC);

CREATE TABLE IF NOT EXISTS queries (
    key        TEXT PRIMARY KEY,
    kind       TEXT,
    url        TEXT,
    params     TEXT,
    resume_key TEXT,
    pages      INTEGER DEFAULT 0,
    urls_found INTEGER DEFAULT 0,
    done       INTEGER DEFAULT 0,
    last_run   REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id       TEXT PRIMARY KEY,
    kind     TEXT,
    started  REAL,
    ended    REAL,
    requests INTEGER DEFAULT 0,
    ok       INTEGER DEFAULT 0,
    absent   INTEGER DEFAULT 0,
    failed   INTEGER DEFAULT 0,
    state    TEXT,
    reason   TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- discovery -------------------------------------------------------
    def add_url(self, url: str, *, scope: str = "", source: str = "") -> int:
        now = time.time()
        host = url.split("/")[2] if "://" in url and len(url.split("/")) > 2 else ""
        cur = self.db.execute(
            "INSERT INTO urls (url, host, scope, source, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen",
            (url, host, scope, source, now, now),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def enqueue(
        self,
        url: str,
        *,
        timestamp: str = "",
        kind: str = "capture",
        want: str = "",
        priority: int = 0,
        note: str = "",
        requeue_states: tuple[str, ...] = ("absent", "failed", "blocked"),
    ) -> bool:
        """Add a job. Returns True if it is new (or was requeued)."""
        now = time.time()
        row = self.db.execute(
            "SELECT id, state FROM queue WHERE url=? AND timestamp=? AND kind=?",
            (url, timestamp, kind),
        ).fetchone()
        if row:
            if row["state"] in requeue_states:
                self.db.execute(
                    "UPDATE queue SET state='pending', updated=?, note=? WHERE id=?",
                    (now, note, row["id"]),
                )
                self.db.commit()
                return True
            return False
        self.db.execute(
            "INSERT INTO queue (url, timestamp, kind, want, priority, state, created, updated, note) "
            "VALUES (?,?,?,?,?,'pending',?,?,?)",
            (url, timestamp, kind, want, priority, now, now, note),
        )
        self.db.commit()
        return True

    # -- working the queue ----------------------------------------------
    def next_pending(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM queue WHERE state IN ('pending','retry') "
            "ORDER BY priority DESC, id ASC LIMIT 1"
        ).fetchone()

    def pending(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM queue WHERE state IN ('pending','retry') "
            "ORDER BY priority DESC, id ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()

    def mark(self, item_id: int, state: str, note: str = "") -> None:
        self.db.execute(
            "UPDATE queue SET state=?, updated=?, attempts=attempts+1, note=? WHERE id=?",
            (state, time.time(), note[:500], item_id),
        )
        self.db.commit()

    def queue_counts(self) -> dict:
        rows = self.db.execute(
            "SELECT state, COUNT(*) AS n FROM queue GROUP BY state"
        ).fetchall()
        counts = {r["state"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    # -- captures --------------------------------------------------------
    def have_capture(self, url: str, timestamp: str = "") -> sqlite3.Row | None:
        if timestamp:
            return self.db.execute(
                "SELECT * FROM captures WHERE url=? AND timestamp=?", (url, timestamp)
            ).fetchone()
        return self.db.execute(
            "SELECT * FROM captures WHERE url=? ORDER BY timestamp DESC LIMIT 1", (url,)
        ).fetchone()

    def record_capture(self, **fields) -> None:
        cols = [
            "url", "timestamp", "status", "mime", "length", "digest",
            "sniffed", "nbytes", "sha256", "fetched_at", "path",
        ]
        values = [fields.get(c) for c in cols]
        if not values[9]:
            values[9] = time.time()
        self.db.execute(
            "INSERT INTO captures (%s) VALUES (%s) "
            "ON CONFLICT(url, timestamp) DO UPDATE SET "
            "status=excluded.status, mime=excluded.mime, length=excluded.length, "
            "digest=excluded.digest, sniffed=excluded.sniffed, nbytes=excluded.nbytes, "
            "sha256=excluded.sha256, fetched_at=excluded.fetched_at, path=excluded.path"
            % (", ".join(cols), ",".join("?" * len(cols))),
            values,
        )
        self.db.commit()

    def capture_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0])

    # -- request log -----------------------------------------------------
    def log_request(
        self,
        *,
        url: str,
        status: int,
        action: str,
        nbytes: int = 0,
        elapsed_ms: int = 0,
        attempt: int = 1,
        retry_after: str = "",
        content_type: str = "",
        note: str = "",
        run: str = "",
    ) -> None:
        host = url.split("/")[2] if "://" in url and len(url.split("/")) > 2 else ""
        self.db.execute(
            "INSERT INTO requests (ts,url,host,status,action,nbytes,elapsed_ms,attempt,"
            "retry_after,content_type,note,run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(), url, host, status, action, nbytes, elapsed_ms,
                attempt, retry_after, content_type, note[:500], run,
            ),
        )
        self.db.commit()

    def recent_requests(self, n: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()

    def request_count_today(self) -> int:
        day_start = time.time() - (time.time() % 86400)
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM requests WHERE ts >= ?", (day_start,)
            ).fetchone()[0]
        )

    def requests_for_run(self, run: str) -> int:
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM requests WHERE run=?", (run,)
            ).fetchone()[0]
        )

    # -- discovery cursor ------------------------------------------------
    def query_state(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM queries WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def save_query_state(self, key: str, **fields) -> None:
        existing = self.query_state(key)
        now = time.time()
        if existing is None:
            self.db.execute(
                "INSERT INTO queries (key,kind,url,params,resume_key,pages,urls_found,done,last_run) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    fields.get("kind", "cdx"),
                    fields.get("url", ""),
                    json.dumps(fields.get("params") or {}),
                    fields.get("resume_key", ""),
                    int(fields.get("pages", 0)),
                    int(fields.get("urls_found", 0)),
                    int(bool(fields.get("done", False))),
                    now,
                ),
            )
        else:
            self.db.execute(
                "UPDATE queries SET resume_key=?, pages=?, urls_found=?, done=?, last_run=? WHERE key=?",
                (
                    fields.get("resume_key", existing["resume_key"]),
                    int(fields.get("pages", existing["pages"])),
                    int(fields.get("urls_found", existing["urls_found"])),
                    int(bool(fields.get("done", existing["done"]))),
                    now,
                    key,
                ),
            )
        self.db.commit()

    # -- runs ------------------------------------------------------------
    def start_run(self, kind: str) -> str:
        run = "%s-%s" % (kind, uuid.uuid4().hex[:8])
        self.db.execute(
            "INSERT INTO runs (id,kind,started,state) VALUES (?,?,?,'running')",
            (run, kind, time.time()),
        )
        self.db.commit()
        return run

    def finish_run(self, run: str, *, state: str, reason: str = "", **totals) -> None:
        sets = ["ended=?", "state=?", "reason=?"]
        vals: list = [time.time(), state, reason[:500]]
        for col in ("requests", "ok", "absent", "failed"):
            if col in totals:
                sets.append(f"{col}=?")
                vals.append(int(totals[col]))
        vals.append(run)
        self.db.execute("UPDATE runs SET %s WHERE id=?" % ", ".join(sets), vals)
        self.db.commit()

    def recent_runs(self, n: int = 5) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM runs ORDER BY started DESC LIMIT ?", (n,)
        ).fetchall()
