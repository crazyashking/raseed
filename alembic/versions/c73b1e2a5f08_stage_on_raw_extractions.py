"""record which stage paid for a raw_extractions row

Stage 2's model fallback is a billed API call. Brief 16.6's daily cap is
computed by summing `raw_extractions.cost_micros_usd` over a rolling window, so
a categorization call that is not written to this table makes the cap quietly
under-count, and one that is written without a marker makes a Stage 2 row
indistinguishable from a Stage 1 one.

Defaults to `EXTRACTION`, so every row written before Stage 2 existed keeps
meaning exactly what it meant.

Revision ID: c73b1e2a5f08
Revises: a4d90b17c6e2
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c73b1e2a5f08"
down_revision: str | None = "a4d90b17c6e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "raw_extractions"

_STAGE = sa.Enum("EXTRACTION", "CATEGORIZATION", name="extractionstage", native_enum=False)


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.add_column(
            sa.Column(
                "stage",
                _STAGE,
                nullable=False,
                server_default="EXTRACTION",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.drop_column("stage")
