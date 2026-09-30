"""管网故障隔离与复供的纯领域逻辑（不依赖数据库 / Web 框架）。

规则概述：
- 管网为无向图，节点分为水源(source)/用水点(sink)/连接点(junction)。
- 已关闭阀门的管段视为不可通行。
- 隔离事件区分“待修复(pending_repair)”与“已复供(restored)”两种状态：
  待修复事件的故障管段仍断管，计算通水时同样视为不可通行。
- 隔离方案图 = 当前通水图移除故障管段，再关闭所有“拟关闭管段”。
  当前通水图中：已关闭阀门、所有待修复事件的故障管段均不可通行。
- 只有故障管段**两端**在方案图中都无法从任一水源到达，方案才可隔离(isolatable)。
- 失供用水点 = 方案前由任一水源可达、方案后不可达的 sink，按编号排序。

复供（恢复供水）：
- 复供预演图 = 把目标事件的故障管段视为已修复并重开该事件阀门；
  其他待修复事件的故障管段继续移除、其关闭的阀门继续关闭。
- 预计恢复用水点 = 当前不通水、复供图中通水的 sink。
- 仅当复供图中其余待修复故障管段两端仍都无法从水源到达（无重新进水风险），
  才允许确认复供。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

# 问题代码 ---------------------------------------------------------------

# 阻断类问题：出现任一即不能隔离、提交时拒绝（422）
FAULT_PIPE_NOT_FOUND = "FAULT_PIPE_NOT_FOUND"
CLOSE_PIPE_NOT_FOUND = "CLOSE_PIPE_NOT_FOUND"
CLOSE_PIPE_NO_VALVE = "CLOSE_PIPE_NO_VALVE"
CLOSE_PIPE_DUPLICATE = "CLOSE_PIPE_DUPLICATE"
CLOSE_PIPE_IS_FAULT = "CLOSE_PIPE_IS_FAULT"
# 导入期问题
DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
DUPLICATE_PIPE_ID = "DUPLICATE_PIPE_ID"
PIPE_SELF_LOOP = "PIPE_SELF_LOOP"
PIPE_ENDPOINT_UNKNOWN = "PIPE_ENDPOINT_UNKNOWN"
NON_VALVE_PIPE_CLOSED = "NON_VALVE_PIPE_CLOSED"
# 复供类问题
EVENT_NOT_FOUND = "EVENT_NOT_FOUND"
EVENT_ALREADY_RESTORED = "EVENT_ALREADY_RESTORED"
RESTORE_UNSAFE = "RESTORE_UNSAFE"

# 结构性阻断问题（预演会给出，提交据此拒绝）
BLOCKING_ISSUE_CODES = frozenset(
    {
        FAULT_PIPE_NOT_FOUND,
        CLOSE_PIPE_NOT_FOUND,
        CLOSE_PIPE_NO_VALVE,
        CLOSE_PIPE_DUPLICATE,
        CLOSE_PIPE_IS_FAULT,
    }
)

STATUS_PENDING_REPAIR = "pending_repair"
STATUS_RESTORED = "restored"
RESTORE_STATUSES = frozenset({STATUS_PENDING_REPAIR, STATUS_RESTORED})


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # source / sink / junction
    name: str | None = None


@dataclass(frozen=True)
class Pipe:
    id: str
    from_node: str
    to_node: str
    has_valve: bool = False
    valve_closed: bool = False
    name: str | None = None


@dataclass(frozen=True)
class RepairEvent:
    """隔离事件的领域视图：编号、状态、故障管段与提交时关闭的阀门。"""

    id: int
    status: str
    fault_pipe_id: str
    closed_pipe_ids: tuple[str, ...] = ()


@dataclass
class Issue:
    code: str
    message: str
    pipe_id: str | None = None
    # 复供阻断时，故障管段中仍可从水源到达的端点
    node_id: str | None = None

    def as_dict(self) -> dict:
        data = {"code": self.code, "message": self.message, "pipe_id": self.pipe_id}
        if self.node_id is not None:
            data["node_id"] = self.node_id
        return data


@dataclass
class PlanResult:
    isolatable: bool
    affected_sink_ids: list[str]
    issues: list[Issue] = field(default_factory=list)
    # 方案中实际被阻断的管段（已关 ∪ 拟关，故障管段单独移除）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class RestorationResult:
    """复供预演结果。

    safe=False 时 issues 中给出阻断原因（事件不存在 / 已复供 / 重新进水风险），
    其余字段尽力给出（事件缺失时只能为空）。
    found=False 表示该事件编号不存在（可能已随整网替换失效）。
    """

    safe: bool
    event_id: int | None
    status: str | None
    fault_pipe_id: str | None
    found: bool = True
    # 该事件提交时关闭的阀门
    event_pipe_ids: list[str] = field(default_factory=list)
    # 复供后实际会被重开（当前仍关闭且未被其他待修复事件占用）的阀门
    reopen_pipe_ids: list[str] = field(default_factory=list)
    # 复供后仍须保持关闭的阀门（被其他待修复事件占用）
    stay_closed_pipe_ids: list[str] = field(default_factory=list)
    # 预计恢复供水的用水点（当前失供、重开后通水）
    restored_sink_ids: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


# ---------------------------------------------------------------- 导入校验


def validate_import(
    nodes: list[Node], pipes: list[Pipe]
) -> list[Issue]:
    """校验整网导入数据：重号、自环、悬空端点、无阀管段初始关闭。"""
    issues: list[Issue] = []

    node_ids: set[str] = set()
    for n in nodes:
        if n.id in node_ids:
            issues.append(Issue(DUPLICATE_NODE_ID, f"节点编号重复：{n.id}", n.id))
        node_ids.add(n.id)

    pipe_ids: set[str] = set()
    for p in pipes:
        if p.id in pipe_ids:
            issues.append(Issue(DUPLICATE_PIPE_ID, f"管段编号重复：{p.id}", p.id))
        pipe_ids.add(p.id)

        if p.from_node == p.to_node:
            issues.append(Issue(PIPE_SELF_LOOP, f"管段自环（两端为同一节点）：{p.id}", p.id))
        for endpoint in (p.from_node, p.to_node):
            if endpoint not in node_ids:
                issues.append(
                    Issue(
                        PIPE_ENDPOINT_UNKNOWN,
                        f"管段 {p.id} 的端点 {endpoint} 未在节点列表中定义（悬空端点）",
                        p.id,
                    )
                )
        if p.valve_closed and not p.has_valve:
            issues.append(
                Issue(
                    NON_VALVE_PIPE_CLOSED,
                    f"管段 {p.id} 没有阀门，不能设置为关闭状态",
                    p.id,
                )
            )
    return issues


# ---------------------------------------------------------------- 可达性


def _reachable(
    nodes: dict[str, Node],
    pipes: list[Pipe],
    removed_pipe_ids: frozenset[str],
) -> set[str]:
    """无向 BFS：从全部水源出发，返回可达节点集合。

    removed_pipe_ids 中的管段视为不存在/不可通行。
    """
    adjacency: dict[str, list[str]] = {nid: [] for nid in nodes}
    for p in pipes:
        if p.id in removed_pipe_ids:
            continue
        if p.from_node not in adjacency or p.to_node not in adjacency:
            continue
        adjacency[p.from_node].append(p.to_node)
        adjacency[p.to_node].append(p.from_node)

    reachable: set[str] = set()
    queue: deque[str] = deque()
    for nid, node in nodes.items():
        if node.type == "source":
            if nid not in reachable:
                reachable.add(nid)
                queue.append(nid)

    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    return reachable


# ---------------------------------------------------------------- 方案评估


def validate_plan_request(
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool,
) -> list[Issue]:
    """校验故障/拟关列表本身的合法性。

    for_submit=True 时额外校验“已关闭阀门不得重复关闭”。
    """
    issues: list[Issue] = []

    if fault_pipe_id not in pipes:
        issues.append(
            Issue(FAULT_PIPE_NOT_FOUND, f"故障管段不存在：{fault_pipe_id}", fault_pipe_id)
        )

    seen: set[str] = set()
    for pid in close_pipe_ids:
        if pid in seen:
            issues.append(Issue(CLOSE_PIPE_DUPLICATE, f"拟关闭管段重复：{pid}", pid))
            continue
        seen.add(pid)

        if pid == fault_pipe_id:
            issues.append(
                Issue(CLOSE_PIPE_IS_FAULT, "不能把故障管段本身作为关闭阀门", pid)
            )
            continue
        pipe = pipes.get(pid)
        if pipe is None:
            issues.append(Issue(CLOSE_PIPE_NOT_FOUND, f"拟关闭管段不存在：{pid}", pid))
        elif not pipe.has_valve:
            issues.append(Issue(CLOSE_PIPE_NO_VALVE, f"管段没有阀门，无法关闭：{pid}", pid))
        elif for_submit and pipe.valve_closed:
            issues.append(
                Issue(
                    CLOSE_PIPE_DUPLICATE,
                    f"阀门已处于关闭状态，不能重复关闭：{pid}",
                    pid,
                )
            )
    return issues


def evaluate_plan(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool = False,
    pending_fault_pipe_ids: frozenset[str] = frozenset(),
) -> PlanResult:
    """评估隔离方案。

    预演(for_submit=False)：只做结构校验，已关闭阀门不会产生“重复关闭”问题；
    提交(for_submit=True)：对已关闭阀门再次关闭视为 CLOSE_PIPE_DUPLICATE。

    pending_fault_pipe_ids：其余“待修复”事件的故障管段。这些管段尚未修复，
    在方案前、方案后的通水图中都视为不可通行（本次故障管段单独移除）。
    """
    issues = validate_plan_request(
        pipes, fault_pipe_id, close_pipe_ids, for_submit=for_submit
    )
    blocking = [i for i in issues if i.code in BLOCKING_ISSUE_CODES]

    fault = pipes.get(fault_pipe_id)
    if fault is None or blocking:
        # 结构不合法：无法进行可达性评估
        return PlanResult(
            isolatable=False, affected_sink_ids=[], issues=issues
        )

    pipe_list = list(pipes.values())

    # 其他待修复事件的故障管段仍断管，始终不可通行
    other_pending = frozenset(
        pid for pid in pending_fault_pipe_ids if pid != fault_pipe_id and pid in pipes
    )

    # 方案前（当前通水态）：已关闭阀门、待修复故障管段不可通行；本次故障管段尚未移除
    already_closed = frozenset(
        p.id for p in pipe_list if p.has_valve and p.valve_closed
    )
    before_removed = already_closed | other_pending
    before_reachable = _reachable(nodes, pipe_list, before_removed)

    # 方案后：故障管段移除；已关阀门继续生效；拟关阀门全部关闭
    blocked = already_closed | frozenset(close_pipe_ids)
    after_removed = blocked | other_pending | frozenset({fault_pipe_id})
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    both_ends_isolated = (
        fault.from_node not in after_reachable
        and fault.to_node not in after_reachable
    )

    affected = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid in before_reachable and nid not in after_reachable
    )

    return PlanResult(
        isolatable=both_ends_isolated,
        affected_sink_ids=affected,
        issues=issues,
        blocked_pipe_ids=frozenset(blocked),
    )


# ---------------------------------------------------------------- 复供评估


def evaluate_restoration(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    events: dict[int, RepairEvent],
    event_id: int,
) -> RestorationResult:
    """评估某隔离事件的复供方案（只读，不改变任何状态）。

    - 事件不存在：safe=False，issues=[EVENT_NOT_FOUND]
    - 事件已复供：safe=False，issues=[EVENT_ALREADY_RESTORED]
    - 复供图 = 目标事件故障管段视为已修复、其独占阀门重开；
      其他待修复事件的故障管段继续移除，仍被其占用的阀门继续关闭。
    - 仅当复供图中其余待修复故障管段两端仍都无法从水源到达才 safe=True。
    """
    event = events.get(event_id)
    if event is None:
        return RestorationResult(
            safe=False,
            event_id=event_id,
            status=None,
            fault_pipe_id=None,
            found=False,
            issues=[
                Issue(
                    EVENT_NOT_FOUND,
                    f"隔离事件不存在：{event_id}（可能已随整网替换失效）",
                )
            ],
        )

    if event.status == STATUS_RESTORED:
        return RestorationResult(
            safe=False,
            event_id=event.id,
            status=event.status,
            fault_pipe_id=event.fault_pipe_id,
            event_pipe_ids=list(event.closed_pipe_ids),
            issues=[
                Issue(
                    EVENT_ALREADY_RESTORED,
                    f"隔离事件 {event.id} 已复供，不能重复确认",
                    event.fault_pipe_id,
                )
            ],
        )

    pipe_list = list(pipes.values())

    other_pending = [
        e
        for e in events.values()
        if e.id != event.id and e.status == STATUS_PENDING_REPAIR
    ]
    # 其他待修复事件的故障管段：复供后仍然断管
    others_faults = frozenset(e.fault_pipe_id for e in other_pending)

    # 当前实际关闭的阀门（阀位表为准）
    current_closed = frozenset(
        p.id for p in pipe_list if p.has_valve and p.valve_closed
    )
    # 其他待修复事件仍占用的阀门：重开本事件后必须保持关闭
    valves_held_by_others = frozenset(
        pid for e in other_pending for pid in e.closed_pipe_ids
    )
    # 本事件登记关闭、且当前确实关闭、且不被其他事件占用的阀门才会真正重开
    event_valves = frozenset(
        pid for pid in event.closed_pipe_ids if pid in pipes and pipes[pid].has_valve
    )
    reopen = current_closed & event_valves - valves_held_by_others
    after_closed = current_closed - reopen

    # 当前通水图：所有已关阀门 + 所有待修复故障管段（含本事件）
    all_pending_faults = others_faults | frozenset({event.fault_pipe_id})
    now_reachable = _reachable(
        nodes, pipe_list, current_closed | all_pending_faults
    )
    # 复供图：本事件已修复（其故障管段恢复通行），仅排除其他待修复故障与仍关阀门
    after_reachable = _reachable(
        nodes, pipe_list, after_closed | others_faults
    )

    restored_sinks = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink"
        and nid not in now_reachable
        and nid in after_reachable
    )

    # 重新进水风险：其他待修复故障管段只要任一端重新可达即不安全
    issues: list[Issue] = []
    for other in other_pending:
        fault = pipes.get(other.fault_pipe_id)
        if fault is None:
            continue
        for endpoint in (fault.from_node, fault.to_node):
            if endpoint in after_reachable:
                issues.append(
                    Issue(
                        RESTORE_UNSAFE,
                        f"复供事件 {event.id} 后，待修复故障管段 {other.fault_pipe_id} "
                        f"的端点 {endpoint} 将重新从水源到达，存在重新进水风险",
                        other.fault_pipe_id,
                        endpoint,
                    )
                )

    return RestorationResult(
        safe=not issues,
        event_id=event.id,
        status=event.status,
        fault_pipe_id=event.fault_pipe_id,
        event_pipe_ids=sorted(event_valves),
        reopen_pipe_ids=sorted(reopen),
        stay_closed_pipe_ids=sorted(
            (event_valves & valves_held_by_others) | (current_closed - event_valves)
        ),
        restored_sink_ids=restored_sinks,
        issues=issues,
    )
