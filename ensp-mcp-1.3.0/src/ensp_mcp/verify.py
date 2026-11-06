"""实验自动验收：把“配置下发完到底对不对”变成可判定的检查项。

一条检查项 = 在某台设备上跑一条命令 + 对回显的判定规则。
判定规则可以组合，全部满足才算 PASS。

设计上刻意不内置“什么算配置正确”的硬编码知识：规则全部来自显式的
``expect``，套件只是常见实验的规则模板。这样同一个工具既能验收内置的
双机热备 / BFD / OSPF，也能验收用户自己定义的任意检查。
"""

from __future__ import annotations

import re
from typing import Any

from . import console

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
ERROR = "ERROR"

CATEGORY_NAMES = {
    "AR": "路由器",
    "LSW": "交换机",
    "FW": "防火墙",
    "WLAN": "无线",
    "CLIENT": "终端",
    "OTHER": "其它",
}

# 回显里出现这些就说明命令本身没执行成功，不必再看 expect
_ERROR_RE = re.compile(r"^\s*(?:Error:|Wrong parameter|Unrecognized command|Incomplete command|Failure:)", re.M)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _search(pattern: str, text: str) -> re.Match[str] | None:
    try:
        return re.search(pattern, text, re.M | re.I | re.S)
    except re.error as exc:
        raise ValueError(f"正则 {pattern!r} 无效：{exc}") from exc


def evaluate(expect: dict | None, output: str, *, command: str = "") -> tuple[bool, list[str]]:
    """按 expect 判定一次命令回显，返回（是否通过，未满足的条件说明）。

    expect 支持的键（可组合）：

    - ``contains`` / ``notContains``：字符串或字符串列表
    - ``regex`` / ``notRegex``：正则源码
    - ``minCount``：``{"pattern": 正则, "atLeast": 次数}``
    - ``reachable``：布尔，按 ping 统计判断连通性
    - ``maxLoss``：丢包率上限（百分比）

    expect 为空表示“只要命令跑通就算通过”。
    """
    expect = expect or {}
    if not isinstance(expect, dict):
        raise ValueError("expect 必须是对象。")
    allowed = {"contains", "notContains", "regex", "notRegex", "minCount", "reachable", "maxLoss"}
    unknown = set(expect) - allowed
    if unknown:
        raise ValueError(f"expect 里不认识的字段：{', '.join(sorted(unknown))}。可用：{', '.join(sorted(allowed))}")

    text = output or ""
    problems: list[str] = []

    error = console._error_in(text)
    if error:
        return False, [f"命令执行报错：{error}"]

    for needle in _as_list(expect.get("contains")):
        if needle not in text:
            problems.append(f"回显里找不到「{needle}」")
    for needle in _as_list(expect.get("notContains")):
        if needle in text:
            problems.append(f"回显里出现了不该有的「{needle}」")

    pattern = expect.get("regex")
    if pattern and not _search(str(pattern), text):
        problems.append(f"没有匹配到正则 /{pattern}/")
    banned = expect.get("notRegex")
    if banned and _search(str(banned), text):
        problems.append(f"匹配到了不该出现的 /{banned}/")

    min_count = expect.get("minCount")
    if min_count is not None:
        if not isinstance(min_count, dict) or not min_count.get("pattern"):
            raise ValueError("minCount 必须是 {\"pattern\": 正则, \"atLeast\": 次数} 形式。")
        at_least = min_count.get("atLeast", 1)
        if isinstance(at_least, bool) or not isinstance(at_least, int) or at_least < 1:
            raise ValueError("minCount.atLeast 必须是正整数。")
        found = len(_search_all(str(min_count["pattern"]), text))
        if found < at_least:
            problems.append(f"/{min_count['pattern']}/ 出现 {found} 次，少于要求的 {at_least} 次")

    if "reachable" in expect:
        parsed = console.parse_ping(text)
        want = bool(expect["reachable"])
        actual = parsed.get("reachable")
        if actual is None:
            problems.append("ping 回显无法解析出连通性")
        elif bool(actual) != want:
            problems.append(f"ping 实际{'通' if actual else '不通'}，期望{'通' if want else '不通'}")

    if "maxLoss" in expect:
        parsed = console.parse_ping(text)
        loss = parsed.get("lossPercent")
        try:
            limit = float(expect["maxLoss"])
        except (TypeError, ValueError) as exc:
            raise ValueError("maxLoss 必须是数字（百分比）。") from exc
        if loss is None:
            problems.append("ping 回显无法解析出丢包率")
        elif loss > limit:
            problems.append(f"丢包率 {loss}% 超过上限 {limit}%")

    return (not problems), problems


def _search_all(pattern: str, text: str) -> list[str]:
    try:
        return re.findall(pattern, text, re.M | re.I | re.S)
    except re.error as exc:
        raise ValueError(f"正则 {pattern!r} 无效：{exc}") from exc


# ---------------------------------------------------------------------------
# 内置套件
# ---------------------------------------------------------------------------

