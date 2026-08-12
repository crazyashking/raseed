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

    `USER_SUPPLIED` is the third value, added 2026-08-12 when that edit was
    actually built. A person confirming a receipt they photographed a week late
    is told the date could not be read and asked for it, and the answer they
    give is neither printed nor guessed. Recording it as `MESSAGE_TIMESTAMP`
    would mark a date the user typed as inferred, and the dashboard flags
    inferred dates.
    """

    RECEIPT_PRINTED = "receipt_printed"
    MESSAGE_TIMESTAMP = "message_timestamp"
    USER_SUPPLIED = "user_supplied"


class ExtractionStage(enum.Enum):
    """Which stage of the pipeline paid for a `raw_extractions` row.

    Stage 2's model fallback is a billed API call like any other, and brief 16.6's
    daily cap is computed by summing `cost_micros_usd` over this table. Recording
    categorization calls anywhere else, or nowhere, would make that cap quietly
    under-count the moment the fallback starts firing.

    Stage 1 rows are what a transaction derives from. Stage 2 rows are billing
    and provenance only: nothing has a foreign key to one.
    """

    EXTRACTION = "extraction"
    CATEGORIZATION = "categorization"


class CategorySource(enum.Enum):
    """How a line item's category was decided. Brief 18.5.

    Mirrors `raseed.enrichment.lexicon.Source` and is kept separate for the same
    reason as `ReconciliationOutcome`: the stored values must not shift because
    an in-memory enum got renamed. A test pins the two together.

    This column is what makes a future lexicon improvement re-runnable against
    only the weak rows. Without it, improving the lexicon means re-categorizing
    a year of receipts or nothing.
    """

    LEXICON_EXACT = "lexicon_exact"
    LEXICON_FUZZY = "lexicon_fuzzy"
    LLM = "llm"
    MANUAL = "manual"


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

    #: Which stage paid for this row. Defaults to extraction so every row written
    #: before Stage 2 existed keeps meaning what it meant.
    stage: Mapped[ExtractionStage] = mapped_column(
        Enum(ExtractionStage, native_enum=False),
        nullable=False,
        default=ExtractionStage.EXTRACTION,
        server_default=ExtractionStage.EXTRACTION.name,
    )

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


class PendingReceiptRow(Base):
    """A receipt shown to the user and waiting on a Confirm or Discard.

    Nothing here is in the ledger. This is the queue in front of it, and it
    exists on disk for one reason: it used to live in memory, so restarting the
    bot invalidated every outstanding confirm button while the buttons stayed on
    screen. A receipt that had already been read and paid for could then only be
    resent and paid for again. That cost three real receipts on 2026-08-09.

    Almost nothing is stored here, because almost nothing needs to be.
    `raw_extractions` already holds the model's response verbatim and is
    immutable, and the reconciliation and MRP checks are pure functions of it.
    So this row carries the decisions a human made (a merchant pick, an accepted
    gap) and a pointer, and the rest is recomputed on read for free.

    **There is no chat identifier here.** `key_hash` is an HMAC of the Telegram
    chat and message IDs under `USER_ID_SECRET`, the same construction
    `identity.py` uses for `user_id`. The bot always has the real key in hand
    when it looks a row up, because the key arrives in the callback data, so
    nothing is lost by never storing it. A stolen database still cannot be turned
    back into a list of accounts, which is the property W3 bought and this table
    must not sell back.
    """

    __tablename__ = "pending_receipts"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_pending_receipts_key"),
        Index("ix_pending_receipts_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    #: HMAC-SHA256 of the callback key. Never the key itself. See the docstring.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    raw_extraction_id: Mapped[str] = mapped_column(ForeignKey("raw_extractions.id"), nullable=False)

    #: Bucketed on later, so it is resolved once here rather than re-guessed.
    #: Invariant 6.
    occurred_on_local: Mapped[dt.date] = mapped_column(Date, nullable=False)
    date_source: Mapped[DateSource] = mapped_column(
        Enum(DateSource, native_enum=False), nullable=False
    )

    #: Which receipt of the batch this row is, counting from zero.
    #:
    #: One call over several images can return several receipts, and they all
    #: point at the same immutable `raw_extractions` row. This is what says
    #: which one to read back out of it.
    receipt_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: Deleted on confirm or discard. Invariant 7. Null for text entry.
    #:
    #: A JSON array, because a receipt too long for one photograph arrives as
    #: several and every one of them has to be deleted. Held per pending receipt
    #: rather than per batch: deleting is idempotent, so two receipts out of one
    #: batch both listing the same files costs nothing and leaves nothing behind
    #: if only one of them is ever answered.
    image_paths: Mapped[str | None] = mapped_column(String(2000))

    #: Choices the user already made, which a restart must not throw away.
    merchant_slug: Mapped[str | None] = mapped_column(String(80))
    gap_accepted: Mapped[bool] = mapped_column(nullable=False, default=False)

    created_at: Mapped[dt.datetime] = _created_at()
    #: Soft, like every table except `raw_extractions`. A pending receipt is not
    #: ledger data, but invariant 2 is stated without exceptions and one column
    #: is a cheaper price than an exception.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


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
    #:
    #: The batch's own digest for the first receipt read out of it, and a digest
    #: derived from the batch and the position for every one after. The unique
    #: index above is why: two receipts out of one batch would otherwise collide
    #: on it. See `adapters.images.receipt_sha256`.
    image_sha256: Mapped[str | None] = mapped_column(String(64))

    #: Which receipt of the batch this is, counting from zero. Zero for every
    #: row written before 2026-08-12, and for every batch holding one receipt.
    receipt_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

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
        CheckConstraint(
            "category_confidence_bp IS NULL OR "
            "(category_confidence_bp >= 0 AND category_confidence_bp <= 10000)",
            name="ck_line_items_confidence_range",
        ),
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
    #: The printed name with the quantity stripped and slugified:
    #: `Amul Taaza Toned Milk 500ml` becomes `amul-taaza-toned-milk`.
    normalized_slug: Mapped[str | None] = mapped_column(String(200))

    #: The lexicon's canonical name for what this actually is: `milk`, `okra`,
    #: `mung_bean`. Distinct from `normalized_slug` on purpose. That one keeps
    #: the brand and cannot tell you `Bhindi 500g` and `Okra 500g` are the same
    #: vegetable; this one is exactly that answer, and it is what brief 3.7's
    #: item canonicalization is built on. Null when the lexicon did not know it.
    canonical_slug: Mapped[str | None] = mapped_column(String(120))

    #: In `unit_normalized` units, integer. `1kg` is stored as 1000 with
    #: `unit` "kg" and `unit_normalized` "g", so price-per-unit comparisons work
    #: without a float anywhere. See `enrichment.normalize`.
    quantity: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(16))
    unit_normalized: Mapped[str | None] = mapped_column(String(16))

    #: `2 x 500ml` is pack_count 2 and quantity 500, never quantity 1000. A
    #: two-pack and a one-litre bottle are different products at different
    #: prices. Null means the name said nothing about packs.
    pack_count: Mapped[int | None] = mapped_column(Integer)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"))

    #: Brief 18.5. Null means Stage 2 has not run on this row yet, which is a
    #: different fact from "it ran and landed on uncategorized".
    category_source: Mapped[CategorySource | None] = mapped_column(
        Enum(CategorySource, native_enum=False)
    )

    #: Confidence in BASIS POINTS, integer, 0 to 10000. Brief 18.5 asks for a
    #: float here. It does not get one: the no-floating-point test walks every
    #: column of every table, not only the money ones, and carving an exception
    #: into that test to hold a similarity score is a bad trade. Basis points
    #: keep four significant digits, which is more than rapidfuzz's scores
    #: meaningfully carry, and 7826 reads as plainly as 78.26.
    #:
    #: Null on the exact path, where there is nothing to be uncertain about.
    category_confidence_bp: Mapped[int | None] = mapped_column(Integer)

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


class LexiconMiss(Base):
    """A product word the lexicon did not know. Brief 4.5.

    The brief is blunt that its own "300 to 400 terms" estimate was invented and
    should be deleted from thinking, and that the real answer comes out of this
    table after a month of real receipts. So this is not diagnostics: it is the
    mechanism by which the lexicon grows from actual spending.

    One row per occurrence rather than a counter, so the weekly review is
    ``GROUP BY term ORDER BY count(*) DESC`` and nothing here ever needs an
    UPDATE. `lexicon_version` is stored so a miss recorded against version 1 is
    distinguishable from one that survived version 2, which is what makes
    "did adding those terms help" answerable instead of a feeling.

    `deleted_at` is how a term gets dismissed: a brand name or a flavour word is
    never going in the YAML, and soft-deleting it keeps it out of next week's
    list without losing the fact that it was seen.
    """

    __tablename__ = "lexicon_misses"
    __table_args__ = (
        Index("ix_lexicon_misses_user_term", "user_id", "term"),
        Index("ix_lexicon_misses_version", "lexicon_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)

    #: The unmatched token or n-gram, lowercased, exactly as `tokenize` produced
    #: it. This is what gets pasted into the YAML if it turns out to be real.
    term: Mapped[str] = mapped_column(String(200), nullable=False)

    #: The whole printed product name the token came from. Without it a bare
    #: `kaju` is ambiguous between the nut and a sweet, and the review is
    #: guesswork again. Product text, not PII.
    raw_name: Mapped[str] = mapped_column(String(400), nullable=False)

    #: Which lexicon this missed against, and at what version.
    lexicon_name: Mapped[str] = mapped_column(String(80), nullable=False)
    lexicon_version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Nullable because the backfill tool re-runs Stage 2 over stored extractions
    #: whose line items may since have been soft deleted.
    line_item_id: Mapped[str | None] = mapped_column(ForeignKey("transaction_line_items.id"))

    created_at: Mapped[dt.datetime] = _created_at()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


#: Tables that may be soft deleted. `raw_extractions` is absent on purpose:
#: it is immutable, so there is nothing to mark. Invariant 5.
SOFT_DELETABLE_TABLES: Final[tuple[str, ...]] = (
    "users",
    "pending_receipts",
    "merchants",
    "categories",
    "transactions",
    "transaction_line_items",
    "transaction_adjustments",
    "lexicon_misses",
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
    "PendingReceiptRow",
    "RawExtraction",
    "ReconciliationOutcome",
    "Source",
    "Transaction",
    "TransactionAdjustment",
    "TransactionLineItem",
    "User",
    "new_id",
]
