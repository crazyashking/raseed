"""Turning integer minor units into something a human reads.

One definition, used by the bot and the dashboard both. Money formatting that
exists in two places drifts, and a ledger whose Telegram total disagrees with
its dashboard total is worse than useless.

Nothing here ever builds a float. Invariant 1: money is integer minor units plus
an ISO currency code, and that holds all the way to the last character printed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

#: Symbol by ISO 4217 code. A currency with no entry falls back to its code, so
#: an unfamiliar receipt reads "USD 12.30" rather than silently printing rupees.
SYMBOLS: Final[dict[str, str]] = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}

#: Minor units per major unit, as the exponent, for the currencies that are not
#: the assumed two. ISO 4217 lists these explicitly. A yen has no subdivision at
#: all, so printing "¥1,200.00" invents two digits the currency does not have,
#: and a dinar has three, so printing two loses a real one.
EXPONENTS: Final[dict[str, int]] = {
    "BHD": 3,
    "CLP": 0,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "VND": 0,
}


def exponent(currency: str) -> int:
    """How many decimal places `currency` prints. Two unless ISO 4217 says otherwise."""
    return EXPONENTS.get(currency.upper(), 2)


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


def apportion(total_minor: int, weights: Sequence[int]) -> list[int]:
    """Split `total_minor` across `weights`, exactly, in integer minor units.

    Used to give every line item its share of what was actually paid, since
    delivery, taxes and order-level coupons belong to the order rather than to
    any one line. Without this a category tab shows the sum of printed line
    prices, which on a receipt with a coupon is more money than left the account.

    The parts always sum to `total_minor`. Largest remainder does that: floor
    each share, then hand the leftover units one at a time to whoever was cut by
    the most. Ties break on position, so the result is deterministic and a page
    reload cannot move a paisa between categories.

    Weights of zero are legitimate (a free item), and so is a total of zero.
    When every weight is zero there is no proportion to honour and the total is
    split evenly, which keeps the money on the page instead of losing it.

    Sign is carried separately because refunds are their own negative rows per
    brief 16.8, and floor division on a negative numerator rounds away from zero
    and would hand out more than the total.
    """
    count = len(weights)
    if count == 0:
        return []

    magnitudes = [abs(w) for w in weights]
    denominator = sum(magnitudes)
    numerator = abs(total_minor)

    if denominator == 0:
        base, leftover = divmod(numerator, count)
        parts = [base + (1 if i < leftover else 0) for i in range(count)]
    else:
        parts = [(numerator * m) // denominator for m in magnitudes]
        remainders = [(numerator * m) % denominator for m in magnitudes]
        leftover = numerator - sum(parts)
        # `-remainders[i]` sorts the biggest shortfall first; `i` keeps ties in
        # receipt order rather than in whatever order the set iterated.
        for index in sorted(range(count), key=lambda i: (-remainders[i], i))[:leftover]:
            parts[index] += 1

    return [-p for p in parts] if total_minor < 0 else parts


def money(minor: int, currency: str = "INR") -> str:
    """Format integer minor units in `currency`.

    The sign goes outside the symbol, so a refund reads `-₹56.00` rather than
    `₹-56.00`. Refunds are their own negative row per brief 16.8, so this is a
    real case and not a hypothetical one.
    """
    symbol = SYMBOLS.get(currency.upper())
    prefix = symbol if symbol else f"{currency.upper()} "
    sign = "-" if minor < 0 else ""
    places = exponent(currency)
    whole, fraction = divmod(abs(minor), 10**places)
    if places == 0:
        return f"{sign}{prefix}{whole:,}"
    return f"{sign}{prefix}{whole:,}.{fraction:0{places}d}"


def compact(minor: int, currency: str = "INR") -> str:
    """A chart-tick amount, short enough for six of them across a phone.

    Scaled the way the currency is spoken. Rupees go by lakh and crore, because
    a lakh is a lakh and calling it 0.1 million is a translation nobody asked
    for. Everything else goes by thousand and million.

    Integer arithmetic throughout, including the single decimal place, because
    invariant 1 does not stop applying at the point a number reaches a screen.
    """
    sign = "-" if minor < 0 else ""
    major = abs(minor) // (10 ** exponent(currency))

    if currency.upper() == "INR":
        scales = ((10_000_000, "Cr"), (100_000, "L"), (1_000, "k"))
    else:
        scales = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k"))

    for divisor, suffix in scales:
        if major >= divisor:
            whole, tenth = divmod((major * 10) // divisor, 10)
            body = f"{whole}" if tenth == 0 else f"{whole}.{tenth}"
            return f"{sign}{body}{suffix}"
    return f"{sign}{major:,}"


__all__ = [
    "EXPONENTS",
    "SYMBOLS",
    "apportion",
    "average",
    "compact",
    "exponent",
    "money",
]
