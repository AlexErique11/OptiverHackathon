"""Independent offline risk gate. Deliberately does not use shared capacity()."""
HARD_LIMIT = 100


def integer(value, name, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def validate_cap(cap):
    integer(cap, "cap", 1)
    if cap > HARD_LIMIT:
        raise ValueError("Position cap above 100 is forbidden")


def envelope(position, orders):
    """Orders: (side, remaining), including pending inserts and cancel-pending."""
    integer(position, "position")
    low = high = position
    for side, remaining in orders:
        integer(remaining, "remaining", 0)
        if side == "bid":
            high += remaining
        elif side == "ask":
            low -= remaining
        else:
            raise ValueError("Unknown side")
    return low, high


def safe(position, orders, cap=100):
    validate_cap(cap)
    low, high = envelope(position, orders)
    return -cap <= low <= high <= cap
