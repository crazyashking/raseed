"""The ledger schema.

Five properties are required to be present from the first migration rather than
added later (brief section 17, item 4): `user_id`, `occurred_on_local`,
`merchant_tz`, `source`, `deleted_at`. Every one of them is cheap now and a
painful migration later.

The shape of this file is governed by the invariants in `CLAUDE.md`:

- **Integer minor units.** Every money column is `Integer` and named `*_minor`.
  There is no `Float`, `Numeric` or `Decimal` anywhere, and a test enforces that
  by walking the metadata. Invariant 1.
- **Append-only with soft deletes.** Mutable tables carry `deleted_at`. Nothing
  is ever hard deleted, so a correction is a new row and history survives.
  Invariant 2.
- **`raw_extractions` is immutable.** It deliberately has no `updated_at` and no
  `deleted_at`, because it is never updated and never deleted. Every other table
  derives from it, which is what makes Stage 2 re-runnable for free when the
  taxonomy changes. Invariants 5 and 12.
- **No PII columns.** No address, phone, personal name, or card digits, in any
  table. A test walks every column name against a banned list. Invariant 3.
- **`user_id` everywhere**, even though there is exactly one user. Invariant 8.
- **Period queries bucket on `occurred_on_local`**, a DATE in the merchant's
  timezone, never on UTC and never on server time. `occurred_at_utc` exists for
  ordering and dedupe only. Invariant 6, brief section 16.2.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: The taxonomy from brief section 3.8, which is only what Ashrit actually named.
#: No sub-categories at launch. New ones get created from what accumulates in
#: `uncategorized`, not invented up front.
SEED_CATEGORIES: Final[tuple[tuple[str, str], ...]] = (
    ("groceries", "Groceries"),
    ("food-and-dining", "Food & Dining"),
    ("drinks", "Drinks"),
    ("entertainment", "Entertainment"),
    ("uncategorized", "Uncategorized"),
)

#: `uncategorized` always exists and is never a failure state (section 3.8).
UNCATEGORIZED_SLUG: Final[str] = "uncategorized"


def new_id() -> str:
    """A fresh primary key.

    UUID4 as text rather than an autoincrementing integer, because IDs travel in
    Telegram callback data and an integer would let one user guess another user's
    rows the moment this stops being single-user.
    """
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata."""


class Source(enum.Enum):
    """How a transaction entered the ledger.

    Stored so that a row's provenance survives, and so `/export` can say where
    each number came from.
    """

    TELEGRAM_IMAGE = "telegram_image"
    TELEGRAM_PDF = "telegram_pdf"
    TELEGRAM_TEXT = "telegram_text"


class DateSource(enum.Enum):
    """Where `occurred_on_local` came from.

    Brief section 24.4: the natural screenshot a user takes carries no date at
    all, so the date often has to be inferred from the Telegram message. Never
    silently guess without recording that you guessed. Anything on
    `MESSAGE_TIMESTAMP` is editable from the confirm keyboard.
    """

    RECEIPT_PRINTED = "receipt_printed"
    MESSAGE_TIMESTAMP = "message_timestamp"


class ReconciliationOutcome(enum.Enum):
    """The gate's verdict, persisted alongside the row it let through.

    Mirrors `raseed.validation.reconcile.Outcome`. Kept as its own enum so the
    database schema does not shift underneath stored rows if the gate's internal
    naming ever changes.
    """

    BALANCED = "balanced"
    SKIPPED = "skipped"
    CLASS_2 = "class_2_arithmetic_mismatch"


