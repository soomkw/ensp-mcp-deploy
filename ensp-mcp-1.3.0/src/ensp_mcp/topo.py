"""``.topo`` 的对象模型：设备、链路，以及两者的增删查改。"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import random
import re
import uuid
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from . import catalog, xmlio
from .catalog import Model, Port

# 设备图标半径，用于把连线端点从图标中心推到边缘
ICON_RADIUS = 35.5
# 有 console 的设备从 2000 号端口开始依次占用
CONSOLE_PORT_BASE = 2000
# 接口在 eNSP 界面里现加的型号：拓扑文件里接口数为 0，但允许连线
DYNAMIC_PORT_MODELS = {"Cloud"}
# Cloud 最多能加的接口数，够用且能挡住手滑写出的离谱序号
DYNAMIC_PORT_LIMIT = 32


def new_device_id(rng: random.Random | None = None) -> str:
    """生成 eNSP 风格的 GUID：第三段小写，其余大写。"""
    r = rng or random
    raw = uuid.UUID(int=r.getrandbits(128), version=4)
    parts = str(raw).upper().split("-")
    parts[2] = parts[2].lower()
    return "-".join(parts)


@dataclass
class Device:
    name: str
    model: str
    cx: float
    cy: float
    dev_id: str = field(default_factory=new_device_id)
    com_port: int = 0
    system_mac: str = ""
    settings: str = ""
    poe: str = "0"
    bootmode: str = "1"
    edit_left: int | None = None
    edit_top: int | None = None
    _element: ET.Element | None = field(default=None, repr=False, compare=False)

    @property
    def spec(self) -> Model:
        return catalog.model_from_element(self._element) if self._element is not None else catalog.get_model(self.model)

    def to_element(self) -> ET.Element:
        left = self.edit_left if self.edit_left is not None else int(self.cx) + 27
        top = self.edit_top if self.edit_top is not None else int(self.cy) + 54
        dev = ET.Element(
            "dev",
            {
                "id": self.dev_id,
                "name": self.name,
                "poe": self.poe,
                "model": self.model,
                "settings": self.settings,
                "system_mac": self.system_mac,
                "com_port": str(self.com_port),
                "bootmode": self.bootmode,
                "cx": f"{float(self.cx):.6f}",
                "cy": f"{float(self.cy):.6f}",
                "edit_left": str(left),
                "edit_top": str(top),
            },
        )
        if self._element is not None:
            original = copy.deepcopy(self._element)
            original.attrib.update(dev.attrib)
            return original
        dev.append(catalog.build_slot_element(self.spec))
        return dev

    @classmethod
    def from_element(cls, element: ET.Element) -> "Device":
        return cls(
            name=element.get("name", ""),
            model=element.get("model", ""),
            cx=float(element.get("cx", "0") or 0),
            cy=float(element.get("cy", "0") or 0),
            dev_id=element.get("id", ""),
            com_port=int(element.get("com_port", "0") or 0),
            system_mac=element.get("system_mac", ""),
            settings=element.get("settings", ""),
            poe=element.get("poe", "0"),
            bootmode=element.get("bootmode", "1"),
            edit_left=_maybe_int(element.get("edit_left")),
            edit_top=_maybe_int(element.get("edit_top")),
            _element=copy.deepcopy(element),
        )


def _maybe_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


@dataclass
class Link:
    src_id: str
    dst_id: str
    src_index: int
    dst_index: int
    line_type: str = "Copper"
    _element: ET.Element | None = field(default=None, repr=False, compare=False)

    def to_element(self, devices: dict[str, Device]) -> ET.Element:
        if self.src_id not in devices or self.dst_id not in devices:
            if self._element is not None:
                return copy.deepcopy(self._element)
            raise TopologyError("链路指向不存在的设备，请先修复或删除该链路。")
        src = devices[self.src_id]
        dst = devices[self.dst_id]
        sx, sy, tx, ty = _edge_points(src.cx, src.cy, dst.cx, dst.cy)
        line = ET.Element(
            "line", {"srcDeviceID": self.src_id, "destDeviceID": self.dst_id}
        )
        ET.SubElement(
            line,
            "interfacePair",
            {
                "lineName": self.line_type,
                "srcIndex": str(self.src_index),
                "srcBoundRectIsMoved": "0",
                "srcBoundRect_X": f"{sx:.6f}",
                "srcBoundRect_Y": f"{sy:.6f}",
                "srcOffset_X": "0.000000",
                "srcOffset_Y": "0.000000",
                "tarIndex": str(self.dst_index),
                "tarBoundRectIsMoved": "0",
                "tarBoundRect_X": f"{tx:.6f}",
                "tarBoundRect_Y": f"{ty:.6f}",
                "tarOffset_X": "0.000000",
                "tarOffset_Y": "0.000000",
            },
        )
        if self._element is not None:
            original = copy.deepcopy(self._element)
            original.attrib.update(line.attrib)
            pair = original.find("interfacePair")
            generated = line.find("interfacePair")
            assert pair is not None and generated is not None
            for key, value in generated.attrib.items():
                endpoint_is_free = (
                    key.startswith("srcBoundRect_") and pair.get("srcBoundRectIsMoved", "0") == "0"
                ) or (
                    key.startswith("tarBoundRect_") and pair.get("tarBoundRectIsMoved", "0") == "0"
                )
                if key in ("lineName", "srcIndex", "tarIndex") or key not in pair.attrib or endpoint_is_free:
                    pair.set(key, value)
            return original
        return line


def _edge_points(
    src_cx: float, src_cy: float, dst_cx: float, dst_cy: float
) -> tuple[float, float, float, float]:
    """把连线端点从图标中心沿连线方向推到图标边缘。

    ``srcBoundRectIsMoved="0"`` 表示用户没有手动拖过接口标签，
    eNSP 打开时会自行重算，这里给出的只需是合理近似。
    """
    sx0, sy0 = src_cx + ICON_RADIUS, src_cy + ICON_RADIUS
    dx0, dy0 = dst_cx + ICON_RADIUS, dst_cy + ICON_RADIUS
    dx, dy = dx0 - sx0, dy0 - sy0
    dist = max(math.hypot(dx, dy), 1.0)
    ratio = ICON_RADIUS / dist
    return (
        sx0 + dx * ratio,
        sy0 + dy * ratio,
        dx0 - dx * ratio,
        dy0 - dy * ratio,
    )


class TopologyError(ValueError):
    """拓扑操作失败，消息里带上可行的取值。"""


class Topology:
    def __init__(self, root: ET.Element | None = None):
        self.root = root if root is not None else xmlio.new_root()
        self._source_path: str | None = None
        self._source_digest: str | None = None
        self.devices: list[Device] = [
            Device.from_element(e) for e in self.root.iterfind("./devices/dev")
        ]
        self.links: list[Link] = []
        for line in self.root.iterfind("./lines/line"):
            for pair in line.findall("interfacePair"):
                original = copy.deepcopy(line)
                for child in list(original):
                    if child.tag == "interfacePair":
                        original.remove(child)
                original.append(copy.deepcopy(pair))
                self.links.append(Link(
                    src_id=line.get("srcDeviceID", ""),
                    dst_id=line.get("destDeviceID", ""),
                    src_index=int(pair.get("srcIndex", "0") or 0),
                    dst_index=int(pair.get("tarIndex", "0") or 0),
                    line_type=pair.get("lineName", "Copper"),
                    _element=original,
                ))

    # -- 读写 ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "Topology":
        digest = _file_digest(path)
        topo = cls(xmlio.parse(path))
        topo._source_path = os.path.normcase(os.path.abspath(path))
        topo._source_digest = digest
        return topo

    def save(self, path: str) -> None:
        if self._source_path == os.path.normcase(os.path.abspath(path)):
            if not os.path.exists(path) or _file_digest(path) != self._source_digest:
                raise TopologyError("拓扑文件已被其他程序修改，请重新读取后再操作。")
        root = copy.deepcopy(self.root)
        for tag in ("devices", "lines", "shapes", "txttips"):
            if root.find(tag) is None:
                ET.SubElement(root, tag)
        devices = root.find("devices")
        lines = root.find("lines")
        assert devices is not None and lines is not None
        by_id = self.by_id()
        if len(by_id) != len(self.devices) or any(not d.dev_id for d in self.devices):
            raise TopologyError("设备 ID 为空或重复，无法安全保存。")
        devices[:] = [child for child in devices if child.tag != "dev"]
        lines[:] = [child for child in lines if child.tag != "line" or child.find("interfacePair") is None]
        for device in self.devices:
            devices.append(device.to_element())
        for link in self.links:
            lines.append(link.to_element(by_id))
        xmlio.write(path, root)
        self.root = root
        self._source_path = os.path.normcase(os.path.abspath(path))
        self._source_digest = _file_digest(path)

    # -- 查找 ---------------------------------------------------------------

    def by_id(self) -> dict[str, Device]:
        return {d.dev_id: d for d in self.devices}

    def find(self, name: str) -> Device:
        for device in self.devices:
            if device.name == name:
                return device
        for device in self.devices:
            if device.name.lower() == name.lower():
                return device
        raise TopologyError(
            f"拓扑里没有设备 {name!r}。现有设备："
            f"{', '.join(d.name for d in self.devices) or '（空）'}"
        )

    def has(self, name: str) -> bool:
        return any(d.name.lower() == name.lower() for d in self.devices)

    def used_ports(self, dev_id: str) -> set[int]:
        used: set[int] = set()
        for link in self.links:
            if link.src_id == dev_id:
                used.add(link.src_index)
            if link.dst_id == dev_id:
                used.add(link.dst_index)
        return used

    def free_ports(self, device: Device) -> list[Port]:
        used = self.used_ports(device.dev_id)
        if device.model in DYNAMIC_PORT_MODELS:
            return [
                _dynamic_port(i)
                for i in range(DYNAMIC_PORT_LIMIT)
                if i not in used
            ]
        return [p for p in device.spec.ports if p.index not in used]

    def resolve_port(self, device: Device, token: str | int | None) -> Port:
        """把接口名/序号解析成 Port，省略时自动挑下一个空闲口。"""
        spec = device.spec

        # Cloud 的接口是在 eNSP 界面里现加的，拓扑文件里看不到，按序号放行
        if device.model in DYNAMIC_PORT_MODELS:
            if token is None or token == "":
                free = self.free_ports(device)
                if not free:
                    raise TopologyError(f"{device.name} 的 Cloud 接口已全部占用。")
                return free[0]
            index = _dynamic_index(device, token)
            if not 0 <= index < DYNAMIC_PORT_LIMIT:
                raise TopologyError(
                    f"{device.name}（Cloud）的接口序号只能是 0~{DYNAMIC_PORT_LIMIT - 1}，"
                    f"收到 {index}。"
                )
            return _dynamic_port(index)

        if not spec.ports:
            raise TopologyError(
                f"{device.name}（{device.model}）没有有线接口，不能拉线，"
                f"这类设备只能在 eNSP 里和 AP 无线关联。"
            )

        if token is None or token == "":
            free = self.free_ports(device)
            if not free:
                raise TopologyError(
                    f"{device.name}（{device.model}）的 {spec.port_count} 个接口已全部占用。"
                )
            return free[0]

        if isinstance(token, int) or (isinstance(token, str) and token.isdigit()):
            index = int(token)
            if not 0 <= index < spec.port_count:
                raise TopologyError(
                    f"{device.name}（{device.model}）的接口序号只能是 0~{spec.port_count - 1}，"
                    f"收到 {index}。"
                )
            return spec.ports[index]

        port = spec.port_by_name(str(token))
        if port is None:
            available = ", ".join(p.name for p in spec.ports[:6])
            more = " …" if spec.port_count > 6 else ""
            raise TopologyError(
                f"{device.name}（{device.model}）没有接口 {token!r}。"
                f"可用接口：{available}{more}"
            )
        return port

    # -- 增删 ---------------------------------------------------------------

    def next_console_port(self) -> int:
        used = {d.com_port for d in self.devices if d.com_port >= CONSOLE_PORT_BASE}
        port = CONSOLE_PORT_BASE
        while port in used:
            port += 1
        return port

    def unique_name(self, base: str) -> str:
        if not self.has(base):
            return base
        stem = re.sub(r"\d+$", "", base) or base
        index = 1
        while self.has(f"{stem}{index}"):
            index += 1
        return f"{stem}{index}"

    def add_device(
        self,
        model_name: str,
        name: str | None = None,
        cx: float | None = None,
        cy: float | None = None,
        settings: dict | None = None,
        rng: random.Random | None = None,
    ) -> Device:
        spec = catalog.get_model(model_name)
        chosen = name or self.unique_name(_default_name(spec))
        if not isinstance(chosen, str) or not chosen.strip() or any(ord(c) < 32 for c in chosen):
            raise TopologyError("设备名必须是非空文本，不能包含控制字符。")
        for value in (cx, cy):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise TopologyError("设备坐标必须是有限数值。")
        if self.has(chosen):
            raise TopologyError(f"设备名 {chosen!r} 已存在，换一个名字或省略 name 让它自动编号。")

        mac = catalog.random_mac(spec.mac_oui, rng)
        if settings and settings.get("mac"):
            mac = catalog.normalise_mac(settings["mac"])
        device = Device(
            name=chosen,
            model=spec.name,
            cx=cx if cx is not None else 0.0,
            cy=cy if cy is not None else 0.0,
            dev_id=new_device_id(rng),
            com_port=self.next_console_port() if spec.has_console else 0,
            system_mac=mac,
            settings=catalog.render_settings(spec, mac, settings),
            bootmode="0" if spec.name == "Cloud" else "1",
        )
        self.devices.append(device)
        return device

    def remove_device(self, name: str) -> Device:
        device = self.find(name)
        self.devices.remove(device)
        self.links = [
            l for l in self.links if device.dev_id not in (l.src_id, l.dst_id)
        ]
        return device

    def connect(
        self,
        src_name: str,
        dst_name: str,
        src_port: str | int | None = None,
        dst_port: str | int | None = None,
        line_type: str | None = None,
    ) -> tuple[Link, Port, Port]:
        src = self.find(src_name)
        dst = self.find(dst_name)
        if src.dev_id == dst.dev_id:
            raise TopologyError(f"不能把 {src.name} 连到它自己。")

        a = self.resolve_port(src, src_port)
        b = self.resolve_port(dst, dst_port)

        for taken, device, port in ((self.used_ports(src.dev_id), src, a),
                                    (self.used_ports(dst.dev_id), dst, b)):
            if port.index in taken:
                raise TopologyError(
                    f"{device.name} 的 {port.name} 已经接了线，一个接口只能连一条。"
                )

        resolved_type = compatible_line_type(a, b, line_type)

        link = Link(src.dev_id, dst.dev_id, a.index, b.index, resolved_type)
        self.links.append(link)
        return link, a, b

    def disconnect(self, src_name: str, dst_name: str) -> int:
        src = self.find(src_name)
        dst = self.find(dst_name)
        pair = {src.dev_id, dst.dev_id}
        before = len(self.links)
        self.links = [l for l in self.links if {l.src_id, l.dst_id} != pair]
        return before - len(self.links)

    # -- 概览 ---------------------------------------------------------------

    def summary(self) -> dict:
        by_id = self.by_id()
        devices = []
        for device in self.devices:
            spec = device.spec
            used = self.used_ports(device.dev_id)
            devices.append(
                {
                    "name": device.name,
                    "model": device.model,
                    "category": spec.category,
                    "x": round(device.cx, 1),
                    "y": round(device.cy, 1),
                    "consolePort": device.com_port or None,
                    "systemMac": device.system_mac or None,
                    "portsTotal": spec.port_count,
                    "portsUsed": len(used),
                    "portsFree": [p.name for p in self.free_ports(device)],
                }
            )

        links = []
        for link in self.links:
            src = by_id.get(link.src_id)
            dst = by_id.get(link.dst_id)
            if src is None or dst is None:
                continue
            links.append(
                {
                    "from": src.name,
                    "fromPort": _port_name(src, link.src_index),
                    "to": dst.name,
                    "toPort": _port_name(dst, link.dst_index),
                    "lineType": link.line_type,
                }
            )

        counts: dict[str, int] = {}
        for device in self.devices:
            counts[device.model] = counts.get(device.model, 0) + 1

        return {
            "deviceCount": len(self.devices),
            "linkCount": len(links),
            "modelCounts": dict(sorted(counts.items())),
            "devices": devices,
            "links": links,
        }


def _dynamic_port(index: int) -> Port:
    """Cloud 的虚拟接口，编号跟着 eNSP 界面里的添加顺序走。"""
    return Port(
        index=index,
        name=f"Ethernet0/0/{index}",
        short=f"Eth0/0/{index}",
        kind="Ethernet",
        line_type="Copper",
    )


def _dynamic_index(device: Device, token: str | int) -> int:
    if isinstance(token, int):
        return token
    text = str(token).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    tail = re.fullmatch(r"(?:Ethernet|Eth)0/0/(\d+)", text, re.I)
    if tail is None:
        raise TopologyError(
            f"{device.name}（Cloud）的接口请用序号或 Ethernet0/0/N 的形式，收到 {token!r}。"
        )
    return int(tail.group(1))


def _file_digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def compatible_line_type(a: Port, b: Port, line_type: str | None = None) -> str:
    serial = {"Serial", "E1"}
    if (a.kind in serial) != (b.kind in serial):
        raise TopologyError(f"接口类型不兼容：{a.name} ({a.kind}) 与 {b.name} ({b.kind})。")
    if (a.kind == "POS") != (b.kind == "POS"):
        raise TopologyError("POS 接口只能连接 POS 接口。")
    default = "Serial" if a.kind in serial else "Fiber" if a.kind == "POS" else "Copper"
    chosen = line_type or default
    if chosen not in ("Copper", "Fiber", "Serial"):
        raise TopologyError("线型只能是 Copper / Fiber / Serial。")
    if (chosen == "Serial") != (a.kind in serial) or (a.kind == "POS" and chosen != "Fiber"):
        raise TopologyError(f"{chosen} 线型与接口类型不兼容。")
    return chosen


def port_of(device: Device, index: int) -> Port:
    """取设备第 index 个接口，越界时返回一个占位 Port 而不是抛错。"""
    if device.model in DYNAMIC_PORT_MODELS:
        return _dynamic_port(index)
    ports = device.spec.ports
    if 0 <= index < len(ports):
        return ports[index]
    return Port(index, f"#{index}", f"#{index}", "Unknown", "Copper")


def _port_name(device: Device, index: int) -> str:
    return port_of(device, index).name


def _default_name(spec: Model) -> str:
    """沿用 eNSP 自己的默认命名前缀，例如 S5700 叫 LSW1、NE40E 叫 NE1。"""
    name = spec.name
    if name.startswith("AR"):
        prefix = "AR"
    elif name.startswith("NE"):
        prefix = "NE"
    elif name.startswith("CE"):
        prefix = "CE"
    elif name.startswith("S") and spec.category == "LSW":
        prefix = "LSW"
    elif name.startswith("AC"):
        prefix = "AC"
    elif name.startswith("AD"):
        prefix = "AD"
    elif name.startswith("AP"):
        prefix = "AP"
    elif name == "R250D":
        prefix = "SAP"
    elif name == "Router":
        prefix = "R"
    elif spec.category == "FW":
        prefix = "FW"
    else:
        prefix = name
    return prefix + "1"
