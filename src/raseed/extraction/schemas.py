"""The Stage 1 extraction contract.

This is the schema a vision model is constrained to when it reads a receipt. It
describes what is *printed*, verbatim, and nothing else. No categorization, no
normalization, no arithmetic. Those belong to Stage 2 (`raseed.enrichment`) and
to the reconciliation gate (`raseed.validation.reconcile`).

Three properties of this module are load-bearing:

**Field order is part of the contract.** Models generate left to right, so a
field placed early conditions everything after it. `is_receipt` and
`receipt_confidence` come first (brief section 21.2) so the model commits to
"this is a receipt" before it has invented a single line item. Within a line,
`mrp_minor` precedes `line_total_minor` (section 24.3) so the struck-through
price is read as the struck-through price rather than as the amount paid, which
is the specific failure mode section 24.5 names.

**Money is integer minor units.** `StrictInt` rather than `int`, so `5600.0` and
`"5600"` are rejected at the boundary instead of quietly becoming a float three
layers down. Invariant 1.

**There are no PII fields, by construction.** No address, no phone, no personal
name, no card digits. They are absent from the schema, so a model cannot return
them and there is nothing to leak or to redact later. Invariant 3.
`merchant_name` is a business, not a person.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

#: A money amount in minor units (paise), always non-negative.
#:
#: Signs are carried by the field's meaning, not by the number: `discounts` hold
#: positive amounts that the reconciliation gate subtracts. A negative value here
#: means the model misread something.
MinorAmount = Annotated[StrictInt, Field(ge=0)]

#: ISO 4217. INR only in practice, but the column stays honest (brief 3.5).
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

#: Verbatim text copied off the receipt. Never empty, never padded.
PrintedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Frozen(BaseModel):
    """Base config for every extraction model.

    `extra="forbid"` matters more than it looks: it means a provider that starts
    returning an undocumented field fails loudly instead of having it silently
    dropped. `frozen=True` supports invariant 5, that a raw extraction is never
    mutated after it is produced.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class LineItem(_Frozen):
    """One purchased item, exactly as printed.

    `line_total_minor` is always the amount actually PAID for this line, after
    any product-level discount already reflected in the printed price. A discount
    baked into the line price is never repeated in `ExtractionResult.discounts`.
    Invariant 13, brief section 24.3.
    """

    raw_name: PrintedText = Field(
        description=(
            "The item name exactly as printed, including brand, Hinglish spelling, "
            "size text and any slashes. Do not translate, expand, correct or tidy it."
        )
    )
    quantity_text: str | None = Field(
        default=None,
        description=(
            "The quantity string as printed, for example '500 ml x 2'. Copy it "
            "verbatim. Do not parse it into a number and a unit; that happens later."
        ),
    )
    mrp_minor: MinorAmount | None = Field(
        default=None,
        description=(
            "The struck-through or crossed-out price in paise, if one is shown. "
            "This is the price NOT paid. Null when no struck-through price appears."
        ),
    )
    line_total_minor: MinorAmount = Field(
        description=(
            "The amount actually paid for this line, in paise. This is the price "
            "shown in normal type, never the struck-through one."
        )
    )


class Charge(_Frozen):
    """An additive fee: delivery, handling, convenience, platform, surge, packaging.

    Zero is a legitimate value. Receipts routinely print 'Delivery charges FREE'
    or a struck-out fee resolving to nothing, and that line is worth keeping.
    """

    label: PrintedText = Field(description="The fee name exactly as printed.")
    amount_minor: MinorAmount = Field(
        description="The fee in paise. Zero if the receipt shows the fee as free or waived."
    )


class Tax(_Frozen):
    """A tax line: CGST, SGST, IGST, cess, or a single combined GST line."""

    label: PrintedText = Field(description="The tax name exactly as printed.")
    amount_minor: MinorAmount = Field(description="The tax amount in paise.")


