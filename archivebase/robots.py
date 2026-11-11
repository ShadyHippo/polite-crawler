"""A small, honest robots.txt implementation.

`archive.org/robots.txt` currently disallows only `/control/` and `/report/`;
`web.archive.org` serves no robots.txt at all (404). So robots.txt is a signal,
not a permission slip: it cannot tell you the request rate a server is willing
to absorb, and a 404 on it is not consent.

Modes (`[robots] mode`):

  off      do not fetch it
  warn     fetch it, record what it says in the request log, and proceed
  enforce  fetch it and skip any URL it disallows

`warn` is the default because a dead robots.txt should not silently turn into
"this archive is closed", and because archive replay hosts routinely answer 404.
For a live origin site, use `enforce`.
"""

from __future__ import annotations

import re


def _compile(pattern: str) -> re.Pattern:
    pattern = pattern.strip()
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    rx = "".join(".*" if c == "*" else re.escape(c) for c in pattern)
    return re.compile("^" + rx + ("$" if anchored else ""))


def parse(text: str) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Return [(agents, [(directive, path), ...]), ...] in file order."""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if in_rules:
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(value.lower())
        elif field in ("allow", "disallow"):
            if not agents:
                agents = ["*"]
            in_rules = True
            if value:
                rules.append((field, value))
        # sitemap / crawl-delay / host are recorded but not acted on
    if agents or rules:
        groups.append((agents, rules))
    return groups


def _group_for(groups, ua: str) -> list[tuple[str, str]]:
    ua = ua.lower()
    specific: list[tuple[str, str]] = []
    wildcard: list[tuple[str, str]] = []
    for agents, rules in groups:
        for agent in agents:
            if agent == "*":
                wildcard.extend(rules)
            elif agent and agent in ua:
                specific.extend(rules)
    return specific or wildcard


def allows(text: str, ua: str, path: str) -> tuple[bool, str]:
    """Longest-match wins; Allow beats Disallow on a tie."""
    if not text.strip():
        return True, "empty robots.txt"
    rules = _group_for(parse(text), ua)
    if not rules:
        return True, "no matching group"
    best: tuple[int, bool, str] | None = None
    for directive, pattern in rules:
        if _compile(pattern).match(path):
            score = len(pattern)
            allow = directive == "allow"
            if best is None or score > best[0] or (score == best[0] and allow and not best[1]):
                best = (score, allow, pattern)
    if best is None:
        return True, "no rule matches"
    return best[1], f"{'Allow' if best[1] else 'Disallow'}: {best[2]}"
