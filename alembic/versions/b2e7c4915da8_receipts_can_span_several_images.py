"""receipts can span several images

One call over several images can now return several receipts, or one receipt
built from all of them. Three columns follow from that.

`transactions.receipt_index` and `pending_receipts.receipt_index` say which
receipt of the batch a row is. Every row already in the ledger came from one
image and is receipt zero, which is what the server default records.

`pending_receipts.image_path` becomes `image_paths`, holding a JSON array. A
receipt too long to photograph in one go leaves several files behind and
invariant 7 does not permit keeping any of them. Existing values are single
paths and are rewritten into one-element arrays rather than left in a column
whose name now promises a list.

`transactions.image_sha256` keeps its shape. The first receipt out of a batch
keeps the batch's own digest, so every existing row's dedupe key is untouched,
and only a second receipt out of one batch gets a derived one. See
`adapters.images.receipt_sha256`.

Revision ID: b2e7c4915da8
Revises: 6777f3118190
Create Date: 2026-08-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2e7c4915da8"
down_revision: str | Sequence[str] | None = "6777f3118190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch:
        batch.add_column(
            sa.Column("receipt_index", sa.Integer(), nullable=False, server_default="0")
        )

    with op.batch_alter_table("pending_receipts", schema=None) as batch:
        batch.add_column(
            sa.Column("receipt_index", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("image_paths", sa.String(length=2000), nullable=True))

    # A path is JSON-encoded rather than wrapped in brackets by hand, so a
    # Windows backslash survives the trip. `json_array` is in every SQLite build
    # this project runs on, and the WHERE clause leaves the nulls null.
    op.execute(
        sa.text(
            "UPDATE pending_receipts SET image_paths = json_array(image_path) "
            "WHERE image_path IS NOT NULL"
        )
    )

    with op.batch_alter_table("pending_receipts", schema=None) as batch:
        batch.drop_column("image_path")


def downgrade() -> None:
    with op.batch_alter_table("pending_receipts", schema=None) as batch:
        batch.add_column(sa.Column("image_path", sa.String(length=500), nullable=True))

    # Only the first path survives going back, which is the honest thing a
    # single column can hold. A batch that came back down leaves its later
    # images to `ImageStore.sweep`.
    op.execute(
        sa.text(
            "UPDATE pending_receipts SET image_path = json_extract(image_paths, '$[0]') "
            "WHERE image_paths IS NOT NULL"
        )
    )

    with op.batch_alter_table("pending_receipts", schema=None) as batch:
        batch.drop_column("image_paths")
        batch.drop_column("receipt_index")

    with op.batch_alter_table("transactions", schema=None) as batch:
        batch.drop_column("receipt_index")
