"""Audit a project for HTTP calls that bypass the gate.

The failure mode this exists for: a project grows a hundred small scripts, each
with its own `urllib` call or `curl` line and its own idea of pacing. Individually
they look polite. Together they are a crawler with a hundred heads, and the
global limiter protects nothing because most traffic never touches it.

    $ arcb audit ~/Projects/SomeArchive
      scripts/harvest.sh            12 direct calls   (curl)
      scripts/brute_force.py         4 direct calls   (urllib.request)

One allow-listed exception is a line carrying an `archivebase-audit: ignore`
comment, so a deliberate one-off can be recorded next to the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", "site-packages", ".next", ".cache", "archivebase",
}

#: Directories that hold *preserved artifacts* rather than our code. Scanning
#: them finds thousands of minified third-party bundles and buries the real
#: answer. `--all` includes them.
ARTIFACT_DIRS = {
    "games", "game", "content", "media", "swf", "thumbs", "blobs", "vendor",
    "third_party", "3rdparty", "node_modules", "static", "public", "screenshots",
    "captures", "downloads",
}

SKIP_FILE_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock", ".svg", ".png", ".jpg", ".gif",
    ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".swf", ".bundle.js",
)

SCAN_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".php"}

IGNORE = re.compile(r"archivebase-audit:\s*ignore", re.IGNORECASE)

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("urllib", re.compile(r"\burllib\.request\.(urlopen|Request|build_opener)\b|\burllib2\b")),
    ("requests", re.compile(r"\brequests\.(get|post|put|head|delete|request|Session)\s*\(")),
    ("http.client", re.compile(r"\bhttp\.client\b|\bhttplib\b")),
    ("httpx/aiohttp", re.compile(r"\b(httpx|aiohttp)\.(get|post|ClientSession|AsyncClient)\b")),
    ("pycurl", re.compile(r"\bpycurl\b")),
    ("curl", re.compile(r"(?<![\w.@/-])(curl|wget)\s+[^\n]*https?://")),
    ("js-fetch", re.compile(r"\bfetch\s*\(\s*[`'\"]https?://|\bXMLHttpRequest\b")),
    ("node-http", re.compile(r"require\(['\"](https?|axios|node-fetch)['\"]\)|from ['\"]axios['\"]")),
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    code: str


def _iter_files(root: Path, *, include_artifacts: bool):
    skip = set(SKIP_DIRS)
    if not include_artifacts:
        skip |= ARTIFACT_DIRS
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in skip for part in rel_parts[:-1]):
            continue
        name = path.name.lower()
        if any(name.endswith(suffix) for suffix in SKIP_FILE_SUFFIXES):
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        yield path


def audit_tree(
    root: str | Path,
    *,
    skip_paths: tuple[str, ...] = (),
    include_artifacts: bool = False,
) -> list[Finding]:
    root = Path(root).expanduser().resolve()
    findings: list[Finding] = []
    for path in _iter_files(root, include_artifacts=include_artifacts):
        rel = str(path.relative_to(root))
        if any(rel.startswith(s.strip("/")) for s in skip_paths if s):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if IGNORE.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith("#") and "curl" not in stripped:
                # a comment that merely mentions urllib is documentation
                if not any(p.search(stripped) for p in (PATTERNS[5][1], PATTERNS[4][1])):
                    continue
            for kind, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(rel, lineno, kind, stripped[:160]))
                    break
    return findings


def summarise(findings: list[Finding]) -> list[tuple[str, int, set[str]]]:
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.path, []).append(f)
    rows = [
        (path, len(items), {i.kind for i in items})
        for path, items in by_file.items()
    ]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def render(findings: list[Finding], *, root: str = ".", limit: int = 40) -> str:
    if not findings:
        return (
            "no direct HTTP calls found outside archivebase\n"
            "every request in this tree is expected to go through Fetcher.get()"
        )
    out = [f"{len(findings)} direct HTTP call(s) in {len({f.path for f in findings})} file(s):", ""]
    for path, count, kinds in summarise(findings):
        out.append(f"  {count:4d}  {path}   ({', '.join(sorted(kinds))})")
    out.append("")
    out.append("Details:")
    for f in findings[:limit]:
        out.append(f"  {f.path}:{f.line}  [{f.kind}]  {f.code}")
    if len(findings) > limit:
        out.append(f"  ... {len(findings) - limit} more")
    out.append("")
    out.append(
        "Route each of these through Fetcher.get(url, want=...), or annotate the\n"
        "line with `# archivebase-audit: ignore` and say why in the commit message."
    )
    return "\n".join(out)
