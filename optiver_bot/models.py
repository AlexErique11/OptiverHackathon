"""Shared contract. Prices are floats at the boundary; rounding uses Decimal."""
from dataclasses import dataclass
from typing import Literal


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
    # Explicitly supplied by caller. No default exchange limit is assumed.
    position_limit: int
    order_size: int = 1
    half_spread_ticks: float = 2.0
    inventory_skew_ticks: float = 2.0
    max_book_age_seconds: float = 2.0


@dataclass(frozen=True)
class DesiredOrder:
    instrument_id: str
    side: Literal["bid", "ask"]
    price: float
    volume: int
    order_type: Literal["limit", "ioc"] = "limit"
