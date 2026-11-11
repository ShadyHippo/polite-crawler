import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from archivebase.gate import (
    BudgetExhausted,
    Gate,
    Halt,
    Paused,
    ReviewRequired,
)

ROOT = Path(__file__).resolve().parent.parent


CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from archivebase.gate import Gate

    state, gap, hold = sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    g = Gate(state, min_gap=gap, daily_budget=10000, max_wait=60)
    for _ in range(3):
        r = g.reserve("https://example.org/a.swf")
        print("%.5f" % time.time(), flush=True)
        time.sleep(hold)
        g.settle(r, action="ok", status=200)
    """
)


class GateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def gate(self, **kw):
        kw.setdefault("min_gap", 0.0)
        kw.setdefault("daily_budget", 1000)
        return Gate(self.dir, **kw)


class TestGlobalSpacing(GateTestBase):
    def _run(self, hold: float, gap: float):
        return subprocess.Popen(
            [sys.executable, "-c", CHILD, str(ROOT), str(self.dir), str(gap), str(hold)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_two_processes_share_one_gap(self):
        """The whole point: two crawlers must not each get their own rate."""
        gap = 0.4
        a, b = self._run(0.0, gap), self._run(0.0, gap)
        out_a, err_a = a.communicate(timeout=60)
        out_b, err_b = b.communicate(timeout=60)
        self.assertEqual(a.returncode, 0, err_a)
        self.assertEqual(b.returncode, 0, err_b)

        stamps = sorted(
            float(x)
            for blob in (out_a, out_b)
            for x in blob.split()
        )
        self.assertEqual(len(stamps), 6)
        diffs = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreaterEqual(
            min(diffs), gap * 0.75,
            f"requests closer than the global gap: {min(diffs):.3f}s",
        )

    def test_one_request_in_flight_across_processes(self):
        """A slow request blocks everybody else; it does not overlap."""
        hold = 0.8
        slow, fast = self._run(hold, 0.0), self._run(0.0, 0.0)
        out_slow, err_slow = slow.communicate(timeout=60)
        out_fast, err_fast = fast.communicate(timeout=60)
        self.assertEqual(slow.returncode, 0, err_slow)
        self.assertEqual(fast.returncode, 0, err_fast)

        slow_stamps = [float(x) for x in out_slow.split()]
        fast_stamps = [float(x) for x in out_fast.split()]
        self.assertEqual(len(slow_stamps), 3)
        self.assertEqual(len(fast_stamps), 3)

        # No request from the fast process may start while the slow one is
        # holding the slot. Absent the in-flight marker both would overlap.
        for fast_at in fast_stamps:
            for slow_at in slow_stamps:
                self.assertFalse(
                    slow_at + 0.05 < fast_at < slow_at + hold - 0.05,
                    f"fast request at {fast_at:.3f} overlapped the slow one "
                    f"reserved at {slow_at:.3f}",
                )


class TestStops(GateTestBase):
    def test_throttle_sets_a_cooldown_that_blocks_everyone(self):
        g = self.gate()
        r = g.reserve("https://example.org/x")
        g.settle(r, action="throttle", status=429, wait=120, reason="HTTP 429")
        snap = g.snapshot()
        self.assertGreater(snap["blocked_until"], time.time())
        with self.assertRaises(Halt):
            g.reserve("https://example.org/y")
        # A second, independent Gate object (another process) sees the same stop.
        with self.assertRaises(Halt):
            Gate(self.dir, min_gap=0.0).reserve("https://example.org/z")

    def test_403_requires_a_human(self):
        g = self.gate()
        r = g.reserve("https://example.org/x")
        g.settle(r, action="blocked", status=403, reason="HTTP 403")
        with self.assertRaises(ReviewRequired):
            g.reserve("https://example.org/x")
        # Even after the cooldown expires, review still blocks.
        with self.assertRaises(ReviewRequired):
            Gate(self.dir, min_gap=0.0).reserve("https://example.org/x")

    def test_resume_refuses_without_acknowledgement(self):
        g = self.gate()
        r = g.reserve("https://example.org/x")
        g.settle(r, action="blocked", status=403, reason="HTTP 403")
        with self.assertRaises(ReviewRequired):
            g.resume(reason="looked at it")
        g.resume(reason="looked at it", acknowledge=True, clear_block=True)
        g.reserve("https://example.org/x")  # no raise

    def test_resume_needs_a_reason(self):
        g = self.gate()
        with self.assertRaises(ValueError):
            g.resume(reason="  ")

    def test_pause_blocks_until_resumed(self):
        g = self.gate()
        g.pause("testing")
        with self.assertRaises(Paused):
            g.reserve("https://example.org/x")
        g.resume(reason="done testing")
        g.reserve("https://example.org/x")

    def test_daily_budget_is_a_hard_ceiling(self):
        g = self.gate(daily_budget=2)
        for _ in range(2):
            r = g.reserve("https://example.org/x")
            g.settle(r, action="ok", status=200)
        with self.assertRaises(BudgetExhausted):
            g.reserve("https://example.org/x")


class TestBookkeeping(GateTestBase):
    def test_strikes_reset_after_a_success(self):
        g = self.gate()
        r = g.reserve("https://example.org/x")
        g.settle(r, action="retry", status=500, reason="boom")
        self.assertEqual(g.snapshot()["strikes"], 1)
        r = g.reserve("https://example.org/x", attempt=2)
        g.settle(r, action="ok", status=200)
        self.assertEqual(g.snapshot()["strikes"], 0)

    def test_stale_in_flight_is_recovered(self):
        g = self.gate()
        r = g.reserve("https://example.org/x")
        # Simulate a process that died mid-request.
        state = json.loads((self.dir / "gate.json").read_text())
        state["in_flight"] = {"since": time.time() - 10_000, "pid": 999999,
                              "url": "https://example.org/x"}
        state["next_allowed_at"] = 0.0
        (self.dir / "gate.json").write_text(json.dumps(state))
        g2 = Gate(self.dir, min_gap=0.0, stale_after=60)
        res = g2.reserve("https://example.org/y")
        self.assertEqual(res.url, "https://example.org/y")
        g.settle(r, action="ok", status=200)

    def test_resume_reports_a_cooldown_it_did_not_clear(self):
        g = self.gate()
        r = g.reserve("https://example.org/x")
        g.settle(r, action="throttle", status=429, wait=120)
        cleared = g.resume(reason="paused only")
        self.assertIn("cooldown_until", cleared)
        self.assertGreater(cleared["cooldown_seconds"], 0)
        with self.assertRaises(Halt):
            g.reserve("https://example.org/x")

    def test_events_are_recorded(self):
        g = self.gate()
        r = g.reserve("https://example.org/x")
        g.settle(r, action="absent", status=404)
        kinds = [e["kind"] for e in g.snapshot()["events"]]
        self.assertIn("absent", kinds)

    def test_gap_override_per_host(self):
        g = self.gate(min_gap=10.0, host_gaps={"web.archive.org": 30.0})
        self.assertEqual(g.gap_for("web.archive.org"), 30.0)
        self.assertEqual(g.gap_for("example.org"), 10.0)

    def test_longest_host_pattern_wins(self):
        g = self.gate(host_gaps={"archive.org": 15.0, "web.archive.org": 30.0})
        self.assertEqual(g.gap_for("web.archive.org"), 30.0)
        self.assertEqual(g.gap_for("archive.org"), 15.0)

    def test_host_budget_is_enforced_separately(self):
        g = self.gate(daily_budget=100, host_budgets={"web.archive.org": 2})
        for _ in range(2):
            r = g.reserve("https://web.archive.org/web/1/x.swf")
            g.settle(r, action="ok", status=200)
        with self.assertRaises(BudgetExhausted):
            g.reserve("https://web.archive.org/web/1/y.swf")
        # Another host is unaffected.
        r = g.reserve("https://example.org/y.swf")
        g.settle(r, action="ok", status=200)


if __name__ == "__main__":
    unittest.main()
