"""eNSP 设备型号目录。

型号的接口在 .topo 里有两种写法：

``legacy``
    ``<slot number="slot17" isMainBoard="1">`` 下挂若干
    ``<interface sztype=".." interfacename=".." count="N"/>`` 分组。

``card``
    ``<slot id="1">`` 下挂逐个展开的
    ``<interface category=".." type=".." slotIndex=".." cardIndex=".."
    interfaceIndex=".."/>``。

本表对齐 eNSP V1.3.00.200T 的 ``res/items.xml`` 与 ``res/cards.xml``，
并用一份含全部机型的参考拓扑逐项核对过实际写出的 XML。
"""

from __future__ import annotations

import random
import re
import ipaddress
from dataclasses import dataclass, replace
from xml.etree import ElementTree as ET

# 接口种类 -> (完整名前缀, 简称前缀, .topo 里的 sztype, 默认线型)
_KINDS = {
    "GE": ("GigabitEthernet", "GE", "Ethernet", "Copper"),
    "Ethernet": ("Ethernet", "Eth", "Ethernet", "Copper"),
    "Serial": ("Serial", "S", "Serial", "Serial"),
    "POS": ("Pos", "Pos", "Ethernet", "Fiber"),
    "E1": ("E1", "E1", "Serial", "Serial"),
}

CATEGORY_LABELS = {
    "AR": "路由器",
    "LSW": "交换机",
    "WLAN": "无线局域网",
    "FW": "防火墙",
    "CLIENT": "终端",
    "OTHER": "其它设备",
}


@dataclass(frozen=True)
class Port:
    """设备上的一个接口。

    ``index`` 是 .topo 的 ``<interfacePair>`` 里 ``srcIndex`` / ``tarIndex``
    实际使用的扁平序号，跨接口种类连续编号。
    """

    index: int
    name: str
    short: str
    kind: str
    line_type: str


@dataclass(frozen=True)
class Model:
    name: str
    category: str
    slot_format: str
    ports: tuple[Port, ...]
    mac_oui: str | None
    has_console: bool
    settings_kind: str | None
    # legacy: [(kind, count)]；card: [(category, type, slotIndex, cardIndex, count)]
    groups: tuple = ()
    cards: tuple = ()
    note: str = ""

    @property
    def port_count(self) -> int:
        return len(self.ports)

    def port_by_name(self, token: str) -> Port | None:
        """按接口名查找，接受全名、简称，以及 ``GE0/0/1`` 这类混写。"""
        want = _normalise_port_token(token)
        for port in self.ports:
            if _normalise_port_token(port.name) == want:
                return port
        return None

    def describe(self) -> dict:
        return {
            "model": self.name,
            "category": self.category,
            "categoryLabel": CATEGORY_LABELS.get(self.category, self.category),
            "portCount": self.port_count,
            "hasConsole": self.has_console,
            "slotFormat": self.slot_format,
            "ports": [
                {"index": p.index, "name": p.name, "short": p.short, "kind": p.kind}
                for p in self.ports
            ],
            "note": self.note,
        }


def _normalise_port_token(token: str) -> str:
    """把接口名压成可比较的形式：``GE0/0/1`` / ``gi0/0/1`` -> ``ge0/0/1``。"""
    text = token.strip().lower().replace(" ", "")
    text = re.sub(r"^gigabitethernet", "ge", text)
    text = re.sub(r"^gigabit", "ge", text)
    text = re.sub(r"^gi(?=\d)", "ge", text)
    text = re.sub(r"^g(?=\d)", "ge", text)
    text = re.sub(r"^ethernet", "eth", text)
    text = re.sub(r"^e(?=\d)", "eth", text)
    text = re.sub(r"^serial", "s", text)
    return text


# ---------------------------------------------------------------------------
# 型号定义
# ---------------------------------------------------------------------------

