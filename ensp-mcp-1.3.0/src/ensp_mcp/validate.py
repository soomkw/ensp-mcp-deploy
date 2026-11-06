"""拓扑校验：在拿到 eNSP 里打开之前先把明显的错挑出来。

``error`` 会让 eNSP 打开异常或链路失效，``warning`` 只是提醒。
"""

from __future__ import annotations

from collections import defaultdict
import math

from . import annotations, catalog
from .topo import DYNAMIC_PORT_MODELS, DYNAMIC_PORT_LIMIT, Topology, TopologyError, port_of, compatible_line_type


def check(topo: Topology) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    by_id = topo.by_id()

    _check_names(topo, errors)
    ids = set()
    for device in topo.devices:
        if not device.dev_id or device.dev_id in ids:
            errors.append(f"{device.name} 的设备 ID 为空或重复")
        ids.add(device.dev_id)
        if not math.isfinite(device.cx) or not math.isfinite(device.cy):
            errors.append(f"{device.name} 的坐标不是有限数值")
        if not 0 <= device.com_port <= 65535:
            errors.append(f"{device.name} 的 console 端口超出范围")
        if device.spec.has_console and device.com_port == 0:
            warnings.append(f"{device.name} 没有设置 console 端口")
    _check_console_ports(topo, errors)
    _check_links(topo, by_id, errors, warnings)
    _check_isolated(topo, warnings)
    _check_overlap(topo, warnings)
    annotation_errors, annotation_warnings = annotations.validation_issues(topo)
    errors.extend(annotation_errors)
    warnings.extend(annotation_warnings)
    advisories = _model_advisories(topo, warnings)

    return {
        "ok": not errors,
        "errorCount": len(errors),
        "warningCount": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "advisories": advisories,
    }


def _check_names(topo: Topology, errors: list[str]) -> None:
    seen: dict[str, str] = {}
    for device in topo.devices:
        key = device.name.lower()
        if key in seen:
            errors.append(f"设备名重复：{device.name}（与 {seen[key]} 冲突）")
        else:
            seen[key] = device.name
        if not device.name.strip():
            errors.append(f"有设备没有名字（型号 {device.model}）")


def _check_console_ports(topo: Topology, errors: list[str]) -> None:
    used: dict[int, list[str]] = defaultdict(list)
    for device in topo.devices:
        if device.com_port:
            used[device.com_port].append(device.name)
    for port, names in sorted(used.items()):
        if len(names) > 1:
            errors.append(
                f"console 端口 {port} 被多台设备占用：{', '.join(names)}"
            )


def _check_links(topo: Topology, by_id: dict, errors: list[str], warnings: list[str]) -> None:
    occupied: dict[tuple[str, int], int] = defaultdict(int)

    for link in topo.links:
        src = by_id.get(link.src_id)
        dst = by_id.get(link.dst_id)
        if src is None or dst is None:
            errors.append("有链路指向了不存在的设备，可能是删设备时没清理干净")
            continue

        for device, index in ((src, link.src_index), (dst, link.dst_index)):
            spec = device.spec
            if device.model in DYNAMIC_PORT_MODELS:
                if not 0 <= index < DYNAMIC_PORT_LIMIT:
                    errors.append(f"{device.name} 的 Cloud 接口序号 {index} 越界")
            elif spec.port_count == 0:
                errors.append(
                    f"{device.name}（{device.model}）没有有线接口，不能拉线，"
                    f"这类设备只能和 AP 无线关联"
                )
            elif not 0 <= index < spec.port_count:
                errors.append(
                    f"{device.name}（{device.model}）的接口序号 {index} 越界，"
                    f"该型号只有 {spec.port_count} 个接口"
                )
            occupied[(device.dev_id, index)] += 1

        if src.dev_id == dst.dev_id:
            errors.append(f"{src.name} 自己连自己")
        a, b = port_of(src, link.src_index), port_of(dst, link.dst_index)
        if a.kind != "Unknown" and b.kind != "Unknown" and link.line_type != "Auto":
            try:
                compatible_line_type(a, b, link.line_type)
            except TopologyError as exc:
                errors.append(f"{src.name} → {dst.name}：{exc}")

    for (dev_id, index), count in occupied.items():
        if count > 1:
            device = by_id[dev_id]
            port = port_of(device, index).name
            errors.append(f"{device.name} 的 {port} 接了 {count} 条线，一个接口只能连一条")

    for device in topo.devices:
        if device.model in DYNAMIC_PORT_MODELS and topo.used_ports(device.dev_id):
            warnings.append(
                f"{device.name} 是 Cloud，拓扑文件里接口数为 0，"
                f"需要在 eNSP 里手工添加接口并绑定网卡后连线才生效"
            )


def _check_isolated(topo: Topology, warnings: list[str]) -> None:
    linked: set[str] = set()
    for link in topo.links:
        linked.add(link.src_id)
        linked.add(link.dst_id)
    for device in topo.devices:
        if device.dev_id in linked:
            continue
        if device.spec.port_count == 0 and device.model not in DYNAMIC_PORT_MODELS:
            continue  # STA / Cellphone 本来就没有线
        warnings.append(f"{device.name}（{device.model}）没有连接任何设备")


def _check_overlap(topo: Topology, warnings: list[str]) -> None:
    devices = annotations.device_rectangles(topo.devices)
    for i, (a_device, a_rect) in enumerate(devices):
        for b_device, b_rect in devices[i + 1 :]:
            if a_rect.overlaps(b_rect):
                warnings.append(
                    f"{a_device.name} 和 {b_device.name} 的图标或名称重叠，"
                    f"请用 topo_layout 普通布局或 zones 分区布局重新摆开"
                )


def _model_advisories(topo: Topology, warnings: list[str]) -> list[dict]:
    result = []
    for model_name in dict.fromkeys(device.model for device in topo.devices):
        try:
            policy = catalog.advisory(model_name)
        except (KeyError, ValueError):
            continue
        if policy["selectionTier"] != "conditional" and not policy["runtimePrerequisites"]:
            continue
        result.append(policy)
        alternative = policy.get("recommendedAlternative")
        suffix = f"；没有硬性型号要求时优先使用 {alternative}" if alternative else ""
        if policy["selectionTier"] == "conditional":
            warnings.append(
                f"{model_name} 属于条件使用型号，启动风险为 {policy['startupRisk']}，"
                f"请先运行 environment_check 检查镜像与虚拟化环境{suffix}"
            )
        else:
            warnings.append(
                f"{model_name} 的启动依赖 VirtualBox 基机和虚拟化环境；"
                "离线拓扑校验无法确认，请先运行 environment_check"
            )
    return result
