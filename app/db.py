"""PostgreSQL 访问层：建表、快照读取、整网导入、隔离提交。"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg2
import psycopg2.extras
from psycopg2.errors import UndefinedTable

from .config import get_settings
from .planning import (
    Node,
    Pipe,
    PlanResult,
    RepairEvent,
    STATUS_PENDING_REPAIR,
    STATUS_RESTORED,
)

DDL = """
CREATE TABLE IF NOT EXISTS network_meta (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    version     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT network_meta_singleton CHECK (id = 1)
);

INSERT INTO network_meta (id, version)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS nodes (
    id     TEXT PRIMARY KEY,
    type   TEXT NOT NULL CHECK (type IN ('source', 'sink', 'junction')),
    name   TEXT
);

CREATE TABLE IF NOT EXISTS pipes (
    id           TEXT PRIMARY KEY,
    from_node    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    to_node      TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    has_valve    BOOLEAN NOT NULL DEFAULT FALSE,
    valve_closed BOOLEAN NOT NULL DEFAULT FALSE,
    name         TEXT,
    CONSTRAINT pipes_no_self_loop CHECK (from_node <> to_node)
);

CREATE INDEX IF NOT EXISTS idx_pipes_from_node ON pipes (from_node);
CREATE INDEX IF NOT EXISTS idx_pipes_to_node ON pipes (to_node);

CREATE TABLE IF NOT EXISTS isolation_events (
    id                BIGSERIAL PRIMARY KEY,
    version_before    INTEGER NOT NULL,
    version_after     INTEGER NOT NULL,
    fault_pipe_id     TEXT NOT NULL,
    closed_pipe_ids   JSONB NOT NULL,
    affected_sink_ids JSONB NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending_repair'
                        CHECK (status IN ('pending_repair', 'restored')),
    restored_at       TIMESTAMPTZ,
    restore_version   INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 兼容已存在的旧表：补齐复供相关列
ALTER TABLE isolation_events ADD COLUMN IF NOT EXISTS status TEXT
    NOT NULL DEFAULT 'pending_repair';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'isolation_events_status_check'
    ) THEN
        ALTER TABLE isolation_events
            ADD CONSTRAINT isolation_events_status_check
            CHECK (status IN ('pending_repair', 'restored'));
    END IF;
END $$;
ALTER TABLE isolation_events ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;
ALTER TABLE isolation_events ADD COLUMN IF NOT EXISTS restore_version INTEGER;
"""


@contextmanager
def get_conn() -> Iterator:
    """获取一个数据库连接，调用方负责 commit/rollback。"""
    conn = psycopg2.connect(get_settings().database_url)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator:
    """事务上下文：正常结束提交，异常回滚。"""
    with get_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """启动时建表；等待 PostgreSQL 就绪。"""
    settings = get_settings()
    last_error: Exception | None = None
    for attempt in range(1, settings.db_connect_retries + 1):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(DDL)
                conn.commit()
            return
        except (psycopg2.OperationalError, UndefinedTable) as exc:  # pragma: no cover
            last_error = exc
            time.sleep(settings.db_connect_retry_interval)
    raise RuntimeError(f"数据库初始化失败：{last_error}")


# ---------------------------------------------------------------- 快照


def load_snapshot(conn, *, for_update: bool = False) -> tuple[int, datetime | None, dict[str, Node], dict[str, Pipe]]:
    """读取当前版本与全部节点/管段。for_update=True 时锁定元数据行。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT version, updated_at FROM network_meta WHERE id = 1"
            f"{' FOR UPDATE' if for_update else ''}"
        )
        row = cur.fetchone()
        version = row["version"] if row else 0
        updated_at = row["updated_at"] if row else None

        cur.execute("SELECT id, type, name FROM nodes")
        nodes = {
            r["id"]: Node(id=r["id"], type=r["type"], name=r["name"])
            for r in cur.fetchall()
        }

        cur.execute(
            "SELECT id, from_node, to_node, has_valve, valve_closed, name "
            "FROM pipes ORDER BY id"
        )
        pipes = {
            r["id"]: Pipe(
                id=r["id"],
                from_node=r["from_node"],
                to_node=r["to_node"],
                has_valve=r["has_valve"],
                valve_closed=r["valve_closed"],
                name=r["name"],
            )
            for r in cur.fetchall()
        }
    return version, updated_at, nodes, pipes


# ---------------------------------------------------------------- 整网导入


