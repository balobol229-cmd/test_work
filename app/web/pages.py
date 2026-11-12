"""Веб-интерфейс: серверные HTML-страницы и формы. Без JS-сборки, всё рендерит сервер.

Каждая форма — POST → операция ядра в одной транзакции → redirect с сообщением.
В формы создания зашит одноразовый ключ повтора: двойной клик не проведёт операцию дважды.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import inventory as inv
from app.core import views
from app.core.errors import DomainError, Shortage
from app.db.models import Document, Product, Stock, Store, Warehouse
from app.db.session import SessionLocal
from app.sources.seed import reset, seed
from app.web import flash

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")
templates.env.globals["new_key"] = lambda: uuid4().hex
templates.env.filters["n"] = lambda v: f"{v:,}".replace(",", " ") if isinstance(v, int) else v
templates.env.globals["place"] = views.place_label
templates.env.globals["ACTIONS"] = views.ACTIONS
templates.env.globals["can_delete"] = inv.can_delete
# Версия стилей по времени изменения файла: браузер не покажет устаревший CSS после обновления.
templates.env.globals["css_version"] = int((Path(__file__).parent.parent / "static" / "style.css").stat().st_mtime)

REPLAYED = "Повтор: эта операция уже проведена, остатки не изменились"


def _render(request: Request, name: str, page: str, **ctx) -> HTMLResponse:
    with SessionLocal() as s:
        rec = views.reconcile(s)
        if "store" not in ctx:
            ctx["store"] = s.scalar(select(Store).order_by(Store.id).limit(1))
    response = templates.TemplateResponse(
        request, name, {"page": page, "rec": rec, "flash": flash.pop(request), **ctx}
    )
    response.delete_cookie(flash.COOKIE)
    return response


def _refs(s: Session) -> dict:
    return {
        "products": list(s.scalars(select(Product).order_by(Product.sku))),
        "active_warehouses": list(
            s.scalars(select(Warehouse).where(Warehouse.archived_at.is_(None)).order_by(Warehouse.id))
        ),
        "all_warehouses": list(s.scalars(select(Warehouse).order_by(Warehouse.id))),
    }


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _act(
    back: str,
    fn: Callable,
    *args,
    done: Callable[[Session, inv.Result], tuple[str, str]],
    retry: dict | None = None,
    **kwargs,
):
    """Выполняет операцию и возвращает redirect с сообщением о результате.

    `retry` — что нужно, чтобы кнопки вариантов при нехватке повторили операцию.
    """
    try:
        with SessionLocal.begin() as s:
            res = fn(s, *args, **kwargs)
            if getattr(res, "replayed", False):
                kind, text = "info", REPLAYED
            else:
                kind, text = done(s, res)
    except Shortage as e:
        info: inv.ShortageInfo = e.details["shortage"]
        return flash.redirect(back, "error", e.message, shortage=info.as_dict(), retry=retry)
    except DomainError as e:
        return flash.redirect(back, "error", e.message)
    return flash.redirect(back, kind, text)


def _what(d: Document) -> str:
    return f"{d.product.sku} · {d.qty} шт."


# --- Остатки ----------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def stock_page(request: Request):
    with SessionLocal() as s:
        overview = views.stock_overview(s)
    return _render(request, "stock.html", "stock", o=overview, store=overview.store)


@router.get("/check", response_class=HTMLResponse)
def check_page(request: Request):
    return _render(request, "check.html", "check")


# --- Операции ---------------------------------------------------------------


@router.get("/operations", response_class=HTMLResponse)
def operations_page(request: Request):
    with SessionLocal() as s:
        ops = views.operations(s)
        refs = _refs(s)
        available = {
            (st.product_id, st.warehouse_id): st.available for st in s.scalars(select(Stock))
        }
    return _render(request, "operations.html", "operations", ops=ops, available=available, **refs)


@router.post("/operations/receipt")
def post_receipt(product_id: str = Form(), warehouse_id: str = Form(), qty: str = Form(), key: str = Form()):
    def done(s, r):
        return "ok", f"Приход проведён: {_what(r.document)} на «{r.document.to_warehouse.name}»"

    return _act("/operations", inv.create_receipt, _int(product_id), _int(warehouse_id), _int(qty), key=key, done=done)


@router.post("/operations/transfer")
def post_transfer(
    product_id: str = Form(), from_id: str = Form(), to_id: str = Form(), qty: str = Form(), key: str = Form()
):
    def done(s, r):
        d = r.document
        return "ok", (
            f"Перемещение отправлено: {_what(d)}, «{d.from_warehouse.name}» → «{d.to_warehouse.name}». "
            "Товар числится в пути до приёмки"
        )

    return _act(
        "/operations", inv.create_transfer, _int(product_id), _int(from_id), _int(to_id), _int(qty), key=key, done=done
    )


@router.post("/operations/transfer/{doc_id}/receive")
def post_transfer_receive(doc_id: int):
    def done(s, r):
        return "ok", f"Перемещение принято на «{r.document.to_warehouse.name}»: {_what(r.document)}"

    return _act("/operations", inv.receive_transfer, doc_id, done=done)


def _fbs_done(s: Session, r: inv.Result) -> tuple[str, str]:
    d = r.document
    name = f"Заказ {d.order_ref}" if d.order_ref else f"Заказ №{d.id}"
    if r.shortage:
        return "warn", f"{name} ждёт товара. {r.shortage.message} Выберите, как его собрать"
    if r.parts:
        parts = ", ".join(f"{p.qty} шт. с «{p.from_warehouse.name}»" for p in [d, *r.parts])
        return "ok", f"{name} разделён и зарезервирован: {parts}"
    return "ok", f"{name} зарезервирован: {_what(d)} на «{d.from_warehouse.name}»"


@router.post("/operations/fbs")
def post_fbs(
    product_id: str = Form(), warehouse_id: str = Form(), qty: str = Form(), key: str = Form(),
    order_ref: str = Form(""),
):
    return _act(
        "/operations", inv.create_fbs_order, _int(product_id), _int(warehouse_id), _int(qty),
        order_ref=order_ref, key=key, done=_fbs_done,
    )


@router.post("/operations/fbs/{doc_id}/reserve")
def post_fbs_reserve(doc_id: int, warehouse_id: str = Form("")):
    return _act("/operations", inv.reserve_fbs, doc_id, warehouse_id=_int(warehouse_id), done=_fbs_done)


@router.post("/operations/fbs/{doc_id}/split")
def post_fbs_split(doc_id: int):
    return _act("/operations", inv.split_fbs, doc_id, done=_fbs_done)


@router.post("/operations/fbs/{doc_id}/ship")
def post_fbs_ship(doc_id: int):
    def done(s, r):
        return "ok", f"Заказ отправлен клиенту: {_what(r.document)} с «{r.document.from_warehouse.name}»"

    return _act("/operations", inv.ship_fbs, doc_id, done=done)


_DELETED = {
    "receipt": "Приход удалён: {what} списано с «{wh}» обратной записью. В истории видны обе записи",
    "transfer": "Перемещение отменено: {what} вернулось на «{wh}»",
    "fbs": "Заказ удалён, резерв снят: {what} снова доступно на «{wh}»",
    "fbo": "Поставка FBO отменена: {what} вернулось на «{wh}»",
}


@router.post("/documents/{doc_id}/delete")
def post_delete_document(doc_id: int, back: str = Form("/operations")):
    """«Удалить» = отменить операцию обратным движением. Строки истории не стираются."""
    back = back if back.startswith("/") and not back.startswith("//") else "/operations"

    def done(s, r):
        d = r.document
        wh = d.to_warehouse if d.kind == "receipt" else d.from_warehouse
        return "ok", _DELETED[d.kind].format(what=_what(d), wh=wh.name if wh else "?")

    return _act(back, inv.delete_document, doc_id, done=done)


@router.post("/reset")
def post_reset():
    """Возвращает базу к тестовым данным из задания. Для демонстрации и записи видео."""
    with SessionLocal.begin() as s:
        reset(s)
        seed(s)
    return flash.redirect("/", "ok", "Данные сброшены к тестовым из задания: 25 шт. на двух складах")


@router.post("/operations/fbo")
def post_fbo(product_id: str = Form(), warehouse_id: str = Form(), qty: str = Form(), key: str = Form()):
    def done(s, r):
        return "ok", f"Поставка FBO отгружена: {_what(r.document)} с «{r.document.from_warehouse.name}», в пути на склад WB"

    # Для FBO нехватка — отказ: поставку собирают с одного склада. Варианты придут
    # в сообщении кнопками; у каждой кнопки свой новый ключ, это уже другая операция.
    pid, wid, q = _int(product_id), _int(warehouse_id), _int(qty)
    return _act(
        "/operations", inv.create_fbo_shipment, pid, wid, q, key=key, done=done,
        retry={"product_id": pid, "qty": q},
    )


@router.post("/operations/fbo/{doc_id}/accept")
def post_fbo_accept(doc_id: int):
    def done(s, r):
        return "ok", f"Поставка принята складом WB: {_what(r.document)}"

    return _act("/operations", inv.accept_fbo, doc_id, done=done)


# --- Склады -----------------------------------------------------------------


@router.get("/warehouses", response_class=HTMLResponse)
def warehouses_page(request: Request):
    with SessionLocal() as s:
        overview = views.stock_overview(s)
        refs = _refs(s)
        units = {
            w.id: sum(r.cells[w.id].on_hand for r in overview.rows if w.id in r.cells) for w in refs["all_warehouses"]
        }
        reserved = {
            w.id: sum(r.cells[w.id].reserved for r in overview.rows if w.id in r.cells) for w in refs["all_warehouses"]
        }
    return _render(request, "warehouses.html", "warehouses", units=units, reserved=reserved, **refs)


@router.post("/warehouses")
def post_warehouse(name: str = Form("")):
    try:
        with SessionLocal.begin() as s:
            wh = inv.create_warehouse(s, name)
            text = f"Склад «{wh.name}» добавлен"
    except DomainError as e:
        return flash.redirect("/warehouses", "error", e.message)
    return flash.redirect("/warehouses", "ok", text)


@router.post("/warehouses/{warehouse_id}/rename")
def post_rename(warehouse_id: int, name: str = Form("")):
    try:
        with SessionLocal.begin() as s:
            old = s.get(Warehouse, warehouse_id)
            old_name = old.name if old else ""
            wh = inv.rename_warehouse(s, warehouse_id, name)
            text = f"«{old_name}» переименован в «{wh.name}». История осталась при складе"
    except DomainError as e:
        return flash.redirect("/warehouses", "error", e.message)
    return flash.redirect("/warehouses", "ok", text)


@router.post("/warehouses/{warehouse_id}/delete")
def post_delete(warehouse_id: int):
    try:
        with SessionLocal.begin() as s:
            res = inv.archive_warehouse(s, warehouse_id)
            name = res.warehouse.name
    except DomainError as e:
        return flash.redirect("/warehouses", "error", e.message)
    if res.replayed:
        return flash.redirect("/warehouses", "info", f"Склад «{name}» уже удалён")
    if res.left_on_hand:
        left = ", ".join(f"{sku} · {q} шт." for sku, q in res.left_on_hand)
        return flash.redirect(
            "/warehouses", "warn",
            f"Склад «{name}» удалён. На нём числится {left}: остаток виден на экране остатков, "
            "вывезти его можно перемещением",
        )
    return flash.redirect("/warehouses", "ok", f"Склад «{name}» удалён")


# --- История и устройство системы --------------------------------------------


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, product_id: str = ""):
    pid = _int(product_id)
    with SessionLocal() as s:
        moves = views.history(s, pid)
        refs = _refs(s)
    return _render(request, "history.html", "history", moves=moves, product_id=pid, **refs)


@router.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    return _render(request, "system.html", "system")
