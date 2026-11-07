"""Схемы запросов и ответов локального API."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


class ScanRequest(BaseModel):
    paths: list[Annotated[str, StringConstraints(min_length=1, max_length=32767)]] = Field(
        default_factory=list, max_length=100
    )


class ScanStarted(BaseModel):
    job_id: str


class ScanCancelled(BaseModel):
    cancelled: bool


class OpenRequest(BaseModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=32767)]
