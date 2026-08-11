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

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from raseed.db.models import (
    SEED_CATEGORIES,
    UNCATEGORIZED_SLUG,
    AdjustmentKind,
    Category,
    RawExtraction,
    ReconciliationOutcome,
    Transaction,
    TransactionAdjustment,
    TransactionLineItem,
)
from raseed.money import apportion, average

#: What a line item with no category is called. Enrichment (commit 9) fills
#: `category_id`, so a live line lands here only when it was stored before that
#: commit or when Stage 2 genuinely could not place it. Brief 3.8 is explicit
#: that this is a real category and not a failure state, which is why it gets a
#: tab of its own like any other.
UNCATEGORIZED: str = "Uncategorized"


@dataclass(frozen=True, slots=True)
class Bucket:
    """One period and what was spent in it."""

    label: str
    total_minor: int
    count: int
    #: Never summed across currencies. See `currencies`.
    currency: str = "INR"


@dataclass(frozen=True, slots=True)
class Slice:
    """One category's, or one item's, share of a period."""

    name: str
    total_minor: int
    share: float
    #: How many line items rolled into this. Only meaningful for item slices,
    #: where "bought 4 times" is the interesting part.
    count: int = 0
    #: Never summed across currencies. See `currencies`.
    currency: str = "INR"


@dataclass(frozen=True, slots=True)
class Tab:
    """One entry in the top nav. Brief section 2.

    `slug` is the identity, never `name`: brief 3.8 makes display names mutable
    and says nothing downstream may key off them. `None` is the All tab.
    """

    slug: str | None
    name: str
    total_minor: int
    #: Never summed across currencies. See `currencies`.
    currency: str = "INR"

    @property
    def empty(self) -> bool:
        """Nothing in this domain this period.

        Rendered greyed out rather than hidden, so the tab order does not
        reshuffle from month to month (brief section 2).
        """
        return self.total_minor == 0


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
    #: What this receipt contributed to the *selected category*, when one is
    #: selected. None on the All tab, where the grand total is the right figure.
    category_minor: int | None = None

    @property
    def flagged(self) -> bool:
        """Whether this row needs a human to look at it."""
        return self.outcome is ReconciliationOutcome.CLASS_2

    @property
    def amount_minor(self) -> int:
        """The figure to show and to total.

        On a category tab this is the part of the receipt that belongs to that
        category, apportioned so that every tab's figures sum back to the grand
        total. See `line_shares`. Summing grand totals under a category filter
        instead would count delivery and tax once per category and report more
        money than ever left the account.
        """
        return self.grand_total_minor if self.category_minor is None else self.category_minor


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
    #: What every figure above is denominated in. Never summed across
    #: currencies: see `currencies` for why the page renders one of these per
    #: currency instead of adding paise to cents.
    currency: str = "INR"

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
class CurrencyView:
    """One currency's worth of the page: the hero, its chart and its breakdown.

    A user with receipts in one currency has exactly one of these and the page
    looks as it always did. A user who came back from a trip has two, stacked,
    with nothing added across them.
    """

    overview: Overview
    buckets: list[Bucket]
    slices: list[Slice]

    @property
    def currency(self) -> str:
        return self.overview.currency


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


def _category_filter(session: Session, *, user_id: str, slug: str) -> ColumnElement[bool] | None:
    """Match line items in one category, or None if that slug does not exist.

    `uncategorized` is two things at once and has to match both: the seeded
    category row, and every line item whose `category_id` is still NULL because
    enrichment has not run over it. `category_totals` already folds NULL into
    Uncategorized for display, and the tab has to agree with the number the tab
    itself shows.
    """
    category_id = session.scalars(
        select(Category.id)
        .where(
            Category.user_id == user_id,
            Category.slug == slug,
            Category.deleted_at.is_(None),
        )
        .limit(1)
    ).first()

    if slug == UNCATEGORIZED_SLUG:
        if category_id is None:
            return TransactionLineItem.category_id.is_(None)
        return or_(
            TransactionLineItem.category_id == category_id,
            TransactionLineItem.category_id.is_(None),
        )
    if category_id is None:
        return None
    return TransactionLineItem.category_id == category_id