# name, category, [(kind, count)], 接口起始编号, MAC OUI, 有无 console, settings 类型
_LEGACY_SPEC: list[tuple] = [
    ("AR201", "AR", [("Ethernet", 8), ("Ethernet", 1)], 0, "00-E0-FC", True, None),
    ("AR1220", "AR", [("GE", 2), ("Ethernet", 8)], 0, "00-E0-FC", True, None),
    ("AR2220", "AR", [("GE", 1), ("GE", 2)], 0, "00-E0-FC", True, None),
    ("AR2240", "AR", [("GE", 1), ("GE", 2)], 0, "00-E0-FC", True, None),
    ("AR3260", "AR", [("GE", 1), ("GE", 2)], 0, "00-E0-FC", True, None),
    ("Router", "AR", [("Ethernet", 2), ("GE", 4), ("Serial", 4)], 0, "54-89-98", True, None),
    ("S5700", "LSW", [("GE", 24)], 1, "4C-1F-CC", True, None),
    ("S3700", "LSW", [("Ethernet", 22), ("GE", 2)], 1, "4C-1F-CC", True, None),
    ("AC6005", "WLAN", [("GE", 8)], 1, "00-E0-FC", True, None),
    ("AC6605", "WLAN", [("GE", 24)], 1, "00-E0-FC", True, None),
    ("AP2010", "WLAN", [("GE", 3)], 0, "00-E0-FC", True, "ap"),
    ("AP2030", "WLAN", [("GE", 5)], 0, "00-E0-FC", True, "ap"),
    ("AP2050", "WLAN", [("GE", 5)], 0, "00-E0-FC", True, "ap"),
    ("AP3030", "WLAN", [("GE", 1)], 0, "00-E0-FC", True, "ap"),
    ("AP4030", "WLAN", [("GE", 2)], 0, "00-E0-FC", True, "ap"),
    ("AP4050", "WLAN", [("GE", 2)], 0, "00-E0-FC", True, "ap"),
    ("AP5010", "WLAN", [("GE", 1)], 0, "00-E0-FC", True, "ap"),
    ("AP5030", "WLAN", [("GE", 2)], 0, "00-E0-FC", True, "ap"),
    ("AP6010", "WLAN", [("GE", 1)], 0, "00-E0-FC", True, "ap"),
    ("AP6050", "WLAN", [("GE", 2)], 0, "00-E0-FC", True, "ap"),
    ("AP6510", "WLAN", [("GE", 1)], 0, "00-E0-FC", True, "ap"),
    ("AP6610", "WLAN", [("GE", 1)], 0, "00-E0-FC", True, "ap"),
    ("AP7030", "WLAN", [("GE", 2)], 0, "00-E0-FC", True, "ap"),
    ("AP7050", "WLAN", [("GE", 2)], 0, "00-E0-FC", True, "ap"),
    ("AP8030", "WLAN", [("GE", 3)], 0, "00-E0-FC", True, "ap"),
    ("AP8130", "WLAN", [("GE", 3)], 0, "00-E0-FC", True, "ap"),
    ("AP9131", "WLAN", [("GE", 2)], 0, "00-E0-FC", True, "ap"),
    ("AD9430", "WLAN", [("GE", 28)], 0, "00-E0-FC", True, None),
    ("R250D", "WLAN", [("GE", 1)], 0, "00-E0-FC", True, "ap"),
    ("USG5500", "FW", [("GE", 9)], 0, None, True, None),
    ("PC", "CLIENT", [("Ethernet", 1)], 0, "54-89-98", False, "pc"),
    ("MCS", "CLIENT", [("Ethernet", 1)], 0, "54-89-98", False, "mcs"),
    ("Client", "CLIENT", [("Ethernet", 1)], 0, "54-89-98", False, "host"),
    ("Server", "CLIENT", [("Ethernet", 1)], 0, "54-89-98", False, "host"),
    ("STA", "CLIENT", [], 0, "54-89-98", False, "sta"),
    ("Cellphone", "CLIENT", [], 0, "54-89-98", False, "sta"),
    ("Cloud", "OTHER", [("Ethernet", 0), ("GE", 0), ("Serial", 0)], 0, None, False, None),
    ("FRSW", "OTHER", [("Serial", 16)], 1, None, False, None),
    ("HUB", "OTHER", [("Ethernet", 16)], 1, None, False, None),
]

