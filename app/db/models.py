"""Единая база: магазины, товары, склады ФФ, остатки, документы и журнал движений.

Как устроен учёт:
- `documents` — что произошло с точки зрения бизнеса (приход, перемещение, заказ FBS, поставка FBO)
  и в каком статусе это сейчас;
- `movements` — журнал движений «откуда → куда, сколько». Каждое действие пишет одну строку,
  единицы не появляются и не исчезают: они переходят между местами (`Loc`);
- `stock` — текущий остаток на складе ФФ (физически и в резерве). Это материализованная сумма
  журнала: её удобно блокировать (`SELECT ... FOR UPDATE`) и проверять CHECK-ограничениями.

Сверка (`app.core.views.reconcile`) проверяет, что эти три источника сходятся.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DocKind(StrEnum):
    RECEIPT = "receipt"  # приход от поставщика на ФФ
    TRANSFER = "transfer"  # перемещение между ФФ
    FBS = "fbs"  # заказ FBS: резерв на ФФ, затем отправка клиенту
    FBO = "fbo"  # поставка FBO на склад маркетплейса


class DocStatus(StrEnum):
    DONE = "done"  # приход проведён
    IN_TRANSIT = "in_transit"  # перемещение или FBO в пути
    RECEIVED = "received"  # перемещение принято на ФФ-получателе
    ACCEPTED = "accepted"  # FBO принята складом маркетплейса
    WAITING = "waiting"  # FBS: на выбранном ФФ не хватило, спрос сохранён
    RESERVED = "reserved"  # FBS: товар заблокирован на ФФ
    SHIPPED = "shipped"  # FBS: отправлен, едет к клиенту
    CANCELLED = "cancelled"  # FBS: отменён


class Loc(StrEnum):
    """Места, между которыми двигаются единицы товара."""

    SUPPLIER = "supplier"  # вне системы: откуда пришёл приход
    FF = "ff"  # на складе ФФ, свободно
    FF_RESERVE = "ff_reserve"  # на складе ФФ, в резерве FBS
    TRANSIT_FF = "transit_ff"  # в пути между ФФ
    TRANSIT_MP = "transit_mp"  # FBO: в пути на склад маркетплейса
    MP = "mp"  # FBO: принято складом маркетплейса
    CUSTOMER = "customer"  # FBS: отправлено клиенту


def _in(column: str, enum: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(e.value) for e in enum)})"


class Store(Base):
    """Магазин (кабинет) маркетплейса. Товары разных магазинов не смешиваются."""

    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("marketplace", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    marketplace: Mapped[str] = mapped_column(String(20))  # WB; Ozon и Яндекс Маркет — вторая очередь


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("store_id", "sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))

    store: Mapped[Store] = relationship()


class Warehouse(Base):
    """Склад фулфилмента. Удаление мягкое: `archived_at`, остатки и история остаются."""

    __tablename__ = "warehouses"
    __table_args__ = (
        Index(
            "uq_warehouses_active_name",
            "name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_active(self) -> bool:
        return self.archived_at is None


class Stock(Base):
    __tablename__ = "stock"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        CheckConstraint("reserved <= on_hand", name="reserved_within_on_hand"),
    )

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(default=0, server_default="0")  # физически на складе
    reserved: Mapped[int] = mapped_column(default=0, server_default="0")  # из них в резерве FBS

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint(_in("kind", DocKind), name="kind_valid"),
        CheckConstraint(_in("status", DocStatus), name="status_valid"),
        Index("ix_documents_open", "kind", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int]
    from_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    order_ref: Mapped[str | None] = mapped_column(String(64))  # номер заказа маркетплейса
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))  # часть разделённого заказа
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    product: Mapped[Product] = relationship()
    from_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[to_warehouse_id])


class Movement(Base):
    """Журнал движений. Только добавление: строки не меняются и не удаляются."""

    __tablename__ = "movements"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint(_in("from_loc", Loc), name="from_loc_valid"),
        CheckConstraint(_in("to_loc", Loc), name="to_loc_valid"),
        Index("ix_movements_product", "product_id"),
        Index("ix_movements_document", "document_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    action: Mapped[str] = mapped_column(String(32))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int]
    from_loc: Mapped[str] = mapped_column(String(16))
    from_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    to_loc: Mapped[str] = mapped_column(String(16))
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship()
    product: Mapped[Product] = relationship()
    from_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[to_warehouse_id])
