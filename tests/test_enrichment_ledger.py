"""Persisting Stage 2: categories on line items, misses in the log, cost in the cap.

The decisions themselves are tested in `test_categorize.py`. What is under test
here is that they survive the trip to the database and that the Stage 2 spend is
visible to brief 16.6's daily cap, which is the one way a billed call can go
quietly missing.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from raseed.db import ledger, queries
from raseed.db.models import (
    Category,
    CategorySource,
    DateSource,
    ExtractionStage,
    LexiconMiss,
    RawExtraction,
    Source,
    TransactionLineItem,
    User,
)
from raseed.enrichment.categorize import Item, categorize
from raseed.enrichment.providers.base import CategorizationProviderResult
from raseed.enrichment.schemas import CategorizationResult
from raseed.extraction.providers.base import ProviderResult
from raseed.extraction.schemas import ExtractionResult
from raseed.validation.reconcile import reconcile

ALLOWED = ("groceries", "food-and-dining", "drinks", "entertainment", "uncategorized")


def extraction_with(*names: str, quantity_text: str | None = None) -> ExtractionResult:
    """A minimal balanced receipt whose line items are the given names."""
    items = [
        {
            "raw_name": name,
            "quantity_text": quantity_text,
            "mrp_minor": None,
            "line_total_minor": 1000,
        }
        for name in names
    ]
    return ExtractionResult.model_validate(
        {
            "is_receipt": True,
            "receipt_confidence": 1.0,
            "rejection_reason": None,
            "merchant_name": "Test Mart",
            "order_id": None,
            "order_datetime_local": None,
            "currency": "INR",
            "line_items": items,
            "charges": [],
            "taxes": [],
            "discounts": [],
            "printed_product_discount_minor": None,
            "grand_total_minor": 1000 * len(items),
        }
    )


def stored_extraction(session: Session, user: User, extraction: ExtractionResult) -> RawExtraction:
    return ledger.record_extraction(
        session,
        user_id=user.id,
        result=ProviderResult(
            extraction=extraction,
            model_id="gemini-3.6-flash",
            prompt_version="v1",
            response_text=extraction.model_dump_json(),
            input_tokens=2000,
            output_tokens=400,
            cost_micros_usd=7900,
        ),
        source=Source.TELEGRAM_IMAGE,
        image_sha256="c" * 64,
    )


def commit(
    session: Session, user: User, extraction: ExtractionResult, *, enrich: bool = True
) -> list[TransactionLineItem]:
    raw = stored_extraction(session, user, extraction)
    enrichment = (
        categorize(
            [Item(item.raw_name, item.quantity_text) for item in extraction.line_items],
            allowed=ALLOWED,
        )
        if enrich
        else None
    )
    transaction = ledger.record_transaction(
        session,
        raw=raw,
        reconciliation=reconcile(extraction),
        occurred_on_local=dt.date(2026, 8, 8),
        date_source=DateSource.MESSAGE_TIMESTAMP,
        enrichment=enrichment,
    )
    session.commit()
    return list(
        session.scalars(
            select(TransactionLineItem)
            .where(TransactionLineItem.transaction_id == transaction.id)
            .order_by(TransactionLineItem.position)
        ).all()
    )


# ---------------------------------------------------------------------------
# Categories on line items
# ---------------------------------------------------------------------------


def test_a_lexicon_hit_lands_on_the_line_item(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    (item,) = commit(session, user, extraction_with("Green Cucumber"))

    groceries = session.scalars(select(Category).where(Category.slug == "groceries")).one()
    assert item.category_id == groceries.id
    assert item.category_source is CategorySource.LEXICON_EXACT
    assert item.category_confidence_bp is None
    assert item.canonical_slug == "cucumber"
    assert item.normalized_slug == "green-cucumber"


def test_the_quantity_parse_lands_on_the_line_item(seeded: tuple[Session, User]) -> None:
    """Brief 16.7, and the reason `quantity` is an integer in the small unit."""
    session, user = seeded
    (item,) = commit(session, user, extraction_with("Toor Dal 1kg"))

    assert item.normalized_slug == "toor-dal"
    assert item.canonical_slug == "pigeon_pea"
    assert item.quantity == 1000
    assert item.unit == "kg"
    assert item.unit_normalized == "g"
    assert item.pack_count is None


def test_the_printed_name_is_never_touched(seeded: tuple[Session, User]) -> None:
    """Stage 2 derives. It does not rewrite what was printed. Brief 3.7."""
    session, user = seeded
    (item,) = commit(session, user, extraction_with("Green Cucumber"))
    assert item.raw_name == "Green Cucumber"


def test_an_unknown_item_stores_undecided(seeded: tuple[Session, User]) -> None:
    """Null source is the set the backfill tool re-runs. It is not the same fact
    as "ran and landed on uncategorized"."""
    session, user = seeded
    (item,) = commit(session, user, extraction_with("Colgate Strong Teeth"))

    uncategorized = session.scalars(select(Category).where(Category.slug == "uncategorized")).one()
    assert item.category_id == uncategorized.id
    assert item.category_source is None
    assert item.category_confidence_bp is None


def test_a_receipt_without_enrichment_still_lands(seeded: tuple[Session, User]) -> None:
    """Stage 2 must never be able to cost the user a receipt."""
    session, user = seeded
    items = commit(session, user, extraction_with("Banana", "Green Cucumber"), enrich=False)

    assert [item.raw_name for item in items] == ["Banana", "Green Cucumber"]
    assert all(item.category_id is None for item in items)
    assert all(item.category_source is None for item in items)


def test_confidence_is_stored_in_range(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    (item,) = commit(session, user, extraction_with("panner tikka"))
    assert item.category_source is CategorySource.LEXICON_FUZZY
    assert item.category_confidence_bp is not None
    assert 0 <= item.category_confidence_bp <= 10_000


# ---------------------------------------------------------------------------
# The miss log
# ---------------------------------------------------------------------------


def test_misses_are_written_with_their_line_item(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    (item,) = commit(session, user, extraction_with("Colgate Strong Teeth"))

    misses = session.scalars(select(LexiconMiss).order_by(LexiconMiss.term)).all()
    assert [miss.term for miss in misses] == ["colgate", "strong", "teeth"]
    assert all(miss.line_item_id == item.id for miss in misses)
    assert all(miss.raw_name == "Colgate Strong Teeth" for miss in misses)
    assert all(miss.lexicon_name == "hinglish" for miss in misses)
    assert all(miss.lexicon_version >= 1 for miss in misses)


def test_a_known_item_logs_nothing(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    commit(session, user, extraction_with("Banana"))
    assert session.scalars(select(LexiconMiss)).all() == []


def test_one_row_per_occurrence(seeded: tuple[Session, User]) -> None:
    """No counter column, so the weekly review is a GROUP BY and nothing UPDATEs."""
    session, user = seeded
    commit(session, user, extraction_with("Colgate Strong Teeth", "Colgate Whitening"))

    colgate = session.scalars(select(LexiconMiss).where(LexiconMiss.term == "colgate")).all()
    assert len(colgate) == 2


# ---------------------------------------------------------------------------
# Cost, which is the thing that goes quietly missing
# ---------------------------------------------------------------------------


def categorization_result(cost: int) -> CategorizationProviderResult:
    return CategorizationProviderResult(
        categorization=CategorizationResult(items=[]),
        model_id="gemini-3.5-flash-lite",
        prompt_version="categorize-v1",
        response_text='{"items": []}',
        input_tokens=300,
        output_tokens=40,
        cost_micros_usd=cost,
    )


def test_a_stage_two_call_is_recorded_as_its_own_row(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    raw = ledger.record_categorization(
        session,
        user_id=user.id,
        result=categorization_result(120),
        source=Source.TELEGRAM_IMAGE,
        image_sha256="d" * 64,
    )
    session.commit()

    assert raw.stage is ExtractionStage.CATEGORIZATION
    assert raw.model_id == "gemini-3.5-flash-lite"
    assert raw.prompt_version == "categorize-v1"


def test_stage_one_rows_stay_marked_as_extraction(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    raw = stored_extraction(session, user, extraction_with("Banana"))
    session.commit()
    assert raw.stage is ExtractionStage.EXTRACTION


def test_stage_two_spend_counts_against_the_daily_cap(seeded: tuple[Session, User]) -> None:
    """The whole reason Stage 2 writes to this table at all. Brief 16.6."""
    session, user = seeded
    since = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    stored_extraction(session, user, extraction_with("Banana"))
    session.commit()
    after_stage_one = queries.spend_micros_since(session, user_id=user.id, since=since)

    ledger.record_categorization(
        session,
        user_id=user.id,
        result=categorization_result(120),
        source=Source.TELEGRAM_IMAGE,
        image_sha256=None,
    )
    session.commit()

    assert after_stage_one == 7900
    assert queries.spend_micros_since(session, user_id=user.id, since=since) == 8020