class AdjustmentKind(enum.Enum):
    """What a non-item line on the bill is.

    `DISCOUNT` is order level only: coupons, promo codes, wallet credits. A
    product discount already inside a line price is never recorded here.
    Invariant 13.
    """

    CHARGE = "charge"
    TAX = "tax"
    DISCOUNT = "discount"


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    """One human.

    Deliberately holds no Telegram ID, no name, no handle. Access control lives
    in `TELEGRAM_ALLOWED_USER_IDS` in the environment, so no external account
    identifier ever lands in the database. When multi-user arrives, that mapping
    becomes its own table and this one does not change.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[dt.datetime] = _created_at()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Merchant(Base):
    """A shop. A business, never a person.

    `merchant_tz` is the reason this table exists at all: period bucketing runs
    on the merchant's local calendar date, so the timezone has to live somewhere
    durable (brief section 16.2).
    """

    __tablename__ = "merchants"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_merchants_user_slug"),
        Index("ix_merchants_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    #: Stable identifier. Never key off `display_name`, which is editable.
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    #: IANA name, for example "Asia/Kolkata". Not an offset, because offsets
    #: change and IANA names do not.
    merchant_tz: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")

    created_at: Mapped[dt.datetime] = _created_at()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Category(Base):
    """A spend category.

    Slugs are stable and display names are mutable, so nothing downstream may key
    off the display name (brief section 3.8).
    """

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_categories_user_slug"),
        Index("ix_categories_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: True for the seeded set. `uncategorized` in particular must never be
    #: deleted, because it is where every Stage 2 miss lands.
    is_system: Mapped[bool] = mapped_column(nullable=False, default=False)

    created_at: Mapped[dt.datetime] = _created_at()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class RawExtraction(Base):
    """What the model returned, verbatim, forever.

    IMMUTABLE. Never UPDATE, never DELETE, which is why this class has no
    `updated_at` and no `deleted_at`. Invariant 5.

    Every other table derives from this one. That is what makes it free to re-run
    Stage 2 across the whole history when the taxonomy changes in month four,
    rather than needing the images back.
    """

    __tablename__ = "raw_extractions"
    __table_args__ = (Index("ix_raw_extractions_user_hash", "user_id", "image_sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    #: SHA-256 of the raw image bytes, the primary dedupe key (brief 3.6 as
    #: revised by 24.4). Null for text entry, which has no image.
    image_sha256: Mapped[str | None] = mapped_column(String(64))

    source: Mapped[Source] = mapped_column(Enum(Source, native_enum=False), nullable=False)

    #: Exactly which model and prompt produced this, so a regression is
    #: attributable rather than mysterious.
    model_id: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)

    #: The provider's response as returned. Text, not a parsed structure, so it
    #: survives a schema change in this repo.
    response_json: Mapped[str] = mapped_column(Text, nullable=False)

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)

    #: Cost in MICRO US dollars, integer. A receipt costs roughly 7,900 of these,
    #: so cents would round the whole thing to zero and a float would violate
    #: invariant 1. Micros keep it exact and keep the daily cost cap honest.
    cost_micros_usd: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[dt.datetime] = _created_at()


class Transaction(Base):
    """One purchase, or one refund.

    A refund is its own row with a negative `grand_total_minor` and
    `related_transaction_id` pointing at the original (brief 16.8). The original
    is never edited, because editing it would break the append-only property and
    lose the fact that a refund happened. Category totals then net out with no
    special-case logic.

    That is also why there is no non-negativity constraint on the money columns
    here, unlike the extraction schema: a negative total is meaningful in a
    ledger and impossible on a printed receipt.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        #: Dedupe level 1: the same image never lands twice **while it is live**.
        #:
        #: Partial, and that is load-bearing. A plain UNIQUE over all rows
        #: contradicted `queries.find_by_image_hash`, which deliberately ignores
        #: soft-deleted matches so that resending a receipt is how you undo an
        #: `/undo`. With the constraint covering deleted rows too, the dedupe
        #: check said "not a duplicate", extraction ran and was paid for, and
        #: then the INSERT died on the constraint. It crashed the live bot twice
        #: on 2026-08-08. See docs/DECISIONS.md.
        #:
        #: Invariant 2 keeps soft-deleted rows forever, so the exclusion is what
        #: makes deletion reversible rather than permanent.
        Index(
            "uq_transactions_user_image",
            "user_id",
            "image_sha256",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        #: Every period query runs off this pair. Invariant 6.
        Index("ix_transactions_user_local_date", "user_id", "occurred_on_local"),
        Index("ix_transactions_user_merchant", "user_id", "merchant_id"),
        CheckConstraint("currency = upper(currency)", name="ck_transactions_currency_upper"),
        CheckConstraint("length(currency) = 3", name="ck_transactions_currency_len"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    #: Null for manual text entry, which never went through a model (brief 3.9).
    raw_extraction_id: Mapped[str | None] = mapped_column(ForeignKey("raw_extractions.id"))

    #: Null is normal. The screenshot a user actually takes prints no merchant
    #: name, and inferring one from app styling is not reliable (brief 24.4).
    merchant_id: Mapped[str | None] = mapped_column(ForeignKey("merchants.id"))

    source: Mapped[Source] = mapped_column(Enum(Source, native_enum=False), nullable=False)

    #: Repeated from the extraction so dedupe does not need a join.
    image_sha256: Mapped[str | None] = mapped_column(String(64))

    #: Printed on the receipt, if it printed one. Not a natural key on its own.
    order_id: Mapped[str | None] = mapped_column(String(120))

    # --- Money. Integer minor units, always. Invariant 1. --------------------
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    grand_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The gap the reconciliation gate could not account for, from brief 3.3.
    #: Zero on a receipt that balanced. If this is populated on 30% of one
    #: merchant's receipts, the prompt has a specific bug and this says where.
    unaccounted_adjustment_minor: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    reconciliation_outcome: Mapped[ReconciliationOutcome] = mapped_column(
        Enum(ReconciliationOutcome, native_enum=False), nullable=False
    )

    # --- Time. See brief 16.2 and invariant 6. -------------------------------
    #: For ordering and dedupe only. Never for bucketing.
    occurred_at_utc: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    #: The merchant-local calendar date. THIS is what every monthly, weekly and
    #: quarterly query buckets on. Not null, because a transaction with no
    #: bucket cannot appear in any summary.
    occurred_on_local: Mapped[dt.date] = mapped_column(Date, nullable=False)

    #: Whether the date above was read off the receipt or inferred from the
    #: message. A guess is recorded as a guess, never silently.
    date_source: Mapped[DateSource] = mapped_column(
        Enum(DateSource, native_enum=False), nullable=False
    )

    #: Points at the original purchase when this row is a refund (brief 16.8).
    related_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))

    created_at: Mapped[dt.datetime] = _created_at()

    #: Soft delete. Never a hard delete, so the append-only property survives
    #: `/undo` and a mistaken confirm. Invariant 2, brief 16.3.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    line_items: Mapped[list[TransactionLineItem]] = relationship(
        back_populates="transaction", cascade="save-update, merge"
    )
    adjustments: Mapped[list[TransactionAdjustment]] = relationship(
        back_populates="transaction", cascade="save-update, merge"
    )


