"""separate the lexicon's canonical name from the printed slug

Brief 16.7 defines `normalized_slug` with a worked example:
`Amul Taaza Toned Milk 500ml` becomes `amul-taaza-toned-milk`. That is the
printed name with the quantity stripped, and it keeps the brand.

Stage 2 also produces a different and more useful answer: what the item actually
is, from the lexicon. `Bhindi 500g` and `Okra 500g` share no `normalized_slug`
at all, and brief 3.7's item canonicalization needs them to be one product.
Overloading one column with both meanings would have lost the brand permanently,
so `canonical_slug` is its own column.

Revision ID: d18a6f40b3c5
Revises: c73b1e2a5f08
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d18a6f40b3c5"
down_revision: str | None = "c73b1e2a5f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "transaction_line_items"


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.add_column(sa.Column("canonical_slug", sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.drop_column("canonical_slug")