# name, category, [(category, type, slotIndex, cardIndex, count)], MAC OUI, 有无 console
_CARD_SPEC: list[tuple] = [
    ("NE40E", "AR", [("Ethernet", "Ethernet", 1, 0, 10)], "70-7B-E8", True),
    ("CE6800", "LSW", [("Ethernet", "GE", 1, 0, 20)], "70-7B-E8", True),
    ("CE12800", "LSW", [("Ethernet", "GE", 1, 0, 10)], "70-7B-E8", True),
    ("USG6000V", "FW", [("Ethernet", "GE", 0, 0, 1), ("Ethernet", "GE", 1, 0, 7)], "00-E0-FC", True),
]

_NOTES = {
    "Router": "eNSP 已不再维护，建议改用 AR 系列。",
    "USG5500": "eNSP 防火墙实验的兼容性优先默认型号；没有明确的 USG6000V 要求时优先使用。",
    "STA": "无线终端，只能关联 AP，不能拉线。",
    "Cellphone": "无线终端，只能关联 AP，不能拉线。",
    "Cloud": "接口在 eNSP 界面里动态添加并绑定真实网卡，拓扑文件中初始为 0 个。",
    "AD9430": "0~23 口为下行口，24~27 口为上行口。",
    "USG6000V": "通常需要先安装并注册对应设备镜像，且依赖兼容的 eNSP / VirtualBox 环境；首次登录需要 admin / Admin@123 并强制改密码。",
}


# 每个类别在没有明确型号要求时的默认选择。这里只声明已经确认过的策略，
# 不为其它类别猜测默认型号。
CATEGORY_DEFAULTS: dict[str, str] = {
    "FW": "USG5500",
}


# 运行条件是目录元数据，不放进 Model 字段，避免改变既有 Model 的位置参数、
# 相等比较和调用方序列化行为。
_MODEL_ADVISORIES: dict[str, dict] = {
    "USG5500": {
        "selectionTier": "preferred",
        "runtimeType": "local_simulator",
        "externalImageRequired": False,
        "startupRisk": "normal",
        "fallbackModel": None,
        "recommendedAlternative": None,
        "runtimePrerequisites": [],
        "useWhen": "没有明确要求 USG6000V 或其独有能力的普通 eNSP 防火墙实验。",
    },
    "USG6000V": {
        "selectionTier": "conditional",
        "runtimeType": "ngfw_image",
        "externalImageRequired": True,
        "startupRisk": "high",
        "fallbackModel": "USG5500",
        "recommendedAlternative": "USG5500",
        "runtimePrerequisites": [
            "已安装并注册与当前 eNSP 版本匹配的 USG6000V 设备镜像",
            "eNSP 与 VirtualBox 版本及虚拟网络环境兼容",
        ],
        "useWhen": "明确要求 USG6000V，或实验依赖 USG6000V 才具备的能力。",
    },
}

for _name in ("AR201", "AR1220", "AR2220", "AR2240", "AR3260"):
    _MODEL_ADVISORIES[_name] = {
        "selectionTier": "standard",
        "runtimeType": "virtualbox_ar",
        "externalImageRequired": False,
        "startupRisk": "environment-dependent",
        "runtimePrerequisites": [
            "兼容的 VirtualBox 5.2.x 能取得硬件虚拟化能力",
            "AR_Base 基机、基础磁盘和 Link 快照完整",
            "VirtualBox Host-Only 网卡可用",
        ],
        "useWhen": "实验需要华为 AR 系列路由器，并已通过 environment_check。",
    }

for _name in (
    "AC6005", "AC6605", "AP2010", "AP2030", "AP2050", "AP3030", "AP4030",
    "AP4050", "AP5010", "AP5030", "AP6010", "AP6050", "AP6510", "AP6610",
    "AP7030", "AP7050", "AP8030", "AP8130", "AP9131", "AD9430", "R250D",
):
    _MODEL_ADVISORIES[_name] = {
        "selectionTier": "standard",
        "runtimeType": "virtualbox_wlan",
        "externalImageRequired": False,
        "startupRisk": "environment-dependent",
        "runtimePrerequisites": [
            "兼容的 VirtualBox 5.2.x 能取得硬件虚拟化能力",
            "对应 WLAN 基机、基础磁盘和 Link 快照完整",
            "VirtualBox Host-Only 网卡可用",
        ],
        "useWhen": "无线实验需要该型号，并已通过 environment_check。",
    }

