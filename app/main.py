"""供水管网故障隔离服务 —— FastAPI 入口。

接口前缀 /api：
- GET  /health                                  健康检查
- POST /api/network/import                      整网导入（替换式，版本重置为 1，旧事件失效）
- GET  /api/network                             读取当前管网与版本
- POST /api/isolation/preview                   隔离方案预演（只读，计入待修复故障）
- POST /api/isolation/submit                    隔离提交（原子关阀 + 版本 +1，生成待修复事件）
- POST /api/isolation/restore/preview           复供预演（只读：重开阀门、预计恢复用水点、阻断原因）
- POST /api/isolation/restore/confirm           复供确认（原子重开 + 事件置为已复供 + 版本 +1）
- GET  /api/isolation/events                    隔离操作历史
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import db
from .config import get_settings
from .planning import (
    Node,
    Pipe,
    RepairEvent,
    STATUS_PENDING_REPAIR,
    evaluate_plan,
    evaluate_restoration,
    validate_import,
    BLOCKING_ISSUE_CODES,
    RESTORE_UNSAFE,
)
from .schemas import (
    ImportIn,
    PlanIn,
    RestoreIn,
    ImportOut,
    NetworkOut,
    PreviewOut,
    SubmitOut,
    RestorePreviewOut,
    RestoreConfirmOut,
    EventOut,
)
from .serializers import (
    serialize_preview,
    serialize_restore_preview,
    serialize_snapshot,
)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, issues: list | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.issues = issues or []
        super().__init__(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="供水管网故障隔离服务",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request, exc: ApiError):
    body: dict = {"error": {"code": exc.code, "message": exc.message}}
    if exc.issues:
        body["error"]["issues"] = [i.as_dict() for i in exc.issues]
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(db.VersionConflict)
async def version_conflict_handler(request, exc: db.VersionConflict):
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "VERSION_CONFLICT",
                "message": f"版本过期：当前版本为 {exc.current}，提交基于版本 {exc.expected}，请重新读取后再试",
                "current_version": exc.current,
                "expected_version": exc.expected,
            }
        },
    )


@app.exception_handler(db.EventNotFound)
async def event_not_found_handler(request, exc: db.EventNotFound):
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "EVENT_NOT_FOUND",
                "message": f"隔离事件不存在：{exc.event_id}（可能已随整网替换失效）",
            }
        },
    )


@app.exception_handler(db.EventAlreadyRestored)
async def event_already_restored_handler(request, exc: db.EventAlreadyRestored):
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "EVENT_ALREADY_RESTORED",
                "message": f"隔离事件 {exc.event_id} 已复供，不能重复确认",
            }
        },
    )


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 管网


def _to_domain_nodes(payload: ImportIn) -> list[Node]:
    return [Node(id=n.id, type=n.type, name=n.name) for n in payload.nodes]


def _to_domain_pipes(payload: ImportIn) -> list[Pipe]:
    return [
        Pipe(
            id=p.id,
            from_node=p.from_node,
            to_node=p.to_node,
            has_valve=p.has_valve,
            valve_closed=p.valve_closed,
            name=p.name,
        )
        for p in payload.pipes
    ]


@app.post(
    "/api/network/import",
    response_model=ImportOut,
    tags=["管网"],
    summary="整网导入（替换当前管网，版本重置为 1）",
)
def import_network(payload: ImportIn):
    nodes = _to_domain_nodes(payload)
    pipes = _to_domain_pipes(payload)

    issues = validate_import(nodes, pipes)
    if issues:
        raise ApiError(
            422,
            "IMPORT_INVALID",
            "管网数据校验失败：存在悬空端点、重号、自环或无阀管段被标记关闭",
            issues=issues,
        )

    with db.transaction() as conn:
        version = db.replace_network(conn, nodes, pipes)

    return {
        "version": version,
        "node_count": len(nodes),
        "pipe_count": len(pipes),
    }


@app.get("/api/network", response_model=NetworkOut, tags=["管网"], summary="读取当前管网与版本")
def get_network():
    with db.get_conn() as conn:
        version, updated_at, nodes, pipes = db.load_snapshot(conn)
    return serialize_snapshot(version, updated_at, nodes, pipes)


# ---------------------------------------------------------------- 隔离


@app.post(
    "/api/isolation/preview",
    response_model=PreviewOut,
    tags=["故障隔离"],
    summary="预演：判断能否隔离并计算失供用水点（只读，不改状态；计入待修复故障）",
)
def preview_isolation(payload: PlanIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        pending = db.load_repair_events(conn, status=STATUS_PENDING_REPAIR)

    pending_faults = frozenset(e.fault_pipe_id for e in pending.values())
    result = evaluate_plan(
        nodes,
        pipes,
        payload.fault_pipe_id,
        list(payload.close_pipe_ids),
        for_submit=False,
        pending_fault_pipe_ids=pending_faults,
    )
    return serialize_preview(
        version, payload.version, payload.fault_pipe_id, payload.close_pipe_ids, result
    )


@app.post(
    "/api/isolation/submit",
    response_model=SubmitOut,
    tags=["故障隔离"],
    summary="提交隔离：基于当前版本重新校验，原子更新阀位与版本（事件状态为待修复）",
)
def submit_isolation(payload: PlanIn):
    close_ids = list(dict.fromkeys(payload.close_pipe_ids))  # 去重但保序（重复仍会被拒）

    with db.transaction() as conn:
        # 锁定当前版本后基于最新数据重新校验
        version, _updated_at, nodes, pipes = db.load_snapshot(conn, for_update=True)

        if version != payload.version:
            raise db.VersionConflict(version, payload.version)

        pending = db.load_repair_events(conn, status=STATUS_PENDING_REPAIR)
        pending_faults = frozenset(e.fault_pipe_id for e in pending.values())
        result = evaluate_plan(
            nodes,
            pipes,
            payload.fault_pipe_id,
            list(payload.close_pipe_ids),
            for_submit=True,
            pending_fault_pipe_ids=pending_faults,
        )

        blocking = [i for i in result.issues if i.code in BLOCKING_ISSUE_CODES]
        if blocking:
            raise ApiError(
                422,
                "PLAN_INVALID",
                "隔离方案非法（管段不存在、无阀管段、重复关闭或把故障管段当作阀门）",
                issues=blocking,
            )
        if not result.isolatable:
            raise ApiError(
                422,
                "NOT_ISOLATABLE",
                "该方案无法隔离故障管段：故障管段至少有一端在方案图中仍可从水源到达",
                issues=result.issues,
            )

        outcome = db.submit_isolation(
            conn,
            expected_version=payload.version,
            fault_pipe_id=payload.fault_pipe_id,
            close_pipe_ids=close_ids,
            result=result,
        )

    return {
        "version": outcome["version"],
        "fault_pipe_id": payload.fault_pipe_id,
        "closed_pipe_ids": close_ids,
        "affected_sink_ids": result.affected_sink_ids,
        "event_id": outcome["event_id"],
        "event_status": STATUS_PENDING_REPAIR,
    }


# ---------------------------------------------------------------- 复供


@app.post(
    "/api/isolation/restore/preview",
    response_model=RestorePreviewOut,
    tags=["复供"],
    summary="复供预演：返回所关阀门、预计恢复用水点与阻断原因（只读，不改状态）",
)
def preview_restoration(payload: RestoreIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        events = db.load_repair_events(conn)

    result = evaluate_restoration(nodes, pipes, events, payload.event_id)
    return serialize_restore_preview(version, payload.version, result)


@app.post(
    "/api/isolation/restore/confirm",
    response_model=RestoreConfirmOut,
    tags=["复供"],
    summary="确认复供：同事务复核版本与安全性，原子重开阀门、更新事件状态与版本",
)
def confirm_restoration(payload: RestoreIn):
    with db.transaction() as conn:
        # 锁定当前版本后基于最新数据重新评估
        version, _updated_at, nodes, pipes = db.load_snapshot(conn, for_update=True)

        if version != payload.version:
            raise db.VersionConflict(version, payload.version)

        events = db.load_repair_events(conn)
        result = evaluate_restoration(nodes, pipes, events, payload.event_id)

        # 顺序：事件不存在(404) -> 已复供(409) -> 重新进水风险(422)
        if not result.found:
            raise db.EventNotFound(payload.event_id)
        if result.status == "restored":
            raise db.EventAlreadyRestored(payload.event_id)
        if not result.safe:
            raise ApiError(
                422,
                RESTORE_UNSAFE,
                "复供存在重新进水风险：其余待修复故障管段至少有一端将重新从水源到达",
                issues=result.issues,
            )

        outcome = db.confirm_restoration(
            conn,
            expected_version=payload.version,
            event_id=payload.event_id,
            reopen_pipe_ids=result.reopen_pipe_ids,
        )

    return {
        "version": outcome["version"],
        "event_id": payload.event_id,
        "event_status": "restored",
        "fault_pipe_id": result.fault_pipe_id,
        "reopened_pipe_ids": result.reopen_pipe_ids,
        "restored_sink_ids": result.restored_sink_ids,
    }


@app.get(
    "/api/isolation/events",
    response_model=list[EventOut],
    tags=["故障隔离"],
    summary="隔离操作历史（按时间倒序）",
)
def get_events():
    settings = get_settings()
    with db.get_conn() as conn:
        return db.list_events(conn, settings.history_limit)
