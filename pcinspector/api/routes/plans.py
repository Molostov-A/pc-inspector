"""HTTP-обёртки планов, подтверждения, истории и карантина."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ... import services
from ..schemas import AddPlanItems, PlanCreate, PlanEdit, PlanExecution, PlanItemEdit, PurgeRequest

router = APIRouter(prefix="/api")


@router.get("/plans")
def list_plans(request: Request) -> list[dict[str, Any]]:
    return services.list_plans(db_path=request.app.state.db_path)


@router.post("/plans", status_code=201)
def create_plan(body: PlanCreate, request: Request) -> dict[str, Any]:
    return services.create_plan(body.title, note=body.note, db_path=request.app.state.db_path)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, request: Request) -> dict[str, Any]:
    return services.get_plan(plan_id, db_path=request.app.state.db_path)


@router.patch("/plans/{plan_id}")
def edit_plan(plan_id: int, body: PlanEdit, request: Request) -> dict[str, Any]:
    return services.edit_plan(plan_id, **body.model_dump(), db_path=request.app.state.db_path)


@router.post("/plans/{plan_id}/items")
def add_items(plan_id: int, body: AddPlanItems, request: Request) -> dict[str, Any]:
    return services.add_plan_items(
        plan_id,
        [services.PlanItemIn(**item.model_dump()) for item in body.items],
        keep_path=body.keep_path,
        db_path=request.app.state.db_path,
    )


@router.patch("/plans/{plan_id}/items/{item_id}")
def edit_item(plan_id: int, item_id: int, body: PlanItemEdit, request: Request) -> dict[str, Any]:
    return services.edit_plan_item(plan_id, item_id, **body.model_dump(), db_path=request.app.state.db_path)


@router.delete("/plans/{plan_id}/items/{item_id}")
def delete_item(plan_id: int, item_id: int, request: Request) -> dict[str, Any]:
    return services.edit_plan_item(plan_id, item_id, remove=True, db_path=request.app.state.db_path)


@router.get("/plans/{plan_id}/confirm-token")
@router.post("/plans/{plan_id}/confirm-token")
def confirm_token(plan_id: int, revision: int, request: Request) -> dict[str, str]:
    return {"confirm_token": services.plan_confirmation(plan_id, revision, db_path=request.app.state.db_path)}


@router.post("/plans/{plan_id}/execute")
def execute_plan(plan_id: int, body: PlanExecution, request: Request) -> dict[str, Any]:
    return services.execute_plan(plan_id, body.confirm_token, db_path=request.app.state.db_path)


@router.get("/history")
def history(request: Request) -> list[dict[str, Any]]:
    return services.action_history(db_path=request.app.state.db_path)


@router.post("/history/{action_id}/restore")
def restore(action_id: int, request: Request) -> dict[str, Any]:
    return services.restore_action(action_id, db_path=request.app.state.db_path)


@router.get("/quarantine")
def quarantine(request: Request) -> dict[str, Any]:
    return services.list_quarantine(db_path=request.app.state.db_path)


@router.post("/quarantine/purge")
def purge(body: PurgeRequest, request: Request) -> dict[str, Any]:
    return services.purge_quarantine(**body.model_dump(), db_path=request.app.state.db_path)
