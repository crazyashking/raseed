"""Bootstrapping a fresh ledger.

The initial migration cannot seed categories, because a category needs a
`user_id` and no user exists at migration time. So the seed runs on first
connect instead, and is idempotent: calling it repeatedly is a no-op.

Brief section 3.8: the taxonomy starts as only what Ashrit actually named, with
no sub-categories, and grows from what accumulates in `uncategorized`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from raseed.db.models import SEED_CATEGORIES, UNCATEGORIZED_SLUG, Category, User


def ensure_user(session: Session) -> User:
    """Return the single user, creating it if the ledger is empty.

    There is exactly one user for now, and `user_id` is on every table so that
    stays true without a migration when it stops being one.
    """
    user = session.scalars(select(User).where(User.deleted_at.is_(None)).limit(1)).first()
    if user is not None:
        return user

    user = User()
    session.add(user)
    session.flush()
    return user


def ensure_categories(session: Session, user_id: str) -> list[Category]:
    """Create any missing seed category for this user, and return all of them.

    Never deletes or renames an existing one. `display_name` is deliberately
    mutable and is left alone if the row already exists, because Ashrit may
    rename a category and nothing keys off the display name.
    """
    existing = {
        category.slug: category
        for category in session.scalars(select(Category).where(Category.user_id == user_id))
    }

    for slug, display_name in SEED_CATEGORIES:
        if slug in existing:
            continue
        category = Category(user_id=user_id, slug=slug, display_name=display_name, is_system=True)
        session.add(category)
        existing[slug] = category

    session.flush()
    return [existing[slug] for slug, _ in SEED_CATEGORIES]


def uncategorized(session: Session, user_id: str) -> Category:
    """The bucket every Stage 2 miss lands in.

    It always exists and is never a failure state (brief 3.8). Raises rather
    than returning None, because a ledger without it is broken.
    """
    category = session.scalars(
        select(Category).where(Category.user_id == user_id, Category.slug == UNCATEGORIZED_SLUG)
    ).first()
    if category is None:
        msg = f"user {user_id} has no '{UNCATEGORIZED_SLUG}' category. Run ensure_categories first."
        raise LookupError(msg)
    return category


def bootstrap(session: Session) -> User:
    """Make a freshly migrated database usable. Idempotent."""
    user = ensure_user(session)
    ensure_categories(session, user.id)
    return user


__all__ = ["bootstrap", "ensure_categories", "ensure_user", "uncategorized"]
