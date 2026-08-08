"""Tests for the Stage 1 extraction contract.

These run entirely against `tests/fixtures/`. No API calls, no images, no `data/`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from conftest import as_extraction_payload, load_fixture
from raseed.extraction.schemas import (
    Charge,
    Discount,
    ExtractionResult,
    LineItem,
    Tax,
)

ALL_MODELS: tuple[type[BaseModel], ...] = (ExtractionResult, LineItem, Charge, Tax, Discount)


# ---------------------------------------------------------------------------
# The committed fixtures round-trip through the contract
# ---------------------------------------------------------------------------


def test_every_fixture_validates(fixture_name: str) -> None:
    result = ExtractionResult.model_validate(as_extraction_payload(fixture_name))
    assert result.is_receipt is True
    assert result.currency == "INR"


def test_fixture_values_survive_validation(fixture_name: str) -> None:
    """Validation must not quietly coerce, reorder or drop anything."""
    raw = load_fixture(fixture_name)
    result = ExtractionResult.model_validate(as_extraction_payload(fixture_name))

    assert result.grand_total_minor == raw["grand_total_minor"]
    assert len(result.line_items) == len(raw["line_items"])
    assert len(result.charges) == len(raw["charges"])
    assert len(result.discounts) == len(raw["discounts"])
    assert len(result.taxes) == len(raw["taxes"])

    for parsed, original in zip(result.line_items, raw["line_items"], strict=True):
        assert parsed.raw_name == original["raw_name"]
        assert parsed.quantity_text == original["quantity_text"]
        assert parsed.mrp_minor == original["mrp_minor"]
        assert parsed.line_total_minor == original["line_total_minor"]


def test_fixtures_cover_the_three_shapes_we_care_about() -> None:
    """A guard on the fixture set itself, so a future edit cannot silently narrow it."""
    normal = ExtractionResult.model_validate(as_extraction_payload("blinkit_001"))
    coupon = ExtractionResult.model_validate(as_extraction_payload("blinkit_026"))
    no_items = ExtractionResult.model_validate(as_extraction_payload("blinkit_032"))

    assert any(item.mrp_minor is None for item in normal.line_items), "001 should have a null MRP"
    assert coupon.discounts, "026 should carry an order-level discount"
    assert no_items.line_items == [], "032 should have zero line items"


def test_zero_line_items_is_valid_not_an_error() -> None:
    """Utility bills have no itemisation. The contract must accept that (brief 3.8)."""
    result = ExtractionResult.model_validate(as_extraction_payload("blinkit_032"))
    assert result.line_items == []
    assert result.grand_total_minor == 1500


# ---------------------------------------------------------------------------
# Field order is part of the contract (brief 21.2 and 24.3)
# ---------------------------------------------------------------------------


def test_reasoning_fields_come_first() -> None:
    names = list(ExtractionResult.model_fields)
    assert names[:3] == ["is_receipt", "receipt_confidence", "rejection_reason"]


def test_is_receipt_precedes_every_answer_field() -> None:
    names = list(ExtractionResult.model_fields)
    for answer_field in ("line_items", "charges", "taxes", "discounts", "grand_total_minor"):
        assert names.index("is_receipt") < names.index(answer_field)
        assert names.index("receipt_confidence") < names.index(answer_field)


def test_mrp_precedes_line_total() -> None:
    """The struck-through price is read before the paid price (brief 24.5)."""
    names = list(LineItem.model_fields)
    assert names.index("mrp_minor") < names.index("line_total_minor")


def test_json_schema_preserves_property_order() -> None:
    """Providers build their constrained decoder from this. Order must survive."""
    properties = list(ExtractionResult.model_json_schema()["properties"])
    assert properties[:3] == ["is_receipt", "receipt_confidence", "rejection_reason"]


# ---------------------------------------------------------------------------
# Invariant 3: no PII fields, anywhere, ever
# ---------------------------------------------------------------------------

BANNED_FIELD_TOKENS = (
    "address",
    "aadhaar",
    "aadhar",
    "card",
    "contact",
    "customer",
    "cvv",
    "email",
    "mobile",
    "pan_",
    "phone",
    "pincode",
    "postcode",
    "recipient",
    "upi",
    "zip",
)


@pytest.mark.parametrize("model", ALL_MODELS)
def test_no_pii_field_names(model: type[BaseModel]) -> None:
    for field_name in model.model_fields:
        lowered = field_name.lower()
        for banned in BANNED_FIELD_TOKENS:
            assert banned not in lowered, f"{model.__name__}.{field_name} looks like PII"


def test_only_business_names_are_captured() -> None:
    """`merchant_name` is a shop. There is no field for a person's name."""
    name_fields = {f for f in ExtractionResult.model_fields if "name" in f}
    assert name_fields == {"merchant_name"}
    assert {f for f in LineItem.model_fields if "name" in f} == {"raw_name"}


