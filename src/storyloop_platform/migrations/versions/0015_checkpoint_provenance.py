"""Bind checkpoint caches to scenario identity and committed sources.

Old summaries lack provenance, so discard only this derived cache. The events
and observations needed to rebuild it remain authoritative.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_checkpoint_provenance"
down_revision = "0014_observation_join_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM agent_contexts")
    op.add_column("agent_contexts", sa.Column("scenario_id", sa.Text, nullable=False, server_default=""))
    op.add_column("agent_contexts", sa.Column("scenario_version", sa.Text, nullable=False, server_default=""))
    op.add_column("agent_contexts", sa.Column("source_ids", sa.Text, nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("agent_contexts") as batch:
        batch.drop_column("source_ids")
        batch.drop_column("scenario_version")
        batch.drop_column("scenario_id")
