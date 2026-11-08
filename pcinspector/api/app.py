"""Локальный FastAPI-сервер, статика и защита запросов."""

from __future__ import annotations

import logging
import secrets
import socket
import sqlite3
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import Thread

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from .routes import plans, reports, scan, settings

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parents[1] / "web"
logger = logging.getLogger(__name__)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(*, port: int = DEFAULT_PORT, db_path: Path | str | None = None) -> FastAPI:
    if not 1 <= port <= 65535:
        raise ValueError("Порт должен быть от 1 до 65535")
    host = HOST if port == 80 else f"{HOST}:{port}"
    origin = f"http://{host}"
    allowed_hosts = {host, f"{HOST}:80"} if port == 80 else {host}
    token = secrets.token_urlsafe(32)
    template = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = template.replace("__PCI_TOKEN__", token)

    app = FastAPI(title="PC Inspector", docs_url=None, redoc_url=None)
    app.state.db_path = db_path

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or hosts[0] not in allowed_hosts:
            return _error(403, "invalid_host", "Допустим только локальный адрес сервера")
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        confirmation = request.url.path.rstrip("/").endswith("/confirm-token")
        if mutation or confirmation:
            supplied = request.headers.getlist("x-pci-token")
            origins = request.headers.getlist("origin")
            if (
                origins != [origin]
                or len(supplied) != 1
                or not secrets.compare_digest(supplied[0].encode(), token.encode())
            ):
                return _error(403, "forbidden", "Нужны токен и локальный источник запроса")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path == "/" or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        if error.status_code == 404:
            detail = "Ресурс не найден" if error.detail == "Not Found" else str(error.detail)
            return _error(404, "not_found", detail)
        return _error(error.status_code, "http_error", str(error.detail))

    @app.exception_handler(RequestValidationError)
    async def request_error(_request: Request, _error_info: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", "Проверьте поля и значения запроса")

    @app.exception_handler(KeyError)
    async def missing(_request: Request, error: KeyError) -> JSONResponse:
        return _error(404, "not_found", str(error.args[0]))

    @app.exception_handler(ValueError)
    @app.exception_handler(OverflowError)
    async def value_error(_request: Request, error: ValueError | OverflowError) -> JSONResponse:
        return _error(422, "invalid_parameters", str(error))

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def index_error(_request: Request, error: sqlite3.Error | OSError) -> JSONResponse:
        logger.error("Ошибка чтения данных: %s", error)
        return _error(500, "data_error", "Не удалось прочитать индекс или данные о томах")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return html

    app.include_router(reports.router)
    app.include_router(scan.router)
    app.include_router(plans.router)
    app.include_router(settings.router)
    for directory in ("css", "js", "vendor"):
        app.mount(f"/{directory}", StaticFiles(directory=WEB_DIR / directory), name=directory)
    return app


class _BrowserServer(uvicorn.Server):
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            Thread(
                target=webbrowser.open,
                args=(f"http://{HOST}:{self.config.port}/",),
                daemon=True,
            ).start()


def serve(port: int, open_browser: bool, db_path: Path | str | None = None) -> None:
    app = create_app(port=port, db_path=db_path)
    config = uvicorn.Config(app, host=HOST, port=port, proxy_headers=False)
    server_class = _BrowserServer if open_browser else uvicorn.Server
    server_class(config).run()
