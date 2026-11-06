"""把拓扑导成写文档用得上的格式：Mermaid、drawio、CSV。"""

from __future__ import annotations

import csv
import io
import re
from xml.sax.saxutils import escape

from .topo import Topology, port_of

FORMATS = ("mermaid", "drawio", "csv")

_MERMAID_SHAPES = {
    "AR": ("([", "])"),
    "LSW": ("[", "]"),
    "FW": ("{{", "}}"),
    "WLAN": ("(", ")"),
    "CLIENT": ("[/", "/]"),
    "OTHER": ("((", "))"),
}

_DRAWIO_STYLES = {
    "AR": "ellipse;fillColor=#2f6fb5;fontColor=#ffffff;strokeColor=#1f2937;",
    "LSW": "rounded=0;fillColor=#2e9e57;fontColor=#ffffff;strokeColor=#1f2937;",
    "FW": "rounded=0;fillColor=#c9483c;fontColor=#ffffff;strokeColor=#1f2937;",
    "WLAN": "rounded=1;fillColor=#7a4fb5;fontColor=#ffffff;strokeColor=#1f2937;",
    "CLIENT": "rounded=0;fillColor=#6b7280;fontColor=#ffffff;strokeColor=#1f2937;",
    "OTHER": "ellipse;fillColor=#d08a2c;fontColor=#ffffff;strokeColor=#1f2937;",
}


def _safe_id(name: str, taken: dict[str, str]) -> str:
    """Mermaid 的节点 id 不能带连字符之类的字符。"""
    if name in taken:
        return taken[name]
    ident = "n_" + re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit():
        ident = f"n_{ident}"
    base = ident
    suffix = 1
    while ident in taken.values():
        ident = f"{base}_{suffix}"
        suffix += 1
    taken[name] = ident
    return ident


def to_mermaid(topo: Topology, direction: str = "TD") -> str:
    by_id = topo.by_id()
    taken: dict[str, str] = {}
    lines = [f"graph {direction}"]

    for device in topo.devices:
        ident = _safe_id(device.name, taken)
        open_tag, close_tag = _MERMAID_SHAPES.get(
            device.spec.category, _MERMAID_SHAPES["OTHER"]
        )
        label = _mermaid_text(device.name) + "<br/>" + _mermaid_text(device.model)
        lines.append(f'    {ident}{open_tag}"{label}"{close_tag}')

    for link in topo.links:
        src = by_id.get(link.src_id)
        dst = by_id.get(link.dst_id)
        if src is None or dst is None:
            continue
        label = f"{_short(src, link.src_index)} — {_short(dst, link.dst_index)}"
        lines.append(
            f"    {_safe_id(src.name, taken)} ---|{label}| {_safe_id(dst.name, taken)}"
        )
    return "\n".join(lines)


def to_drawio(topo: Topology) -> str:
    by_id = topo.by_id()
    cells: list[str] = []
    ids: dict[str, str] = {}

    for index, device in enumerate(topo.devices, start=2):
        cell_id = f"d{index}"
        ids[device.dev_id] = cell_id
        style = _DRAWIO_STYLES.get(device.spec.category, _DRAWIO_STYLES["OTHER"])
        label = escape(f"{device.name}\n{device.model}", {'"': '&quot;', '\n': '&#10;'})
        cells.append(
            f'        <mxCell id="{cell_id}" value="{label}" style="{style}" '
            f'vertex="1" parent="1">\n'
            f'          <mxGeometry x="{device.cx:.0f}" y="{device.cy:.0f}" '
            f'width="80" height="50" as="geometry" />\n'
            f"        </mxCell>"
        )

    for index, link in enumerate(topo.links, start=1):
        src = by_id.get(link.src_id)
        dst = by_id.get(link.dst_id)
        if src is None or dst is None:
            continue
        label = escape(f"{_short(src, link.src_index)} — {_short(dst, link.dst_index)}")
        cells.append(
            f'        <mxCell id="e{index}" value="{label}" '
            f'style="endArrow=none;html=1;fontSize=9;" edge="1" parent="1" '
            f'source="{ids[src.dev_id]}" target="{ids[dst.dev_id]}">\n'
            f'          <mxGeometry relative="1" as="geometry" />\n'
            f"        </mxCell>"
        )

    body = "\n".join(cells)
    return (
        '<mxfile host="ensp-mcp">\n'
        '  <diagram name="topology">\n'
        "    <mxGraphModel dx="
        '"1200" dy="800" grid="1" gridSize="10" page="1" '
        'pageWidth="1600" pageHeight="1200">\n'
        "      <root>\n"
        '        <mxCell id="0" />\n'
        '        <mxCell id="1" parent="0" />\n'
        f"{body}\n"
        "      </root>\n"
        "    </mxGraphModel>\n"
        "  </diagram>\n"
        "</mxfile>\n"
    )


def to_csv(topo: Topology) -> dict[str, str]:
    by_id = topo.by_id()

    devices_buf = io.StringIO()
    writer = csv.writer(devices_buf, lineterminator="\n")
    writer.writerow(["名称", "型号", "类别", "console端口", "MAC", "接口总数", "已用接口", "X", "Y"])
    for device in topo.devices:
        spec = device.spec
        writer.writerow(
            [
                device.name,
                device.model,
                spec.category,
                device.com_port or "",
                device.system_mac,
                spec.port_count,
                len(topo.used_ports(device.dev_id)),
                f"{device.cx:.0f}",
                f"{device.cy:.0f}",
            ]
        )

    links_buf = io.StringIO()
    writer = csv.writer(links_buf, lineterminator="\n")
    writer.writerow(["序号", "源设备", "源接口", "目标设备", "目标接口", "线型"])
    for index, link in enumerate(topo.links, start=1):
        src = by_id.get(link.src_id)
        dst = by_id.get(link.dst_id)
        if src is None or dst is None:
            continue
        writer.writerow(
            [
                f"L{index:02d}",
                src.name,
                _full(src, link.src_index),
                dst.name,
                _full(dst, link.dst_index),
                link.line_type,
            ]
        )

    return {"devices": devices_buf.getvalue(), "links": links_buf.getvalue()}


def _short(device, index: int) -> str:
    return port_of(device, index).short


def _full(device, index: int) -> str:
    return port_of(device, index).name


def _mermaid_text(text: str) -> str:
    return "".join(f"#{ord(c)};" if c in '#\"&<>[]{}|\\\n\r' else c for c in text)
