"""할 일 화면과 API. 전부 소유자 전용."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from pa.core import task_ops
from pa.core.errors import IllegalTransition, NotFound
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _guard(fn, *args):
    try:
        fn(*args)
    except NotFound:
        raise HTTPException(404, "할 일을 찾을 수 없습니다")
    except IllegalTransition:
        raise HTTPException(409, "지금은 할 수 없는 상태입니다")
    return {"ok": True}


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    rows = [dict(r) for r in con.execute(
        "SELECT id, title, due_at, status FROM task WHERE status IN ('candidate','open') "
        "ORDER BY status DESC, COALESCE(due_at,'9999'), created_at LIMIT 200")]
    return templates.TemplateResponse(request, "tasks.html", {
        "open_tasks": [r for r in rows if r["status"] == "open"],
        "candidates": [r for r in rows if r["status"] == "candidate"]})


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_at: str | None = None
    all_day: bool = False


@router.post("/api/tasks")
async def add_task(body: TaskIn, request: Request, _: str = Depends(require_owner)):
    try:
        tid = task_ops.add(request.app.state.con, body.title, _now(), due_at=body.due_at, all_day=body.all_day)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": tid}


@router.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: str, request: Request, _: str = Depends(require_owner)):
    return _guard(task_ops.complete, request.app.state.con, task_id, _now())


@router.post("/api/tasks/{task_id}/confirm")
async def confirm_task(task_id: str, request: Request, _: str = Depends(require_owner)):
    return _guard(task_ops.confirm, request.app.state.con, task_id, _now())


@router.post("/api/tasks/{task_id}/drop")
async def drop_task(task_id: str, request: Request, _: str = Depends(require_owner)):
    return _guard(task_ops.drop, request.app.state.con, task_id, _now())


@router.post("/api/tasks/{task_id}/restore")
async def restore_task(task_id: str, request: Request, _: str = Depends(require_owner)):
    return _guard(task_ops.restore, request.app.state.con, task_id, _now())


class MergeIn(BaseModel):
    other_id: str


@router.post("/api/tasks/{task_id}/merge")
async def merge_task(task_id: str, body: MergeIn, request: Request, _: str = Depends(require_owner)):
    return _guard(task_ops.merge, request.app.state.con, task_id, body.other_id, _now())