for _name in ("NE40E", "CE6800", "CE12800"):
    _MODEL_ADVISORIES[_name] = {
        "selectionTier": "conditional",
        "runtimeType": "svrp_image",
        "externalImageRequired": True,
        "startupRisk": "high",
        "runtimePrerequisites": [
            "已安装与当前 eNSP 版本匹配的 SVRP 镜像 svrpbox.img",
            "兼容的 VirtualBox 5.2.x 能取得硬件虚拟化能力",
        ],
        "useWhen": "实验明确要求 NE40E/CE 数据中心型号且 SVRP 环境已通过预检。",
    }

_MODEL_ADVISORIES["Router"] = {
    "selectionTier": "conditional",
    "runtimeType": "legacy_unknown",
    "externalImageRequired": None,
    "startupRisk": "high",
    "fallbackModel": "AR2220",
    "recommendedAlternative": "AR2220",
    "runtimePrerequisites": ["该旧型号已停止维护，必须在目标 eNSP 环境中真实启动验证"],
    "useWhen": "仅用于必须复现旧 Router 型号的历史实验。",
}


def _build_legacy(spec: tuple) -> Model:
    name, category, groups, base, oui, console, settings_kind = spec
    ports: list[Port] = []
    counters: dict[str, int] = {}
    for kind, count in groups:
        full, short, _sztype, line_type = _KINDS[kind]
        for _ in range(count):
            seq = counters.get(kind, base)
            counters[kind] = seq + 1
            ports.append(
                Port(
                    index=len(ports),
                    name=f"{full}0/0/{seq}",
                    short=f"{short}0/0/{seq}",
                    kind=kind,
                    line_type=line_type,
                )
            )
    return Model(
        name=name,
        category=category,
        slot_format="legacy",
        ports=tuple(ports),
        mac_oui=oui,
        has_console=console,
        settings_kind=settings_kind,
        groups=tuple(groups),
        note=_NOTES.get(name, ""),
    )


def _build_card(spec: tuple) -> Model:
    name, category, cards, oui, console = spec
    ports: list[Port] = []
    for _cat, kind, slot_index, card_index, count in cards:
        full, short, _sztype, line_type = _KINDS[kind]
        for iface_index in range(count):
            ports.append(
                Port(
                    index=len(ports),
                    name=f"{full}{slot_index}/{card_index}/{iface_index}",
                    short=f"{short}{slot_index}/{card_index}/{iface_index}",
                    kind=kind,
                    line_type=line_type,
                )
            )
    return Model(
        name=name,
        category=category,
        slot_format="card",
        ports=tuple(ports),
        mac_oui=oui,
        has_console=console,
        settings_kind=None,
        cards=tuple(cards),
        note=_NOTES.get(name, ""),
    )


MODELS: dict[str, Model] = {}
for _spec in _LEGACY_SPEC:
    _m = _build_legacy(_spec)
    MODELS[_m.name] = _m
for _spec in _CARD_SPEC:
    _m = _build_card(_spec)
    MODELS[_m.name] = _m


def get_model(name: str) -> Model:
    """按型号取定义，大小写不敏感；找不到时给出可用型号提示。"""
    if not isinstance(name, str):
        raise ValueError("型号必须是文本。")
    if name in MODELS:
        return MODELS[name]
    lowered = {k.lower(): k for k in MODELS}
    if name.lower() in lowered:
        return MODELS[lowered[name.lower()]]
    raise KeyError(
        f"未知型号 {name!r}。可用型号：{', '.join(sorted(MODELS))}"
    )


def advisory(name: str) -> dict:
    """返回型号选择和启动条件提示。

    返回值只增加目录能力，不改变 :class:`Model` 的字段或 ``describe`` 结果。
    未配置专项策略的型号使用保守的 ``standard`` / ``unknown`` 值，避免把
    离线建图能力误报成真实 eNSP 启动兼容性。每次返回独立对象，调用方可安全
    地补充展示字段而不会改坏全局目录。
    """
    model = get_model(name)
    default_model = CATEGORY_DEFAULTS.get(model.category)
    specific = _MODEL_ADVISORIES.get(model.name, {})
    fallback = specific.get("fallbackModel")
    return {
        "model": model.name,
        "category": model.category,
        "defaultForCategory": default_model == model.name,
        "categoryDefault": default_model,
        "selectionTier": specific.get(
            "selectionTier", "preferred" if default_model == model.name else "standard"
        ),
        "runtimeType": specific.get("runtimeType", "unknown"),
        "externalImageRequired": specific.get("externalImageRequired"),
        "startupRisk": specific.get("startupRisk", "unknown"),
        "fallbackModel": fallback,
        "recommendedAlternative": specific.get("recommendedAlternative", fallback),
        "runtimePrerequisites": list(specific.get("runtimePrerequisites", ())),
        "useWhen": specific.get("useWhen", ""),
    }


