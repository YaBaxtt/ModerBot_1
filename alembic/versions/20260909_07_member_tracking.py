"""Track member profile snapshots independently in every connected chat."""
from alembic import op

from app.database.models import MemberProfileSnapshot

revision = '20260909_07'
down_revision = '20260909_06'
branch_labels = None
depends_on = None


def upgrade():
    MemberProfileSnapshot.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to preserve member history.')
