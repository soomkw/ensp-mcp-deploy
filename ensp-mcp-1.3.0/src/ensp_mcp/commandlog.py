"""命令留痕：记下每条命令打到了哪台设备，并生成可交互的 HTML 可视化。

日志是 JSONL 追加写入，落在 ``%USERPROFILE%\\.ensp-mcp\\commands.jsonl``
（可用环境变量 ``ENSP_MCP_LOG_DIR`` 改目录），一条命令一行，进程崩了也读得回来。
"""

from __future__ import annotations

import html
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

MAX_LOG_BYTES = 8 * 1024 * 1024
OUTPUT_KEEP = 2000

CATEGORY_STYLE = {
    "AR": "#3b82f6",
    "LSW": "#22c55e",
    "FW": "#ef4444",
    "WLAN": "#a855f7",
    "CLIENT": "#94a3b8",
    "OTHER": "#f59e0b",
}
CATEGORY_LABEL = {
    "AR": "路由器",
    "LSW": "交换机",
    "FW": "防火墙",
    "WLAN": "无线",
    "CLIENT": "终端",
    "OTHER": "其它",
}

NODE_W = 146.0
NODE_H = 54.0
GAP_X = 46.0
GAP_Y = 40.0
PAD_X = 70.0
PAD_Y = 46.0


# ---------------------------------------------------------------------------
# 日志读写
# ---------------------------------------------------------------------------


def log_dir() -> Path:
    override = os.environ.get("ENSP_MCP_LOG_DIR", "").strip()
    base = Path(override) if override else Path.home() / ".ensp-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_path() -> Path:
    return log_dir() / "commands.jsonl"


