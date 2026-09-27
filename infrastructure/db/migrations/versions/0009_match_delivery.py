"""Track match notification delivery so failed sends are retried."""
from alembic import op
import sqlalchemy as sa


revision = "0009_match_delivery"
down_revision = "0008_no_company_on_kids_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("matches", sa.Column("first_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("matches", sa.Column("second_notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("matches", sa.Column("notify_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False))
    op.add_column("matches", sa.Column("last_notify_attempt_at", sa.DateTime(timezone=True), nullable=True))
    # Matches made before this change were notified the old way; do not resend them.
    op.execute("UPDATE matches SET first_notified_at = created_at, second_notified_at = created_at")


def downgrade() -> None:
    for column in ("last_notify_attempt_at", "notify_attempts", "second_notified_at", "first_notified_at"):
        op.drop_column("matches", column)
