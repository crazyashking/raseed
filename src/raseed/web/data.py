"""Everything the dashboard reads, and nothing else.

Two rules hold throughout, and they are the same two that govern `db/queries.py`:

- **Every period bucket is on `occurred_on_local`.** Never UTC, never server
  time, never `created_at`. Invariant 6. A receipt bought on 31 July and
  photographed on 2 August belongs to July.
- **Soft-deleted rows are excluded everywhere.** A caller who wants them has to
  ask, which is the right way round.

Bucketing happens in Python rather than in SQL. `strftime('%Y-%m', ...)` would be
shorter but it is SQLite-specific, and at one user's volume the difference is
unmeasurable. Correctness that survives a database change is worth more than a
query that saves a millisecond.

Nothing here writes. The dashboard is read-only by construction, so no route can
mutate the ledger even if a handler is wrong.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from raseed.db.models import (
    AdjustmentKind,
    Category,
    RawExtraction,
    ReconciliationOutcome,
    Transaction,
    TransactionAdjustment,
    TransactionLineItem,
)

#: What a line item with no category is called. Enrichment (commit 9) is what
#: fills `category_id`; until it exists every line lands here, and the dashboard
#: says so plainly rather than drawing an empty chart.
UNCATEGORIZED: str = "Uncategorized"


@dataclass(frozen=True, slots=True)
class Bucket:
    """One period and what was spent in it."""

    label: str
    total_minor: int
    count: int


@dataclass(frozen=True, slots=True)
class Slice:
    """One category's share of a period."""

    name: str
    total_minor: int
    share: float


@dataclass(frozen=True, slots=True)
class Row:
    """One transaction, flattened for a table."""

    id: str
    occurred_on_local: dt.date
    grand_total_minor: int
    currency: str
    outcome: ReconciliationOutcome
    unaccounted_adjustment_minor: int
    date_source: str
    item_count: int

    @property
    def flagged(self) -> bool:
        """Whether this row needs a human to look at it."""
        return self.outcome is ReconciliationOutcome.CLASS_2


@dataclass(frozen=True, slots=True)
class Overview:
    """The numbers at the top of the page."""

    period_label: str
    total_minor: int
    receipt_count: int
    previous_total_minor: int
    average_minor: int
    flagged_count: int
    api_spend_micros: int

    @property
    def delta_minor(self) -> int:
        return self.total_minor - self.previous_total_minor

    @property
    def delta_pct(self) -> float | None:
        """None when there is no previous period to compare against.

        Returning None rather than 0 or 100 keeps "no data" distinguishable from
        "no change", which a spend figure has no business blurring.
        """
        if self.previous_total_minor == 0:
            return None
        return (self.delta_minor / self.previous_total_minor) * 100


@dataclass(frozen=True, slots=True)
class LineDetail:
    """One line on one receipt."""

    position: int
    raw_name: str
    quantity_text: str | None
    mrp_minor: int | None
    line_total_minor: int
    category: str

    @property
    def saved_minor(self) -> int | None:
        """MRP minus paid, when an MRP was printed. Brief 24.3."""
        if self.mrp_minor is None:
            return None
        return self.mrp_minor - self.line_total_minor


@dataclass(frozen=True, slots=True)
class AdjustmentDetail:
    """One charge, tax or discount on one receipt."""

    kind: AdjustmentKind
    label: str
    amount_minor: int


@dataclass(slots=True)
class Receipt:
    """One transaction with everything hanging off it."""

    row: Row
    lines: list[LineDetail] = field(default_factory=list)
    adjustments: list[AdjustmentDetail] = field(default_factory=list)
    order_id: str | None = None
    source: str = ""

    @property
    def line_subtotal_minor(self) -> int:
        return sum(line.line_total_minor for line in self.lines)

    def total_for(self, kind: AdjustmentKind) -> int:
        return sum(a.amount_minor for a in self.adjustments if a.kind is kind)


def month_label(day: dt.date) -> str:
    return day.strftime("%B %Y")


def month_key(day: dt.date) -> str:
    return day.strftime("%Y-%m")


