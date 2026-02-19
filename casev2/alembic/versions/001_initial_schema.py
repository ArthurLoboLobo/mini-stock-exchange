"""Initial schema: brokers, orders, trades

Revision ID: 001
Revises:
Create Date: 2026-02-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

order_side = postgresql.ENUM("bid", "ask", name="order_side", create_type=False)
order_type = postgresql.ENUM("limit", "market", name="order_type", create_type=False)
order_status = postgresql.ENUM("open", "closed", name="order_status", create_type=False)


def upgrade() -> None:
    order_side.create(op.get_bind(), checkfirst=True)
    order_type.create(op.get_bind(), checkfirst=True)
    order_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "brokers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("api_key_hash", sa.String(255), nullable=False),
        sa.Column("webhook_url", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brokers.id"), nullable=False),
        sa.Column("document_number", sa.String(20), nullable=False),
        sa.Column("side", order_side, nullable=False),
        sa.Column("order_type", order_type, nullable=False),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("price", sa.Integer, nullable=True),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("remaining_quantity", sa.Integer, nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", order_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_orders_matching",
        "orders",
        ["symbol", "side", "price", "created_at"],
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_orders_broker", "orders", ["broker_id", "created_at"])

    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("buy_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("sell_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("symbol", sa.String(10), nullable=False),
        sa.Column("price", sa.Integer, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index("ix_trades_symbol", "trades", ["symbol", "created_at"])
    op.create_index("ix_trades_buy_order", "trades", ["buy_order_id"])
    op.create_index("ix_trades_sell_order", "trades", ["sell_order_id"])


def downgrade() -> None:
    op.drop_table("trades")
    op.drop_table("orders")
    op.drop_table("brokers")

    order_status.drop(op.get_bind(), checkfirst=True)
    order_type.drop(op.get_bind(), checkfirst=True)
    order_side.drop(op.get_bind(), checkfirst=True)
