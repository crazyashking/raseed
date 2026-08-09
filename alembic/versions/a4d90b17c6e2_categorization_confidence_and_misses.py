"""categorization confidence, and the lexicon miss log

Brief 18.5 and 4.5. Two things Stage 2 needs before it can run at all:

`transaction_line_items` gets `category_source` and `category_confidence_bp`, so
a row records not just what category it landed in but how that was decided. That
is what makes a future lexicon improvement re-runnable against only the weak
rows instead of against a year of receipts.

Confidence is stored in basis points rather than as the float brief 18.5 asks
for. The no-floating-point test walks every column of every table, not only the
money ones, and adding an exception to that test to hold a similarity score is a
worse trade than storing 7826 for 78.26.

`lexicon_misses` is new. The brief retracts its own "300 to 400 terms" estimate
and says the real number comes out of this table after a month of real receipts,
so this is the mechanism by which the lexicon grows rather than a diagnostic.
One row per occurrence, no counter column, so nothing here needs an UPDATE.

Revision ID: a4d90b17c6e2
Revises: 8f2c1a740e93
Create Date: 2026-08-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d90b17c6e2"
down_revision: str | None = "8f2c1a740e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINE_ITEMS = "transaction_line_items"
MISSES = "lexicon_misses"
CONFIDENCE_CHECK = "ck_line_items_confidence_range"

_SOURCE = sa.Enum(
    "LEXICON_EXACT",
    "LEXICON_FUZZY",
    "LLM",
    "MANUAL",
    name="categorysource",
    native_enum=False,
)


def upgrade() -> None:
    with op.batch_alter_table(LINE_ITEMS, schema=None) as batch:
        batch.add_column(sa.Column("category_source", _SOURCE, nullable=True))
        batch.add_column(sa.Column("category_confidence_bp", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            CONFIDENCE_CHECK,
            "category_confidence_bp IS NULL OR "
            "(category_confidence_bp >= 0 AND category_confidence_bp <= 10000)",
        )

    op.create_table(
        MISSES,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("term", sa.String(length=200), nullable=False),
        sa.Column("raw_name", sa.String(length=400), nullable=False),
        sa.Column("lexicon_name", sa.String(length=80), nullable=False),
        sa.Column("lexicon_version", sa.Integer(), nullable=False),
        sa.Column("line_item_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["line_item_id"], [f"{LINE_ITEMS}.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lexicon_misses_user_term", MISSES, ["user_id", "term"])
    op.create_index("ix_lexicon_misses_version", MISSES, ["lexicon_version"])


def downgrade() -> None:
    op.drop_index("ix_lexicon_misses_version", table_name=MISSES)
    op.drop_index("ix_lexicon_misses_user_term", table_name=MISSES)
    op.drop_table(MISSES)

    with op.batch_alter_table(LINE_ITEMS, schema=None) as batch:
        batch.drop_constraint(CONFIDENCE_CHECK, type_="check")
        batch.drop_column("category_confidence_bp")
        batch.drop_column("category_source")
