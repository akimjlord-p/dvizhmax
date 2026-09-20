# Schema reference

Generated from SQLAlchemy metadata. 20 tables. SQL types are PostgreSQL types.

## cities

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| name | VARCHAR(200) | False |  |  |
| timezone | VARCHAR(100) | False |  |  |
| source_codes | JSONB | False | '{}'::jsonb |  |
| id | UUID | False | application default |  |

Primary key: `id`.


## tags

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| code | VARCHAR(100) | False |  |  |
| name | VARCHAR(200) | False |  |  |
| kind | VARCHAR(20) | False |  |  |
| description | TEXT | False |  |  |
| is_active | BOOLEAN | False | true |  |
| show_in_onboarding | BOOLEAN | False | false |  |
| id | UUID | False | application default |  |

Primary key: `id`.

- CHECK `kind IN ('primary', 'secondary')`
- UNIQUE `code`
- UNIQUE `id, kind`

## import_runs

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| source | VARCHAR(50) | False |  |  |
| city_id | UUID | False |  | cities.id |
| status | VARCHAR(20) | False | running |  |
| started_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| finished_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| created_count | INTEGER | False | 0 |  |
| updated_count | INTEGER | False | 0 |  |
| error | TEXT | True |  |  |
| id | UUID | False | application default |  |

Primary key: `id`.

- CHECK `created_count >= 0 AND updated_count >= 0`
- CHECK `status IN ('running', 'succeeded', 'failed')`
- CHECK `finished_at >= started_at`
- FK `city_id` -> `cities.id`
- `CREATE INDEX ix_import_runs_city_id ON import_runs (city_id)`

## places

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| city_id | UUID | False |  | cities.id |
| source | VARCHAR(50) | False |  |  |
| external_id | VARCHAR(200) | False |  |  |
| name | TEXT | False |  |  |
| address | TEXT | True |  |  |
| latitude | DOUBLE PRECISION | True |  |  |
| longitude | DOUBLE PRECISION | True |  |  |
| id | UUID | False | application default |  |

Primary key: `id`.

- CHECK `latitude BETWEEN -90 AND 90`
- CHECK `longitude BETWEEN -180 AND 180`
- FK `city_id` -> `cities.id`
- UNIQUE `source, external_id`
- `CREATE INDEX ix_places_city_id ON places (city_id)`

## users

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| max_user_id | BIGINT | False |  |  |
| max_username | TEXT | True |  |  |
| name | VARCHAR(100) | True |  |  |
| description | TEXT | True |  |  |
| gender | VARCHAR(30) | True |  |  |
| age | SMALLINT | True |  |  |
| photo_url | TEXT | True |  |  |
| photo_attachment | JSONB | True |  |  |
| city_id | UUID | True |  | cities.id |
| profile_status | VARCHAR(20) | False | guest |  |
| onboarding_step | VARCHAR(50) | True |  |  |
| notifications_enabled | BOOLEAN | False | true |  |
| id | UUID | False | application default |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `id`.

- CHECK `profile_status != 'active' OR (name IS NOT NULL AND length(trim(name)) > 0 AND gender IS NOT NULL AND length(trim(gender)) > 0 AND age IS NOT NULL AND city_id IS NOT NULL AND (photo_url IS NOT NULL OR photo_attachment IS NOT NULL))`
- CHECK `age BETWEEN 1 AND 120`
- CHECK `profile_status IN ('guest', 'draft', 'active', 'hidden')`
- FK `city_id` -> `cities.id`
- UNIQUE `max_user_id`
- `CREATE INDEX ix_users_city_id ON users (city_id)`

