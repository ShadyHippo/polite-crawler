# Polite Crawler

A request gate for archival crawling: one machine-wide slot, one daily budget,
and a stop button the *server* can press by answering `403`, `429` or `503`.

Build a crawler on top and the polite behaviour is the default behaviour, not
something you remember to add after the first complaint.

Python 3.11+, standard library only, no dependencies.

> Names: the distribution is **polite-crawler**, the import package is
> **`archivebase`** (its working name), and the command is **`arcb`**.

```bash
pip install -e .
arcb init --project MyMuseum --contact https://github.com/you/mymuseum
arcb doctor
arcb discover --prefix "example.com/activities/" --mime application/x-shockwave-flash
arcb fetch --limit 5
```

---

## The problem this solves

A personal preservation project wanted a few thousand Flash games off a dead
site. The obvious approach — a script per problem, each one fetching what it
needed — worked for months. Then the IP got a suspected-abuse warning, and the
audit of the codebase was uncomfortable:

```
$ arcb audit .
57 direct HTTP call(s) in 33 file(s):

     4  scripts/search.py      (urllib)
     3  scripts/asset_hunter.py (urllib)
     2  scripts/harvest.py      (urllib)
     ...
```

Each script was individually reasonable. Several had their own rate limit —
3 seconds here, 4 seconds there. The server saw the sum.

Three things had gone wrong, and only one of them was "went too fast":

1. **Politeness was per process.** Three scripts each limited to one request
   every ten seconds produce three requests every ten seconds.
2. **A block looked like a miss.** A `403` was returned as a plain status code,
   recorded like a `404`, and the crawler moved on to the next URL.
3. **A `200` was trusted.** Bodies were saved under whatever name the URL ended
   in, so HTML error pages and gzip blobs ended up stored as `.swf` and `.mp3`.

This package makes all three structurally hard. It is the shape a crawler should
have had from the start.

---

## How it works

```
        discover (cheap)                 fetch (deliberate)
  CDX pagination, resume keys  →  queue  →  gate  →  cache
                                              │
                                    403 → stop, human required
                                    429 → stop, honour Retry-After
                                    503 → stop, back off
                                    drop → stop, not "absent"
```

Everything that leaves the machine goes through `Fetcher.get()`. Before it does,
it takes the single global slot (a `flock`-guarded JSON file), waits out the
minimum gap, and charges the request to the daily budget. After it does, the
response is classified, the outcome is settled back into the gate, and the
request is appended to a log.

The gate is **one file for the whole machine**, not one per process and not one
per project. That is the entire point: the server sees the sum, so the budget
has to be the sum too.

---

## What it guarantees

| Property | How |
|---|---|
| One request in flight, machine-wide | `gate.json` + `flock`; a reservation holds the slot until the response is settled |
| A minimum gap (10 s default) | set in the same lock as the reservation |
| A hard daily ceiling (500 default) | counted in the gate, shared by every process and project on the box |
| Bounded runs (60 requests / 30 min) | checked before every queue item |
| No duplicate downloads | captures keyed `(url, timestamp)`; content-addressed blobs; a held capture is never re-fetched |
| A stop the server can trigger | 403 → review required; 429/503 → cooldown; both persisted to disk so a process started later inherits them |
| A stop a human can trigger | `arcb pause` writes a `PAUSED` file |
| No automatic resume after a block | review state only clears with `--i-have-reviewed --reason "…"` |
| Dropped connection ≠ absent file | treated as throttling; the job stops |
| A 200 is not trusted on faith | the body is sniffed; HTML is never saved as a SWF |
| Resumable discovery | CDX `showResumeKey` cursor persisted per query |
| Everything auditable | every request logged with status, timing, `Retry-After` and the decision taken |
| Identifiable | the User-Agent must carry a name/version and a contact URL, and may not impersonate a browser |

---

## Two stages

Discovery and retrieval are separate commands, because discovering 10,000 URLs
does not mean downloading 10,000 captures.

```bash
# Stage 1 — catalogue. Narrow, paginated, resumable. No payloads.
arcb discover --prefix "example.com/games/" \
              --mime application/x-shockwave-flash \
              --pages 1 --limit 500

# Stage 2 — retrieve. Bounded, cached, stops on objection.
arcb fetch --limit 5
```

Look before you leap:

```bash
arcb fetch --dry-run       # what would be fetched, no network
arcb status                # gate, queue, recent runs
arcb export --to ./museum  # write cached captures out as a host/path tree
```

---

## Commands

```
arcb init [--project N] [--contact URL]   write a starter archivebase.toml
arcb doctor                               is this project safe to run?
arcb status                               gate state, queue, recent runs
arcb discover --prefix URL                catalogue captures via CDX
arcb enqueue URL [URL…] --want swf        queue something you reviewed by hand
arcb fetch [--limit N] [--runtime S]      retrieve within budget
arcb export --to DIR                      materialise the cache as files
arcb pause [--reason TEXT]                stop everything now
arcb resume --reason TEXT                 clear a pause / cooldown / review
arcb log [-n N]                           recent requests and outcomes
arcb audit [PATH]                         find HTTP calls that bypass the gate
```

