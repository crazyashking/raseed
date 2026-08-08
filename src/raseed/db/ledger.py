"""Every write the application makes.

Two rules govern this module and nothing here is allowed to bend them:

- **Append only.** A correction is a new row. Nothing already written is edited.
  Invariant 2.
- **`raw_extractions` is written once and never touched again.** Invariant 5.

`record_extraction` is called as soon as a model answers, before the user has
confirmed anything, because what the model said is worth keeping even if the
receipt is discarded. `record_transaction` is called only on confirm.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from raseed.db.models import (
    AdjustmentKind,
    DateSource,
    Merchant,
    RawExtraction,
    ReconciliationOutcome,
    Source,
    Transaction,
    TransactionAdjustment,
    TransactionLineItem,
)
from raseed.extraction.providers.base import ProviderResult
from raseed.extraction.schemas import ExtractionResult
from raseed.validation.reconcile import Outcome, Reconciliation

#: The gate's outcomes that may be stored, mapped onto their database enum.
#: `CLASS_1` is absent on purpose: a failed extraction stores nothing at all.
STORABLE_OUTCOMES: dict[Outcome, ReconciliationOutcome] = {
    Outcome.BALANCED: ReconciliationOutcome.BALANCED,
    Outcome.SKIPPED: ReconciliationOutcome.SKIPPED,
    Outcome.CLASS_2: ReconciliationOutcome.CLASS_2,
}


class NotStorableError(RuntimeError):
    """Raised on an attempt to store something the gate rejected outright."""


def record_extraction(
    session: Session,
    *,
    user_id: str,
    result: ProviderResult,
    source: Source,
    image_sha256: str | None,
) -> RawExtraction:
    """Write what the model said, verbatim, before anyone decides what to do with it.

    Written even when the extraction is later discarded, because a wrong answer
    is evidence about the prompt. Never updated, never deleted. Invariant 5.
    """
    raw = RawExtraction(
        user_id=user_id,
        image_sha256=image_sha256,
        source=source,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        response_json=result.response_text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_micros_usd=result.cost_micros_usd,
    )
    session.add(raw)
    session.flush()
    return raw


def record_transaction(
    session: Session,
    *,
    user_id: str,
    raw: RawExtraction,
    reconciliation: Reconciliation,
    occurred_on_local: dt.date,
    date_source: DateSource,
    merchant: Merchant | None = None,
    occurred_at_utc: dt.datetime | None = None,
) -> Transaction:
    """Commit a confirmed receipt to the ledger.

    The extraction is re-read from `raw.response_json` rather than passed in
    separately, so what is stored is provably what the model said.

    Raises:
        NotStorableError: The gate returned Class 1. Nothing is written.
    """
    outcome = STORABLE_OUTCOMES.get(reconciliation.outcome)
    if outcome is None:
        msg = (
            f"refusing to store a {reconciliation.outcome.value} extraction: "
            f"{reconciliation.reason}"
        )
        raise NotStorableError(msg)

    extraction = ExtractionResult.model_validate_json(raw.response_json)

    transaction = Transaction(
        user_id=user_id,
        raw_extraction_id=raw.id,
        merchant_id=merchant.id if merchant else None,
        source=raw.source,
        image_sha256=raw.image_sha256,
        order_id=extraction.order_id,
        currency=extraction.currency,
        grand_total_minor=extraction.grand_total_minor or 0,
        unaccounted_adjustment_minor=reconciliation.unaccounted_adjustment_minor or 0,
        reconciliation_outcome=outcome,
        occurred_at_utc=occurred_at_utc,
        occurred_on_local=occurred_on_local,
        date_source=date_source,
    )
    session.add(transaction)
    session.flush()

    for position, item in enumerate(extraction.line_items):
        session.add(
            TransactionLineItem(
                user_id=user_id,
                transaction_id=transaction.id,
                position=position,
                raw_name=item.raw_name,
                quantity_text=item.quantity_text,
                mrp_minor=item.mrp_minor,
                line_total_minor=item.line_total_minor,
            )
        )

    position = 0
    for kind, rows in (
        (AdjustmentKind.CHARGE, extraction.charges),
        (AdjustmentKind.TAX, extraction.taxes),
        (AdjustmentKind.DISCOUNT, extraction.discounts),
    ):
        for row in rows:
            session.add(
                TransactionAdjustment(
                    user_id=user_id,
                    transaction_id=transaction.id,
                    position=position,
                    kind=kind,
                    label=row.label,
                    amount_minor=row.amount_minor,
                )
            )
            position += 1

    session.flush()
    return transaction


def soft_delete_transaction(
    session: Session, *, transaction: Transaction, when: dt.datetime
) -> Transaction:
    """Mark a transaction deleted without removing it. Invariant 2, brief 16.3.

    `when` is passed in rather than read from the clock so this stays testable
    and so the caller owns the timestamp.
    """
    transaction.deleted_at = when
    session.flush()
    return transaction


def ensure_merchant(
    session: Session, *, user_id: str, slug: str, display_name: str, merchant_tz: str
) -> Merchant:
    """Find or create a merchant. Never renames an existing one.

    `display_name` is mutable by design and only the slug is stable, so an
    existing row is returned untouched. Brief 3.8.
    """
    existing = session.scalars(
        select(Merchant).where(Merchant.user_id == user_id, Merchant.slug == slug).limit(1)
    ).first()
    if existing is not None:
        return existing

    merchant = Merchant(
        user_id=user_id, slug=slug, display_name=display_name, merchant_tz=merchant_tz
    )
    session.add(merchant)
    session.flush()
    return merchant


__all__ = [
    "STORABLE_OUTCOMES",
    "NotStorableError",
    "ensure_merchant",
    "record_extraction",
    "record_transaction",
    "soft_delete_transaction",
]
