"""categories

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Справочник категорий и категория у товара (для уровня просмотра «Категории»)."""
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.add_column("products", sa.Column("category_id", sa.Integer(), nullable=True))
    op.create_foreign_key("products_category_id_fkey", "products", "categories", ["category_id"], ["id"])
    # Базы, заполненные до этой миграции: разложить товары из задания по категориям.
    op.execute(
        "INSERT INTO categories (name) SELECT unnest(ARRAY['Органайзеры', 'Контейнеры и корзины']) "
        "WHERE EXISTS (SELECT 1 FROM products WHERE sku IN ('SKU-101', 'SKU-102', 'SKU-103'))"
    )
    op.execute(
        "UPDATE products p SET category_id = c.id FROM categories c "
        "WHERE (p.sku = 'SKU-101' AND c.name = 'Органайзеры') "
        "OR (p.sku IN ('SKU-102', 'SKU-103') AND c.name = 'Контейнеры и корзины')"
    )


def downgrade() -> None:
    op.drop_constraint("products_category_id_fkey", "products", type_="foreignkey")
    op.drop_column("products", "category_id")
    op.drop_table("categories")
