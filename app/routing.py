"""通道改派核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 请求沿用 ``POST /compile`` 的口令数组，另附一份按 id 指定的备用通道列表；
  未列出的口令只能留在原通道，列出的口令候选通道为「原通道 + 备用通道」
  （原通道始终可选），最多 ``MAX_ALTERNATIVE_COMMANDS`` 条口令可携带备用通道。
* 先复用 ``app.compiler`` 的编排器解析并校验时间；改派只改变口令所在通道，
  **不改变任何口令的起止时刻**。
* 时间重叠（半开区间）的口令不得分到同一通道；首尾相接（前一个 end 等于后一个
  start）可以共用通道。
* 在所有无冲突方案中，全局选择 **改派口令数最少** 者；仍有并列时，把全部分配
  按口令 id 排序后得到的通道名序列做字典序比较，取最小者。
* 未知 id、空字符串或重复的备用通道、备用通道口令数超限，以及无可行方案时
  抛出结构化错误，由 HTTP 层统一转换为 422。
"""

from __future__ import annotations

from typing import Any

from .compiler import CompileError, compile_commands

# 允许携带备用通道的口令条数上限：把搜索空间约束在小规模内
MAX_ALTERNATIVE_COMMANDS = 12


class RoutingError(CompileError):
    """通道改派失败的业务异常；复用 CompileError 的 422 转换通道。"""


def assign_channels(
    commands: list[dict[str, Any]], alternatives: list[dict[str, Any]]
) -> dict[str, Any]:
    """在不改变起止时刻的前提下做通道改派，返回改派后的 ``timeline`` 与 ``changed_ids``。

    不会修改传入的 ``commands`` / ``alternatives``。

    :raises CompileError: 口令数组非法（重复 id、依赖环等，与 /compile 相同）。
    :raises RoutingError: 备用通道列表非法（超上限、重复 id、未知 id、空通道名、
        重复通道）或不存在无冲突的改派方案时抛出，``code`` 可用于程序化区分错误类型。
    """
    # 1) 复用编排器解析并校验时间（拷贝一份，避免 setdefault 改动调用方数据）；
    #    改派只换通道，起止时刻以这次解析为准且保持不变
    timeline = compile_commands([dict(item) for item in commands])["timeline"]

    # 2) 备用通道列表的字段级校验
    options = _validate_alternatives(timeline, alternatives)

    # 3) 冲突图：时间重叠（半开区间）的口令之间连边，同通道即冲突
    ids = [entry["id"] for entry in timeline]
    starts = {entry["id"]: entry["start_ms"] for entry in timeline}
    ends = {entry["id"]: entry["end_ms"] for entry in timeline}
    adjacency = _build_overlap_graph(ids, starts, ends)

    # 4) 各连通分量独立求解（分量之间互不影响），合并得到全局最优：
    #    总改派数 = 各分量最小改派数之和；字典序最小的全局通道名序列
    #    也由各分量各自的最小序列合并而成
    original = {entry["id"]: entry["channel"] for entry in timeline}
    assignment: dict[str, str] = {}
    for component in _connected_components(ids, adjacency):
        solved = _solve_component(component, adjacency, options, original)
        if solved is None:
            raise RoutingError(
                "NO_FEASIBLE_ASSIGNMENT",
                "不存在无冲突的改派方案：可用通道不足以隔开所有时间重叠的口令",
            )
        assignment.update(solved)

    # 5) 输出：timeline 保持 /compile 的 (start_ms, id) 排序，仅替换通道
    changed_ids = sorted(cid for cid in ids if assignment[cid] != original[cid])
    return {
        "timeline": [
            {**entry, "channel": assignment[entry["id"]]} for entry in timeline
        ],
        "changed_ids": changed_ids,
    }


