"""Independent contract audit, separate from the strategy owner's own tests.

Set EVALUATION_STRATEGY_ROOT to audit another complete optiver_bot checkout.
The oracle intentionally never calls shared risk code.
"""
from itertools import permutations, product
import unittest

from evaluation.risk_oracle import envelope, safe, validate_cap
from evaluation.runtime import DesiredOrder, Snapshot, StrategyConfig, capacity, strategy
from evaluation.replay import Order, Replay, Settings


class SharedRiskAudit(unittest.TestCase):
    def test_config_rejects_above_100_and_invalid_caps(self):
        for cap in (101, 1000, 0, -1, True, 100.0):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                StrategyConfig(position_limit=cap)

    def test_capacity_rejects_above_100(self):
        for cap in (101, 1000):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                capacity(0, cap)

    def test_observed_position_outside_hard_limit_rejected(self):
        for position in (-101, 101):
            with self.subTest(position=position), self.assertRaises(ValueError):
                capacity(position, 100)

    def test_short_selling_and_inclusive_boundaries(self):
        self.assertEqual(capacity(0, 100), (100, 100))
        self.assertEqual(capacity(100, 100), (0, 200))
        self.assertEqual(capacity(-100, 100), (200, 0))
        decide = strategy("optiver_bot.strategies.inventory_maker")
        for position, allowed in ((-100, "bid"), (100, "ask")):
            orders = decide(Snapshot("A", 99.9, 100.1, .1, position), StrategyConfig(position_limit=100))
            self.assertEqual([o.side for o in orders], [allowed])
        flat = decide(Snapshot("A", 99.9, 100.1, .1), StrategyConfig(position_limit=100))
        self.assertIn("ask", [o.side for o in flat], "A flat account may sell short")

    def test_all_positions_and_independent_outstanding_envelopes(self):
        decide = strategy("optiver_bot.strategies.inventory_maker")
        for position in range(-100, 101):
            for requested_buy, requested_sell in product((0, 1, 5, 100), repeat=2):
                buys, sells = min(requested_buy, 100-position), min(requested_sell, 100+position)
                room_buy, room_sell = capacity(position, 100, buys, sells)
                self.assertEqual((room_buy, room_sell), (100-position-buys, 100+position-sells))
                pending = [("bid", buys), ("ask", sells)]
                orders = decide(Snapshot("A", 99.9, 100.1, .1, position, buys, sells),
                                StrategyConfig(position_limit=100, order_size=10))
                self.assertTrue(safe(position, pending + [(o.side, o.volume) for o in orders]))

    def test_no_netting_across_sides_or_instruments(self):
        self.assertEqual(capacity(99, 100, buys=1, sells=100)[0], 0)
        self.assertEqual(capacity(-99, 100, buys=100, sells=1)[1], 0)
        # Each instrument is checked independently despite zero portfolio net.
        self.assertFalse(safe(99, [("bid", 2)]))
        self.assertFalse(safe(-99, [("ask", 2)]))
        self.assertEqual(capacity(99, 100, buys=1), (0, 199))
        self.assertEqual(capacity(-99, 100, sells=1), (199, 0))

    def test_partial_fill_preserves_reserved_capacity(self):
        for position, side in ((90, "bid"), (-90, "ask")):
            before = capacity(position, 100, 10 if side == "bid" else 0, 10 if side == "ask" else 0)
            for filled in range(11):
                sign = 1 if side == "bid" else -1
                after = capacity(position + sign * filled, 100,
                                 10-filled if side == "bid" else 0, 10-filled if side == "ask" else 0)
                self.assertEqual(after[0 if side == "bid" else 1], before[0 if side == "bid" else 1])