def line_shares(session: Session, *, user_id: str) -> dict[str, int]:
    """Each live line item's share of what its receipt actually cost.

    Categories hang off line items, and the money that left the account also
    includes delivery, packaging, tax and order-level coupons, none of which
    belong to any single line. Summing raw line prices under a category tab
    therefore reports a figure the account never saw. On a real receipt of
    2026-08-11 that read ₹1,138.00 of biryani under Food & Dining against a
    ₹987.48 grand total, because two coupons were worth more than the fees.

    So every line gets its proportional share of the grand total instead, and
    the shares sum back to it exactly (`money.apportion`). A category tab, the
    breakdown under it, and the All tab then all agree, which is the property a
    reader assumes without being told.

    Storage is untouched. `line_total_minor` remains the printed price, which is
    what invariant 13 requires and what the receipt drill-down still shows.

    A receipt with no line items at all (brief 3.4's `skip_reconciliation` case)
    has nothing to apportion across and so appears under no category. Its money
    is still counted on the All tab, which is the only figure that is always the
    whole truth.
    """
    grand_totals: dict[str, int] = {}
    for transaction_id, grand in session.execute(
        select(Transaction.id, Transaction.grand_total_minor).where(_live(user_id))
    ):
        grand_totals[transaction_id] = grand

    per_transaction: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for item_id, transaction_id, amount in session.execute(
        select(
            TransactionLineItem.id,
            TransactionLineItem.transaction_id,
            TransactionLineItem.line_total_minor,
        )
        .join(Transaction, Transaction.id == TransactionLineItem.transaction_id)
        .where(_live(user_id), TransactionLineItem.deleted_at.is_(None))
    ):
        per_transaction[transaction_id].append((item_id, amount))

    shares: dict[str, int] = {}
    for transaction_id, items in per_transaction.items():
        grand = grand_totals.get(transaction_id)
        if grand is None:
            continue
        parts = apportion(grand, [amount for _, amount in items])
        for (item_id, _), part in zip(items, parts, strict=True):
            shares[item_id] = part
    return shares


def _category_subtotals(session: Session, *, user_id: str, slug: str) -> dict[str, int]:
    """Per transaction, what it spent in one category. Missing key means nothing."""
    condition = _category_filter(session, user_id=user_id, slug=slug)
    if condition is None:
        return {}

    shares = line_shares(session, user_id=user_id)
    totals: dict[str, int] = defaultdict(int)
    rows = session.execute(
        select(TransactionLineItem.transaction_id, TransactionLineItem.id)
        .join(Transaction, Transaction.id == TransactionLineItem.transaction_id)
        .where(_live(user_id), TransactionLineItem.deleted_at.is_(None), condition)
    )
    for transaction_id, item_id in rows:
        totals[transaction_id] += shares.get(item_id, 0)
    return dict(totals)


def currencies(session: Session, *, user_id: str) -> list[str]:
    """Every currency this user has receipts in, biggest spender first.

    A page renders one section per entry. Nothing is ever converted: a receipt
    in dollars and a receipt in rupees are two facts, and adding them needs a
    rate, a date to read that rate on, and a rounding rule, none of which a
    ledger built on exact integers should invent. Deferred as Job B.

    Ordered by spend so the currency someone actually lives in leads the page,
    and the occasional holiday receipt follows it.
    """
    totals: dict[str, int] = defaultdict(int)
    for currency, grand in session.execute(
        select(Transaction.currency, Transaction.grand_total_minor).where(_live(user_id))
    ):
        totals[currency] += abs(grand)
    return [c for c, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]


