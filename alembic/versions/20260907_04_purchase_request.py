"""Keep one purchase per confirmation message, including after restart."""
from alembic import op
import sqlalchemy as sa

revision = "20260907_04"
down_revision = "20260907_03"
branch_labels = None
depends_on = None


def upgrade():
    # Initial migration in this project imports live models: fresh databases
    # already contain this column. Existing databases need an additive change.
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('shop_purchases')}
    if 'request_key' not in columns:
        op.add_column('shop_purchases', sa.Column('request_key', sa.String(128), nullable=True))
        op.create_index('uq_purchase_request_key', 'shop_purchases', ['request_key'], unique=True)


def downgrade():
    raise RuntimeError('Downgrade is disabled to protect purchase history.')
