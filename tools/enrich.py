"""Re-run Stage 2 over line items already in the ledger.

Brief 3.8 and 18.5. This is the payoff for keeping `raw_extractions` immutable
and for splitting extraction from categorization: when the lexicon grows, the
whole history can be re-categorized without the receipt images, which are long
since deleted (invariant 7), and without paying a vision model again.

    .venv\\Scripts\\python tools\\enrich.py                  # report only, free
    .venv\\Scripts\\python tools\\enrich.py --weak-only       # only the rows worth redoing
    .venv\\Scripts\\python tools\\enrich.py --apply           # write the changes
    .venv\\Scripts\\python tools\\enrich.py --model --apply   # let the fallback run too
    .venv\\Scripts\\python tools\\enrich.py --misses          # what the lexicon still does not know

**Reporting is the default and it is free.** Nothing is written and no API call
is made unless you ask, so the normal way to price a lexicon change is to edit
the YAML, run this, and read the diff before deciding.

**`--model` costs money.** Without it the fallback never runs, unmatched items
stay `uncategorized`, and the whole pass is a local lexicon lookup. With it, one
batched call is made per receipt that still has unmatched items, billed to the
same `raw_extractions` table the daily cap reads, so a large backfill is visible
in the budget like anything else.

**What `--apply` writes.** Only the Stage 2 columns: `category_id`,
`category_source`, `category_confidence_bp`, `normalized_slug`, plus new rows in
`lexicon_misses`. It never touches `raw_name`, any money column, or anything in
`raw_extractions`. Those are the facts; these are a cache over them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from raseed.db import ledger  # noqa: E402
from raseed.db.models import (  # noqa: E402
    UNCATEGORIZED_SLUG,
    Category,
    LexiconMiss,
    Source,
    Transaction,
    TransactionLineItem,
    User,
)
from raseed.enrichment import lexicon as lex  # noqa: E402
from raseed.enrichment.categorize import (  # noqa: E402
    DEFAULT_CATEGORIZER_MODEL_ID,
    Decision,
    Item,
    categorize,
)
from raseed.enrichment.providers.base import CategorizationProvider  # noqa: E402
from raseed.enrichment.providers.gemini import GeminiCategorizer  # noqa: E402
from raseed.extraction.pricing import format_usd  # noqa: E402

#: Below this a row is worth re-running: the lexicon has probably learned the
#: word since. Expressed in basis points to match the stored column.
WEAK_CONFIDENCE_BP = 8_000


@dataclass
class Change:
    """One line item's before and after."""

    line_item: TransactionLineItem
    decision: Decision
    was_category: str | None
    was_source: str | None

    @property
    def category_changed(self) -> bool:
        return self.was_category != self.decision.category_slug

    def describe(self) -> str:
        before = f"{self.was_category or '-'} ({self.was_source or 'nothing'})"
        after = f"{self.decision.category_slug} ({self.decision.source.value if self.decision.source else 'nothing'})"
        return f"  {self.line_item.raw_name[:56]:<56}  {before:<32} -> {after}"


def allowed_slugs(session: Session, *, user_id: str) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(Category.slug).where(Category.user_id == user_id, Category.deleted_at.is_(None))
        ).all()
    )


def slug_of(session: Session, category_id: str | None) -> str | None:
    if category_id is None:
        return None
    category = session.get(Category, category_id)
    return category.slug if category else None


def items_by_transaction(
    session: Session, *, user_id: str, weak_only: bool
) -> dict[str, list[TransactionLineItem]]:
    """Live line items, grouped by receipt so the fallback batches per receipt."""
    query = (
        select(TransactionLineItem)
        .join(Transaction, Transaction.id == TransactionLineItem.transaction_id)
        .where(
            TransactionLineItem.user_id == user_id,
            TransactionLineItem.deleted_at.is_(None),
            Transaction.deleted_at.is_(None),
        )
        .order_by(TransactionLineItem.transaction_id, TransactionLineItem.position)
    )
    if weak_only:
        query = query.where(
            (TransactionLineItem.category_source.is_(None))
            | (TransactionLineItem.category_confidence_bp < WEAK_CONFIDENCE_BP)
        )

    grouped: dict[str, list[TransactionLineItem]] = defaultdict(list)
    for item in session.scalars(query).all():
        grouped[item.transaction_id].append(item)
    return grouped


