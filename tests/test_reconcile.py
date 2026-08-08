"""Tests for the reconciliation gate and the MRP cross-check.

Everything here runs against `tests/fixtures/`. `data/` is gitignored, so the
suite must never reach for it. The one-off sweep across all 32 eval records lives
in `tools/sweep_eval.py` instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import as_extraction_payload
from raseed.extraction.schemas import ExtractionResult
from raseed.validation.reconcile import (
    DEFAULT_TOLERANCE_MINOR,
    MrpOutcome,
    Outcome,
    computed_total,
    cross_check_mrp,
    reconcile,
)


def build(name: str, **overrides: Any) -> ExtractionResult:
    return ExtractionResult.model_validate(as_extraction_payload(name, **overrides))


# ---------------------------------------------------------------------------
# The tolerance itself
# ---------------------------------------------------------------------------


def test_default_tolerance_is_one_rupee() -> None:
    """Brief 18.4. GST rounding makes anything tighter reject correct receipts."""
    assert DEFAULT_TOLERANCE_MINOR == 100


def test_negative_tolerance_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        reconcile(build("blinkit_001"), tolerance_minor=-1)


# ---------------------------------------------------------------------------
# The committed fixtures
# ---------------------------------------------------------------------------


def test_every_fixture_is_accepted(fixture_name: str) -> None:
    assert reconcile(build(fixture_name)).accepted


@pytest.mark.parametrize(
    ("name", "expected_total", "expected_outcome"),
    [
        ("blinkit_001", 21900, Outcome.BALANCED),
        ("blinkit_026", 33900, Outcome.BALANCED),
        ("blinkit_032", 1500, Outcome.SKIPPED),
    ],
)
def test_fixture_arithmetic(name: str, expected_total: int, expected_outcome: Outcome) -> None:
    result = build(name)
    assert computed_total(result) == expected_total
    verdict = reconcile(result)
    assert verdict.outcome is expected_outcome
    assert verdict.stated_total_minor == expected_total
    assert verdict.delta_minor == 0


def test_an_order_level_discount_is_subtracted() -> None:
    """026 only balances because the FLAT100 coupon comes off the total."""
    result = build("blinkit_026")
    without_discount = sum(item.line_total_minor for item in result.line_items) + sum(
        charge.amount_minor for charge in result.charges
    )
    assert without_discount == 43900
    assert computed_total(result) == 33900
    assert reconcile(result).outcome is Outcome.BALANCED


# ---------------------------------------------------------------------------
# Tolerance boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("drift", [0, 1, -1, 99, -99, 100, -100])
def test_drift_within_tolerance_still_balances(drift: int) -> None:
    result = build("blinkit_001", grand_total_minor=21900 + drift)
    verdict = reconcile(result)
    assert verdict.outcome is Outcome.BALANCED
    assert verdict.delta_minor == drift


@pytest.mark.parametrize("drift", [101, -101, 5000])
def test_drift_beyond_tolerance_is_class_2(drift: int) -> None:
    verdict = reconcile(build("blinkit_001", grand_total_minor=21900 + drift))
    assert verdict.outcome is Outcome.CLASS_2
    assert verdict.delta_minor == drift
    assert not verdict.accepted
    assert verdict.reason


def test_tolerance_is_configurable() -> None:
    result = build("blinkit_001", grand_total_minor=22400)
    assert reconcile(result).outcome is Outcome.CLASS_2
    assert reconcile(result, tolerance_minor=500).outcome is Outcome.BALANCED


# ---------------------------------------------------------------------------
# Class 1: extraction failed, store nothing (brief 3.3)
# ---------------------------------------------------------------------------


def test_a_non_receipt_is_class_1() -> None:
    result = ExtractionResult.model_validate(
        {
            "is_receipt": False,
            "receipt_confidence": 0.02,
            "rejection_reason": "A screenshot of a chat conversation.",
        }
    )
    verdict = reconcile(result)
    assert verdict.outcome is Outcome.CLASS_1
    assert not verdict.accepted
    assert verdict.reason == "A screenshot of a chat conversation."


def test_a_missing_grand_total_is_class_1() -> None:
    verdict = reconcile(build("blinkit_001", grand_total_minor=None))
    assert verdict.outcome is Outcome.CLASS_1
    assert verdict.delta_minor is None
    assert "grand total" in verdict.reason


def test_a_total_with_nothing_else_read_is_class_1() -> None:
    """A number and no items at all is a failed read, not a utility bill."""
    verdict = reconcile(
        build(
            "blinkit_001",
            line_items=[],
            charges=[],
            taxes=[],
            discounts=[],
            grand_total_minor=21900,
        )
    )
    assert verdict.outcome is Outcome.CLASS_1


def test_low_confidence_is_class_1_when_a_floor_is_supplied() -> None:
    result = build("blinkit_001", receipt_confidence=0.3)
    assert reconcile(result, min_confidence=0.5).outcome is Outcome.CLASS_1


def test_confidence_is_not_checked_by_default() -> None:
    """The brief names low confidence as Class 1 but never fixes a number."""
    assert reconcile(build("blinkit_001", receipt_confidence=0.01)).outcome is Outcome.BALANCED


# ---------------------------------------------------------------------------
# skip_reconciliation (brief 3.8)
# ---------------------------------------------------------------------------


def test_zero_line_items_skips_rather_than_fails() -> None:
    """A utility bill has no itemisation. Failing it would reject every bill forever."""
    verdict = reconcile(build("blinkit_032"))
    assert verdict.outcome is Outcome.SKIPPED
    assert verdict.accepted


def test_skip_holds_even_when_the_arithmetic_would_not() -> None:
    """There is nothing to reconcile against, so a mismatch is not evidence of a misread."""
    verdict = reconcile(build("blinkit_032", grand_total_minor=999999))
    assert verdict.outcome is Outcome.SKIPPED
    assert verdict.accepted
    assert verdict.delta_minor == 999999 - 1500


def test_skipped_receipts_carry_no_adjustment() -> None:
    assert reconcile(build("blinkit_032")).unaccounted_adjustment_minor is None


# ---------------------------------------------------------------------------
# Class 2 and unaccounted_adjustment
# ---------------------------------------------------------------------------


def test_the_adjustment_makes_a_class_2_receipt_balance() -> None:
    """Brief 3.3: log the gap so the ledger stays arithmetically honest."""
    verdict = reconcile(build("blinkit_001", grand_total_minor=22400))
    assert verdict.outcome is Outcome.CLASS_2
    adjustment = verdict.unaccounted_adjustment_minor
    assert adjustment == 500
    assert verdict.computed_total_minor + adjustment == verdict.stated_total_minor


def test_only_class_2_produces_an_adjustment() -> None:
    for name in ("blinkit_001", "blinkit_026", "blinkit_032"):
        assert reconcile(build(name)).unaccounted_adjustment_minor is None


def test_a_negative_gap_is_recorded_signed() -> None:
    verdict = reconcile(build("blinkit_001", grand_total_minor=21000))
    assert verdict.unaccounted_adjustment_minor == -900


# ---------------------------------------------------------------------------
# Invariant 13: a product discount is never repeated at order level
# ---------------------------------------------------------------------------


def test_double_counting_a_product_discount_breaks_the_gate() -> None:
    """This is the bug the gate exists to catch, made concrete.

    026's line prices are already net of the product discount. Repeating that
    5600 paise saving in `discounts[]` subtracts it twice, and the arithmetic
    stops working. Nothing about the line items would look wrong.
    """
    result = build("blinkit_026")
    doubled = build(
        "blinkit_026",
        discounts=[
            *(d.model_dump() for d in result.discounts),
            {"label": "Product discount", "amount_minor": 5600},
        ],
    )
    verdict = reconcile(doubled)
    assert verdict.outcome is Outcome.CLASS_2
    assert verdict.delta_minor == 5600


def test_the_printed_savings_line_is_not_a_reconciliation_term() -> None:
    """`printed_product_discount_minor` is a cross-check input only."""
    with_savings = build("blinkit_026", printed_product_discount_minor=5600)
    assert reconcile(with_savings).outcome is Outcome.BALANCED
    assert computed_total(with_savings) == 33900


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_reconcile_is_deterministic_and_does_not_mutate() -> None:
    result = build("blinkit_001")
    before = result.model_dump()
    assert reconcile(result) == reconcile(result)
    assert result.model_dump() == before


# ---------------------------------------------------------------------------
# The MRP cross-check (brief 24.3)
# ---------------------------------------------------------------------------


def test_every_fixture_passes_the_mrp_cross_check(fixture_name: str) -> None:
    assert cross_check_mrp(build(fixture_name)).passed


@pytest.mark.parametrize(
    ("name", "lines_with_mrp", "derived_savings"),
    [
        ("blinkit_001", 5, 2700),
        ("blinkit_026", 3, 5600),
    ],
)
def test_derived_savings(name: str, lines_with_mrp: int, derived_savings: int) -> None:
    check = cross_check_mrp(build(name))
    assert check.outcome is MrpOutcome.OK
    assert check.lines_with_mrp == lines_with_mrp
    assert check.derived_savings_minor == derived_savings


def test_no_mrp_anywhere_is_not_applicable() -> None:
    check = cross_check_mrp(build("blinkit_032"))
    assert check.outcome is MrpOutcome.NOT_APPLICABLE
    assert check.passed
    assert check.lines_with_mrp == 0


def test_an_mrp_below_the_price_paid_is_impossible() -> None:
    """Paying above MRP is not legal in India, so this is always a misread."""
    check = cross_check_mrp(
        build(
            "blinkit_001",
            line_items=[
                {
                    "raw_name": "Amul Taaza Toned Milk 500ml",
                    "quantity_text": "500 ml x 2",
                    "mrp_minor": 5000,
                    "line_total_minor": 5600,
                }
            ],
        )
    )
    assert check.outcome is MrpOutcome.MISMATCH
    assert not check.passed
    assert check.impossible_line_indices == (0,)


def test_the_mrp_check_catches_what_reconciliation_cannot() -> None:
    """Section 24.5: the model reads the struck-through price as the price paid.

    Swap MRP and paid price on one line of 001 and adjust the total to match. The
    reconciliation gate is satisfied, because the arithmetic is now internally
    consistent. The MRP check still fails, which is the whole point of having two.
    """
    swapped = build(
        "blinkit_001",
        line_items=[
            {
                "raw_name": "Britannia Brown Bread 400g",
                "quantity_text": "400 g x 1",
                "mrp_minor": 5000,
                "line_total_minor": 5500,
            }
        ],
        charges=[],
        taxes=[],
        discounts=[],
        grand_total_minor=5500,
    )
    assert reconcile(swapped).outcome is Outcome.BALANCED
    assert cross_check_mrp(swapped).outcome is MrpOutcome.MISMATCH


def test_a_printed_savings_line_that_agrees_passes() -> None:
    check = cross_check_mrp(build("blinkit_026", printed_product_discount_minor=5600))
    assert check.outcome is MrpOutcome.OK
    assert check.printed_savings_minor == 5600
    assert check.delta_minor == 0


def test_a_printed_savings_line_that_disagrees_fails() -> None:
    check = cross_check_mrp(build("blinkit_026", printed_product_discount_minor=9900))
    assert check.outcome is MrpOutcome.MISMATCH
    assert check.delta_minor == 9900 - 5600


def test_the_savings_comparison_respects_tolerance() -> None:
    assert cross_check_mrp(build("blinkit_026", printed_product_discount_minor=5700)).passed
    assert not cross_check_mrp(build("blinkit_026", printed_product_discount_minor=5701)).passed


def test_mrp_check_rejects_a_negative_tolerance() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        cross_check_mrp(build("blinkit_001"), tolerance_minor=-1)


def test_the_two_validators_are_independent() -> None:
    """A receipt can pass one and fail the other, in both directions."""
    mrp_ok_recon_bad = build("blinkit_001", grand_total_minor=99999)
    assert not reconcile(mrp_ok_recon_bad).accepted
    assert cross_check_mrp(mrp_ok_recon_bad).passed
