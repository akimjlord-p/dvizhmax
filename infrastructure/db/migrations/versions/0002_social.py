"""User profiles, reactions, plans, matches and notification queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_social"
down_revision = "0001_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen schema, independent of current ORM models.
    op.create_table('users',
    sa.Column('max_user_id', sa.BigInteger(), nullable=False),
    sa.Column('max_username', sa.Text(), nullable=True),
    sa.Column('name', sa.String(length=100), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('gender', sa.String(length=30), nullable=True),
    sa.Column('age', sa.SmallInteger(), nullable=True),
    sa.Column('photo_url', sa.Text(), nullable=True),
    sa.Column('photo_attachment', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('city_id', sa.Uuid(), nullable=True),
    sa.Column('profile_status', sa.String(length=20), server_default='guest', nullable=False),
    sa.Column('onboarding_step', sa.String(length=50), nullable=True),
    sa.Column('notifications_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("profile_status != 'active' OR (name IS NOT NULL AND length(trim(name)) > 0 AND gender IS NOT NULL AND length(trim(gender)) > 0 AND age IS NOT NULL AND city_id IS NOT NULL AND (photo_url IS NOT NULL OR photo_attachment IS NOT NULL))", name=op.f('ck_users_active_profile_complete')),
    sa.CheckConstraint("profile_status IN ('guest', 'draft', 'active', 'hidden')", name=op.f('ck_users_profile_status')),
    sa.CheckConstraint('age BETWEEN 1 AND 120', name=op.f('ck_users_age_range')),
    sa.ForeignKeyConstraint(['city_id'], ['cities.id'], name=op.f('fk_users_city_id_cities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('max_user_id', name=op.f('uq_users_max_user_id'))
    )
    op.create_index(op.f('ix_users_city_id'), 'users', ['city_id'], unique=False)
    op.create_table('user_blocks',
    sa.Column('blocker_id', sa.Uuid(), nullable=False),
    sa.Column('blocked_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('blocker_id != blocked_id', name=op.f('ck_user_blocks_not_self')),
    sa.ForeignKeyConstraint(['blocked_id'], ['users.id'], name=op.f('fk_user_blocks_blocked_id_users')),
    sa.ForeignKeyConstraint(['blocker_id'], ['users.id'], name=op.f('fk_user_blocks_blocker_id_users')),
    sa.PrimaryKeyConstraint('blocker_id', 'blocked_id', name=op.f('pk_user_blocks'))
    )
    op.create_index(op.f('ix_user_blocks_blocked_id'), 'user_blocks', ['blocked_id'], unique=False)
    op.create_table('user_consents',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('document_version', sa.String(length=100), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('revoked_at >= accepted_at', name=op.f('ck_user_consents_time_order')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_consents_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_consents'))
    )
    op.create_index(op.f('ix_user_consents_user_id'), 'user_consents', ['user_id'], unique=False)
    op.create_index('uq_user_consents_current', 'user_consents', ['user_id', 'document_version'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('user_tag_weights',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('tag_id', sa.Uuid(), nullable=False),
    sa.Column('initial_weight', sa.Numeric(precision=12, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('reaction_weight', sa.Numeric(precision=12, scale=2), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('initial_weight >= 0', name=op.f('ck_user_tag_weights_initial_nonnegative')),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], name=op.f('fk_user_tag_weights_tag_id_tags')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_tag_weights_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', 'tag_id', name=op.f('pk_user_tag_weights'))
    )
    op.create_index(op.f('ix_user_tag_weights_tag_id'), 'user_tag_weights', ['tag_id'], unique=False)
    op.create_table('companion_views',
    sa.Column('viewer_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('shown_user_id', sa.Uuid(), nullable=False),
    sa.Column('shown_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('viewer_id != shown_user_id', name=op.f('ck_companion_views_not_self')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_companion_views_event_id_events')),
    sa.ForeignKeyConstraint(['shown_user_id'], ['users.id'], name=op.f('fk_companion_views_shown_user_id_users')),
    sa.ForeignKeyConstraint(['viewer_id'], ['users.id'], name=op.f('fk_companion_views_viewer_id_users')),
    sa.PrimaryKeyConstraint('viewer_id', 'event_id', 'shown_user_id', name=op.f('pk_companion_views'))
    )
    op.create_index('ix_companion_views_shown_user', 'companion_views', ['shown_user_id'], unique=False)
    op.create_table('event_plans',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('planned_date', sa.Date(), nullable=True),
    sa.Column('planned_time', sa.Time(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='planned', nullable=False),
    sa.Column('company_status', sa.String(length=20), server_default='not_looking', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("company_status != 'looking' OR status = 'planned'", name=op.f('ck_event_plans_search_requires_plan')),
    sa.CheckConstraint("company_status IN ('not_looking', 'looking', 'found')", name=op.f('ck_event_plans_company_status')),
    sa.CheckConstraint("status IN ('planned', 'cancelled', 'completed')", name=op.f('ck_event_plans_status')),
    sa.CheckConstraint('planned_time IS NULL OR planned_date IS NOT NULL', name=op.f('ck_event_plans_time_requires_date')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_event_plans_event_id_events')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_event_plans_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_plans')),
    sa.UniqueConstraint('id', 'event_id', name=op.f('uq_event_plans_id')),
    sa.UniqueConstraint('user_id', 'event_id', name=op.f('uq_event_plans_user_id'))
    )
    op.create_index(op.f('ix_event_plans_event_id'), 'event_plans', ['event_id'], unique=False)
    op.create_index('ix_event_plans_search', 'event_plans', ['event_id', 'planned_date'], unique=False, postgresql_where=sa.text("status = 'planned' AND company_status = 'looking'"))
    op.create_table('event_reactions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('reaction', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("reaction IN ('like', 'dislike', 'skip')", name=op.f('ck_event_reactions_reaction')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_event_reactions_event_id_events')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_event_reactions_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', 'event_id', name=op.f('pk_event_reactions'))
    )
    op.create_index(op.f('ix_event_reactions_event_id'), 'event_reactions', ['event_id'], unique=False)
    op.create_index('ix_event_reactions_user_reaction', 'event_reactions', ['user_id', 'reaction'], unique=False)
    op.create_table('companion_interests',
    sa.Column('sender_plan_id', sa.Uuid(), nullable=False),
    sa.Column('recipient_plan_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('viewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('announced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'withdrawn')", name=op.f('ck_companion_interests_status')),
    sa.CheckConstraint('sender_plan_id != recipient_plan_id', name=op.f('ck_companion_interests_not_self')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_companion_interests_event_id_events')),
    sa.ForeignKeyConstraint(['recipient_plan_id', 'event_id'], ['event_plans.id', 'event_plans.event_id'], name=op.f('fk_companion_interests_recipient_plan_id_event_plans')),
    sa.ForeignKeyConstraint(['sender_plan_id', 'event_id'], ['event_plans.id', 'event_plans.event_id'], name=op.f('fk_companion_interests_sender_plan_id_event_plans')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_companion_interests')),
    sa.UniqueConstraint('sender_plan_id', 'recipient_plan_id', name=op.f('uq_companion_interests_sender_plan_id'))
    )
    op.create_index('ix_companion_interests_event', 'companion_interests', ['event_id'], unique=False)
    op.create_index('ix_companion_interests_incoming', 'companion_interests', ['recipient_plan_id', 'status', 'viewed_at', 'announced_at'], unique=False)
    op.create_table('matches',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('first_user_id', sa.Uuid(), nullable=False),
    sa.Column('second_user_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("status IN ('active', 'closed')", name=op.f('ck_matches_status')),
    sa.CheckConstraint('first_user_id < second_user_id', name=op.f('ck_matches_ordered_users')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_matches_event_id_events')),
    sa.ForeignKeyConstraint(['first_user_id', 'event_id'], ['event_plans.user_id', 'event_plans.event_id'], name=op.f('fk_matches_first_user_id_event_plans')),
    sa.ForeignKeyConstraint(['second_user_id', 'event_id'], ['event_plans.user_id', 'event_plans.event_id'], name=op.f('fk_matches_second_user_id_event_plans')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_matches')),
    sa.UniqueConstraint('event_id', 'first_user_id', 'second_user_id', name=op.f('uq_matches_event_id'))
    )
    op.create_index('ix_matches_first_user', 'matches', ['first_user_id'], unique=False)
    op.create_index('ix_matches_second_user', 'matches', ['second_user_id'], unique=False)
    op.create_table('notifications',
    sa.Column('recipient_id', sa.Uuid(), nullable=False),
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('match_id', sa.Uuid(), nullable=True),
    sa.Column('type', sa.String(length=30), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('deduplication_key', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('attempt_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('max_message_id', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(type = 'match' AND match_id IS NOT NULL) OR (type = 'interests_digest' AND match_id IS NULL)", name=op.f('ck_notifications_match_required')),
    sa.CheckConstraint("status != 'sent' OR sent_at IS NOT NULL", name=op.f('ck_notifications_sent_timestamp')),
    sa.CheckConstraint("status IN ('pending', 'processing', 'sent', 'failed', 'cancelled')", name=op.f('ck_notifications_status')),
    sa.CheckConstraint("type IN ('interests_digest', 'match')", name=op.f('ck_notifications_type')),
    sa.CheckConstraint('attempt_count >= 0', name=op.f('ck_notifications_attempt_nonnegative')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_notifications_event_id_events')),
    sa.ForeignKeyConstraint(['match_id'], ['matches.id'], name=op.f('fk_notifications_match_id_matches')),
    sa.ForeignKeyConstraint(['recipient_id'], ['users.id'], name=op.f('fk_notifications_recipient_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('deduplication_key', name=op.f('uq_notifications_deduplication_key')),
    sa.UniqueConstraint('match_id', 'recipient_id', name=op.f('uq_notifications_match_id'))
    )
    op.create_index('ix_notifications_due', 'notifications', ['next_attempt_at'], unique=False, postgresql_where=sa.text("status = 'pending'"))
    op.create_index(op.f('ix_notifications_event_id'), 'notifications', ['event_id'], unique=False)
    op.create_index('ix_notifications_lease', 'notifications', ['locked_until'], unique=False, postgresql_where=sa.text("status = 'processing'"))
    op.create_index(op.f('ix_notifications_recipient_id'), 'notifications', ['recipient_id'], unique=False)
    op.create_index('uq_notifications_pending_digest', 'notifications', ['recipient_id', 'event_id'], unique=True, postgresql_where=sa.text("type = 'interests_digest' AND status IN ('pending', 'processing')"))
    op.create_table('notification_interests',
    sa.Column('notification_id', sa.Uuid(), nullable=False),
    sa.Column('companion_interest_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['companion_interest_id'], ['companion_interests.id'], name=op.f('fk_notification_interests_companion_interest_id_companion_interests')),
    sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], name=op.f('fk_notification_interests_notification_id_notifications')),
    sa.PrimaryKeyConstraint('notification_id', 'companion_interest_id', name=op.f('pk_notification_interests')),
    sa.UniqueConstraint('companion_interest_id', name=op.f('uq_notification_interests_companion_interest_id'))
    )


def downgrade() -> None:
    # Frozen schema, independent of current ORM models.
    op.drop_table('notification_interests')
    op.drop_index('uq_notifications_pending_digest', table_name='notifications', postgresql_where=sa.text("type = 'interests_digest' AND status IN ('pending', 'processing')"))
    op.drop_index(op.f('ix_notifications_recipient_id'), table_name='notifications')
    op.drop_index('ix_notifications_lease', table_name='notifications', postgresql_where=sa.text("status = 'processing'"))
    op.drop_index(op.f('ix_notifications_event_id'), table_name='notifications')
    op.drop_index('ix_notifications_due', table_name='notifications', postgresql_where=sa.text("status = 'pending'"))
    op.drop_table('notifications')
    op.drop_index('ix_matches_second_user', table_name='matches')
    op.drop_index('ix_matches_first_user', table_name='matches')
    op.drop_table('matches')
    op.drop_index('ix_companion_interests_incoming', table_name='companion_interests')
    op.drop_index('ix_companion_interests_event', table_name='companion_interests')
    op.drop_table('companion_interests')
    op.drop_index('ix_event_reactions_user_reaction', table_name='event_reactions')
    op.drop_index(op.f('ix_event_reactions_event_id'), table_name='event_reactions')
    op.drop_table('event_reactions')
    op.drop_index('ix_event_plans_search', table_name='event_plans', postgresql_where=sa.text("status = 'planned' AND company_status = 'looking'"))
    op.drop_index(op.f('ix_event_plans_event_id'), table_name='event_plans')
    op.drop_table('event_plans')
    op.drop_index('ix_companion_views_shown_user', table_name='companion_views')
    op.drop_table('companion_views')
    op.drop_index(op.f('ix_user_tag_weights_tag_id'), table_name='user_tag_weights')
    op.drop_table('user_tag_weights')
    op.drop_index('uq_user_consents_current', table_name='user_consents', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_index(op.f('ix_user_consents_user_id'), table_name='user_consents')
    op.drop_table('user_consents')
    op.drop_index(op.f('ix_user_blocks_blocked_id'), table_name='user_blocks')
    op.drop_table('user_blocks')
    op.drop_index(op.f('ix_users_city_id'), table_name='users')
    op.drop_table('users')
