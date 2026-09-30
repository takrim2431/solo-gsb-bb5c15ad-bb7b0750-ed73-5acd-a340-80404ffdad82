"""故障隔离核心逻辑的单元测试（仅依赖标准库）。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.planning import (  # noqa: E402
    Node,
    Pipe,
    RepairEvent,
    evaluate_plan,
    evaluate_restoration,
    validate_import,
    DUPLICATE_NODE_ID,
    DUPLICATE_PIPE_ID,
    PIPE_SELF_LOOP,
    PIPE_ENDPOINT_UNKNOWN,
    NON_VALVE_PIPE_CLOSED,
    FAULT_PIPE_NOT_FOUND,
    CLOSE_PIPE_NOT_FOUND,
    CLOSE_PIPE_NO_VALVE,
    CLOSE_PIPE_DUPLICATE,
    CLOSE_PIPE_IS_FAULT,
    EVENT_NOT_FOUND,
    EVENT_ALREADY_RESTORED,
    RESTORE_UNSAFE,
    STATUS_PENDING_REPAIR,
    STATUS_RESTORED,
)


def build_demo():
    """演示管网：

    S --P1(v)-- A --P2(v)-- B --P4(v)-- D --P6(无阀)-- U3
                 |           |
               P3(v)       P5(v)
                 |           |
                 U1          U2
    故障管段 P6（B? 不：D-U3）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
        "U3": Node("U3", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True),
        "P5": Pipe("P5", "B", "U2", has_valve=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
    }
    return nodes, pipes


class ImportValidationTests(unittest.TestCase):
    def test_valid_import(self):
        nodes, pipes = build_demo()
        self.assertEqual(validate_import(list(nodes.values()), list(pipes.values())), [])

    def test_dangling_endpoint(self):
        nodes = [Node("S", "source")]
        pipes = [Pipe("P1", "S", "X")]
        issues = validate_import(nodes, pipes)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, PIPE_ENDPOINT_UNKNOWN)

    def test_self_loop(self):
        nodes = [Node("A", "junction")]
        pipes = [Pipe("P1", "A", "A")]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == PIPE_SELF_LOOP for i in issues))

    def test_duplicate_ids(self):
        nodes = [Node("A", "junction"), Node("A", "sink")]
        pipes = [
            Pipe("P1", "A", "A"),  # 自环也会报
            Pipe("P1", "A", "A"),
        ]
        issues = validate_import(nodes, pipes)
        codes = [i.code for i in issues]
        self.assertIn(DUPLICATE_NODE_ID, codes)
        self.assertIn(DUPLICATE_PIPE_ID, codes)

    def test_non_valve_closed_rejected(self):
        nodes = [Node("S", "source"), Node("U", "sink")]
        pipes = [Pipe("P1", "S", "U", has_valve=False, valve_closed=True)]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == NON_VALVE_PIPE_CLOSED for i in issues))


