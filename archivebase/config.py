"""Configuration: one TOML file, sane defaults, no magic.

The defaults are the whole point of this package, so they are intentionally
conservative and are what you get if you write no config at all:

* one request in flight, machine-wide
* at least 10 seconds between requests
* at most 500 requests per UTC day
* at most 60 requests / 30 minutes per run
* no automatic retry loop on a block: 403 stops and demands a human
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_GATE_DIR = Path(
    os.environ.get("ARCHIVEBASE_STATE", "~/.local/state/archivebase")
).expanduser()

ROBOTS_MODES = ("off", "warn", "enforce")


def _slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return text or "project"


class ConfigError(RuntimeError):
    """The config (or the user agent built from it) is not safe to run."""


# --------------------------------------------------------------- host rules


@dataclass(frozen=True)
class HostRule:
    """A per-host override. The global gate still applies; this only tightens."""

    pattern: str
    min_gap_seconds: float | None = None
    max_requests_per_day: int | None = None
    enabled: bool = True

    def matches(self, host: str) -> bool:
        pat = self.pattern.lower().strip()
        host = (host or "").lower().split(":")[0]
        if not pat:
            return False
        if pat.startswith("*."):
            base = pat[2:]
            return host == base or host.endswith("." + base)
        return host == pat or host.endswith("." + pat)


# ------------------------------------------------------------------- policy


@dataclass(frozen=True)
class Policy:
    """How fast we go and what we do when a server objects."""

    concurrency: int = 1
    min_gap_seconds: float = 10.0
    max_requests_per_day: int = 500
    max_requests_per_run: int = 60
    max_runtime_seconds: int = 1800
    # 1 attempt + 1 retry. A retry is *never* immediate: the gate spaces it,
    # and a throttling answer additionally raises the cooldown.
    attempts_per_url: int = 2
    backoff_seconds: tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0)
    respect_retry_after: bool = True
    stop_on_403: bool = True
    stop_on_429: bool = True
    stop_on_503: bool = True
    stop_on_5xx: bool = False
    stop_on_drop: bool = True
    max_redirects: int = 3
    max_bytes: int = 80 * 1024 * 1024
    timeout_seconds: float = 120.0


# ------------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    project: str = "archivebase"
    contact: str = ""
    user_agent: str = ""
    version: str = "0.1"
    policy: Policy = field(default_factory=Policy)
    robots: str = "warn"
    #: Per-project data: catalogue, queue, capture blobs. Defaults to
    #: `<gate_dir>/projects/<slug>`.
    data_dir: Path | None = None
    #: Machine-wide: the request slot, the daily budget, the PAUSED file.
    #: Shared by every project on this machine on purpose.
    gate_dir: Path = field(default_factory=lambda: DEFAULT_GATE_DIR)
    cache_dir: Path | None = None
    hosts: tuple[HostRule, ...] = ()
    allow_browser_ua: bool = False
    path: Path | None = None

    # -- derived ---------------------------------------------------------
    @property
    def effective_ua(self) -> str:
        if self.user_agent:
            return self.user_agent
        contact = self.contact.strip()
        if contact:
            return f"{self.project}/{self.version} (+{contact})"
        return f"{self.project}/{self.version}"

    @property
    def project_dir(self) -> Path:
        if self.data_dir is not None:
            return self.data_dir
        return self.gate_dir / "projects" / _slug(self.project)

    @property
    def db_path(self) -> Path:
        return self.project_dir / "archivebase.db"

    @property
    def gate_path(self) -> Path:
        return self.gate_dir / "gate.json"

    @property
    def pause_path(self) -> Path:
        return self.gate_dir / "PAUSED"

    @property
    def blob_dir(self) -> Path:
        return (self.cache_dir or (self.project_dir / "blobs")) / "sha256"

    def host_rule(self, host: str) -> HostRule | None:
        """Longest matching pattern wins, so web.archive.org beats archive.org."""
        best: HostRule | None = None
        for rule in self.hosts:
            if not rule.matches(host):
                continue
            if best is None or len(rule.pattern.lstrip("*.")) > len(
                best.pattern.lstrip("*.")
            ):
                best = rule
        return best

    def gap_for(self, host: str) -> float:
        rule = self.host_rule(host)
        if rule and rule.min_gap_seconds is not None:
            return max(0.0, float(rule.min_gap_seconds))
        return max(0.0, float(self.policy.min_gap_seconds))

    def host_enabled(self, host: str) -> bool:
        rule = self.host_rule(host)
        return True if rule is None else bool(rule.enabled)

    @property
    def host_gaps(self) -> dict[str, float]:
        return {
            h.pattern: float(h.min_gap_seconds)
            for h in self.hosts
            if h.min_gap_seconds is not None
        }

    @property
    def host_budgets(self) -> dict[str, int]:
        return {
            h.pattern: int(h.max_requests_per_day)
            for h in self.hosts
            if h.max_requests_per_day is not None
        }

    def replace(self, **kw) -> "Config":
        return replace(self, **kw)


# ------------------------------------------------------------------ loading


def _as_float(v, default: float) -> float:
    return float(default if v is None else v)


def _as_int(v, default: int) -> int:
    return int(default if v is None else v)


def _as_bool(v, default: bool) -> bool:
    return bool(default if v is None else v)


def _as_tuple(v, default: tuple[float, ...]) -> tuple[float, ...]:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return (float(v),)
    return tuple(float(x) for x in v)


def _policy_from(raw: dict) -> Policy:
    d = Policy()
    return Policy(
        concurrency=_as_int(raw.get("concurrency"), d.concurrency),
        min_gap_seconds=_as_float(raw.get("min_gap_seconds"), d.min_gap_seconds),
        max_requests_per_day=_as_int(
            raw.get("max_requests_per_day"), d.max_requests_per_day
        ),
        max_requests_per_run=_as_int(
            raw.get("max_requests_per_run"), d.max_requests_per_run
        ),
        max_runtime_seconds=_as_int(
            raw.get("max_runtime_seconds"), d.max_runtime_seconds
        ),
        attempts_per_url=_as_int(raw.get("attempts_per_url"), d.attempts_per_url),
        backoff_seconds=_as_tuple(raw.get("backoff_seconds"), d.backoff_seconds),
        respect_retry_after=_as_bool(
            raw.get("respect_retry_after"), d.respect_retry_after
        ),
        stop_on_403=_as_bool(raw.get("stop_on_403"), d.stop_on_403),
        stop_on_429=_as_bool(raw.get("stop_on_429"), d.stop_on_429),
        stop_on_503=_as_bool(raw.get("stop_on_503"), d.stop_on_503),
        stop_on_5xx=_as_bool(raw.get("stop_on_5xx"), d.stop_on_5xx),
        stop_on_drop=_as_bool(raw.get("stop_on_drop"), d.stop_on_drop),
        max_redirects=_as_int(raw.get("max_redirects"), d.max_redirects),
        max_bytes=_as_int(raw.get("max_bytes"), d.max_bytes),
        timeout_seconds=_as_float(raw.get("timeout_seconds"), d.timeout_seconds),
    )


def config_search_paths(cwd: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("ARCHIVEBASE_CONFIG")
    if env:
        paths.append(Path(env).expanduser())
    here = (cwd or Path.cwd()).resolve()
    for base in [here, *here.parents]:
        paths.append(base / "archivebase.toml")
    paths.append(DEFAULT_GATE_DIR / "archivebase.toml")
    return paths


def load(path: str | Path | None = None) -> Config:
    """Load the nearest archivebase.toml, or the built-in defaults."""
    cfg_path: Path | None = None
    if path:
        cfg_path = Path(path).expanduser()
        if not cfg_path.exists():
            raise ConfigError(f"no config at {cfg_path}")
    else:
        for candidate in config_search_paths():
            if candidate.is_file():
                cfg_path = candidate
                break

    if cfg_path is None:
        cfg = Config()
        validate_user_agent(cfg.effective_ua, allow_browser=cfg.allow_browser_ua)
        return cfg

    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{cfg_path}: {exc}") from exc

    project = raw.get("project", {}) or {}
    policy_raw = dict(raw.get("policy", {}) or {})
    robots_raw = raw.get("robots", {}) or {}
    storage = raw.get("storage", {}) or {}

    robots = str(robots_raw.get("mode", "warn")).lower()
    if robots not in ROBOTS_MODES:
        raise ConfigError(f"robots.mode must be one of {ROBOTS_MODES}, not {robots!r}")

    state_dir = storage.get("data_dir", storage.get("state_dir")) or None
    gate_dir = storage.get("gate_dir") or None
    cache_dir = storage.get("cache_dir") or None

    hosts = tuple(
        HostRule(
            pattern=str(h.get("pattern", "")),
            min_gap_seconds=(
                None if h.get("min_gap_seconds") is None else float(h["min_gap_seconds"])
            ),
            max_requests_per_day=(
                None if h.get("max_requests_per_day") is None
                else int(h["max_requests_per_day"])
            ),
            enabled=bool(h.get("enabled", True)),
        )
        for h in (raw.get("host") or [])
        if h.get("pattern")
    )

    cfg = Config(
        project=str(project.get("name", "archivebase")),
        contact=str(project.get("contact", "")),
        user_agent=str(project.get("user_agent", "") or ""),
        version=str(project.get("version", "0.1")),
        policy=_policy_from(policy_raw),
        robots=robots,
        data_dir=Path(str(state_dir)).expanduser() if state_dir else None,
        gate_dir=Path(str(gate_dir)).expanduser() if gate_dir else DEFAULT_GATE_DIR,
        cache_dir=Path(str(cache_dir)).expanduser() if cache_dir else None,
        hosts=hosts,
        allow_browser_ua=bool(project.get("allow_browser_ua", False)),
        path=cfg_path,
    )
    validate_user_agent(cfg.effective_ua, allow_browser=cfg.allow_browser_ua)
    return cfg


# ------------------------------------------------------------- user agent


_BROWSER_BRANDS = ("chrome/", "chromium/", "safari/", "firefox/", "edg/", "applewebkit")
_CONTACT_RE = re.compile(r"(\+https?://\S+|mailto:\S+|https?://\S+)")


def validate_user_agent(ua: str, *, allow_browser: bool = False) -> str:
    """Refuse to run with an anonymous or impersonating User-Agent.

    A crawler that cannot be identified cannot be contacted, and a crawler that
    pretends to be Chrome invites blocking instead of a conversation.
    """
    ua = (ua or "").strip()
    if not ua:
        raise ConfigError("project.user_agent is empty and no contact is set")
    low = ua.lower()

    if not allow_browser:
        impersonating = any(brand in low for brand in _BROWSER_BRANDS)
        # "Mozilla/5.0 (compatible; MyBot/1.0; +url)" is a long-standing,
        # honest convention. A bare "Mozilla/5.0 ... Chrome/120" is not.
        if impersonating and "(compatible;" not in low:
            raise ConfigError(
                "user agent impersonates a browser: %r\n"
                "Use '<Project>/<version> (+contact-url)'. Do not impersonate "
                "Chrome or rotate user agents to get around a block." % ua
            )

    if not _CONTACT_RE.search(ua):
        raise ConfigError(
            "user agent has no contact URL: %r\n"
            "Add project.contact (e.g. https://github.com/you/project) so an "
            "administrator can reach you before blocking you." % ua
        )

    if not re.search(r"[A-Za-z0-9_.-]+/\d", ua):
        raise ConfigError(f"user agent should carry a name/version token: {ua!r}")
    return ua


# ------------------------------------------------------------------- write

_TEMPLATE = """\
# archivebase — politeness policy for this project.
# Every value here is a *ceiling*, not a target. Lower is always allowed.

