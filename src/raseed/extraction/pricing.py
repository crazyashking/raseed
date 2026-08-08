"""What a call costs, in integer micro-dollars.

Money is never a float in this codebase, and an API bill is money. A receipt
costs roughly $0.0079, which rounds to zero in cents, so the unit here is the
micro-dollar: one millionth of a US dollar. A receipt is about 7,900 of them.

Rates below were checked live on **2026-08-08** against
`ai.google.dev/gemini-api/docs/pricing` during the section 21.3 preflight, and
are recorded in `docs/DECISIONS.md`. They are per one million tokens, not per
image, which is the misreading that makes Flash look expensive.

Note that the brief's section 4.2 is stale here: it names "Gemini 3 Flash" at
$0.50/$3, which belongs to a preview endpoint. The GA model is $1.50/$7.50.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

#: The date every rate below was verified. Rates move; this says how stale they
#: are. `tools/eval_extraction.py` warns when this is more than 90 days old.
RATES_CHECKED_ON: Final[dt.date] = dt.date(2026, 8, 8)

#: One US dollar, in micro-dollars.
MICROS_PER_USD: Final[int] = 1_000_000

_PER_MILLION: Final[int] = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelRate:
    """Input and output price for one model, in micro-dollars per 1M tokens."""

    model_id: str
    input_micros_per_million: int
    output_micros_per_million: int


#: Every model the provider interface knows how to price. A model that is not
#: here cannot be billed, and `cost_micros` refuses rather than guessing, because
#: a silently wrong cost defeats the daily cap in brief section 16.6.
RATES: Final[dict[str, ModelRate]] = {
    rate.model_id: rate
    for rate in (
        ModelRate("gemini-3.6-flash", 1_500_000, 7_500_000),
        ModelRate("gemini-3.5-flash", 1_500_000, 9_000_000),
        ModelRate("gemini-3.5-flash-lite", 300_000, 2_500_000),
        ModelRate("gemini-3.1-flash-lite", 250_000, 1_500_000),
        ModelRate("gemini-2.5-flash", 300_000, 2_500_000),
        ModelRate("gemini-2.5-flash-lite", 100_000, 400_000),
    )
}


class UnknownModelError(LookupError):
    """Raised when asked to price a model with no published rate on file."""


def _ceil_div(numerator: int, denominator: int) -> int:
    """Integer division that rounds up.

    Rounding up rather than truncating means the recorded cost is never lower
    than the real one, so the daily cap trips early rather than late.
    """
    return -(-numerator // denominator)


def cost_micros(model_id: str, *, input_tokens: int, output_tokens: int) -> int:
    """What one call cost, in micro-dollars.

    Args:
        model_id: Must be a key in `RATES`.
        input_tokens: Prompt plus image tokens.
        output_tokens: Everything generated, including thinking tokens, which
            are billed at the output rate.

    Raises:
        UnknownModelError: The model has no rate on file.
        ValueError: A token count is negative.
    """
    if input_tokens < 0 or output_tokens < 0:
        msg = f"token counts must be non-negative, got {input_tokens} and {output_tokens}"
        raise ValueError(msg)

    rate = RATES.get(model_id)
    if rate is None:
        known = ", ".join(sorted(RATES))
        msg = f"no published rate for {model_id!r}. Known models: {known}"
        raise UnknownModelError(msg)

    return _ceil_div(input_tokens * rate.input_micros_per_million, _PER_MILLION) + _ceil_div(
        output_tokens * rate.output_micros_per_million, _PER_MILLION
    )


def format_usd(micros: int) -> str:
    """Render micro-dollars for a human, without ever building a float total."""
    whole, fraction = divmod(abs(micros), MICROS_PER_USD)
    sign = "-" if micros < 0 else ""
    return f"{sign}${whole}.{fraction:06d}"


__all__ = [
    "MICROS_PER_USD",
    "RATES",
    "RATES_CHECKED_ON",
    "ModelRate",
    "UnknownModelError",
    "cost_micros",
    "format_usd",
]
