"""Print desired quotes for artificial snapshots. Never connects or sends orders."""
from dataclasses import replace

from optiver_bot.models import Snapshot, StrategyConfig
from optiver_bot.strategies.inventory_maker import decide


def main() -> None:
    config = StrategyConfig(position_limit=20)  # Demo value, not an event limit.
    snapshot = Snapshot("DEMO_STOCK", bid=99.9, ask=100.1, tick_size=0.1)
    print("OFFLINE DEMO: artificial prices/limits; no orders sent.\n")
    for position in (0, 10, -10, 20, -20):
        print(f"Position {position:+d}:")
        for order in decide(replace(snapshot, position=position), config):
            print(f"  {order.side:3s} {order.volume} @ {order.price:.2f} ({order.order_type})")


if __name__ == "__main__":
    main()
