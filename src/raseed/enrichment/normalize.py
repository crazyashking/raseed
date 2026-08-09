"""Quantity, unit and slug parsing. Brief 16.7.

Deterministic, regex-driven, no model. `"Amul Taaza Toned Milk 500ml"` becomes a
slug, a number and a unit, which is the whole prerequisite for comparing prices
per unit and for the item canonicalization in brief 3.7.

Three calls the brief leaves open, made here and recorded:

**`quantity` is expressed in `unit_normalized`, not in `unit`.** The brief's
worked example (`500ml` to `quantity 500, unit ml, unit_normalized ml`) does not
disambiguate the case that matters, which is `1kg`. Storing `quantity 1` with
`unit_normalized "g"` would mean one gram. So `1kg` becomes `1000 g` and `1.5 L`
becomes `1500 ml`. Normalizing down to the small unit is also what keeps the
column an integer, which invariant 1's reasoning applies to just as well as it
does to money: a float quantity drifts and then price-per-unit comparisons drift
with it.

**`unit` keeps what was printed.** Lowercased, but `kg` stays `kg`. Losing it
would make the parse unverifiable against the receipt.

**`pack_count` is separate from `quantity`, not multiplied into it.** `2 x 500ml`
is `pack_count 2, quantity 500`, exactly as the brief writes it. Multiplying
would make a two-pack indistinguishable from a one-litre bottle, and those are
different products at different prices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Longest slug the column holds. Truncated at a word boundary rather than
#: mid-word, so a truncated slug is still readable.
MAX_SLUG_LENGTH: Final[int] = 200


@dataclass(frozen=True, slots=True)
class Unit:
    """One printed unit spelling, and what it means."""

    #: What goes in `unit`: the printed form, canonicalized for spelling only.
    printed: str
    #: What goes in `unit_normalized`: g, ml or pcs.
    normalized: str
    #: How many `normalized` units one of `printed` is worth.
    factor: int


#: Every unit spelling seen on Indian quick-commerce receipts, and a few more.
#: Longest keys first when matching, so `kgs` is not read as `kg` plus a stray
#: `s`, and `ltr` is not read as `l`.
UNITS: Final[dict[str, Unit]] = {
    "kilogram": Unit("kg", "g", 1000),
    "kilograms": Unit("kg", "g", 1000),
    "kilo": Unit("kg", "g", 1000),
    "kgs": Unit("kg", "g", 1000),
    "kg": Unit("kg", "g", 1000),
    "grams": Unit("g", "g", 1),
    "gram": Unit("g", "g", 1),
    "gms": Unit("g", "g", 1),
    "gm": Unit("g", "g", 1),
    "g": Unit("g", "g", 1),
    "millilitre": Unit("ml", "ml", 1),
    "milliliter": Unit("ml", "ml", 1),
    "mls": Unit("ml", "ml", 1),
    "ml": Unit("ml", "ml", 1),
    "litres": Unit("l", "ml", 1000),
    "liters": Unit("l", "ml", 1000),
    "litre": Unit("l", "ml", 1000),
    "liter": Unit("l", "ml", 1000),
    "ltr": Unit("l", "ml", 1000),
    "lt": Unit("l", "ml", 1000),
    "l": Unit("l", "ml", 1000),
    "dozen": Unit("dozen", "pcs", 12),
    "pieces": Unit("pcs", "pcs", 1),
    "piece": Unit("pcs", "pcs", 1),
    "pcs": Unit("pcs", "pcs", 1),
    "pc": Unit("pcs", "pcs", 1),
    #: Blinkit and Zepto both print a bare `N` for a count: "Eggs 6 N".
    "n": Unit("pcs", "pcs", 1),
    "units": Unit("pcs", "pcs", 1),
    "unit": Unit("pcs", "pcs", 1),
}

_UNIT_ALTERNATION: Final[str] = "|".join(sorted(UNITS, key=len, reverse=True))

#: A number, optional space, then a unit. The unit must end on a word boundary
#: so `1 lot` is not read as one litre.
#:
#: The lookbehind blocks a digit or a dot, not any word character. Blocking a
#: digit is what stops `1.5kg` also matching a bare `5kg` at the decimal. Letters
#: have to be allowed through, because `2x1.5L` puts an `x` immediately before
#: the amount and that is a real spelling on multipacks.
_AMOUNT = re.compile(
    rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({_UNIT_ALTERNATION})(?![a-z0-9])",
    re.IGNORECASE,
)

#: `2 x 500ml`, `2x500 ml`, `2 X 500ML`. The multiplier is what precedes the x.
#: The multiplication sign is deliberate alongside the letter x: receipts print
#: both, and ruff's ambiguous-character rule is about text a human types, not
#: about a character class that has to match what a merchant printed.
_PACK_MULTIPLIER = re.compile(r"(?<![\w.])(\d+)\s*[x×]\s*(?=\d)", re.IGNORECASE)  # noqa: RUF001

#: `500 ml x 2`, the trailing form. This is what Blinkit actually prints in its
#: quantity column, and it means two units of 500 ml rather than a 500 ml pack
#: of two. Anchored to the end so `2 x 500 ml x 3` cannot match twice.
_PACK_SUFFIX = re.compile(r"[x×]\s*(\d+)\s*$", re.IGNORECASE)  # noqa: RUF001

#: `Pack of 2`, `Combo of 3`, `Set of 4`.
_PACK_OF = re.compile(r"\b(?:pack|combo|set|box)\s+of\s+(\d+)\b", re.IGNORECASE)

_NOT_SLUG = re.compile(r"[^a-z0-9]+")

#: Apostrophes are deleted rather than turned into a separator, so `Haldiram's`
#: slugs to `haldirams` and not to `haldiram-s` with a stray letter of its own.
#: Three spellings, because a receipt prints whichever one its font shipped with.
_APOSTROPHE = re.compile(r"['’ʼ]")  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class Normalized:
    """What a printed product name resolves to. Every field may be null."""

    normalized_slug: str | None
    quantity: int | None
    unit: str | None
    unit_normalized: str | None
    pack_count: int | None


def slugify(text: str) -> str | None:
    """A stable, lowercase, hyphenated slug. Never a partial word at the end."""
    slug = _NOT_SLUG.sub("-", _APOSTROPHE.sub("", text.lower())).strip("-")
    if not slug:
        return None
    if len(slug) <= MAX_SLUG_LENGTH:
        return slug
    return slug[:MAX_SLUG_LENGTH].rsplit("-", 1)[0] or slug[:MAX_SLUG_LENGTH]


def _amount(text: str) -> tuple[int, str, str] | None:
    """The last amount-and-unit in a string, as (quantity, unit, normalized).

    The **last** one, because a product name that carries two is shaped like
    "Milk 500ml Pack of 2 1L" far less often than it is shaped like
    "2 x 500 ml", where the leading number is the pack count. `_PACK_MULTIPLIER`
    has already claimed that number when it applies.
    """
    matches = list(_AMOUNT.finditer(text))
    if not matches:
        return None

    raw, spelling = matches[-1].groups()
    unit = UNITS[spelling.lower()]

    # A printed 1.5 L is 1500 ml. Rounding rather than truncating, so 0.33 L is
    # 330 ml and not 329. Nothing here is money, so this is safe.
    quantity = round(float(raw) * unit.factor)
    if quantity <= 0:
        return None
    return quantity, unit.printed, unit.normalized


def _pack_count(text: str) -> int | None:
    """`2 x 500ml` or `Pack of 2`. Null when the name says nothing about packs.

    Null rather than 1, because "this is a single item" and "the name did not
    say" are different facts and only one of them is worth trusting later.
    """
    for pattern in (_PACK_MULTIPLIER, _PACK_SUFFIX, _PACK_OF):
        found = pattern.search(text)
        if found:
            return int(found.group(1))
    return None


def _strip_quantities(text: str) -> str:
    """Remove every amount, multiplier and pack phrase from a name.

    The multiplier goes first. `_PACK_MULTIPLIER` only matches when a digit
    follows the `x`, so removing `500ml` from `2 x 500ml` first would strand the
    `2 x` and slug it as `2-x-pepsi`.
    """
    without = _PACK_OF.sub(" ", text)
    without = _PACK_SUFFIX.sub(" ", without)
    without = _PACK_MULTIPLIER.sub(" ", without)
    return _AMOUNT.sub(" ", without)


def normalize(raw_name: str, *, quantity_text: str | None = None) -> Normalized:
    """Split a printed product name into a slug, a quantity and a unit.

    Args:
        raw_name: The name exactly as printed. Never modified, only read.
        quantity_text: The separate quantity column, when the receipt prints one.
            Preferred over the name, because a receipt that bothers to print a
            quantity column is more reliable than a size buried in a title.

    Returns:
        Every field independently nullable. A name with no quantity in it still
        gets a slug, and that slug is the useful half.
    """
    slug = slugify(_strip_quantities(raw_name))

    pack = _pack_count(quantity_text or "") or _pack_count(raw_name)

    amount = _amount(quantity_text) if quantity_text else None
    if amount is None:
        amount = _amount(raw_name)

    if amount is None:
        return Normalized(
            normalized_slug=slug,
            quantity=None,
            unit=None,
            unit_normalized=None,
            pack_count=pack,
        )

    quantity, unit, unit_normalized = amount
    return Normalized(
        normalized_slug=slug,
        quantity=quantity,
        unit=unit,
        unit_normalized=unit_normalized,
        pack_count=pack,
    )


__all__ = [
    "MAX_SLUG_LENGTH",
    "UNITS",
    "Normalized",
    "Unit",
    "normalize",
    "slugify",
]
