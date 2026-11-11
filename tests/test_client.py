import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from archivebase import policy
from archivebase.client import Fetcher
from archivebase.config import Config, Policy
from archivebase.gate import Halt, ReviewRequired
from archivebase.runner import fetch_pending

SWF = b"FWS" + bytes([10]) + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 120


class Server:
    """A throwaway HTTP origin that records every request it receives."""

    def __init__(self):
        self.routes: dict[str, tuple] = {}
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                # Route on the path only; query strings are part of the test.
                route_key = self.path.split("?", 1)[0]
                route = outer.routes.get(route_key)
                if route is None:
                    body = b"not found"
                    self.send_response(404)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                status, body, headers = route
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path=""):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.server = Server()

    def tearDown(self):
        self.server.close()
        self._tmp.cleanup()

    def cfg(self, **policy_kw):
        pol = dict(
            min_gap_seconds=0.0,
            max_requests_per_day=1000,
            max_requests_per_run=50,
            attempts_per_url=1,
            timeout_seconds=10,
            max_bytes=2_000_000,
        )
        pol.update(policy_kw)
        return Config(
            project="test",
            contact="https://example.org/contact",
            data_dir=self.dir,
            gate_dir=self.dir,
            robots="off",
            policy=Policy(**pol),
        )

    def fetcher(self, **kw):
        return Fetcher(self.cfg(**kw))


class TestFetching(ClientTestCase):
    def test_ok_swf_is_cached_by_content(self):
        self.server.routes["/g.swf"] = (200, SWF, {"Content-Type": "application/x-shockwave-flash"})
        with self.fetcher() as f:
            r = f.get(self.server.url("/g.swf"), want="swf")
            self.assertTrue(r.ok, r.note)
            self.assertEqual(r.sniffed, "swf")
            self.assertTrue(r.sha256)
            self.assertEqual(f.cache.get(r.sha256), SWF)

    def test_404_is_recorded_as_absent_not_as_an_error(self):
        with self.fetcher() as f:
            r = f.get(self.server.url("/missing.swf"), want="swf")
            self.assertEqual(r.action, policy.ABSENT)
            self.assertEqual(r.status, 404)

    def test_recorded_capture_is_never_downloaded_twice(self):
        self.server.routes["/g.swf"] = (200, SWF, {})
        with self.fetcher() as f:
            first = f.get(self.server.url("/g.swf"), want="swf", timestamp="20200101000000")
            f.store.record_capture(
                url=self.server.url("/g.swf"),
                timestamp="20200101000000",
                status=first.status,
                mime=first.mime,
                length=len(first.body),
                digest="",
                sniffed=first.sniffed,
                nbytes=len(first.body),
                sha256=first.sha256,
                path="",
            )
            second = f.get(self.server.url("/g.swf"), want="swf", timestamp="20200101000000")
            self.assertTrue(second.from_cache)
            self.assertEqual(len([h for h in self.server.hits if h == "/g.swf"]), 1)

    def test_html_error_page_is_not_a_swf(self):
        self.server.routes["/g.swf"] = (
            200,
            b"<html><head><title>404 Not Found</title></head><body>nope</body></html>",
            {"Content-Type": "text/html"},
        )
        with self.fetcher() as f:
            r = f.get(self.server.url("/g.swf"), want="swf")
            self.assertEqual(r.action, policy.SUSPECT)
            self.assertEqual(r.sniffed, "html")
            self.assertIn("wanted swf", r.note)

    def test_gzip_body_is_rejected(self):
        self.server.routes["/m.mp3"] = (200, b"\x1f\x8b\x08\x00" + b"\x00" * 60, {})
        with self.fetcher() as f:
            r = f.get(self.server.url("/m.mp3"), want="mp3")
            self.assertEqual(r.action, policy.SUSPECT)
            self.assertEqual(r.sniffed, "gzip")

    def test_redirect_is_followed_as_a_second_gated_request(self):
        self.server.routes["/r.swf"] = (302, b"", {"Location": "/g.swf"})
        self.server.routes["/g.swf"] = (200, SWF, {})
        with self.fetcher() as f:
            r = f.get(self.server.url("/r.swf"), want="swf")
            self.assertTrue(r.ok, r.note)
            self.assertEqual(self.server.hits, ["/r.swf", "/g.swf"])

    def test_redirect_loop_stops(self):
        self.server.routes["/a.swf"] = (302, b"", {"Location": "/b.swf"})
        self.server.routes["/b.swf"] = (302, b"", {"Location": "/a.swf"})
        with self.fetcher(max_redirects=2, attempts_per_url=2) as f:
            r = f.get(self.server.url("/a.swf"), want="swf")
            self.assertEqual(r.action, policy.ERROR)
            self.assertIn("redirect", r.note)

    def test_png_is_accepted(self):
        self.server.routes["/p.png"] = (200, PNG, {"Content-Type": "image/png"})
        with self.fetcher() as f:
            r = f.get(self.server.url("/p.png"), want="png")
            self.assertTrue(r.ok)
            self.assertEqual(r.sniffed, "png")


