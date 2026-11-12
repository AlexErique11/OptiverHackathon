import unittest
from dataclasses import replace
from decimal import Decimal

from optiver_bot.models import Snapshot, StrategyConfig
from optiver_bot.risk import capacity, round_price
from optiver_bot.strategies.inventory_maker import decide


class RiskTests(unittest.TestCase):
    def test_reserve_both_sides_independently(self):
        self.assertEqual(capacity(18, 20, buys=1, sells=3), (1, 35))
        self.assertEqual(capacity(-20, 20), (40, 0))

    def test_reject_invalid_limits_and_volumes(self):
        for args in ((0, 0), (0, 20, -1), (0.5, 20), (0, True)):
            with self.assertRaises(ValueError):
                capacity(*args)

    def test_tick_rounding(self):
        self.assertEqual(round_price(100.07, 0.05), 100.05)
        self.assertEqual(round_price(100.07, 0.05, up=True), 100.1)
        self.assertEqual(round_price(100.1, 0.1), 100.1)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot("DEMO", 99.9, 100.1, 0.1)
        self.config = StrategyConfig(position_limit=20)

    def test_inventory_moves_quotes_in_correct_direction(self):
        def prices(position):
            return {order.side: order.price for order in decide(
                replace(self.snapshot, position=position), self.config)}
        flat, long, short = prices(0), prices(10), prices(-10)
        self.assertLess(long["ask"], flat["ask"])
        self.assertLess(long["bid"], flat["bid"])
        self.assertGreater(short["bid"], flat["bid"])

    def test_limit_disables_risk_increasing_side(self):
        for position, allowed in ((20, "ask"), (-20, "bid")):
            orders = decide(replace(self.snapshot, position=position), self.config)
            self.assertEqual([order.side for order in orders], [allowed])

    def test_outstanding_orders_exhaust_capacity(self):
        orders = decide(replace(self.snapshot, outstanding_buy=20,
                                outstanding_sell=20), self.config)
        self.assertEqual(orders, [])

    def test_bad_or_stale_data_produces_no_desired_orders(self):
        for changes in ({"bid": 101}, {"bid": float("nan")},
                        {"book_age_seconds": 3}, {"tick_size": 0}):
            self.assertEqual(decide(replace(self.snapshot, **changes), self.config), [])

    def test_all_inventory_levels_fit_capacity_and_tick_grid(self):
        for position in range(-20, 21):
            snapshot = replace(self.snapshot, position=position,
                               outstanding_buy=2, outstanding_sell=3)
            rooms = dict(zip(("bid", "ask"), capacity(position, 20, 2, 3)))
            for order in decide(snapshot, replace(self.config, order_size=5)):
                self.assertGreater(order.volume, 0)
                self.assertLessEqual(order.volume, rooms[order.side])
                self.assertEqual(Decimal(str(order.price)) % Decimal("0.1"), 0)
                if order.side == "bid":
                    self.assertLess(order.price, snapshot.ask)
                else:
                    self.assertGreater(order.price, snapshot.bid)


if __name__ == "__main__":
    unittest.main()
