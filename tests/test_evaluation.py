import unittest
from unittest.mock import patch

from evaluation.audit import audit_candidate
from evaluation.compare import compare, default_suite, generate, ignored_output, split_chronologically
from evaluation.replay import Order, Replay, Settings, validate_frames
from evaluation.runtime import DesiredOrder


def frame(t, mid=100, trades=None, depth=10):
    return {"timestamp": t, "books": {"A": {"bid": mid-.1, "ask": mid+.1,
            "tick_size": .1, "bid_volume": depth, "ask_volume": depth,
            "trades": trades or []}}}


def replay(settings=None):
    return Replay(default_suite()["baseline"], settings or Settings())


class AccountingAndExecutionTests(unittest.TestCase):
    def test_hand_calculated_short_directional_attribution(self):
        r = replay(Settings(fee_per_unit=.02))
        r.orders = [Order(1, "A", "ask", 100.2, 2, "limit", 0, 0)]
        r.decide = lambda snapshot, config: []
        m = r.run([frame(0, trades=[{"aggressor": "buy", "price": 100.3, "volume": 2}]), frame(1, mid=99)])
        self.assertEqual(m["terminal_inventory"], {"A": -2})
        self.assertAlmostEqual(m["spread_capture"], .4)
        self.assertAlmostEqual(m["directional_pnl"], 2)
        self.assertAlmostEqual(m["fees"], .04)
        self.assertAlmostEqual(m["pnl_after_costs"], 2.36)
        self.assertAlmostEqual(m["liquidation_estimate_after_costs"], 2.12)

    def test_drawdown_starts_at_zero_and_counts_adverse_costs(self):
        r = replay(Settings(fee_per_unit=1))
        r.orders = [Order(1, "A", "bid", 100, 1, "limit", 0, 0)]
        r.decide = lambda snapshot, config: []
        m = r.run([frame(0, trades=[{"aggressor": "sell", "price": 99.8, "volume": 1}]), frame(1, mid=99)])
        self.assertAlmostEqual(m["pnl_after_costs"], -2)
        self.assertAlmostEqual(m["max_drawdown"], 2)

    def test_queue_partial_fill_and_volume_is_not_reused(self):
        r = replay()
        r.orders = [Order(1, "A", "bid", 100, 5, "limit", 0, 3),
                    Order(2, "A", "bid", 100, 5, "limit", 0, 0)]
        r.market(frame(0, trades=[{"aggressor": "sell", "price": 99.8, "volume": 5}]), 0)
        self.assertEqual(r.positions["A"], 2)
        self.assertEqual([o.remaining for o in r.orders], [3, 5])

    def test_touch_does_not_fill_in_strict_model(self):
        for model, expected in (("through", 0), ("touch", 1), ("none", 0)):
            r = replay(Settings(passive_model=model))
            r.orders = [Order(1, "A", "bid", 100, 1, "limit", 0, 0)]
            r.market(frame(0, trades=[{"aggressor": "sell", "price": 100, "volume": 1}]), 0)
            self.assertEqual(r.positions["A"], expected)

    def test_ioc_partial_fill_and_no_depth_cancels_remainder(self):
        for depth, expected in ((2, 2), (0, 0)):
            r = replay()
            r.orders = [Order(1, "A", "bid", 101, 5, "ioc", 0, 0)]
            r.market(frame(0, depth=depth), 0)
            self.assertEqual(r.positions["A"], expected)
            self.assertEqual(r.orders, [])

    def test_latency_blocks_same_event_and_pending_order_is_reserved(self):
        r = replay(Settings(latency_steps=2))
        r.decide = lambda snapshot, config: [DesiredOrder("A", "bid", 100, 1)]
        r.market(frame(0), 0)
        r.reconcile(frame(0), 0)
        traded = frame(1, trades=[{"aggressor": "sell", "price": 99.8, "volume": 20}])
        r.market(traded, 1)
        self.assertEqual(r.positions["A"], 0)
        self.assertEqual(r.pending("A"), [("bid", 1)])
        r.market(traded, 2)
        self.assertEqual(r.positions["A"], 1)

    def test_unchanged_targets_keep_order_and_queue(self):
        r = replay()
        r.decide = lambda snapshot, config: [DesiredOrder("A", "bid", 99.8, 1)]
        for step in range(5):
            r.market(frame(step), step)
            r.reconcile(frame(step), step)
        self.assertEqual(r.counts["insert_attempts"], 1)
        self.assertEqual(r.counts["cancel_attempts"], 0)
        self.assertEqual(r.orders[0].queue, 10)

    def test_request_budget_counts_rejected_cancel(self):
        r = replay(Settings(max_requests_per_step=1))
        r.orders = [Order(1, "A", "bid", 99, 1, "limit", 0, 0),
                    Order(2, "A", "ask", 101, 1, "limit", 0, 0)]
        r.decide = lambda snapshot, config: []
        r.market(frame(0), 0)
        r.reconcile(frame(0), 0)
        self.assertEqual(r.counts["request_attempts"], 2)
        self.assertEqual(r.counts["accepted_requests"], 1)
        self.assertEqual(r.counts["rejected_requests"], 1)
        self.assertEqual(len(r.orders), 2)


class EvaluationProtocolTests(unittest.TestCase):
    def test_holdout_change_cannot_change_selection(self):
        data = generate(1, 60)
        suite = default_suite()
        first, _ = compare(data, suite)
        # Perturb only the sealed final partition; selected parameters stay fixed.
        for f in data["frames"][48:]:
            for book in f["books"].values():
                book["trades"] = []
                book["bid"] += 10
                book["ask"] += 10
        second, _ = compare(data, suite)
        self.assertEqual(first["selected"], second["selected"])

    def test_deterministic_common_inputs(self):
        data = generate(1729, 60)
        self.assertEqual(data, generate(1729, 60))
        self.assertEqual(compare(data, default_suite()), compare(data, default_suite()))
        parts = split_chronologically(data["frames"])
        self.assertLess(parts["train"][-1]["timestamp"], parts["validation"][0]["timestamp"])
        self.assertLess(parts["validation"][-1]["timestamp"], parts["holdout"][0]["timestamp"])

    def test_output_must_be_ignored(self):
        with self.assertRaises(ValueError):
            ignored_output("evaluation/should_not_write.json")
        self.assertTrue(ignored_output("results/test/ignored.json").is_absolute())

    def test_bad_timestamps_and_partial_universe_rejected(self):
        for frames in ([frame(1), frame(1)], [frame(2), frame(1)],
                       [frame(1), {"timestamp": 2, "books": {}}]):
            with self.assertRaises(ValueError):
                validate_frames(frames)

    def test_harness_rejects_caps_above_100(self):
        candidate = default_suite()["baseline"]
        candidate["config"]["position_limit"] = 101
        with self.assertRaises(ValueError):
            Replay(candidate, Settings())

    def test_raw_candidate_audit_is_not_masked_by_execution_gate(self):
        unsafe = lambda snapshot, config: [DesiredOrder(snapshot.instrument_id, "bid", 99.8, 201)]
        with patch("evaluation.audit.strategy", return_value=unsafe):
            with self.assertRaisesRegex(ValueError, "Unsafe raw proposal"):
                audit_candidate(default_suite()["baseline"])


if __name__ == "__main__":
    unittest.main()
