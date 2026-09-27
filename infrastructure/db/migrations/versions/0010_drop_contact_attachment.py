"""Contacts are typed text only; drop the unused attachment column."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_drop_contact_attachment"
down_revision = "0009_match_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("match_contacts", "contact_attachment")


def downgrade() -> None:
    op.add_column(
        "match_contacts",
        sa.Column("contact_attachment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
