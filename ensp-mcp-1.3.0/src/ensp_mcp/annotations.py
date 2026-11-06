"""eNSP 原生图形、文字标注和分区布局。

eNSP 的 ``shape`` / ``txttip`` 没有稳定的对象 ID。为避免误删手工标注，
本模块只在标注容器为空时创建分区；调用方必须显式允许，才能清空并重建。
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Iterable
from xml.etree import ElementTree as ET

from .topo import Device, Topology, TopologyError


ZONE_ORIGIN_X = 40
ZONE_ORIGIN_Y = 40
ZONE_GAP_X = 55
ZONE_GAP_Y = 55
ZONE_PADDING_X = 42
ZONE_PADDING_BOTTOM = 28
ZONE_HEADER_HEIGHT = 45
ZONE_CONTENT_TOP = 24
CELL_HEIGHT = 112
CELL_GAP_X = 36
CELL_GAP_Y = 32
ICON_WIDTH = 71
DEVICE_LABEL_BOTTOM = 104

DEFAULT_ZONE_COLORS = (
    "#E8F1FF",
    "#EAF8EE",
    "#FFF3DF",
    "#F3ECFF",
    "#E7F7F7",
    "#FCEBED",
)


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def overlaps(self, other: "Rect", gap: float = 0.0) -> bool:
        return not (
            self.right + gap <= other.left
            or other.right + gap <= self.left
            or self.bottom + gap <= other.top
            or other.bottom + gap <= self.top
        )

    def contains(self, other: "Rect", padding: float = 0.0) -> bool:
        return (
            self.left + padding <= other.left
            and self.top + padding <= other.top
            and self.right - padding >= other.right
            and self.bottom - padding >= other.bottom
        )


@dataclass
class ZonePlan:
    key: str
    title: str
    devices: list[Device]
    color: str
    colorref: int
    columns: int
    rows: int
    cell_width: int
    width: int
    height: int
    left: int = 0
    top: int = 0

    @property
    def rect(self) -> Rect:
        return Rect(self.left, self.top, self.left + self.width, self.top + self.height)


def _container(root: ET.Element, tag: str) -> ET.Element:
    element = root.find(tag)
    if element is None:
        element = ET.SubElement(root, tag)
    return element


def _display_width(text: str, font_size: int) -> int:
    """保守估算 eNSP 文本宽度，兼顾中文和拉丁字符。"""
    units = 0.0
    for char in text:
        width = unicodedata.east_asian_width(char)
        units += 1.0 if width in ("W", "F") else 0.62
    return max(1, math.ceil(units * font_size))


def _normalise_color(value: object) -> tuple[str, int]:
    if not isinstance(value, str) or not value.startswith("#") or len(value) != 7:
        raise TopologyError("分区 color 必须是 #RRGGBB，例如 #E8F1FF。")
    try:
        red, green, blue = (int(value[i : i + 2], 16) for i in (1, 3, 5))
    except ValueError as exc:
        raise TopologyError("分区 color 必须是 #RRGGBB，例如 #E8F1FF。") from exc
    # eNSP 使用 Windows COLORREF，即十进制的 0x00BBGGRR。
    return value.upper(), red | (green << 8) | (blue << 16)


def _device_rect(device: Device) -> Rect:
    label_width = _display_width(device.name, 12) + 12
    centre = device.cx + ICON_WIDTH / 2
    return Rect(
        min(device.cx, centre - label_width / 2),
        device.cy,
        max(device.cx + ICON_WIDTH, centre + label_width / 2),
        device.cy + DEVICE_LABEL_BOTTOM,
    )


def _validate_title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TopologyError("每个分区都要有非空 title（也可用 name）。")
    title = value.strip()
    if len(title) > 120:
        raise TopologyError("分区标题最多 120 个字符。")
    if any(ord(char) < 32 for char in title):
        raise TopologyError("分区标题不能包含换行或其它控制字符。")
    return title


def _positive_int(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise TopologyError(f"{field} 必须是 1–{maximum} 的整数。")
    return value


def _make_plan(index: int, title: str, devices: list[Device], color: object, columns: object | None) -> ZonePlan:
    canonical, colorref = _normalise_color(color)
    count = len(devices)
    if count == 0:
        raise TopologyError(f"分区 {title!r} 没有设备。")
    if columns is None or columns == 0:
        chosen_columns = min(4, max(1, math.ceil(math.sqrt(count))))
    else:
        chosen_columns = min(count, _positive_int(columns, "分区 columns", 50))
    rows = math.ceil(count / chosen_columns)
    longest = max((_display_width(device.name, 12) for device in devices), default=0)
    cell_width = max(124, longest + 28)
    content_width = chosen_columns * cell_width + (chosen_columns - 1) * CELL_GAP_X
    title_width = _display_width(title, 14) + 30
    width = max(title_width, ZONE_PADDING_X * 2 + content_width)
    height = (
        ZONE_HEADER_HEIGHT
        + ZONE_CONTENT_TOP
        + rows * CELL_HEIGHT
        + (rows - 1) * CELL_GAP_Y
        + ZONE_PADDING_BOTTOM
    )
    return ZonePlan(
        key=f"zone-{index + 1}",
        title=title,
        devices=devices,
        color=canonical,
        colorref=colorref,
        columns=chosen_columns,
        rows=rows,
        cell_width=cell_width,
        width=math.ceil(width),
        height=math.ceil(height),
    )


def _normalise_zones(topo: Topology, zones: list[dict], include_unassigned: bool) -> tuple[list[ZonePlan], list[Device]]:
    if not isinstance(zones, list) or not zones:
        raise TopologyError("zones 必须是非空列表；不需要分区时请省略 zones。")
    if len(zones) > 100:
        raise TopologyError("一个拓扑最多创建 100 个分区。")
    seen_titles: set[str] = set()
    assigned: dict[str, str] = {}
    plans: list[ZonePlan] = []

    for index, entry in enumerate(zones):
        if not isinstance(entry, dict):
            raise TopologyError(f"zones[{index}] 必须是对象。")
        title = _validate_title(entry.get("title", entry.get("name")))
        folded = title.casefold()
        if folded in seen_titles:
            raise TopologyError(f"分区标题重复：{title}。")
        seen_titles.add(folded)
        names = entry.get("devices")
        if not isinstance(names, list) or not names:
            raise TopologyError(f"分区 {title!r} 的 devices 必须是非空设备名列表。")
        members: list[Device] = []
        local_names: set[str] = set()
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise TopologyError(f"分区 {title!r} 含无效设备名。")
            device = topo.find(name.strip())
            if device.dev_id in local_names:
                raise TopologyError(f"设备 {device.name} 在分区 {title!r} 中出现了多次。")
            if device.dev_id in assigned:
                raise TopologyError(
                    f"设备 {device.name} 同时属于分区 {assigned[device.dev_id]!r} 和 {title!r}。"
                )
            local_names.add(device.dev_id)
            assigned[device.dev_id] = title
            members.append(device)
        default_color = DEFAULT_ZONE_COLORS[index % len(DEFAULT_ZONE_COLORS)]
        plans.append(
            _make_plan(index, title, members, entry.get("color", default_color), entry.get("columns"))
        )

    unassigned = [device for device in topo.devices if device.dev_id not in assigned]
    if include_unassigned and unassigned:
        title = "未分区"
        suffix = 2
        while title.casefold() in seen_titles:
            title = f"未分区 {suffix}"
            suffix += 1
        plans.append(_make_plan(len(plans), title, unassigned, "#F1F3F5", None))
        unassigned = []
    return plans, unassigned


def _place_panels(plans: list[ZonePlan], canvas_columns: int | None) -> None:
    column_count = (
        min(len(plans), math.ceil(math.sqrt(len(plans))))
        if canvas_columns is None or canvas_columns == 0
        else min(len(plans), _positive_int(canvas_columns, "zone_columns", 20))
    )
    row_count = math.ceil(len(plans) / column_count)
    column_widths = [0] * column_count
    row_heights = [0] * row_count
    for index, plan in enumerate(plans):
        row, column = divmod(index, column_count)
        column_widths[column] = max(column_widths[column], plan.width)
        row_heights[row] = max(row_heights[row], plan.height)

    x_offsets = [ZONE_ORIGIN_X]
    for width in column_widths[:-1]:
        x_offsets.append(x_offsets[-1] + width + ZONE_GAP_X)
    y_offsets = [ZONE_ORIGIN_Y]
    for height in row_heights[:-1]:
        y_offsets.append(y_offsets[-1] + height + ZONE_GAP_Y)

    for index, plan in enumerate(plans):
        row, column = divmod(index, column_count)
        plan.left = x_offsets[column]
        plan.top = y_offsets[row]


def _place_devices(plan: ZonePlan) -> None:
    content_width = plan.columns * plan.cell_width + (plan.columns - 1) * CELL_GAP_X
    start_x = plan.left + (plan.width - content_width) / 2
    start_y = plan.top + ZONE_HEADER_HEIGHT + ZONE_CONTENT_TOP
    for index, device in enumerate(plan.devices):
        row, column = divmod(index, plan.columns)
        cell_left = start_x + column * (plan.cell_width + CELL_GAP_X)
        device.cx = cell_left + (plan.cell_width - ICON_WIDTH) / 2
        device.cy = start_y + row * (CELL_HEIGHT + CELL_GAP_Y)
        device.edit_left = None
        device.edit_top = None


def _append_zone_xml(shapes: ET.Element, txttips: ET.Element, plan: ZonePlan) -> None:
    ET.SubElement(
        shapes,
        "shape",
        {
            "type": "1",
            "filloption": "1",
            "color": str(plan.colorref),
            "upleftcorner": f"{plan.left},{plan.top}",
            "width": str(plan.width),
            "height": str(plan.height),
            "startpt": "0,0",
            "endpt": "0,0",
            "borderStyle": "5",
            "borderWidth": "1",
        },
    )
    title_width = _display_width(plan.title, 14) + 12
    ET.SubElement(
        txttips,
        "txttip",
        {
            "left": str(plan.left + 14),
            "top": str(plan.top + 9),
            "right": str(plan.left + 14 + title_width),
            "bottom": str(plan.top + 34),
            "content": plan.title,
            "fontname": "Consolas",
            "fontstyle": "0",
            "editsize": "100",
            "txtcolor": "-16777216",
            "txtbkcolor": "-1",
            "charset": "1",
        },
    )


def _place_unassigned(devices: list[Device], after_y: int) -> list[list[str]]:
    if not devices:
        return []
    columns = min(6, max(1, math.ceil(math.sqrt(len(devices)))))
    cell_width = max(124, max(_display_width(device.name, 12) for device in devices) + 28)
    rows: list[list[str]] = []
    for index, device in enumerate(devices):
        row, column = divmod(index, columns)
        if row == len(rows):
            rows.append([])
        rows[row].append(device.name)
        cell_left = ZONE_ORIGIN_X + column * (cell_width + CELL_GAP_X)
        device.cx = cell_left + (cell_width - ICON_WIDTH) / 2
        device.cy = after_y + row * (CELL_HEIGHT + CELL_GAP_Y)
        device.edit_left = None
        device.edit_top = None
    return rows


def _assert_generated_geometry(plans: list[ZonePlan]) -> None:
    for index, plan in enumerate(plans):
        for other in plans[index + 1 :]:
            if plan.rect.overlaps(other.rect):
                raise TopologyError(f"分区 {plan.title!r} 与 {other.title!r} 的背景框发生重叠。")
        for device in plan.devices:
            if not plan.rect.contains(_device_rect(device), padding=8):
                raise TopologyError(f"设备 {device.name} 的图标或名称超出分区 {plan.title!r}。")


def apply_zones(
    topo: Topology,
    zones: list[dict],
    *,
    replace_annotations: bool = False,
    include_unassigned: bool = True,
    zone_columns: int | None = None,
) -> dict:
    """按分区重排设备，并生成 eNSP 原生背景矩形和标题。"""
    if not isinstance(replace_annotations, bool) or not isinstance(include_unassigned, bool):
        raise TopologyError("replace_annotations 和 include_unassigned 必须是布尔值。")
    plans, unassigned = _normalise_zones(topo, zones, include_unassigned)
    _place_panels(plans, zone_columns)

    shapes = _container(topo.root, "shapes")
    txttips = _container(topo.root, "txttips")
    if (list(shapes) or list(txttips)) and not replace_annotations:
        raise TopologyError(
            "拓扑已有图形或文字标注，无法判断哪些可以删除。"
            "若要用本次分区完整重建标注，请设置 replace_annotations=true。"
        )

    if replace_annotations:
        shapes[:] = []
        txttips[:] = []
    for plan in plans:
        _place_devices(plan)
        _append_zone_xml(shapes, txttips, plan)
    _assert_generated_geometry(plans)

    bottom = max((plan.top + plan.height for plan in plans), default=ZONE_ORIGIN_Y)
    unassigned_rows = _place_unassigned(unassigned, bottom + ZONE_GAP_Y)
    return {
        "mode": "zones",
        "zoneCount": len(plans),
        "zones": [
            {
                "title": plan.title,
                "devices": [device.name for device in plan.devices],
                "color": plan.color,
                "bounds": {
                    "left": plan.left,
                    "top": plan.top,
                    "right": plan.left + plan.width,
                    "bottom": plan.top + plan.height,
                },
                "columns": plan.columns,
            }
            for plan in plans
        ],
        "unassigned": [name for row in unassigned_rows for name in row],
        "annotationsReplaced": replace_annotations,
    }


def _parse_pair(value: str | None) -> tuple[float, float] | None:
    if value is None:
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def shape_rect(element: ET.Element) -> Rect | None:
    if element.tag != "shape" or element.get("type") != "1":
        return None
    corner = _parse_pair(element.get("upleftcorner"))
    if corner is None:
        return None
    try:
        width = float(element.get("width", ""))
        height = float(element.get("height", ""))
    except ValueError:
        return None
    if not all(math.isfinite(value) for value in (*corner, width, height)) or width <= 0 or height <= 0:
        return None
    return Rect(corner[0], corner[1], corner[0] + width, corner[1] + height)


def text_rect(element: ET.Element) -> Rect | None:
    if element.tag != "txttip":
        return None
    try:
        values = [float(element.get(key, "")) for key in ("left", "top", "right", "bottom")]
    except ValueError:
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    rect = Rect(*values)
    return rect if rect.width > 0 and rect.height > 0 else None


def summary(topo: Topology) -> dict:
    shapes = list(topo.root.iterfind("./shapes/shape"))
    tips = list(topo.root.iterfind("./txttips/txttip"))
    return {
        "shapeCount": len(shapes),
        "rectangleCount": sum(shape_rect(shape) is not None for shape in shapes),
        "textCount": len(tips),
        "texts": [tip.get("content", "") for tip in tips],
    }


def validation_issues(topo: Topology) -> tuple[list[str], list[str]]:
    """检查标注本身及其与设备的碰撞。"""
    errors: list[str] = []
    warnings: list[str] = []
    rectangles: list[tuple[int, Rect]] = []
    for index, shape in enumerate(topo.root.iterfind("./shapes/shape"), start=1):
        if shape.get("type") != "1":
            continue
        rect = shape_rect(shape)
        if rect is None:
            corner = _parse_pair(shape.get("upleftcorner"))
            try:
                width = float(shape.get("width", ""))
                height = float(shape.get("height", ""))
            except ValueError:
                width = height = math.nan
            if (
                corner is not None
                and all(math.isfinite(value) for value in (*corner, width, height))
                and width >= 0
                and height >= 0
                and (width == 0 or height == 0)
            ):
                warnings.append(f"矩形标注 {index} 的面积为零，eNSP 会把它当作不可见占位项")
            else:
                errors.append(f"矩形标注 {index} 的坐标或尺寸无效")
        else:
            rectangles.append((index, rect))

    texts: list[tuple[int, ET.Element, Rect]] = []
    for index, tip in enumerate(topo.root.iterfind("./txttips/txttip"), start=1):
        rect = text_rect(tip)
        if rect is None:
            errors.append(f"文字标注 {index} 的边界无效")
        else:
            texts.append((index, tip, rect))

    for pos, (a_index, a) in enumerate(rectangles):
        for b_index, b in rectangles[pos + 1 :]:
            if a.overlaps(b):
                warnings.append(f"矩形标注 {a_index} 与 {b_index} 发生重叠")

    for pos, (a_index, _a_tip, a) in enumerate(texts):
        for b_index, _b_tip, b in texts[pos + 1 :]:
            if a.overlaps(b):
                warnings.append(f"文字标注 {a_index} 与 {b_index} 发生重叠")

    for text_index, tip, rect in texts:
        for device in topo.devices:
            if rect.overlaps(_device_rect(device)):
                content = tip.get("content", "")
                warnings.append(
                    f"文字标注 {text_index}（{content or '无内容'}）与设备 {device.name} 重叠"
                )
    return errors, warnings


def device_rectangles(devices: Iterable[Device]) -> list[tuple[Device, Rect]]:
    return [(device, _device_rect(device)) for device in devices]