class TransactionLineItem(Base):
    """One purchased item on one transaction.

    `raw_name` is preserved exactly as printed and is never overwritten, because
    it is what keeps item canonicalization solvable later without the image
    (brief 3.7). The normalized columns are filled by Stage 2 and are null until
    then.
    """

    __tablename__ = "transaction_line_items"
    __table_args__ = (
        UniqueConstraint("transaction_id", "position", name="uq_line_items_txn_position"),
        Index("ix_line_items_user_category", "user_id", "category_id"),
        CheckConstraint("mrp_minor IS NULL OR mrp_minor >= 0", name="ck_line_items_mrp_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False)

    #: Order as printed on the receipt, zero based.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- Stage 1: verbatim. Never edited. ------------------------------------
    raw_name: Mapped[str] = mapped_column(String(400), nullable=False)
    quantity_text: Mapped[str | None] = mapped_column(String(120))
    mrp_minor: Mapped[int | None] = mapped_column(Integer)
    line_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- Stage 2: derived from raw_name, null until enrichment runs. ---------
    #: Brief 16.7. Parsed deterministically with a regex table, not by a model.
    normalized_slug: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(16))
    unit_normalized: Mapped[str | None] = mapped_column(String(16))
    pack_count: Mapped[int | None] = mapped_column(Integer)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))

    created_at: Mapped[dt.datetime] = _created_at()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    transaction: Mapped[Transaction] = relationship(back_populates="line_items")


class TransactionAdjustment(Base):
    """A charge, a tax, or an order-level discount on one transaction.

    Without this table the ledger could not reproduce its own grand total
    without reparsing `raw_extractions.response_json`, which would make `/export`
    and every summary depend on a text blob.

    Amounts are stored as positive magnitudes and the `kind` carries the sign,
    exactly as in the extraction contract. A `DISCOUNT` row is order level only.
    Invariant 13.
    """

    __tablename__ = "transaction_adjustments"
    __table_args__ = (
        UniqueConstraint("transaction_id", "position", name="uq_adjustments_txn_position"),
        Index("ix_adjustments_user_kind", "user_id", "kind"),
        CheckConstraint("amount_minor >= 0", name="ck_adjustments_amount_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False)

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[AdjustmentKind] = mapped_column(
        Enum(AdjustmentKind, native_enum=False), nullable=False
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[dt.datetime] = _created_at()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    transaction: Mapped[Transaction] = relationship(back_populates="adjustments")


#: Tables that may be soft deleted. `raw_extractions` is absent on purpose:
#: it is immutable, so there is nothing to mark. Invariant 5.
SOFT_DELETABLE_TABLES: Final[tuple[str, ...]] = (
    "users",
    "merchants",
    "categories",
    "transactions",
    "transaction_line_items",
    "transaction_adjustments",
)

__all__ = [
    "SEED_CATEGORIES",
    "SOFT_DELETABLE_TABLES",
    "UNCATEGORIZED_SLUG",
    "AdjustmentKind",
    "Base",
    "Category",
    "DateSource",
    "Merchant",
    "RawExtraction",
    "ReconciliationOutcome",
    "Source",
    "Transaction",
    "TransactionAdjustment",
    "TransactionLineItem",
    "User",
    "new_id",
]
