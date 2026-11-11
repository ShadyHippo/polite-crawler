import unittest

from archivebase.cdx import Query, parse


SAMPLE = """\
http://barbie.com/games/a.swf 20100101000000 200 application/x-shockwave-flash 1234 ABCDEF
http://barbie.com/games/b.swf 20100202000000 200 application/x-shockwave-flash 99 GHIJKL

abcdefghijklmnop
"""


class TestParse(unittest.TestCase):
    def test_rows_and_resume_key(self):
        rows, key = parse(SAMPLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].original, "http://barbie.com/games/a.swf")
        self.assertEqual(rows[0].timestamp, "20100101000000")
        self.assertEqual(rows[0].ext, "swf")
        self.assertTrue(rows[0].is_asset)
        self.assertEqual(key, "abcdefghijklmnop")

    def test_no_resume_key(self):
        rows, key = parse("http://x/a.swf 20100101000000 200 application/x-shockwave-flash 1 D\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(key, "")

    def test_html_error_page_yields_nothing(self):
        """A block page must not look like 'no captures exist'."""
        rows, key = parse("<!DOCTYPE html><html><body>Blocked</body></html>")
        self.assertEqual(rows, [])
        self.assertEqual(key, "")

    def test_junk_lines_are_ignored(self):
        rows, _ = parse("not a row\n\nhttp://x/y.swf 20100101000000 200 x 1 D\n")
        self.assertEqual(len(rows), 1)

    def test_non_asset_mimetype(self):
        rows, _ = parse("http://x/page.html 20100101000000 200 text/html 10 D\n")
        self.assertFalse(rows[0].is_asset)


class TestQuery(unittest.TestCase):
    def test_defaults_are_narrow_and_paged(self):
        q = Query(prefix="barbie.com/games/")
        params = dict(q.params())
        self.assertEqual(params["matchType"], "prefix")
        self.assertEqual(params["limit"], "500")
        self.assertEqual(params["showResumeKey"], "true")
        filters = [v for k, v in q.params() if k == "filter"]
        self.assertEqual(filters, ["statuscode:200"])

    def test_resume_key_is_carried(self):
        q = Query(prefix="barbie.com/games/")
        self.assertIn("resumeKey=abc", q.url("abc"))

    def test_domain_scans_are_opt_in(self):
        q = Query(prefix="barbie.com", match_type="domain")
        self.assertIn("matchType=domain", q.url())
        self.assertNotEqual(Query(prefix="a/").key, Query(prefix="a/", match_type="domain").key)

    def test_page_size_is_capped(self):
        q = Query(prefix="x/", limit=10_000)
        self.assertEqual(dict(q.params())["limit"], "5000")

    def test_key_is_stable_and_specific(self):
        a = Query(prefix="x/")
        b = Query(prefix="x/")
        c = Query(prefix="x/", mimes=("image/png",))
        self.assertEqual(a.key, b.key)
        self.assertNotEqual(a.key, c.key)


class TestDiscovery(unittest.TestCase):
    """End to end through a local fake CDX endpoint."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from archivebase import cdx
        from archivebase.client import Fetcher
        from archivebase.config import Config, Policy
        from tests.test_client import Server

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.server = Server()
        self.cdx = cdx
        self.old_endpoint = cdx.CDX_ENDPOINT
        cdx.CDX_ENDPOINT = self.server.url("/cdx/search/cdx")
        self.cfg = Config(
            project="t", contact="https://example.org/t",
            data_dir=self.dir, gate_dir=self.dir, robots="off",
            policy=Policy(min_gap_seconds=0.0, attempts_per_url=1,
                          max_requests_per_day=100),
        )
        self.Fetcher = Fetcher

    def tearDown(self):
        self.cdx.CDX_ENDPOINT = self.old_endpoint
        self.server.close()
        self._tmp.cleanup()

    def test_a_page_is_catalogued_and_the_resume_key_saved(self):
        body = (
            b"http://barbie.com/g/a.swf 20100101000000 200 "
            b"application/x-shockwave-flash 12 AAAA\n"
            b"http://barbie.com/g/page.html 20100101000000 200 text/html 5 BBBB\n"
            b"\nresumekey123456\n"
        )
        self.server.routes["/cdx/search/cdx"] = (200, body, {"Content-Type": "text/plain"})
        with self.Fetcher(self.cfg) as f:
            q = self.cdx.Query(prefix="barbie.com/g/")
            result = self.cdx.Discovery(f, f.store).run(q, max_pages=1, echo=lambda *a: None)
            self.assertEqual(result["state"], "ok")
            self.assertEqual(result["rows"], 2)
            self.assertEqual(result["added"], 1)          # only the SWF is queued
            state = f.store.query_state(q.key)
            self.assertEqual(state["resume_key"], "resumekey123456")
            self.assertEqual(f.store.queue_counts().get("pending"), 1)

    def test_second_page_uses_the_saved_key(self):
        body = b"http://barbie.com/g/a.swf 20100101000000 200 application/x-shockwave-flash 1 A\n"
        self.server.routes["/cdx/search/cdx"] = (200, body, {})
        with self.Fetcher(self.cfg) as f:
            q = self.cdx.Query(prefix="barbie.com/g/")
            d = self.cdx.Discovery(f, f.store)
            d.run(q, max_pages=1, echo=lambda *a: None)
            d.run(q, max_pages=1, echo=lambda *a: None)
            self.assertEqual(len(self.server.hits), 2)
            # The queue de-duplicates, so the second page adds nothing new.
            self.assertEqual(f.store.queue_counts().get("pending"), 1)

    def test_json_is_not_a_results_page(self):
        self.server.routes["/cdx/search/cdx"] = (
            200, b'[["urlkey"],["x"]]', {"Content-Type": "application/json"}
        )
        with self.Fetcher(self.cfg) as f:
            result = self.cdx.Discovery(f, f.store).run(
                self.cdx.Query(prefix="x/"), max_pages=1, echo=lambda *a: None
            )
            self.assertEqual(result["state"], "suspect")
            self.assertEqual(f.store.queue_counts().get("total", 0), 0)

    def test_html_block_page_is_not_an_empty_result(self):
        self.server.routes["/cdx/search/cdx"] = (
            200, b"<!DOCTYPE html><html><body>Blocked</body></html>",
            {"Content-Type": "text/html"},
        )
        with self.Fetcher(self.cfg) as f:
            result = self.cdx.Discovery(f, f.store).run(
                self.cdx.Query(prefix="x/"), max_pages=1, echo=lambda *a: None
            )
            self.assertEqual(result["state"], "suspect")


if __name__ == "__main__":
    unittest.main()