_SELECTION_TIER_ORDER = {
    "preferred": 0,
    "standard": 1,
    "conditional": 2,
}


def model_sort_key(model: Model | str) -> tuple[str, int, str]:
    """供 ``sorted`` 使用：同类别先列默认首选，再列普通和条件型号。"""
    spec = get_model(model) if isinstance(model, str) else model
    if not isinstance(spec, Model):
        raise TypeError("model 必须是型号名称或 Model。")
    policy = advisory(spec.name)
    tier = _SELECTION_TIER_ORDER.get(policy["selectionTier"], 1)
    return spec.category, tier, spec.name.casefold()


def sort_models(models=None) -> list[Model]:
    """按推荐顺序返回型号；省略 ``models`` 时排序整个内置目录。"""
    source = MODELS.values() if models is None else models
    resolved = [get_model(item) if isinstance(item, str) else item for item in source]
    if any(not isinstance(item, Model) for item in resolved):
        raise TypeError("models 中每一项都必须是型号名称或 Model。")
    return sorted(resolved, key=model_sort_key)


def model_from_element(element: ET.Element) -> Model:
    """已存在设备的实际接口来自 XML；内置表只提供类别和命名默认值。"""
    name = element.get("model", "")
    try:
        base = get_model(name)
    except KeyError:
        base = Model(name, "OTHER", "legacy", (), None, bool(int(element.get("com_port", "0") or 0)), None)
    slots = element.findall("slot")
    if not slots:
        return base
    ports: list[Port] = []
    counters: dict[tuple[str, int], int] = {}
    start = 1 if base.name in ("S5700", "S3700", "AC6005", "AC6605", "FRSW", "HUB") else 0
    for slot_number, slot in enumerate(slots):
        for iface in slot.findall("interface"):
            kind = iface.get("type", iface.get("interfacename", "Ethernet"))
            kind = {"GigabitEthernet": "GE", "Pos": "POS"}.get(kind, kind)
            if kind not in _KINDS:
                raise ValueError(f"{name} 含不支持的接口类型 {kind!r}；原文件未修改。")
            full, short, _sztype, cable = _KINDS[kind]
            if "interfaceIndex" in iface.attrib:
                indices = (int(iface.get("slotIndex", "0")), int(iface.get("cardIndex", "0")), int(iface.get("interfaceIndex", "0")))
                if min(indices) < 0:
                    raise ValueError("接口编号不能为负数。")
                suffix = "/".join(map(str, indices))
                ports.append(Port(len(ports), full + suffix, short + suffix, kind, cable))
            else:
                count = int(iface.get("count", "0"))
                if not 0 <= count <= 4096:
                    raise ValueError("XML 接口数量必须在 0–4096 之间。")
                for _ in range(count):
                    key = (kind, slot_number)
                    index = counters.get(key, start)
                    counters[key] = index + 1
                    suffix = f"0/{slot_number}/{index}"
                    ports.append(Port(len(ports), full + suffix, short + suffix, kind, cable))
    return replace(base, ports=tuple(ports))


