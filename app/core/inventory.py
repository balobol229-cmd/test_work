"""Расчётное ядро: все операции, которые меняют остатки.

Правила, на которых держится учёт:

1. Остатки меняются только здесь и только через `_move`: одна строка журнала «откуда → куда»
   и одновременно правка `stock`. Единицы не появляются и не исчезают.
2. Каждая операция выполняется в одной транзакции. Вызывающий код открывает её
   (`with SessionLocal.begin() as s`), ядро ничего не коммитит само.
3. Перед изменением остатков блокируем строки `stock` этого товара (`FOR UPDATE`) в порядке
   `warehouse_id`. Две параллельные операции с одним товаром выстраиваются в очередь
   и не продают одну единицу дважды. Порядок блокировок одинаковый, поэтому дедлоков нет.
4. Повтор безопасен. Создание документа с тем же `idempotency_key` возвращает уже
   созданный документ. Переход статуса (отправить, принять, отменить), который уже был
   выполнен, ничего не меняет и возвращает `replayed=True`.
5. Защита последней линии — CHECK-ограничения в базе: остаток и резерв не уходят в минус,
   резерв не больше физического остатка.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import (
    IdempotencyConflict,
    InvalidOperation,
    NotFound,
    Shortage,
    ValidationFailed,
    WarehouseBusy,
)
from app.db.models import DocKind, DocStatus, Document, Loc, Movement, Product, Stock, Warehouse

Place = tuple[Loc, int | None]

# Каждая блокировка (FOR UPDATE / FOR SHARE) перечитывает строку из базы. Без этого SQLAlchemy
# вернул бы объект, уже загруженный раньше в этой транзакции, — то есть состояние ДО того, как
# мы дождались блокировки. Именно так стресс-тест поймал двойное проведение при одновременных
# «Принять» и «Удалить».
_FRESH = {"populate_existing": True}  # место и склад, если место на складе


# ---------------------------------------------------------------------------
# Результаты
# ---------------------------------------------------------------------------


@dataclass
class Option:
    """Вариант решения нехватки, который предлагаем менеджеру."""

    kind: str  # "switch" — взять с другого ФФ, "split" — разделить, "transfer" — довезти
    text: str
    warehouse_id: int | None = None
    plan: list[tuple[int, str, int]] = field(default_factory=list)  # (склад, название, шт.)


@dataclass
class ShortageInfo:
    warehouse_id: int
    warehouse_name: str
    requested: int
    available: int
    total_available: int
    options: list[Option]
    plan: list[tuple[int, str, int]] = field(default_factory=list)  # как собрать заказ, если товара хватает

    @property
    def message(self) -> str:
        if self.total_available >= self.requested:
            return (
                f"На «{self.warehouse_name}» доступно {self.available} из {self.requested} шт., "
                f"но всего на складах есть {self.total_available} шт."
            )
        return (
            f"Не хватает товара: на «{self.warehouse_name}» доступно {self.available} шт., "
            f"на всех складах {self.total_available} из {self.requested} шт."
        )

    def as_dict(self) -> dict:
        return {
            "warehouse_id": self.warehouse_id,
            "warehouse": self.warehouse_name,
            "requested": self.requested,
            "available": self.available,
            "total_available": self.total_available,
            "options": [
                {
                    "kind": o.kind,
                    "text": o.text,
                    "warehouse_id": o.warehouse_id,
                    "plan": [{"warehouse_id": w, "warehouse": n, "qty": q} for w, n, q in o.plan],
                }
                for o in self.options
            ],
        }


@dataclass
class Result:
    document: Document
    replayed: bool = False  # повтор: остатки не менялись
    shortage: ShortageInfo | None = None
    parts: list[Document] = field(default_factory=list)  # документы, созданные при разделении


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


# Колонки количества и id в PostgreSQL — INTEGER (до 2 147 483 647). Значения проверяем
# до базы: иначе огромное число из формы дошло бы до PostgreSQL и вернуло ошибку 500.
MAX_QTY = 1_000_000
_MAX_ID = 2_147_483_647
MAX_TEXT = {"order_ref": 64, "key": 100}


def _check_qty(qty: int) -> None:
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        raise ValidationFailed("Количество должно быть целым числом больше нуля")
    if qty > MAX_QTY:
        raise ValidationFailed(
            f"Слишком большое количество: в одной операции не больше {MAX_QTY:,} шт.".replace(",", " ")
        )


def _check_id(value: int | None, what: str) -> int:
    """Неверный или несуществующий id — это «не найдено», а не ошибка базы."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= _MAX_ID:
        raise NotFound(f"{what} не найден")
    return value


