"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-12

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Таблицы учёта: магазины, товары, склады ФФ, остатки, документы, журнал движений."""
    op.create_table(
        "stores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("marketplace", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("marketplace", "name"),
    )
    op.create_table(
        "warehouses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_warehouses_active_name",
        "warehouses",
        ["name"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["stores.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "sku"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("from_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("to_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("order_ref", sa.String(length=64), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind IN ('receipt', 'transfer', 'fbs', 'fbo')", name="kind_valid"),
        sa.CheckConstraint(
            "status IN ('done', 'in_transit', 'received', 'accepted', 'waiting', 'reserved', 'shipped', 'cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint("qty > 0", name="qty_positive"),
        sa.ForeignKeyConstraint(
            ["from_warehouse_id"],
            ["warehouses.id"],
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_warehouse_id"],
            ["warehouses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_documents_open", "documents", ["kind", "status"], unique=False)
    op.create_table(
        "stock",
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("on_hand", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        sa.CheckConstraint("reserved <= on_hand", name="reserved_within_on_hand"),
        sa.CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
        ),
        sa.PrimaryKeyConstraint("product_id", "warehouse_id"),
    )
    op.create_table(
        "movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("from_loc", sa.String(length=16), nullable=False),
        sa.Column("from_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("to_loc", sa.String(length=16), nullable=False),
        sa.Column("to_warehouse_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "from_loc IN ('supplier', 'ff', 'ff_reserve', 'transit_ff', 'transit_mp', 'mp', 'customer')",
            name="from_loc_valid",
        ),
        sa.CheckConstraint(
            "to_loc IN ('supplier', 'ff', 'ff_reserve', 'transit_ff', 'transit_mp', 'mp', 'customer')",
            name="to_loc_valid",
        ),
        sa.CheckConstraint("qty > 0", name="qty_positive"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["from_warehouse_id"],
            ["warehouses.id"],
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_warehouse_id"],
            ["warehouses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_movements_document", "movements", ["document_id"], unique=False)
    op.create_index("ix_movements_product", "movements", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_movements_product", table_name="movements")
    op.drop_index("ix_movements_document", table_name="movements")
    op.drop_table("movements")
    op.drop_table("stock")
    op.drop_index("ix_documents_open", table_name="documents")
    op.drop_table("documents")
    op.drop_table("products")
    op.drop_index("uq_warehouses_active_name", table_name="warehouses", postgresql_where=sa.text("archived_at IS NULL"))
    op.drop_table("warehouses")
    op.drop_table("stores")