def normalise_mac(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("MAC 必须是文本。")
    compact = re.sub(r"[-:.]", "", value)
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        raise ValueError("MAC 必须包含 12 位十六进制数字。")
    return "-".join(compact[i:i + 2].upper() for i in range(0, 12, 2))


# ---------------------------------------------------------------------------
# settings 串
# ---------------------------------------------------------------------------

_PC_SETTINGS = (
    " -simpc_ip {ip}  -simpc_mask {mask}  -simpc_gateway {gateway} "
    " -simpc_mac {mac}  -simpc_mc_dstip 0.0.0.0 "
    " -simpc_mc_dstmac 00-00-00-00-00-00  -simpc_dns1 {dns1}  -simpc_dns2 {dns2} "
    " -simpc_ipv6 ::  -simpc_prefix 128  -simpc_gatewayv6 :: "
    " -simpc_dhcp_state 0  -simpc_dhcpv6_state 0  -simpc_dns_auto_state 0 "
    " -simpc_igmp_version 1  -simpc_group_ip_start 0.0.0.0 "
    " -simpc_src_ip_start 0.0.0.0  -simpc_group_num 0  -simpc_group_step 0 "
    " -simpc_src_num 0  -simpc_src_step 0  -simpc_type MODE_IS_INCLUDE "
)

_MCS_SETTINGS = (
    " -simpc_ip {ip}  -simpc_mask {mask}  -simpc_gateway {gateway} "
    " -simpc_mac {mac}  -simpc_mc_dstip 0.0.0.0 "
    " -simpc_mc_dstmac 00-00-00-00-00-00 "
)

_HOST_SETTINGS = (
    "-domain 0 -eth {mac} -ipaddr {ip} -ipmask {mask} -gateway {gateway} "
    "-ipv6addr 2000::2 -ipv6gateway 2000::1 -prefixlen 64 -ipv4dns {dns1} "
    "-ipv6dns 3000::1 -dnslist NULL"
)

_STA_SETTINGS = (
    " -simsta_ip {ip}  -simsta_mask {mask}  -simsta_gateway {gateway} "
    " -simsta_mac {mac}  -simsta_check 0 "
)

_AP_SETTINGS = " -apMac {mac} -apSN {sn}"

# 各 settings 类型接受的可调字段，供 list_models 自描述
SETTINGS_FIELDS = {
    "pc": ("ip", "mask", "gateway", "dns1", "dns2", "mac"),
    "mcs": ("ip", "mask", "gateway", "mac"),
    "host": ("ip", "mask", "gateway", "dns1", "mac"),
    "sta": ("ip", "mask", "gateway", "mac"),
    "ap": ("mac", "sn"),
}


def render_settings(model: Model, mac: str, overrides: dict | None = None) -> str:
    """生成 ``<dev settings="...">`` 的内容。"""
    kind = model.settings_kind
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("settings 必须是对象。")
    allowed = set(SETTINGS_FIELDS.get(kind, ()))
    unknown = set(overrides or {}) - allowed
    if unknown:
        raise ValueError(f"{model.name} 不支持 settings 字段：{', '.join(sorted(unknown))}")
    if kind is None:
        return ""

    values = {
        "mac": mac,
        "ip": "0.0.0.0",
        "mask": "0.0.0.0" if kind in ("pc", "mcs", "sta") else "255.255.255.0",
        "gateway": "0.0.0.0",
        "dns1": "0.0.0.0",
        "dns2": "0.0.0.0",
        "sn": random_ap_sn(),
    }
    for key, value in (overrides or {}).items():
        if key in values and value is not None:
            values[key] = str(value)
    for key in ("ip", "gateway", "dns1", "dns2"):
        ipaddress.IPv4Address(values[key])
    ipaddress.IPv4Network("0.0.0.0/" + values["mask"])
    values["mac"] = normalise_mac(values["mac"])
    if kind == "ap" and not re.fullmatch(r"[A-Za-z0-9]{20}", values["sn"]):
        raise ValueError("AP 序列号必须是 20 位字母或数字。")

    template = {
        "pc": _PC_SETTINGS,
        "mcs": _MCS_SETTINGS,
        "host": _HOST_SETTINGS,
        "sta": _STA_SETTINGS,
        "ap": _AP_SETTINGS,
    }[kind]
    return template.format(**values)


def random_mac(oui: str | None, rng: random.Random | None = None) -> str:
    """按厂商 OUI 生成 ``XX-XX-XX-XX-XX-XX`` 形式的 MAC。"""
    if not oui:
        return ""
    r = rng or random
    tail = "-".join(f"{r.randint(0, 255):02X}" for _ in range(3))
    return f"{oui}-{tail}"


def random_ap_sn(rng: random.Random | None = None) -> str:
    """AP 的 20 位序列号：固定前缀 ``210235448310`` + 8 位十六进制。"""
    r = rng or random
    return "210235448310" + "".join(r.choice("0123456789ABCDEF") for _ in range(8))


# ---------------------------------------------------------------------------
# slot XML
# ---------------------------------------------------------------------------


def build_slot_element(model: Model) -> ET.Element:
    """生成 ``<dev>`` 下的 ``<slot>`` 子树。"""
    if model.slot_format == "card":
        slot = ET.Element("slot", {"id": "1"})
        for cat, kind, slot_index, card_index, count in model.cards:
            for iface_index in range(count):
                ET.SubElement(
                    slot,
                    "interface",
                    {
                        "category": cat,
                        "type": kind,
                        "slotIndex": str(slot_index),
                        "cardIndex": str(card_index),
                        "interfaceIndex": str(iface_index),
                    },
                )
        return slot

    slot = ET.Element("slot", {"number": "slot17", "isMainBoard": "1"})
    for kind, count in model.groups:
        _full, _short, sztype, _line = _KINDS[kind]
        ET.SubElement(
            slot,
            "interface",
            {
                "sztype": sztype,
                "interfacename": "GE" if kind == "GE" else kind,
                "count": str(count),
            },
        )
    return slot


def learn_models_from_topo(path: str) -> dict[str, dict]:
    """从一份现成 .topo 里读出每种型号实际写出的接口布局。

    用来核对内置表，或在 eNSP 版本不同时发现差异。
    """
    from . import xmlio

    root = xmlio.parse(path)
    found: dict[str, dict] = {}
    for dev in root.iterfind("./devices/dev"):
        model = dev.get("model", "")
        actual = model_from_element(dev)
        signature = [{"name": p.name, "kind": p.kind} for p in actual.ports]
        if model in found:
            if signature not in found[model]["interfaceVariants"]:
                found[model]["interfaceVariants"].append(signature)
            continue
        slot = dev.find("slot")
        entry: dict = {"model": model, "systemMac": dev.get("system_mac", "")}
        if slot is None:
            entry["slotFormat"] = "none"
            entry["ports"] = 0
        elif slot.get("id") is not None:
            entry["slotFormat"] = "card"
            entry["ports"] = len(slot.findall("interface"))
            entry["cards"] = [
                {
                    "category": i.get("category"),
                    "type": i.get("type"),
                    "slotIndex": int(i.get("slotIndex", 0)),
                    "cardIndex": int(i.get("cardIndex", 0)),
                    "interfaceIndex": int(i.get("interfaceIndex", 0)),
                }
                for i in slot.findall("interface")
            ]
        else:
            entry["slotFormat"] = "legacy"
            groups = [
                (i.get("interfacename"), int(i.get("count", 0)))
                for i in slot.findall("interface")
            ]
            entry["groups"] = [{"kind": k, "count": c} for k, c in groups]
            entry["ports"] = sum(c for _k, c in groups)
        found[model] = entry
        entry["ports"] = actual.port_count
        entry["interfaceVariants"] = [signature]
    return found


def diff_against_builtin(learned: dict[str, dict]) -> list[str]:
    """把 learn_models_from_topo 的结果与内置表比对，返回差异描述。"""
    issues: list[str] = []
    for model_name, entry in sorted(learned.items()):
        try:
            model = get_model(model_name)
        except KeyError:
            issues.append(f"{model_name}: 内置表中没有该型号")
            continue
        if entry["ports"] != model.port_count:
            issues.append(
                f"{model_name}: 接口数不一致，拓扑里是 {entry['ports']}，"
                f"内置表是 {model.port_count}"
            )
        if entry["slotFormat"] != model.slot_format and entry["slotFormat"] != "none":
            issues.append(
                f"{model_name}: slot 写法不一致，拓扑里是 {entry['slotFormat']}，"
                f"内置表是 {model.slot_format}"
            )
        expected = [{"name": p.name, "kind": p.kind} for p in model.ports]
        if any(variant != expected for variant in entry.get("interfaceVariants", [])):
            issues.append(f"{model_name}: 实际接口编号或类型与内置表不同，编辑时采用 XML 中的实际接口")
    return issues
