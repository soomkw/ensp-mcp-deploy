"""把拓扑画成 PNG，让 agent 不开 eNSP 也能看见自己搭出来的东西。"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

from .topo import Topology, port_of

MARGIN = 70
NODE_RADIUS = 22
MIN_SIZE = (900, 600)

# 按设备类别区分颜色和形状
STYLE = {
    "AR": ("#2f6fb5", "ellipse", "路由器"),
    "LSW": ("#2e9e57", "rect", "交换机"),
    "FW": ("#c9483c", "rect", "防火墙"),
    "WLAN": ("#7a4fb5", "round", "无线"),
    "CLIENT": ("#6b7280", "rect", "终端"),
    "OTHER": ("#d08a2c", "ellipse", "其它"),
}

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\segoeui.ttf",
)


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _bounds(topo: Topology) -> tuple[float, float, float, float]:
    xs = [d.cx for d in topo.devices] or [0.0]
    ys = [d.cy for d in topo.devices] or [0.0]
    return min(xs), min(ys), max(xs), max(ys)


def render(topo: Topology, out_path: str, scale: float = 1.0, show_ports: bool = True) -> dict:
    """画出拓扑并存成 PNG，返回图片尺寸等信息。"""
    if not topo.devices:
        raise ValueError("拓扑里还没有设备，没什么可画的。")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("scale 必须是大于零的有限数值。")

    min_x, min_y, max_x, max_y = _bounds(topo)
    width = max(int((max_x - min_x) * scale) + MARGIN * 2, MIN_SIZE[0])
    height = max(int((max_y - min_y) * scale) + MARGIN * 2 + 40, MIN_SIZE[1])
    if width * height > 40_000_000:
        raise ValueError("图片超过 4000 万像素，请减小 scale 或调整布局。")

    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)
    name_font = _font(13)
    port_font = _font(10)
    title_font = _font(16)

    def point(device) -> tuple[float, float]:
        return (
            (device.cx - min_x) * scale + MARGIN,
            (device.cy - min_y) * scale + MARGIN + 30,
        )

    by_id = topo.by_id()

    # 先画线，再贴接口名，最后画设备图标和名字，保证名字在最上层
    for link in topo.links:
        src = by_id.get(link.src_id)
        dst = by_id.get(link.dst_id)
        if src is None or dst is None:
            continue
        x1, y1 = point(src)
        x2, y2 = point(dst)
        colour = "#94a3b8" if link.line_type == "Copper" else "#e08a2c"
        draw.line((x1, y1, x2, y2), fill=colour, width=2)

    if show_ports:
        # 同一台设备上引出的多条线，标签沿线错开摆，减少互相压盖
        fanout: dict[str, int] = {}
        for link in topo.links:
            src = by_id.get(link.src_id)
            dst = by_id.get(link.dst_id)
            if src is None or dst is None:
                continue
            x1, y1 = point(src)
            x2, y2 = point(dst)
            for near, far, device, index in (
                ((x1, y1), (x2, y2), src, link.src_index),
                ((x2, y2), (x1, y1), dst, link.dst_index),
            ):
                rank = fanout.get(device.dev_id, 0)
                fanout[device.dev_id] = rank + 1
                _port_label(
                    draw, port_font, near, far, _short(device, index), (rank % 2) * 15
                )

    for device in topo.devices:
        x, y = point(device)
        colour, shape, _label = STYLE.get(device.spec.category, STYLE["OTHER"])
        box = (x - NODE_RADIUS, y - NODE_RADIUS, x + NODE_RADIUS, y + NODE_RADIUS)
        if shape == "ellipse":
            draw.ellipse(box, fill=colour, outline="#1f2937", width=2)
        elif shape == "round":
            draw.rounded_rectangle(box, radius=8, fill=colour, outline="#1f2937", width=2)
        else:
            draw.rectangle(box, fill=colour, outline="#1f2937", width=2)

        draw.text((x, y), device.model[:7], fill="#ffffff", font=port_font, anchor="mm")

        label_y = y + NODE_RADIUS + 3
        box = draw.textbbox((x, label_y), device.name, font=name_font, anchor="ma")
        draw.rectangle((box[0] - 3, box[1] - 1, box[2] + 3, box[3] + 1), fill="#ffffff")
        draw.text((x, label_y), device.name, fill="#111827", font=name_font, anchor="ma")

    title = f"{len(topo.devices)} 台设备 · {len(topo.links)} 条链路"
    draw.text((MARGIN, 18), title, fill="#111827", font=title_font, anchor="lm")
    _legend(draw, width, port_font, topo)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    image.save(out_path)
    return {"path": os.path.abspath(out_path), "width": width, "height": height}


def _short(device, index: int) -> str:
    return port_of(device, index).short


def _port_label(draw, font, near, far, text: str, offset: float = 0.0) -> None:
    """把接口名贴在靠近该设备的那一端，位置按固定像素距离算。

    用固定距离而不是链路长度百分比，短链路上标签才不会压到设备名。
    """
    x1, y1 = near
    x2, y2 = far
    length = max(math.hypot(x2 - x1, y2 - y1), 1.0)
    distance = min(NODE_RADIUS + 26 + offset, length * 0.45)
    tx = x1 + (x2 - x1) / length * distance
    ty = y1 + (y2 - y1) / length * distance
    box = draw.textbbox((tx, ty), text, font=font, anchor="mm")
    draw.rectangle((box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill="#ffffff")
    draw.text((tx, ty), text, fill="#475569", font=font, anchor="mm")


def _legend(draw, width: int, font, topo: Topology) -> None:
    present = []
    for device in topo.devices:
        category = device.spec.category
        if category not in present:
            present.append(category)

    x = width - 20
    y = 18
    for category in reversed(present):
        colour, _shape, label = STYLE.get(category, STYLE["OTHER"])
        text_width = draw.textlength(label, font=font)
        draw.rectangle((x - text_width - 18, y - 6, x - text_width - 6, y + 6), fill=colour)
        draw.text((x, y), label, fill="#374151", font=font, anchor="rm")
        x -= text_width + 32
