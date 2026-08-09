"""Every read the application makes.

All period logic lives here, and it buckets on `occurred_on_local`, never on UTC
and never on server time. Invariant 6, brief section 16.2.

Soft-deleted rows are excluded everywhere by default. A caller that wants them
has to ask, which is the right way round: forgetting the filter should return
less than you expected, not more.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from raseed.db.models import Merchant, RawExtraction, Transaction


def find_by_image_hash(session: Session, *, user_id: str, image_sha256: str) -> Transaction | None:
    """The first dedupe level from brief 3.6: the same image bytes, already logged.

    Returns the live row if there is one. A soft-deleted match does not count,
    because the user deleted it on purpose and resending is how you undo that.
    """
    return session.scalars(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.image_sha256 == image_sha256,
            Transaction.deleted_at.is_(None),
        )
        .limit(1)
    ).first()


def find_soft_duplicate(
    session: Session,
    *,
    user_id: str,
    grand_total_minor: int,
    occurred_on_local: dt.date,
) -> Transaction | None:
    """The second dedupe level from brief 24.4, deliberately a soft match.

    Same amount on the same local date. This is a prompt to the user, never a
    silent rejection: two genuinely identical orders on one day are possible.
    """
    return session.scalars(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.grand_total_minor == grand_total_minor,
            Transaction.occurred_on_local == occurred_on_local,
            Transaction.deleted_at.is_(None),
        )
        .limit(1)
    ).first()


def spend_micros_since(session: Session, *, user_id: str, since: dt.datetime) -> int:
    """API spend since a moment, in micro-dollars. Brief section 16.6.

    This deliberately does NOT bucket on `occurred_on_local`, and that is not a
    violation of invariant 6. Invariant 6 governs reporting on when money was
    SPENT AT A MERCHANT. This measures when tokens were BILLED, which is a rate
    limit on an API budget and has nothing to do with a receipt's date. A
    receipt from last month, uploaded today, costs today's budget.
    """
    total = session.scalar(
        select(func.coalesce(func.sum(RawExtraction.cost_micros_usd), 0)).where(
            RawExtraction.user_id == user_id,
            RawExtraction.created_at >= since,
        )
    )
    return int(total or 0)


def global_spend_micros_since(session: Session, *, since: dt.datetime) -> int:
    """API spend across every user since a moment, in micro-dollars.

    The per-user cap does not bound the bill. Five users each obediently under
    $1 a day is $5 a day, and the account that gets billed is one account. This
    is the only number that says what the whole thing can cost.

    Deliberately not filtered by user, and deliberately not summed from
    `spend_micros_since` per user: a user who is not in the allowlist any more,
    or was never seen again, still spent real money and it still counts.
    """
    total = session.scalar(
        select(func.coalesce(func.sum(RawExtraction.cost_micros_usd), 0)).where(
            RawExtraction.created_at >= since
        )
    )
    return int(total or 0)


def recent_transactions(session: Session, *, user_id: str, limit: int = 10) -> list[Transaction]:
    """The newest rows, for `/recent` and `/undo`. Brief section 16.3."""
    return list(
        session.scalars(
            select(Transaction)
            .where(Transaction.user_id == user_id, Transaction.deleted_at.is_(None))
            .order_by(Transaction.created_at.desc())
            .limit(limit)
        )
    )


def total_for_period(session: Session, *, user_id: str, start: dt.date, end: dt.date) -> int:
    """Sum of a date range, inclusive on both ends, in minor units.

    Buckets on `occurred_on_local`, the merchant-local calendar date. Invariant 6.
    Refunds carry negative totals and therefore net out with no special case.
    """
    total = session.scalar(
        select(func.coalesce(func.sum(Transaction.grand_total_minor), 0)).where(
            Transaction.user_id == user_id,
            Transaction.deleted_at.is_(None),
            Transaction.occurred_on_local >= start,
            Transaction.occurred_on_local <= end,
        )
    )
    return int(total or 0)


def known_merchants(session: Session, *, user_id: str, limit: int = 8) -> list[Merchant]:
    """Merchants seen before, for the confirm keyboard's quick-pick.

    Brief 24.4: the screenshot a user takes rarely prints a merchant name, so the
    keyboard offers previously seen ones. One tap, no typing, and the table fills
    itself from real use.
    """
    return list(
        session.scalars(
            select(Merchant)
            .where(Merchant.user_id == user_id, Merchant.deleted_at.is_(None))
            .order_by(Merchant.display_name)
            .limit(limit)
        )
    )


__all__ = [
    "find_by_image_hash",
    "find_soft_duplicate",
    "known_merchants",
    "recent_transactions",
    "spend_micros_since",
    "total_for_period",
]
