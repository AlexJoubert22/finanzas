"""phase 11.2 ai_rejected signal status

Revision ID: 30738afb34b2
Revises: f9ce7c25e1cc
Create Date: 2026-05-01 00:24:44.849183

Adds ``'ai_rejected'`` to the ``signals.status`` CHECK constraint.
SQLite has no ``ALTER CONSTRAINT``, so we use ``batch_alter_table``
which copies the table under the hood.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "30738afb34b2"
down_revision: str | None = "f9ce7c25e1cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: drop old check, add new one with ai_rejected."""
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.drop_constraint("ck_signals_status", type_="check")
        batch_op.create_check_constraint(
            "ck_signals_status",
            "status IN ('pending', 'expired', 'consumed', 'cancelled', 'ai_rejected')",
        )


def downgrade() -> None:
    """Downgrade schema: restore the original 4-status check.

    Any existing 'ai_rejected' rows would violate the original
    constraint, so the downgrade flips them to 'cancelled' first as
    the safest semantic equivalent (operator manually rejected).
    """
    op.execute(
        "UPDATE signals SET status='cancelled' WHERE status='ai_rejected'"
    )
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.drop_constraint("ck_signals_status", type_="check")
        batch_op.create_check_constraint(
            "ck_signals_status",
            "status IN ('pending', 'expired', 'consumed', 'cancelled')",
        )
