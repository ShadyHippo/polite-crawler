# Policy

The rules this shell enforces, the reasoning behind each number, and what to do
when a server says no. Every number is a **ceiling**, not a target. Lower is
always allowed.

These are engineering choices for a hobby archive project, **not published rate
limits for any archive**. No public documentation promises you a rate at all;
that is exactly why you have to pick a conservative one yourself and stop when
told.

---

## 1. The numbers

| Setting | Default | Why this value |
|---|---|---|
| Concurrent requests | **1** | One in flight, machine-wide. A slow 4 MB capture never overlaps the next request. |
| Requests per second | ~0.1 | The honest number: one every ten seconds. |
| Minimum delay | **10 s** | Unremarkable in a server log, and fast enough that a 60-request run finishes in half an hour. |
| Burst | **1** | No burst allowance. A burst is how "polite crawler" becomes "incident" when a retry loop runs away. |
| Requests per day | **500** | Shared by the whole machine, including retries and `robots.txt` fetches. |
| Requests per run | **60** | A single invocation cannot run away. |
| Run duration | **30 min** | Then it stops, with queue state saved. |
| Automatic retries | **1** total retry, never immediate | A retry is a request. The gate spaces it; a throttle pushes it further out. |
| Duplicate capture downloads | **0** | `(url, timestamp)` is unique in the cache. |
| Backoff after a block | **60 s → 5 m → 15 m → 1 h** | Grows, then caps. `Retry-After` can raise it, never lower it. |

The daily budget counts **attempts**, including failed ones, because the server
counts them the same way you do.

### Why it must be global

    Three scripts, each limited to one request every 10 seconds
    = three requests every 10 seconds.

Politeness is a property of the *machine*, not of a process. The state lives in
`~/.local/state/archivebase/gate.json` behind an `flock`, so every process —
including one started by cron a second later — takes its turn in the same queue.

The gate is stored once per machine, not once per project, so two projects on
the same box draw from the same 500 requests and the same `PAUSED` file.
Catalogues are per project; the politeness state is not.

If you run crawlers on two machines, halve both budgets. The gate cannot see
across the network.

---

## 2. Responses are instructions

| Response | Behaviour |
|---|---|
| `200` | Continue, after checking the body is actually the file requested |
| `301/302/307/308` | Follow as a **separate gated request** (a redirect loop still costs requests) |
| `404` / `410` | Record as absent, terminal. Never retried. |
| `400` / `405` | Your bug, not theirs. Record and stop retrying. |
| `401` / `403` | **Stop the entire job.** Set review-required. No automatic resume, ever. |
| `429` | **Stop the entire job.** Honour `Retry-After`; otherwise back off 60 s and grow. |
| `503` | **Stop the entire job.** Back off substantially. |
| other `5xx` | Limited retries with exponential backoff, then give up for this run. |
| dropped connection, reset, timeout, TLS error | **Throttling, never "the file is not there".** Job stops. |

That last row is not theoretical. A busy archive host sometimes answers a
request it does not want by dropping the TCP connection instead of returning
`429` — `urllib` reports `URLError`, `curl` reports `000`. A crawler that
catches that as "no such file" writes off files that are still there, and nobody
looks again.

### The 200 rule

A `200` proves the server answered; it does not prove you got the file. Three
real failure modes from an archival project:

- a `200` whose body was `<html><head><title>404 Not Found</title>` saved as
  `game.swf`
- a gzip body saved as `music.mp3`, because the server ignored
  `Accept-Encoding: identity`
- a 15-byte stub SWF (`FWS`, zero frames) recorded as a recovered game

So the body is sniffed and refused if it does not match what was asked for. A
`200` HTML page is `suspect`, not `ok`, and is never stored as an asset.

---

## 3. Identify yourself

```
YourProject/0.1 (+https://your-project-page.example/contact)
```

- A name, a version, and a contact URL an administrator can actually reach.
- Do **not** impersonate a browser. `Mozilla/5.0 (compatible; Name/1.0; +URL)`
  is an honest, long-standing convention and is accepted; a bare browser string
  is refused at startup.
- Do **not** rotate user agents or IPs to get around a block. If you find
  yourself doing that, the project has already failed.

