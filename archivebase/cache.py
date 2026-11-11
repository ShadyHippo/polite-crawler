"""Content-addressed blob cache. Re-downloading the same bytes is a bug.

Captures are keyed by (url, timestamp) in the store; the bytes live in
`blobs/sha256/ab/abcdef...`. Two captures of the same file at different
timestamps share one blob, and a capture that is already recorded is never
fetched again — which is the "duplicate capture downloads = 0" promise.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class Cache:
    def __init__(self, blob_dir: str | Path):
        self.root = Path(blob_dir).expanduser()

    def path_for(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        dest = self.path_for(sha)
        if dest.exists():
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        return sha

    def get(self, sha: str) -> bytes | None:
        path = self.path_for(sha)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def has(self, sha: str) -> bool:
        return self.path_for(sha).exists()

    def size(self) -> tuple[int, int]:
        """(files, bytes) currently in the cache."""
        files = total = 0
        if not self.root.exists():
            return 0, 0
        for p in self.root.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return files, total
