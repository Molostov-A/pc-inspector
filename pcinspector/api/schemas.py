"""Схемы запросов и ответов локального API."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from ..services import Action


class ScanRequest(BaseModel):
    paths: list[Annotated[str, StringConstraints(min_length=1, max_length=32767)]] = Field(
        default_factory=list, max_length=100
    )


class ScanStarted(BaseModel):
    job_id: str


class HashRequest(BaseModel):
    root: str = Field(default="", max_length=32767)
    min_size: str | int = "1M"


class ScanCancelled(BaseModel):
    cancelled: bool


class OpenRequest(BaseModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=32767)]


class PlanCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=2000)


class PlanEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    note: str | None = Field(default=None, max_length=2000)
    cancel: bool = False


class PlanItemInput(BaseModel):
    path: str = Field(min_length=1, max_length=32767)
    action: Action = "recycle"
    reason: str = Field(default="", max_length=1000)
    target: str | None = Field(default=None, max_length=32767)
    safety: str = "review"
    source: str = Field(default="manual", min_length=1, max_length=80)


class AddPlanItems(BaseModel):
    items: list[PlanItemInput] = Field(min_length=1, max_length=500)
    keep_path: str | None = Field(default=None, min_length=1, max_length=32767)


class PlanItemEdit(BaseModel):
    action: Action | None = None
    target: str | None = Field(default=None, max_length=32767)
    selected: bool | None = None


class PlanExecution(BaseModel):
    confirm_token: str = Field(min_length=1, max_length=100)


class PurgeRequest(BaseModel):
    older_than_days: int = Field(default=30, ge=1, le=3650)
    confirmed: bool = False
