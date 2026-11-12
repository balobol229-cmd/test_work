"""Источник данных для прототипа: тестовые данные из задания.

Один магазин WB и два ФФ. Начальные количества заводятся обычными приходами,
поэтому они видны в истории и участвуют в сверке, как любые другие операции.

    python -m app.sources.seed          # заполнить пустую базу
    python -m app.sources.seed --reset  # очистить и заполнить заново
"""

from __future__ import annotations

import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core import inventory
from app.db.models import Category, Product, Store, Warehouse
from app.db.session import SessionLocal

STORE = ("Магазин 01", "WB")
WAREHOUSES = ["Север", "Юг"]
PRODUCTS = [
    # артикул, товар, Север, Юг — из задания
    ("SKU-101", "Органайзер", 10, 6),
    ("SKU-102", "Контейнер", 5, 0),
    ("SKU-103", "Корзина", 0, 4),
]
# Категорий в задании нет: они добавлены для уровня просмотра «Категории» из ТЗ Ракеты.
CATEGORIES = {"SKU-101": "Органайзеры", "SKU-102": "Контейнеры и корзины", "SKU-103": "Контейнеры и корзины"}


def reset(s: Session) -> None:
    s.execute(
        text("TRUNCATE movements, documents, stock, products, categories, warehouses, stores RESTART IDENTITY CASCADE")
    )


def seed(s: Session) -> bool:
    """Заполняет базу, если она пустая. Возвращает True, если данные добавлены."""
    if s.scalar(select(Product.id).limit(1)) is not None:
        return False
    store = Store(name=STORE[0], marketplace=STORE[1])
    s.add(store)
    warehouses = [Warehouse(name=name) for name in WAREHOUSES]
    s.add_all(warehouses)
    s.flush()
    categories = {name: Category(name=name) for name in dict.fromkeys(CATEGORIES.values())}
    s.add_all(categories.values())
    s.flush()
    for sku, name, *qty in PRODUCTS:
        product = Product(store_id=store.id, category_id=categories[CATEGORIES[sku]].id, sku=sku, name=name)
        s.add(product)
        s.flush()
        for wh, q in zip(warehouses, qty, strict=True):
            if q:
                inventory.create_receipt(s, product.id, wh.id, q, key=f"seed:{sku}:{wh.name}")
    return True


def main() -> None:
    with SessionLocal.begin() as s:
        if "--reset" in sys.argv:
            reset(s)
        added = seed(s)
    print("Тестовые данные загружены" if added else "В базе уже есть данные, пропускаю")


if __name__ == "__main__":
    main()
