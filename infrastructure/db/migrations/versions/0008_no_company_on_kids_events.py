"""Turn off company search on children's events created before it was forbidden."""
from alembic import op


revision = "0008_no_company_on_kids_events"
down_revision = "0007_match_contacts"
branch_labels = None
depends_on = None

# Mirrors infrastructure.db.repositories.feed.is_for_kids.
KIDS_PLANS = """
    SELECT p.id FROM event_plans p
    JOIN events e ON e.id = p.event_id
    JOIN event_sources s ON s.event_id = e.id AND s.is_primary
    WHERE p.status = 'planned' AND p.company_status = 'looking'
      AND (s.raw_payload -> 'categories' ? 'kids'
           OR e.title ~* '(для детей|детск|малыш|дошкольн|для школьников|для подростков)')
"""


def upgrade() -> None:
    op.execute(f"""
        UPDATE companion_interests SET status = 'withdrawn'
        WHERE status = 'active'
          AND (sender_plan_id IN ({KIDS_PLANS}) OR recipient_plan_id IN ({KIDS_PLANS}))
    """)
    op.execute(f"UPDATE event_plans SET company_status = 'not_looking' WHERE id IN ({KIDS_PLANS})")


def downgrade() -> None:
    # Data-only change: the previous search state is not recorded.
    pass
