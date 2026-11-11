import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from archivebase.cli import main
from tests.test_client import SWF, Server

CONFIG = """\
[project]
name = "Demo Archive"
version = "0.1"
contact = "https://example.org/demo"

[policy]
concurrency = 1
min_gap_seconds = 0
max_requests_per_day = 50
max_requests_per_run = 10
max_runtime_seconds = 60
attempts_per_url = 1
timeout_seconds = 10

[robots]
mode = "off"

[storage]
data_dir = "{state}/data"
gate_dir = "{state}/gate"
"""


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "state"
        self.config = self.dir / "archivebase.toml"
        self.config.write_text(CONFIG.format(state=self.state), encoding="utf-8")
        self.server = Server()

    def tearDown(self):
        self.server.close()
        self._tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.config), *argv])
        return code, out.getvalue() + err.getvalue()

    def test_config_flag_works_on_both_sides_of_the_subcommand(self):
        for argv in (
            ["--config", str(self.config), "status"],
            ["status", "--config", str(self.config)],
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("Demo Archive", out.getvalue(), argv)

    def test_status_before_anything_happens(self):
        code, text = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Demo Archive", text)
        self.assertIn("gate", text)
        self.assertIn("queue is empty", text)

    def test_discover_refuses_a_domain_scan_without_confirmation(self):
        code, text = self.run_cli("discover", "--prefix", "barbie.com", "--broad")
        self.assertEqual(code, 2)
        self.assertIn("--yes", text)

    def test_fetch_downloads_and_archives(self):
        self.server.routes["/g.swf"] = (200, SWF, {"Content-Type": "application/x-shockwave-flash"})
        code, text = self.run_cli("enqueue", self.server.url("/g.swf"), "--want", "swf")
        self.assertEqual(code, 0)
        code, text = self.run_cli("fetch")
        self.assertEqual(code, 0, text)
        self.assertIn("ok=1", text)

        code, text = self.run_cli("status")
        self.assertIn("captures held   1", text)

        # The archive is a file tree on demand, not a pile of blobs.
        dest = self.dir / "export"
        code, _ = self.run_cli("export", "--to", str(dest))
        self.assertEqual(code, 0)
        written = list(dest.rglob("g.swf"))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].read_bytes(), SWF)

    def test_a_403_stops_the_cli_with_exit_78_until_reviewed(self):
        self.server.routes["/x.swf"] = (403, b"nope", {})
        self.run_cli("enqueue", self.server.url("/x.swf"), "--want", "swf")

        code, text = self.run_cli("fetch")
        self.assertEqual(code, 78, text)
        self.assertIn("manual review", text)

        # Still refusing on the next run, with no further traffic.
        before = len(self.server.hits)
        code, _ = self.run_cli("fetch")
        self.assertEqual(code, 78)
        self.assertEqual(len(self.server.hits), before)

        # A reason alone is not enough: the human must acknowledge the block.
        code, text = self.run_cli("resume", "--reason", "looked at the log")
        self.assertEqual(code, 78)

        code, text = self.run_cli(
            "resume", "--reason", "checked: one 403, waited a day",
            "--i-have-reviewed",
        )
        self.assertEqual(code, 0, text)

    def test_doctor_reports_an_open_review(self):
        self.server.routes["/x.swf"] = (403, b"nope", {})
        self.run_cli("enqueue", self.server.url("/x.swf"), "--want", "swf")
        self.run_cli("fetch")
        code, text = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("crawling disabled pending review", text)

    def test_audit_fails_on_a_project_that_bypasses_the_gate(self):
        bad = self.dir / "bad"
        bad.mkdir()
        (bad / "harvest.sh").write_text("curl -sS https://example.org/x\n", encoding="utf-8")
        code, text = self.run_cli("audit", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("harvest.sh", text)

    def test_log_shows_the_request(self):
        self.server.routes["/g.swf"] = (200, SWF, {})
        self.run_cli("enqueue", self.server.url("/g.swf"), "--want", "swf")
        self.run_cli("fetch")
        code, text = self.run_cli("log", "-n", "5")
        self.assertEqual(code, 0)
        self.assertIn("/g.swf", text)
        self.assertIn("ok", text)


if __name__ == "__main__":
    unittest.main()