check_id = _check_id


def _check_text(value: str | None, field: str, label: str) -> None:
    if value is not None and len(value) > MAX_TEXT[field]:
        raise ValidationFailed(f"{label} длиннее {MAX_TEXT[field]} символов")


def _product(s: Session, product_id: int) -> Product:
    product = s.get(Product, _check_id(product_id, "Товар"))
    if product is None:
        raise NotFound("Товар не найден")
    return product


def _warehouse(s: Session, warehouse_id: int, *, must_be_active: bool = True) -> Warehouse:
    """Читает склад с блокировкой FOR SHARE: пока операция идёт, склад нельзя удалить."""
    _check_id(warehouse_id, "Склад")
    wh = s.scalar(
        select(Warehouse).where(Warehouse.id == warehouse_id).with_for_update(read=True).execution_options(**_FRESH)
    )
    if wh is None:
        raise NotFound("Склад не найден")
    if must_be_active and not wh.is_active:
        raise InvalidOperation(f"Склад «{wh.name}» удалён. С него можно только вывезти остаток перемещением")
    return wh


def _lock_stock(s: Session, product_id: int) -> dict[int, Stock]:
    """Блокирует остатки товара на всех складах и возвращает их по `warehouse_id`."""
    s.execute(
        pg_insert(Stock)
        .from_select(["product_id", "warehouse_id"], select(literal(product_id), Warehouse.id))
        .on_conflict_do_nothing()
    )
    rows = s.scalars(
        select(Stock)
        .where(Stock.product_id == product_id)
        .order_by(Stock.warehouse_id)
        .with_for_update()
        .execution_options(**_FRESH)
    ).all()
    return {row.warehouse_id: row for row in rows}


def _lock_document(s: Session, doc_id: int, kind: DocKind) -> Document:
    _check_id(doc_id, "Документ")
    doc = s.scalar(
        select(Document)
        .where(Document.id == doc_id, Document.kind == kind)
        .with_for_update()
        .execution_options(**_FRESH)
    )
    if doc is None:
        raise NotFound("Документ не найден")
    return doc


def _by_key(s: Session, key: str | None, kind: DocKind, **params) -> Document | None:
    """Находит документ, уже созданный с этим ключом повтора."""
    if not key:
        return None
    _check_text(key, "key", "Ключ повтора")
    # Два одновременных запроса с одним ключом встанут в очередь здесь.
    s.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))
    doc = s.scalar(select(Document).where(Document.idempotency_key == key))
    if doc is None:
        return None
    if doc.kind != kind or any(getattr(doc, k) != v for k, v in params.items()):
        raise IdempotencyConflict("Этот ключ повтора уже использован для другой операции")
    return doc


def _move(
    s: Session,
    doc: Document,
    action: str,
    qty: int,
    src: Place,
    dst: Place,
    stocks: dict[int, Stock],
) -> None:
    """Переносит `qty` единиц из `src` в `dst`: пишет журнал и правит остатки на ФФ."""
    for (loc, wh_id), sign in ((src, -1), (dst, +1)):
        if loc in (Loc.FF, Loc.FF_RESERVE):
            row = stocks[wh_id]
            row.on_hand += sign * qty
            if loc == Loc.FF_RESERVE:
                row.reserved += sign * qty
    s.add(
        Movement(
            document=doc,
            action=action,
            product_id=doc.product_id,
            qty=qty,
            from_loc=src[0],
            from_warehouse_id=src[1],
            to_loc=dst[0],
            to_warehouse_id=dst[1],
        )
    )


def _active_warehouses(s: Session) -> list[Warehouse]:
    return list(s.scalars(select(Warehouse).where(Warehouse.archived_at.is_(None)).order_by(Warehouse.id)))


