"""Отчёты индекса, доступные тома и карта папок."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from ... import services
from ..schemas import OpenRequest

router = APIRouter(prefix="/api")


@router.post("/open")
def open_in_explorer(body: OpenRequest, request: Request) -> dict[str, bool]:
    try:
        services.open_in_explorer(body.path, db_path=request.app.state.db_path)
    except FileNotFoundError:
        raise HTTPException(404, "Путь отсутствует в индексе или больше недоступен") from None
    return {"opened": True}


@router.get("/roots")
def roots(request: Request) -> dict[str, Any]:
    return services.list_roots(db_path=request.app.state.db_path).to_dict()


@router.get("/drives")
def drives(request: Request) -> list[dict[str, Any]]:
    return services.list_drives(db_path=request.app.state.db_path)


@router.get("/analyzers")
def analyzers() -> list[dict[str, Any]]:
    return services.analyzer_catalog()


@router.get("/analyzers/{analyzer_id}")
def analyzer(analyzer_id: str, request: Request) -> dict[str, Any]:
    params = dict(request.query_params)
    if "db_path" in params or "analyzer_id" in params:
        raise HTTPException(422, "Недопустимый параметр анализатора")
    try:
        report = services.run_analyzer(analyzer_id, db_path=request.app.state.db_path, **params)
    except KeyError:
        raise HTTPException(404, "Анализатор не найден") from None
    return report.to_dict()


@router.get("/tree")
def tree(
    request: Request,
    path: str = "",
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    files: bool = False,
) -> dict[str, Any]:
    read = services.folder_contents if files else services.tree
    return read(path, limit, db_path=request.app.state.db_path).to_dict()


@router.get("/folders/top")
def largest_folders(request: Request) -> dict[str, Any]:
    return services.largest_folders(db_path=request.app.state.db_path).to_dict()


@router.get("/treemap")
def treemap(
    request: Request,
    path: str = "",
    depth: Annotated[int, Query(ge=1, le=2)] = 2,
    files: bool = False,
) -> list[dict[str, Any]]:
    return [
        dict(node)
        for node in services.treemap(
            path,
            depth,
            include_files=files,
            db_path=request.app.state.db_path,
        )
    ]
