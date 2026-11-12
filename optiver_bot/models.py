"""Shared contract. Prices are floats at the boundary; rounding uses Decimal."""
from dataclasses import dataclass
from typing import Literal


# Confirmed event rule: this is a hard net-position limit on EACH instrument.
MAX_POSITION_PER_INSTRUMENT = 100


@dataclass(frozen=True)
class Snapshot:
    instrument_id: str
    bid: float
    ask: float
    tick_size: float
    position: int = 0
    outstanding_buy: int = 0
    outstanding_sell: int = 0
    book_age_seconds: float = 0.0


@dataclass(frozen=True)
class StrategyConfig:
    # A smaller internal cap is allowed; the exchange hard limit is always +/-100.
    position_limit: int = MAX_POSITION_PER_INSTRUMENT
    order_size: int = 1
    half_spread_ticks: float = 2.0
    inventory_skew_ticks: float = 2.0
    max_book_age_seconds: float = 2.0

    def __post_init__(self) -> None:
        if (type(self.position_limit) is not int
                or not 0 < self.position_limit <= MAX_POSITION_PER_INSTRUMENT):
            raise ValueError("Per-instrument position cap must be an integer from 1 to 100")


@dataclass(frozen=True)
class DesiredOrder:
    instrument_id: str
    side: Literal["bid", "ask"]
    price: float
    volume: int
    order_type: Literal["limit", "ioc"] = "limit"
