"""Pure position-target strategy. Inputs are supplied by the one execution owner."""
from dataclasses import dataclass
from math import isfinite, sqrt

from optiver_bot.models import DesiredOrder, Snapshot
from optiver_bot.risk import capacity, round_price


@dataclass(frozen=True)
class Config:
    cap: int = 10
    clip: int = 1
    target: int = 3
    warmup: int = 30
    fast: int = 5
    slow: int = 20
    min_changes: int = 10
    confirmation: int = 3
    strength: float = 1.5
    breadth: float = 0.625
    coverage: float = 0.75
    max_age: float = 1.0
    max_spread_ticks: float = 3.0
    max_hold: float = 60.0
    cooldown: float = 5.0
    news_pause: float = 20.0
    entry_ttl: float = 3.0
    stop_ticks: float = 5.0
    stop_sigma: float = 2.0
    round_trip_cost_ticks: float | None = None
    gross_limit: float | None = None
    position_cash_risk: float | None = None
    loss_limit: float | None = None
    no_entry_seconds: float = 90.0
    wind_down_seconds: float = 60.0
    max_active: int = 1

    def __post_init__(self):
        for name in ('cap', 'clip', 'target', 'warmup', 'fast', 'slow',
                     'min_changes', 'confirmation', 'max_active'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if not self.clip <= self.target <= self.cap <= 100:
            raise ValueError('Require clip <= target <= cap <= 100')
        if not self.fast < self.slow <= self.warmup <= 120 or self.min_changes > self.warmup:
            raise ValueError('Invalid history windows')
        for name in ('strength', 'breadth', 'coverage', 'max_age', 'max_spread_ticks',
                     'max_hold', 'cooldown', 'news_pause', 'entry_ttl', 'stop_ticks',
                     'stop_sigma', 'no_entry_seconds', 'wind_down_seconds'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError(f'Invalid {name}')
        if not 0 < self.breadth <= 1 or not 0 < self.coverage <= 1:
            raise ValueError('Invalid breadth/coverage')
        if self.wind_down_seconds > self.no_entry_seconds:
            raise ValueError('Wind down must follow entry cutoff')
        for name in ('round_trip_cost_ticks', 'gross_limit', 'position_cash_risk', 'loss_limit'):
            value = getattr(self, name)
            if value is not None and (type(value) not in (int, float)
                                      or not isfinite(value) or value < 0
                                      or (name != 'round_trip_cost_ticks' and value == 0)):
                raise ValueError(f'Invalid {name}')


@dataclass(frozen=True)
class Features:
    ready: bool = False
    fast_return: float = 0.0
    slow_return: float = 0.0
    sigma: float = 0.0
    basket: float = 0.0
    up_breadth: float = 0.0
    down_breadth: float = 0.0
    imbalance: float = 0.0


@dataclass(frozen=True)
class PositionSnapshot(Snapshot):
    target_position: int = 0
    account_ok: bool = False
    risk_exit: bool = False


@dataclass(frozen=True)
class DepthSnapshot(Snapshot):
    bid_volume: int = 0
    ask_volume: int = 0


def valid_book(s: Snapshot, cfg: Config) -> bool:
    values = (s.bid, s.ask, s.tick_size, s.book_age_seconds)
    return (all(type(v) in (int, float) and isfinite(v) for v in values)
            and 0 < s.bid < s.ask and s.tick_size > 0
            and 0 <= s.book_age_seconds <= cfg.max_age
            and all(abs(p / s.tick_size - round(p / s.tick_size)) < 1e-6
                    for p in (s.bid, s.ask)))


def direction(f: Features, s: Snapshot, cfg: Config) -> int:
    """Descriptive continuation gate, NOT a calibrated return forecast."""
    if not f.ready or cfg.round_trip_cost_ticks is None or not valid_book(s, cfg):
        return 0
    if not all(isfinite(x) for x in vars(f).values()):
        return 0
    if (s.ask - s.bid) / s.tick_size > cfg.max_spread_ticks + 1e-6:
        return 0
    mid = (s.bid + s.ask) / 2
    noise = max(f.sigma * sqrt(cfg.slow), s.tick_size / mid)
    cost = cfg.round_trip_cost_ticks * s.tick_size / mid
    for d, breadth in ((1, f.up_breadth), (-1, f.down_breadth)):
        if (d * f.fast_return > 0 and d * f.slow_return > 2 * cost
                and d * f.basket > 0 and breadth >= cfg.breadth
                and d * f.slow_return / noise >= cfg.strength
                and d * f.imbalance >= -0.25):
            return d
    return 0


def decide(snapshot: PositionSnapshot, config: Config) -> list[DesiredOrder]:
    """Targets represent TOTAL desired remaining orders, never extra inserts.

    Existing reservations are included when testing capacity, but the retained
    portion of a matching target is not counted as an additional proposed fill.
    The executor must cancel/reconcile unmatched orders before any replacement.
    """
    s, c = snapshot, config
    if not isinstance(s, PositionSnapshot) or not s.account_ok or not valid_book(s, c):
        return []
    capacity(s.position, c.cap, s.outstanding_buy, s.outstanding_sell)
    if type(s.target_position) is not int or abs(s.target_position) > c.cap:
        raise ValueError('Invalid position target')
    if s.position + s.outstanding_buy > 100 or s.position - s.outstanding_sell < -100:
        return []
    delta = s.target_position - s.position
    if not delta:
        return []
    side = 'bid' if delta > 0 else 'ask'
    # Never reverse an inherited position in one action.
    reducing = s.position * delta < 0
    volume = min(c.clip, abs(delta), abs(s.position) if reducing else c.cap)
    reserved = s.outstanding_buy if delta > 0 else s.outstanding_sell
    room = (c.cap - s.position if delta > 0 else c.cap + s.position)
    # Recovery of inventory outside the internal cap still allows reduction.
    if reducing:
        room = max(room, abs(s.position))
    volume = min(volume, max(0, room - reserved) + min(reserved, volume))
    if volume <= 0:
        return []
    price = s.bid if delta > 0 else s.ask
    if reducing and s.risk_exit:
        price = s.ask if delta > 0 else s.bid
    price = round_price(price, s.tick_size, up=side == 'ask')
    return [DesiredOrder(s.instrument_id, side, price, volume,
                         'ioc' if reducing and s.risk_exit else 'limit')]
