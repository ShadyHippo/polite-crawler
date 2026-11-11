import tempfile
import unittest
from pathlib import Path

from archivebase.config import (
    Config,
    ConfigError,
    load,
    validate_user_agent,
    write_default,
)


class TestUserAgent(unittest.TestCase):
    def test_plain_bot_agent_is_fine(self):
        ua = "BarbieArchive/1.0 (+https://github.com/ShadyHippo/BarbieArchive)"
        self.assertEqual(validate_user_agent(ua), ua)

    def test_compatible_mozilla_form_is_allowed(self):
        ua = "Mozilla/5.0 (compatible; BarbieArchive/1.0; +https://example.org/contact)"
        self.assertEqual(validate_user_agent(ua), ua)

    def test_browser_impersonation_is_refused(self):
        with self.assertRaises(ConfigError):
            validate_user_agent(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
            )

    def test_missing_contact_is_refused(self):
        with self.assertRaises(ConfigError):
            validate_user_agent("SomeBot/1.0")

    def test_missing_version_is_refused(self):
        with self.assertRaises(ConfigError):
            validate_user_agent("SomeBot (+https://example.org)")

    def test_override_exists_but_must_be_deliberate(self):
        ua = "Mozilla/5.0 Chrome/120 (+https://example.org)"
        self.assertEqual(validate_user_agent(ua, allow_browser=True), ua)


class TestLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_match_the_documented_policy(self):
        cfg = Config(project="x", contact="https://example.org")
        self.assertEqual(cfg.policy.min_gap_seconds, 10.0)
        self.assertEqual(cfg.policy.max_requests_per_day, 500)
        self.assertEqual(cfg.policy.max_runtime_seconds, 1800)
        self.assertEqual(cfg.policy.concurrency, 1)

    def test_effective_user_agent_uses_contact(self):
        cfg = Config(project="Barbie", version="0.2", contact="https://example.org/p")
        self.assertEqual(cfg.effective_ua, "Barbie/0.2 (+https://example.org/p)")

    def test_write_then_load(self):
        path = write_default(self.dir / "archivebase.toml",
                             project="proj", contact="https://example.org/c")
        cfg = load(path)
        self.assertEqual(cfg.project, "proj")
        self.assertEqual(cfg.robots, "warn")
        self.assertEqual(cfg.project_dir, cfg.gate_dir / "projects" / "proj")
        self.assertEqual(cfg.gate_dir, Config().gate_dir)

    def test_write_refuses_to_clobber(self):
        write_default(self.dir / "archivebase.toml", project="p", contact="https://e.org")
        with self.assertRaises(ConfigError):
            write_default(self.dir / "archivebase.toml", project="p", contact="https://e.org")

    def test_bad_config_is_an_error_naming_the_file(self):
        path = self.dir / "archivebase.toml"
        path.write_text("[policy\nmin_gap_seconds = 1\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load(path)

    def test_host_override(self):
        path = self.dir / "archivebase.toml"
        path.write_text(
            '[project]\nname="p"\ncontact="https://e.org"\n'
            '[policy]\nmin_gap_seconds = 5\n'
            '[[host]]\npattern="web.archive.org"\nmin_gap_seconds=30\n',
            encoding="utf-8",
        )
        cfg = load(path)
        self.assertEqual(cfg.gap_for("web.archive.org"), 30.0)
        self.assertEqual(cfg.gap_for("other.example"), 5.0)

    def test_robots_mode_is_validated(self):
        path = self.dir / "archivebase.toml"
        path.write_text(
            '[project]\nname="p"\ncontact="https://e.org"\n[robots]\nmode="sometimes"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load(path)


if __name__ == "__main__":
    unittest.main()