def shortage_info(s: Session, wh: Warehouse, qty: int, stocks: dict[int, Stock]) -> ShortageInfo:
    """Считает, чего не хватает на `wh`, и какие есть варианты на других активных ФФ."""
    actives = _active_warehouses(s)
    avail = {w.id: (stocks[w.id].available if w.id in stocks else 0) for w in actives}
    here = avail.get(wh.id, 0)
    total = sum(avail.values())
    others = sorted((w for w in actives if w.id != wh.id), key=lambda w: -avail[w.id])

    options: list[Option] = []
    plan: list[tuple[int, str, int]] = []
    for w in others:
        if avail[w.id] >= qty:
            options.append(Option("switch", f"Взять все {qty} шт. с «{w.name}»", warehouse_id=w.id))
    if total >= qty:
        plan = _split_plan(wh, here, others, avail, qty)
        if len(plan) > 1:
            parts = " + ".join(f"{q} с «{n}»" for _, n, q in plan)
            options.append(Option("split", f"Разделить: {parts}", plan=plan))
        missing = qty - here
        moves: list[tuple[int, str, int]] = []
        for w in others:
            if missing <= 0:
                break
            take = min(avail[w.id], missing)
            if take > 0:
                moves.append((w.id, w.name, take))
                missing -= take
        if moves:
            parts = ", ".join(f"{q} шт. с «{n}»" for _, n, q in moves)
            options.append(Option("transfer", f"Переместить на «{wh.name}»: {parts}", plan=moves))

    return ShortageInfo(wh.id, wh.name, qty, here, total, options, plan)


def _split_plan(
    wh: Warehouse, here: int, others: list[Warehouse], avail: dict[int, int], qty: int
) -> list[tuple[int, str, int]]:
    """Сначала берём всё, что есть на выбранном ФФ, остальное — с самых полных складов."""
    plan: list[tuple[int, str, int]] = []
    left = qty
    if here > 0:
        take = min(here, left)
        plan.append((wh.id, wh.name, take))
        left -= take
    for w in others:
        if left <= 0:
            break
        take = min(avail[w.id], left)
        if take > 0:
            plan.append((w.id, w.name, take))
            left -= take
    return plan


def _require_available(
    s: Session, wh: Warehouse, qty: int, stocks: dict[int, Stock], *, offer_options: bool = True
) -> None:
    if stocks[wh.id].available >= qty:
        return
    info = shortage_info(s, wh, qty, stocks)
    if not offer_options:
        info.options = []
    raise Shortage(info.message, shortage=info)


# ---------------------------------------------------------------------------
# Склады ФФ
# ---------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationFailed("Введите название склада")
    if len(name) > 100:
        raise ValidationFailed("Название склада длиннее 100 символов")
    return name


def _ensure_unique_name(s: Session, name: str, exclude_id: int | None = None) -> None:
    query = select(Warehouse.id).where(Warehouse.archived_at.is_(None), func.lower(Warehouse.name) == name.lower())
    if exclude_id is not None:
        query = query.where(Warehouse.id != exclude_id)
    if s.scalar(query) is not None:
        raise ValidationFailed(f"Склад «{name}» уже есть")


def create_warehouse(s: Session, name: str) -> Warehouse:
    name = _clean_name(name)
    _ensure_unique_name(s, name)
    wh = Warehouse(name=name)
    s.add(wh)
    s.flush()
    return wh


def rename_warehouse(s: Session, warehouse_id: int, name: str) -> Warehouse:
    name = _clean_name(name)
    wh = _warehouse(s, warehouse_id)
    _ensure_unique_name(s, name, exclude_id=wh.id)
    wh.name = name
    s.flush()
    return wh


@dataclass
class ArchiveResult:
    warehouse: Warehouse
    replayed: bool = False
    left_on_hand: list[tuple[str, int]] = field(default_factory=list)  # (артикул, шт.)


