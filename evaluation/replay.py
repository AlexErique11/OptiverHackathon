"""Deterministic, deliberately limited top-of-book/tape replay; never connects."""
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from math import isfinite

from evaluation.risk_oracle import envelope, integer, safe, validate_cap
from evaluation.runtime import Snapshot, StrategyConfig, strategy


@dataclass(frozen=True)
class Settings:
    fee_per_unit: float = 0.01
    slippage_ticks: float = 0.0
    latency_steps: int = 1
    cancel_latency_steps: int = 1
    queue_fraction: float = 1.0
    passive_model: str = "through"
    max_requests_per_step: int = 20

    def __post_init__(self):
        for name in ("fee_per_unit", "slippage_ticks", "queue_fraction"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value) or value < 0:
                raise ValueError(f"Invalid {name}")
        for name in ("latency_steps", "cancel_latency_steps"):
            integer(getattr(self, name), name, 0)
        integer(self.max_requests_per_step, "max_requests_per_step", 1)
        if self.passive_model not in ("none", "through", "touch"):
            raise ValueError("passive_model must be none, through or touch")


@dataclass
class Order:
    id: int
    instrument: str
    side: str
    price: float
    remaining: int
    order_type: str
    active_step: int
    queue: float
    cancel_step: int | None = None


def validate_frames(frames):
    if len(frames) < 2:
        raise ValueError("At least two frames required")
    previous = -float("inf")
    universe = None
    for frame in frames:
        timestamp = frame["timestamp"]
        if type(timestamp) not in (int, float) or not isfinite(timestamp) or timestamp <= previous:
            raise ValueError("Timestamps must be finite, strictly increasing numeric seconds")
        previous = timestamp
        books = frame["books"]
        if not books or (universe is not None and set(books) != universe):
            raise ValueError("Every frame must contain the same complete instrument universe")
        universe = set(books)
        for instrument, book in books.items():
            if not isinstance(instrument, str) or not instrument:
                raise ValueError("Invalid instrument")
            for name in ("bid", "ask", "tick_size"):
                if type(book[name]) not in (int, float) or not isfinite(book[name]) or book[name] <= 0:
                    raise ValueError(f"Invalid {name}")
            if book["bid"] >= book["ask"]:
                raise ValueError("Locked/crossed book")
            for name in ("bid_volume", "ask_volume"):
                integer(book[name], name, 0)
            age = book.get("age_seconds", 0)
            if type(age) not in (int, float) or not isfinite(age) or age < 0:
                raise ValueError("Invalid book age")
            for trade in book.get("trades", []):
                if trade["aggressor"] not in ("buy", "sell"):
                    raise ValueError("Trade requires buy/sell aggressor")
                integer(trade["volume"], "trade volume", 1)
                if type(trade["price"]) not in (int, float) or not isfinite(trade["price"]) or trade["price"] <= 0:
                    raise ValueError("Invalid trade price")
    return frames


