"""Allow an active profile without a photo."""
from alembic import op
import sqlalchemy as sa


revision = "0004_profile_photo_optional"
down_revision = "0003_multiple_primary_tags"
branch_labels = None
depends_on = None


_WITHOUT_PHOTO = (
    "profile_status != 'active' OR (name IS NOT NULL AND length(trim(name)) > 0 "
    "AND gender IS NOT NULL AND length(trim(gender)) > 0 AND age IS NOT NULL "
    "AND city_id IS NOT NULL)"
)
_WITH_PHOTO = (
    "profile_status != 'active' OR (name IS NOT NULL AND length(trim(name)) > 0 "
    "AND gender IS NOT NULL AND length(trim(gender)) > 0 AND age IS NOT NULL "
    "AND city_id IS NOT NULL AND (photo_url IS NOT NULL OR photo_attachment IS NOT NULL))"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_users_active_profile_complete"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_active_profile_complete"), "users", _WITHOUT_PHOTO)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_active_profile_complete"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_active_profile_complete"), "users", _WITH_PHOTO)