def month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    """First and last day of the month `day` falls in, inclusive."""
    start = day.replace(day=1)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start, next_start - dt.timedelta(days=1)


def previous_month(day: dt.date) -> dt.date:
    """Any day in the month before the one `day` falls in."""
    return day.replace(day=1) - dt.timedelta(days=1)


def _live(user_id: str) -> ColumnElement[bool]:
    """The filter every query shares: this user, not soft-deleted."""
    return and_(Transaction.user_id == user_id, Transaction.deleted_at.is_(None))


def _rows(session: Session, *, user_id: str) -> list[Row]:
    """Every live transaction with its line count, newest first."""
    transactions = list(
        session.scalars(
            select(Transaction)
            .where(_live(user_id))
            .order_by(Transaction.occurred_on_local.desc(), Transaction.created_at.desc())
        )
    )
    if not transactions:
        return []

    counts: dict[str, int] = defaultdict(int)
    for txn_id in session.scalars(
        select(TransactionLineItem.transaction_id).where(
            TransactionLineItem.user_id == user_id,
            TransactionLineItem.deleted_at.is_(None),
        )
    ):
        counts[txn_id] += 1

    return [
        Row(
            id=t.id,
            occurred_on_local=t.occurred_on_local,
            grand_total_minor=t.grand_total_minor,
            currency=t.currency,
            outcome=t.reconciliation_outcome,
            unaccounted_adjustment_minor=t.unaccounted_adjustment_minor,
            date_source=t.date_source.value,
            item_count=counts[t.id],
        )
        for t in transactions
    ]


def monthly_totals(session: Session, *, user_id: str, months: int, today: dt.date) -> list[Bucket]:
    """The last `months` calendar months, oldest first, gaps included as zero.

    A month with no receipts is a real fact about the data and gets a bar of
    zero rather than being dropped, which would make the chart lie about time.
    """
    by_month: dict[str, tuple[int, int]] = {}
    for row in _rows(session, user_id=user_id):
        key = month_key(row.occurred_on_local)
        total, count = by_month.get(key, (0, 0))
        by_month[key] = (total + row.grand_total_minor, count + 1)

    buckets: list[Bucket] = []
    cursor = today.replace(day=1)
    for _ in range(months):
        total, count = by_month.get(month_key(cursor), (0, 0))
        buckets.append(Bucket(label=cursor.strftime("%b"), total_minor=total, count=count))
        cursor = previous_month(cursor).replace(day=1)
    return list(reversed(buckets))


