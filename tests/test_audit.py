import tempfile
import unittest
from pathlib import Path

from archivebase.audit import audit_tree, render, summarise


class TestAudit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel: str, text: str):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_finds_urllib_and_curl(self):
        self.write("scripts/a.py", "import urllib.request\nurllib.request.urlopen(u)\n")
        self.write("scripts/b.sh", "curl -sS https://example.org/x\n")
        findings = audit_tree(self.root)
        kinds = {f.kind for f in findings}
        self.assertIn("urllib", kinds)
        self.assertIn("curl", kinds)

    def test_ignore_comment_is_honoured(self):
        self.write(
            "scripts/a.py",
            "import urllib.request  # archivebase-audit: ignore (one-off probe)\n",
        )
        self.assertEqual(audit_tree(self.root), [])

    def test_ignores_the_shell_itself(self):
        self.write("archivebase/client.py", "urllib.request.urlopen(x)\n")
        self.write("node_modules/x/index.js", "fetch('https://x')\n")
        self.assertEqual(audit_tree(self.root), [])

    def test_finds_js_fetch(self):
        self.write("ui/app.js", "fetch('https://example.org/data.json')\n")
        findings = audit_tree(self.root)
        self.assertEqual([f.kind for f in findings], ["js-fetch"])

    def test_summary_groups_by_file(self):
        self.write("a.py", "urllib.request.urlopen(u)\nurllib.request.urlopen(v)\n")
        self.write("b.sh", "wget https://x\n")
        rows = summarise(audit_tree(self.root))
        self.assertEqual(rows[0][0], "a.py")
        self.assertEqual(rows[0][1], 2)

    def test_render_says_clean_when_clean(self):
        self.assertIn("no direct HTTP calls", render([]))

    def test_render_points_at_the_lines(self):
        self.write("a.py", "urllib.request.urlopen(u)\n")
        text = render(audit_tree(self.root))
        self.assertIn("a.py:1", text)
        self.assertIn("Fetcher.get", text)

    def test_skip_paths(self):
        self.write("vendor/a.py", "urllib.request.urlopen(u)\n")
        self.write("src/b.py", "urllib.request.urlopen(u)\n")
        findings = audit_tree(self.root, skip_paths=("vendor",))
        self.assertEqual({f.path for f in findings}, {"src/b.py"})


if __name__ == "__main__":
    unittest.main()
