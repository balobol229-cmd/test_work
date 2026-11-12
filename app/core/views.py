"""Расчётное ядро, часть для чтения: экран остатков, история и сверка."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.inventory import ShortageInfo, shortage_info
from app.db.models import (
    DocKind,
    DocStatus,
    Document,
    Loc,
    Movement,
    Product,
    Stock,
    Store,
    Warehouse,
)

# ---------------------------------------------------------------------------
# Экран остатков
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    on_hand: int = 0
    reserved: int = 0

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass
class Row:
    product: Product
    cells: dict[int, Cell]
    transit_ff: int = 0   # в пути между ФФ
    transit_mp: int = 0   # FBO, в пути на склад маркетплейса
    mp: int = 0           # FBO, принято складом маркетплейса
    customer: int = 0     # FBS, отправлено клиенту
    waiting: int = 0      # FBS, ждёт товара: спрос, который сейчас нечем закрыть

    @property
    def on_hand(self) -> int:
        return sum(c.on_hand for c in self.cells.values())

    @property
    def reserved(self) -> int:
        return sum(c.reserved for c in self.cells.values())

    @property
    def available(self) -> int:
        return sum(c.available for c in self.cells.values())

    @property
    def total(self) -> int:
        """Всё, что числится за товаром: на ФФ, в пути, у маркетплейса, у клиента."""
        return self.on_hand + self.transit_ff + self.transit_mp + self.mp + self.customer

    def segments(self, warehouses: list[Warehouse]) -> list[tuple[str, str, int]]:
        """Полоса «где товар»: (css-класс, подпись, шт.) в порядке движения товара."""
        parts = [
            (f"seg-free-{i % 3}", f"Доступно · {w.name}", self.cells[w.id].available)
            for i, w in enumerate(warehouses)
        ]
        parts += [
            ("seg-reserve", "Резерв FBS", self.reserved),
            ("seg-transit", "В пути между ФФ", self.transit_ff),
            ("seg-mp-transit", "FBO в пути", self.transit_mp),
            ("seg-mp", "На складе WB", self.mp),
            ("seg-customer", "К клиенту", self.customer),
        ]
        return [p for p in parts if p[2] > 0]


@dataclass
class Overview:
    store: Store | None
    warehouses: list[Warehouse]  # активные, затем удалённые с остатком
    rows: list[Row]
    in_transit: list[Document]

    def total(self, attr: str) -> int:
        return sum(getattr(r, attr) for r in self.rows)


def _doc_sums(s: Session) -> dict[tuple, int]:
    rows = s.execute(
        select(Document.product_id, Document.kind, Document.status, Document.from_warehouse_id, func.sum(Document.qty))
        .group_by(Document.product_id, Document.kind, Document.status, Document.from_warehouse_id)
    ).all()
    sums: dict[tuple, int] = defaultdict(int)
    for product_id, kind, status, wh_id, qty in rows:
        sums[(product_id, kind, status)] += qty
        sums[(product_id, kind, status, wh_id)] += qty
    return sums


def stock_overview(s: Session) -> Overview:
    products = list(s.scalars(select(Product).options(joinedload(Product.store)).order_by(Product.sku)))
    stocks = s.scalars(select(Stock)).all()
    by_key = {(st.product_id, st.warehouse_id): st for st in stocks}

    all_wh = list(s.scalars(select(Warehouse).order_by(Warehouse.id)))
    archived_with_stock = {st.warehouse_id for st in stocks if st.on_hand > 0}
    warehouses = [w for w in all_wh if w.is_active] + [
        w for w in all_wh if not w.is_active and w.id in archived_with_stock
    ]

    sums = _doc_sums(s)
    rows = []
    for p in products:
        cells = {}
        for w in warehouses:
            st = by_key.get((p.id, w.id))
            cells[w.id] = Cell(st.on_hand, st.reserved) if st else Cell()
        rows.append(
            Row(
                product=p,
                cells=cells,
                transit_ff=sums[(p.id, DocKind.TRANSFER, DocStatus.IN_TRANSIT)],
                transit_mp=sums[(p.id, DocKind.FBO, DocStatus.IN_TRANSIT)],
                mp=sums[(p.id, DocKind.FBO, DocStatus.ACCEPTED)],
                customer=sums[(p.id, DocKind.FBS, DocStatus.SHIPPED)],
                waiting=sums[(p.id, DocKind.FBS, DocStatus.WAITING)],
            )
        )

    in_transit = list(
        s.scalars(
            select(Document)
            .options(joinedload(Document.product), joinedload(Document.from_warehouse), joinedload(Document.to_warehouse))
            .where(Document.kind == DocKind.TRANSFER, Document.status == DocStatus.IN_TRANSIT)
            .order_by(Document.id)
        )
    )
    store = products[0].store if products else s.scalar(select(Store).limit(1))
    return Overview(store, warehouses, rows, in_transit)


# ---------------------------------------------------------------------------
# Операции
# ---------------------------------------------------------------------------


@dataclass
class OpenOrder:
    document: Document
    shortage: ShortageInfo | None = None


@dataclass
class Operations:
    transfers: list[Document]
    fbs: list[OpenOrder]
    fbo: list[Document]
    recent: list[Document]


def operations(s: Session) -> Operations:
    def docs(*where, limit: int | None = None, newest_first: bool = False):
        q = (
            select(Document)
            .options(joinedload(Document.product), joinedload(Document.from_warehouse), joinedload(Document.to_warehouse))
            .where(*where)
            .order_by(Document.id.desc() if newest_first else Document.id)
        )
        if limit:
            q = q.limit(limit)
        return list(s.scalars(q))

    transfers = docs(Document.kind == DocKind.TRANSFER, Document.status == DocStatus.IN_TRANSIT)
    fbo = docs(Document.kind == DocKind.FBO, Document.status == DocStatus.IN_TRANSIT)
    fbs = []
    for d in docs(Document.kind == DocKind.FBS, Document.status.in_([DocStatus.WAITING, DocStatus.RESERVED])):
        info = None
        if d.status == DocStatus.WAITING:
            stocks = {st.warehouse_id: st for st in s.scalars(select(Stock).where(Stock.product_id == d.product_id))}
            info = shortage_info(s, d.from_warehouse, d.qty, stocks)
        fbs.append(OpenOrder(d, info))
    recent = docs(
        Document.status.in_([DocStatus.DONE, DocStatus.RECEIVED, DocStatus.ACCEPTED, DocStatus.SHIPPED, DocStatus.CANCELLED]),
        limit=12,
        newest_first=True,
    )
    return Operations(transfers, fbs, fbo, recent)


# ---------------------------------------------------------------------------
# История
# ---------------------------------------------------------------------------

ACTIONS = {
    "receipt": "Приход",
    "transfer_send": "Отправка перемещения",
    "transfer_receive": "Приёмка перемещения",
    "fbs_reserve": "Резерв FBS",
    "fbs_ship": "Отправка FBS",
    "fbs_cancel": "Отмена FBS",
    "fbo_ship": "Отгрузка FBO",
    "fbo_accept": "Приёмка на складе WB",
    "receipt_cancel": "Удаление прихода",
    "transfer_cancel": "Отмена перемещения",
    "fbo_cancel": "Отмена поставки FBO",
}

PLACES = {
    Loc.SUPPLIER: "Поставщик",
    Loc.TRANSIT_FF: "В пути между ФФ",
    Loc.TRANSIT_MP: "В пути на склад WB",
    Loc.MP: "Склад WB",
    Loc.CUSTOMER: "Клиент",
}


def place_label(loc: str, wh: Warehouse | None) -> str:
    if loc in (Loc.FF, Loc.FF_RESERVE):
        name = wh.name if wh else "?"
        if wh and not wh.is_active:
            name += " (удалён)"
        return f"{name} · резерв" if loc == Loc.FF_RESERVE else name
    return PLACES[Loc(loc)]


def history(s: Session, product_id: int | None = None, limit: int = 300) -> list[Movement]:
    q = (
        select(Movement)
        .options(
            joinedload(Movement.product),
            joinedload(Movement.document),
            joinedload(Movement.from_warehouse),
            joinedload(Movement.to_warehouse),
        )
        .order_by(Movement.id.desc())
        .limit(limit)
    )
    if product_id:
        q = q.where(Movement.product_id == product_id)
    return list(s.scalars(q))


# ---------------------------------------------------------------------------
# Сверка
# ---------------------------------------------------------------------------


@dataclass
class Check:
    title: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class Reconciliation:
    checks: list[Check]
    units: int  # сколько единиц пришло всего и столько же должно где-то числиться

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def reconcile(s: Session) -> Reconciliation:
    """Сверяет три независимых источника: остатки `stock`, журнал движений и документы."""
    skus = dict(s.execute(select(Product.id, Product.sku)).all())
    wh_names = dict(s.execute(select(Warehouse.id, Warehouse.name)).all())

    # Баланс каждого места по журналу: всё, что пришло, минус всё, что ушло.
    ledger: dict[tuple, int] = defaultdict(int)
    for p, fl, fw, tl, tw, qty in s.execute(
        select(
            Movement.product_id, Movement.from_loc, Movement.from_warehouse_id,
            Movement.to_loc, Movement.to_warehouse_id, func.sum(Movement.qty),
        ).group_by(
            Movement.product_id, Movement.from_loc, Movement.from_warehouse_id,
            Movement.to_loc, Movement.to_warehouse_id,
        )
    ):
        ledger[(p, fl, fw)] -= qty
        ledger[(p, tl, tw)] += qty

    stocks = s.scalars(select(Stock)).all()
    sums = _doc_sums(s)

    # 1. Остатки на ФФ совпадают с журналом.
    c1 = Check("Остатки на складах совпадают с журналом движений")
    for st in stocks:
        free = ledger[(st.product_id, Loc.FF, st.warehouse_id)]
        res = ledger[(st.product_id, Loc.FF_RESERVE, st.warehouse_id)]
        if (st.on_hand, st.reserved) != (free + res, res):
            c1.problems.append(
                f"{skus[st.product_id]} на «{wh_names[st.warehouse_id]}»: в остатках "
                f"{st.on_hand}/{st.reserved}, по журналу {free + res}/{res}"
            )

    # 2. Открытые документы совпадают с журналом.
    c2 = Check("Товар в пути, у WB и у клиента совпадает с документами")
    for pid, sku in skus.items():
        for label, loc, kind, status in (
            ("в пути между ФФ", Loc.TRANSIT_FF, DocKind.TRANSFER, DocStatus.IN_TRANSIT),
            ("FBO в пути", Loc.TRANSIT_MP, DocKind.FBO, DocStatus.IN_TRANSIT),
            ("на складе WB", Loc.MP, DocKind.FBO, DocStatus.ACCEPTED),
            ("у клиента", Loc.CUSTOMER, DocKind.FBS, DocStatus.SHIPPED),
        ):
            by_docs, by_ledger = sums[(pid, kind, status)], ledger[(pid, loc, None)]
            if by_docs != by_ledger:
                c2.problems.append(f"{sku} {label}: по документам {by_docs}, по журналу {by_ledger}")
        for st in (x for x in stocks if x.product_id == pid):
            by_docs = sums[(pid, DocKind.FBS, DocStatus.RESERVED, st.warehouse_id)]
            if by_docs != st.reserved:
                c2.problems.append(
                    f"{sku} резерв на «{wh_names[st.warehouse_id]}»: заказов на {by_docs}, в остатках {st.reserved}"
                )

    # 3. Каждая принятая единица где-то числится, и только в одном месте.
    c3 = Check("Каждая принятая единица числится ровно в одном месте")
    units = 0
    for pid, sku in skus.items():
        received = sums[(pid, DocKind.RECEIPT, DocStatus.DONE)]
        units += received
        located = (
            sum(st.on_hand for st in stocks if st.product_id == pid)
            + sums[(pid, DocKind.TRANSFER, DocStatus.IN_TRANSIT)]
            + sums[(pid, DocKind.FBO, DocStatus.IN_TRANSIT)]
            + sums[(pid, DocKind.FBO, DocStatus.ACCEPTED)]
            + sums[(pid, DocKind.FBS, DocStatus.SHIPPED)]
        )
        if received != located:
            c3.problems.append(f"{sku}: принято {received}, числится {located}")

    return Reconciliation([c1, c2, c3], units)
