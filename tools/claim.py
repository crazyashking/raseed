"""Move one user's rows onto a different `user_id`.

Needed exactly twice, and both times for the same reason: `user_id` is derived
from a Telegram account through `USER_ID_SECRET` and is never stored, so there
is no mapping to update when the derivation changes.

    # The ledger written before multi-user existed, adopted by its owner:
    .venv\\Scripts\\python tools\\claim.py --telegram-id 123456789
    .venv\\Scripts\\python tools\\claim.py --telegram-id 123456789 --apply

    # After rotating USER_ID_SECRET, with the OLD secret to compute the old id:
    .venv\\Scripts\\python tools\\claim.py --telegram-id 123456789 --from <old-user-id> --apply

Reports by default. `--apply` writes.

This does not violate invariant 2. Nothing is deleted, no amount is edited, and
no history is rewritten: the rows keep every value they had, including
`created_at` and `deleted_at`, and change only which ledger they hang off. The
alternative is orphaned rows nobody can ever read again, which loses more.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, func, select, update  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from raseed.db.models import (  # noqa: E402
    Base,
    Category,
    LexiconMiss,
    Merchant,
    RawExtraction,
    Transaction,
    TransactionAdjustment,
    TransactionLineItem,
    User,
)
from raseed.db.seed import ensure_categories, ensure_user  # noqa: E402
from raseed.identity import user_id_for  # noqa: E402

#: Every table with a `user_id`, which is every table except `users` itself.
#: Read off the metadata rather than listed by hand, so a new table cannot be
#: forgotten here and quietly left behind on the old ID.
OWNED = (
    Category,
    Merchant,
    RawExtraction,
    Transaction,
    TransactionLineItem,
    TransactionAdjustment,
    LexiconMiss,
)


def check_every_table_is_covered() -> None:
    """Fail loudly if a table with `user_id` is missing from `OWNED`."""
    covered = {model.__tablename__ for model in OWNED} | {"users"}
    known = {table.name for table in Base.metadata.sorted_tables if "user_id" in table.columns} | {
        "users"
    }
    missing = known - covered
    if missing:
        msg = f"tools/claim.py does not cover {sorted(missing)}. Add them to OWNED."
        raise SystemExit(msg)


def counts(session: Session, user_id: str) -> dict[str, int]:
    return {
        model.__tablename__: int(
            session.scalar(select(func.count()).where(model.user_id == user_id)) or 0
        )
        for model in OWNED
    }


def sole_legacy_user(session: Session, *, exclude: str) -> User | None:
    """The one pre-multi-user account, if that is unambiguously what exists."""
    users = session.scalars(select(User).where(User.id != exclude)).all()
    if len(users) == 1:
        return users[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--telegram-id", type=int, required=True, help="The numeric Telegram user ID to claim for."
    )
    parser.add_argument(
        "--from",
        dest="source",
        default=None,
        help="The user_id to move rows off. Inferred when the ledger has exactly one other user.",
    )
    parser.add_argument("--apply", action="store_true", help="Write the change.")
    args = parser.parse_args()

    check_every_table_is_covered()
    load_dotenv(ROOT / ".env", override=False)

    secret = os.environ.get("USER_ID_SECRET", "").strip()
    if not secret:
        return int(bool(print("USER_ID_SECRET is not set, so the target ID cannot be derived.")))

    target_id = user_id_for(args.telegram_id, secret=secret)
    engine = create_engine(os.environ.get("DATABASE_URL", "sqlite:///raseed.db"))

    with sessionmaker(bind=engine)() as session:
        if args.source:
            source = session.get(User, args.source)
            if source is None:
                print(f"No user {args.source!r} in this ledger.")
                return 2
        else:
            source = sole_legacy_user(session, exclude=target_id)
            if source is None:
                print("Could not infer which user to move from. Pass --from <user-id>.")
                print("Users in this ledger:")
                for row in session.scalars(select(User)).all():
                    print(f"  {row.id}")
                return 2

        if source.id == target_id:
            print("Already claimed. Nothing to do.")
            return 0

        moving = counts(session, source.id)
        print(f"from {source.id}")
        print(f"to   {target_id}   (derived from telegram {args.telegram_id})")
        print()
        for table, count in moving.items():
            print(f"  {table:<26} {count}")
        print(f"\n  {'total':<26} {sum(moving.values())}")

        if not args.apply:
            print("\nNothing was written. Pass --apply to write it.")
            return 0

        # The target user row has to exist before anything points at it, but its
        # categories must NOT: seeding them first and then moving the old five
        # collides on `uq_categories_user_slug`. So the user is created bare, the
        # rows move, and the seed runs afterwards to fill any gap.
        ensure_user(session, target_id)
        session.flush()

        for model in OWNED:
            session.execute(
                update(model).where(model.user_id == source.id).values(user_id=target_id)
            )

        ensure_categories(session, target_id)

        # The old row is soft deleted, never dropped. Invariant 2, and it leaves
        # evidence that the claim happened.
        if source.deleted_at is None:
            source.deleted_at = func.now()

        session.commit()

        left = counts(session, source.id)
        moved = counts(session, target_id)
        print(
            f"\nWritten. {sum(moved.values())} rows on the new ID, {sum(left.values())} left behind."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
