"""自动布局：把设备摆到合理的坐标上，省得手工填 cx/cy。"""

from __future__ import annotations

import math
from collections import defaultdict, deque

from .topo import Device, Topology, TopologyError

# eNSP 画布上比较舒服的间距
COL_GAP = 165.0
ROW_GAP = 150.0
ORIGIN_X = 120.0
ORIGIN_Y = 90.0

MODES = ("layered", "tree", "grid", "ring")

# 分层布局在没有连线可依据时的兜底次序
_CATEGORY_DEPTH = {
    "OTHER": 0,
    "AR": 1,
    "FW": 2,
    "LSW": 3,
    "WLAN": 4,
    "CLIENT": 5,
}


def _adjacency(topo: Topology) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for device in topo.devices:
        graph[device.dev_id] = set()
    for link in topo.links:
        if link.src_id in graph and link.dst_id in graph:
            graph[link.src_id].add(link.dst_id)
            graph[link.dst_id].add(link.src_id)
    return graph


def _infer_layers(topo: Topology) -> list[list[Device]]:
    """按到出口设备的跳数分层，孤立设备退回按类别归层。"""
    graph = _adjacency(topo)
    by_id = topo.by_id()

    roots = [d.dev_id for d in topo.devices if d.model == "Cloud"]
    if not roots:
        roots = [d.dev_id for d in topo.devices if d.spec.category == "AR"]
    if not roots and topo.devices:
        roots = [max(topo.devices, key=lambda d: len(graph[d.dev_id])).dev_id]

    depth: dict[str, int] = {}
    queue: deque[str] = deque()
    for root in roots:
        depth[root] = 0
        queue.append(root)
    while queue:
        current = queue.popleft()
        for neighbour in sorted(graph[current]):
            if neighbour not in depth:
                depth[neighbour] = depth[current] + 1
                queue.append(neighbour)

    # 没连上任何东西的设备，按类别给个层号，免得全挤在第 0 层
    for device in topo.devices:
        if device.dev_id not in depth:
            depth[device.dev_id] = _CATEGORY_DEPTH.get(device.spec.category, 3)

    # 终端一律沉到最底层，视觉上更像真实拓扑图
    if depth:
        bottom = max(depth.values())
        for device in topo.devices:
            if device.spec.category == "CLIENT":
                depth[device.dev_id] = max(depth[device.dev_id], bottom)

    grouped: dict[int, list[Device]] = defaultdict(list)
    for dev_id, level in depth.items():
        grouped[level].append(by_id[dev_id])
    return [grouped[k] for k in sorted(grouped)]


def _place_rows(rows: list[list[Device]]) -> None:
    """把每层水平居中排开。"""
    if not rows:
        return
    widest = max(len(row) for row in rows)
    span = max(widest - 1, 1) * COL_GAP
    for row_index, row in enumerate(rows):
        row.sort(key=lambda d: d.name)
        count = len(row)
        row_span = (count - 1) * COL_GAP
        start = ORIGIN_X + (span - row_span) / 2
        for col_index, device in enumerate(row):
            device.cx = start + col_index * COL_GAP
            device.cy = ORIGIN_Y + row_index * ROW_GAP
            device.edit_left = None
            device.edit_top = None


def _layered(topo: Topology, groups: list[list[str]] | None) -> list[list[str]]:
    if groups:
        seen: set[str] = set()
        rows: list[list[Device]] = []
        for group in groups:
            row = []
            for name in group:
                device = topo.find(name)
                if device.dev_id in seen:
                    raise TopologyError(f"设备 {device.name} 在 groups 里出现了多次。")
                seen.add(device.dev_id)
                row.append(device)
            rows.append(row)
        leftovers = [d for d in topo.devices if d.dev_id not in seen]
        if leftovers:
            rows.append(leftovers)
    else:
        rows = _infer_layers(topo)

    _place_rows(rows)
    return [[d.name for d in row] for row in rows]


