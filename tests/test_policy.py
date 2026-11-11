import unittest

from archivebase import policy


class TestStatusTable(unittest.TestCase):
    def test_2xx_continues(self):
        v = policy.classify(200)
        self.assertEqual(v.action, policy.OK)
        self.assertFalse(v.halt)

    def test_404_is_an_answer_not_a_failure(self):
        v = policy.classify(404)
        self.assertEqual(v.action, policy.ABSENT)
        self.assertFalse(v.halt)
        self.assertFalse(v.retryable)
        self.assertEqual(policy.classify(410).action, policy.ABSENT)

    def test_403_stops_and_requires_review(self):
        v = policy.classify(403)
        self.assertEqual(v.action, policy.BLOCKED)
        self.assertTrue(v.halt)
        self.assertTrue(v.review)
        self.assertTrue(policy.classify(401).review)

    def test_429_stops_the_whole_job(self):
        v = policy.classify(429)
        self.assertEqual(v.action, policy.THROTTLE)
        self.assertTrue(v.halt)
        self.assertTrue(v.retryable)
        self.assertEqual(v.wait, 0.0)

    def test_429_honours_retry_after(self):
        self.assertEqual(policy.classify(429, retry_after="120").wait, 120.0)

    def test_503_backs_off_substantially(self):
        v = policy.classify(503)
        self.assertEqual(v.action, policy.THROTTLE)
        self.assertTrue(v.halt)

    def test_5xx_retries_but_does_not_always_halt(self):
        v = policy.classify(500)
        self.assertEqual(v.action, policy.RETRY)
        self.assertTrue(v.retryable)
        self.assertFalse(v.halt)
        self.assertTrue(policy.classify(500, stop_on_5xx=True).halt)

    def test_400_is_our_bug(self):
        v = policy.classify(400)
        self.assertEqual(v.action, policy.ERROR)
        self.assertFalse(v.retryable)
        self.assertFalse(v.halt)

    def test_redirects_are_their_own_hop(self):
        self.assertEqual(policy.classify(302).action, policy.REDIRECT)


class TestRetryAfter(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(policy.parse_retry_after("30"), 30.0)

    def test_http_date(self):
        # A date far in the past clamps to zero rather than going negative.
        self.assertEqual(
            policy.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1_700_000_000),
            0.0,
        )

    def test_garbage(self):
        self.assertIsNone(policy.parse_retry_after("soon"))
        self.assertIsNone(policy.parse_retry_after(""))

    def test_clamped_to_a_day(self):
        self.assertEqual(policy.parse_retry_after("999999"), 86400.0)


class TestDroppedConnections(unittest.TestCase):
    def test_a_drop_is_throttling_not_absence(self):
        v = policy.classify_exception(ConnectionResetError("reset by peer"))
        self.assertEqual(v.action, policy.DROP)
        self.assertTrue(v.halt)

    def test_timeout_is_a_drop(self):
        v = policy.classify_exception(TimeoutError("timed out"))
        self.assertEqual(v.action, policy.DROP)
        self.assertTrue(v.halt)


class TestCooldown(unittest.TestCase):
    def test_grows_then_caps(self):
        seq = (60.0, 300.0)
        self.assertEqual(policy.cooldown_for(1, seq), 60.0)
        self.assertEqual(policy.cooldown_for(2, seq), 300.0)
        self.assertEqual(policy.cooldown_for(9, seq), 300.0)

    def test_retry_after_can_raise_the_floor(self):
        self.assertEqual(policy.cooldown_for(1, (60.0,), floor=600.0), 600.0)


if __name__ == "__main__":
    unittest.main()
