"""Distinguish ordinary turns from explicit fact query receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0020_portal_turn_request_kind"
down_revision = "0019_generated_scene_generation_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portal_turns", sa.Column(
        "request_kind", sa.Text, nullable=False, server_default="turn"))


def downgrade() -> None:
    bind = op.get_bind()
    has_facts = bind.execute(sa.text("""SELECT 1 FROM portal_turns
        WHERE request_kind='fact_query' LIMIT 1""")).first()
    if has_facts is not None:
        raise RuntimeError("Cannot downgrade portal_turns with fact_query receipts; plan a data migration first")
    with op.batch_alter_table("portal_turns") as batch:
        batch.drop_column("request_kind")
