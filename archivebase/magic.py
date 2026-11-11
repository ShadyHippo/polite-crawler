"""Content sniffing: never save an HTML error page under a .swf name.

Every one of these was found by hand in a real archive project:

  * a 200 OK whose body was `<html><head><title>404 Not Found</title>` saved as
    `game.swf`
  * a gzip-compressed body saved as `music.mp3` because the server ignored
    Accept-Encoding: identity
  * a 15-byte stub SWF (`FWS` + zero frames) recorded as a recovered game

So a 200 is not proof of success. The body has to look like what was asked for.
"""

from __future__ import annotations

SWF_MAGIC = (b"FWS", b"CWS", b"ZWS")
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
GZIP_MAGIC = (b"\x1f\x8b",)


def sniff(data: bytes) -> str:
    """Best-effort identification of a body from its first bytes."""
    if not data:
        return "empty"
    head = data[:1024]

    for magic in SWF_MAGIC:
        if head[:3] == magic:
            return "swf" if len(data) > 15 else "swf-stub"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head.startswith(ZIP_MAGIC):
        return "zip"
    if head.startswith(GZIP_MAGIC):
        return "gzip"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    if head[:4] == b"FLV\x01":
        return "flv"
    if head[:4] == b"RIFF":
        return "riff"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x20") or head[4:8] == b"ftyp":
        return "mp4"

    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    low = text[:400].lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html") or b"<head" in low[:200]:
        return "html"
    if text[:1] in (b"{", b"["):
        return "json"
    if low.startswith(b"<?xml") or (text[:1] == b"<" and b"<html" not in low[:200]):
        return "xml"
    if text and all(32 <= b < 127 or b in (9, 10, 13) for b in text[:128]):
        return "text"
    return "binary"


#: What we accept for a requested extension. Anything not listed accepts any
#: non-empty, non-HTML body.
EXPECTED: dict[str, set[str]] = {
    "swf": {"swf"},
    "png": {"png"},
    "jpg": {"jpeg"},
    "jpeg": {"jpeg"},
    "gif": {"gif"},
    "pdf": {"pdf"},
    "mp3": {"mp3"},
    "flv": {"flv"},
    "mp4": {"mp4"},
    "zip": {"zip"},
    "xml": {"xml", "text", "json"},
    "json": {"json", "text"},
    "js": {"text", "json", "xml", "html"},
    "css": {"text"},
    "html": {"html", "xml"},
    "htm": {"html", "xml"},
    "txt": {"text", "json", "xml"},
    "csv": {"text"},
    "woff": {"binary"},
    "woff2": {"binary"},
    "ttf": {"binary"},
    "otf": {"binary"},
    "bin": {"binary", "zip", "gzip"},
    "wav": {"riff"},
}

#: Never acceptable as a *saved artifact*, whatever was asked for.
NEVER_SAVE = {"empty", "swf-stub", "gzip", "html"}


def expected_for(want: str | None) -> set[str] | None:
    if not want:
        return None
    key = want.lower().lstrip(".")
    if "/" in key:  # a mimetype rather than an extension
        key = {
            "application/x-shockwave-flash": "swf",
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
            "audio/mpeg": "mp3",
            "text/xml": "xml",
            "application/xml": "xml",
            "application/json": "json",
            "text/html": "html",
            "application/pdf": "pdf",
            "video/x-flv": "flv",
        }.get(key.split(";")[0].strip(), "")
    return EXPECTED.get(key)


def check(want: str | None, data: bytes, *, allow_html: bool = False) -> tuple[bool, str, str]:
    """Return (acceptable, sniffed_kind, reason)."""
    kind = sniff(data)
    if kind == "empty":
        return False, kind, "empty body"
    if kind == "swf-stub":
        return False, kind, "SWF stub with no content"

    allowed = expected_for(want)
    if allowed is None:
        if kind in NEVER_SAVE and not allow_html:
            return False, kind, f"body is {kind}, not the file requested"
        return True, kind, ""

    if kind in allowed:
        # An HTML body behind a text-ish extension is only a warning-like pass
        # when HTML was explicitly asked for.
        return True, kind, ""
    return False, kind, f"wanted {want}, body is {kind}"
