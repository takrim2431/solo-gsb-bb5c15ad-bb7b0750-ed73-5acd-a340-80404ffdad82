"""API 路由层集成测试。

本机无 PostgreSQL，使用内存假连接替身替换 app.db 的数据访问函数，
覆盖 FastAPI 路由、请求校验、事务与错误码的完整行为。
运行：python3 -m unittest tests.test_api -v
（需要 fastapi/httpx 依赖）
"""
import json
import os
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.planning import Node, Pipe  # noqa: E402


class FakeStore:
    def __init__(self):
        self.version = 0
        self.updated_at = None
        self.nodes: dict[str, Node] = {}
        self.pipes: dict[str, Pipe] = {}
        self.events = []
        self.next_event_id = 1
        self.fail_commit = False


STORE = FakeStore()


@contextmanager
def fake_get_conn():
    yield "FAKE_CONN"


@contextmanager
def fake_transaction():
    conn = "FAKE_TX"
    # 简单快照：提交时把暂存区落盘
    snapshot = (
        STORE.version,
        dict(STORE.nodes),
        {k: Pipe(**v.__dict__) for k, v in STORE.pipes.items()},
    )
    tmp = {
        "version": STORE.version,
        "nodes": dict(STORE.nodes),
        "pipes": dict(STORE.pipes),
        "events": list(STORE.events),
        "next_id": STORE.next_event_id,
    }
    try:
        yield conn
        if STORE.fail_commit:
            raise RuntimeError("simulated commit failure")
    except Exception:
        STORE.version = tmp["version"]
        STORE.nodes = tmp["nodes"]
        STORE.pipes = tmp["pipes"]
        STORE.events = tmp["events"]
        STORE.next_event_id = tmp["next_id"]
        raise


def fake_init_db():
    return None


def fake_load_snapshot(conn, for_update=False):
    return STORE.version, STORE.updated_at, dict(STORE.nodes), dict(STORE.pipes)


def fake_replace_network(conn, nodes, pipes):
    STORE.version = 1
    STORE.nodes = {n.id: n for n in nodes}
    STORE.pipes = {p.id: p for p in pipes}
    STORE.events = []
    STORE.next_event_id = 1
    return 1


def fake_submit_isolation(conn, expected_version, fault_pipe_id, close_pipe_ids, result):
    # 复制 db.submit_isolation 的关键语义：锁行版本比对 -> 更新阀位 -> +1 -> 事件
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    for pid in close_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, True, p.name
        )
    before = STORE.version
    STORE.version += 1
    eid = STORE.next_event_id
    STORE.next_event_id += 1
    import datetime

    event = {
        "id": eid,
        "version_before": before,
        "version_after": STORE.version,
        "fault_pipe_id": fault_pipe_id,
        "closed_pipe_ids": list(close_pipe_ids),
        "affected_sink_ids": list(result.affected_sink_ids),
        "status": "open",
        "restored_at": None,
        "created_at": datetime.datetime(2026, 9, 30, 12, 0, 0),
    }
    STORE.events.insert(0, event)
    return {"version": STORE.version, "event_id": eid, "created_at": event["created_at"]}


def fake_list_events(conn, limit):
    return STORE.events[:limit]


def fake_get_event(conn, event_id, for_update=False):
    for e in STORE.events:
        if e["id"] == event_id:
            return e
    return None


def fake_list_open_events(conn, exclude_event_id=None, for_update=False):
    return [
        e
        for e in STORE.events
        if e.get("status", "open") == "open" and e["id"] != exclude_event_id
    ]


def fake_confirm_restore(
    conn, event_id, expected_version, reopened_pipe_ids, restored_sink_ids
):
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    event = next((e for e in STORE.events if e["id"] == event_id), None)
    if event is None or event.get("status", "open") != "open":
        raise RuntimeError("event not open")
    for pid in reopened_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, False, p.name
        )
    before = STORE.version
    STORE.version += 1
    import datetime

    restored_at = datetime.datetime(2026, 9, 30, 13, 0, 0)
    event["status"] = "restored"
    event["restored_at"] = restored_at
    return {"version": STORE.version, "restored_at": restored_at}


# 替换数据访问层
db.get_conn = fake_get_conn
db.transaction = fake_transaction
db.init_db = fake_init_db
db.load_snapshot = fake_load_snapshot
db.replace_network = fake_replace_network
db.submit_isolation = fake_submit_isolation
db.list_events = fake_list_events
db.get_event = fake_get_event
db.list_open_events = fake_list_open_events
db.confirm_restore = fake_confirm_restore


VALID_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source", "name": "一水厂"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
    ],
}


