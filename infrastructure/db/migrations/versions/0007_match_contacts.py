"""Store contacts that match participants voluntarily share."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_match_contacts"
down_revision = "0006_adult_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_contacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("sender_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="awaiting", nullable=False),
        sa.Column("contact_text", sa.Text(), nullable=True),
        sa.Column("contact_attachment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('awaiting', 'confirming', 'sent', 'cancelled')",
            name=op.f("ck_match_contacts_status"),
        ),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], name=op.f("fk_match_contacts_match_id_matches")),
        sa.ForeignKeyConstraint(["sender_id"], ["users.id"], name=op.f("fk_match_contacts_sender_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_contacts")),
        sa.UniqueConstraint("match_id", "sender_id", name=op.f("uq_match_contacts_match_id")),
    )
    op.create_index(
        "uq_match_contacts_pending_sender",
        "match_contacts",
        ["sender_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('awaiting', 'confirming')"),
    )


def downgrade() -> None:
    op.drop_index("uq_match_contacts_pending_sender", table_name="match_contacts")
    op.drop_table("match_contacts")
