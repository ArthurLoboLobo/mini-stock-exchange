"""Add unique index on brokers.api_key_hash

Revision ID: 003
Revises: 002
Create Date: 2026-02-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_brokers_api_key_hash",
        "brokers",
        ["api_key_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_brokers_api_key_hash", table_name="brokers")
