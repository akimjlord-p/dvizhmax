"""Initial event catalog, images and one-time tagging state."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen initial schema; independent of current ORM models.
    op.create_table('cities',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('timezone', sa.String(length=100), nullable=False),
    sa.Column('source_codes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cities'))
    )
    op.create_table('tags',
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('show_in_onboarding', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("kind IN ('primary', 'secondary')", name=op.f('ck_tags_kind')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tags')),
    sa.UniqueConstraint('code', name=op.f('uq_tags_code')),
    sa.UniqueConstraint('id', 'kind', name=op.f('uq_tags_id'))
    )
    op.create_table('import_runs',
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('city_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='running', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('updated_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("status IN ('running', 'succeeded', 'failed')", name=op.f('ck_import_runs_status')),
    sa.CheckConstraint('created_count >= 0 AND updated_count >= 0', name=op.f('ck_import_runs_counts_nonnegative')),
    sa.CheckConstraint('finished_at >= started_at', name=op.f('ck_import_runs_time_order')),
    sa.ForeignKeyConstraint(['city_id'], ['cities.id'], name=op.f('fk_import_runs_city_id_cities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_import_runs'))
    )
    op.create_index(op.f('ix_import_runs_city_id'), 'import_runs', ['city_id'], unique=False)
    op.create_table('places',
    sa.Column('city_id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('latitude', sa.Double(), nullable=True),
    sa.Column('longitude', sa.Double(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('latitude BETWEEN -90 AND 90', name=op.f('ck_places_latitude_range')),
    sa.CheckConstraint('longitude BETWEEN -180 AND 180', name=op.f('ck_places_longitude_range')),
    sa.ForeignKeyConstraint(['city_id'], ['cities.id'], name=op.f('fk_places_city_id_cities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_places')),
    sa.UniqueConstraint('source', 'external_id', name=op.f('uq_places_source'))
    )
    op.create_index(op.f('ix_places_city_id'), 'places', ['city_id'], unique=False)
    op.create_table('events',
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('city_id', sa.Uuid(), nullable=False),
    sa.Column('place_id', sa.Uuid(), nullable=True),
    sa.Column('price_text', sa.Text(), nullable=True),
    sa.Column('price_min', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('is_free', sa.Boolean(), nullable=True),
    sa.Column('age_min', sa.SmallInteger(), nullable=True),
    sa.Column('data_status', sa.String(length=20), server_default='current', nullable=False),
    sa.Column('tagging_status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('tagging_operation_id', sa.Text(), nullable=True),
    sa.Column('tagging_error', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("data_status IN ('current', 'uncertain', 'unavailable')", name=op.f('ck_events_data_status')),
    sa.CheckConstraint("tagging_status IN ('pending', 'processing', 'done', 'failed')", name=op.f('ck_events_tagging_status')),
    sa.CheckConstraint('age_min >= 0', name=op.f('ck_events_age_nonnegative')),
    sa.CheckConstraint('price_min >= 0', name=op.f('ck_events_price_nonnegative')),
    sa.ForeignKeyConstraint(['city_id'], ['cities.id'], name=op.f('fk_events_city_id_cities')),
    sa.ForeignKeyConstraint(['place_id'], ['places.id'], name=op.f('fk_events_place_id_places')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events'))
    )
    op.create_index('ix_events_city_status', 'events', ['city_id', 'data_status'], unique=False)
    op.create_index(op.f('ix_events_place_id'), 'events', ['place_id'], unique=False)
    op.create_index(op.f('ix_events_tagging_status'), 'events', ['tagging_status'], unique=False)
    op.create_table('event_sources',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_http_status', sa.SmallInteger(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('last_http_status BETWEEN 100 AND 599', name=op.f('ck_event_sources_http_status')),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_event_sources_event_id_events')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_sources')),
    sa.UniqueConstraint('source', 'external_id', name=op.f('uq_event_sources_source'))
    )
    op.create_index(op.f('ix_event_sources_event_id'), 'event_sources', ['event_id'], unique=False)
    op.create_index('uq_event_sources_primary', 'event_sources', ['event_id'], unique=True, postgresql_where=sa.text('is_primary'))
    op.create_table('event_tags',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('tag_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], name=op.f('fk_event_tags_event_id_events')),
    sa.ForeignKeyConstraint(['tag_id', 'kind'], ['tags.id', 'tags.kind'], name=op.f('fk_event_tags_tag_id_tags')),
    sa.PrimaryKeyConstraint('event_id', 'tag_id', name=op.f('pk_event_tags'))
    )
    op.create_index('ix_event_tags_tag_id', 'event_tags', ['tag_id'], unique=False)
    op.create_index('uq_event_tags_primary', 'event_tags', ['event_id'], unique=True, postgresql_where=sa.text("kind = 'primary'"))
    op.create_table('event_images',
    sa.Column('event_source_id', sa.Uuid(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('credit_name', sa.Text(), nullable=True),
    sa.Column('credit_url', sa.Text(), nullable=True),
    sa.Column('max_attachment', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('position >= 0', name=op.f('ck_event_images_position_nonnegative')),
    sa.ForeignKeyConstraint(['event_source_id'], ['event_sources.id'], name=op.f('fk_event_images_event_source_id_event_sources')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_images')),
    sa.UniqueConstraint('event_source_id', 'url', name=op.f('uq_event_images_event_source_id'))
    )
    op.create_index('ix_event_images_source_position', 'event_images', ['event_source_id', 'position'], unique=False)
    op.create_table('event_schedules',
    sa.Column('event_source_id', sa.Uuid(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('start_time', sa.Time(), nullable=True),
    sa.Column('end_time', sa.Time(), nullable=True),
    sa.Column('is_startless', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_endless', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('use_place_schedule', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('recurrence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('ends_at >= starts_at', name=op.f('ck_event_schedules_period_order')),
    sa.ForeignKeyConstraint(['event_source_id'], ['event_sources.id'], name=op.f('fk_event_schedules_event_source_id_event_sources')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_schedules'))
    )
    op.create_index(op.f('ix_event_schedules_ends_at'), 'event_schedules', ['ends_at'], unique=False)
    op.create_index(op.f('ix_event_schedules_event_source_id'), 'event_schedules', ['event_source_id'], unique=False)
    op.create_index(op.f('ix_event_schedules_starts_at'), 'event_schedules', ['starts_at'], unique=False)


def downgrade() -> None:
    # Frozen initial schema; independent of current ORM models.
    op.drop_index(op.f('ix_event_schedules_starts_at'), table_name='event_schedules')
    op.drop_index(op.f('ix_event_schedules_event_source_id'), table_name='event_schedules')
    op.drop_index(op.f('ix_event_schedules_ends_at'), table_name='event_schedules')
    op.drop_table('event_schedules')
    op.drop_index('ix_event_images_source_position', table_name='event_images')
    op.drop_table('event_images')
    op.drop_index('uq_event_tags_primary', table_name='event_tags', postgresql_where=sa.text("kind = 'primary'"))
    op.drop_index('ix_event_tags_tag_id', table_name='event_tags')
    op.drop_table('event_tags')
    op.drop_index('uq_event_sources_primary', table_name='event_sources', postgresql_where=sa.text('is_primary'))
    op.drop_index(op.f('ix_event_sources_event_id'), table_name='event_sources')
    op.drop_table('event_sources')
    op.drop_index(op.f('ix_events_tagging_status'), table_name='events')
    op.drop_index(op.f('ix_events_place_id'), table_name='events')
    op.drop_index('ix_events_city_status', table_name='events')
    op.drop_table('events')
    op.drop_index(op.f('ix_places_city_id'), table_name='places')
    op.drop_table('places')
    op.drop_index(op.f('ix_import_runs_city_id'), table_name='import_runs')
    op.drop_table('import_runs')
    op.drop_table('tags')
    op.drop_table('cities')