## events

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| title | TEXT | False |  |  |
| description | TEXT | True |  |  |
| city_id | UUID | False |  | cities.id |
| place_id | UUID | True |  | places.id |
| price_text | TEXT | True |  |  |
| price_min | NUMERIC(12, 2) | True |  |  |
| currency | VARCHAR(3) | True |  |  |
| is_free | BOOLEAN | True |  |  |
| age_min | SMALLINT | True |  |  |
| data_status | VARCHAR(20) | False | current |  |
| tagging_status | VARCHAR(20) | False | pending |  |
| tagging_operation_id | TEXT | True |  |  |
| tagging_error | TEXT | True |  |  |
| id | UUID | False | application default |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `id`.

- CHECK `age_min >= 0`
- CHECK `data_status IN ('current', 'uncertain', 'unavailable')`
- CHECK `price_min >= 0`
- CHECK `tagging_status IN ('pending', 'processing', 'done', 'failed')`
- FK `city_id` -> `cities.id`
- FK `place_id` -> `places.id`
- `CREATE INDEX ix_events_city_status ON events (city_id, data_status)`
- `CREATE INDEX ix_events_place_id ON events (place_id)`
- `CREATE INDEX ix_events_tagging_status ON events (tagging_status)`

## user_blocks

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| blocker_id | UUID | False |  | users.id |
| blocked_id | UUID | False |  | users.id |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `blocker_id, blocked_id`.

- CHECK `blocker_id != blocked_id`
- FK `blocked_id` -> `users.id`
- FK `blocker_id` -> `users.id`
- `CREATE INDEX ix_user_blocks_blocked_id ON user_blocks (blocked_id)`

## user_consents

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| user_id | UUID | False |  | users.id |
| document_version | VARCHAR(100) | False |  |  |
| accepted_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| revoked_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| id | UUID | False | application default |  |

Primary key: `id`.

- CHECK `revoked_at >= accepted_at`
- FK `user_id` -> `users.id`
- `CREATE INDEX ix_user_consents_user_id ON user_consents (user_id)`
- `CREATE UNIQUE INDEX uq_user_consents_current ON user_consents (user_id, document_version) WHERE revoked_at IS NULL`

## user_tag_weights

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| user_id | UUID | False |  | users.id |
| tag_id | UUID | False |  | tags.id |
| initial_weight | NUMERIC(12, 2) | False | 0 |  |
| reaction_weight | NUMERIC(12, 2) | False | 0 |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `user_id, tag_id`.

- CHECK `initial_weight >= 0`
- FK `tag_id` -> `tags.id`
- FK `user_id` -> `users.id`
- `CREATE INDEX ix_user_tag_weights_tag_id ON user_tag_weights (tag_id)`

## companion_views

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| viewer_id | UUID | False |  | users.id |
| event_id | UUID | False |  | events.id |
| shown_user_id | UUID | False |  | users.id |
| shown_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `viewer_id, event_id, shown_user_id`.

- CHECK `viewer_id != shown_user_id`
- FK `event_id` -> `events.id`
- FK `shown_user_id` -> `users.id`
- FK `viewer_id` -> `users.id`
- `CREATE INDEX ix_companion_views_shown_user ON companion_views (shown_user_id)`

## event_plans

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| user_id | UUID | False |  | users.id |
| event_id | UUID | False |  | events.id |
| planned_date | DATE | True |  |  |
| planned_time | TIME WITHOUT TIME ZONE | True |  |  |
| status | VARCHAR(20) | False | planned |  |
| company_status | VARCHAR(20) | False | not_looking |  |
| id | UUID | False | application default |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `id`.

- CHECK `company_status IN ('not_looking', 'looking', 'found')`
- CHECK `company_status != 'looking' OR status = 'planned'`
- CHECK `status IN ('planned', 'cancelled', 'completed')`
- CHECK `planned_time IS NULL OR planned_date IS NOT NULL`
- FK `event_id` -> `events.id`
- FK `user_id` -> `users.id`
- UNIQUE `id, event_id`
- UNIQUE `user_id, event_id`
- `CREATE INDEX ix_event_plans_event_id ON event_plans (event_id)`
- `CREATE INDEX ix_event_plans_search ON event_plans (event_id, planned_date) WHERE status = 'planned' AND company_status = 'looking'`