class TestStopping(ClientTestCase):
    def test_429_stops_and_records_a_cooldown(self):
        self.server.routes["/t.swf"] = (429, b"slow down", {"Retry-After": "60"})
        with self.fetcher() as f:
            with self.assertRaises(Halt) as ctx:
                f.get(self.server.url("/t.swf"), want="swf")
            self.assertIsNotNone(ctx.exception.until)
            snap = f.gate.snapshot()
            self.assertFalse(snap["review_required"])
            self.assertGreater(snap["blocked_until"], 0)
            # The next attempt must not reach the network at all.
            with self.assertRaises(Halt):
                f.get(self.server.url("/t.swf"), want="swf")
            self.assertEqual(self.server.hits, ["/t.swf"])

    def test_403_disables_crawling_until_reviewed(self):
        self.server.routes["/x.swf"] = (403, b"go away", {})
        with self.fetcher() as f:
            with self.assertRaises(ReviewRequired):
                f.get(self.server.url("/x.swf"), want="swf")
            self.assertTrue(f.gate.snapshot()["review_required"])
            with self.assertRaises(ReviewRequired):
                f.get(self.server.url("/y.swf"), want="swf")
            self.assertEqual(self.server.hits, ["/x.swf"])

    def test_503_stops_with_a_cooldown(self):
        self.server.routes["/s.swf"] = (503, b"busy", {})
        with self.fetcher() as f:
            with self.assertRaises(Halt):
                f.get(self.server.url("/s.swf"), want="swf")
            self.assertGreater(f.gate.snapshot()["blocked_until"], 0)

    def test_500_retries_once_then_gives_up_without_halting(self):
        self.server.routes["/e.swf"] = (500, b"boom", {})
        with self.fetcher(attempts_per_url=2) as f:
            r = f.get(self.server.url("/e.swf"), want="swf")
            self.assertEqual(r.action, policy.RETRY)
            self.assertEqual(len(self.server.hits), 2)
            self.assertFalse(f.gate.snapshot()["review_required"])


class TestRobots(ClientTestCase):
    def cfg(self, mode="warn", **kw):
        return super().cfg(**kw).replace(robots=mode)

    def test_warn_mode_records_but_proceeds(self):
        self.server.routes["/robots.txt"] = (
            200, b"User-agent: *\nDisallow: /\n", {"Content-Type": "text/plain"}
        )
        self.server.routes["/g.swf"] = (200, SWF, {})
        with self.fetcher() as f:
            r = f.get(self.server.url("/g.swf"), want="swf")
            self.assertTrue(r.ok, r.note)
            self.assertIn("/robots.txt", self.server.hits)

    def test_enforce_mode_skips_the_url(self):
        self.server.routes["/robots.txt"] = (200, b"User-agent: *\nDisallow: /\n", {})
        self.server.routes["/g.swf"] = (200, SWF, {})
        with Fetcher(self.cfg("enforce")) as f:
            r = f.get(self.server.url("/g.swf"), want="swf")
            self.assertEqual(r.action, "robots")
            self.assertNotIn("/g.swf", self.server.hits)

    def test_missing_robots_txt_is_not_consent_but_does_not_block(self):
        self.server.routes["/g.swf"] = (200, SWF, {})
        with self.fetcher() as f:
            r = f.get(self.server.url("/g.swf"), want="swf")
            self.assertTrue(r.ok)

    def test_disabled_host_is_skipped(self):
        cfg = self.cfg().replace(hosts=())
        self.server.routes["/g.swf"] = (200, SWF, {})
        from archivebase.config import HostRule

        cfg = cfg.replace(hosts=(HostRule(pattern="127.0.0.1", enabled=False),))
        with Fetcher(cfg) as f:
            r = f.get(self.server.url("/g.swf"), want="swf")
            self.assertEqual(r.action, "robots")


class TestRunner(ClientTestCase):
    def test_a_run_is_bounded_and_marks_the_queue(self):
        self.server.routes["/a.swf"] = (200, SWF, {})
        self.server.routes["/b.swf"] = (200, SWF, {})
        cfg = self.cfg()
        with Fetcher(cfg) as f:
            f.store.enqueue(self.server.url("/a.swf"), want="swf")
            f.store.enqueue(self.server.url("/b.swf"), want="swf")
            summary = fetch_pending(f, echo=lambda *a, **k: None)
            self.assertEqual(summary.state, "empty")
            self.assertEqual(summary.ok, 2)
            counts = f.store.queue_counts()
            self.assertEqual(counts.get("ok"), 2)
            # A second run has nothing to do and must send no requests.
            before = len(self.server.hits)
            summary2 = fetch_pending(f, echo=lambda *a, **k: None)
            self.assertEqual(summary2.state, "empty")
            self.assertEqual(len(self.server.hits), before)

    def test_dry_run_touches_nothing(self):
        cfg = self.cfg()
        with Fetcher(cfg) as f:
            f.store.enqueue(self.server.url("/a.swf"), want="swf")
            summary = fetch_pending(f, dry_run=True, echo=lambda *a, **k: None)
            self.assertEqual(summary.state, "dry-run")
            self.assertEqual(self.server.hits, [])

    def test_absent_items_are_not_retried_ever(self):
        cfg = self.cfg()
        with Fetcher(cfg) as f:
            f.store.enqueue(self.server.url("/gone.swf"), want="swf")
            fetch_pending(f, echo=lambda *a, **k: None)
            self.assertEqual(len(self.server.hits), 1)
            fetch_pending(f, echo=lambda *a, **k: None)
            self.assertEqual(len(self.server.hits), 1)
            self.assertEqual(f.store.queue_counts().get("absent"), 1)


if __name__ == "__main__":
    unittest.main()
