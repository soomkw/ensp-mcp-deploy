"""eNSP 一站式 MCP：搭拓扑 + 配设备。

拓扑侧全是离线的文件操作，不需要开 eNSP；设备侧要求 eNSP 已启动
且目标设备已开机（图标变绿）。
"""

from __future__ import annotations

import json
import os
import asyncio
import functools
import ipaddress
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import (
    annotations,
    catalog,
    commandlog,
    console,
    doctor,
    export,
    layout,
    render,
    validate,
    verify,
    xmlio,
)
from .topo import Topology, TopologyError

pool = console.ConsolePool()


@asynccontextmanager
async def _lifespan(app):
    try:
        yield {}
    finally:
        await pool.close_all()


mcp = FastMCP("eNSP MCP", lifespan=_lifespan)
_file_lock = threading.RLock()


def _serialised(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with _file_lock:
            return function(*args, **kwargs)
    return wrapped

# topo_* 工具用过的最后一个拓扑，供 device_* 按设备名找 console 端口时兜底
_last_topo: str | None = None


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _ok(**payload: Any) -> str:
    return _dump({"ok": True, **payload})


def _err(message: str, **extra: Any) -> str:
    return _dump({"ok": False, "error": message, **extra})


def _load(path: str) -> Topology:
    global _last_topo
    if not os.path.exists(path):
        raise TopologyError(f"找不到文件 {path}。先用 topo_create 建一个，或检查路径。")
    topo = Topology.load(path)
    _last_topo = os.path.abspath(path)
    return topo


def _save(topo: Topology, path: str) -> None:
    global _last_topo
    topo.save(path)
    _last_topo = os.path.abspath(path)


# ---------------------------------------------------------------------------
# 设备库
# ---------------------------------------------------------------------------


@mcp.tool()
@_serialised
def list_models(category: str = "", model: str = "", topo_path: str = "") -> str:
    """列出 eNSP 支持的设备型号，以及每种型号的接口清单。

    搭拓扑前先调这个，就知道有哪些型号可选、每个型号有几个口、口叫什么名字。
    连线时 fromPort / toPort 填的就是这里的接口名。

    Args:
        category: 只看某一类，可选 AR（路由器）、LSW（交换机）、WLAN（无线）、
            FW（防火墙）、CLIENT（终端）、OTHER（其它）。留空看全部。
        model: 只看某一个型号，例如 "S5700"。填了会连完整接口列表一起返回。
        topo_path: 可选。给一份现成 .topo，会顺带核对内置型号表和这份文件
            里实际写出的接口布局是否一致，用于确认 eNSP 版本差异。
    """
    try:
        if model:
            spec = catalog.get_model(model)
            payload = spec.describe()
            payload["advisory"] = catalog.advisory(spec.name)
            if spec.settings_kind:
                payload["settingsFields"] = list(
                    catalog.SETTINGS_FIELDS.get(spec.settings_kind, ())
                )
            result = {"model": payload}
            if topo_path:
                learned = catalog.learn_models_from_topo(topo_path)
                selected = {k: v for k, v in learned.items() if k.lower() == model.lower()}
                result["catalogCheck"] = {"modelsInFile": sorted(selected), "differences": catalog.diff_against_builtin(selected)}
            return _ok(**result)

        models = []
        for spec in catalog.sort_models():
            if category and spec.category.upper() != category.upper():
                continue
            models.append(
                {
                    "model": spec.name,
                    "category": spec.category,
                    "categoryLabel": catalog.CATEGORY_LABELS.get(spec.category, ""),
                    "portCount": spec.port_count,
                    "portsPreview": [p.name for p in spec.ports[:3]],
                    "hasConsole": spec.has_console,
                    "note": spec.note,
                    "advisory": catalog.advisory(spec.name),
                }
            )

        defaults = {
            key: value
            for key, value in catalog.CATEGORY_DEFAULTS.items()
            if not category or key.upper() == category.upper()
        }
        result: dict[str, Any] = {
            "count": len(models),
            "models": models,
            "categoryDefaults": defaults,
            "selectionPolicy": "显式 model 始终按指定型号创建；没有硬性型号要求时采用 categoryDefaults。",
        }
        if topo_path:
            learned = catalog.learn_models_from_topo(topo_path)
            result["catalogCheck"] = {
                "modelsInFile": sorted(learned),
                "differences": catalog.diff_against_builtin(learned) or ["无差异"],
            }
        return _ok(**result)
    except (KeyError, ValueError, OSError) as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# 拓扑
# ---------------------------------------------------------------------------


@mcp.tool()
@_serialised
def topo_create(path: str, overwrite: bool = False) -> str:
    """新建一个空的 .topo 文件。

    Args:
        path: 要写到哪，例如 "D:/work/campus.topo"。
        overwrite: 目标已存在时是否覆盖，默认不覆盖。
    """
    try:
        if os.path.exists(path) and not overwrite:
            return _err(f"{path} 已经存在。要覆盖就把 overwrite 设成 true。")
        topo = Topology()
        _save(topo, path)
        return _ok(path=os.path.abspath(path), deviceCount=0, linkCount=0)
    except (OSError, ValueError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_inspect(path: str) -> str:
    """读一份 .topo，返回设备清单、链路清单和每台设备的空闲接口。

    每台设备会带上 consolePort，那就是 device_* 工具要用的端口号。
    """
    try:
        topo = _load(path)
        model_advisories = []
        for model_name in dict.fromkeys(device.model for device in topo.devices):
            try:
                policy = catalog.advisory(model_name)
            except (KeyError, ValueError):
                continue
            if policy["selectionTier"] == "conditional" or policy["runtimePrerequisites"]:
                model_advisories.append(policy)
        return _ok(
            path=os.path.abspath(path),
            **topo.summary(),
            annotations=annotations.summary(topo),
            modelAdvisories=model_advisories,
        )
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_add_devices(path: str, devices: list[dict]) -> str:
    """往拓扑里批量加设备。

    Args:
        path: .topo 文件路径。
        devices: 设备列表，每项支持这些字段：

            - model（必填）：型号，见 list_models。
            - name：设备名，省略则按 eNSP 习惯自动编号（S5700 叫 LSW1、
              AR2220 叫 AR1，重名自动往后排）。
            - count：一次加几台，默认 1；配合省略 name 可以批量生成。
            - x / y：画布坐标，省略则先堆在原点，之后跑 topo_layout 自动摆开。
            - settings：终端类设备的初始参数，PC/Server 可填
              {"ip": "...", "mask": "...", "gateway": "..."}，
              AP 可填 {"mac": "...", "sn": "..."}。具体字段见
              list_models 返回的 settingsFields。

        举例：[{"model": "S5700", "name": "Core", "count": 2},
              {"model": "PC", "settings": {"ip": "192.168.1.10"}}]

    console 端口会自动从 2000 起顺序分配，不会和已有设备冲突。
    """
    try:
        topo = _load(path)
        added = []
        advisories = []
        if not devices:
            return _err("devices 不能为空。")
        for entry in devices:
            if not isinstance(entry, dict) or not entry.get("model"):
                return _err(f"每一项都要有 model 字段，收到 {entry!r}")
            count = entry.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
                return _err("count 必须是 1–1000 的整数。")
            if len(topo.devices) + count > 1000:
                return _err("单个拓扑最多支持 1000 台设备。")
            base_name = entry.get("name")
            if base_name is not None and (not isinstance(base_name, str) or not base_name.strip()):
                return _err("name 必须是非空文本。")
            if entry.get("settings") is not None and not isinstance(entry["settings"], dict):
                return _err("settings 必须是对象。")
            for n in range(count):
                name = base_name
                if name and count > 1:
                    name = topo.unique_name(f"{base_name}{n + 1}")
                elif name and topo.has(name):
                    return _err(f"设备名 {name!r} 已存在，换个名字或省略 name 让它自动编号。")
                device = topo.add_device(
                    model_name=entry["model"],
                    name=name,
                    cx=entry.get("x"),
                    cy=entry.get("y"),
                    settings=entry.get("settings"),
                )
                added.append(
                    {
                        "name": device.name,
                        "model": device.model,
                        "consolePort": device.com_port or None,
                        "ports": device.spec.port_count,
                    }
                )
                policy = catalog.advisory(device.model)
                if (policy["selectionTier"] == "conditional" or policy["runtimePrerequisites"]) and not any(
                    item["model"] == policy["model"] for item in advisories
                ):
                    advisories.append(policy)
        _save(topo, path)
        return _ok(added=added, deviceCount=len(topo.devices), advisories=advisories)
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_connect(path: str, links: list[dict]) -> str:
    """批量连线。

    Args:
        path: .topo 文件路径。
        links: 链路列表，每项支持：

            - from / to（必填）：两端设备名。
            - fromPort / toPort：接口名（"GE0/0/1"、"GigabitEthernet0/0/1"
              都认）或接口序号。省略则自动挑该设备第一个空闲口。
            - lineType：线型，默认按接口类型自动判断（串口给 Serial，
              其余给 Copper）。也可显式写 Copper / Fiber / Serial。

        举例：[{"from": "Core1", "to": "Core2", "fromPort": "GE0/0/1",
               "toPort": "GE0/0/1"},
              {"from": "Core1", "to": "PC1"}]

    一个接口只能连一条线，重复占用会直接报错并指出是哪个口。
    """
    try:
        topo = _load(path)
        made = []
        for entry in links:
            if not isinstance(entry, dict) or not entry.get("from") or not entry.get("to"):
                return _err(f"每一项都要有 from 和 to，收到 {entry!r}")
            link, src_port, dst_port = topo.connect(
                src_name=entry["from"],
                dst_name=entry["to"],
                src_port=entry.get("fromPort"),
                dst_port=entry.get("toPort"),
                line_type=entry.get("lineType"),
            )
            made.append(
                {
                    "from": entry["from"],
                    "fromPort": src_port.name,
                    "to": entry["to"],
                    "toPort": dst_port.name,
                    "lineType": link.line_type,
                }
            )
        _save(topo, path)
        return _ok(connected=made, linkCount=len(topo.links))
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_remove(path: str, devices: list[str] | None = None, links: list[dict] | None = None) -> str:
    """删设备或删连线。

    Args:
        path: .topo 文件路径。
        devices: 要删的设备名列表，删设备会连带删掉它身上所有的线。
        links: 要断开的链路，每项 {"from": "A", "to": "B"}。
    """
    try:
        topo = _load(path)
        removed_devices = []
        removed_links = 0
        for name in devices or []:
            removed_devices.append(topo.remove_device(name).name)
        for entry in links or []:
            removed_links += topo.disconnect(entry["from"], entry["to"])
        _save(topo, path)
        return _ok(
            removedDevices=removed_devices,
            removedLinks=removed_links,
            deviceCount=len(topo.devices),
            linkCount=len(topo.links),
        )
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_layout(
    path: str,
    mode: str = "layered",
    groups: list[list[str]] | None = None,
    root: str = "",
    columns: int = 0,
    zones: list[dict] | None = None,
    replace_annotations: bool = False,
    include_unassigned: bool = True,
    zone_columns: int = 0,
    allow_annotation_desync: bool = False,
) -> str:
    """自动排布设备坐标，不用手工填 x/y。

    Args:
        path: .topo 文件路径。
        mode: 布局方式。

            - layered（默认）：按连线关系自动分层，出口在上、终端在下，
              最贴近常见的园区网拓扑图。
            - tree：从 root 出发按层级展开。
            - grid：网格铺开，适合还没连线的时候。
            - ring：环形，适合环网。
        groups: 只在 layered 下有效。手工指定每一层放哪些设备，
            例如 [["Internet"], ["AR1"], ["Core1","Core2"]]。
            没列进去的设备会自动补到最后一层。
        root: 只在 tree 下有效，指定树根设备名。省略则挑连线最多的那台。
        columns: 只在 grid 下有效，每行几台。省略则自动取平方根。
        zones: 可选的分区列表。每项包含 title（或 name）、devices，可选
            color（#RRGGBB）和 columns。给了 zones 后会使用分区布局，
            mode/groups/root/columns 不再参与排布。
        replace_annotations: 拓扑已有背景图形或文字时，是否明确清空并按 zones
            重建。默认拒绝覆盖，因为 eNSP 标注没有稳定 ID，无法可靠区分手工项。
        include_unassigned: zones 未包含的设备是否自动放入“未分区”背景框，默认是。
        zone_columns: 整体画布每行放几个分区；0 表示自动。
        allow_annotation_desync: 普通布局遇到已有标注时是否仍移动设备。默认否，
            避免背景框和文字留在旧位置；仅在确认标注无需同步时才设为 true。
    """
    try:
        topo = _load(path)
        if zones is not None:
            result = annotations.apply_zones(
                topo,
                zones,
                replace_annotations=replace_annotations,
                include_unassigned=include_unassigned,
                zone_columns=zone_columns or None,
            )
        else:
            has_annotations = bool(
                list(topo.root.iterfind("./shapes/*"))
                or list(topo.root.iterfind("./txttips/*"))
            )
            if has_annotations and not allow_annotation_desync:
                return _err(
                    "拓扑已有背景图形或文字；普通布局只移动设备，会导致标注错位。"
                    "请使用 zones 重建分区，或确认这些标注无需同步后设置 "
                    "allow_annotation_desync=true。"
                )
            result = layout.apply(
                topo,
                mode=mode,
                groups=groups,
                root=root or None,
                columns=columns or None,
            )
        reset_count = layout.reset_link_labels(topo)
        result["linkLabelsReset"] = reset_count
        _save(topo, path)
        return _ok(**result)
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_validate(path: str) -> str:
    """检查拓扑有没有问题，建议在交给 eNSP 打开之前跑一次。

    errors 会让 eNSP 打开异常或链路失效，必须改；warnings 只是提醒。
    查的内容包括：设备重名、接口序号越界、一个口接了多条线、
    console 端口冲突、孤立设备、图标/文字/背景框重叠，以及条件型号风险。
    """
    try:
        topo = _load(path)
        return _ok(**validate.check(topo))
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_render(path: str, out_path: str = "", scale: float = 1.0, show_ports: bool = True) -> str:
    """可选：把拓扑画成辅助 PNG，不用开 eNSP 就能粗看布局。

    PNG 不是 eNSP 原生界面，也不能证明 VirtualBox、镜像或设备能启动；
    正常建图和验收流程不依赖它。

    Args:
        path: .topo 文件路径。
        out_path: 图片输出路径，省略则和 .topo 同名同目录，扩展名换成 .png。
        scale: 缩放比例，设备多、连线挤的时候调大到 1.5 左右。
        show_ports: 是否在连线两端标出接口名，默认标。
    """
    try:
        topo = _load(path)
        target = out_path or os.path.splitext(path)[0] + ".png"
        return _ok(**render.render(topo, target, scale=scale, show_ports=show_ports))
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# eNSP / VirtualBox 启动前检查
# ---------------------------------------------------------------------------


@mcp.tool()
@_serialised
def environment_check(
    path: str = "", models: list[str] | None = None, ensp_home: str = ""
) -> str:
    """只读检查 eNSP、VirtualBox、模板机和设备镜像，不启动或修改任何虚拟机。

    检查结果的 readyForTopology=true 才表示这份拓扑的已知前置条件
    已满足。检查会识别 VirtualBox 版本、Hyper-V/VBS 冲突、Host-Only 网卡、
    AR/WLAN 基机、SVRP 镜像和 USG6000V 镜像/注册状态。

    Args:
        path: 可选的 .topo。给出后按其中实际型号判断 readyForTopology。
        models: 可选的计划型号列表；还没创建拓扑时可先检查，例如
            ["AR2220", "USG5500"]。与 path 同时给出时合并检查。
        ensp_home: 可选的 eNSP 安装目录；省略时从环境、注册表和常见目录查找。
    """
    try:
        if models is not None and not isinstance(models, list):
            return _err("models 必须是型号名称列表。")
        if models is not None and any(
            not isinstance(model, str) or not model.strip() for model in models
        ):
            return _err("models 中每一项都必须是非空型号名称。")
        return _dump(doctor.inspect_environment(path, ensp_home, models))
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def topo_export(path: str, format: str = "mermaid", out_path: str = "") -> str:
    """把拓扑导成写文档用得上的格式。

    Args:
        path: .topo 文件路径。
        format: mermaid（流程图源码）、drawio（可导入 draw.io）、
            csv（设备表 + 接口连接表）。
        out_path: 输出文件路径，省略则只把内容返回、不落盘。
            csv 会写成两个文件，分别加 _devices / _links 后缀。
    """
    try:
        topo = _load(path)
        fmt = format.lower()
        if fmt not in export.FORMATS:
            return _err(f"format 只能是 {', '.join(export.FORMATS)}，收到 {format!r}")

        if fmt == "csv":
            tables = export.to_csv(topo)
            written = {}
            if out_path:
                stem = os.path.splitext(out_path)[0]
                for key, content in tables.items():
                    target = f"{stem}_{key}.csv"
                    xmlio.atomic_write(target, content, encoding="utf-8-sig")
                    written[key] = os.path.abspath(target)
            return _ok(format=fmt, files=written or None, devices=tables["devices"], links=tables["links"])

        content = export.to_mermaid(topo) if fmt == "mermaid" else export.to_drawio(topo)
        written_path = None
        if out_path:
            xmlio.atomic_write(out_path, content)
            written_path = os.path.abspath(out_path)
        return _ok(format=fmt, file=written_path, content=content)
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# 设备配置
# ---------------------------------------------------------------------------


def _resolve_target(target: str, topo_path: str = "") -> tuple[int, str]:
    text = str(target).strip()
    if text.isdigit():
        port = int(text)
        if not 1 <= port <= 65535:
            raise TopologyError("console 端口必须在 1–65535 之间。")
        return port, text
    path = topo_path or _last_topo
    if not path:
        raise TopologyError("按设备名连接需要 topo_path，或先调用 topo_inspect。")
    device = Topology.load(path).find(text)
    if not 1 <= device.com_port <= 65535:
        raise TopologyError(f"{device.name} 没有可用的 console 端口。")
    return device.com_port, device.name


def _failure(exc: Exception) -> str:
    payload = {"code": getattr(exc, "code", "invalid_request")}
    if isinstance(exc, console.ConsoleError) and exc.partial_output:
        payload["partialOutput"] = exc.partial_output
    return _err(str(exc), **payload)


def _log_topo(topo_path: str) -> str:
    """留痕时用实际生效的拓扑路径：显式给的优先，否则回落到最近一次用过的。"""
    path = topo_path or _last_topo or ""
    return os.path.abspath(path) if path else ""


@asynccontextmanager
async def _connection(target, topo_path, host, username, password, new_password):
    credentials = console.validate_credentials(username, password, new_password)
    port, label = _resolve_target(target, topo_path)
    if not host.strip() or any(ord(c) < 33 for c in host):
        raise ValueError("host 必须是有效的主机名或 IP 地址。")
    async with pool.lock_for(port, host):
        device = await pool.acquire(port, host, credentials=credentials, new_password=new_password)
        yield device, port, label


def _validate_commands(commands: list[str]) -> list[str]:
    if not commands or len(commands) > 10000:
        raise ValueError("commands 必须包含 1–10000 条命令。")
    for command in commands:
        if not isinstance(command, str) or any(ord(c) < 32 for c in command):
            raise ValueError("每条命令必须是单行文本，不能包含控制字符。")
    active = [c.strip() for c in commands if c.strip() and not c.strip().startswith(("#", "!"))]
    if not active:
        raise ValueError("没有可执行的命令。")
    return active


async def _execute(
    device,
    commands,
    save,
    stop_on_error,
    confirm="Y",
    *,
    label: str = "",
    port: Any = "",
    tool: str = "",
    topo_path: str = "",
    host: str = "127.0.0.1",
    batch: str = "",
) -> dict:
    results = await device.send_many(commands, confirm=confirm, stop_on_error=stop_on_error)
    failures = [r for r in results if r["error"]]
    saved = None
    save_status = {"requested": save, "status": "not_requested"}
    if save:
        if failures or len(results) != len(commands):
            save_status["status"] = "skipped_due_to_errors"
        else:
            try:
                saved = await device.save_config()
                save_status.update(status="saved", ok=True)
            except console.ConsoleError as exc:
                save_status.update(status="failed", ok=False, code=exc.code, error=str(exc), output=exc.partial_output)
    ok = not failures and len(results) == len(commands) and save_status["status"] != "failed"
    payload = {"ok": ok, "results": results, "failures": failures, "errorCount": len(failures) + int(save_status["status"] == "failed"),
               "executedCount": len(results), "requestedCount": len(commands), "stoppedEarly": len(results) < len(commands),
               "saved": saved, "saveStatus": save_status,
               "view": device.prompt.view if device.prompt else "unknown", "hostname": device.prompt.hostname if device.prompt else None}
    if not ok:
        payload["error"] = "部分命令或保存未成功；请检查 results 和 saveStatus。"

    # 命令留痕：每条命令记到哪台设备上，供 command_trace 生成可视化
    logged = list(results)
    if save and save_status["status"] in ("saved", "failed"):
        logged.append(
            {
                "command": "save",
                "ok": save_status["status"] == "saved",
                "output": saved or "",
                "error": save_status.get("error"),
                "view": "user",
            }
        )
    if label and logged:
        try:
            commandlog.record(
                device=label,
                port=port,
                tool=tool or "device_exec",
                results=logged,
                topo_path=topo_path,
                host=host,
                batch=batch,
            )
        except OSError:
            pass
    return payload


@mcp.tool()
async def device_exec(target: str, commands: list[str], topo_path: str = "", host: str = "127.0.0.1",
                      view: str = "keep", save: bool = False, username: str = "", password: str = "",
                      new_password: str = "", stop_on_error: bool = True, confirm: str = "Y") -> str:
    """在已启动的 eNSP 设备 console 上逐条执行命令。

    target 为设备名（配 topo_path）或端口号。view 为 keep/user/system。
    username/password 用于需要登录的 console，只有密码的 console 可只给 password。
    new_password 仅在设备要求初次设置或修改密码时使用；不主动修改已有密码。
    stop_on_error 默认遇错停止；失败时不 save。confirm 为 Y/N/YES/NO。
    返回 ok、逐条 results、errorCount、stoppedEarly 和 saveStatus；超时断线不会重试命令。
    """
    try:
        commands = _validate_commands(commands)
        if view not in ("keep", "user", "system"):
            return _err("view 只能是 keep / user / system。")
        if confirm.upper() not in ("Y", "N", "YES", "NO"):
            return _err("confirm 只能是 Y / N / YES / NO。")
        async with _connection(target, topo_path, host, username, password, new_password) as (device, port, label):
            if view == "user":
                await device.to_user_view()
            elif view == "system":
                await device.to_system_view()
            payload = await _execute(
                device,
                commands,
                save,
                stop_on_error,
                confirm,
                label=label,
                port=port,
                tool="device_exec",
                topo_path=_log_topo(topo_path),
                host=host,
            )
        return _dump({"device": label, "consolePort": port, **payload})
    except (console.ConsoleError, ValueError, KeyError, TypeError, OSError) as exc:
        return _failure(exc)


def _config_commands(config: str) -> list[str]:
    commands = []
    boundary = False
    for raw in config.splitlines():
        line = raw.strip()
        if not line or line.startswith("!"):
            continue
        if line.startswith("#"):
            boundary = bool(commands) if line == "#" else boundary
            continue
        if line == "return":
            boundary = bool(commands)
            continue
        if line == "system-view" and not commands:
            continue
        if boundary:
            commands.extend(["return", "system-view"])
            boundary = False
        commands.append(line)
    return _validate_commands(commands)


@mcp.tool()
async def device_push_config(target: str, config: str = "", config_file: str = "", topo_path: str = "",
                             host: str = "127.0.0.1", save: bool = True, username: str = "", password: str = "",
                             new_password: str = "", stop_on_error: bool = True) -> str:
    """从文本或 UTF-8 .cfg 下发配置，config/config_file 必须且只能选一个。

    自动进入系统视图，支持带 # 分段的 display current-configuration 配置文本，
    分段时退出子视图再进入系统视图。默认遇错停止且不保存；不会自动回滚已生效配置。
    username/password/new_password 与 device_exec 相同。save 默认 true。
    CE 提示符标记存在未提交改动时，保存前先 commit；任何保存异常都返回失败状态。
    """
    try:
        if bool(config) == bool(config_file):
            return _err("config 和 config_file 必须给一个，且只给一个。")
        if config_file:
            with open(config_file, "r", encoding="utf-8-sig") as handle:
                config = handle.read()
        commands = _config_commands(config)
        async with _connection(target, topo_path, host, username, password, new_password) as (device, port, label):
            await device.to_system_view()
            payload = await _execute(
                device,
                commands,
                save,
                stop_on_error,
                label=label,
                port=port,
                tool="device_push_config",
                topo_path=_log_topo(topo_path),
                host=host,
            )
        return _dump({"device": label, "consolePort": port, "lineCount": len(commands), **payload})
    except (console.ConsoleError, ValueError, KeyError, TypeError, OSError) as exc:
        return _failure(exc)


@mcp.tool()
async def device_fetch_config(target: str, topo_path: str = "", host: str = "127.0.0.1", save_to: str = "",
                              compare_with: str = "", username: str = "", password: str = "", new_password: str = "") -> str:
    """读回运行配置；save_to 可保存 UTF-8 文件，compare_with 可与基线文件比较。

    diff 同时提供 identical、onlyOnDevice、onlyInFile 和带上下文的 unifiedDiff，
    保留行顺序及重复行。命令失败或超时不写出配置文件。登录参数与 device_exec 相同。
    """
    try:
        baseline = None
        if compare_with:
            with open(compare_with, "r", encoding="utf-8-sig") as handle:
                baseline = handle.read()
        async with _connection(target, topo_path, host, username, password, new_password) as (device, port, label):
            await device.to_user_view()
            text = await device.send("display current-configuration", timeout=25.0)
        error = console._error_in(text)
        commandlog.record(
            device=label,
            port=port,
            tool="device_fetch_config",
            results=[
                {
                    "command": "display current-configuration",
                    "ok": not error,
                    "error": error,
                    "output": text,
                    "view": "user",
                }
            ],
            topo_path=_log_topo(topo_path),
            host=host,
        )
        if error:
            return _err(error, code="command_failed", output=text)
        if not text.strip():
            return _err("设备未返回配置内容。", code="empty_configuration")
        payload = {"device": label, "consolePort": port, "config": text, "lineCount": len(text.splitlines())}
        if baseline is not None:
            payload["diff"] = console.compare_configs(baseline, text)
        if save_to:
            xmlio.atomic_write(save_to, text + "\n")
            payload["savedTo"] = os.path.abspath(save_to)
        return _ok(**payload)
    except (console.ConsoleError, ValueError, KeyError, TypeError, OSError) as exc:
        return _failure(exc)


def _destination(value: str) -> str:
    if not value or len(value) > 253 or any(ord(c) < 33 for c in value):
        raise ValueError("destination 必须是单个 IP 地址或主机名。")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        labels = value.rstrip(".").split(".")
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
            raise ValueError("destination 必须是单个 IP 地址或主机名。")
        return value


@mcp.tool()
async def device_probe(target: str, action: str = "ping", destination: str = "", count: int = 5,
                       topo_path: str = "", host: str = "127.0.0.1", username: str = "", password: str = "",
                       new_password: str = "") -> str:
    """设备连通性和状态查询。action: ping/tracert/interfaces/arp/mac/route/vlan/ospf。

    ping/tracert 需要 destination；ping count 为 1–100。ping 返回统计和 reachable，
    无法解析时 reachable 为 null；其他查询返回 output 原文。ok 表示命令执行状态，
    ping 是否连通看 parsed.reachable。登录参数与 device_exec 相同。
    """
    try:
        commands = {"interfaces": "display ip interface brief", "arp": "display arp", "mac": "display mac-address",
                    "route": "display ip routing-table", "vlan": "display vlan", "ospf": "display ospf peer brief"}
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            return _err("count 必须是 1–100 的整数。")
        timeout = None
        if action in ("ping", "tracert"):
            destination = _destination(destination)
            command = f"ping -c {count} {destination}" if action == "ping" else f"tracert {destination}"
            timeout = max(25.0, count * 5.0 + 5.0) if action == "ping" else 180.0
        elif action in commands:
            command = commands[action]
        else:
            return _err("不支持的 action。")
        async with _connection(target, topo_path, host, username, password, new_password) as (device, port, label):
            await device.to_user_view()
            output = await device.send(command, timeout=timeout)
        error = console._error_in(output)
        commandlog.record(
            device=label,
            port=port,
            tool="device_probe",
            results=[
                {
                    "command": command,
                    "ok": not error,
                    "error": error,
                    "output": output,
                    "view": "user",
                }
            ],
            topo_path=_log_topo(topo_path),
            host=host,
        )
        payload = {"device": label, "consolePort": port, "action": action, "command": command, "output": output}
        if error:
            return _err(error, code="command_failed", **payload)
        if action == "ping":
            payload["parsed"] = console.parse_ping(output)
        return _ok(**payload)
    except (console.ConsoleError, ValueError, KeyError, TypeError, OSError) as exc:
        return _failure(exc)


@mcp.tool()
async def device_sessions(action: str = "list", target: str = "", host: str = "127.0.0.1") -> str:
    """查看或关闭 console 会话。action 为 list/close/close_all。

    close 时 target 为端口号。后台每 30 秒回收空闲超过 5 分钟的连接；
    服务退出时关闭所有连接。关闭操作等待该连接正在执行的命令结束。
    """
    try:
        if action == "list":
            await pool.reap_expired()
            return _ok(sessions=pool.sessions())
        if action == "close":
            if not target.isdigit():
                return _err("target 必须是 console 端口号。")
            port, _ = _resolve_target(target)
            return _ok(closed=await pool.close(port, host), port=port)
        if action == "close_all":
            return _ok(closed=await pool.close_all())
        return _err("action 只能是 list / close / close_all。")
    except (console.ConsoleError, ValueError, OSError) as exc:
        return _failure(exc)


# ---------------------------------------------------------------------------
# 批量并发
# ---------------------------------------------------------------------------

MAX_CONCURRENCY = 16
OUTPUT_PREVIEW = 600


def _device_table(topo_path: str) -> list[dict]:
    topo = _load(topo_path)
    return [
        {
            "name": device.name,
            "model": device.model,
            "category": device.spec.category,
            "consolePort": device.com_port or None,
        }
        for device in topo.devices
    ]


def _pick_devices(requested: list[str] | None, table: list[dict]) -> list[dict]:
    """把 devices 参数解析成设备条目：支持设备名、类别（AR/FW/...）和 all。"""
    connectable = [entry for entry in table if entry["consolePort"]]
    if not requested:
        if not connectable:
            raise ValueError("拓扑里没有带 console 端口的设备。先用 topo_add_devices 加设备，并在 eNSP 里开机。")
        return connectable
    chosen: list[dict] = []
    seen: set[str] = set()
    for raw in requested:
        text = str(raw).strip()
        if not text:
            continue
        if text.lower() == "all":
            selected = connectable
        else:
            exact = [entry for entry in table if entry["name"] == text]
            if exact:
                selected = exact
            elif text.upper() in verify.CATEGORY_NAMES:
                selected = [entry for entry in connectable if str(entry["category"]).upper() == text.upper()]
                if not selected:
                    raise ValueError(f"拓扑里没有{verify.CATEGORY_NAMES[text.upper()]}类设备。")
            else:
                known = ", ".join(entry["name"] for entry in table) or "（空拓扑）"
                raise ValueError(f"拓扑里没有设备 {text!r}。现有设备：{known}")
        for entry in selected:
            if entry["name"] in seen:
                continue
            if not entry["consolePort"]:
                raise ValueError(f"{entry['name']} 没有 console 端口，无法下发命令（PC / Server 类设备只能看拓扑）。")
            seen.add(entry["name"])
            chosen.append(entry)
    if not chosen:
        raise ValueError("没有选中任何设备。")
    return chosen


def _concurrency(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency 必须是 1–{MAX_CONCURRENCY} 的整数。")
    return value


@mcp.tool()
async def device_batch(
    topo_path: str,
    devices: list[str] | None = None,
    commands: list[str] | None = None,
    configs: dict[str, str] | None = None,
    host: str = "127.0.0.1",
    concurrency: int = 4,
    save: bool | None = None,
    stop_on_error: bool = True,
    include_results: bool = False,
    username: str = "",
    password: str = "",
    new_password: str = "",
) -> str:
    """对多台设备并发执行命令或下发配置，单台失败不影响其他设备。

    两种模式，commands 和 configs 必须且只能给一个：

    - commands：同一组命令打到所有选中设备，适合批量查询（display ...）
      或统一配置片段。
    - configs：每台设备一份配置，写成 {"Core1": "sysname Core1\\n...", "Core2": ...}，
      键必须是拓扑里的设备名。

    devices 省略表示拓扑里所有带 console 的设备；也可以给设备名、设备类别
    （AR / LSW / FW / WLAN / CLIENT）或 "all" 混合使用，例如
    ["FW1", "FW2", "LSW"]。

    concurrency 是同时连几台，默认 4，范围 1–16。eNSP 的 console 端口本身能
    承受并发，但一次开太多会拖慢设备启动较慢的那些，建议 4–8。

    save 省略时按模式决定：commands 模式不保存（查询命令不需要），
    configs 模式保存。返回每台设备的 ok、执行条数、错误数和失败命令，
    默认不回传全部回显；要看完整 results 就设 include_results=true。

    所有命令同样会自动留痕，这次调用的命令在报告里归为同一个 batch。
    """
    try:
        if bool(commands) == bool(configs):
            return _err("commands 和 configs 必须给一个，且只给一个。")
        limit = _concurrency(concurrency)
        table = _device_table(topo_path)
        targets = _pick_devices(devices, table)
        batch_id = f"batch-{int(time.time() * 1000)}"

        if configs is not None:
            if not isinstance(configs, dict) or not configs:
                return _err("configs 必须是非空对象，键是设备名。")
            allowed = {entry["name"]: entry for entry in targets}
            unknown = [name for name in configs if name not in allowed]
            if unknown:
                return _err(
                    f"configs 里的设备不在本次目标中：{', '.join(sorted(unknown))}。"
                    f"可选：{', '.join(sorted(allowed))}",
                )
            mode = "config"
            tool_name = "device_batch_config"
            plan = {name: _config_commands(text) for name, text in configs.items()}
            do_save = True if save is None else save
            targets = [entry for entry in targets if entry["name"] in plan]
            if not targets:
                return _err("configs 里的设备都没被 devices 选中。")
        else:
            for command in commands or []:
                if not isinstance(command, str):
                    return _err("commands 里每一项都必须是文本。")
            active = _validate_commands(list(commands or []))
            mode = "exec"
            tool_name = "device_batch_exec"
            plan = {entry["name"]: active for entry in targets}
            do_save = False if save is None else save

        semaphore = asyncio.Semaphore(limit)

        async def run_one(entry: dict) -> dict:
            async with semaphore:
                began = time.monotonic()
                try:
                    async with _connection(
                        entry["name"], topo_path, host, username, password, new_password
                    ) as (device, port, label):
                        if mode == "config":
                            await device.to_system_view()
                        payload = await _execute(
                            device,
                            plan[entry["name"]],
                            do_save,
                            stop_on_error,
                            label=label,
                            port=port,
                            tool=tool_name,
                            topo_path=_log_topo(topo_path),
                            host=host,
                            batch=batch_id,
                        )
                    item = {
                        "device": label,
                        "consolePort": port,
                        "ok": payload["ok"],
                        "executedCount": payload["executedCount"],
                        "requestedCount": payload["requestedCount"],
                        "errorCount": payload["errorCount"],
                        "stoppedEarly": payload["stoppedEarly"],
                        "saveStatus": payload["saveStatus"]["status"],
                        "durationMs": int((time.monotonic() - began) * 1000),
                        "failures": [
                            {"command": failure["command"], "error": failure["error"]}
                            for failure in payload["failures"]
                        ],
                    }
                    if include_results:
                        item["results"] = payload["results"]
                    return item
                except Exception as exc:  # 单台失败不能拖垮整批
                    return {
                        "device": entry["name"],
                        "consolePort": entry["consolePort"],
                        "ok": False,
                        "executedCount": 0,
                        "requestedCount": len(plan[entry["name"]]),
                        "errorCount": 1,
                        "stoppedEarly": True,
                        "saveStatus": "not_requested",
                        "durationMs": int((time.monotonic() - began) * 1000),
                        "error": str(exc),
                        "code": getattr(exc, "code", "invalid_request"),
                        "failures": [],
                    }

        started = time.monotonic()
        results = await asyncio.gather(*(run_one(entry) for entry in targets))
        elapsed = round(time.monotonic() - started, 1)

        failed = [item for item in results if not item["ok"]]
        payload: dict[str, Any] = {
            "mode": mode,
            "batch": batch_id,
            "deviceCount": len(results),
            "succeeded": len(results) - len(failed),
            "failed": len(failed),
            "elapsedSeconds": elapsed,
            "concurrency": limit,
            "devices": list(results),
        }
        if failed:
            payload["error"] = (
                f"{len(failed)} 台设备未成功：{', '.join(item['device'] for item in failed)}。"
                "逐台看 devices[].error 和 failures。"
            )
        return _dump({"ok": not failed, **payload})
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# 实验自动验收
# ---------------------------------------------------------------------------


@mcp.tool()
async def lab_verify(
    topo_path: str,
    suite: str = "",
    checks: list[dict] | None = None,
    devices: list[str] | None = None,
    host: str = "127.0.0.1",
    concurrency: int = 4,
    username: str = "",
    password: str = "",
    new_password: str = "",
    include_output: bool = True,
    save_report: str = "",
) -> str:
    """按规则验证实验是否真的做对了，给出 PASS / FAIL 判定。

    检查项 = 某台设备上跑一条命令 + 对回显的判定规则。两种给法：

    - suite：内置套件名，见下。
    - checks：自定义检查项列表，每项：
      {"name": "VRRP 主备", "target": "FW1", "command": "display vrrp",
       "expect": {"minCount": {"pattern": "State\\\\s*:\\\\s*(Master|Backup)", "atLeast": 1}}}

      target 可以是设备名、设备类别（AR / LSW / FW / WLAN / CLIENT）或 all，
      用类别时会自动展开到这类所有设备。

    expect 可组合的规则：contains / notContains（字符串或列表）、regex、
    notRegex、minCount{"pattern","atLeast"}、reachable（布尔，ping 是否通）、
    maxLoss（丢包率上限）。留空表示“命令跑通就算过”。

    内置套件：
    - reachable：逐台登录，只验证 console 可连（最简单的连通性体检）
    - fw_ha：防火墙双机热备（HRP 角色 + 心跳口 + VRRP 主备）
    - bfd：BFD 会话 Up
    - ospf：OSPF 邻居 Full
    - vrrp：VRRP 主备
    - vlan：交换机上存在业务 VLAN

    devices 可限定范围。检查项并发执行，concurrency 默认 4、最大 16。
    返回 verdict（PASS / FAIL）、passed / failed / skipped 计数和每条检查的
    状态、未满足的条件、回显片段。save_report 可把结果另存为 JSON。

    验收跑过的命令同样自动留痕，tool 记为 lab_verify。
    """
    try:
        if checks is not None and not isinstance(checks, list):
            return _err("checks 必须是检查项列表。")
        limit = _concurrency(concurrency)
        table = _device_table(topo_path)
        if devices:
            table = _pick_devices(devices, table)

        if checks:
            spec = list(checks)
            suite_name = "custom"
        else:
            suite_name = (suite or "reachable").strip().lower()
            spec = verify.resolve_suite(suite_name)

        tasks, skipped = verify.expand(spec, table)
        if not tasks:
            return _dump(
                {
                    "ok": False,
                    "verdict": verify.SKIP,
                    "suite": suite_name,
                    "error": "没有可执行的检查项：" + "；".join(item["reason"] for item in skipped),
                    "passed": 0,
                    "failed": 0,
                    "skipped": len(skipped),
                    "total": len(skipped),
                    "checks": [],
                    "skippedChecks": skipped,
                }
            )

        batch_id = f"verify-{int(time.time() * 1000)}"
        semaphore = asyncio.Semaphore(limit)

        async def run_check(task: dict) -> dict:
            async with semaphore:
                started = time.monotonic()
                base = {
                    "name": task["name"],
                    "device": task["device"],
                    "consolePort": task["consolePort"],
                    "command": task["command"],
                }
                try:
                    async with _connection(
                        task["device"], topo_path, host, username, password, new_password
                    ) as (device, port, label):
                        await device.to_user_view()
                        output = await device.send(task["command"])
                except Exception as exc:
                    elapsed = int((time.monotonic() - started) * 1000)
                    try:
                        commandlog.record(
                            device=task["device"],
                            port=task["consolePort"],
                            tool="lab_verify",
                            results=[
                                {
                                    "command": task["command"],
                                    "ok": False,
                                    "error": str(exc),
                                    "output": getattr(exc, "partial_output", ""),
                                    "view": "unknown",
                                }
                            ],
                            topo_path=_log_topo(topo_path),
                            host=host,
                            batch=batch_id,
                        )
                    except OSError:
                        pass
                    return {
                        **base,
                        "status": verify.ERROR,
                        "problems": [str(exc)],
                        "durationMs": elapsed,
                    }

                elapsed = int((time.monotonic() - started) * 1000)
                try:
                    passed, problems = verify.evaluate(task["expect"], output, command=task["command"])
                except ValueError as exc:
                    passed, problems = False, [str(exc)]
                try:
                    commandlog.record(
                        device=label,
                        port=port,
                        tool="lab_verify",
                        results=[
                            {
                                "command": task["command"],
                                "ok": passed,
                                "error": None if passed else "；".join(problems),
                                "output": output,
                                "view": "user",
                            }
                        ],
                        topo_path=_log_topo(topo_path),
                        host=host,
                        batch=batch_id,
                    )
                except OSError:
                    pass
                result = {
                    **base,
                    "status": verify.PASS if passed else verify.FAIL,
                    "durationMs": elapsed,
                }
                if problems:
                    result["problems"] = problems
                if include_output:
                    text = output.strip()
                    result["output"] = text if len(text) <= OUTPUT_PREVIEW else text[:OUTPUT_PREVIEW] + "…（已截断）"
                return result

        started = time.monotonic()
        results = await asyncio.gather(*(run_check(task) for task in tasks))
        elapsed = round(time.monotonic() - started, 1)

        passed_items = [item for item in results if item["status"] == verify.PASS]
        failed_items = [item for item in results if item["status"] in (verify.FAIL, verify.ERROR)]
        verdict = verify.PASS if not failed_items else verify.FAIL

        payload: dict[str, Any] = {
            "verdict": verdict,
            "suite": suite_name,
            "topo": _log_topo(topo_path),
            "batch": batch_id,
            "total": len(results) + len(skipped),
            "passed": len(passed_items),
            "failed": len(failed_items),
            "skipped": len(skipped),
            "elapsedSeconds": elapsed,
            "checks": list(results),
            "skippedChecks": skipped,
        }
        if save_report:
            xmlio.atomic_write(save_report, _dump(payload) + "\n")
            payload["report"] = os.path.abspath(save_report)
        if failed_items:
            payload["error"] = (
                f"验收未通过：{len(failed_items)} 项失败 —— "
                + "；".join(f"{item['device']}: {item['name']}" for item in failed_items[:6])
            )
        return _dump({"ok": verdict == verify.PASS, **payload})
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


@mcp.tool()
@_serialised
def command_trace(
    path: str = "",
    device: str = "",
    out_path: str = "",
    limit: int = 2000,
    show_output: bool = True,
    open_browser: bool = False,
) -> str:
    """把“哪条命令打到了哪台设备”整理成可交互的 HTML 可视化报告。

    报告分两块：左边是按 .topo 真实坐标画的拓扑，每台设备角标写着它收到多少条命令、
    有几条失败，点设备会跳到右边对应的命令明细；右边按设备列出每条命令的时间、
    来自哪个工具（下发 / 执行 / 探测 / 读回）、执行状态和可展开的回显。

    所有设备操作都会自动留痕，不需要额外开关。日志在
    %USERPROFILE%\\.ensp-mcp\\commands.jsonl，可用 ENSP_MCP_LOG_DIR 改目录。

    Args:
        path: .topo 路径。给了就只看这份拓扑上的记录，并按它的坐标画图；
            省略则用最近一次 topo_* 用过的拓扑，再没有就把日志里的设备平铺成网格。
        device: 只看某一台设备，例如 "Core1"。省略看全部。
        out_path: 报告输出路径，省略则写到拓扑同目录的 <拓扑名>-commands.html。
        limit: 最多取最近多少条命令记录，默认 2000，范围 1–20000。
        show_output: 明细里是否附带命令回显，默认带。
        open_browser: 生成后是否自动用默认浏览器打开，默认不打开。

    返回 report（HTML 绝对路径）、commandCount、failedCommands、byDevice 等统计。
    """
    try:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20000:
            return _err("limit 必须是 1–20000 的整数。")
        topo = _log_topo(path)
        if topo and not os.path.exists(topo):
            return _err(f"找不到拓扑文件 {topo}。不带 path 就用最近一次用过的拓扑。")
        entries = commandlog.load(topo_path=topo, device=device, limit=limit)
        result = commandlog.build_report(
            topo_path=topo,
            entries=entries,
            out_path=out_path,
            show_output=show_output,
        )
        if open_browser:
            try:
                import webbrowser

                webbrowser.open("file:///" + result["report"].replace("\\", "/"))
            except OSError:
                pass
        return _ok(
            message="报告已生成，用浏览器打开 report 指向的文件即可。",
            log=os.path.abspath(commandlog.log_path()),
            **result,
        )
    except (TopologyError, KeyError, TypeError, ValueError, OSError) as exc:
        return _err(str(exc))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
