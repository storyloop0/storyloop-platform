"""Best-effort model execution audit snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0016_model_execution_audits"
down_revision = "0015_checkpoint_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_execution_audits",
        sa.Column("execution_id", sa.Text, primary_key=True),
        sa.Column("player_id", sa.Text, nullable=False),
        sa.Column("game_id", sa.Text, nullable=False),
        sa.Column("request_id", sa.Text, nullable=False),
        sa.Column("input_hash", sa.Text, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("error_type", sa.Text),
        sa.Column("attempts_json", sa.Text),
        sa.Column("summary_json", sa.Text),
        sa.Column("policy_json", sa.Text),
        sa.Column("created_order", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_model_execution_audits_request", "model_execution_audits",
                    ["game_id", "request_id", "created_order"])


def downgrade() -> None:
    op.drop_index("ix_model_execution_audits_request", table_name="model_execution_audits")
    op.drop_table("model_execution_audits")