Exit codes are meaningful, so a supervisor or cron entry can react:

| code | meaning | what to do |
|---|---|---|
| `0` | queue empty, or the run's budget ran out normally | nothing |
| `75` | stopped temporarily: throttled, paused, cooling down, or the slot is busy | wait, then run again |
| `78` | stopped permanently until a human reviews a block | read `arcb log`, fix the cause, `arcb resume --i-have-reviewed` |
| `2` | config error or refused unsafe action | read the message |

---

## Python

```python
from archivebase import load, Fetcher
from archivebase.cdx import Query, Discovery

cfg = load()  # nearest archivebase.toml

with Fetcher(cfg) as f:
    r = f.get("https://example.com/game.swf", want="swf")
    if r.ok:
        print(r.sniffed, len(r.body), r.sha256)

    Discovery(f, f.store).run(Query(prefix="example.com/games/"), max_pages=1)
```

`get()` raises `ReviewRequired` (403), `Halt` (429/503/cooldown) or
`BudgetExhausted` (daily ceiling). Catch those and **stop** — they are not
"retry in a tight loop" signals.

---

## Configuration

```toml
[project]
name = "MyMuseum"
version = "0.1"
contact = "https://github.com/you/mymuseum"   # goes into the User-Agent

[policy]
concurrency = 1
min_gap_seconds = 10.0
max_requests_per_day = 500
max_requests_per_run = 60
max_runtime_seconds = 1800
attempts_per_url = 2          # 1 try + 1 retry, never immediate
backoff_seconds = [60, 300, 900, 3600]
stop_on_403 = true
stop_on_429 = true
stop_on_503 = true
stop_on_5xx = false
stop_on_drop = true

[robots]
mode = "warn"                 # off | warn | enforce

[[host]]
pattern = "web.archive.org"
min_gap_seconds = 30
max_requests_per_day = 80
```

`examples/archivebase.toml` is the annotated version;
`examples/archive-replay.toml` is a stricter profile for a project that has
already been asked to slow down.

State lives in two places on purpose:

```
~/.local/state/archivebase/          machine-wide gate
  gate.json                          slot, daily budget, cooldowns, review flag
  PAUSED                             one file that stops everything
  projects/<slug>/                   per project
    archivebase.db                   catalogue, queue, request log
    blobs/sha256/…                   content-addressed captures
```

---

## robots.txt is a signal, not a permission slip

Measured from a normal residential connection:

- `https://archive.org/robots.txt` → `200`, disallows only `/control/` and `/report/`
- `https://web.archive.org/robots.txt` → **`404`** (a 146-byte HTML error page)

A 404 on robots.txt is neither consent nor prohibition: it says nothing about
the request rate a host will absorb. That is why the default mode is `warn` —
record what it says and proceed — and why the gate, not robots.txt, is what
actually protects you. Use `enforce` for a live origin site.

---

## What this cannot do

- **It coordinates processes that use it.** A script calling `urllib` directly
  is invisible to the gate. That is what `arcb audit` is for; run it before
  launching a new crawler and after any refactor.
- **It does not make a server like you.** It makes you slow, scarce,
  identifiable, and quick to stop.
- **It is per machine.** A second machine has its own gate; halve both budgets.
- **It is not permission.** A polite User-Agent and a ten-second gap are good
  manners, not authorisation. If you plan to catalogue thousands of captures,
  ask the archive first — `POLICY.md` has a letter that does it.
- **The audit has blind spots.** It matches source patterns, so a `curl` command
  assembled from a list of arguments (or an HTTP call in a dependency) will not
  be seen. Treat a clean audit as a floor, not a ceiling.

---

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

93 tests, no network required except loopback. Two subprocesses prove the gap
and the single slot are genuinely shared; a local HTTP server proves that
403/429/503 stop the job, that a 404 is recorded as absent, that an HTML body is
never accepted as a SWF, and that a second run sends zero requests.
`tests/test_cli.py` drives the real CLI end to end.

---

## Layout

```
archivebase/
  gate.py      the file-locked global slot, budget and stop state
  policy.py    status code → decision (pure, testable)
  client.py    the only network path: robots, gate, UA, sniff, log
  cdx.py       narrow paginated CDX discovery with persisted resume keys
  store.py     SQLite: catalogue, queue, request log, runs, cursors
  cache.py     content-addressed blob cache
  magic.py     body sniffing (a 200 is not automatically a success)
  robots.py    small robots.txt parser
  runner.py    bounded, resumable queue worker
  audit.py     find HTTP calls that bypass the gate
  report.py    status / log rendering
  cli.py       `arcb`
tests/         the proof
examples/      annotated configs
POLICY.md      the politeness contract, and how to talk to an archive
```

---

## License

MIT — see [LICENSE](LICENSE).
