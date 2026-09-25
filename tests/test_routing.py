"""app.routing 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import copy
import unittest

from app.compiler import CompileError
from app.routing import RoutingError, assign_channels


def cmd(cid, channel, anchor="START", offset=0, duration=1000):
    return {
        "id": cid,
        "channel": channel,
        "anchor": anchor,
        "offset_ms": offset,
        "duration_ms": duration,
    }


def alt(cid, channels):
    return {"id": cid, "channels": list(channels)}


def assign(commands, alternatives):
    return assign_channels(commands, alternatives)


def channels_of(result):
    return {entry["id"]: entry["channel"] for entry in result["timeline"]}


def times_of(result):
    return {entry["id"]: (entry["start_ms"], entry["end_ms"]) for entry in result["timeline"]}


class BasicTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(assign([], []), {"timeline": [], "changed_ids": []})

    def test_no_conflict_keeps_everything(self):
        # 原编排已无冲突：0 条改派就是最优，提供了备用通道也不使用
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=200, duration=100),
            ],
            [alt("b", ["sub"])],
        )
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(channels_of(result), {"a": "main", "b": "main"})

    def test_touching_intervals_may_share_channel(self):
        # a [0,100) 与 b [100,200) 首尾相接，不算重叠，无需改派
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", anchor="a", offset=0, duration=100),
            ],
            [],
        )
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(channels_of(result), {"a": "main", "b": "main"})

    def test_reassigns_overlapping_command(self):
        # a [0,100) 与 b [50,150) 在 main 上重叠；b 有备用通道 sub
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=50, duration=100),
            ],
            [alt("b", ["sub"])],
        )
        self.assertEqual(result["changed_ids"], ["b"])
        self.assertEqual(channels_of(result), {"a": "main", "b": "sub"})

    def test_times_are_never_changed(self):
        # 改派只换通道：所有起止时刻与 /compile 的解析结果一致
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", anchor="a", offset=-50, duration=100),
                cmd("c", "sub", offset=0, duration=80),
            ],
            [alt("b", ["sub", "backup"])],
        )
        self.assertEqual(
            times_of(result),
            {"a": (0, 100), "b": (50, 150), "c": (0, 80)},
        )

    def test_timeline_stays_sorted_by_start_then_id(self):
        result = assign(
            [
                cmd("b", "main", offset=50, duration=100),
                cmd("a", "main", offset=0, duration=100),
            ],
            [alt("b", ["sub"])],
        )
        self.assertEqual(
            [entry["id"] for entry in result["timeline"]], ["a", "b"]
        )

    def test_does_not_mutate_input(self):
        # 故意省略 anchor / offset_ms（/compile 会做 setdefault），调用后入参不应被改动
        commands = [
            {"id": "a", "channel": "main", "duration_ms": 100},
            {"id": "b", "channel": "main", "offset_ms": 50, "duration_ms": 100},
        ]
        alternatives = [alt("b", ["sub"])]
        snapshot = copy.deepcopy((commands, alternatives))
        assign(commands, alternatives)
        self.assertEqual((commands, alternatives), snapshot)


class AlternativesSemanticsTests(unittest.TestCase):
    def test_unlisted_command_must_stay(self):
        # a 没有备用通道，只能留在 main；改派的只能是 b
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=50, duration=100),
            ],
            [alt("b", ["sub"])],
        )
        self.assertEqual(result["changed_ids"], ["b"])
        self.assertEqual(channels_of(result)["a"], "main")

    def test_original_channel_is_always_an_option(self):
        # b 虽列出备用通道，但留在原通道即可无冲突：0 改派优先
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=200, duration=100),
            ],
            [alt("b", ["sub"])],
        )
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(channels_of(result)["b"], "main")

    def test_original_channel_in_backup_list_is_deduplicated(self):
        # 备用通道里重复填写原通道不算“重复的备用通道”，静默去重
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=50, duration=100),
            ],
            [alt("b", ["main", "sub"])],
        )
        self.assertEqual(result["changed_ids"], ["b"])
        self.assertEqual(channels_of(result)["b"], "sub")

    def test_empty_backup_list_means_stay(self):
        # 空备用通道列表合法：该口令仍只能留在原通道
        result = assign(
            [cmd("a", "main", offset=0, duration=100)],
            [alt("a", [])],
        )
        self.assertEqual(result["changed_ids"], [])
        self.assertEqual(channels_of(result)["a"], "main")

    def test_fixed_neighbor_blocks_channel(self):
        # a 固定在 main 且与 b 重叠：b 不能去 main，只能去 sub
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=50, duration=100),
            ],
            [alt("b", ["main", "sub"])],
        )
        self.assertEqual(channels_of(result), {"a": "main", "b": "sub"})

    def test_moved_command_must_also_avoid_its_new_channel_mates(self):
        # b 移到 sub 后与 c 重叠，故 b 必须去 backup
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=50, duration=100),
                cmd("c", "sub", offset=60, duration=100),
            ],
            [alt("b", ["sub", "backup"])],
        )
        self.assertEqual(result["changed_ids"], ["b"])
        self.assertEqual(channels_of(result)["b"], "backup")


class OptimalityTests(unittest.TestCase):
    def test_minimal_reassignment_count(self):
        # a [0,100)、b [50,150)、c [100,200) 两两中 b 与两者都重叠（a、c 首尾相接）；
        # 只改派 b 即可全部消除，优于各改派 a、c 的两条方案
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=50, duration=100),
                cmd("c", "main", offset=100, duration=100),
            ],
            [alt("a", ["x"]), alt("b", ["sub"]), alt("c", ["y"])],
        )
        self.assertEqual(result["changed_ids"], ["b"])
        self.assertEqual(channels_of(result)["b"], "sub")

    def test_tie_break_prefers_lexicographically_smaller_channel_sequence(self):
        # x、y 重叠，各有一条备用通道：改派谁都只需 1 条。
        # 按 id 排序后的通道名序列 ("main","a") < ("z","main")，故改派 y 到 a
        result = assign(
            [
                cmd("x", "main", offset=0, duration=100),
                cmd("y", "main", offset=50, duration=100),
            ],
            [alt("x", ["z"]), alt("y", ["a"])],
        )
        self.assertEqual(result["changed_ids"], ["y"])
        self.assertEqual(channels_of(result), {"x": "main", "y": "a"})

    def test_tie_break_may_move_earlier_id_to_smaller_channel(self):
        # 同为 1 条改派：("b","m") < ("m","c")，改派 x 到 b
        result = assign(
            [
                cmd("x", "m", offset=0, duration=100),
                cmd("y", "m", offset=50, duration=100),
            ],
            [alt("x", ["b"]), alt("y", ["c"])],
        )
        self.assertEqual(result["changed_ids"], ["x"])
        self.assertEqual(channels_of(result), {"x": "b", "y": "m"})

    def test_independent_components_each_optimized(self):
        # 两个互不相干的冲突分量，各自改派 1 条
        result = assign(
            [
                cmd("a", "main", offset=0, duration=100),
                cmd("b", "main", offset=50, duration=100),
                cmd("c", "sub", offset=1000, duration=100),
                cmd("d", "sub", offset=1050, duration=100),
            ],
            [alt("b", ["bak1"]), alt("d", ["bak2"])],
        )
        self.assertEqual(result["changed_ids"], ["b", "d"])
        self.assertEqual(
            channels_of(result),
            {"a": "main", "b": "bak1", "c": "sub", "d": "bak2"},
        )

    def test_no_feasible_assignment(self):
        # 三条口令两两重叠，可用通道只有 main / sub 两个：必有一对同通道
        with self.assertRaises(RoutingError) as ctx:
            assign(
                [
                    cmd("a", "main", offset=0, duration=100),
                    cmd("b", "main", offset=10, duration=100),
                    cmd("c", "main", offset=20, duration=100),
                ],
                [alt("a", ["sub"])],
            )
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_ASSIGNMENT")

    def test_no_feasible_when_fixed_commands_already_conflict(self):
        # 两条无备用通道的口令在原通道上重叠，任何改派都救不了
        with self.assertRaises(RoutingError) as ctx:
            assign(
                [
                    cmd("a", "main", offset=0, duration=100),
                    cmd("b", "main", offset=50, duration=100),
                ],
                [],
            )
        self.assertEqual(ctx.exception.code, "NO_FEASIBLE_ASSIGNMENT")


class ValidationTests(unittest.TestCase):
    def test_unknown_command_id(self):
        with self.assertRaises(RoutingError) as ctx:
            assign([cmd("a", "main")], [alt("ghost", ["sub"])])
        self.assertEqual(ctx.exception.code, "UNKNOWN_COMMAND_ID")
        self.assertEqual(ctx.exception.details["ids"], ["ghost"])

    def test_empty_backup_channel(self):
        with self.assertRaises(RoutingError) as ctx:
            assign([cmd("a", "main")], [alt("a", ["sub", ""])])
        self.assertEqual(ctx.exception.code, "INVALID_CHANNEL")
        self.assertEqual(ctx.exception.details["ids"], ["a"])

    def test_duplicate_backup_channel(self):
        with self.assertRaises(RoutingError) as ctx:
            assign([cmd("a", "main")], [alt("a", ["sub", "sub"])])
        self.assertEqual(ctx.exception.code, "DUPLICATE_CHANNEL")
        self.assertEqual(ctx.exception.details["duplicates"], {"a": ["sub"]})

    def test_duplicate_alternative_entry_id(self):
        with self.assertRaises(RoutingError) as ctx:
            assign(
                [cmd("a", "main")],
                [alt("a", ["sub"]), alt("a", ["bak"])],
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")
        self.assertEqual(ctx.exception.details["duplicates"], ["a"])

    def test_too_many_alternatives(self):
        commands = [cmd(f"c{i:02d}", "main", offset=i * 1000, duration=100) for i in range(13)]
        alternatives = [alt(f"c{i:02d}", ["sub"]) for i in range(13)]
        with self.assertRaises(RoutingError) as ctx:
            assign(commands, alternatives)
        self.assertEqual(ctx.exception.code, "TOO_MANY_ALTERNATIVES")
        self.assertEqual(ctx.exception.details["count"], 13)
        self.assertEqual(ctx.exception.details["max"], 12)

    def test_twelve_alternatives_are_allowed(self):
        commands = [cmd(f"c{i:02d}", "main", offset=i * 1000, duration=100) for i in range(12)]
        alternatives = [alt(f"c{i:02d}", ["sub"]) for i in range(12)]
        result = assign(commands, alternatives)
        self.assertEqual(result["changed_ids"], [])

    def test_command_errors_propagate(self):
        # 口令数组沿用 /compile 的校验与错误码
        with self.assertRaises(CompileError) as ctx:
            assign(
                [cmd("a", "main", anchor="b"), cmd("b", "main", anchor="a")],
                [],
            )
        self.assertEqual(ctx.exception.code, "DEPENDENCY_CYCLE")

    def test_commands_checked_before_alternatives(self):
        # 口令数组与备用通道列表同时非法时，先报口令侧错误
        with self.assertRaises(CompileError) as ctx:
            assign(
                [cmd("a", "main", duration=0)],
                [alt("ghost", [""])],
            )
        self.assertEqual(ctx.exception.code, "INVALID_DURATION")


if __name__ == "__main__":
    unittest.main()
