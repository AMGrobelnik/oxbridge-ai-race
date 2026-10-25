"""Offline unit tests for the spending limits. Run: python -m unittest discover -s tests -v"""

import unittest

from agent.spending.policy import SpendingPolicy, micros_to_usd, read_spending_policy, usd_to_micros
from agent.spending.tracker import SpendingTracker


class SpendingTest(unittest.TestCase):
    def test_dollar_parsing_is_exact(self) -> None:
        self.assertEqual(usd_to_micros("0.01"), 10_000)
        self.assertEqual(usd_to_micros("1"), 1_000_000)
        self.assertEqual(usd_to_micros("0.000001"), 1)
        self.assertEqual(micros_to_usd(250_000), "0.25")
        self.assertEqual(micros_to_usd(1_000_000), "1.00")
        for bad in ("-1", "0.0000001", "abc"):
            with self.assertRaises(ValueError):
                usd_to_micros(bad)

    def test_policy_defaults_and_validation(self) -> None:
        self.assertEqual(read_spending_policy({}), SpendingPolicy(budget_micros=1_000_000, max_payment_micros=100_000))
        with self.assertRaises(ValueError):
            read_spending_policy({"AGENT_BUDGET_USD": "0.05", "MAX_PAYMENT_USD": "0.10"})
        with self.assertRaises(ValueError):
            read_spending_policy({"MAX_PAYMENT_USD": "0"})

    def test_per_payment_cap(self) -> None:
        t = SpendingTracker(read_spending_policy({"AGENT_BUDGET_USD": "1", "MAX_PAYMENT_USD": "0.10"}))
        self.assertFalse(t.try_reserve(usd_to_micros("0.11")).ok)
        self.assertTrue(t.try_reserve(usd_to_micros("0.10")).ok)

    def test_total_budget_release_and_commit(self) -> None:
        t = SpendingTracker(read_spending_policy({"AGENT_BUDGET_USD": "0.03", "MAX_PAYMENT_USD": "0.01"}))
        a, b, c = t.try_reserve(10_000), t.try_reserve(10_000), t.try_reserve(10_000)
        self.assertTrue(a.ok and b.ok and c.ok)
        d = t.try_reserve(10_000)
        self.assertFalse(d.ok)
        self.assertIn("exceed the budget", d.reason)

        t.commit(a.reservation_id)
        t.release(b.reservation_id)
        self.assertEqual(t.snapshot()["spentUsd"], "0.02")
        self.assertEqual(t.snapshot()["payments"], 2)
        self.assertTrue(t.try_reserve(10_000).ok)
        # Releasing a committed payment does nothing.
        t.release(a.reservation_id)
        self.assertEqual(t.snapshot()["remainingUsd"], "0.00")


if __name__ == "__main__":
    unittest.main()
