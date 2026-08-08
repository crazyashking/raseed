"""Tests for the ledger schema.

Most of these assert invariants directly against `Base.metadata`, so they fail
the moment a column is added that breaks one. That is deliberate: the invariants
in `CLAUDE.md` are easier to hold if a test holds them rather than a person.

No API calls, no images, no `data/`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Date, DateTime, Integer, Table, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from conftest import as_extraction_payload
from raseed.db.engine import create_engine, foreign_keys_enforced
from raseed.db.models import (
    SEED_CATEGORIES,
    SOFT_DELETABLE_TABLES,
    UNCATEGORIZED_SLUG,
    AdjustmentKind,
    Base,
    Category,
    DateSource,
    Merchant,
    RawExtraction,
    ReconciliationOutcome,
    Source,
    Transaction,
    TransactionAdjustment,
    TransactionLineItem,
    User,
)
from raseed.db.seed import bootstrap, ensure_categories, uncategorized
from raseed.extraction.schemas import ExtractionResult
from raseed.validation.reconcile import Outcome, computed_total, reconcile

TABLES: list[Table] = list(Base.metadata.sorted_tables)

#: By name, because `Model.__table__` is typed as a generic FromClause and the
#: metadata lookup keeps mypy strict happy without a cast.
TABLE: dict[str, Table] = {table.name: table for table in TABLES}


def column_names(table: Table) -> set[str]:
    return {column.name for column in table.columns}


def fetched[Row: Base](session: Session, model: type[Row], pk: str | None) -> Row:
    """`session.get` that fails the test rather than returning None."""
    assert pk is not None, f"no {model.__name__} id to fetch"
    row = session.get(model, pk)
    assert row is not None, f"{model.__name__} {pk} is not in the ledger"
    return row


# ---------------------------------------------------------------------------
# Invariant 8: every table has user_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES, ids=lambda t: str(t.name))
def test_every_table_has_user_id(table: Table) -> None:
    """Costs nothing now. It is the migration you do not want to do later."""
    if table.name == "users":
        assert "id" in column_names(table)
        return
    assert "user_id" in column_names(table)


def test_no_table_was_forgotten() -> None:
    assert {table.name for table in TABLES} == {
        "users",
        "merchants",
        "categories",
        "raw_extractions",
        "transactions",
        "transaction_line_items",
        "transaction_adjustments",
    }


# ---------------------------------------------------------------------------
# Invariant 1: integer minor units, never a float
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", TABLES, ids=lambda t: str(t.name))
def test_no_floating_point_columns_anywhere(table: Table) -> None:
    for column in table.columns:
        type_name = type(column.type).__name__.lower()
        assert "float" not in type_name, f"{table.name}.{column.name} is floating point"
        assert "numeric" not in type_name, f"{table.name}.{column.name} is Numeric"
        assert "decimal" not in type_name, f"{table.name}.{column.name} is Decimal"
        assert "real" not in type_name, f"{table.name}.{column.name} is Real"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: str(t.name))
def test_money_columns_are_integers(table: Table) -> None:
    money = [c for c in table.columns if c.name.endswith("_minor") or "cost" in c.name]
    for column in money:
        assert isinstance(column.type, Integer), f"{table.name}.{column.name} is not Integer"


def test_money_columns_exist_where_expected() -> None:
    assert "grand_total_minor" in column_names(TABLE["transactions"])
    assert "unaccounted_adjustment_minor" in column_names(TABLE["transactions"])
    assert {"mrp_minor", "line_total_minor"} <= column_names(TABLE["transaction_line_items"])
    assert "amount_minor" in column_names(TABLE["transaction_adjustments"])


# ---------------------------------------------------------------------------
# Invariant 3: no PII columns
# ---------------------------------------------------------------------------

BANNED_COLUMN_TOKENS = (
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
    "telegram",
    "upi",
    "zip",
)


@pytest.mark.parametrize("table", TABLES, ids=lambda t: str(t.name))
def test_no_pii_columns(table: Table) -> None:
    for column in table.columns:
        lowered = column.name.lower()
        for banned in BANNED_COLUMN_TOKENS:
            assert banned not in lowered, f"{table.name}.{column.name} looks like PII"


def test_the_user_table_holds_no_external_identifier() -> None:
    """Access control lives in the environment, so no account ID lands in the DB."""
    assert column_names(TABLE["users"]) == {"id", "created_at", "deleted_at"}


# ---------------------------------------------------------------------------
# Invariants 2 and 5: soft deletes everywhere, and raw_extractions is immutable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", SOFT_DELETABLE_TABLES)
def test_soft_deletable_tables_have_deleted_at(table_name: str) -> None:
    assert "deleted_at" in column_names(TABLE[table_name])


def test_only_raw_extractions_is_exempt_from_soft_delete() -> None:
    """Invariant 5 is the only reason a table may lack `deleted_at`."""
    assert set(SOFT_DELETABLE_TABLES) == {t.name for t in TABLES} - {"raw_extractions"}


def test_raw_extractions_cannot_be_soft_deleted_or_updated() -> None:
    """Invariant 5. No deleted_at and no updated_at, because neither ever happens."""
    columns = column_names(TABLE["raw_extractions"])
    assert "deleted_at" not in columns
    assert "updated_at" not in columns


@pytest.mark.parametrize("table", TABLES, ids=lambda t: str(t.name))
def test_nothing_has_an_updated_at(table: Table) -> None:
    """Corrections are new rows, not edits in place. Invariant 2."""
    assert "updated_at" not in column_names(table)


def test_a_soft_delete_leaves_the_row_in_place(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    merchant = Merchant(user_id=user.id, slug="blinkit", display_name="Blinkit")
    session.add(merchant)
    session.commit()

    merchant.deleted_at = dt.datetime(2026, 8, 8, tzinfo=dt.UTC)
    session.commit()

    assert session.get(Merchant, merchant.id) is not None
    live = session.scalars(select(Merchant).where(Merchant.deleted_at.is_(None))).all()
    assert live == []


# ---------------------------------------------------------------------------
# Invariant 6: period queries bucket on occurred_on_local
# ---------------------------------------------------------------------------


def test_occurred_on_local_is_a_date_not_a_timestamp() -> None:
    """A DATE cannot carry a timezone, so it cannot be bucketed on the wrong one."""
    column = TABLE["transactions"].columns["occurred_on_local"]
    assert isinstance(column.type, Date)
    assert not isinstance(column.type, DateTime)
    assert not column.nullable


def test_occurred_at_utc_exists_but_is_optional() -> None:
    """It is for ordering and dedupe only. Never for bucketing."""
    column = TABLE["transactions"].columns["occurred_at_utc"]
    assert isinstance(column.type, DateTime)
    assert column.nullable


def test_period_queries_have_an_index() -> None:
    indexed = {tuple(c.name for c in index.columns) for index in TABLE["transactions"].indexes}
    assert ("user_id", "occurred_on_local") in indexed


def test_the_merchant_carries_the_timezone() -> None:
    """Brief 16.2 puts merchant_tz on the merchant record, and here it is."""
    assert "merchant_tz" in column_names(TABLE["merchants"])
    assert not TABLE["merchants"].columns["merchant_tz"].nullable


def test_a_guessed_date_is_recorded_as_a_guess(seeded: tuple[Session, User]) -> None:
    """Brief 24.4: never silently guess without recording that you guessed."""
    session, user = seeded
    txn = Transaction(
        user_id=user.id,
        source=Source.TELEGRAM_IMAGE,
        grand_total_minor=21900,
        reconciliation_outcome=ReconciliationOutcome.BALANCED,
        occurred_on_local=dt.date(2026, 8, 8),
        date_source=DateSource.MESSAGE_TIMESTAMP,
    )
    session.add(txn)
    session.commit()
    assert fetched(session, Transaction, txn.id).date_source is DateSource.MESSAGE_TIMESTAMP


# ---------------------------------------------------------------------------
# Section 17: the five columns that must be present from the start
# ---------------------------------------------------------------------------


def test_the_five_required_columns_exist_from_the_first_migration() -> None:
    assert "user_id" in column_names(TABLE["transactions"])
    assert "occurred_on_local" in column_names(TABLE["transactions"])
    assert "merchant_tz" in column_names(TABLE["merchants"])
    assert "source" in column_names(TABLE["transactions"])
    assert "deleted_at" in column_names(TABLE["transactions"])


# ---------------------------------------------------------------------------
# Dedupe (brief 3.6 as revised by 24.4)
# ---------------------------------------------------------------------------


def test_the_same_image_cannot_land_twice(seeded: tuple[Session, User]) -> None:
    session, user = seeded

    def txn(sha: str) -> Transaction:
        return Transaction(
            user_id=user.id,
            source=Source.TELEGRAM_IMAGE,
            image_sha256=sha,
            grand_total_minor=1000,
            reconciliation_outcome=ReconciliationOutcome.BALANCED,
            occurred_on_local=dt.date(2026, 8, 8),
            date_source=DateSource.RECEIPT_PRINTED,
        )

    session.add(txn("a" * 64))
    session.commit()
    session.add(txn("a" * 64))
    with pytest.raises(IntegrityError):
        session.commit()


# ---------------------------------------------------------------------------
# Refunds (brief 16.8)
# ---------------------------------------------------------------------------


def test_a_refund_is_its_own_row_with_a_negative_total(seeded: tuple[Session, User]) -> None:
    """The original is never edited, so the append-only property survives."""
    session, user = seeded
    original = Transaction(
        user_id=user.id,
        source=Source.TELEGRAM_IMAGE,
        grand_total_minor=21900,
        reconciliation_outcome=ReconciliationOutcome.BALANCED,
        occurred_on_local=dt.date(2026, 8, 8),
        date_source=DateSource.RECEIPT_PRINTED,
    )
    session.add(original)
    session.commit()

    refund = Transaction(
        user_id=user.id,
        source=Source.TELEGRAM_TEXT,
        grand_total_minor=-5600,
        reconciliation_outcome=ReconciliationOutcome.SKIPPED,
        occurred_on_local=dt.date(2026, 8, 9),
        date_source=DateSource.MESSAGE_TIMESTAMP,
        related_transaction_id=original.id,
    )
    session.add(refund)
    session.commit()

    total = session.scalars(select(Transaction.grand_total_minor)).all()
    assert sum(total) == 21900 - 5600
    assert fetched(session, Transaction, original.id).grand_total_minor == 21900


def test_a_refund_cannot_point_at_a_transaction_that_does_not_exist(
    seeded: tuple[Session, User],
) -> None:
    session, user = seeded
    session.add(
        Transaction(
            user_id=user.id,
            source=Source.TELEGRAM_TEXT,
            grand_total_minor=-100,
            reconciliation_outcome=ReconciliationOutcome.SKIPPED,
            occurred_on_local=dt.date(2026, 8, 9),
            date_source=DateSource.MESSAGE_TIMESTAMP,
            related_transaction_id="does-not-exist",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_foreign_keys_are_actually_enforced(session: Session) -> None:
    """SQLite ignores foreign keys unless asked, once per connection."""
    assert foreign_keys_enforced(session.connection())


# ---------------------------------------------------------------------------
# Seeding (brief 3.8)
# ---------------------------------------------------------------------------


def test_bootstrap_creates_one_user_and_the_seed_categories(session: Session) -> None:
    user = bootstrap(session)
    session.commit()
    slugs = set(session.scalars(select(Category.slug)))
    assert slugs == {slug for slug, _ in SEED_CATEGORIES}
    assert session.scalars(select(Category.user_id)).all() == [user.id] * len(SEED_CATEGORIES)


def test_bootstrap_is_idempotent(session: Session) -> None:
    first = bootstrap(session)
    session.commit()
    second = bootstrap(session)
    session.commit()
    assert first.id == second.id
    assert len(session.scalars(select(Category)).all()) == len(SEED_CATEGORIES)


def test_the_taxonomy_is_only_what_was_actually_named() -> None:
    """Brief 3.8 corrected an earlier twelve-domain version. Do not regrow it."""
    assert [slug for slug, _ in SEED_CATEGORIES] == [
        "groceries",
        "food-and-dining",
        "drinks",
        "entertainment",
        "uncategorized",
    ]


def test_uncategorized_always_exists(seeded: tuple[Session, User]) -> None:
    session, user = seeded
    assert uncategorized(session, user.id).slug == UNCATEGORIZED_SLUG


def test_uncategorized_missing_is_an_error_not_a_none(session: Session) -> None:
    user = User()
    session.add(user)
    session.flush()
    with pytest.raises(LookupError, match=UNCATEGORIZED_SLUG):
        uncategorized(session, user.id)


def test_renaming_a_category_does_not_change_its_slug(seeded: tuple[Session, User]) -> None:
    """Slugs are stable, display names are mutable. Never key off the name."""
    session, user = seeded
    groceries = session.scalars(select(Category).where(Category.slug == "groceries")).one()
    groceries.display_name = "Kirana"
    session.commit()
    ensure_categories(session, user.id)
    session.commit()
    reloaded = session.scalars(select(Category).where(Category.slug == "groceries")).one()
    assert reloaded.display_name == "Kirana"
    assert len(session.scalars(select(Category)).all()) == len(SEED_CATEGORIES)


# ---------------------------------------------------------------------------
# Round trip: a real fixture in, the same numbers back out
# ---------------------------------------------------------------------------


def store(session: Session, user: User, extraction: ExtractionResult) -> Transaction:
    """Write an extraction to the ledger the way the confirm flow eventually will."""
    raw = RawExtraction(
        user_id=user.id,
        image_sha256="b" * 64,
        source=Source.TELEGRAM_IMAGE,
        model_id="gemini-3.6-flash",
        prompt_version="v1",
        response_json=extraction.model_dump_json(),
        input_tokens=2322,
        output_tokens=450,
        cost_micros_usd=7900,
    )
    session.add(raw)
    session.flush()

    txn = Transaction(
        user_id=user.id,
        raw_extraction_id=raw.id,
        source=Source.TELEGRAM_IMAGE,
        image_sha256=raw.image_sha256,
        currency=extraction.currency,
        grand_total_minor=extraction.grand_total_minor or 0,
        reconciliation_outcome=ReconciliationOutcome.BALANCED,
        occurred_on_local=dt.date(2026, 8, 8),
        date_source=DateSource.MESSAGE_TIMESTAMP,
    )
    session.add(txn)
    session.flush()

    for position, item in enumerate(extraction.line_items):
        session.add(
            TransactionLineItem(
                user_id=user.id,
                transaction_id=txn.id,
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
                    user_id=user.id,
                    transaction_id=txn.id,
                    position=position,
                    kind=kind,
                    label=row.label,
                    amount_minor=row.amount_minor,
                )
            )
            position += 1

    session.commit()
    return txn


def test_the_ledger_can_reproduce_its_own_total(
    seeded: tuple[Session, User], fixture_name: str
) -> None:
    """The stored parts must add up to the stored total, without reparsing JSON.

    This is why `transaction_adjustments` exists. If the charges and discounts
    only lived inside `raw_extractions.response_json`, every summary and
    `/export` would depend on a text blob.
    """
    session, user = seeded
    extraction = ExtractionResult.model_validate(as_extraction_payload(fixture_name))
    txn = store(session, user, extraction)

    lines = sum(
        session.scalars(
            select(TransactionLineItem.line_total_minor).where(
                TransactionLineItem.transaction_id == txn.id
            )
        )
    )
    adjustments = session.scalars(
        select(TransactionAdjustment).where(TransactionAdjustment.transaction_id == txn.id)
    ).all()
    charges = sum(a.amount_minor for a in adjustments if a.kind is AdjustmentKind.CHARGE)
    taxes = sum(a.amount_minor for a in adjustments if a.kind is AdjustmentKind.TAX)
    discounts = sum(a.amount_minor for a in adjustments if a.kind is AdjustmentKind.DISCOUNT)

    assert lines + charges + taxes - discounts == txn.grand_total_minor
    assert txn.grand_total_minor == computed_total(extraction)


def test_a_stored_extraction_still_reconciles(seeded: tuple[Session, User]) -> None:
    """`raw_extractions` keeps enough to re-run the gate without the image."""
    session, user = seeded
    extraction = ExtractionResult.model_validate(as_extraction_payload("blinkit_026"))
    txn = store(session, user, extraction)

    raw = fetched(session, RawExtraction, txn.raw_extraction_id)
    replayed = ExtractionResult.model_validate_json(raw.response_json)
    assert reconcile(replayed).outcome is Outcome.BALANCED
    assert replayed == extraction


# ---------------------------------------------------------------------------
# The migration must produce the same schema as the models
# ---------------------------------------------------------------------------


def test_the_migration_matches_the_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-edited migration that drifts from models.py is a silent data bug."""
    repo_root = Path(__file__).resolve().parent.parent
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    migrated = {name for name in inspector.get_table_names() if name != "alembic_version"}
    assert migrated == {table.name for table in TABLES}

    for table in TABLES:
        assert {c["name"] for c in inspector.get_columns(table.name)} == column_names(table), (
            f"{table.name} drifted between models.py and the migration"
        )
    engine.dispose()
