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
import hashlib
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from raseed.db.models import (
    AdjustmentKind,
    Category,
    DateSource,
    ExtractionStage,
    LexiconMiss,
    Merchant,
    RawExtraction,
    ReconciliationOutcome,
    Source,
    Transaction,
    TransactionAdjustment,
    TransactionLineItem,
)
from raseed.enrichment.categorize import Decision
from raseed.enrichment.categorize import Outcome as Stage2Outcome
from raseed.enrichment.providers.base import CategorizationProviderResult
from raseed.extraction.providers.base import ProviderResult
from raseed.extraction.schemas import group_from_json
from raseed.validation.reconcile import Outcome, Reconciliation

#: The gate's outcomes that may be stored, mapped onto their database enum.
#: `CLASS_1` is absent on purpose: a failed extraction stores nothing at all.
STORABLE_OUTCOMES: dict[Outcome, ReconciliationOutcome] = {
    Outcome.BALANCED: ReconciliationOutcome.BALANCED,
    Outcome.SKIPPED: ReconciliationOutcome.SKIPPED,
    Outcome.CLASS_2: ReconciliationOutcome.CLASS_2,
}


def receipt_sha256(batch: str, index: int) -> str:
    """The dedupe key for one receipt out of a batch.

    `transactions` carries a partial unique index on `(user_id, image_sha256)`
    for live rows, which is what stops the same photograph landing twice. Two
    receipts read out of one batch would collide on it, so every receipt after
    the first gets a key derived from the batch and its position.

    The first receipt keeps the batch's own digest untouched. That is what makes
    this change invisible to every row already in the ledger, and it keeps the
    common case, one batch holding one receipt, keyed on exactly what it was
    keyed on before.
    """
    if index == 0:
        return batch
    return hashlib.sha256(f"{batch}:{index}".encode()).hexdigest()


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
        stage=ExtractionStage.EXTRACTION,
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


