"""Turning integer minor units into something a human reads.

One definition, used by the bot and the dashboard both. Money formatting that
exists in two places drifts, and a ledger whose Telegram total disagrees with
its dashboard total is worse than useless.

Nothing here ever builds a float. Invariant 1: money is integer minor units plus
an ISO currency code, and that holds all the way to the last character printed.
"""

from __future__ import annotations

from typing import Final

#: Symbol by ISO 4217 code. A currency with no entry falls back to its code, so
#: an unfamiliar receipt reads "USD 12.30" rather than silently printing rupees.
SYMBOLS: Final[dict[str, str]] = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def rupees(minor: int) -> str:
    """Format integer paise for a human. Never builds a float."""
    return money(minor, "INR")


def average(total_minor: int, count: int) -> int:
    """The mean of `count` amounts totalling `total_minor`, in whole minor units.

    Integer the whole way. `round(total / count)` reaches the same answer for
    every amount this ledger will ever hold, and it gets there through a float,
    which invariant 1 does not allow for money even when the result only ever
    lands on a screen. Rounds half away from zero, so a mean of -50.5 paise
    reads as -51 and not as -50: refunds are their own negative rows per brief
    16.8, so negative means are reachable.
    """
    if count == 0:
        return 0
    sign = -1 if total_minor < 0 else 1
    magnitude = abs(total_minor)
    return sign * ((magnitude + count // 2) // count)


def money(minor: int, currency: str = "INR") -> str:
    """Format integer minor units in `currency`.

    The sign goes outside the symbol, so a refund reads `-₹56.00` rather than
    `₹-56.00`. Refunds are their own negative row per brief 16.8, so this is a
    real case and not a hypothetical one.
    """
    symbol = SYMBOLS.get(currency.upper())
    prefix = symbol if symbol else f"{currency.upper()} "
    sign = "-" if minor < 0 else ""
    whole, fraction = divmod(abs(minor), 100)
    return f"{sign}{prefix}{whole:,}.{fraction:02d}"


__all__ = ["SYMBOLS", "average", "money", "rupees"]