def archive_warehouse(s: Session, warehouse_id: int) -> ArchiveResult:
    """Удаляет склад мягко. Остатки и история остаются и видны на экране остатков.

    Нельзя удалить склад, на который едет перемещение, на котором есть резерв FBS или
    заказ, ожидающий товара: эти операции некуда было бы завершить.
    """
    _check_id(warehouse_id, "Склад")
    wh = s.scalar(select(Warehouse).where(Warehouse.id == warehouse_id).with_for_update().execution_options(**_FRESH))
    if wh is None:
        raise NotFound("Склад не найден")
    if not wh.is_active:
        return ArchiveResult(wh, replayed=True)

    blockers: list[str] = []
    reserved = s.scalar(select(func.coalesce(func.sum(Stock.reserved), 0)).where(Stock.warehouse_id == wh.id))
    if reserved:
        blockers.append(f"в резерве FBS {reserved} шт. — отправьте или отмените заказы")
    incoming = s.scalar(
        select(func.coalesce(func.sum(Document.qty), 0)).where(
            Document.kind == DocKind.TRANSFER,
            Document.status == DocStatus.IN_TRANSIT,
            Document.to_warehouse_id == wh.id,
        )
    )
    if incoming:
        blockers.append(f"к складу едет перемещение на {incoming} шт. — сначала примите его")
    waiting = s.scalar(
        select(func.count()).where(
            Document.kind == DocKind.FBS,
            Document.status == DocStatus.WAITING,
            Document.from_warehouse_id == wh.id,
        )
    )
    if waiting:
        blockers.append(f"заказов FBS ждут товара: {waiting} — перенесите их на другой склад")
    if blockers:
        raise WarehouseBusy(f"Склад «{wh.name}» нельзя удалить: " + "; ".join(blockers), blockers=blockers)

    wh.archived_at = datetime.now(UTC)
    left = s.execute(
        select(Product.sku, Stock.on_hand)
        .join(Product, Product.id == Stock.product_id)
        .where(Stock.warehouse_id == wh.id, Stock.on_hand > 0)
        .order_by(Product.sku)
    ).all()
    s.flush()
    return ArchiveResult(wh, left_on_hand=[(sku, qty) for sku, qty in left])


# ---------------------------------------------------------------------------
# Приход
# ---------------------------------------------------------------------------


def create_receipt(s: Session, product_id: int, warehouse_id: int, qty: int, *, key: str | None = None) -> Result:
    _check_qty(qty)
    if doc := _by_key(s, key, DocKind.RECEIPT, product_id=product_id, to_warehouse_id=warehouse_id, qty=qty):
        return Result(doc, replayed=True)
    _product(s, product_id)
    wh = _warehouse(s, warehouse_id)
    stocks = _lock_stock(s, product_id)

    doc = Document(
        kind=DocKind.RECEIPT,
        status=DocStatus.DONE,
        product_id=product_id,
        qty=qty,
        to_warehouse_id=wh.id,
        idempotency_key=key,
    )
    s.add(doc)
    _move(s, doc, "receipt", qty, (Loc.SUPPLIER, None), (Loc.FF, wh.id), stocks)
    s.flush()
    return Result(doc)


def cancel_receipt(s: Session, doc_id: int) -> Result:
    """Удаляет ошибочный приход обратной записью: товар уходит со склада обратно поставщику.

    Строки журнала не стираются — иначе остатки разошлись бы с историей. Удалить можно,
    только пока весь приход ещё свободен на складе: проданное или зарезервированное
    вернуть поставщику нельзя.
    """
    doc = _lock_document(s, doc_id, DocKind.RECEIPT)
    if doc.status == DocStatus.CANCELLED:
        return Result(doc, replayed=True)
    stocks = _lock_stock(s, doc.product_id)
    free = stocks[doc.to_warehouse_id].available
    if free < doc.qty:
        raise InvalidOperation(
            f"Приход нельзя удалить: на складе свободно {free} из {doc.qty} шт., "
            "остальное уже в резерве, в пути или отгружено"
        )
    _move(s, doc, "receipt_cancel", doc.qty, (Loc.FF, doc.to_warehouse_id), (Loc.SUPPLIER, None), stocks)
    doc.status = DocStatus.CANCELLED
    s.flush()
    return Result(doc)


# ---------------------------------------------------------------------------
# Перемещение между ФФ: отправка → в пути → приёмка
# ---------------------------------------------------------------------------


