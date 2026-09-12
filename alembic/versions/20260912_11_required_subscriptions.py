"""Add required subscription advertising and progress statistics."""
from alembic import op

from app.database.models import RequiredSubscription, RequiredSubscriptionProgress

revision = '20260912_11'
down_revision = '20260909_10'
branch_labels = None
depends_on = None


def upgrade():
    RequiredSubscription.__table__.create(bind=op.get_bind(), checkfirst=True)
    RequiredSubscriptionProgress.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to preserve subscription statistics.')
