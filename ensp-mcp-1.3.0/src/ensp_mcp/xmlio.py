"""``.topo`` 文件的读写细节。

eNSP 写出的文件有两处不能直接交给 ``ElementTree``：

* XML 声明写的是 ``encoding="UNICODE"``，这不是合法的编码名，
  ``ET.parse`` 会直接抛 ``LookupError``。
* 声明说 UNICODE，实际字节可能是 UTF-8 无 BOM、中文 Windows 上的
  CP936，或真正的 UTF-16。读取时必须按字节特征探测，而不能相信声明。

写回时统一用 eNSP 自己的形态：UTF-8 无 BOM、CRLF 换行、四空格缩进、
自闭合标签前留一个空格。
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
from xml.etree import ElementTree as ET

XML_DECLARATION = '<?xml version="1.0" encoding="UNICODE" ?>'
TOPO_VERSION = "1.3.00.200T"

_DECL_RE = re.compile(r"^\s*<\?xml.*?\?>", re.DOTALL)


def read_text(path: str) -> str:
    """按字节探测编码并读成文本。"""
    with open(path, "rb") as handle:
        raw = handle.read()

    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    # 无 BOM 的 UTF-16 LE：ASCII 字符后面跟 0x00
    if len(raw) > 4 and raw[1] == 0 and raw[3] == 0:
        return raw.decode("utf-16-le")
    if len(raw) > 4 and raw[0] == 0 and raw[2] == 0:
        return raw.decode("utf-16-be")
    try:
        # 使用严格模式，避免把 CP936/GBK 字节静默替换成乱码。
        return raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        # eNSP 自带的中文示例沿用 Windows ANSI（CP936）。GB18030 是其
        # 向后兼容超集，也能覆盖稍新的中文字符。
        return raw.decode("gb18030", errors="strict")


def parse(path: str) -> ET.Element:
    """读出 ``<topo>`` 根节点。"""
    text = read_text(path)
    # 声明里的 encoding="UNICODE" 会让解析器报错，直接去掉整个声明
    text = _DECL_RE.sub("", text, count=1).lstrip("\ufeff").lstrip()
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
        root = ET.fromstring(text, parser=parser)
        if root.tag != "topo":
            raise ValueError(f"{path} 的根节点必须是 topo。")
        return root
    except ET.ParseError as exc:
        raise ValueError(f"{path} 不是合法的 .topo 文件：{exc}") from exc


def _serialise(element: ET.Element, depth: int, out: list[str]) -> None:
    pad = "    " * depth
    attrs = "".join(f' {k}="{_escape(v)}"' for k, v in element.attrib.items())
    children = list(element)
    if not children:
        # eNSP 的自闭合标签在斜杠前留一个空格
        out.append(f"{pad}<{element.tag}{attrs} />")
        return
    out.append(f"{pad}<{element.tag}{attrs}>")
    for child in children:
        _serialise(child, depth + 1, out)
    out.append(f"{pad}</{element.tag}>")


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write(path: str, root: ET.Element) -> None:
    """保留文本、注释和扩展字段，用同目录临时文件原子替换。"""
    tree = copy.deepcopy(root)
    ET.indent(tree, space="    ")
    body = ET.tostring(tree, encoding="unicode", short_empty_elements=True)
    text = (XML_DECLARATION + "\n" + body + "\n").replace("\r\n", "\n").replace("\n", "\r\n")
    atomic_write(path, text)


def atomic_write(path: str, text: str, encoding: str = "utf-8") -> None:
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ensp-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_root() -> ET.Element:
    """建一棵空拓扑。``<shapes>`` 与 ``<txttips>`` 必须存在，否则 eNSP 打不开。"""
    root = ET.Element("topo", {"version": TOPO_VERSION})
    ET.SubElement(root, "devices")
    ET.SubElement(root, "lines")
    ET.SubElement(root, "shapes")
    ET.SubElement(root, "txttips")
    return root