class Discount(_Frozen):
    """An ORDER-LEVEL discount only.

    Coupons, promo codes, wallet credits, cashback applied at checkout. Anything
    that reduces the bill as a whole rather than a single item's price.

    A per-product discount is already inside `LineItem.line_total_minor` and must
    never appear here as well. Putting it in both places double-counts it and the
    reconciliation gate will reject a receipt that is actually fine. Invariant 13.
    """

    label: PrintedText = Field(description="The discount or coupon name exactly as printed.")
    amount_minor: MinorAmount = Field(
        description=(
            "The discount in paise as a POSITIVE number. The receipt prints it with "
            "a minus sign; record the magnitude. It is subtracted downstream."
        )
    )


class ExtractionResult(_Frozen):
    """Everything Stage 1 returns for one image.

    Do not reorder the fields. See the module docstring.
    """

    # --- Reasoning fields. These come first on purpose (brief 21.2). ----------
    is_receipt: bool = Field(
        description=(
            "True only if this image is a receipt, bill, or invoice showing amounts "
            "paid. A chat screenshot, a meme, a product listing, a menu or a bank "
            "statement is NOT a receipt. If it is not a receipt, say so here and "
            "leave every list below empty rather than inventing plausible values."
        )
    )
    receipt_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence that this is a receipt, from 0.0 to 1.0.",
    )
    rejection_reason: str | None = Field(
        default=None,
        description=(
            "Required when is_receipt is false: one short sentence on what the image "
            "actually shows. Must be null when is_receipt is true."
        ),
    )

    # --- Header. Frequently absent on the screenshot a user actually takes. ---
    merchant_name: str | None = Field(
        default=None,
        description=(
            "The merchant's business name if printed. Null if it is not printed. "
            "Do not infer it from logos, colours or app styling (brief 24.4)."
        ),
    )
    order_id: str | None = Field(
        default=None, description="The order or invoice number if printed, else null."
    )
    order_datetime_local: str | None = Field(
        default=None,
        description=(
            "The order date and time exactly as printed, as text. Do not convert "
            "timezones, do not reformat, do not guess if it is absent."
        ),
    )
    currency: CurrencyCode = Field(
        default="INR", description="ISO 4217 code for the amounts on this receipt."
    )

    # --- Body, in the order it is printed, top to bottom. --------------------
    line_items: list[LineItem] = Field(
        default_factory=list,
        description=(
            "Every purchased item. Empty for receipts that have no itemisation at "
            "all, such as a utility bill. Never invent items to make a total work."
        ),
    )
    charges: list[Charge] = Field(
        default_factory=list, description="Every additive fee line in the bill summary."
    )
    taxes: list[Tax] = Field(default_factory=list, description="Every tax line.")
    discounts: list[Discount] = Field(
        default_factory=list,
        description=(
            "ORDER-LEVEL discounts only: coupons, promo codes, wallet credits. "
            "Never include a per-item discount that is already reflected in that "
            "item's line price."
        ),
    )
    printed_product_discount_minor: MinorAmount | None = Field(
        default=None,
        description=(
            "If the receipt prints a single summary line for total product savings "
            "(for example 'Product discount -56'), record its magnitude in paise. "
            "This is NOT an order-level discount and must not appear in discounts. "
            "It is a cross-check only. Null if no such line is printed."
        ),
    )
    grand_total_minor: MinorAmount | None = Field(
        default=None,
        description=(
            "The final amount payable, in paise, COPIED from the printed total. "
            "Never compute it by adding the lines above. If it is not legible or "
            "not present, return null."
        ),
    )

    @model_validator(mode="after")
    def _rejection_reason_matches_is_receipt(self) -> Self:
        """A rejection must say why, and an acceptance must not pretend to be one."""
        if not self.is_receipt and not (self.rejection_reason or "").strip():
            msg = "rejection_reason is required when is_receipt is false"
            raise ValueError(msg)
        if self.is_receipt and self.rejection_reason is not None:
            msg = "rejection_reason must be null when is_receipt is true"
            raise ValueError(msg)
        return self

    @property
    def is_storable(self) -> bool:
        """Whether anything from this extraction may be persisted at all.

        Invariant 11: a false `is_receipt` means store nothing. Callers check this
        before the reconciliation gate, not after.
        """
        return self.is_receipt


__all__ = [
    "Charge",
    "CurrencyCode",
    "Discount",
    "ExtractionResult",
    "LineItem",
    "MinorAmount",
    "PrintedText",
    "Tax",
]