def _validate_alternatives(
    timeline: list[dict[str, Any]], alternatives: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """校验备用通道列表，返回每条口令的候选通道（升序，原通道始终在列）。

    未列出的口令候选通道只有原通道；备用通道与原通道相同时静默去重。
    """
    if len(alternatives) > MAX_ALTERNATIVE_COMMANDS:
        raise RoutingError(
            "TOO_MANY_ALTERNATIVES",
            f"携带备用通道的口令最多 {MAX_ALTERNATIVE_COMMANDS} 条，"
            f"实际收到 {len(alternatives)} 条",
            {"count": len(alternatives), "max": MAX_ALTERNATIVE_COMMANDS},
        )

    # 同一 id 在列表中出现多次属于重复指定
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for alt in alternatives:
        aid = alt["id"]
        if aid in seen and aid not in duplicate_ids:
            duplicate_ids.append(aid)
        seen.add(aid)
    if duplicate_ids:
        raise RoutingError(
            "DUPLICATE_ID",
            f"备用通道列表中存在重复的口令 id: {', '.join(sorted(duplicate_ids))}",
            {"duplicates": sorted(duplicate_ids)},
        )

    known_ids = {entry["id"] for entry in timeline}
    unknown = sorted(aid for aid in seen if aid not in known_ids)
    if unknown:
        raise RoutingError(
            "UNKNOWN_COMMAND_ID",
            f"备用通道列表指向未知口令 id: {', '.join(unknown)}",
            {"ids": unknown},
        )

    empty_channel_ids = sorted(
        alt["id"] for alt in alternatives if any(ch == "" for ch in alt["channels"])
    )
    if empty_channel_ids:
        raise RoutingError(
            "INVALID_CHANNEL",
            "备用通道名不能为空",
            {"ids": empty_channel_ids},
        )

    duplicate_channels = {
        alt["id"]: sorted(
            {ch for ch in alt["channels"] if alt["channels"].count(ch) > 1}
        )
        for alt in alternatives
    }
    duplicate_channels = {
        aid: channels for aid, channels in duplicate_channels.items() if channels
    }
    if duplicate_channels:
        raise RoutingError(
            "DUPLICATE_CHANNEL",
            "同一口令的备用通道列表中存在重复通道",
            {"duplicates": duplicate_channels},
        )

    original = {entry["id"]: entry["channel"] for entry in timeline}
    options: dict[str, list[str]] = {
        cid: [channel] for cid, channel in original.items()
    }
    for alt in alternatives:
        cid = alt["id"]
        # 原通道始终可选；备用通道与原通道（或彼此）相同的部分静默去重
        options[cid] = sorted({original[cid], *alt["channels"]})
    return options


def _build_overlap_graph(
    ids: list[str], starts: dict[str, int], ends: dict[str, int]
) -> dict[str, set[str]]:
    """以时间重叠（半开区间相交）为边构建无向图；首尾相接不连边。"""
    adjacency: dict[str, set[str]] = {cid: set() for cid in ids}
    ordered = sorted(ids, key=lambda cid: (starts[cid], cid))
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            if starts[right] >= ends[left]:
                break  # 后续起点更晚；相等即首尾相接，可以共用通道
            adjacency[left].add(right)
            adjacency[right].add(left)
    return adjacency


def _connected_components(
    ids: list[str], adjacency: dict[str, set[str]]
) -> list[list[str]]:
    """按 id 升序返回每个连通分量的节点列表。"""
    unseen = set(ids)
    components: list[list[str]] = []
    for cid in sorted(ids):
        if cid not in unseen:
            continue
        stack = [cid]
        unseen.discard(cid)
        component: list[str] = []
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in adjacency[node]:
                if neighbor in unseen:
                    unseen.discard(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    return components


def _solve_component(
    nodes: list[str],
    adjacency: dict[str, set[str]],
    options: dict[str, list[str]],
    original: dict[str, str],
) -> dict[str, str] | None:
    """求单个连通分量的最优分配：改派数最少，并列时通道名序列字典序最小。

    返回全部分量节点（含固定节点）的通道分配；无可行方案时返回 ``None``。
    """
    fixed = {node: options[node][0] for node in nodes if len(options[node]) == 1}
    flexible = sorted(node for node in nodes if node not in fixed)

    # 固定节点之间若时间重叠又同通道，任何改派都无法隔开
    for node, channel in fixed.items():
        for neighbor in adjacency[node]:
            if neighbor in fixed and fixed[neighbor] == channel:
                return None

    # 固定邻居占用的通道对柔性节点直接禁用
    base_blocked: dict[str, set[str]] = {node: set() for node in flexible}
    for node, channel in fixed.items():
        for neighbor in adjacency[node]:
            if neighbor in base_blocked:
                base_blocked[neighbor].add(channel)

    # 迭代加深：改派预算从 0 递增，首个可行预算即分量最少改派数；
    # 同一预算内按 id 顺序、通道名字典序深度优先，首个可行解即字典序最小者
    for budget in range(len(flexible) + 1):
        solved = _search(flexible, adjacency, options, original, base_blocked, budget)
        if solved is not None:
            return {**fixed, **solved}
    return None


def _search(
    flexible: list[str],
    adjacency: dict[str, set[str]],
    options: dict[str, list[str]],
    original: dict[str, str],
    base_blocked: dict[str, set[str]],
    budget: int,
) -> dict[str, str] | None:
    """带改派预算的深度优先搜索；找到 ``flexible`` 的通道分配或返回 ``None``。"""
    blocked = {node: set(channels) for node, channels in base_blocked.items()}
    assigned: dict[str, str] = {}

    def dfs(index: int, changes: int) -> dict[str, str] | None:
        if index == len(flexible):
            return dict(assigned)
        # 下界剪枝：原通道已被禁用的未指派节点必然改派，
        # 若加上这些必然改派仍超预算，则本分支无可行解
        forced = sum(
            1
            for node in flexible[index:]
            if original[node] in blocked[node]
        )
        if changes + forced > budget:
            return None
        node = flexible[index]
        for channel in options[node]:  # options 已按通道名字典序排列
            if channel in blocked[node]:
                continue  # 与已指派（或固定）邻居同通道，会产生冲突
            delta = 0 if channel == original[node] else 1
            if changes + delta > budget:
                continue  # 超出本预算的改派数
            assigned[node] = channel
            # 前向检查：把该通道禁用给未指派的柔性邻居，邻居无可选通道则剪枝
            pushed: list[str] = []
            feasible = True
            for neighbor in adjacency[node]:
                if neighbor in assigned or neighbor not in blocked:
                    continue  # 已指派邻居的约束在它们的 blocked 中；固定邻居同理
                if channel not in blocked[neighbor]:
                    blocked[neighbor].add(channel)
                    pushed.append(neighbor)
                    if all(opt in blocked[neighbor] for opt in options[neighbor]):
                        feasible = False
                        break
            if feasible:
                solved = dfs(index + 1, changes + delta)
                if solved is not None:
                    return solved
            for neighbor in pushed:
                blocked[neighbor].discard(channel)
            del assigned[node]
        return None

    return dfs(0, 0)