# ---------------------------------------------------------------------------
# Invariant 1: integer paise, never a float
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_amount", [56.0, 56.5, "5600", True, None])
def test_money_rejects_non_integers(bad_amount: Any) -> None:
    with pytest.raises(ValidationError):
        LineItem.model_validate(
            {
                "raw_name": "Chai",
                "quantity_text": None,
                "mrp_minor": None,
                "line_total_minor": bad_amount,
            }
        )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (LineItem, {"raw_name": "Chai", "line_total_minor": -1}),
        (Charge, {"label": "Delivery", "amount_minor": -1}),
        (Tax, {"label": "CGST", "amount_minor": -1}),
        (Discount, {"label": "FLAT100", "amount_minor": -1}),
    ],
)
def test_money_rejects_negatives(model: type[BaseModel], payload: dict[str, Any]) -> None:
    """Discounts are stored positive and subtracted downstream, so nothing is negative."""
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_a_free_charge_is_zero_not_an_error() -> None:
    """'Delivery charges FREE' is a real line worth keeping."""
    assert Charge.model_validate({"label": "Delivery charges", "amount_minor": 0}).amount_minor == 0


def test_grand_total_may_be_absent() -> None:
    """An illegible total is a Class 1 rejection downstream, not a parse failure."""
    result = ExtractionResult.model_validate(
        as_extraction_payload("blinkit_001", grand_total_minor=None)
    )
    assert result.grand_total_minor is None


# ---------------------------------------------------------------------------
# Invariant 11: is_receipt gates everything
# ---------------------------------------------------------------------------


def test_rejection_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="rejection_reason is required"):
        ExtractionResult.model_validate(
            {"is_receipt": False, "receipt_confidence": 0.05, "rejection_reason": None}
        )


def test_a_blank_rejection_reason_is_not_a_reason() -> None:
    with pytest.raises(ValidationError, match="rejection_reason is required"):
        ExtractionResult.model_validate(
            {"is_receipt": False, "receipt_confidence": 0.05, "rejection_reason": "   "}
        )


def test_an_accepted_receipt_carries_no_rejection_reason() -> None:
    with pytest.raises(ValidationError, match="must be null when is_receipt is true"):
        ExtractionResult.model_validate(
            as_extraction_payload("blinkit_001", rejection_reason="looks like a meme")
        )


def test_a_non_receipt_is_valid_and_not_storable() -> None:
    result = ExtractionResult.model_validate(
        {
            "is_receipt": False,
            "receipt_confidence": 0.02,
            "rejection_reason": "A screenshot of a chat conversation.",
        }
    )
    assert result.is_storable is False
    assert result.line_items == []
    assert result.grand_total_minor is None


def test_a_real_receipt_is_storable() -> None:
    assert ExtractionResult.model_validate(as_extraction_payload("blinkit_001")).is_storable


@pytest.mark.parametrize("bad_confidence", [-0.01, 1.01])
def test_confidence_is_bounded(bad_confidence: float) -> None:
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({"is_receipt": True, "receipt_confidence": bad_confidence})


# ---------------------------------------------------------------------------
# Contract hygiene
# ---------------------------------------------------------------------------


#: A minimal valid payload for each model, used by the hygiene tests below.
MINIMAL_PAYLOADS: dict[type[BaseModel], dict[str, Any]] = {
    ExtractionResult: {"is_receipt": True, "receipt_confidence": 1.0},
    LineItem: {"raw_name": "Chai", "line_total_minor": 1000},
    Charge: {"label": "Delivery", "amount_minor": 0},
    Tax: {"label": "CGST", "amount_minor": 0},
    Discount: {"label": "FLAT100", "amount_minor": 1},
}


@pytest.mark.parametrize("model", ALL_MODELS)
def test_unknown_fields_are_rejected(model: type[BaseModel]) -> None:
    """A provider that starts returning a new field fails loudly, not silently."""
    with pytest.raises(ValidationError):
        model.model_validate({**MINIMAL_PAYLOADS[model], "customer_phone": "9999999999"})


@pytest.mark.parametrize("model", ALL_MODELS)
def test_models_are_frozen(model: type[BaseModel]) -> None:
    """Invariant 5: a raw extraction is never mutated after it is produced."""
    assert model.model_config.get("frozen") is True


def test_currency_must_be_an_iso_code() -> None:
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(as_extraction_payload("blinkit_001", currency="rupees"))


def test_raw_name_may_not_be_blank() -> None:
    with pytest.raises(ValidationError):
        LineItem.model_validate({"raw_name": "   ", "line_total_minor": 1000})


def test_printed_product_discount_defaults_to_null() -> None:
    """The fixtures do not print one. The 24.3 cross-check has to cope with that."""
    result = ExtractionResult.model_validate(as_extraction_payload("blinkit_026"))
    assert result.printed_product_discount_minor is None
