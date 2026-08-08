"""The reconciliation gate.

Runs after extraction and before anything is stored. Pure arithmetic: no I/O, no
database, no network, no clock, no randomness. Given the same extraction it
always returns the same verdict, which is what makes it cheap to test and safe to
run on every receipt.

The equation, from brief section 24.3:

    sum(line_total_minor) + sum(charges) + sum(taxes) - sum(discounts) == grand_total_minor

`discounts` are order-level only. A product discount already inside a line price
is not in that sum, because it is already inside `line_total_minor`. Including it
in both places double-counts and rejects receipts that are perfectly fine.
Invariant 13.

**Why the gate exists.** A vision model that misses a fee line or double-counts a
coupon produces line items that all look plausible. Nothing about the output
flags as broken, so the bad numbers land in the ledger quietly. The arithmetic is
what catches it, and it costs one function call and zero API tokens.

**Why it is not a simple pass or fail.** Some receipts never balance no matter how
good the photo is: post-order partial refunds, wallet credits applied at payment
and absent from the itemised list, tips added afterwards. A hard reject on those
produces a loop where the user resends a perfectly clear photo forever, on exactly
the receipts most worth capturing. So failure splits into two classes (section
3.3), and only Class 1 is a photo problem.

`printed_product_discount_minor` is deliberately absent from the equation. It is
an input to the independent MRP cross-check below, never a reconciliation term.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from raseed.extraction.schemas import ExtractionResult

#: Brief section 18.4. Indian CGST and SGST are each rounded to the paisa on a
#: printed receipt, so the printed total legitimately differs from an exact sum by
#: a few paise. A tolerance of 1 paisa rejects receipts whose arithmetic is
#: correct. One rupee is loose enough to absorb rounding and far too tight to hide
#: a misread line, which is the only thing that matters.
DEFAULT_TOLERANCE_MINOR = 100


class Outcome(Enum):
    """What the gate decided.

    `BALANCED` and `SKIPPED` are the two accepted outcomes. `CLASS_1` and
    `CLASS_2` map to the split in brief section 3.3.
    """

    #: Arithmetic agrees with the printed total, within tolerance. Store it.
    BALANCED = "balanced"

    #: Nothing to reconcile against. Zero line items, as on a utility bill.
    #: Section 3.8: reconciliation must skip, not fail, or every such bill is
    #: rejected forever.
    SKIPPED = "skipped"

    #: Extraction failed. Nulls, missing grand total, not a receipt at all. This
    #: is a photo problem. Reject, ask for a clearer image, store NOTHING.
    CLASS_1 = "class_1_extraction_failed"

    #: Extraction looks complete, the item list is plausible, the totals just do
    #: not add up. Show the user the gap and offer a retake or logging it as an
    #: unaccounted adjustment.
    CLASS_2 = "class_2_arithmetic_mismatch"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The gate's verdict, with every number it used to reach it."""

    outcome: Outcome

    #: Sum of line totals, charges and taxes, less order-level discounts.
    computed_total_minor: int

    #: The grand total as printed on the receipt. None means it was not readable,
    #: which is itself a Class 1 failure.
    stated_total_minor: int | None

    #: `stated - computed`. Positive means the receipt charges more than the
    #: lines account for. None when there is no stated total to compare against.
    delta_minor: int | None

    tolerance_minor: int

    #: One line on why, suitable for the reject message. Empty when balanced.
    reason: str = ""

    @property
    def accepted(self) -> bool:
        """Whether this extraction may be stored at all."""
        return self.outcome in (Outcome.BALANCED, Outcome.SKIPPED)

    @property
    def unaccounted_adjustment_minor(self) -> int | None:
        """The gap to record so a Class 2 receipt stores arithmetically balanced.

        Brief section 3.3: this field is the important part. It keeps the ledger
        honest, keeps the receipt in the data, and gives a queryable signal. If it
        is populated on 30% of receipts from one merchant, the prompt has a
        specific bug and this says where to look.

        None on every other outcome, because there is nothing to adjust.
        """
        if self.outcome is not Outcome.CLASS_2:
            return None
        return self.delta_minor


def _sum_lines(result: ExtractionResult) -> int:
    return sum(item.line_total_minor for item in result.line_items)


def _sum_charges(result: ExtractionResult) -> int:
    return sum(charge.amount_minor for charge in result.charges)


def _sum_taxes(result: ExtractionResult) -> int:
    return sum(tax.amount_minor for tax in result.taxes)