def create_transfer(
    s: Session,
    product_id: int,
    from_warehouse_id: int,
    to_warehouse_id: int,
    qty: int,
    *,
    key: str | None = None,
) -> Result:
    _check_qty(qty)
    if from_warehouse_id == to_warehouse_id:
        raise ValidationFailed("Склад отправки и склад приёмки совпадают")
    if doc := _by_key(
        s,
        key,
        DocKind.TRANSFER,
        product_id=product_id,
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        qty=qty,
    ):
        return Result(doc, replayed=True)
    _product(s, product_id)
    # С удалённого склада вывезти остаток можно, привезти на него — нельзя.
    src = _warehouse(s, from_warehouse_id, must_be_active=False)
    dst = _warehouse(s, to_warehouse_id)
    stocks = _lock_stock(s, product_id)
    _require_available(s, src, qty, stocks, offer_options=False)

    doc = Document(
        kind=DocKind.TRANSFER,
        status=DocStatus.IN_TRANSIT,
        product_id=product_id,
        qty=qty,
        from_warehouse_id=src.id,
        to_warehouse_id=dst.id,
        idempotency_key=key,
    )
    s.add(doc)
    _move(s, doc, "transfer_send", qty, (Loc.FF, src.id), (Loc.TRANSIT_FF, None), stocks)
    s.flush()
    return Result(doc)


def receive_transfer(s: Session, doc_id: int) -> Result:
    doc = _lock_document(s, doc_id, DocKind.TRANSFER)
    if doc.status == DocStatus.RECEIVED:
        return Result(doc, replayed=True)
    if doc.status != DocStatus.IN_TRANSIT:
        raise InvalidOperation("Это перемещение нельзя принять")
    stocks = _lock_stock(s, doc.product_id)
    _move(s, doc, "transfer_receive", doc.qty, (Loc.TRANSIT_FF, None), (Loc.FF, doc.to_warehouse_id), stocks)
    doc.status = DocStatus.RECEIVED
    s.flush()
    return Result(doc)


def cancel_transfer(s: Session, doc_id: int) -> Result:
    """Отменяет перемещение в пути: товар возвращается на склад отправки."""
    doc = _lock_document(s, doc_id, DocKind.TRANSFER)
    if doc.status == DocStatus.CANCELLED:
        return Result(doc, replayed=True)
    if doc.status != DocStatus.IN_TRANSIT:
        raise InvalidOperation("Перемещение уже принято. Чтобы вернуть товар, оформите обратное перемещение")
    stocks = _lock_stock(s, doc.product_id)
    _move(s, doc, "transfer_cancel", doc.qty, (Loc.TRANSIT_FF, None), (Loc.FF, doc.from_warehouse_id), stocks)
    doc.status = DocStatus.CANCELLED
    s.flush()
    return Result(doc)


# ---------------------------------------------------------------------------
# FBS: резерв на ФФ → отправка клиенту или отмена
# ---------------------------------------------------------------------------


def create_fbs_order(
    s: Session,
    product_id: int,
    warehouse_id: int,
    qty: int,
    *,
    order_ref: str | None = None,
    key: str | None = None,
) -> Result:
    """Создаёт заказ FBS и сразу резервирует товар на выбранном ФФ.

    Если на этом ФФ не хватает, заказ не теряется: он сохраняется со статусом «ждёт товара»,
    а менеджер получает варианты — взять с другого ФФ, разделить или довезти. Это тот же
    принцип, что в ТЗ Ракеты: нехватка не превращает спрос в ноль.
    """
    _check_qty(qty)
    order_ref = (order_ref or "").strip() or None
    _check_text(order_ref, "order_ref", "Номер заказа")
    # Склад и количество заказа могут измениться при переносе или разделении,
    # поэтому повтор узнаём по товару.
    if doc := _by_key(s, key, DocKind.FBS, product_id=product_id):
        return Result(doc, replayed=True)
    _product(s, product_id)
    wh = _warehouse(s, warehouse_id)
    stocks = _lock_stock(s, product_id)

    doc = Document(
        kind=DocKind.FBS,
        status=DocStatus.WAITING,
        product_id=product_id,
        qty=qty,
        from_warehouse_id=wh.id,
        order_ref=order_ref,
        idempotency_key=key,
    )
    s.add(doc)
    if stocks[wh.id].available >= qty:
        _reserve(s, doc, stocks)
        s.flush()
        return Result(doc)
    s.flush()
    return Result(doc, shortage=shortage_info(s, wh, qty, stocks))


