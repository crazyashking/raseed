"""Quantity, unit and slug parsing. Brief 16.7.

Deterministic, so every case here is a fact about the parser rather than a
sample of a model's behaviour. The cases that matter are the ones where a wrong
answer still looks reasonable: `1kg` stored as `1`, `2 x 500ml` collapsed into
one litre, `1 lot` read as one litre.
"""

from __future__ import annotations

import pytest

from raseed.enrichment.normalize import MAX_SLUG_LENGTH, normalize, slugify


def test_the_briefs_worked_example() -> None:
    """Section 16.7, verbatim."""
    parsed = normalize("Amul Taaza Toned Milk 500ml")
    assert parsed.normalized_slug == "amul-taaza-toned-milk"
    assert parsed.quantity == 500
    assert parsed.unit == "ml"
    assert parsed.unit_normalized == "ml"


# ---------------------------------------------------------------------------
# Units, normalized down to the small one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "quantity", "unit", "normalized"),
    [
        ("Bhindi 500g", 500, "g", "g"),
        ("Toor Dal 1kg", 1000, "kg", "g"),
        ("Rice 5 kilo", 5000, "kg", "g"),
        ("Milk 1 L", 1000, "l", "ml"),
        ("Juice 250 ml", 250, "ml", "ml"),
        ("Ghee 1.5 L", 1500, "l", "ml"),
        ("Atta 10 KG", 10000, "kg", "g"),
        ("Eggs 6 pcs", 6, "pcs", "pcs"),
        ("Eggs 6 N", 6, "pcs", "pcs"),
        ("Eggs 1 dozen", 12, "dozen", "pcs"),
        ("Curd 400 gm", 400, "g", "g"),
        ("Oil 1 ltr", 1000, "l", "ml"),
    ],
)
def test_units(text: str, quantity: int, unit: str, normalized: str) -> None:
    parsed = normalize(text)
    assert parsed.quantity == quantity
    assert parsed.unit == unit
    assert parsed.unit_normalized == normalized


def test_a_kilogram_is_not_stored_as_one() -> None:
    """The case the brief's example does not disambiguate.

    `quantity 1` with `unit_normalized "g"` would mean one gram, and every
    price-per-unit comparison against it would be off by a thousand.
    """
    parsed = normalize("Toor Dal 1kg")
    assert parsed.quantity == 1000
    assert parsed.unit_normalized == "g"


def test_the_quantity_is_always_an_integer() -> None:
    """Same reasoning as invariant 1. A float quantity drifts."""
    parsed = normalize("Juice 0.33 L")
    assert parsed.quantity == 330
    assert isinstance(parsed.quantity, int)


def test_a_word_starting_with_a_unit_letter_is_not_a_unit() -> None:
    """`1 lot` is not one litre."""
    assert normalize("Item 1 lot").quantity is None


def test_a_decimal_does_not_also_match_its_own_fraction() -> None:
    """`1.5kg` must not additionally read as a bare `5kg`."""
    assert normalize("Ghee 1.5 kg").quantity == 1500


def test_a_name_with_no_quantity_still_gets_a_slug() -> None:
    parsed = normalize("Fresho Pyaz / Onion")
    assert parsed.normalized_slug == "fresho-pyaz-onion"
    assert parsed.quantity is None
    assert parsed.unit is None


def test_a_zero_quantity_is_not_recorded() -> None:
    assert normalize("Sample 0 g").quantity is None


# ---------------------------------------------------------------------------
# Packs, kept separate from quantity
# ---------------------------------------------------------------------------


def test_a_leading_multiplier() -> None:
    parsed = normalize("2 x 500ml Pepsi")
    assert parsed.pack_count == 2
    assert parsed.quantity == 500, "not 1000: a two-pack is not a one-litre bottle"
    assert parsed.normalized_slug == "pepsi", "the multiplier is not part of the name"


def test_a_trailing_multiplier() -> None:
    """What Blinkit actually prints in its quantity column."""
    parsed = normalize("Amul Taaza Toned Milk", quantity_text="500 ml x 2")
    assert parsed.pack_count == 2
    assert parsed.quantity == 500


def test_a_tight_multiplier_with_a_decimal() -> None:
    parsed = normalize("Coca Cola 2x1.5L")
    assert parsed.pack_count == 2
    assert parsed.quantity == 1500


def test_pack_of() -> None:
    parsed = normalize("Maggi Noodles Pack of 4")
    assert parsed.pack_count == 4
    assert parsed.normalized_slug == "maggi-noodles"


def test_no_pack_information_is_null_not_one() -> None:
    """ "This is a single item" and "the name did not say" are different facts."""
    assert normalize("Banana").pack_count is None


# ---------------------------------------------------------------------------
# Which text wins
# ---------------------------------------------------------------------------


def test_the_quantity_column_beats_the_title() -> None:
    parsed = normalize("Aalu / Potato 1kg", quantity_text="500 g x 1")
    assert parsed.quantity == 500


def test_the_title_is_used_when_there_is_no_quantity_column() -> None:
    assert normalize("Aalu / Potato 1kg", quantity_text=None).quantity == 1000


def test_the_slug_always_comes_from_the_printed_name() -> None:
    """The quantity column is a size, not a name."""
    parsed = normalize("Aalu / Potato 1kg", quantity_text="500 g")
    assert parsed.normalized_slug == "aalu-potato"


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Fresho Pyaz / Onion", "fresho-pyaz-onion"),
        ("Haldiram's Moong Dal", "haldirams-moong-dal"),
        ("Parle-G Gold Biscuit", "parle-g-gold-biscuit"),
        ("Act II Sour Cream & Cheese", "act-ii-sour-cream-cheese"),
        ("  spaced  out  ", "spaced-out"),
    ],
)
def test_slugify(text: str, slug: str) -> None:
    assert slugify(text) == slug


def test_an_apostrophe_does_not_strand_a_letter() -> None:
    """`haldiram-s` would put a one-letter word in the middle of the slug."""
    assert slugify("Haldiram's") == "haldirams"


def test_a_slug_of_nothing_is_null() -> None:
    assert slugify("!!!") is None
    assert normalize("500g").normalized_slug is None


def test_a_long_slug_is_cut_at_a_word_boundary() -> None:
    slug = slugify("word " * 100)
    assert slug is not None
    assert len(slug) <= MAX_SLUG_LENGTH
    assert not slug.endswith("-")
    assert slug.split("-")[-1] == "word"
