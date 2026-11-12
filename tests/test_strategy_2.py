import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import runpy

from optiver_bot.models import Snapshot
strategy = runpy.run_path(str(Path(__file__).resolve().parents[1]
                             / "optiver_bot/strategies/market_maker #2.py"))
DepthObservation = strategy["DepthObservation"]
DepthSnapshot = strategy["DepthSnapshot"]
PressureConfig = strategy["PressureConfig"]
decide = strategy["decide"]


def book(name="TEST", bid=100.0, ask=100.3, bid_volume=900, ask_volume=100):
    return DepthSnapshot(name, bid, ask, 0.1, bid_volume=bid_volume,
                         ask_volume=ask_volume,
                         previous=DepthObservation(bid, ask, bid_volume, ask_volume, 0.1))


class PressureTests(unittest.TestCase):
    def setUp(self):
        self.config = PressureConfig(order_size=5, round_trip_cost_ticks=0)

    def test_user_books_are_conditional_signals_not_profit_predictions(self):
        # User-supplied display: assumed bid qty, bid, ask, ask qty. Tick assumed 0.1.
        examples = (
            ("AMD", 161.2, 161.5, 3010, 10, "bid", 161.3),
            ("AVGO", 207.0, 207.2, 12, 7, None, None),
            ("CRWV", 136.5, 136.8, 26, 11, None, None),
            ("MU", 71.7, 72.0, 2990, 3000, None, None),
            ("NBIS", 93.0, 93.2, 1010, 6, "bid", 93.1),
            ("NVDA", 139.6, 139.8, 24, 5, "bid", 139.7),
            ("SNDK", 59.6, 59.8, 2, 61, "ask", 59.7),
        )
        for name, bid, ask, bq, aq, side, price in examples:
            with self.subTest(instrument=name):
                orders = decide(book(name, bid, ask, bq, aq), self.config)
                self.assertEqual([(o.side, o.price) for o in orders],
                                 [] if side is None else [(side, price)])

    def test_no_depth_previous_or_explicit_cost_means_no_orders(self):
        self.assertEqual(decide(Snapshot("X", 100, 100.3, 0.1), self.config), [])
        self.assertEqual(decide(replace(book(), previous=None), self.config), [])
        self.assertEqual(decide(book(), PressureConfig()), [])

    def test_pressure_must_persist_and_not_reverse(self):
        for bq, aq in ((500, 500), (100, 900)):
            previous = DepthObservation(100, 100.3, bq, aq, 0.1)
            self.assertEqual(decide(replace(book(), previous=previous), self.config), [])

    def test_avoid_pressure_against_observed_price_movement(self):
        bullish_falling = replace(book(), previous=DepthObservation(100.1, 100.4, 900, 100, 0.1))
        bearish_rising = replace(book(bid_volume=100, ask_volume=900),
                                 previous=DepthObservation(99.9, 100.2, 100, 900, 0.1))
        self.assertEqual(decide(bullish_falling, self.config), [])
        self.assertEqual(decide(bearish_rising, self.config), [])

    def test_freshness_and_distinct_observation_interval(self):
        for age in (0, 0.01, 0.501, -1, float("nan")):
            snap = replace(book(), previous=replace(book().previous, book_age_seconds=age))
            self.assertEqual(decide(snap, self.config), [])
        self.assertEqual(decide(replace(book(), book_age_seconds=0.251), self.config), [])

    def test_costs_can_remove_the_edge_without_crossing_the_spread(self):
        snap = book(ask=100.1)
        orders = decide(snap, self.config)
        self.assertEqual([(o.side, o.price) for o in orders], [("bid", 100)])
        self.assertEqual(decide(snap, replace(self.config, round_trip_cost_ticks=1)), [])

    def test_target_reserves_pending_orders_and_partial_fills(self):
        # I=0.8, cap=100, target fraction=0.5 -> target inventory=40.
        snap = replace(book(), position=35, outstanding_buy=3)
        self.assertEqual(decide(snap, self.config)[0].volume, 2)
        after_fill = replace(snap, position=37, outstanding_buy=1)
        self.assertEqual(decide(after_fill, self.config)[0].volume, 2)
        self.assertEqual(decide(replace(snap, outstanding_buy=5), self.config), [])
        # Opposite outstanding volume never creates buying room.
        self.assertEqual(decide(replace(snap, outstanding_sell=100), self.config)[0].volume, 2)

    def test_short_from_zero_and_both_hard_boundaries(self):
        bearish = book(bid_volume=100, ask_volume=900)
        self.assertEqual(decide(bearish, self.config)[0].side, "ask")
        self.assertEqual(decide(replace(book(), position=100), self.config), [])
        self.assertEqual(decide(replace(bearish, position=-100), self.config), [])
        self.assertEqual(decide(replace(bearish, position=100), self.config)[0].side, "ask")
        self.assertEqual(decide(replace(book(), position=-100), self.config)[0].side, "bid")

    def test_all_positions_and_independent_outstanding_limits(self):
        for cap in (20, 100):
            for pos in range(-100, 101):
                for buys, sells in ((0, 0), (3, 9), (99, 99)):
                    for base in (book(), book(bid_volume=100, ask_volume=900)):
                        snap = replace(base, position=pos, outstanding_buy=buys,
                                       outstanding_sell=sells)
                        orders = decide(snap, replace(self.config, position_limit=cap))
                        self.assertLessEqual(len(orders), 1)
                        for order in orders:
                            self.assertGreater(order.volume, 0)
                            self.assertLessEqual(order.volume, self.config.order_size)
                            self.assertEqual(Decimal(str(order.price)) % Decimal("0.1"), 0)
                            if order.side == "bid":
                                self.assertLessEqual(pos + buys + order.volume, cap)
                                self.assertLess(order.price, snap.ask)
                            else:
                                self.assertGreaterEqual(pos - sells - order.volume, -cap)
                                self.assertGreater(order.price, snap.bid)

    def test_invalid_books_thin_books_and_wide_spreads_abstain(self):
        for snap in (book(ask=100.4), book(bid_volume=0), book(bid_volume=-1),
                     book(bid_volume=1.5), book(bid_volume=4, ask_volume=1),
                     book(bid=100.01), book(bid=float("nan")),
                     replace(book(), tick_size=0), replace(book(), instrument_id="")):
            self.assertEqual(decide(snap, self.config), [])

    def test_invalid_configuration(self):
        for changes in ({"position_limit": 101}, {"order_size": 0}, {"min_depth": True},
                        {"imbalance_threshold": 1}, {"imbalance_threshold": float("nan")},
                        {"max_target_fraction": 1.1}, {"round_trip_cost_ticks": -1},
                        {"max_confirmation_seconds": 0.01}, {"max_spread_ticks": 0.5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.config, **changes)


if __name__ == "__main__":
    unittest.main()