def _reserve(s: Session, doc: Document, stocks: dict[int, Stock]) -> None:
    wh_id = doc.from_warehouse_id
    _move(s, doc, "fbs_reserve", doc.qty, (Loc.FF, wh_id), (Loc.FF_RESERVE, wh_id), stocks)
    doc.status = DocStatus.RESERVED


def _waiting_order(s: Session, doc_id: int) -> tuple[Document, bool]:
    doc = _lock_document(s, doc_id, DocKind.FBS)
    if doc.status == DocStatus.RESERVED:
        return doc, True
    if doc.status != DocStatus.WAITING:
        raise InvalidOperation("Заказ уже отправлен или отменён")
    return doc, False


def reserve_fbs(s: Session, doc_id: int, *, warehouse_id: int | None = None) -> Result:
    """Повторяет резерв для заказа, который ждёт товара. Можно сменить склад."""
    doc, already = _waiting_order(s, doc_id)
    if already:
        return Result(doc, replayed=True)
    wh = _warehouse(s, warehouse_id or doc.from_warehouse_id)
    stocks = _lock_stock(s, doc.product_id)
    _require_available(s, wh, doc.qty, stocks)
    doc.from_warehouse_id = wh.id
    _reserve(s, doc, stocks)
    s.flush()
    return Result(doc)


def split_fbs(s: Session, doc_id: int) -> Result:
    """Делит заказ между складами и резервирует все части сразу, в одной транзакции."""
    doc, already = _waiting_order(s, doc_id)
    if already:
        return Result(doc, replayed=True)
    wh = _warehouse(s, doc.from_warehouse_id)
    stocks = _lock_stock(s, doc.product_id)
    info = shortage_info(s, wh, doc.qty, stocks)
    if info.available >= doc.qty:
        _reserve(s, doc, stocks)
        s.flush()
        return Result(doc)
    if not info.plan:
        raise Shortage(info.message, shortage=info)

    # План может состоять из одной части: на выбранном ФФ ноль, а другой склад закрывает
    # заказ целиком. Тогда «разделить» = собрать заказ там, где товар есть.
    (first_wh, _, first_qty), *rest = info.plan
    doc.qty = first_qty
    doc.from_warehouse_id = first_wh
    _reserve(s, doc, stocks)
    parts = []
    for wh_id, _, qty in rest:
        _warehouse(s, wh_id)
        part = Document(
            kind=DocKind.FBS,
            status=DocStatus.WAITING,
            product_id=doc.product_id,
            qty=qty,
            from_warehouse_id=wh_id,
            order_ref=doc.order_ref,
            parent_id=doc.id,
        )
        s.add(part)
        _reserve(s, part, stocks)
        parts.append(part)
    s.flush()
    return Result(doc, parts=parts)


def ship_fbs(s: Session, doc_id: int) -> Result:
    doc = _lock_document(s, doc_id, DocKind.FBS)
    if doc.status == DocStatus.SHIPPED:
        return Result(doc, replayed=True)
    if doc.status != DocStatus.RESERVED:
        raise InvalidOperation("Отправить можно только зарезервированный заказ")
    stocks = _lock_stock(s, doc.product_id)
    wh_id = doc.from_warehouse_id
    _move(s, doc, "fbs_ship", doc.qty, (Loc.FF_RESERVE, wh_id), (Loc.CUSTOMER, None), stocks)
    doc.status = DocStatus.SHIPPED
    s.flush()
    return Result(doc)