def _tree(topo: Topology, root_name: str | None) -> list[list[str]]:
    graph = _adjacency(topo)
    by_id = topo.by_id()

    if root_name:
        root = topo.find(root_name).dev_id
    elif topo.devices:
        root = max(topo.devices, key=lambda d: len(graph[d.dev_id])).dev_id
    else:
        return []

    depth = {root: 0}
    queue = deque([root])
    while queue:
        current = queue.popleft()
        for neighbour in sorted(graph[current], key=lambda i: by_id[i].name):
            if neighbour not in depth:
                depth[neighbour] = depth[current] + 1
                queue.append(neighbour)
    for device in topo.devices:
        depth.setdefault(device.dev_id, max(depth.values(), default=0) + 1)

    grouped: dict[int, list[Device]] = defaultdict(list)
    for dev_id, level in depth.items():
        grouped[level].append(by_id[dev_id])
    rows = [grouped[k] for k in sorted(grouped)]
    _place_rows(rows)
    return [[d.name for d in row] for row in rows]


def _grid(topo: Topology, columns: int | None) -> list[list[str]]:
    devices = sorted(topo.devices, key=lambda d: (d.spec.category, d.name))
    if not devices:
        return []
    width = columns or max(1, math.ceil(math.sqrt(len(devices))))
    rows = [devices[i : i + width] for i in range(0, len(devices), width)]
    _place_rows(rows)
    return [[d.name for d in row] for row in rows]


def _ring(topo: Topology) -> list[list[str]]:
    devices = sorted(topo.devices, key=lambda d: d.name)
    count = len(devices)
    if count == 0:
        return []
    radius = max(160.0, COL_GAP * count / (2 * math.pi))
    centre_x = ORIGIN_X + radius + 60
    centre_y = ORIGIN_Y + radius + 60
    for index, device in enumerate(devices):
        angle = 2 * math.pi * index / count - math.pi / 2
        device.cx = centre_x + radius * math.cos(angle)
        device.cy = centre_y + radius * math.sin(angle)
        device.edit_left = None
        device.edit_top = None
    return [[d.name for d in devices]]


def apply(
    topo: Topology,
    mode: str = "layered",
    groups: list[list[str]] | None = None,
    root: str | None = None,
    columns: int | None = None,
) -> dict:
    """就地重排所有设备坐标，返回每一层放了哪些设备。"""
    if mode not in MODES:
        raise TopologyError(f"布局模式只能是 {', '.join(MODES)}，收到 {mode!r}。")
    if columns is not None and (isinstance(columns, bool) or not isinstance(columns, int) or columns <= 0):
        raise TopologyError("columns 必须是正整数。")

    if mode == "layered":
        rows = _layered(topo, groups)
    elif mode == "tree":
        rows = _tree(topo, root)
    elif mode == "grid":
        rows = _grid(topo, columns)
    else:
        rows = _ring(topo)

    return {"mode": mode, "rows": rows, "rowCount": len(rows)}


def reset_link_labels(topo: Topology) -> int:
    """设备重排后清除接口标签的手工偏移，让 eNSP 按新端点重新放置。"""
    changed = 0
    for link in topo.links:
        if link._element is None:
            continue
        pair = link._element.find("interfacePair")
        if pair is None:
            continue
        before = tuple(
            pair.get(key)
            for key in (
                "srcBoundRectIsMoved",
                "srcOffset_X",
                "srcOffset_Y",
                "tarBoundRectIsMoved",
                "tarOffset_X",
                "tarOffset_Y",
            )
        )
        pair.set("srcBoundRectIsMoved", "0")
        pair.set("srcOffset_X", "0.000000")
        pair.set("srcOffset_Y", "0.000000")
        pair.set("tarBoundRectIsMoved", "0")
        pair.set("tarOffset_X", "0.000000")
        pair.set("tarOffset_Y", "0.000000")
        after = tuple(
            pair.get(key)
            for key in (
                "srcBoundRectIsMoved",
                "srcOffset_X",
                "srcOffset_Y",
                "tarBoundRectIsMoved",
                "tarOffset_X",
                "tarOffset_Y",
            )
        )
        if before != after:
            changed += 1
    return changed
