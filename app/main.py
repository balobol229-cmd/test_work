from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DataError

from app.core import views
from app.core.errors import DomainError
from app.web import api, flash, pages

app = FastAPI(
    title="CheckStock",
    description="Где сейчас товар: остатки по складам ФФ, резервы FBS, отгрузки FBO",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(pages.router)
app.include_router(api.router)


@app.middleware("http")
async def fresh_stamp_after_ui_actions(request: Request, call_next):
    response = await call_next(request)
    if request.method == "POST" and not request.url.path.startswith("/api"):
        views.invalidate_stamp()
    return response


@app.exception_handler(DomainError)
def domain_error(request: Request, exc: DomainError) -> JSONResponse:
    body = {"error": type(exc).__name__, "message": exc.message}
    if shortage := exc.details.get("shortage"):
        body["shortage"] = shortage.as_dict()
    if blockers := exc.details.get("blockers"):
        body["blockers"] = blockers
    return JSONResponse(body, status_code=exc.status_code)


@app.exception_handler(DataError)
def data_error(request: Request, exc: DataError) -> Response:
    """Страховка: если некорректное значение всё же дошло до базы, отвечаем понятно, а не 500.

    Основная проверка ввода — в ядре; сюда попадать не должно, и тесты это проверяют.
    """
    message = "Некорректное значение в запросе"
    if request.url.path.startswith("/api"):
        return JSONResponse({"error": "DataError", "message": message}, status_code=422)
    return flash.redirect("/operations", "error", message)