## event_reactions

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| user_id | UUID | False |  | users.id |
| event_id | UUID | False |  | events.id |
| reaction | VARCHAR(10) | False |  |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `user_id, event_id`.

- CHECK `reaction IN ('like', 'dislike', 'skip')`
- FK `event_id` -> `events.id`
- FK `user_id` -> `users.id`
- `CREATE INDEX ix_event_reactions_event_id ON event_reactions (event_id)`
- `CREATE INDEX ix_event_reactions_user_reaction ON event_reactions (user_id, reaction)`

## event_sources

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| event_id | UUID | False |  | events.id |
| source | VARCHAR(50) | False |  |  |
| external_id | VARCHAR(200) | False |  |  |
| source_url | TEXT | False |  |  |
| is_primary | BOOLEAN | False | false |  |
| raw_payload | JSONB | False |  |  |
| last_checked_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| last_success_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| last_http_status | SMALLINT | True |  |  |
| id | UUID | False | application default |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `id`.

- CHECK `last_http_status BETWEEN 100 AND 599`
- FK `event_id` -> `events.id`
- UNIQUE `source, external_id`
- `CREATE INDEX ix_event_sources_event_id ON event_sources (event_id)`
- `CREATE UNIQUE INDEX uq_event_sources_primary ON event_sources (event_id) WHERE is_primary`

## event_tags

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| event_id | UUID | False |  | events.id |
| tag_id | UUID | False |  | tags.id |
| kind | VARCHAR(20) | False |  | tags.kind |

Primary key: `event_id, tag_id`.

- FK `event_id` -> `events.id`
- FK `tag_id, kind` -> `tags.id, tags.kind`
- `CREATE INDEX ix_event_tags_tag_id ON event_tags (tag_id)`
- `CREATE UNIQUE INDEX uq_event_tags_primary ON event_tags (event_id) WHERE kind = 'primary'`

## companion_interests

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| sender_plan_id | UUID | False |  | event_plans.id |
| recipient_plan_id | UUID | False |  | event_plans.id |
| event_id | UUID | False |  | event_plans.event_id, event_plans.event_id, events.id |
| status | VARCHAR(20) | False | active |  |
| viewed_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| announced_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| id | UUID | False | application default |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `id`.

- CHECK `sender_plan_id != recipient_plan_id`
- CHECK `status IN ('active', 'withdrawn')`
- FK `event_id` -> `events.id`
- FK `recipient_plan_id, event_id` -> `event_plans.id, event_plans.event_id`
- FK `sender_plan_id, event_id` -> `event_plans.id, event_plans.event_id`
- UNIQUE `sender_plan_id, recipient_plan_id`
- `CREATE INDEX ix_companion_interests_event ON companion_interests (event_id)`
- `CREATE INDEX ix_companion_interests_incoming ON companion_interests (recipient_plan_id, status, viewed_at, announced_at)`

## event_images

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| event_source_id | UUID | False |  | event_sources.id |
| url | TEXT | False |  |  |
| position | INTEGER | False | 0 |  |
| credit_name | TEXT | True |  |  |
| credit_url | TEXT | True |  |  |
| max_attachment | JSONB | True |  |  |
| id | UUID | False | application default |  |

Primary key: `id`.

- CHECK `position >= 0`
- FK `event_source_id` -> `event_sources.id`
- UNIQUE `event_source_id, url`
- `CREATE INDEX ix_event_images_source_position ON event_images (event_source_id, position)`

## event_schedules

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| event_source_id | UUID | False |  | event_sources.id |
| starts_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| ends_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| start_time | TIME WITHOUT TIME ZONE | True |  |  |
| end_time | TIME WITHOUT TIME ZONE | True |  |  |
| is_startless | BOOLEAN | False | false |  |
| is_endless | BOOLEAN | False | false |  |
| use_place_schedule | BOOLEAN | False | false |  |
| recurrence | JSONB | True |  |  |
| raw_payload | JSONB | False |  |  |
| id | UUID | False | application default |  |

