"""Strategy #2 (Razvan): persistent queue pressure. Passive orders; no connection.

DepthSnapshot is an OPTIONAL integration proposal, not a shared-model change.
Regular Snapshot inputs safely abstain. See docs/ALTERNATIVE_PRESSURE.md.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from math import isfinite

from optiver_bot.models import (
    MAX_POSITION_PER_INSTRUMENT, DesiredOrder, Snapshot, StrategyConfig,
)
from optiver_bot.risk import capacity, round_price


def _finite(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and isfinite(value))


@dataclass(frozen=True)
class DepthObservation:
    bid: float
    ask: float
    bid_volume: int
    ask_volume: int
    book_age_seconds: float


@dataclass(frozen=True)
class DepthSnapshot(Snapshot):
    bid_volume: int | None = None
    ask_volume: int | None = None
    previous: DepthObservation | None = None


@dataclass(frozen=True)
class PressureConfig(StrategyConfig):
    max_book_age_seconds: float = 0.25
    round_trip_cost_ticks: float | None = None
    min_edge_ticks: float = 0.25
    imbalance_threshold: float = 0.6
    max_target_fraction: float = 0.5
    max_spread_ticks: float = 3.0
    min_depth: int = 10
    min_confirmation_seconds: float = 0.05
    max_confirmation_seconds: float = 0.5

    def __post_init__(self) -> None:
        super().__post_init__()
        for value in (self.order_size, self.min_depth):
            if type(value) is not int or value <= 0:
                raise ValueError("Order size and minimum depth must be positive integers")
        for value in (self.max_book_age_seconds, self.min_edge_ticks,
                      self.imbalance_threshold, self.max_target_fraction,
                      self.max_spread_ticks, self.min_confirmation_seconds,
                      self.max_confirmation_seconds):
            if not _finite(value) or value <= 0:
                raise ValueError("Pressure parameters must be finite and positive")
        if (self.imbalance_threshold >= 1 or self.max_target_fraction > 1
                or self.max_spread_ticks < 1
                or self.min_confirmation_seconds > self.max_confirmation_seconds):
            raise ValueError("Invalid threshold, target fraction, spread or confirmation interval")
        if self.round_trip_cost_ticks is not None and (
                not _finite(self.round_trip_cost_ticks) or self.round_trip_cost_ticks < 0):
            raise ValueError("Explicit round-trip cost must be finite and nonnegative")


def _valid(book: DepthObservation, tick: Decimal, max_spread: float) -> bool:
    if not all(_finite(v) for v in (book.bid, book.ask, book.book_age_seconds)):
        return False
    if (book.bid <= 0 or book.ask <= book.bid or book.book_age_seconds < 0
            or any(type(v) is not int or v <= 0 for v in
                   (book.bid_volume, book.ask_volume))):
        return False
    bid, ask = Decimal(str(book.bid)), Decimal(str(book.ask))
    # Reject inconsistent prices rather than infer an instrument's tick grid.
    return (bid % tick == 0 and ask % tick == 0
            and ask - bid <= Decimal(str(max_spread)) * tick)


def _imbalance(book: DepthObservation) -> Decimal:
    return Decimal(book.bid_volume - book.ask_volume) / (book.bid_volume + book.ask_volume)


def decide(snapshot: Snapshot, config: PressureConfig) -> list[DesiredOrder]:
    """At most one passive quote towards a signal-dependent inventory target."""
    if not isinstance(config, PressureConfig):
        raise TypeError("Use PressureConfig")
    buy_room, sell_room = capacity(snapshot.position, config.position_limit,
                                   snapshot.outstanding_buy, snapshot.outstanding_sell)
    if (snapshot.position + snapshot.outstanding_buy > MAX_POSITION_PER_INSTRUMENT
            or snapshot.position - snapshot.outstanding_sell < -MAX_POSITION_PER_INSTRUMENT):
        return []
    if (config.round_trip_cost_ticks is None or not isinstance(snapshot, DepthSnapshot)
            or not isinstance(snapshot.previous, DepthObservation)
            or not isinstance(snapshot.instrument_id, str) or not snapshot.instrument_id.strip()
            or not _finite(snapshot.tick_size) or snapshot.tick_size <= 0):
        return []
    tick = Decimal(str(snapshot.tick_size))
    current = DepthObservation(snapshot.bid, snapshot.ask, snapshot.bid_volume,
                               snapshot.ask_volume, snapshot.book_age_seconds)
    previous = snapshot.previous
    if not all(_valid(book, tick, config.max_spread_ticks) for book in (current, previous)):
        return []
    if current.book_age_seconds > config.max_book_age_seconds:
        return []
    interval = Decimal(str(previous.book_age_seconds)) - Decimal(str(current.book_age_seconds))
    if not (Decimal(str(config.min_confirmation_seconds)) <= interval
            <= Decimal(str(config.max_confirmation_seconds))):
        return []
    if min(current.bid_volume + current.ask_volume,
           previous.bid_volume + previous.ask_volume) < config.min_depth:
        return []
    now, before = _imbalance(current), _imbalance(previous)
    threshold = Decimal(str(config.imbalance_threshold))
    if now * before <= 0 or min(abs(now), abs(before)) < threshold:
        return []
    direction = 1 if now > 0 else -1
    bid, ask = Decimal(str(current.bid)), Decimal(str(current.ask))
    old_mid = (Decimal(str(previous.bid)) + Decimal(str(previous.ask))) / 2
    mid = (bid + ask) / 2
    # Do not buy confirmed pressure into an observed falling midpoint, or vice versa.
    if direction * (mid - old_mid) < 0:
        return []
    # Weighted-mid proxy; NOT a calibrated forecast or true fair value.
    microprice = (ask * current.bid_volume + bid * current.ask_volume) / (
        current.bid_volume + current.ask_volume)
    edge = (Decimal(str(config.round_trip_cost_ticks))
            + Decimal(str(config.min_edge_ticks))) * tick
    strength = min(abs(now), abs(before))
    target_size = int((Decimal(config.position_limit) * Decimal(str(config.max_target_fraction))
                       * strength).to_integral_value(rounding=ROUND_FLOOR))
    target = direction * target_size
    if direction > 0:
        # Never count pending sells as available buying capacity.
        volume = min(config.order_size, buy_room,
                     max(0, target - snapshot.position - snapshot.outstanding_buy))
        price = round_price(min(bid + tick, ask - tick, microprice - edge), tick)
        if volume <= 0 or not bid <= Decimal(str(price)) < ask:
            return []
        return [DesiredOrder(snapshot.instrument_id, "bid", price, volume)]
    volume = min(config.order_size, sell_room,
                 max(0, snapshot.position - snapshot.outstanding_sell - target))
    price = round_price(max(ask - tick, bid + tick, microprice + edge), tick, up=True)
    if volume <= 0 or not bid < Decimal(str(price)) <= ask:
        return []
    return [DesiredOrder(snapshot.instrument_id, "ask", price, volume)]