class IsolationPlanTests(unittest.TestCase):
    def test_isolatable_plan_with_one_closed_valve(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4"])
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U3"])
        self.assertEqual(result.issues, [])

    def test_non_isolatable_plan(self):
        nodes, pipes = build_demo()
        # 只关 P5（B-U2）：D 仍可经 P4 从水源到达，故障管段隔离失败
        result = evaluate_plan(nodes, pipes, "P6", ["P5"])
        self.assertFalse(result.isolatable)
        # U2 因关阀失供，U3 因故障管段移除而失供，均按“前可达后不可达”统计
        self.assertEqual(result.affected_sink_ids, ["U2", "U3"])

    def test_already_closed_valve_keeps_effect_preview(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        # 预演时无需重复关闭，已关阀门继续生效
        result = evaluate_plan(nodes, pipes, "P6", [])
        self.assertTrue(result.isolatable)
        # P4 此前已关闭，U3 在方案前就已失供，不计入本次新增失供用水点
        self.assertEqual(result.affected_sink_ids, [])

    def test_submit_rejects_closing_already_closed_valve(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        result = evaluate_plan(nodes, pipes, "P6", ["P4"], for_submit=True)
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_DUPLICATE for i in result.issues))

    def test_fault_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "PX", ["P4"])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.issues[0].code, FAULT_PIPE_NOT_FOUND)

    def test_close_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["PX"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NOT_FOUND for i in result.issues))

    def test_close_pipe_without_valve(self):
        nodes, pipes = build_demo()
        # P6 是故障管段，单独构造一个无阀且非故障的管段来测试
        nodes["E"] = Node("E", "junction")
        pipes["P7"] = Pipe("P7", "B", "E", has_valve=False)
        result = evaluate_plan(nodes, pipes, "P6", ["P7"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NO_VALVE for i in result.issues))

    def test_close_fault_pipe_itself_rejected(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P6"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_IS_FAULT for i in result.issues))

    def test_duplicate_close_entries(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4", "P4"])
        self.assertFalse(result.isolatable)
        dupes = [i for i in result.issues if i.code == CLOSE_PIPE_DUPLICATE]
        self.assertEqual(len(dupes), 1)

    def test_source_endpoint_always_reachable(self):
        # 故障管段直连水源且无额外阀门：水源端恒可达，不能隔离
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P1", [])
        self.assertFalse(result.isolatable)

    def test_affected_sinks_sorted(self):
        nodes, pipes = build_demo()
        # 关 P3 后移除 P6：U1、U3 失供（U1 字典序在前）
        result = evaluate_plan(nodes, pipes, "P6", ["P3"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U3"])
        self.assertFalse(result.isolatable)

    def test_no_affected_sink_when_fault_is_on_dead_branch_without_sink(self):
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A", has_valve=True),
            "P2": Pipe("P2", "A", "B", has_valve=True),
            "P3": Pipe("P3", "A", "U1", has_valve=True),
        }
        # 故障 P2（A-B，B 为盲端连接点），关 P2 自身不可（是故障）；
        # 改为故障 P2、无阀门可关 -> 不可隔离；失供用水点为空
        result = evaluate_plan(nodes, pipes, "P2", [])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.affected_sink_ids, [])


def build_two_fault_network():
    """两段故障的管网（e1: P6/D-U3；e2: P9/E-U4，共用 D 点支路）：

    S -P1(v)- A -P2(v)- B -P4(v)- D -P6(v)- U3
                        |            \
                      P5(v)          P8(v)- E -P9(v)- U4
                        |
                       U2
    e1 提交：故障 P6，关 P4；e2 在 P4 已关后提交：故障 P9，无需再关阀
    （D-E-U4 整条支路因 P4 关闭而与水源断开）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "E": Node("E", "junction"),
        "U2": Node("U2", "sink"),
        "U3": Node("U3", "sink"),
        "U4": Node("U4", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True),
        "P5": Pipe("P5", "B", "U2", has_valve=True),
        "P6": Pipe("P6", "D", "U3", has_valve=True),
        "P8": Pipe("P8", "D", "E", has_valve=True),
        "P9": Pipe("P9", "E", "U4", has_valve=True),
    }
    return nodes, pipes


class PendingFaultPlanTests(unittest.TestCase):
    def test_pending_fault_pipe_excluded_from_water_path(self):
        # 待修复事件的故障管段 P4 已断管：故障 P6 即使不补关阀门也可隔离
        nodes, pipes = build_demo()
        result = evaluate_plan(
            nodes, pipes, "P6", [], pending_fault_pipe_ids=frozenset({"P4"})
        )
        self.assertTrue(result.isolatable)
        # U3 在方案前就因 P4 断管失供，不重复计入
        self.assertEqual(result.affected_sink_ids, [])

    def test_pending_fault_sink_not_counted_again(self):
        # 待修复故障 P5（B-U2）：U2 方案前已失供，只统计本次新增的 U3
        nodes, pipes = build_demo()
        result = evaluate_plan(
            nodes, pipes, "P6", ["P4"], pending_fault_pipe_ids=frozenset({"P5"})
        )
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U3"])

    def test_isolatable_when_other_event_already_closed_branch(self):
        # 对应 e2 的提交场景：P4 已关、P6 待修复，故障 P9 无需关阀即可隔离
        nodes, pipes = build_two_fault_network()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        result = evaluate_plan(
            nodes,
            pipes,
            "P9",
            [],
            pending_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, [])


class RestorationTests(unittest.TestCase):
    def test_restore_single_event_safe(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        events = {
            1: RepairEvent(1, STATUS_PENDING_REPAIR, "P6", ("P4",)),
        }
        result = evaluate_restoration(nodes, pipes, events, 1)
        self.assertTrue(result.safe, result.issues)
        self.assertEqual(result.event_pipe_ids, ["P4"])
        self.assertEqual(result.reopen_pipe_ids, ["P4"])
        self.assertEqual(result.stay_closed_pipe_ids, [])
        self.assertEqual(result.restored_sink_ids, ["U3"])
        self.assertEqual(result.issues, [])

    def test_restore_outer_event_first_is_unsafe(self):
        # 先复供 e1（重开 P4）会让 e2 的故障管段 P9 的 E 端重新进水
        nodes, pipes = build_two_fault_network()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        events = {
            1: RepairEvent(1, STATUS_PENDING_REPAIR, "P6", ("P4",)),
            2: RepairEvent(2, STATUS_PENDING_REPAIR, "P9", ()),
        }
        result = evaluate_restoration(nodes, pipes, events, 1)
        self.assertFalse(result.safe)
        codes = [i.code for i in result.issues]
        self.assertEqual(codes, [RESTORE_UNSAFE])
        self.assertEqual(result.issues[0].pipe_id, "P9")
        self.assertEqual(result.issues[0].node_id, "E")
        # 预演仍给出预计恢复用水点，但不允许确认
        self.assertEqual(result.restored_sink_ids, ["U3"])
        self.assertEqual(result.reopen_pipe_ids, ["P4"])

    def test_restore_inner_event_first_safe(self):
        # 先复供 e2：没有阀门要重开，且 e1 的故障管段 P6 两端仍不可达
        nodes, pipes = build_two_fault_network()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        events = {
            1: RepairEvent(1, STATUS_PENDING_REPAIR, "P6", ("P4",)),
            2: RepairEvent(2, STATUS_PENDING_REPAIR, "P9", ()),
        }
        result = evaluate_restoration(nodes, pipes, events, 2)
        self.assertTrue(result.safe, result.issues)
        self.assertEqual(result.reopen_pipe_ids, [])
        self.assertEqual(result.restored_sink_ids, [])

    def test_restore_after_inner_restored_brings_both_sinks_back(self):
        # e2 已复供后再复供 e1：U3、U4 同时恢复，按编号排序
        nodes, pipes = build_two_fault_network()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        events = {
            1: RepairEvent(1, STATUS_PENDING_REPAIR, "P6", ("P4",)),
            2: RepairEvent(2, STATUS_RESTORED, "P9", ()),
        }
        result = evaluate_restoration(nodes, pipes, events, 1)
        self.assertTrue(result.safe, result.issues)
        self.assertEqual(result.reopen_pipe_ids, ["P4"])
        self.assertEqual(result.restored_sink_ids, ["U3", "U4"])

    def test_restore_valve_held_by_other_event_stays_closed(self):
        # P4 同时被两个事件占用：复供 e1 时 P4 必须保持关闭
        nodes, pipes = build_two_fault_network()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        pipes["P8"] = Pipe("P8", "D", "E", has_valve=True, valve_closed=True)
        events = {
            1: RepairEvent(1, STATUS_PENDING_REPAIR, "P6", ("P4",)),
            2: RepairEvent(2, STATUS_PENDING_REPAIR, "P9", ("P4", "P8")),
        }
        result = evaluate_restoration(nodes, pipes, events, 1)
        self.assertTrue(result.safe, result.issues)
        self.assertEqual(result.reopen_pipe_ids, [])
        self.assertEqual(result.stay_closed_pipe_ids, ["P4", "P8"])
        # P4 不开，U3 仍失供
        self.assertEqual(result.restored_sink_ids, [])

    def test_restore_event_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_restoration(nodes, pipes, {}, 99)
        self.assertFalse(result.safe)
        self.assertIsNone(result.status)
        self.assertEqual(result.issues[0].code, EVENT_NOT_FOUND)

    def test_restore_already_restored(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        events = {
            1: RepairEvent(1, STATUS_RESTORED, "P6", ("P4",)),
        }
        result = evaluate_restoration(nodes, pipes, events, 1)
        self.assertFalse(result.safe)
        self.assertEqual(result.issues[0].code, EVENT_ALREADY_RESTORED)
        self.assertEqual(result.event_pipe_ids, ["P4"])


if __name__ == "__main__":
    unittest.main()