class IndependentRaceAudit(unittest.TestCase):
    def test_invalid_caps_rejected_by_independent_gate(self):
        for cap in (101, 0, True):
            with self.assertRaises(ValueError):
                validate_cap(cap)

    def test_all_partial_fills_and_fill_orders_remain_safe(self):
        orders = [("bid", 3), ("bid", 2), ("ask", 3), ("ask", 2)]
        for initial in (-95, 0, 95):
            for fills in product(range(4), range(3), range(4), range(3)):
                for ordering in permutations(range(4)):
                    position = initial
                    remaining = list(orders)
                    for index in ordering:
                        side, volume = remaining[index]
                        position += fills[index] * (1 if side == "bid" else -1)
                        remaining[index] = side, volume-fills[index]
                        self.assertTrue(safe(position, remaining))

    def test_cancel_request_is_not_cancel_confirmation(self):
        for initial, side, sign in ((99, "bid", 1), (-99, "ask", -1)):
            # Every ordering of a partial/full fill and cancel acknowledgment.
            for ordering in permutations(("cancel_request", "fill", "ack")):
                if ordering.index("cancel_request") > ordering.index("ack"):
                    continue
                position, remaining = initial, 1
                for event in ordering:
                    if event == "fill" and remaining:
                        position += sign
                        remaining = 0
                    elif event == "ack":
                        remaining = 0
                    if remaining:
                        self.assertFalse(safe(position, [(side, remaining), (side, 1)]))
                    # Recompute after acknowledgment/fill, never from old position.
                    room = capacity(position, 100, remaining if side == "bid" else 0,
                                    remaining if side == "ask" else 0)[0 if side == "bid" else 1]
                    self.assertTrue(safe(position, [(side, remaining), (side, min(room, 1))]))

    def test_replay_cancellation_fill_race_and_stale_replacement(self):
        candidate = {"module": "optiver_bot.strategies.inventory_maker", "config": {"position_limit": 100}}
        for initial, side, price, aggressor in ((99, "bid", 100, "sell"), (-99, "ask", 100, "buy")):
            replay = Replay(candidate, Settings())
            replay.positions["A"] = initial
            replay.mid["A"] = 100
            replay.orders = [Order(1, "A", side, price, 1, "limit", 0, 0, cancel_step=1)]
            frame = {"timestamp": 1, "books": {"A": {"bid": 99.9, "ask": 100.1, "tick_size": .1,
                     "bid_volume": 10, "ask_volume": 10, "trades": [
                         {"aggressor": aggressor, "price": 99.8 if side == "bid" else 100.2, "volume": 1}]}}}
            replay.market(frame, 1)
            self.assertEqual(replay.positions["A"], 100 if side == "bid" else -100)
            self.assertEqual(replay.orders, [])
            # Intentionally stale/malicious output must be stopped independently.
            replay.decide = lambda snapshot, config: [DesiredOrder("A", side, price, 1)]
            replay.reconcile(frame, 1)
            self.assertEqual(replay.counts["risk_refusals"], 1)
            self.assertEqual(replay.counts["insert_attempts"], 0)

    def test_pending_inserts_and_cancels_reserve_full_remainder(self):
        replay = Replay({"module": "optiver_bot.strategies.inventory_maker", "config": {"position_limit": 100}}, Settings())
        replay.mid["A"] = 100
        replay.positions["A"] = 98
        replay.orders = [Order(1, "A", "bid", 99, 1, "limit", 10, 0),
                         Order(2, "A", "bid", 99, 1, "limit", 0, 0, cancel_step=10),
                         Order(3, "A", "ask", 101, 100, "limit", 0, 0)]
        self.assertEqual(envelope(98, replay.pending("A")), (-2, 100))
        self.assertFalse(safe(98, replay.pending("A") + [("bid", 1)]))
        replay.check()

    def test_existing_unsafe_orders_fail_closed_without_portfolio_netting(self):
        replay = Replay({"module": "optiver_bot.strategies.inventory_maker", "config": {"position_limit": 100}}, Settings())
        replay.mid = {"A": 100, "B": 100}
        replay.positions.update({"A": 99, "B": -99})
        replay.orders = [Order(1, "A", "bid", 99, 2, "limit", 0, 0, cancel_step=10),
                         Order(2, "B", "ask", 101, 2, "limit", 0, 0, cancel_step=10)]
        with self.assertRaises(AssertionError):
            replay.check()


if __name__ == "__main__":
    unittest.main()
