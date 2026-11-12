"""Copy to a distinct strategy file and implement the common contract."""
from optiver_bot.models import DesiredOrder, Snapshot, StrategyConfig


def decide(snapshot: Snapshot, config: StrategyConfig) -> list[DesiredOrder]:
    # No connection, orders, filesystem access or secrets in this function.
    return []
