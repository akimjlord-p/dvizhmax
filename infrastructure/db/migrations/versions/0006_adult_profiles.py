"""Restrict active profiles to adult users."""
from alembic import op
import sqlalchemy as sa


revision = "0006_adult_profiles"
down_revision = "0005_social_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing underage profiles become hidden and must pass the updated
    # onboarding age step before they can search for company again.
    op.execute("UPDATE users SET profile_status = 'hidden', onboarding_step = 'age' WHERE age IS NOT NULL AND age < 18")
    op.drop_constraint(op.f("ck_users_age_range"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_age_range"), "users", "age BETWEEN 18 AND 120")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_users_age_range"), "users", type_="check")
    op.create_check_constraint(op.f("ck_users_age_range"), "users", "age BETWEEN 1 AND 120")
