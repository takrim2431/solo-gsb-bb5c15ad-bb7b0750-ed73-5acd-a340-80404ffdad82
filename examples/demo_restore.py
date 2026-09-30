#!/usr/bin/env python3
"""复供（恢复供水）服务调用示例（仅依赖标准库）。

用法：
    python3 examples/demo_restore.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/restore_network.json）：

    S -P1[阀]- A -P2[阀]- B -P4[阀]- D -P6[阀]- U3
                           |            \\
                         P5[阀]         P8[阀]- E -P9[阀]- U4
                           |
                          U2

脚本依次演示：
  1. 导入两故障演示管网；
  2. 事件 e1：故障管段 P6（D-U3），关 P4 提交隔离 -> 版本 2，U3 失供；
  3. 事件 e2：故障管段 P9（E-U4），P4 已关、P6 待修复，无需再关阀即可隔离 -> 版本 3；
  4. 复供预演 e1：safe=false，阻断原因 RESTORE_UNSAFE（重开 P4 会让 e2 的 P9 重新进水）；
  5. 用过期版本确认 e1 -> 409 VERSION_CONFLICT；直接确认 -> 422 RESTORE_UNSAFE，状态不变；
  6. 按安全顺序先确认复供 e2 -> 版本 4，无阀门重开；
  7. 再预演/确认复供 e1 -> 重开 P4，U3、U4 恢复，版本 5；
  8. 重复确认 e1 -> 409 EVENT_ALREADY_RESTORED；不存在事件 -> 404 EVENT_NOT_FOUND；
  9. 历史记录（含 pending_repair / restored 状态）。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restore_network.json")


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入两故障演示管网", status, body)
    assert status == 200, body

    # 2. 提交隔离 e1：故障 P6，关 P4（版本 1 -> 2）
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("2. 提交隔离 e1（故障 P6，关 P4）", status, body)
    assert status == 200 and body["event_status"] == "pending_repair"
    e1 = body["event_id"]

    # 3. 提交隔离 e2：P4 已关，故障 P9 无需再关阀（计入待修复 P6 仍可隔离）
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P9", "close_pipe_ids": [], "version": 2},
    )
    show("3. 提交隔离 e2（故障 P9，P4 已关，无需再关阀）", status, body)
    assert status == 200
    e2 = body["event_id"]

    # 4. 复供预演 e1（只读，不改状态）：不安全
    status, body = call(
        "POST", "/api/isolation/restore/preview", {"event_id": e1, "version": 3}
    )
    show("4. 复供预演 e1（预期 safe=false，RESTORE_UNSAFE，阻断端点 E）", status, body)
    assert status == 200 and body["safe"] is False
    assert body["issues"][0]["code"] == "RESTORE_UNSAFE"
    assert body["issues"][0]["pipe_id"] == "P9"
    assert body["issues"][0]["node_id"] == "E"
    assert body["reopen_pipe_ids"] == ["P4"]
    assert body["restored_sink_ids"] == ["U3"]

    # 5a. 过期版本确认 -> 409
    status, body = call(
        "POST", "/api/isolation/restore/confirm", {"event_id": e1, "version": 99}
    )
    show("5a. 过期版本确认复供 e1（预期 409 VERSION_CONFLICT）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT"

    # 5b. 当前版本直接确认 e1 -> 422，且不产生部分变更
    status, body = call(
        "POST", "/api/isolation/restore/confirm", {"event_id": e1, "version": 3}
    )
    show("5b. 确认复供 e1（预期 422 RESTORE_UNSAFE，阀门/版本/事件均不变）", status, body)
    assert status == 422 and body["error"]["code"] == "RESTORE_UNSAFE"

    # 6. 先安全复供 e2（内层故障）：无阀门重开，U4 仍因 P4 关闭暂不恢复
    status, body = call(
        "POST", "/api/isolation/restore/confirm", {"event_id": e2, "version": 3}
    )
    show("6. 确认复供 e2（预期成功，版本 4，无阀门重开）", status, body)
    assert status == 200 and body["version"] == 4
    assert body["reopened_pipe_ids"] == []

    # 7a. 再次预演 e1：此时安全，重开 P4 将同时恢复 U3、U4
    status, body = call(
        "POST", "/api/isolation/restore/preview", {"event_id": e1, "version": 4}
    )
    show("7a. 复供预演 e1（预期 safe=true，U3/U4 恢复）", status, body)
    assert status == 200 and body["safe"] is True
    assert body["restored_sink_ids"] == ["U3", "U4"]

    # 7b. 确认复供 e1
    status, body = call(
        "POST", "/api/isolation/restore/confirm", {"event_id": e1, "version": 4}
    )
    show("7b. 确认复供 e1（预期成功，重开 P4，版本 5）", status, body)
    assert status == 200 and body["version"] == 5
    assert body["reopened_pipe_ids"] == ["P4"]
    assert body["event_status"] == "restored"

    # 8a. 重复确认已复供事件 -> 409
    status, body = call(
        "POST", "/api/isolation/restore/confirm", {"event_id": e1, "version": 5}
    )
    show("8a. 重复确认 e1（预期 409 EVENT_ALREADY_RESTORED）", status, body)
    assert status == 409 and body["error"]["code"] == "EVENT_ALREADY_RESTORED"

    # 8b. 确认不存在的事件 -> 404（整网替换后旧事件同样如此）
    status, body = call(
        "POST", "/api/isolation/restore/confirm", {"event_id": 99999, "version": 5}
    )
    show("8b. 确认不存在的事件（预期 404 EVENT_NOT_FOUND）", status, body)
    assert status == 404 and body["error"]["code"] == "EVENT_NOT_FOUND"

    # 9. 历史记录
    status, body = call("GET", "/api/isolation/events")
    show("9. 隔离/复供历史（含 status / restored_at）", status, body)
    assert status == 200 and len(body) == 2

    print("\n全部复供示例执行完成。")


if __name__ == "__main__":
    main()