def record_categorization(
    session: Session,
    *,
    user_id: str,
    result: CategorizationProviderResult,
    source: Source,
    image_sha256: str | None,
) -> RawExtraction:
    """Write what the Stage 2 fallback said, and what it cost.

    Stored in `raw_extractions` alongside Stage 1 for one reason that outranks
    tidiness: brief 16.6's daily cap sums `cost_micros_usd` over this table, and
    a billed call recorded anywhere else is a call the cap cannot see. The
    `stage` column keeps the two kinds of row apart.

    Nothing has a foreign key to a row written here. It is billing and
    provenance, not something a transaction derives from.
    """
    raw = RawExtraction(
        user_id=user_id,
        image_sha256=image_sha256,
        source=source,
        stage=ExtractionStage.CATEGORIZATION,
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
    raw: RawExtraction,
    reconciliation: Reconciliation,
    occurred_on_local: dt.date,
    date_source: DateSource,
    receipt_index: int = 0,
    merchant: Merchant | None = None,
    enrichment: Stage2Outcome | None = None,
) -> Transaction:
    """Commit a confirmed receipt to the ledger.

    The extraction is re-read from `raw.response_json` rather than passed in
    separately, so what is stored is provably what the model said. The user is
    taken from `raw` for the same reason: passing it alongside would allow a
    caller to file one user's receipt under another.

    `receipt_index` picks one receipt out of that response. A batch of images
    can hold several, they all point at the same immutable row, and the index is
    the only thing that tells them apart.

    `enrichment` is Stage 2's verdict, keyed by line-item position. It is
    optional because Stage 2 is not allowed to be able to lose a receipt: a
    categorizer that is missing, offline or out of budget produces nothing here
    and the receipt still lands, with its items uncategorized and re-runnable
    later from `raw_extractions` for free.

    Raises:
        NotStorableError: The gate returned Class 1. Nothing is written.
    """
    user_id = raw.user_id
    decisions = enrichment.decisions if enrichment else ()
    outcome = STORABLE_OUTCOMES.get(reconciliation.outcome)
    if outcome is None:
        msg = (
            f"refusing to store a {reconciliation.outcome.value} extraction: "
            f"{reconciliation.reason}"
        )
        raise NotStorableError(msg)

    group = group_from_json(raw.response_json)
    try:
        extraction = group.receipts[receipt_index]
    except IndexError as exc:
        msg = (
            f"receipt {receipt_index} was asked for and extraction {raw.id} holds "
            f"{len(group.receipts)}"
        )
        raise NotStorableError(msg) from exc

    transaction = Transaction(
        user_id=user_id,
        raw_extraction_id=raw.id,
        merchant_id=merchant.id if merchant else None,
        source=raw.source,
        image_sha256=(
            receipt_sha256(raw.image_sha256, receipt_index) if raw.image_sha256 else None
        ),
        receipt_index=receipt_index,
        order_id=extraction.order_id,
        currency=extraction.currency,
        grand_total_minor=extraction.grand_total_minor or 0,
        unaccounted_adjustment_minor=reconciliation.unaccounted_adjustment_minor or 0,
        reconciliation_outcome=outcome,
        # `occurred_at_utc` is left null. Nothing has ever passed one, because a
        # screenshot prints no clock, and invariant 6 buckets on the local date
        # regardless. The column stays for the receipts that do print a time.
        occurred_on_local=occurred_on_local,
        date_source=date_source,
    )
    session.add(transaction)
    session.flush()

    by_position = {decision.position: decision for decision in decisions}
    categories = _categories_by_slug(session, user_id=user_id)
    line_rows: dict[int, TransactionLineItem] = {}

    for position, item in enumerate(extraction.line_items):
        decision = by_position.get(position)
        category = categories.get(decision.category_slug) if decision else None

        line_row = TransactionLineItem(
            user_id=user_id,
            transaction_id=transaction.id,
            position=position,
            raw_name=item.raw_name,
            quantity_text=item.quantity_text,
            mrp_minor=item.mrp_minor,
            line_total_minor=item.line_total_minor,
            category_id=category.id if category else None,
            category_source=decision.source if decision else None,
            category_confidence_bp=decision.confidence_bp if decision else None,
            canonical_slug=decision.canonical_slug if decision else None,
            normalized_slug=decision.normalized.normalized_slug if decision else None,
            quantity=decision.normalized.quantity if decision else None,
            unit=decision.normalized.unit if decision else None,
            unit_normalized=decision.normalized.unit_normalized if decision else None,
            pack_count=decision.normalized.pack_count if decision else None,
        )
        session.add(line_row)
        line_rows[position] = line_row

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

    if enrichment is not None:
        record_lexicon_misses(
            session,
            user_id=user_id,
            decisions=decisions,
            line_items=line_rows,
            lexicon_name=enrichment.lexicon_name,
            lexicon_version=enrichment.lexicon_version,
        )

    return transaction


def _categories_by_slug(session: Session, *, user_id: str) -> dict[str, Category]:
    """This user's live categories, keyed by slug.

    One query rather than one per line item, and it is a lookup rather than a
    create: a Stage 2 answer is only ever allowed to name a category that
    already exists, so a slug that has been deleted leaves the item with no
    category rather than resurrecting it.
    """
    rows = session.scalars(
        select(Category).where(Category.user_id == user_id, Category.deleted_at.is_(None))
    ).all()
    return {category.slug: category for category in rows}


def record_lexicon_misses(
    session: Session,
    *,
    user_id: str,
    decisions: Sequence[Decision],
    line_items: dict[int, TransactionLineItem],
    lexicon_name: str,
    lexicon_version: int,
) -> list[LexiconMiss]:
    """Log the words the lexicon did not know. Brief 4.5.

    One row per occurrence, no counter, so the weekly review is a GROUP BY and
    nothing written here ever needs an UPDATE. The lexicon version travels with
    each row, which is what makes "did adding those twelve terms help" a query
    rather than a feeling.
    """
    written: list[LexiconMiss] = []
    for decision in decisions:
        line_item = line_items.get(decision.position)
        for term in decision.misses:
            miss = LexiconMiss(
                user_id=user_id,
                term=term,
                raw_name=decision.raw_name,
                lexicon_name=lexicon_name,
                lexicon_version=lexicon_version,
                line_item_id=line_item.id if line_item else None,
            )
            session.add(miss)
            written.append(miss)

    if written:
        session.flush()
    return written


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
    "receipt_sha256",
    "record_extraction",
    "record_transaction",
    "soft_delete_transaction",
]