def cancel_fbs(s: Session, doc_id: int) -> Result:
    doc = _lock_document(s, doc_id, DocKind.FBS)
    if doc.status == DocStatus.CANCELLED:
        return Result(doc, replayed=True)
    if doc.status == DocStatus.SHIPPED:
        raise InvalidOperation("Заказ уже отправлен клиенту, отменить его нельзя")
    if doc.status == DocStatus.RESERVED:
        stocks = _lock_stock(s, doc.product_id)
        wh_id = doc.from_warehouse_id
        _move(s, doc, "fbs_cancel", doc.qty, (Loc.FF_RESERVE, wh_id), (Loc.FF, wh_id), stocks)
    doc.status = DocStatus.CANCELLED
    s.flush()
    return Result(doc)


# ---------------------------------------------------------------------------
# FBO: отгрузка с ФФ → в пути → приёмка складом маркетплейса
# ---------------------------------------------------------------------------


def create_fbo_shipment(s: Session, product_id: int, warehouse_id: int, qty: int, *, key: str | None = None) -> Result:
    _check_qty(qty)
    if doc := _by_key(s, key, DocKind.FBO, product_id=product_id, from_warehouse_id=warehouse_id, qty=qty):
        return Result(doc, replayed=True)
    _product(s, product_id)
    wh = _warehouse(s, warehouse_id)
    stocks = _lock_stock(s, product_id)
    _require_available(s, wh, qty, stocks)

    doc = Document(
        kind=DocKind.FBO,
        status=DocStatus.IN_TRANSIT,
        product_id=product_id,
        qty=qty,
        from_warehouse_id=wh.id,
        idempotency_key=key,
    )
    s.add(doc)
    _move(s, doc, "fbo_ship", qty, (Loc.FF, wh.id), (Loc.TRANSIT_MP, None), stocks)
    s.flush()
    return Result(doc)


def accept_fbo(s: Session, doc_id: int) -> Result:
    doc = _lock_document(s, doc_id, DocKind.FBO)
    if doc.status == DocStatus.ACCEPTED:
        return Result(doc, replayed=True)
    if doc.status != DocStatus.IN_TRANSIT:
        raise InvalidOperation("Эту поставку нельзя принять")
    stocks = _lock_stock(s, doc.product_id)
    _move(s, doc, "fbo_accept", doc.qty, (Loc.TRANSIT_MP, None), (Loc.MP, None), stocks)
    doc.status = DocStatus.ACCEPTED
    s.flush()
    return Result(doc)


def cancel_fbo(s: Session, doc_id: int) -> Result:
    """Отменяет поставку FBO в пути: товар возвращается на ФФ, с которого уехал."""
    doc = _lock_document(s, doc_id, DocKind.FBO)
    if doc.status == DocStatus.CANCELLED:
        return Result(doc, replayed=True)
    if doc.status != DocStatus.IN_TRANSIT:
        raise InvalidOperation("Поставку уже принял склад WB, отменить её нельзя")
    stocks = _lock_stock(s, doc.product_id)
    _move(s, doc, "fbo_cancel", doc.qty, (Loc.TRANSIT_MP, None), (Loc.FF, doc.from_warehouse_id), stocks)
    doc.status = DocStatus.CANCELLED
    s.flush()
    return Result(doc)


# ---------------------------------------------------------------------------
# Удаление документа из интерфейса
# ---------------------------------------------------------------------------

_CANCEL = {
    DocKind.RECEIPT: cancel_receipt,
    DocKind.TRANSFER: cancel_transfer,
    DocKind.FBS: cancel_fbs,
    DocKind.FBO: cancel_fbo,
}

# В каких статусах документ ещё можно удалить.
DELETABLE = {
    DocKind.RECEIPT: {DocStatus.DONE},
    DocKind.TRANSFER: {DocStatus.IN_TRANSIT},
    DocKind.FBS: {DocStatus.WAITING, DocStatus.RESERVED},
    DocKind.FBO: {DocStatus.IN_TRANSIT},
}


def can_delete(doc: Document) -> bool:
    return doc.status in DELETABLE[DocKind(doc.kind)]


def delete_document(s: Session, doc_id: int) -> Result:
    """«Удалить» в интерфейсе — это отмена операции обратным движением, а не стирание строк."""
    doc = s.get(Document, _check_id(doc_id, "Документ"))
    if doc is None:
        raise NotFound("Документ не найден")
    return _CANCEL[DocKind(doc.kind)](s, doc_id)