def _sum_discounts(result: ExtractionResult) -> int:
    """Order-level discounts only. See the module docstring and invariant 13."""
    return sum(discount.amount_minor for discount in result.discounts)


def computed_total(result: ExtractionResult) -> int:
    """The total the receipt's own parts add up to."""
    return _sum_lines(result) + _sum_charges(result) + _sum_taxes(result) - _sum_discounts(result)


def _class_1_failure(
    result: ExtractionResult,
    *,
    computed: int,
    tolerance_minor: int,
    min_confidence: float | None,
) -> Reconciliation | None:
    """Return a Class 1 verdict if the extraction itself failed, else None.

    Class 1 is a photo problem: reject, ask for a clearer image, store nothing.
    Brief section 3.3.
    """
    stated = result.grand_total_minor

    def failed(reason: str, *, delta: int | None = None) -> Reconciliation:
        return Reconciliation(
            outcome=Outcome.CLASS_1,
            computed_total_minor=computed,
            stated_total_minor=stated,
            delta_minor=delta,
            tolerance_minor=tolerance_minor,
            reason=reason,
        )

    if not result.is_receipt:
        return failed(result.rejection_reason or "The image is not a receipt.")

    if min_confidence is not None and result.receipt_confidence < min_confidence:
        return failed(
            f"Confidence {result.receipt_confidence:.2f} is below the {min_confidence:.2f} floor."
        )

    if stated is None:
        return failed("No grand total could be read from the image.")

    if not (result.line_items or result.charges or result.taxes or result.discounts):
        return failed(
            "A total was read but no items, charges, taxes or discounts were.",
            delta=stated - computed,
        )

    return None


def reconcile(
    result: ExtractionResult,
    *,
    tolerance_minor: int = DEFAULT_TOLERANCE_MINOR,
    min_confidence: float | None = None,
) -> Reconciliation:
    """Decide whether an extraction may be stored.

    Args:
        result: What Stage 1 returned. Never mutated.
        tolerance_minor: How far the arithmetic may drift, in paise. See
            `DEFAULT_TOLERANCE_MINOR`.
        min_confidence: Optional Class 1 floor on `receipt_confidence`. Left off
            by default on purpose: the brief names low confidence as a Class 1
            symptom but never fixes a number, and inventing one here would bake a
            guess into the gate. The bot layer supplies it once real receipts say
            what the threshold should be.

    Returns:
        A `Reconciliation`. Check `.accepted` before storing anything.
    """
    if tolerance_minor < 0:
        msg = f"tolerance_minor must be non-negative, got {tolerance_minor}"
        raise ValueError(msg)

    computed = computed_total(result)

    # --- Class 1: the extraction itself failed. Store nothing. ---------------
    class_1 = _class_1_failure(
        result,
        computed=computed,
        tolerance_minor=tolerance_minor,
        min_confidence=min_confidence,
    )
    if class_1 is not None:
        return class_1

    # Past the Class 1 checks a stated total is guaranteed to exist. Stated as a
    # raise rather than an assert so it survives `python -O`.
    stated = result.grand_total_minor
    if stated is None:  # pragma: no cover
        msg = "unreachable: a missing grand total is already a Class 1 failure"
        raise AssertionError(msg)

    # --- Skip: nothing to reconcile against (section 3.8). -------------------
    # A utility bill prints an amount payable and no itemisation. There is no
    # arithmetic to check, so checking it would reject every such bill forever.
    if not result.line_items:
        return Reconciliation(
            outcome=Outcome.SKIPPED,
            computed_total_minor=computed,
            stated_total_minor=stated,
            delta_minor=stated - computed,
            tolerance_minor=tolerance_minor,
            reason="No line items to reconcile against.",
        )

    # --- The actual gate. ----------------------------------------------------
    delta = stated - computed
    if abs(delta) <= tolerance_minor:
        return Reconciliation(
            outcome=Outcome.BALANCED,
            computed_total_minor=computed,
            stated_total_minor=stated,
            delta_minor=delta,
            tolerance_minor=tolerance_minor,
        )

    return Reconciliation(
        outcome=Outcome.CLASS_2,
        computed_total_minor=computed,
        stated_total_minor=stated,
        delta_minor=delta,
        tolerance_minor=tolerance_minor,
        reason=(
            f"The items add up to {computed} paise but the receipt says {stated}, "
            f"a difference of {delta} paise."
        ),
    )


# ---------------------------------------------------------------------------
# Second, independent validator: the MRP cross-check (brief section 24.3)
# ---------------------------------------------------------------------------