def _rows(
    session: Session,
    *,
    user_id: str,
    category_slug: str | None = None,
    currency: str | None = None,
) -> list[Row]:
    """Every live transaction with its line count, newest first.

    With `category_slug`, only receipts that touched that category come back, and
    each carries what it contributed to it. See `Row.amount_minor` for why that
    is not the grand total.

    With `currency`, only receipts in it. Every caller that totals these rows
    passes one, because summing `amount_minor` across currencies adds paise to
    cents and produces a number denominated in nothing.
    """
    transactions = list(
        session.scalars(
            select(Transaction)
            .where(
                _live(user_id),
                *((Transaction.currency == currency,) if currency is not None else ()),
            )
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

    subtotals = (
        _category_subtotals(session, user_id=user_id, slug=category_slug)
        if category_slug is not None
        else None
    )

    rows = []
    for t in transactions:
        if subtotals is not None and t.id not in subtotals:
            continue
        rows.append(
            Row(
                id=t.id,
                occurred_on_local=t.occurred_on_local,
                grand_total_minor=t.grand_total_minor,
                currency=t.currency,
                outcome=t.reconciliation_outcome,
                unaccounted_adjustment_minor=t.unaccounted_adjustment_minor,
                date_source=t.date_source.value,
                item_count=counts[t.id],
                category_minor=None if subtotals is None else subtotals[t.id],
            )
        )
    return rows


def monthly_totals(
    session: Session,
    *,
    user_id: str,
    months: int,
    today: dt.date,
    category_slug: str | None = None,
    currency: str = "INR",
) -> list[Bucket]:
    """The last `months` calendar months, oldest first, gaps included as zero.

    A month with no receipts is a real fact about the data and gets a bar of
    zero rather than being dropped, which would make the chart lie about time.
    """
    by_month: dict[str, tuple[int, int]] = {}
    for row in _rows(session, user_id=user_id, category_slug=category_slug, currency=currency):
        key = month_key(row.occurred_on_local)
        total, count = by_month.get(key, (0, 0))
        by_month[key] = (total + row.amount_minor, count + 1)

    buckets: list[Bucket] = []
    cursor = today.replace(day=1)
    for _ in range(months):
        total, count = by_month.get(month_key(cursor), (0, 0))
        buckets.append(
            Bucket(
                label=cursor.strftime("%b"),
                total_minor=total,
                count=count,
                currency=currency,
            )
        )
        cursor = previous_month(cursor).replace(day=1)
    return list(reversed(buckets))


def category_totals(
    session: Session,
    *,
    user_id: str,
    start: dt.date,
    end: dt.date,
    currency: str = "INR",
) -> list[Slice]:
    """Spend by category across a date range, inclusive on both ends.

    Categories live on line items, not on transactions, because one grocery
    order is several categories. Each line carries its apportioned share of the
    grand total (`line_shares`), so these slices add up to the money that left
    the account over the range, and the tab bar above them adds up to the same.
    """
    names = {
        c.id: c.display_name
        for c in session.scalars(
            select(Category).where(Category.user_id == user_id, Category.deleted_at.is_(None))
        )
    }

    shares = line_shares(session, user_id=user_id)
    totals: dict[str, int] = defaultdict(int)
    rows = session.execute(
        select(TransactionLineItem.category_id, TransactionLineItem.id)
        .join(Transaction, Transaction.id == TransactionLineItem.transaction_id)
        .where(
            _live(user_id),
            TransactionLineItem.deleted_at.is_(None),
            Transaction.occurred_on_local >= start,
            Transaction.occurred_on_local <= end,
            Transaction.currency == currency,
        )
    )
    for category_id, item_id in rows:
        amount = shares.get(item_id, 0)
        totals[names.get(category_id, UNCATEGORIZED) if category_id else UNCATEGORIZED] += amount

    grand = sum(totals.values())
    return [
        Slice(
            name=name,
            total_minor=amount,
            share=(amount / grand) if grand else 0.0,
            currency=currency,
        )
        for name, amount in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


def overview(
    session: Session,
    *,
    user_id: str,
    today: dt.date,
    category_slug: str | None = None,
    currency: str = "INR",
) -> Overview:
    """The headline figures for the month `today` falls in."""
    start, end = month_bounds(today)
    prev_start, prev_end = month_bounds(previous_month(today))

    rows = _rows(session, user_id=user_id, category_slug=category_slug, currency=currency)
    current = [r for r in rows if start <= r.occurred_on_local <= end]
    previous = [r for r in rows if prev_start <= r.occurred_on_local <= prev_end]

    total = sum(r.amount_minor for r in current)
    return Overview(
        period_label=month_label(today),
        total_minor=total,
        receipt_count=len(current),
        previous_total_minor=sum(r.amount_minor for r in previous),
        average_minor=average(total, len(current)),
        flagged_count=sum(1 for r in rows if r.flagged),
        api_spend_micros=api_spend_micros(session, user_id=user_id),
        currency=currency,
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


def recent(
    session: Session,
    *,
    user_id: str,
    limit: int = 25,
    category_slug: str | None = None,
    currency: str | None = None,
) -> list[Row]:
    return _rows(session, user_id=user_id, category_slug=category_slug, currency=currency)[:limit]


def tabs(
    session: Session,
    *,
    user_id: str,
    start: dt.date,
    end: dt.date,
    currency: str = "INR",
) -> list[Tab]:
    """The top nav. Brief section 2.

    Driven by the category table so a new domain needs no code change, in the
    table's own order so the tabs do not reshuffle as spend moves around. All
    comes first and is the default landing state; a domain with nothing in it
    this period stays in place and renders greyed out.
    """
    # Deliberately reusing `category_totals` rather than running a second query:
    # the number on a tab and the number in the breakdown below it have to be
    # the same number, and the surest way to guarantee that is one source.
    # It keys on display name because that is what a Slice carries. Slugs are
    # unique and display names are not, so two categories renamed to the same
    # thing would share a tab figure. Nothing seeds that, and the alternative
    # duplicates the NULL-folding rule that Uncategorized depends on.
    totals = {
        slice_.name: slice_.total_minor
        for slice_ in category_totals(
            session, user_id=user_id, start=start, end=end, currency=currency
        )
    }

    categories = list(
        session.scalars(
            select(Category).where(Category.user_id == user_id, Category.deleted_at.is_(None))
        )
    )

    # The seeded set keeps the order brief section 2 prints, which is a
    # deliberate ordering and not alphabetical. Sorting on `created_at` alone
    # does not work: the seed writes every row in one flush, so they share a
    # timestamp and fall back to whatever the database returns. Anything added
    # later appends, in the order it was added.
    seed_order = {slug: index for index, (slug, _) in enumerate(SEED_CATEGORIES)}
    last = len(seed_order)
    categories.sort(key=lambda c: (seed_order.get(c.slug, last), c.created_at, c.slug))

    return [
        Tab(slug=None, name="All", total_minor=sum(totals.values()), currency=currency),
        *(
            Tab(
                slug=category.slug,
                name=category.display_name,
                total_minor=totals.get(category.display_name, 0),
                currency=currency,
            )
            for category in categories
        ),
    ]


def top_items(
    session: Session,
    *,
    user_id: str,
    start: dt.date,
    end: dt.date,
    category_slug: str,
    limit: int = 8,
    currency: str = "INR",
) -> list[Slice]:
    """The biggest line items inside one category, for the drill-down.

    Brief section 2 wants domain, then category, then item. There are no
    sub-categories in v0 (brief 3.8), so a domain drills straight to its items.
    Names are grouped verbatim as printed; normalising them here would be
    Stage 2's job, not the dashboard's.

    Amounts are apportioned like everything else on this page, so the items
    listed under a category add up to the category's own figure. That makes an
    item read slightly under its printed price on a discounted order, which is
    the honest number: it is what that item cost you once the coupon landed.
    """
    condition = _category_filter(session, user_id=user_id, slug=category_slug)
    if condition is None:
        return []

    shares = line_shares(session, user_id=user_id)
    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    rows = session.execute(
        select(TransactionLineItem.raw_name, TransactionLineItem.id)
        .join(Transaction, Transaction.id == TransactionLineItem.transaction_id)
        .where(
            _live(user_id),
            TransactionLineItem.deleted_at.is_(None),
            Transaction.occurred_on_local >= start,
            Transaction.occurred_on_local <= end,
            Transaction.currency == currency,
            condition,
        )
    )
    for name, item_id in rows:
        totals[name] += shares.get(item_id, 0)
        counts[name] += 1

    grand = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
    return [
        Slice(
            name=name,
            total_minor=amount,
            share=(amount / grand) if grand else 0.0,
            count=counts[name],
            currency=currency,
        )
        for name, amount in ranked
    ]


def flagged(session: Session, *, user_id: str) -> list[Row]:
    """Rows the gate could not reconcile. Brief 3.3."""
    return [r for r in _rows(session, user_id=user_id) if r.flagged]


def receipt(session: Session, *, user_id: str, transaction_id: str) -> Receipt | None:
    """One transaction with its lines and adjustments, or None if it is not yours.

    The `user_id` filter is a security boundary, not an optimisation: a guessed
    transaction ID must not return another user's receipt. Invariant 8 put
    `user_id` on every table back when there was only one, which is why this
    became a one-line filter rather than a schema change when W3 added more.
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
    "CurrencyView",
    "LineDetail",
    "Overview",
    "Receipt",
    "Row",
    "Slice",
    "Tab",
    "api_spend_micros",
    "category_totals",
    "currencies",
    "flagged",
    "line_shares",
    "month_bounds",
    "month_key",
    "month_label",
    "monthly_totals",
    "overview",
    "previous_month",
    "receipt",
    "recent",
    "tabs",
    "top_items",
]