def build_provider(model_id: str) -> CategorizationProvider:
    """Only constructed when `--model` was asked for. Nothing is spent otherwise."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        msg = "GEMINI_API_KEY is not set, so --model cannot run"
        raise SystemExit(msg)
    return GeminiCategorizer(api_key=api_key, model_id=model_id)


def run_user(
    session: Session,
    *,
    user: User,
    provider: CategorizationProvider | None,
    weak_only: bool,
    apply: bool,
) -> tuple[list[Change], int]:
    """Re-categorize one user's line items. Returns the changes and the cost."""
    categories = {
        category.slug: category
        for category in session.scalars(
            select(Category).where(Category.user_id == user.id, Category.deleted_at.is_(None))
        ).all()
    }
    allowed = tuple(categories)
    if UNCATEGORIZED_SLUG not in allowed:
        return [], 0

    changes: list[Change] = []
    spent = 0

    for transaction_id, items in items_by_transaction(
        session, user_id=user.id, weak_only=weak_only
    ).items():
        outcome = categorize(
            [Item(raw_name=row.raw_name, quantity_text=row.quantity_text) for row in items],
            allowed=allowed,
            provider=provider,
        )
        spent += outcome.cost_micros_usd

        if apply and outcome.provider_result is not None:
            # The backfill's API spend counts against the same daily cap as a
            # live receipt. A large re-run is meant to be visible in the budget.
            transaction = session.get(Transaction, transaction_id)
            ledger.record_categorization(
                session,
                user_id=user.id,
                result=outcome.provider_result,
                source=transaction.source if transaction else Source.TELEGRAM_IMAGE,
                image_sha256=transaction.image_sha256 if transaction else None,
            )

        for item, decision in zip(items, outcome.decisions, strict=True):
            changes.append(
                Change(
                    line_item=item,
                    decision=decision,
                    was_category=slug_of(session, item.category_id),
                    was_source=item.category_source.value if item.category_source else None,
                )
            )

            if not apply:
                continue

            target = categories.get(decision.category_slug)
            item.category_id = target.id if target else None
            item.category_source = decision.source
            item.category_confidence_bp = decision.confidence_bp
            item.canonical_slug = decision.canonical_slug
            item.normalized_slug = decision.normalized.normalized_slug
            item.quantity = decision.normalized.quantity
            item.unit = decision.normalized.unit
            item.unit_normalized = decision.normalized.unit_normalized
            item.pack_count = decision.normalized.pack_count

            for term in decision.misses:
                session.add(
                    LexiconMiss(
                        user_id=user.id,
                        term=term,
                        raw_name=decision.raw_name,
                        lexicon_name=outcome.lexicon_name,
                        lexicon_version=outcome.lexicon_version,
                        line_item_id=item.id,
                    )
                )

    return changes, spent


def report_misses(session: Session, *, limit: int) -> None:
    """The weekly review, as brief 4.5 describes it: what to add to the YAML next."""
    rows = session.execute(
        select(
            LexiconMiss.term,
            func.count().label("seen"),
            func.max(LexiconMiss.lexicon_version).label("version"),
            func.min(LexiconMiss.raw_name).label("example"),
        )
        .where(LexiconMiss.deleted_at.is_(None))
        .group_by(LexiconMiss.term)
        .order_by(func.count().desc(), LexiconMiss.term)
        .limit(limit)
    ).all()

    if not rows:
        print("No misses logged. Either nothing has been categorized yet, or the")
        print("lexicon knew every word it was shown.")
        return

    print(f"{'term':<28} {'seen':>5}  {'v':>3}  example")
    print("-" * 100)
    for term, seen, version, example in rows:
        print(f"{term:<28} {seen:>5}  {version:>3}  {example[:56]}")


def summarise(changes: list[Change], *, spent: int, apply: bool) -> None:
    sources = Counter(
        change.decision.source.value if change.decision.source else "nothing" for change in changes
    )
    moved = [change for change in changes if change.category_changed]

    print()
    print(f"{len(changes)} line items considered, {len(moved)} would change category")
    for source, count in sorted(sources.items(), key=lambda pair: -pair[1]):
        print(f"  {source:<16} {count}")
    if spent:
        print(f"  fallback cost   {format_usd(spent)}")

    if moved:
        print()
        print("Changes:")
        for change in moved[:40]:
            print(change.describe())
        if len(moved) > 40:
            print(f"  ... and {len(moved) - 40} more")

    print()
    print("Written." if apply else "Nothing was written. Pass --apply to write it.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes.")
    parser.add_argument(
        "--model",
        action="store_true",
        help="Let the LLM fallback run on unmatched items. THIS SPENDS MONEY.",
    )
    parser.add_argument(
        "--model-id",
        default=None,
        help=f"Fallback model. Defaults to {DEFAULT_CATEGORIZER_MODEL_ID}.",
    )
    parser.add_argument(
        "--weak-only",
        action="store_true",
        help=(
            "Only rows nothing decided, or decided below a confidence of "
            f"{WEAK_CONFIDENCE_BP} basis points."
        ),
    )
    parser.add_argument(
        "--misses",
        action="store_true",
        help="Print the miss log instead of re-categorizing. Always free.",
    )
    parser.add_argument("--limit", type=int, default=40, help="Rows for --misses.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=False)
    database_url = os.environ.get("DATABASE_URL", "sqlite:///raseed.db")

    engine = create_engine(database_url)
    sessions = sessionmaker(bind=engine)

    with sessions() as session:
        if args.misses:
            report_misses(session, limit=args.limit)
            return 0

        book = lex.load()
        print(f"lexicon: {len(book.terms)} terms, {len(book.by_surface)} surfaces, v{book.version}")

        provider = (
            build_provider(args.model_id or DEFAULT_CATEGORIZER_MODEL_ID) if args.model else None
        )
        if provider is not None:
            print(f"fallback: {provider.model_id}. This will spend money.")
        else:
            print("fallback: off. Unmatched items stay uncategorized.")

        users = session.scalars(select(User).where(User.deleted_at.is_(None))).all()
        all_changes: list[Change] = []
        total_spent = 0

        for user in users:
            changes, spent = run_user(
                session,
                user=user,
                provider=provider,
                weak_only=args.weak_only,
                apply=args.apply,
            )
            all_changes.extend(changes)
            total_spent += spent

        if args.apply:
            session.commit()

        summarise(all_changes, spent=total_spent, apply=args.apply)

    return 0


if __name__ == "__main__":
    print(f"raseed enrich, {dt.datetime.now(dt.UTC).date().isoformat()}")
    raise SystemExit(main())
