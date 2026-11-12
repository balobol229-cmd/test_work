from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.errors import DomainError
from app.web import api, pages

app = FastAPI(
    title="CheckStock",
    description="Где сейчас товар: остатки по складам ФФ, резервы FBS, отгрузки FBO",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(pages.router)
app.include_router(api.router)


@app.exception_handler(DomainError)
def domain_error(request: Request, exc: DomainError) -> JSONResponse:
    body = {"error": type(exc).__name__, "message": exc.message}
    if shortage := exc.details.get("shortage"):
        body["shortage"] = shortage.as_dict()
    if blockers := exc.details.get("blockers"):
        body["blockers"] = blockers
    return JSONResponse(body, status_code=exc.status_code)