Primary key: `id`.

- CHECK `ends_at >= starts_at`
- FK `event_source_id` -> `event_sources.id`
- `CREATE INDEX ix_event_schedules_ends_at ON event_schedules (ends_at)`
- `CREATE INDEX ix_event_schedules_event_source_id ON event_schedules (event_source_id)`
- `CREATE INDEX ix_event_schedules_starts_at ON event_schedules (starts_at)`

## matches

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| event_id | UUID | False |  | event_plans.event_id, event_plans.event_id, events.id |
| first_user_id | UUID | False |  | event_plans.user_id |
| second_user_id | UUID | False |  | event_plans.user_id |
| status | VARCHAR(10) | False | active |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| id | UUID | False | application default |  |

Primary key: `id`.

- CHECK `first_user_id < second_user_id`
- CHECK `status IN ('active', 'closed')`
- FK `event_id` -> `events.id`
- FK `first_user_id, event_id` -> `event_plans.user_id, event_plans.event_id`
- FK `second_user_id, event_id` -> `event_plans.user_id, event_plans.event_id`
- UNIQUE `event_id, first_user_id, second_user_id`
- `CREATE INDEX ix_matches_first_user ON matches (first_user_id)`
- `CREATE INDEX ix_matches_second_user ON matches (second_user_id)`

## notifications

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| recipient_id | UUID | False |  | users.id |
| event_id | UUID | False |  | events.id |
| match_id | UUID | True |  | matches.id |
| type | VARCHAR(30) | False |  |  |
| payload | JSONB | False | '{}'::jsonb |  |
| deduplication_key | VARCHAR(200) | False |  |  |
| status | VARCHAR(20) | False | pending |  |
| attempt_count | INTEGER | False | 0 |  |
| next_attempt_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| locked_until | TIMESTAMP WITH TIME ZONE | True |  |  |
| last_error | TEXT | True |  |  |
| max_message_id | TEXT | True |  |  |
| sent_at | TIMESTAMP WITH TIME ZONE | True |  |  |
| id | UUID | False | application default |  |
| created_at | TIMESTAMP WITH TIME ZONE | False | now() |  |
| updated_at | TIMESTAMP WITH TIME ZONE | False | now() |  |

Primary key: `id`.

- CHECK `attempt_count >= 0`
- CHECK `(type = 'match' AND match_id IS NOT NULL) OR (type = 'interests_digest' AND match_id IS NULL)`
- CHECK `status != 'sent' OR sent_at IS NOT NULL`
- CHECK `status IN ('pending', 'processing', 'sent', 'failed', 'cancelled')`
- CHECK `type IN ('interests_digest', 'match')`
- FK `event_id` -> `events.id`
- FK `match_id` -> `matches.id`
- FK `recipient_id` -> `users.id`
- UNIQUE `deduplication_key`
- UNIQUE `match_id, recipient_id`
- `CREATE INDEX ix_notifications_due ON notifications (next_attempt_at) WHERE status = 'pending'`
- `CREATE INDEX ix_notifications_event_id ON notifications (event_id)`
- `CREATE INDEX ix_notifications_lease ON notifications (locked_until) WHERE status = 'processing'`
- `CREATE INDEX ix_notifications_recipient_id ON notifications (recipient_id)`
- `CREATE UNIQUE INDEX uq_notifications_pending_digest ON notifications (recipient_id, event_id) WHERE type = 'interests_digest' AND status IN ('pending', 'processing')`

## notification_interests

| Column | Type | Nullable | Default | References |
|---|---|---|---|---|
| notification_id | UUID | False |  | notifications.id |
| companion_interest_id | UUID | False |  | companion_interests.id |

Primary key: `notification_id, companion_interest_id`.

- FK `companion_interest_id` -> `companion_interests.id`
- FK `notification_id` -> `notifications.id`
- UNIQUE `companion_interest_id`
