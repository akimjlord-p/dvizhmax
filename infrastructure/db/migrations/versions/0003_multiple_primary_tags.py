"""Allow several primary format tags on one event."""
from alembic import op
import sqlalchemy as sa


revision = "0003_multiple_primary_tags"
down_revision = "0002_social"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "uq_event_tags_primary",
        table_name="event_tags",
        postgresql_where=sa.text("kind = 'primary'"),
    )


def downgrade() -> None:
    # A downgrade can fail if an event already has several primary tags.
    # The data must be resolved before restoring the old invariant.
    op.create_index(
        "uq_event_tags_primary",
        "event_tags",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'primary'"),
    )
