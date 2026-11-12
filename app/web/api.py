"""JSON API. Документация — /docs (Swagger из FastAPI).

Создающие запросы принимают заголовок `Idempotency-Key`: повтор с тем же ключом вернёт
уже созданный документ и `"replayed": true`, остатки не изменятся.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import inventory as inv
from app.core import views
from app.db.models import Document, Warehouse
from app.db.session import SessionLocal

router = APIRouter(prefix="/api", tags=["CheckStock API"])


def tx() -> Iterator[Session]:
    with SessionLocal.begin() as s:
        yield s


Qty = Field(gt=0, description="Количество, шт.")


class WarehouseIn(BaseModel):
    name: str


class ReceiptIn(BaseModel):
    product_id: int
    warehouse_id: int
    qty: int = Qty


class TransferIn(BaseModel):
    product_id: int
    from_warehouse_id: int
    to_warehouse_id: int
    qty: int = Qty


class FbsIn(BaseModel):
    product_id: int
    warehouse_id: int
    qty: int = Qty
    order_ref: str | None = None


class ReserveIn(BaseModel):
    warehouse_id: int | None = Field(None, description="Взять с другого ФФ")


class FboIn(BaseModel):
    product_id: int
    warehouse_id: int
    qty: int = Qty


def _wh(w: Warehouse) -> dict:
    return {"id": w.id, "name": w.name, "deleted": not w.is_active}


def _doc(d: Document) -> dict:
    return {
        "id": d.id,
        "kind": d.kind,
        "status": d.status,
        "product_id": d.product_id,
        "qty": d.qty,
        "from_warehouse_id": d.from_warehouse_id,
        "to_warehouse_id": d.to_warehouse_id,
        "order_ref": d.order_ref,
        "parent_id": d.parent_id,
    }


def _result(res: inv.Result, response: Response, *, created: bool = False) -> dict:
    if created and not res.replayed:
        response.status_code = 201
    body = {"document": _doc(res.document), "replayed": res.replayed}
    if res.shortage:
        body["shortage"] = res.shortage.as_dict()
    if res.parts:
        body["parts"] = [_doc(p) for p in res.parts]
    return body


# --- Чтение -----------------------------------------------------------------


@router.get("/stock", summary="Остатки по товарам и складам")
def get_stock(s: Session = Depends(tx)):
    o = views.stock_overview(s)
    return {
        "store": {"name": o.store.name, "marketplace": o.store.marketplace} if o.store else None,
        "warehouses": [_wh(w) for w in o.warehouses],
        "products": [
            {
                "id": r.product.id,
                "sku": r.product.sku,
                "name": r.product.name,
                "by_warehouse": [
                    {"warehouse_id": wid, "on_hand": c.on_hand, "reserved": c.reserved, "available": c.available}
                    for wid, c in r.cells.items()
                ],
                "in_transit_between_ff": r.transit_ff,
                "fbo_in_transit": r.transit_mp,
                "fbo_at_marketplace": r.mp,
                "fbs_to_customer": r.customer,
                "fbs_waiting": r.waiting,
                "total": r.total,
            }
            for r in o.rows
        ],
    }


@router.get("/check", summary="Сверка: остатки, журнал и документы сходятся")
def get_check(s: Session = Depends(tx)):
    rec = views.reconcile(s)
    return {
        "ok": rec.ok,
        "units": rec.units,
        "checks": [{"title": c.title, "ok": c.ok, "problems": c.problems} for c in rec.checks],
    }


@router.get("/history", summary="История движений")
def get_history(product_id: int | None = None, s: Session = Depends(tx)):
    return [
        {
            "id": m.id,
            "at": m.created_at.isoformat(),
            "action": m.action,
            "sku": m.product.sku,
            "qty": m.qty,
            "from": views.place_label(m.from_loc, m.from_warehouse),
            "to": views.place_label(m.to_loc, m.to_warehouse),
            "document_id": m.document_id,
        }
        for m in views.history(s, product_id)
    ]


# --- Склады -----------------------------------------------------------------


@router.post("/warehouses", status_code=201, summary="Добавить ФФ")
def post_warehouse(body: WarehouseIn, s: Session = Depends(tx)):
    return _wh(inv.create_warehouse(s, body.name))


@router.patch("/warehouses/{warehouse_id}", summary="Переименовать ФФ")
def patch_warehouse(warehouse_id: int, body: WarehouseIn, s: Session = Depends(tx)):
    return _wh(inv.rename_warehouse(s, warehouse_id, body.name))


@router.delete("/warehouses/{warehouse_id}", summary="Удалить ФФ (остатки и история сохраняются)")
def delete_warehouse(warehouse_id: int, s: Session = Depends(tx)):
    res = inv.archive_warehouse(s, warehouse_id)
    return {
        "warehouse": _wh(res.warehouse),
        "replayed": res.replayed,
        "left_on_hand": [{"sku": sku, "qty": q} for sku, q in res.left_on_hand],
    }


# --- Операции ---------------------------------------------------------------


@router.post("/receipts", summary="Приход на ФФ")
def post_receipt(body: ReceiptIn, response: Response, s: Session = Depends(tx),
                 idempotency_key: str | None = Header(None)):
    res = inv.create_receipt(s, body.product_id, body.warehouse_id, body.qty, key=idempotency_key)
    return _result(res, response, created=True)


@router.post("/transfers", summary="Отправить перемещение между ФФ")
def post_transfer(body: TransferIn, response: Response, s: Session = Depends(tx),
                  idempotency_key: str | None = Header(None)):
    res = inv.create_transfer(
        s, body.product_id, body.from_warehouse_id, body.to_warehouse_id, body.qty, key=idempotency_key
    )
    return _result(res, response, created=True)


@router.post("/transfers/{doc_id}/receive", summary="Принять перемещение")
def post_transfer_receive(doc_id: int, response: Response, s: Session = Depends(tx)):
    return _result(inv.receive_transfer(s, doc_id), response)


@router.post("/fbs", summary="Заказ FBS: резерв на ФФ")
def post_fbs(body: FbsIn, response: Response, s: Session = Depends(tx),
             idempotency_key: str | None = Header(None)):
    res = inv.create_fbs_order(
        s, body.product_id, body.warehouse_id, body.qty, order_ref=body.order_ref, key=idempotency_key
    )
    return _result(res, response, created=True)


@router.post("/fbs/{doc_id}/reserve", summary="Повторить резерв, можно с другого ФФ")
def post_fbs_reserve(doc_id: int, response: Response, body: ReserveIn | None = None,
                     s: Session = Depends(tx)):
    return _result(inv.reserve_fbs(s, doc_id, warehouse_id=body.warehouse_id if body else None), response)


@router.post("/fbs/{doc_id}/split", summary="Разделить заказ между ФФ")
def post_fbs_split(doc_id: int, response: Response, s: Session = Depends(tx)):
    return _result(inv.split_fbs(s, doc_id), response)


@router.post("/fbs/{doc_id}/ship", summary="Отправить заказ клиенту")
def post_fbs_ship(doc_id: int, response: Response, s: Session = Depends(tx)):
    return _result(inv.ship_fbs(s, doc_id), response)


@router.post("/fbs/{doc_id}/cancel", summary="Отменить заказ, снять резерв")
def post_fbs_cancel(doc_id: int, response: Response, s: Session = Depends(tx)):
    return _result(inv.cancel_fbs(s, doc_id), response)


@router.post("/documents/{doc_id}/cancel", summary="Удалить операцию: отмена обратным движением, история сохраняется")
def post_document_cancel(doc_id: int, response: Response, s: Session = Depends(tx)):
    return _result(inv.delete_document(s, doc_id), response)


@router.post("/fbo", summary="Отгрузить FBO на склад маркетплейса")
def post_fbo(body: FboIn, response: Response, s: Session = Depends(tx),
             idempotency_key: str | None = Header(None)):
    res = inv.create_fbo_shipment(s, body.product_id, body.warehouse_id, body.qty, key=idempotency_key)
    return _result(res, response, created=True)


@router.post("/fbo/{doc_id}/accept", summary="Поставка принята складом маркетплейса")
def post_fbo_accept(doc_id: int, response: Response, s: Session = Depends(tx)):
    return _result(inv.accept_fbo(s, doc_id), response)