The shell refuses to run if the User-Agent has no contact URL.

---

## 4. robots.txt

Measured 2026-11 from an ordinary residential connection:

| URL | Answer | Content |
|---|---|---|
| `https://archive.org/robots.txt` | `200` | disallows only `/control/` and `/report/` |
| `https://web.archive.org/robots.txt` | `404` | a 146-byte `nginx` HTML error page |

So archive replay publishes no robots.txt at all, and `archive.org`'s own file
says nothing about `/cdx/` or `/web/`. robots.txt tells you about paths, not
about load.

Default mode is `warn`: fetch it, log what it says, proceed. Use `enforce` for a
live origin site where it is a genuine crawl instruction.

---

## 5. What to do when you are blocked

The shell is deliberately unhelpful at the moment you most want to retry:

1. **403** → the job stops and keeps refusing, even after a cooldown, until a
   human clears it. `arcb fetch` exits `78`.
2. **429** → the job stops until a recorded time. `arcb fetch` exits `75`.
3. **503** → same, with a longer backoff.
4. Nothing is retried in a tight loop; nothing resumes on a timer.

Then, in order:

```bash
arcb log -n 40     # what exactly did the server say, and when
arcb status        # is it a block, a cooldown, or a pause?
```

- **Wait.** At minimum a day; longer after a second block.
- **Cut the rate.** Halve the daily budget; raise the gap.
- **Narrow the scope.** One directory prefix instead of a domain scan.
- **Find another source.** Another archive, a mirror, a volunteer rebuild — an
  archive is one source, not the only one.
- **Ask.** If the project needs thousands of captures, write first and describe
  the workflow before you run it.

Only then:

```bash
arcb resume --reason "waited 3 days, halved budget, narrowed to one prefix" \
            --i-have-reviewed
```

`--reason` is required and is written into the gate's event log. The point is
not the flag; it is that resuming after a block is a decision somebody made.

---

## 6. A letter worth sending

Before a large crawl, and after any block:

> Hello,
>
> I'm working on a personal digital preservation project to build a museum of
> archived [kind of content] — mostly material that is otherwise abandoned and
> no longer available anywhere.
>
> I previously experimented with automated discovery using the CDX API and
> archived capture URLs. My crawler was considerably more aggressive than it
> should have been, and I received a suspected-abuse warning.
>
> I've stopped that activity and want to make sure I approach the project
> responsibly before resuming.
>
> I'm happy to:
> - Register and use an account.
> - Identify my crawler with a descriptive User-Agent.
> - Limit concurrency to one request at a time.
> - Introduce substantial delays between requests.
> - Cache all results and avoid duplicate requests.
> - Stop immediately on rate-limit or abuse responses.
>
> Could you advise whether there are recommended request rates or specific APIs
> intended for this kind of research? I'd also appreciate knowing whether there
> is a way to register or identify this project so my automated research doesn't
> inadvertently trigger abuse protections.
>
> I appreciate the work you do to preserve digital history and want to make sure
> my project doesn't negatively affect your infrastructure.
>
> Thank you!

Ask before you scale, not after. An account does not imply a higher rate.

---

## 7. Scope control

The cheapest way to be polite is to want less.

- `matchType=prefix` with a real path. Domain-wide scans require `--yes` and
  should be rare.
- One page of results at a time; the resume key is saved.
- Filter what you ask for: `statuscode:200`, a specific mimetype.
- **Catalogue first, download later.** 10,000 discovered URLs is a spreadsheet,
  not a download list. Pick the 50 that matter.
- Prefer bulk mirrors for bulk data. One CDN request for a whole data pack beats
  a thousand crawler requests for the same bytes.

---

## 8. Checklist before a new crawl project

- [ ] `arcb init` with a real contact URL
- [ ] `arcb doctor` is clean
- [ ] `arcb audit <project>` shows no bypassing HTTP calls
- [ ] The source is the right one (mirror before archive, archive before scrape)
- [ ] The scope is a prefix, not a domain
- [ ] The first run is `--dry-run`, then `--limit 5`
- [ ] You know what `arcb log` will look like if it goes wrong
- [ ] If the plan is thousands of captures: the email has been sent and answered
