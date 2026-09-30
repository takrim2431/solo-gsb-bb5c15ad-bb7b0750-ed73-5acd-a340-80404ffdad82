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
    evaluate_plan,
    evaluate_restore,
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
    FAULT_REWATER_RISK,
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

    def test_pending_fault_pipes_are_blocked_in_plan(self):
        # 待修复事件的故障管段在方案前后均视为不可通行：
        # 其下游用水点方案前已失供，不计入本次 affected
        nodes, pipes = build_demo()
        # P6（D-U3）已是另一条待修复事件的故障管段
        result = evaluate_plan(
            nodes,
            pipes,
            "P5",                      # 本次故障 B-U2
            ["P5"],
            pending_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertFalse(result.isolatable)  # B 仍从 P2 进水
        self.assertNotIn("U3", result.affected_sink_ids)


def build_restore_demo():
    """复供演示管网：

    S --P1(v)-- A --P2(v)-- B --P4(v,关)-- D --P6(无阀)-- U3
                 |                    |
               P3(v)                P7(无阀)
                 |                    |
                 U1            P8(v,开) -- E -- U4
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "E": Node("E", "junction"),
        "U1": Node("U1", "sink"),
        "U3": Node("U3", "sink"),
        "U4": Node("U4", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True, valve_closed=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
        "P7": Pipe("P7", "D", "E", has_valve=False),
        "P8": Pipe("P8", "E", "U4", has_valve=True),
    }
    return nodes, pipes


class RestorePlanningTests(unittest.TestCase):
    def test_safe_restore_when_other_faults_stay_isolated(self):
        nodes, pipes = build_restore_demo()
        # 片区内事件（未关阀）复供：重开集合为空，仍关的 P4 保护故障 P6
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset(),
            other_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, [])
        self.assertEqual(result.blocks, [])

    def test_unsafe_restore_when_pending_fault_end_gets_water(self):
        nodes, pipes = build_restore_demo()
        # 外层事件（关 P4）复供，而片区内 P7 仍待修复：D 端重新进水
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset({"P7"}),
        )
        self.assertFalse(result.safe)
        self.assertEqual(len(result.blocks), 1)
        block = result.blocks[0]
        self.assertEqual(block.fault_pipe_id, "P7")
        self.assertEqual(block.reachable_endpoints, ["D"])
        self.assertEqual(block.as_dict()["code"], FAULT_REWATER_RISK)
        # U3 恢复；U4 因 P7 自身阻断仍不可达
        self.assertEqual(result.restored_sink_ids, ["U3"])

    def test_restore_becomes_safe_after_inner_event_restored(self):
        nodes, pipes = build_restore_demo()
        # 没有其他待修复故障时，重开 P4，U3/U4 同时恢复
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset(),
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, ["U3", "U4"])

    def test_other_closed_valves_keep_effect(self):
        # 复供只重开本事件阀门；另一事件关闭的 P2 继续生效
        nodes, pipes = build_restore_demo()
        pipes["P2"] = Pipe("P2", "A", "B", has_valve=True, valve_closed=True)
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset(),
        )
        # P2 关闭时 B/D 侧整体仍无水，U3/U4 不会恢复，但也无风险
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, [])


if __name__ == "__main__":
    unittest.main()
