"""Чтение и сохранение локальных настроек."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request

from ... import services
from ...core.settings import Settings
from ..schemas import SettingsInput

router = APIRouter(prefix="/api/settings")


@router.get("", response_model=SettingsInput)
def read(request: Request) -> Settings:
    return services.get_settings(db_path=request.app.state.db_path)


@router.put("", response_model=SettingsInput)
def write(body: SettingsInput, request: Request) -> Settings:
    return services.save_settings(cast(Settings, body.model_dump()), db_path=request.app.state.db_path)
