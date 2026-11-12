"""Illustrative passive maker. Parameters are hypotheses, not calibrated values."""
from math import isfinite
from decimal import Decimal

from optiver_bot.models import DesiredOrder, Snapshot, StrategyConfig
from optiver_bot.risk import capacity, round_price


def decide(snapshot: Snapshot, config: StrategyConfig) -> list[DesiredOrder]:
    buy_room, sell_room = capacity(
        snapshot.position, config.position_limit,
        snapshot.outstanding_buy, snapshot.outstanding_sell,
    )
    if type(config.order_size) is not int or config.order_size <= 0:
        raise ValueError("Order size must be a positive integer")
    parameters = (config.half_spread_ticks, config.inventory_skew_ticks,
                  config.max_book_age_seconds)
    if not all(isfinite(value) for value in parameters):
        raise ValueError("Configuration values must be finite")
    if (config.half_spread_ticks < 1 or config.inventory_skew_ticks < 0
            or config.max_book_age_seconds <= 0):
        raise ValueError("Invalid spread, skew or maximum book age")
    prices = (snapshot.bid, snapshot.ask, snapshot.tick_size, snapshot.book_age_seconds)
    if not all(isfinite(value) for value in prices):
        return []
    if (not snapshot.instrument_id or snapshot.tick_size <= 0 or snapshot.bid <= 0
            or snapshot.ask <= snapshot.bid or snapshot.book_age_seconds < 0
            or snapshot.book_age_seconds > config.max_book_age_seconds):
        return []

    # Keep intermediate arithmetic decimal too: binary float noise at a tick
    # boundary could otherwise round an intended 100.1 ask up to 100.2.
    best_bid, best_ask, tick = map(Decimal, map(str, (
        snapshot.bid, snapshot.ask, snapshot.tick_size)))
    midpoint = (best_bid + best_ask) / 2
    center = midpoint - (Decimal(str(config.inventory_skew_ticks)) * tick
                         * snapshot.position / config.position_limit)
    half_spread = Decimal(str(config.half_spread_ticks)) * tick
    bid = round_price(min(center - half_spread, best_ask - tick), tick)
    ask = round_price(max(center + half_spread, best_bid + tick), tick, up=True)
    if bid >= ask:
        return []
    orders = []
    if buy_room and bid > 0:
        orders.append(DesiredOrder(snapshot.instrument_id, "bid", bid,
                                   min(config.order_size, buy_room)))
    if sell_room:
        orders.append(DesiredOrder(snapshot.instrument_id, "ask", ask,
                                   min(config.order_size, sell_room)))
    # [] means the desired order set is empty. The runner must cancel stale orders;
    # merely doing nothing would leave prior quotes active.
    return orders