def _clip(text: str, limit: int = OUTPUT_KEEP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长 {len(text)} 字符）"


def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            backup = path.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
    except OSError:
        pass


def record(
    *,
    device: str,
    port,
    tool: str,
    results: list[dict],
    topo_path: str = "",
    host: str = "127.0.0.1",
    batch: str = "",
) -> int:
    """把一批命令的执行结果写进日志，返回写入条数。"""
    if not results:
        return 0
    path = log_path()
    _rotate(path)
    now = datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")
    batch_id = batch or f"{int(now.timestamp() * 1000)}"
    topo_abs = os.path.abspath(topo_path) if topo_path else ""
    lines = []
    for item in results:
        command = str(item.get("command", "")).strip()
        if not command:
            continue
        error = item.get("error") or None
        lines.append(
            json.dumps(
                {
                    "ts": stamp,
                    "batch": batch_id,
                    "tool": tool,
                    "device": str(device),
                    "port": int(port) if str(port).isdigit() else port,
                    "host": host,
                    "topo": topo_abs,
                    "command": command,
                    "ok": not error,
                    "error": error,
                    "view": item.get("view") or "",
                    "output": _clip(str(item.get("output") or "")),
                },
                ensure_ascii=False,
            )
        )
    if not lines:
        return 0
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return len(lines)


def load(*, topo_path: str = "", device: str = "", limit: int = 2000) -> list[dict]:
    """按拓扑 / 设备过滤日志，只保留最后 limit 条。"""
    path = log_path()
    if not path.exists():
        return []
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("command"):
                entries.append(entry)
    if topo_path:
        want = os.path.normcase(os.path.abspath(topo_path))
        filtered = [
            entry
            for entry in entries
            if not entry.get("topo") or os.path.normcase(str(entry["topo"])) == want
        ]
        if filtered:
            entries = filtered
    if device:
        entries = [
            entry for entry in entries if str(entry.get("device", "")).lower() == device.lower()
        ]
    if limit and len(entries) > limit:
        entries = entries[-limit:]
    return entries


# ---------------------------------------------------------------------------
# 数据整理
# ---------------------------------------------------------------------------


def _topo_devices(topo_path: str) -> tuple[list[dict], list[dict]]:
    """从 .topo 读出设备坐标和链路；读不到就返回空，用日志里的设备名兜底。"""
    if not topo_path or not os.path.exists(topo_path):
        return [], []
    from .topo import Topology

    topo = Topology.load(topo_path)
    devices = []
    for device in topo.devices:
        devices.append(
            {
                "name": device.name,
                "model": device.model,
                "category": device.spec.category,
                "x": device.cx,
                "y": device.cy,
                "port": device.com_port or None,
            }
        )
    by_id = topo.by_id()
    names = {device.dev_id: device.name for device in topo.devices}
    links = []
    for link in topo.links:
        src = names.get(link.src_id)
        dst = names.get(link.dst_id)
        if not src or not dst:
            continue
        src_dev = by_id.get(link.src_id)
        dst_dev = by_id.get(link.dst_id)
        links.append(
            {
                "from": src,
                "to": dst,
                "lineType": link.line_type,
                "fromPort": _port_name(src_dev, link.src_index),
                "toPort": _port_name(dst_dev, link.dst_index),
            }
        )
    return devices, links


def _port_name(device, index) -> str:
    try:
        from .topo import port_of

        return port_of(device, index).short
    except Exception:  # pragma: no cover - 端口表异常时不影响可视化
        return str(index)


def _group(entries: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        groups.setdefault(str(entry.get("device", "?")), []).append(entry)
    return groups


def _place(devices: list[dict]) -> dict[str, tuple[float, float]]:
    """按 .topo 坐标摆位；坐标缺失或全叠在一起时退化成网格。"""
    positioned = [
        device
        for device in devices
        if isinstance(device.get("x"), (int, float)) and isinstance(device.get("y"), (int, float))
    ]
    floating = [device for device in devices if device not in positioned]
    positions: dict[str, tuple[float, float]] = {}

    if len(positioned) >= 2:
        xs = [float(device["x"]) for device in positioned]
        ys = [float(device["y"]) for device in positioned]
        if max(xs) - min(xs) >= NODE_W * 0.8 or max(ys) - min(ys) >= NODE_H * 0.8:
            for device in positioned:
                positions[device["name"]] = (float(device["x"]), float(device["y"]))
            if floating:
                base_y = max(ys) + NODE_H + 60
                for index, device in enumerate(floating):
                    positions[device["name"]] = (
                        min(xs) + (index % 4) * (NODE_W + GAP_X),
                        base_y + (index // 4) * (NODE_H + GAP_Y),
                    )
            return positions

    columns = max(1, math.ceil(math.sqrt(len(devices))))
    for index, device in enumerate(devices):
        positions[device["name"]] = (
            index % columns * (NODE_W + GAP_X),
            index // columns * (NODE_H + GAP_Y * 2.2),
        )
    return positions


def _clock(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return ts[-8:] if len(ts) >= 8 else ts


def _tool_label(tool: str) -> str:
    return {
        "device_push_config": "下发",
        "device_exec": "执行",
        "device_probe": "探测",
        "device_fetch_config": "读回",
    }.get(tool, tool.replace("device_", ""))


# ---------------------------------------------------------------------------
# HTML 片段
# ---------------------------------------------------------------------------


def _svg(devices, links, positions, groups) -> str:
    xs = [x for x, _ in positions.values()] or [0.0]
    ys = [y for _, y in positions.values()] or [0.0]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x + NODE_W + PAD_X * 2
    height = max_y - min_y + NODE_H + PAD_Y * 2
    offset_x = PAD_X + NODE_W / 2 - min_x
    offset_y = PAD_Y + NODE_H / 2 - min_y

    def point(name):
        x, y = positions[name]
        return x + offset_x, y + offset_y

    parts = [
        f'<svg class="canvas" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="拓扑与命令分布">',
        "<defs>"
        '<filter id="glow" x="-40%" y="-40%" width="180%" height="180%">'
        '<feGaussianBlur stdDeviation="4" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>'
        "</filter>"
        "</defs>",
    ]

    for link in links:
        if link["from"] not in positions or link["to"] not in positions:
            continue
        x1, y1 = point(link["from"])
        x2, y2 = point(link["to"])
        dash = ' stroke-dasharray="6 5"' if link.get("lineType") == "Serial" else ""
        parts.append(
            f'<line class="link" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"{dash} '
            f'><title>{html.escape(link["from"])} {html.escape(str(link.get("fromPort", "")))}'
            f' — {html.escape(link["to"])} {html.escape(str(link.get("toPort", "")))}</title></line>'
        )

    for device in devices:
        name = device["name"]
        x, y = point(name)
        items = groups.get(name, [])
        fails = sum(1 for item in items if not item.get("ok"))
        colour = CATEGORY_STYLE.get(device.get("category") or "OTHER", CATEGORY_STYLE["OTHER"])
        classes = "node"
        if items:
            classes += " has-cmd"
        if fails:
            classes += " has-fail"
        if not items:
            classes += " silent"

        badge = ""
        if items:
            badge_cls = "badge fail" if fails else "badge"
            badge = (
                f'<g class="{badge_cls}">'
                f'<circle cx="{NODE_W / 2 - 6:.1f}" cy="{-NODE_H / 2 + 6:.1f}" r="12"/>'
                f'<text x="{NODE_W / 2 - 6:.1f}" y="{-NODE_H / 2 + 10:.1f}">{len(items)}</text></g>'
            )
        else:
            badge = (
                f'<g class="badge idle">'
                f'<circle cx="{NODE_W / 2 - 6:.1f}" cy="{-NODE_H / 2 + 6:.1f}" r="12"/>'
                f'<text x="{NODE_W / 2 - 6:.1f}" y="{-NODE_H / 2 + 10:.1f}">0</text></g>'
            )

        name_text = name if len(name) <= 14 else name[:13] + "…"
        port_text = f'{device.get("model") or "?"}'
        if device.get("port"):
            port_text += f" · {device['port']}"
        parts.append(
            f'<g class="{classes}" data-dev="{html.escape(name)}" data-count="{len(items)}" '
            f'data-fail="{fails}" transform="translate({x:.1f},{y:.1f})">'
            f'<rect class="node-body" x="{-NODE_W / 2:.1f}" y="{-NODE_H / 2:.1f}" '
            f'width="{NODE_W:.1f}" height="{NODE_H:.1f}" rx="10"/>'
            f'<rect x="{-NODE_W / 2 + 1:.1f}" y="{-NODE_H / 2 + 1:.1f}" width="5" '
            f'height="{NODE_H - 2:.1f}" rx="2.5" fill="{colour}"/>'
            f'<text class="node-name" x="{-NODE_W / 2 + 16:.1f}" y="{4:.1f}">{html.escape(name_text)}</text>'
            f'<text class="node-meta" x="{-NODE_W / 2 + 16:.1f}" y="{20:.1f}">{html.escape(port_text)}</text>'
            f"{badge}</g>"
        )

    parts.append("</svg>")
    return "".join(parts)


def _panel(name: str, items: list[dict], device: dict | None, show_output: bool) -> str:
    fails = sum(1 for item in items if not item.get("ok"))
    category = (device or {}).get("category") or "OTHER"
    colour = CATEGORY_STYLE.get(category, CATEGORY_STYLE["OTHER"])
    model = (device or {}).get("model") or (items[0].get("model") if items else "") or "—"
    port = (device or {}).get("port") or (items[0].get("port") if items else "") or "—"

    rows = []
    for index, item in enumerate(items):
        state = "ok" if item.get("ok") else "bad"
        icon = "✓" if item.get("ok") else "✕"
        tool = _tool_label(str(item.get("tool", "")))
        hidden = ' class="extra"' if index >= 40 else ""
        rows.append(
            f'<tr class="cmd {state}{hidden}" data-state="{state}" data-row="{index}">'
            f'<td class="t">{_clock(str(item.get("ts", "")))}</td>'
            f'<td class="tl"><span class="chip">{html.escape(tool)}</span></td>'
            f'<td class="c"><code>{html.escape(str(item.get("command", "")))}</code></td>'
            f'<td class="s"><span class="mark {state}">{icon}</span></td></tr>'
        )
        if show_output:
            body = str(item.get("error") or item.get("output") or "")
            if body.strip():
                rows.append(
                    f'<tr class="out {"extra" if index >= 40 else ""}" data-parent="{index}">'
                    f'<td colspan="4"><pre>{html.escape(body[:OUTPUT_KEEP])}</pre></td></tr>'
                )

    more = (
        f'<button class="more" type="button">展开全部 {len(items)} 条</button>'
        if len(items) > 40
        else ""
    )
    fail_tag = f'<span class="tag fail">{fails} 失败</span>' if fails else ""
    return (
        f'<article class="panel" data-dev="{html.escape(name)}">'
        f'<header><span class="dot" style="background:{colour}"></span>'
        f'<h3>{html.escape(name)}</h3>'
        f'<span class="meta">{html.escape(str(model))} · console {html.escape(str(port))}</span>'
        f'<span class="tag">{len(items)} 条</span>{fail_tag}</header>'
        f'<table>{"".join(rows)}</table>{more}</article>'
    )


def _empty_panel(name: str, device: dict | None) -> str:
    colour = CATEGORY_STYLE.get((device or {}).get("category") or "OTHER", CATEGORY_STYLE["OTHER"])
    model = (device or {}).get("model") or "—"
    port = (device or {}).get("port") or "—"
    return (
        f'<article class="panel silent-panel" data-dev="{html.escape(name)}">'
        f'<header><span class="dot" style="background:{colour}"></span>'
        f'<h3>{html.escape(name)}</h3>'
        f'<span class="meta">{html.escape(str(model))} · console {html.escape(str(port))}</span>'
        f'<span class="tag">0 条</span></header>'
        f'<p class="hint">这台设备还没有命令记录。</p></article>'
    )


CSS = """
:root{
  --bg:#0b1017; --panel:#121a24; --panel2:#0e151d; --line:#22303f;
  --text:#e7eef7; --muted:#8ea0b5; --ok:#3fb950; --bad:#f85149; --warn:#d29922;
  --mono:"Cascadia Mono","JetBrains Mono",Consolas,"Courier New",monospace;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 15% -10%,#162438 0%,#0b1017 55%);
  color:var(--text);font:14px/1.55 "Microsoft YaHei","Segoe UI",system-ui,sans-serif;padding:28px 34px 60px}
h1{font-size:20px;margin:0 0 4px;letter-spacing:.5px}
h1 span{color:#5b9dff}
.sub{color:var(--muted);font-size:12.5px;margin-bottom:22px}
.sub code{font-family:var(--mono);color:#a8c2e0}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}
.stat{background:linear-gradient(180deg,#14202c,#101822);border:1px solid var(--line);
  border-radius:12px;padding:12px 18px;min-width:118px}
.stat b{display:block;font-size:22px;line-height:1.2;font-family:var(--mono)}
.stat small{color:var(--muted);font-size:11.5px;letter-spacing:.4px}
.stat.bad b{color:var(--bad)} .stat.ok b{color:var(--ok)}
.layout{display:grid;grid-template-columns:minmax(430px,46%) 1fr;gap:22px;align-items:start}
@media(max-width:1080px){.layout{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
.card h2{font-size:13px;margin:0 0 12px;color:var(--muted);font-weight:600;letter-spacing:1px}
.canvas{width:100%;height:auto;display:block;overflow:visible}
.link{stroke:#2c3d52;stroke-width:2}
.node{cursor:pointer}
.node-body{fill:#16212e;stroke:#2b3b4e;stroke-width:1.4}
.node.has-cmd .node-body{stroke:#3d5673}
.node.has-fail .node-body{stroke:#f85149;stroke-width:1.8}
.node.silent{opacity:.55}
.node:hover .node-body,.node.active .node-body{stroke:#5b9dff;filter:url(#glow)}
.node-name{fill:#e7eef7;font-size:13px;font-family:"Microsoft YaHei",sans-serif}
.node-meta{fill:#8ea0b5;font-size:10.5px;font-family:var(--mono)}
.badge circle{fill:#1d4ed8}
.badge.fail circle{fill:#b91c1c}
.badge.idle circle{fill:#334155}
.badge text{fill:#fff;font-size:11px;font-family:var(--mono);text-anchor:middle;font-weight:700}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;color:var(--muted);font-size:11.5px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px}
.panels{display:flex;flex-direction:column;gap:14px;max-height:76vh;overflow:auto;padding-right:4px}
.panel{background:var(--panel2);border:1px solid var(--line);border-radius:14px;overflow:hidden;flex:none}
.panel.active{border-color:#5b9dff;box-shadow:0 0 0 1px #5b9dff33,0 8px 26px #0008}
.panel header{display:flex;align-items:center;gap:9px;padding:11px 14px;border-bottom:1px solid var(--line);
  background:#131d28;position:sticky;top:0;z-index:2}
.panel h3{font-size:14px;margin:0;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.meta{color:var(--muted);font-size:11.5px;font-family:var(--mono)}
.tag{margin-left:auto;font-size:11px;color:#b9cbdf;background:#1b2836;border:1px solid var(--line);
  border-radius:999px;padding:1px 9px}
.tag.fail{color:#ffb4ae;background:#3a1518;border-color:#7a2a2a;margin-left:6px}
table{width:100%;border-collapse:collapse}
tr.cmd{border-top:1px solid #1a2531}
tr.cmd:first-child{border-top:none}
tr.cmd td{padding:6px 8px;vertical-align:top;font-size:12.5px}
td.t{color:#7d8fa4;font-family:var(--mono);font-size:11.5px;white-space:nowrap;width:64px}
td.tl{width:52px}
.chip{font-size:10.5px;padding:1px 7px;border-radius:6px;background:#1c2b3b;color:#9fc0e4;
  border:1px solid #2a3d52;white-space:nowrap}
td.c code{font-family:var(--mono);color:#dbe7f5;font-size:12.5px;word-break:break-all}
td.s{width:26px;text-align:center}
.mark{font-weight:700}
.mark.ok{color:var(--ok)} .mark.bad{color:var(--bad)}
tr.cmd.bad td.c code{color:#ffb4ae}
tr.cmd:hover{background:#16212d}
tr.out td{background:#0a1119;padding:0;border-top:1px dashed #1b2735}
tr.out pre{margin:0;padding:8px 14px 10px 78px;font-family:var(--mono);font-size:11.5px;
  color:#8fa3ba;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-all}
tr.out.hide,tr.cmd.extra.hide{display:none}
tr.cmd.extra,tr.out.extra{display:none}
tr.cmd.extra.show,tr.out.extra.show{display:table-row}
.more{margin:0;width:100%;background:#152132;color:#9fc0e4;border:none;border-top:1px solid var(--line);
  padding:8px;font-size:12px;cursor:pointer;font-family:inherit}
.more:hover{background:#1a2a3d}
.hint{color:var(--muted);font-size:12.5px;margin:0;padding:14px}
.empty{color:var(--muted);padding:26px;text-align:center;border:1px dashed var(--line);border-radius:14px}
footer{color:#5d6c7e;font-size:11.5px;margin-top:26px}
"""

JS = """
(function(){
  function q(sel,root){return Array.prototype.slice.call((root||document).querySelectorAll(sel));}
  var nodes=q('.node'), panels=q('.panel');
  function activate(name){
    nodes.forEach(function(n){n.classList.toggle('active', n.dataset.dev===name);});
    panels.forEach(function(p){p.classList.toggle('active', p.dataset.dev===name);});
    var target=panels.filter(function(p){return p.dataset.dev===name;})[0];
    if(target){target.scrollIntoView({behavior:'smooth',block:'nearest'});}
  }
  nodes.forEach(function(n){n.addEventListener('click',function(){activate(n.dataset.dev);});});
  q('.more').forEach(function(btn){
    btn.addEventListener('click',function(){
      var panel=btn.closest('.panel');
      q('.extra',panel).forEach(function(row){row.classList.add('show');});
      btn.remove();
    });
  });
  q('tr.cmd').forEach(function(row){
    row.addEventListener('click',function(){
      var next=row.nextElementSibling;
      if(next&&next.classList.contains('out')){next.classList.toggle('hide');}
    });
  });
})();
"""


def build_report(
    *,
    topo_path: str = "",
    entries: list[dict] | None = None,
    out_path: str = "",
    show_output: bool = True,
) -> dict:
    """生成可视化报告，返回文件路径和统计信息。"""
    entries = list(entries or [])
    devices, links = _topo_devices(topo_path)
    groups = _group(entries)

    known = {device["name"] for device in devices}
    for name, items in groups.items():
        if name in known:
            continue
        first = items[0]
        devices.append(
            {
                "name": name,
                "model": first.get("model") or "",
                "category": "OTHER",
                "x": None,
                "y": None,
                "port": first.get("port"),
            }
        )
        known.add(name)

    positions = _place(devices)
    coloured = {device["name"]: device for device in devices}

    total = len(entries)
    failures = sum(1 for entry in entries if not entry.get("ok"))
    touched = sum(1 for name in groups if groups[name])
    first_ts = _clock(str(entries[0].get("ts", ""))) if entries else "—"
    last_ts = _clock(str(entries[-1].get("ts", ""))) if entries else "—"

    topo_label = os.path.basename(topo_path) if topo_path else "（未指定拓扑）"

    if topo_path and os.path.exists(topo_path):
        svg = _svg(devices, links, positions, groups)
    elif devices:
        svg = _svg(devices, [], positions, groups)
    else:
        svg = ""

    order = sorted(
        devices,
        key=lambda device: (
            0 if groups.get(device["name"]) else 1,
            -len(groups.get(device["name"], [])),
            device["name"],
        ),
    )
    panels = []
    for device in order:
        name = device["name"]
        if groups.get(name):
            panels.append(_panel(name, groups[name], coloured.get(name), show_output))
        else:
            panels.append(_empty_panel(name, coloured.get(name)))

    legend = "".join(
        f'<span><i style="background:{colour}"></i>{label}</span>'
        for key, label in CATEGORY_LABEL.items()
        if any(device.get("category") == key for device in devices)
        for colour in [CATEGORY_STYLE[key]]
    )

    if not entries:
        panels_html = (
            '<div class="empty">还没有命令记录。<br>'
            "先用 device_exec / device_push_config / device_probe 操作设备，"
            "再回来看这份报告。</div>"
        )
    else:
        panels_html = '<div class="panels">' + "".join(panels) + "</div>"

    body = (
        f'<h1>eNSP <span>命令留痕</span>可视化</h1>'
        f'<div class="sub"><code>{html.escape(os.path.abspath(topo_path) if topo_path else "—")}</code>'
        f" · 生成于 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}</div>"
        f'<div class="stats">'
        f'<div class="stat"><b>{len(devices)}</b><small>拓扑设备</small></div>'
        f'<div class="stat ok"><b>{touched}</b><small>已下发设备</small></div>'
        f'<div class="stat"><b>{total}</b><small>命令总数</small></div>'
        f'<div class="stat bad"><b>{failures}</b><small>失败命令</small></div>'
        f"</div>"
        f'<div class="layout"><div class="card"><h2>拓扑与命令分布（点设备看明细）</h2>{svg}'
        f'<div class="legend">{legend}<span><i style="background:#b91c1c"></i>角标=命令数/失败</span></div></div>'
        f"<div>{panels_html}</div></div>"
        f"<footer>日志文件：{html.escape(str(log_path()))} · 时间范围 {first_ts} – {last_ts}</footer>"
    )

    document = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>命令留痕 · {html.escape(topo_label)}</title>"
        f"<style>{CSS}</style></head><body>{body}<script>{JS}</script></body></html>"
    )

    if not out_path:
        if topo_path:
            out_path = os.path.splitext(topo_path)[0] + "-commands.html"
        else:
            out_path = str(log_dir() / "command-trace.html")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(document)

    return {
        "report": os.path.abspath(out_path),
        "deviceCount": len(devices),
        "touchedDevices": touched,
        "commandCount": total,
        "failedCommands": failures,
        "byDevice": [
            {
                "device": name,
                "commands": len(groups[name]),
                "failed": sum(1 for item in groups[name] if not item.get("ok")),
            }
            for name in sorted(groups, key=lambda n: -len(groups[n]))
        ],
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