class MrpOutcome(Enum):
    """Result of the MRP cross-check."""

    #: Every line with a struck-through price is internally consistent, and any
    #: printed savings line agrees with the derived figure.
    OK = "ok"

    #: No line carried an MRP, so there is nothing to cross-check.
    NOT_APPLICABLE = "not_applicable"

    #: A line price was misread. See `reason`.
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class MrpCrossCheck:
    """Whether the struck-through prices corroborate the paid prices.

    This is deliberately independent of `reconcile`. The reconciliation gate can
    pass while every line price is wrong, as long as the errors happen to cancel
    against the total. This check looks at a different quantity entirely, so the
    two fail for different reasons. Section 24.3 calls it a second validator at
    zero cost, which is exactly what it is.
    """

    outcome: MrpOutcome

    #: How many lines carried a struck-through price.
    lines_with_mrp: int

    #: `sum(mrp_minor) - sum(line_total_minor)` over those lines. This is the
    #: "you saved 104 rupees on this order" number.
    derived_savings_minor: int

    #: The savings figure printed on the receipt, if it printed one.
    printed_savings_minor: int | None

    #: `printed - derived`. None when the receipt printed no savings line.
    delta_minor: int | None

    tolerance_minor: int

    #: Indices into `line_items` where the struck-through price is below the paid
    #: price. Paying above MRP is not legal in India, so this is always a misread.
    impossible_line_indices: tuple[int, ...] = ()

    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is not MrpOutcome.MISMATCH


def cross_check_mrp(
    result: ExtractionResult,
    *,
    tolerance_minor: int = DEFAULT_TOLERANCE_MINOR,
) -> MrpCrossCheck:
    """Corroborate the paid prices against the struck-through prices.

    Two things are checked:

    1. **Per line.** A struck-through price below the price paid is impossible,
       so it means one of the two was misread. This is the concrete guard against
       the failure mode in section 24.5, where the model reads the crossed-out
       MRP as the amount paid.
    2. **In aggregate.** When the receipt prints a total savings line,
       `sum(mrp) - sum(line_total)` must equal it within tolerance.

    Returns:
        An `MrpCrossCheck`. Check `.passed`.
    """
    if tolerance_minor < 0:
        msg = f"tolerance_minor must be non-negative, got {tolerance_minor}"
        raise ValueError(msg)

    priced = [
        (index, item) for index, item in enumerate(result.line_items) if item.mrp_minor is not None
    ]
    impossible = tuple(
        index
        for index, item in priced
        if item.mrp_minor is not None and item.mrp_minor < item.line_total_minor
    )
    derived = sum(
        item.mrp_minor - item.line_total_minor for _, item in priced if item.mrp_minor is not None
    )
    printed = result.printed_product_discount_minor
    delta = None if printed is None else printed - derived

    if impossible:
        return MrpCrossCheck(
            outcome=MrpOutcome.MISMATCH,
            lines_with_mrp=len(priced),
            derived_savings_minor=derived,
            printed_savings_minor=printed,
            delta_minor=delta,
            tolerance_minor=tolerance_minor,
            impossible_line_indices=impossible,
            reason=(
                f"{len(impossible)} line(s) show a struck-through price below the "
                f"price paid, at index {list(impossible)}. One of the two was misread."
            ),
        )

    if not priced:
        return MrpCrossCheck(
            outcome=MrpOutcome.NOT_APPLICABLE,
            lines_with_mrp=0,
            derived_savings_minor=0,
            printed_savings_minor=printed,
            delta_minor=delta,
            tolerance_minor=tolerance_minor,
            reason="No line carried a struck-through price.",
        )

    if delta is not None and abs(delta) > tolerance_minor:
        return MrpCrossCheck(
            outcome=MrpOutcome.MISMATCH,
            lines_with_mrp=len(priced),
            derived_savings_minor=derived,
            printed_savings_minor=printed,
            delta_minor=delta,
            tolerance_minor=tolerance_minor,
            reason=(
                f"The lines imply {derived} paise of product savings but the receipt "
                f"prints {printed}, a difference of {delta} paise."
            ),
        )

    return MrpCrossCheck(
        outcome=MrpOutcome.OK,
        lines_with_mrp=len(priced),
        derived_savings_minor=derived,
        printed_savings_minor=printed,
        delta_minor=delta,
        tolerance_minor=tolerance_minor,
    )


__all__ = [
    "DEFAULT_TOLERANCE_MINOR",
    "MrpCrossCheck",
    "MrpOutcome",
    "Outcome",
    "Reconciliation",
    "computed_total",
    "cross_check_mrp",
    "reconcile",
]