[project]
name = "{project}"
version = "0.1"
# A real, reachable contact. It goes into the User-Agent so an administrator
# can email you instead of blocking you.
contact = "{contact}"
# user_agent = ""      # defaults to "<name>/<version> (+<contact>)"

[policy]
concurrency = 1                 # the gate enforces this machine-wide
min_gap_seconds = 10.0          # >= 10s between requests
max_requests_per_day = 500      # hard daily ceiling, shared by all processes
max_requests_per_run = 60       # a single job cannot run away
max_runtime_seconds = 1800      # 30 minutes
attempts_per_url = 2            # 1 try + 1 retry, never immediate
backoff_seconds = [60, 300, 900, 3600]
respect_retry_after = true
stop_on_403 = true              # stop + manual review
stop_on_429 = true              # stop the whole job, back off
stop_on_503 = true              # stop, back off substantially
stop_on_5xx = false             # limited retries with backoff
stop_on_drop = true             # dropped connection = throttling, not "absent"
max_redirects = 3
max_bytes = 80000000
timeout_seconds = 120

[robots]
# off | warn | enforce
# "warn" fetches robots.txt, logs what it says, and proceeds. "enforce" obeys it.
mode = "warn"

[storage]
# Per-project catalogue, queue and blobs. Default:
#   ~/.local/state/archivebase/projects/<project-slug>
# data_dir = ""
# Machine-wide gate: one slot, one daily budget, one PAUSED file, shared by
# every project on this machine on purpose. Default: ~/.local/state/archivebase
# gate_dir = ""
# cache_dir = ""

# [[host]]
# pattern = "web.archive.org"
# min_gap_seconds = 15
# max_requests_per_day = 200
# enabled = true
"""


def write_default(path: str | Path, *, project: str, contact: str) -> Path:
    p = Path(path)
    if p.exists():
        raise ConfigError(f"{p} already exists — refusing to overwrite")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_TEMPLATE.format(project=project, contact=contact), encoding="utf-8")
    return p