def replace_network(conn, nodes: list[Node], pipes: list[Pipe]) -> int:
    """在已开启的事务中整网替换：清空旧数据并重写，版本重置为 1。

    返回新版本号。导入是对管网的整体重新定义，因此版本从头计数；
    历史事件也随整网替换一并清空，避免引用旧编号。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO network_meta (id, version) VALUES (1, 0)")

        cur.execute("TRUNCATE isolation_events")
        cur.execute("TRUNCATE pipes")
        cur.execute("TRUNCATE nodes")

        cur.executemany(
            "INSERT INTO nodes (id, type, name) VALUES (%s, %s, %s)",
            [(n.id, n.type, n.name) for n in nodes],
        )
        cur.executemany(
            "INSERT INTO pipes (id, from_node, to_node, has_valve, valve_closed, name) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (p.id, p.from_node, p.to_node, p.has_valve, p.valve_closed, p.name)
                for p in pipes
            ],
        )
        cur.execute(
            "UPDATE network_meta SET version = 1, updated_at = now() WHERE id = 1"
        )
    return 1


# ---------------------------------------------------------------- 隔离提交


def submit_isolation(
    conn,
    expected_version: int,
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    result: PlanResult,
) -> dict:
    """在已开启的事务中完成提交。

    调用方须先用当前快照重新评估并确认 result.isolatable 且无阻断问题。
    本函数只负责：锁行 → 版本比对 → 原子更新阀位、版本 → 写事件。
    返回新版本信息；版本过期抛出 VersionConflict。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        if current_version != expected_version:
            raise VersionConflict(current_version, expected_version)

        if close_pipe_ids:
            cur.execute(
                "UPDATE pipes SET valve_closed = TRUE "
                "WHERE id = ANY(%s) AND has_valve = TRUE",
                (close_pipe_ids,),
            )
        new_version = current_version + 1
        cur.execute(
            "UPDATE network_meta SET version = %s, updated_at = now() WHERE id = 1",
            (new_version,),
        )
        cur.execute(
            "INSERT INTO isolation_events "
            "(version_before, version_after, fault_pipe_id, "
            " closed_pipe_ids, affected_sink_ids, status) "
            "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s) "
            "RETURNING id, created_at",
            (
                current_version,
                new_version,
                fault_pipe_id,
                json.dumps(close_pipe_ids),
                json.dumps(result.affected_sink_ids),
                STATUS_PENDING_REPAIR,
            ),
        )
        event_id, created_at = cur.fetchone()

    return {
        "version": new_version,
        "event_id": event_id,
        "created_at": created_at,
    }


class VersionConflict(Exception):
    def __init__(self, current: int, expected: int):
        self.current = current
        self.expected = expected
        super().__init__(f"版本过期：当前版本 {current}，提交基于版本 {expected}")


class EventNotFound(Exception):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"隔离事件不存在：{event_id}（可能已随整网替换失效）")


class EventAlreadyRestored(Exception):
    def __init__(self, event_id: int):
        self.event_id = event_id
        super().__init__(f"隔离事件 {event_id} 已复供，不能重复确认")


# ---------------------------------------------------------------- 复供


def load_repair_events(conn, *, status: str | None = None) -> dict[int, RepairEvent]:
    """读取隔离事件的领域视图，按事件编号索引。status 可限定状态。"""
    with conn.cursor() as cur:
        if status is None:
            cur.execute(
                "SELECT id, status, fault_pipe_id, closed_pipe_ids "
                "FROM isolation_events ORDER BY id"
            )
        else:
            cur.execute(
                "SELECT id, status, fault_pipe_id, closed_pipe_ids "
                "FROM isolation_events WHERE status = %s ORDER BY id",
                (status,),
            )
        rows = cur.fetchall()

    events: dict[int, RepairEvent] = {}
    for event_id, event_status, fault_pipe_id, closed in rows:
        if isinstance(closed, str):  # 防御：某些驱动配置下 jsonb 可能返回字符串
            closed = json.loads(closed)
        events[event_id] = RepairEvent(
            id=event_id,
            status=event_status,
            fault_pipe_id=fault_pipe_id,
            closed_pipe_ids=tuple(closed),
        )
    return events


def confirm_restoration(
    conn,
    expected_version: int,
    event_id: int,
    reopen_pipe_ids: list[str],
) -> dict:
    """在已开启的事务中确认复供。

    调用方须已用当前快照完成版本、事件存在性与重新进水风险的领域校验。
    本函数负责：锁元数据行复核版本 → 锁事件行复核状态 → 重开阀门 →
    更新事件为已复供 → 版本号 +1。任何一步失败均抛异常，由事务整体回滚。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        if current_version != expected_version:
            raise VersionConflict(current_version, expected_version)

        # 锁定事件行后复核状态，防止并发重复复供
        cur.execute(
            "SELECT status FROM isolation_events WHERE id = %s FOR UPDATE",
            (event_id,),
        )
        event_row = cur.fetchone()
        if event_row is None:
            raise EventNotFound(event_id)
        if event_row[0] == STATUS_RESTORED:
            raise EventAlreadyRestored(event_id)

        if reopen_pipe_ids:
            cur.execute(
                "UPDATE pipes SET valve_closed = FALSE WHERE id = ANY(%s)",
                (reopen_pipe_ids,),
            )

        new_version = current_version + 1
        cur.execute(
            "UPDATE network_meta SET version = %s, updated_at = now() WHERE id = 1",
            (new_version,),
        )
        cur.execute(
            "UPDATE isolation_events "
            "SET status = %s, restored_at = now(), restore_version = %s "
            "WHERE id = %s",
            (STATUS_RESTORED, new_version, event_id),
        )

    return {"version": new_version, "event_id": event_id}


# ---------------------------------------------------------------- 历史


def list_events(conn, limit: int) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, version_before, version_after, fault_pipe_id, "
            "closed_pipe_ids, affected_sink_ids, status, restored_at, "
            "restore_version, created_at "
            "FROM isolation_events ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()

    events = []
    for r in rows:
        closed = r["closed_pipe_ids"]
        affected = r["affected_sink_ids"]
        # 防御：某些驱动配置下 jsonb 可能返回字符串
        if isinstance(closed, str):
            closed = json.loads(closed)
        if isinstance(affected, str):
            affected = json.loads(affected)
        events.append(
            {
                "id": r["id"],
                "version_before": r["version_before"],
                "version_after": r["version_after"],
                "fault_pipe_id": r["fault_pipe_id"],
                "closed_pipe_ids": closed,
                "affected_sink_ids": affected,
                "status": r["status"],
                "restored_at": r["restored_at"],
                "restore_version": r["restore_version"],
                "created_at": r["created_at"],
            }
        )
    return events