SUITES: dict[str, dict[str, Any]] = {
    "reachable": {
        "label": "所有设备 console 可登录",
        "description": "逐台登录并执行一条无害命令，只验证能连上、能进用户视图。",
        "checks": [
            {"name": "console 可登录", "target": "all", "command": "display clock", "expect": {}},
        ],
    },
    "fw_ha": {
        "label": "防火墙双机热备（HRP + VRRP）",
        "description": "验收 HRP 主备角色、心跳口绑定和 VRRP 组状态，适用于双机热备/负载分担实验。",
        "checks": [
            {
                "name": "HRP 已启用且有主备角色",
                "target": "FW",
                "command": "display hrp state",
                "expect": {"regex": r"Role\s*[:：]\s*(?:active|standby)"},
            },
            {
                "name": "HRP 心跳口已绑定",
                "target": "FW",
                "command": "display hrp interface",
                "expect": {
                    "notContains": ["not configured", "not enabled"],
                    "regex": r"(?:GigabitEthernet|Ethernet|Eth-Trunk|Vlanif)\d",
                },
            },
            {
                "name": "VRRP 组为主备状态",
                "target": "FW",
                "command": "display vrrp",
                "expect": {"minCount": {"pattern": r"State\s*[:：]\s*(?:Master|Backup)", "atLeast": 1}},
            },
        ],
    },
    "bfd": {
        "label": "BFD 会话 Up",
        "description": "验收 BFD 会话已建立且状态为 Up，适用于 BFD 联动/双机切换实验。",
        "checks": [
            {
                "name": "存在 Up 状态的 BFD 会话",
                "target": "all",
                "command": "display bfd session all",
                "expect": {
                    "notContains": ["No BFD session"],
                    "minCount": {"pattern": r"\bUp\b", "atLeast": 1},
                },
            },
        ],
    },
    "ospf": {
        "label": "OSPF 邻居 Full",
        "description": "验收 OSPF 邻居已建立并达到 Full，适用于路由协议实验。",
        "checks": [
            {
                "name": "OSPF 邻居达到 Full",
                "target": "all",
                "command": "display ospf peer brief",
                "expect": {
                    "notContains": ["No OSPF"],
                    "minCount": {"pattern": r"\bFull\b", "atLeast": 1},
                },
            },
        ],
    },
    "vrrp": {
        "label": "VRRP 主备",
        "description": "只验收 VRRP 组状态，不涉及 HRP。",
        "checks": [
            {
                "name": "VRRP 组为主备状态",
                "target": "all",
                "command": "display vrrp",
                "expect": {"minCount": {"pattern": r"State\s*[:：]\s*(?:Master|Backup)", "atLeast": 1}},
            },
        ],
    },
    "vlan": {
        "label": "VLAN 已创建",
        "description": "验收交换机上除默认 VLAN 1 之外还存在业务 VLAN。",
        "checks": [
            {
                "name": "存在业务 VLAN",
                "target": "LSW",
                "command": "display vlan",
                "expect": {"minCount": {"pattern": r"^\s*(?:VLAN ID|VLAN)\s*[:：]?\s*\d+", "atLeast": 1}},
            },
        ],
    },
}


def suite_names() -> list[str]:
    return sorted(SUITES)


def resolve_suite(name: str) -> list[dict]:
    """取内置套件的检查项，未知套件名给出可用列表。"""
    key = str(name).strip().lower()
    if key not in SUITES:
        raise ValueError(f"没有叫 {name!r} 的套件。可用：{', '.join(suite_names())}")
    return [dict(check) for check in SUITES[key]["checks"]]


def _targets(target: str, devices: list[dict]) -> list[dict]:
    """把 target 解析成具体设备列表。target 可以是设备名、设备类别或 all。"""
    text = str(target or "").strip()
    connectable = [device for device in devices if device.get("consolePort")]
    if not text or text.lower() == "all":
        return connectable
    exact = [device for device in connectable if device.get("name") == text]
    if exact:
        return exact
    category = text.upper()
    if category in CATEGORY_NAMES:
        return [device for device in connectable if str(device.get("category", "")).upper() == category]
    known = ", ".join(device["name"] for device in devices) or "（空拓扑）"
    raise ValueError(f"{text!r} 既不是拓扑里的设备名，也不是设备类别（{'/'.join(sorted(CATEGORY_NAMES))}）。现有设备：{known}")


def expand(checks: list[dict], devices: list[dict]) -> tuple[list[dict], list[dict]]:
    """把检查项按 target 展开成「每台设备一条任务」，返回（任务列表，跳过项）。"""
    tasks: list[dict] = []
    skipped: list[dict] = []
    for raw in checks:
        if not isinstance(raw, dict):
            raise ValueError(f"检查项必须是对象，收到 {raw!r}")
        command = str(raw.get("command") or "").strip()
        if not command:
            raise ValueError("每条检查项都要有 command。")
        if any(ord(char) < 32 for char in command):
            raise ValueError("command 必须是单行文本。")
        name = str(raw.get("name") or command)[:80]
        expect = raw.get("expect") or {}
        target = str(raw.get("target") or "all")
        try:
            selected = _targets(target, devices)
        except ValueError as exc:
            skipped.append({"name": name, "target": target, "status": SKIP, "reason": str(exc)})
            continue
        if not selected:
            reason = (
                f"拓扑里没有可连接的 {CATEGORY_NAMES.get(target.upper(), target)} 设备"
                if target.upper() in CATEGORY_NAMES
                else f"拓扑里没有匹配 {target} 的可连接设备"
            )
            skipped.append({"name": name, "target": target, "status": SKIP, "reason": reason})
            continue
        for device in selected:
            tasks.append(
                {
                    "name": name,
                    "device": device["name"],
                    "consolePort": device["consolePort"],
                    "command": command,
                    "expect": expect,
                }
            )
    return tasks, skipped