def category_totals(session: Session, *, user_id: str, start: dt.date, end: dt.date) -> list[Slice]:
    """Spend by category across a date range, inclusive on both ends.

    Categories live on line items, not on transactions, because one grocery
    order is several categories. That means this sums line totals and therefore
    excludes charges, taxes and order-level discounts: the slices add up to the
    basket, not to the grand total. The page says so rather than quietly
    presenting a number that does not reconcile.
    """
    names = {
        c.id: c.display_name
        for c in session.scalars(
            select(Category).where(Category.user_id == user_id, Category.deleted_at.is_(None))
        )
    }

    totals: dict[str, int] = defaultdict(int)
    rows = session.execute(
        select(TransactionLineItem.category_id, TransactionLineItem.line_total_minor)
        .join(Transaction, Transaction.id == TransactionLineItem.transaction_id)
        .where(
            _live(user_id),
            TransactionLineItem.deleted_at.is_(None),
            Transaction.occurred_on_local >= start,
            Transaction.occurred_on_local <= end,
        )
    )
    for category_id, amount in rows:
        totals[names.get(category_id, UNCATEGORIZED) if category_id else UNCATEGORIZED] += amount

    grand = sum(totals.values())
    return [
        Slice(name=name, total_minor=amount, share=(amount / grand) if grand else 0.0)
        for name, amount in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


def overview(session: Session, *, user_id: str, today: dt.date) -> Overview:
    """The headline figures for the month `today` falls in."""
    start, end = month_bounds(today)
    prev_start, prev_end = month_bounds(previous_month(today))

    rows = _rows(session, user_id=user_id)
    current = [r for r in rows if start <= r.occurred_on_local <= end]
    previous = [r for r in rows if prev_start <= r.occurred_on_local <= prev_end]

    total = sum(r.grand_total_minor for r in current)
    return Overview(
        period_label=month_label(today),
        total_minor=total,
        receipt_count=len(current),
        previous_total_minor=sum(r.grand_total_minor for r in previous),
        average_minor=round(total / len(current)) if current else 0,
        flagged_count=sum(1 for r in rows if r.flagged),
        api_spend_micros=api_spend_micros(session, user_id=user_id),
    )


def api_spend_micros(session: Session, *, user_id: str) -> int:
    """Everything the extraction provider has ever cost, in micro-dollars.

    Deliberately not bucketed on `occurred_on_local`. This measures when tokens
    were billed, not when money was spent at a merchant, so invariant 6 does not
    apply. See the same note on `db.queries.spend_micros_since`.
    """
    total = 0
    for cost in session.scalars(
        select(RawExtraction.cost_micros_usd).where(RawExtraction.user_id == user_id)
    ):
        total += cost or 0
    return total


def recent(session: Session, *, user_id: str, limit: int = 25) -> list[Row]:
    return _rows(session, user_id=user_id)[:limit]


def flagged(session: Session, *, user_id: str) -> list[Row]:
    """Rows the gate could not reconcile. Brief 3.3."""
    return [r for r in _rows(session, user_id=user_id) if r.flagged]


def receipt(session: Session, *, user_id: str, transaction_id: str) -> Receipt | None:
    """One transaction with its lines and adjustments, or None if it is not yours.

    The `user_id` filter is a security boundary, not an optimisation: a guessed
    transaction ID must not return another user's receipt. Invariant 8 is why
    every table carries `user_id` even while there is one user.
    """
    txn = session.scalars(
        select(Transaction).where(_live(user_id), Transaction.id == transaction_id).limit(1)
    ).first()
    if txn is None:
        return None

    names = {
        c.id: c.display_name
        for c in session.scalars(
            select(Category).where(Category.user_id == user_id, Category.deleted_at.is_(None))
        )
    }

    lines = [
        LineDetail(
            position=item.position,
            raw_name=item.raw_name,
            quantity_text=item.quantity_text,
            mrp_minor=item.mrp_minor,
            line_total_minor=item.line_total_minor,
            category=names.get(item.category_id, UNCATEGORIZED)
            if item.category_id
            else UNCATEGORIZED,
        )
        for item in session.scalars(
            select(TransactionLineItem)
            .where(
                TransactionLineItem.transaction_id == txn.id,
                TransactionLineItem.deleted_at.is_(None),
            )
            .order_by(TransactionLineItem.position)
        )
    ]

    adjustments = [
        AdjustmentDetail(kind=a.kind, label=a.label, amount_minor=a.amount_minor)
        for a in session.scalars(
            select(TransactionAdjustment)
            .where(
                TransactionAdjustment.transaction_id == txn.id,
                TransactionAdjustment.deleted_at.is_(None),
            )
            .order_by(TransactionAdjustment.position)
        )
    ]

    return Receipt(
        row=Row(
            id=txn.id,
            occurred_on_local=txn.occurred_on_local,
            grand_total_minor=txn.grand_total_minor,
            currency=txn.currency,
            outcome=txn.reconciliation_outcome,
            unaccounted_adjustment_minor=txn.unaccounted_adjustment_minor,
            date_source=txn.date_source.value,
            item_count=len(lines),
        ),
        lines=lines,
        adjustments=adjustments,
        order_id=txn.order_id,
        source=txn.source.value,
    )


__all__ = [
    "UNCATEGORIZED",
    "AdjustmentDetail",
    "Bucket",
    "LineDetail",
    "Overview",
    "Receipt",
    "Row",
    "Slice",
    "api_spend_micros",
    "category_totals",
    "flagged",
    "month_bounds",
    "month_key",
    "month_label",
    "monthly_totals",
    "overview",
    "previous_month",
    "receipt",
    "recent",
]
