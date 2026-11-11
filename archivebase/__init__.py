"""archivebase — a polite base shell for archival crawling.

Every outbound request in a project built on this shell goes through one
process-wide *and* machine-wide gate: a file-locked reservation system that
enforces a single in-flight request, a minimum gap between requests, a daily
request budget, and an emergency stop that a server can trigger by answering
403, 429 or 503.

The shell is deliberately small and dependency-free (Python 3.11+, stdlib only)
so it can be vendored into a project and read by a human in one sitting.

    from archivebase import load, Fetcher
    cfg = load()
    with Fetcher(cfg) as f:
        r = f.get("https://example.org/game.swf", want="swf")
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # pragma: no cover - import-time guard
    raise RuntimeError("archivebase needs Python 3.11 or newer (tomllib)")

from .config import Config, ConfigError, Policy, load, validate_user_agent
from .gate import (
    BudgetExhausted,
    Gate,
    GateError,
    Halt,
    Paused,
    ReviewRequired,
)
from .client import FetchResult, Fetcher

__version__ = "0.1.0"

__all__ = [
    "BudgetExhausted",
    "Config",
    "ConfigError",
    "FetchResult",
    "Fetcher",
    "Gate",
    "GateError",
    "Halt",
    "Paused",
    "Policy",
    "ReviewRequired",
    "load",
    "validate_user_agent",
    "__version__",
]
