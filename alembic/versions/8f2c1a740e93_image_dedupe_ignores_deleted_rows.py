"""image dedupe ignores soft-deleted rows

Fixes a contradiction between two files that both shipped in commit 4 and 7.

`db/queries.find_by_image_hash` deliberately ignores soft-deleted matches, and
says why: the user deleted that receipt on purpose, so resending the image is
how they undo the undo. But `uq_transactions_user_image` covered every row
including deleted ones, so that path was structurally impossible.

What it cost, live on 2026-08-08: `/undo` a receipt, resend the same image, and
the dedupe check reports "not a duplicate", extraction runs and is billed, and
the INSERT then dies on the constraint. With no error handler registered the
user saw nothing at all. It happened twice in four minutes.

The unique index becomes partial: `WHERE deleted_at IS NULL`. Live rows still
cannot collide, and invariant 2's soft-deleted rows stop blocking their own
replacements. SQLite and Postgres both support partial indexes, which matters
because brief section 8 puts Postgres on the roadmap.

Revision ID: 8f2c1a740e93
Revises: 656deaa29f37
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f2c1a740e93"
down_revision: str | None = "656deaa29f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_transactions_user_image"
TABLE = "transactions"
COLUMNS = ["user_id", "image_sha256"]


def upgrade() -> None:
    # The old constraint is a table-level UNIQUE, which SQLite cannot drop in
    # place, so this rebuilds the table. `render_as_batch` is why that works.
    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.drop_constraint(INDEX_NAME, type_="unique")

    op.create_index(
        INDEX_NAME,
        TABLE,
        COLUMNS,
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Reinstate the total constraint.

    This can fail, and that is honest rather than a defect: if any image hash is
    duplicated across a live row and a soft-deleted one, the old constraint
    cannot be satisfied. Those rows are exactly what this migration was written
    to allow.
    """
    op.drop_index(INDEX_NAME, table_name=TABLE)

    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.create_unique_constraint(INDEX_NAME, COLUMNS)
