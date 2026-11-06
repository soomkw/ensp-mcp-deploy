"""Read-only environment and topology diagnostics: python -m ensp_mcp.doctor."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
from pathlib import Path

from . import __version__, catalog, preflight, validate
from .topo import Topology


async def _port_status(host: str, port: int) -> dict:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
        writer.close()
        await writer.wait_closed()
        return {"port": port, "tcpOpen": True}
    except (OSError, asyncio.TimeoutError):
        return {"port": port, "tcpOpen": False}


def inspect_environment(
    topo_path: str = "", ensp_home: str = "", models: list[str] | None = None
) -> dict:
    dependencies = {}
    for name in ('mcp', 'telnetlib3', 'pillow'):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    result = {"version": __version__, "python": platform.python_version(), "dependencies": dependencies,
              "modelCount": len(catalog.MODELS), "ok": all(dependencies.values())}
    required_models: list[str] = list(models or [])
    if topo_path:
        topo = Topology.load(topo_path)
        required_models.extend(device.model for device in topo.devices)
        checked = validate.check(topo)
        result['topology'] = {"path": str(Path(topo_path).resolve()), "deviceCount": len(topo.devices),
                              "linkCount": len(topo.links), "validation": checked,
                              "consolePorts": sorted({d.com_port for d in topo.devices if 1 <= d.com_port <= 65535})}
        result['ok'] = result['ok'] and checked['ok']
    runtime = preflight.inspect_runtime(required_models, ensp_home=ensp_home)
    result['runtime'] = runtime
    result['readyForTopology'] = runtime['readyForTopology']
    result['requestedModels'] = list(dict.fromkeys(required_models))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description='eNSP MCP 环境诊断；默认不连接任何设备。')
    parser.add_argument('--topo', default='', help='.topo 文件路径')
    parser.add_argument('--ensp-home', default='', help='可选：显式指定 eNSP 安装目录')
    parser.add_argument('--model', action='append', default=[], help='可重复：检查计划使用的设备型号')
    parser.add_argument('--check-ports', action='store_true', help='可选：只检查拓扑内 console TCP 端口，不发送命令')
    parser.add_argument('--strict-runtime', action='store_true', help='拓扑所需运行环境不就绪时返回非零状态')
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    if args.check_ports and not args.topo:
        parser.error('--check-ports 需要 --topo')
    try:
        result = inspect_environment(args.topo, args.ensp_home, args.model)
        if args.check_ports:
            async def probe():
                semaphore = asyncio.Semaphore(16)
                async def limited(port):
                    async with semaphore:
                        return await _port_status(args.host, port)
                return await asyncio.gather(*(limited(port) for port in result['topology']['consolePorts']))
            ports = asyncio.run(probe())
            result['ports'] = ports
            result['ok'] = result['ok'] and all(p['tcpOpen'] for p in ports)
    except (ValueError, KeyError, OSError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    strict_failed = bool(
        args.strict_runtime
        and (args.topo or args.model)
        and result.get('readyForTopology') is not True
    )
    raise SystemExit(0 if result['ok'] and not strict_failed else 1)


if __name__ == '__main__':
    main()