class Replay:
    def __init__(self, candidate, settings):
        validate_cap(candidate["config"]["position_limit"])
        self.config = StrategyConfig(**candidate["config"])
        self.decide = strategy(candidate["module"])
        self.settings = settings
        self.orders = []
        self.positions = Counter()
        self.mid = {}
        self.cash = self.fees = self.spread = self.directional = 0.0
        self.counts = Counter()
        self.reasons = Counter()
        self.trace = []
        self.peak_inventory = Counter()
        self.low_position = Counter()
        self.high_position = Counter()
        self.peak_reserved = Counter()
        self.peak_exposure = 0.0
        self.max_drawdown = self.high_water = 0.0
        self.next_id = 0
        self.step_requests = 0

    def pending(self, instrument):
        return [(o.side, o.remaining) for o in self.orders if o.instrument == instrument]

    def check(self):
        for instrument in self.mid:
            if not safe(self.positions[instrument], self.pending(instrument), self.config.position_limit):
                raise AssertionError(f"Unsafe reserved exposure: {instrument}")
            low, high = envelope(self.positions[instrument], self.pending(instrument))
            self.peak_reserved[instrument] = max(self.peak_reserved[instrument], abs(low), abs(high))

    def reject(self, reason, risk=False):
        self.counts["risk_refusals" if risk else "rejected_requests"] += 1
        self.reasons[reason] += 1

    def request(self, kind):
        self.counts["request_attempts"] += 1
        self.counts[kind + "_attempts"] += 1
        self.step_requests += 1
        if self.step_requests > self.settings.max_requests_per_step:
            self.reject("modeled_request_budget")
            return False
        self.counts["accepted_requests"] += 1
        self.counts[kind + "_accepted"] += 1
        return True

    def fill(self, order, volume, price, book, liquidity):
        sign = 1 if order.side == "bid" else -1
        # Slippage is an adverse accounting penalty, including on passive fills;
        # it is not permission for an exchange to violate the limit price.
        effective = price + sign * self.settings.slippage_ticks * book["tick_size"]
        self.cash -= sign * volume * effective
        cost = volume * self.settings.fee_per_unit
        self.cash -= cost
        self.fees += cost
        self.spread += sign * volume * (self.mid[order.instrument] - effective)
        self.positions[order.instrument] += sign * volume
        order.remaining -= volume
        self.counts["fills"] += 1
        self.counts["filled_units"] += volume
        self.counts[liquidity + "_units"] += volume
        instrument = order.instrument
        position = self.positions[instrument]
        self.peak_inventory[instrument] = max(self.peak_inventory[instrument], abs(position))
        self.low_position[instrument] = min(self.low_position[instrument], position)
        self.high_position[instrument] = max(self.high_position[instrument], position)
        self.peak_exposure = max(self.peak_exposure, sum(abs(q) * self.mid[i] for i, q in self.positions.items()))
        self.check()

    def market(self, frame, step):
        # Mark inventory carried into this event before filling at its new mark.
        for instrument, book in frame["books"].items():
            new_mid = (book["bid"] + book["ask"]) / 2
            self.directional += self.positions[instrument] * (new_mid - self.mid.get(instrument, new_mid))
            self.mid[instrument] = new_mid
        # Worst-case ordering: fills happen before cancellation acknowledgments.
        for instrument, book in frame["books"].items():
            active = [o for o in self.orders if o.instrument == instrument and o.active_step <= step]
            depths = {"bid": book["ask_volume"], "ask": book["bid_volume"]}
            for order in active:
                crossing = order.price >= book["ask"] if order.side == "bid" else order.price <= book["bid"]
                if crossing and depths[order.side]:
                    volume = min(order.remaining, depths[order.side])
                    self.fill(order, volume, book["ask"] if order.side == "bid" else book["bid"], book, "taker")
                    depths[order.side] -= volume
                # IOC remainders expire immediately, even with no displayed depth.
                if order.order_type == "ioc":
                    order.remaining = 0
            if self.settings.passive_model != "none":
                for trade in book.get("trades", []):
                    available = trade["volume"]
                    side = "bid" if trade["aggressor"] == "sell" else "ask"
                    eligible = sorted((o for o in active if o.side == side and o.remaining),
                                      key=lambda o: (-o.price if side == "bid" else o.price, o.id))
                    for order in eligible:
                        through = trade["price"] < order.price if side == "bid" else trade["price"] > order.price
                        if not through and not (self.settings.passive_model == "touch" and trade["price"] == order.price):
                            continue
                        queued = min(available, order.queue)
                        order.queue -= queued
                        available -= queued
                        volume = min(order.remaining, int(available))
                        if volume:
                            self.fill(order, volume, order.price, book, "maker")
                            available -= volume
                        if available <= 0:
                            break
        self.orders = [o for o in self.orders if o.remaining and
                       (o.cancel_step is None or o.cancel_step > step)]
        self.check()

    def reconcile(self, frame, step):
        self.step_requests = 0
        # Counts are modeled, with no actual API calls. Reads are separate from
        # the insert/cancel request budget whose event definition is unknown.
        self.counts["modeled_read_requests"] += 1 + 2 * len(frame["books"])
        for instrument, book in sorted(frame["books"].items()):
            pending = self.pending(instrument)
            snapshot = Snapshot(instrument, book["bid"], book["ask"], book["tick_size"],
                                self.positions[instrument],
                                sum(v for side, v in pending if side == "bid"),
                                sum(v for side, v in pending if side == "ask"),
                                book.get("age_seconds", 0))
            desired = self.decide(snapshot, self.config)
            def key(o):
                return o.side, o.price, getattr(o, "volume", getattr(o, "remaining", 0)), o.order_type
            kept = set()
            new = []
            for order in desired:
                valid = (order.instrument_id == instrument and order.side in ("bid", "ask")
                         and type(order.volume) is int and order.volume > 0
                         and type(order.price) in (int, float) and isfinite(order.price) and order.price > 0
                         and order.order_type in ("limit", "ioc")
                         and Decimal(str(order.price)) % Decimal(str(book["tick_size"])) == 0)
                if not valid:
                    self.reject("invalid_desired_order", risk=True)
                    continue
                matching = next((o for o in self.orders if o.instrument == instrument
                                 and o.cancel_step is None and o.id not in kept and key(o) == key(order)), None)
                if matching:
                    kept.add(matching.id)
                else:
                    new.append(order)
            for order in self.orders:
                if order.instrument == instrument and order.id not in kept and order.cancel_step is None:
                    if self.request("cancel"):
                        order.cancel_step = step + self.settings.cancel_latency_steps
            # A zero-delay cancel acknowledges after this frame's fills. Positive
            # delay retains the full remaining quantity through later fills.
            self.orders = [o for o in self.orders if o.cancel_step is None or o.cancel_step > step]
            for desired_order in new:
                if not safe(self.positions[instrument], self.pending(instrument) +
                            [(desired_order.side, desired_order.volume)], self.config.position_limit):
                    self.reject("independent_position_gate", risk=True)
                    continue
                if self.request("insert"):
                    self.next_id += 1
                    # Missing depth at a non-best price is treated conservatively
                    # as best-level depth, not as an empty queue.
                    queue = self.settings.queue_fraction * book["bid_volume" if desired_order.side == "bid" else "ask_volume"]
                    self.orders.append(Order(self.next_id, instrument, desired_order.side,
                                             desired_order.price, desired_order.volume, desired_order.order_type,
                                             step + max(1, self.settings.latency_steps), queue))
            self.check()

    def run(self, frames):
        validate_frames(frames)
        for step, frame in enumerate(frames):
            self.market(frame, step)
            # No fresh quotes on the last frame; keep existing reservations in
            # terminal diagnostics rather than pretending they were canceled.
            if step < len(frames) - 1:
                self.reconcile(frame, step)
            equity = self.cash + sum(q * self.mid[i] for i, q in self.positions.items())
            gross = sum(abs(q) * self.mid[i] for i, q in self.positions.items())
            self.peak_exposure = max(self.peak_exposure, gross)
            self.high_water = max(self.high_water, equity)
            self.max_drawdown = max(self.max_drawdown, self.high_water - equity)
            self.trace.append({"timestamp": frame["timestamp"], "pnl_after_costs": equity,
                               "positions": {i: self.positions[i] for i in self.mid}, "gross_exposure": gross,
                               "reserved": {i: envelope(self.positions[i], self.pending(i)) for i in self.mid},
                               "spread_capture": self.spread, "directional_pnl": self.directional,
                               "fees": self.fees, "counts": dict(self.counts)})
        pnl = self.trace[-1]["pnl_after_costs"]
        if abs(pnl - (self.spread + self.directional - self.fees)) > 1e-7:
            raise AssertionError("PnL attribution does not reconcile")
        close_cost = sum(abs(self.positions[i]) * ((book["ask"] - book["bid"]) / 2 +
                         self.settings.fee_per_unit + self.settings.slippage_ticks * book["tick_size"])
                         for i, book in frames[-1]["books"].items())
        return {"pnl_after_costs": pnl, "liquidation_estimate_after_costs": pnl - close_cost,
                "max_drawdown": self.max_drawdown, "spread_capture": self.spread,
                "directional_pnl": self.directional, "fees": self.fees,
                "peak_abs_inventory": {i: self.peak_inventory[i] for i in self.mid},
                "min_position": {i: self.low_position[i] for i in self.mid},
                "max_position": {i: self.high_position[i] for i in self.mid},
                "terminal_inventory": {i: self.positions[i] for i in self.mid},
                "peak_reserved_abs_position": {i: self.peak_reserved[i] for i in self.mid},
                "peak_gross_exposure": self.peak_exposure,
                "terminal_gross_exposure": self.trace[-1]["gross_exposure"],
                "counts": {name: self.counts[name] for name in (
                    "fills", "filled_units", "maker_units", "taker_units", "risk_refusals",
                    "request_attempts", "accepted_requests", "rejected_requests",
                    "insert_attempts", "insert_accepted", "cancel_attempts", "cancel_accepted",
                    "modeled_read_requests")}, "rejection_reasons": dict(self.reasons),
                "settings": asdict(self.settings)}
