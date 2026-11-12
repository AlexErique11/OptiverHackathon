"""Small helpers, not a complete exchange/portfolio risk engine."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite


def capacity(position: int, limit: int, buys: int = 0, sells: int = 0) -> tuple[int, int]:
    values = (position, limit, buys, sells)
    if any(type(value) is not int for value in values):
        raise ValueError("Positions and volumes must be integers")
    if limit <= 0 or buys < 0 or sells < 0:
        raise ValueError("Invalid limit or outstanding volume")
    # Each side can fill alone. Never net possible buys against possible sells.
    return max(0, limit - position - buys), max(0, limit + position - sells)


def round_price(price: float | Decimal, tick: float | Decimal, *, up: bool = False) -> float:
    if not isfinite(price) or not isfinite(tick) or tick <= 0:
        raise ValueError("Price and tick must be finite; tick must be positive")
    value, step = Decimal(str(price)), Decimal(str(tick))
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return float((value / step).to_integral_value(rounding=rounding) * step)
