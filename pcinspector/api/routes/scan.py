"""Запуск, прогресс и отмена фонового сканирования."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ... import services
from ..schemas import HashRequest, ScanCancelled, ScanRequest, ScanStarted

router = APIRouter(prefix="/api")


@router.post("/scan", status_code=202)
def scan(body: ScanRequest, request: Request) -> ScanStarted:
    return ScanStarted(job_id=services.start_scan(body.paths, db_path=request.app.state.db_path))


@router.post("/hash", status_code=202)
def start_hash(body: HashRequest, request: Request) -> ScanStarted:
    return ScanStarted(
        job_id=services.start_hash(body.root, body.min_size, db_path=request.app.state.db_path)
    )


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    try:
        return asdict(services.job_status(job_id))
    except KeyError:
        raise HTTPException(404, "Задача не найдена") from None


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> ScanCancelled:
    try:
        return ScanCancelled(cancelled=services.cancel_job(job_id))
    except KeyError:
        raise HTTPException(404, "Задача не найдена") from None