RESTORE_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "E", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
        {"id": "U4", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
        # P7 为已隔离区内的无阀管段（E2 故障管段），自身无阀可关
        {"id": "P7", "from": "D", "to": "E", "has_valve": False},
        {"id": "P8", "from": "E", "to": "U4", "has_valve": True},
    ],
}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # lifespan 会调用 init_db（已替换为 fake）
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.fail_commit = False

    def _import(self):
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_full_happy_path(self):
        info = self._import()
        self.assertEqual(info["version"], 1)

        r = self.client.get("/api/network")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(len(body["nodes"]), 7)
        self.assertEqual(len(body["pipes"]), 6)
        # 序列化别名：from/to
        p1 = next(p for p in body["pipes"] if p["id"] == "P1")
        self.assertEqual(p1["from"], "S")
        self.assertEqual(p1["to"], "A")
        # 无阀管段 valve_closed 序列化为 null
        p6 = next(p for p in body["pipes"] if p["id"] == "P6")
        self.assertIsNone(p6["valve_closed"])

        # 不可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["isolatable"])

        # 可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertTrue(body["version_match"])

        # 提交成功
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertEqual(body["event_id"], 1)

        # 阀位确实落库
        r = self.client.get("/api/network")
        p4 = next(p for p in r.json()["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])

        # 历史
        r = self.client.get("/api/isolation/events")
        self.assertEqual(r.status_code, 200)
        events = r.json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["fault_pipe_id"], "P6")
        self.assertEqual(events[0]["closed_pipe_ids"], ["P4"])

    def test_import_rejects_dangling_self_loop_duplicate(self):
        bad = {
            "nodes": [
                {"id": "S", "type": "source"},
                {"id": "S", "type": "junction"},
            ],
            "pipes": [
                {"id": "P1", "from": "S", "to": "X"},
                {"id": "P1", "from": "S", "to": "S"},
            ],
        }
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("DUPLICATE_NODE_ID", codes)
        self.assertIn("DUPLICATE_PIPE_ID", codes)
        self.assertIn("PIPE_ENDPOINT_UNKNOWN", codes)
        self.assertIn("PIPE_SELF_LOOP", codes)
        # 拒绝后状态不变（空网版本 0）
        r = self.client.get("/api/network")
        self.assertEqual(r.json()["version"], 0)

    def test_non_valve_pipe_initial_closed_rejected(self):
        bad = dict(VALID_NETWORK)
        bad["pipes"] = [
            dict(p, valve_closed=True) if p["id"] == "P6" else p
            for p in VALID_NETWORK["pipes"]
        ]
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        self.assertIn("NON_VALVE_PIPE_CLOSED", r.text)

    def test_submit_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 1)

    def test_submit_no_valve_pipe(self):
        self._import()
        # 关闭无阀管段 P6（以 P5 为故障管段）-> 422 CLOSE_PIPE_NO_VALVE
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_NO_VALVE", r.text)
        self.assertEqual(STORE.version, 1)

    def test_submit_duplicate_and_already_closed(self):
        self._import()
        # 列表内重复
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4", "P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

        # 先成功关 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)

        # 版本 2 下再次关已关闭的 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 2},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

    def test_submit_not_isolatable(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "NOT_ISOLATABLE")
        self.assertEqual(STORE.version, 1)

    def test_submit_fault_pipe_as_valve(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_IS_FAULT", r.text)

    def test_preview_stale_version_flag(self):
        self._import()
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 5},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["version_match"])
        self.assertEqual(body["version"], 1)

    def test_request_body_validation_422(self):
        self._import()
        # version 不允许负数
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": [], "version": -1},
        )
        self.assertEqual(r.status_code, 422)

    def test_submit_against_empty_network_is_version_conflict(self):
        # 空网版本为 0：基于版本 1 提交应得到 409，而非参数校验错误
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")

    def test_reimport_resets_version_and_events(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(STORE.version, 2)
        # 重新导入 -> 版本重置为 1、历史清空
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(STORE.events, [])

    def test_failed_submit_rolls_back(self):
        self._import()
        STORE.fail_commit = True
        # TestClient 默认抛出服务端异常；真实 HTTP 下客户端会收到 500。
        # 关键断言：事务回滚后版本与阀位均不变。
        with self.assertRaises(RuntimeError):
            self.client.post(
                "/api/isolation/submit",
                json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
            )
        self.assertEqual(STORE.version, 1)
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    # ---------------------------------------------------------- 复供

    def _submit_two_faults(self):
        # 导入复供演示管网
        r = self.client.post("/api/network/import", json=RESTORE_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        # 事件 1：故障 P6（D-U3），关 P4（B-D）隔离整个 D 片区
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 1)
        # 事件 2：故障 P7（D-E，无阀）。D 片区已被 P4 隔离，
        # 故无需再关阀门（P8 阀保持开启），提交空方案即可隔离
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P7", "close_pipe_ids": [], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 2)
        self.assertEqual(STORE.version, 3)

    def test_restore_preview_readonly_and_safe_for_inner_fault(self):
        self._submit_two_faults()
        # 复供事件 2（片区内故障 P7，未关任何阀）：重开集合为空，
        # 仍关闭的 P4 继续阻断，其余故障 P6 两端 D/U3 均不可达 -> 安全
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["version_match"])
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["fault_pipe_id"], "P7")
        self.assertEqual(body["closed_pipe_ids"], [])
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], [])
        self.assertEqual(body["block_reasons"], [])
        # 预演不改状态
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertFalse(STORE.pipes["P8"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "open")

    def test_restore_preview_flags_rewater_risk_for_outer_fault(self):
        self._submit_two_faults()
        # 复供事件 1（外层故障 P6，关 P4）：重开 P4 后 D 区进水，
        # 仍待修复的 P7 的 D 端将重新从水源到达（仅一端进水即有风险）-> 禁止复供。
        # U3 恢复；U4 因故障管段 P7 本身仍阻断，暂不恢复。
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U3"])
        self.assertEqual(len(body["block_reasons"]), 1)
        block = body["block_reasons"][0]
        self.assertEqual(block["code"], "FAULT_REWATER_RISK")
        self.assertEqual(block["pipe_id"], "P7")
        self.assertEqual(block["reachable_endpoints"], ["D"])

    def test_restore_confirm_rejected_when_unsafe(self):
        self._submit_two_faults()
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 3}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "RESTORE_UNSAFE")
        self.assertIn("FAULT_REWATER_RISK", r.text)
        # 无部分变更：阀位、版本、事件状态均不变
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[-1]["status"], "open")
        self.assertEqual(STORE.events[0]["status"], "open")

    def test_restore_confirm_safe_then_outer_becomes_restorable(self):
        self._submit_two_faults()
        # 先安全复供区内事件 2（没有阀门被重开，不恢复用水点）
        r = self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 4)
        self.assertEqual(body["status"], "restored")
        self.assertEqual(body["reopened_pipe_ids"], [])
        self.assertEqual(body["restored_sink_ids"], [])
        # P4 仍关闭
        self.assertTrue(STORE.pipes["P4"].valve_closed)

        # 其余待修复故障只剩 P6，复供事件 1 变安全，U3/U4 恢复
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 4}
        )
        body = r.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U3", "U4"])

        r = self.client.post("/api/isolation/events/1/restore", json={"version": 4})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 5)
        self.assertEqual(r.json()["restored_sink_ids"], ["U3", "U4"])
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    def test_restore_event_not_found_and_already_restored(self):
        self._submit_two_faults()
        # 不存在的事件 -> 404
        r = self.client.post(
            "/api/isolation/events/999/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")
        r = self.client.post(
            "/api/isolation/events/999/restore", json={"version": 3}
        )
        self.assertEqual(r.status_code, 404)

        # 成功复供事件 2 后重复复供 -> 409
        r = self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 4}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")
        r = self.client.post(
            "/api/isolation/events/2/restore", json={"version": 4}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(STORE.version, 4)

    def test_restore_version_conflict(self):
        self._submit_two_faults()
        r = self.client.post(
            "/api/isolation/events/2/restore", json={"version": 99}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "open")
        # 预演不拒绝过期版本，只标记 version_match=false
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 99}
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["version_match"])

    def test_events_history_contains_status_fields(self):
        self._submit_two_faults()
        r = self.client.get("/api/isolation/events")
        events = r.json()
        self.assertEqual([e["id"] for e in events], [2, 1])
        for e in events:
            self.assertEqual(e["status"], "open")
            self.assertIsNone(e["restored_at"])
        # 复供后历史反映新状态
        self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        events = self.client.get("/api/isolation/events").json()
        e2 = next(e for e in events if e["id"] == 2)
        self.assertEqual(e2["status"], "restored")
        self.assertIsNotNone(e2["restored_at"])

    def test_preview_and_submit_count_pending_faults(self):
        self._submit_two_faults()
        # 两条待修复事件后：D 片区（U3/U4）已因关 P4 与故障 P7 失供。
        # 对 P5（B-U2）做隔离预演（关 P2 隔离 B 侧）：通水计算排除待修复故障
        # 管段 P6/P7，U3/U4 在方案前已失供不重复计入；新增失供为 U2。
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P2"], "version": 3},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U2"])

    def test_reimport_invalidates_old_events_for_restore(self):
        self._submit_two_faults()
        r = self.client.post("/api/network/import", json=RESTORE_NETWORK)
        self.assertEqual(r.status_code, 200)
        # 整网替换后旧事件失效（历史清空）：复供旧事件编号 -> 404
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
