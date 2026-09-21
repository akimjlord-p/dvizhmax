"""Separate profile display from reaction; normalize event preference signals."""
from alembic import op
import sqlalchemy as sa

revision = "0005_social_actions"
down_revision = "0004_profile_photo_optional"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companion_views", sa.Column("reacted_at", sa.DateTime(timezone=True), nullable=True))
    # Before this migration a view was only written when reacting, including skips.
    op.execute("UPDATE companion_views SET reacted_at = shown_at")
    # Tags are immutable for imported cards. Rebuild reaction contributions from
    # saved signals; retain onboarding weights. Old like->plan paths counted 0.6.
    op.execute("UPDATE user_tag_weights SET reaction_weight = 0")
    op.execute("""
        INSERT INTO user_tag_weights (user_id, tag_id, initial_weight, reaction_weight)
        SELECT signals.user_id, et.tag_id, 0, SUM(signals.weight)
        FROM (
            SELECT COALESCE(r.user_id, p.user_id) AS user_id,
                   COALESCE(r.event_id, p.event_id) AS event_id,
                   CASE WHEN p.status IN ('planned', 'completed') THEN 0.5
                        WHEN r.reaction = 'like' THEN 0.1 ELSE 0 END AS weight
            FROM event_reactions r
            FULL OUTER JOIN event_plans p ON p.user_id = r.user_id AND p.event_id = r.event_id
        ) signals
        JOIN event_tags et ON et.event_id = signals.event_id
        GROUP BY signals.user_id, et.tag_id
        ON CONFLICT (user_id, tag_id) DO UPDATE
        SET reaction_weight = EXCLUDED.reaction_weight, updated_at = now()
    """)


def downgrade() -> None:
    # Normalized weights remain: the old click path cannot be reconstructed.
    op.drop_column("companion_views", "reacted_at")
