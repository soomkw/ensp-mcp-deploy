"""Create and verify a two-switch eNSP lab via the MCP protocol.

create: offline generation only. run --apply: configure the created, running lab.
run without --apply: read and verify only, useful after manually restarting devices.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ensp_mcp import layout, validate
from ensp_mcp.topo import Topology


def create(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'acceptance.topo'
    if path.exists():
        raise ValueError(f'{path} 已存在，请使用新的目录，避免覆盖联调记录。')
    topo = Topology()
    topo.add_device('S5700', name='MCP-LSW1')
    topo.add_device('S5700', name='MCP-LSW2')
    topo.connect('MCP-LSW1', 'MCP-LSW2', 'GE0/0/1', 'GE0/0/1')
    layout.apply(topo, mode='grid')
    topo.save(str(path))
    for index in (1, 2):
        config = f'#\nsysname MCP-LSW{index}\n#\ninterface GigabitEthernet0/0/1\n port link-type access\n port default vlan 1\n undo shutdown\n#\ninterface Vlanif1\n ip address 192.0.2.{index} 255.255.255.252\n#\nreturn\n'
        (directory / f'MCP-LSW{index}.cfg').write_text(config, encoding='utf-8')
    return {'ok':True, 'topology':str(path.resolve()), 'validation':validate.check(topo),
            'next':'在 eNSP 中打开 acceptance.topo 并启动两台设备，随后运行 run --apply。'}


async def run(directory: Path, apply: bool, username: str, password: str, new_password: str) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    directory = directory.resolve()
    path = directory / 'acceptance.topo'
    topo = Topology.load(str(path))
    if {d.name for d in topo.devices} != {'MCP-LSW1', 'MCP-LSW2'} or any(d.model != 'S5700' for d in topo.devices):
        raise ValueError('该脚本仅接受 create 生成的两台 S5700 验收拓扑。')
    report = {'ok':False, 'mode':'apply' if apply else 'verify_only', 'steps':[]}
    parameters = StdioServerParameters(command=sys.executable, args=['-B','-m','ensp_mcp.server'],
        env={**os.environ, 'PYTHONPATH':str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE':'1'})
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as client:
                await client.initialize()
                async def call(tool, args):
                    raw = await client.call_tool(tool, args)
                    if raw.isError:
                        raise RuntimeError(f'{tool} MCP 调用失败：{raw.content}')
                    result = json.loads(raw.content[0].text)
                    report['steps'].append({'tool':tool, 'target':args.get('target'), 'result':result})
                    if not result['ok']:
                        raise RuntimeError(f"{tool} 失败：{result.get('error', result)}")
                    return result

                for index in (1, 2):
                    auth = {'target':f'MCP-LSW{index}', 'topo_path':str(path), 'username':username, 'password':password, 'new_password':new_password}
                    await call('device_exec', {**auth, 'view':'user', 'commands':['display version']})
                    if apply:
                        await call('device_push_config', {**auth, 'config_file':str(directory / f'MCP-LSW{index}.cfg'), 'save':True})
                    fetched = await call('device_fetch_config', {**auth, 'save_to':str(directory / f'MCP-LSW{index}-running.cfg')})
                    if f'ip address 192.0.2.{index} 255.255.255.252' not in fetched['config']:
                        raise RuntimeError(f'MCP-LSW{index} 运行配置未包含预期地址。')
                    saved = await call('device_exec', {**auth, 'view':'user', 'commands':['display saved-configuration']})
                    if f'192.0.2.{index}' not in saved['results'][0]['output']:
                        raise RuntimeError(f'MCP-LSW{index} 保存配置未包含预期地址。')
                for index in (1, 2):
                    auth = {'target':f'MCP-LSW{index}', 'topo_path':str(path), 'username':username, 'password':password, 'new_password':new_password}
                    for attempt in range(3):
                        probe = await call('device_probe', {**auth, 'destination':f'192.0.2.{3-index}'})
                        if probe['parsed']['reachable'] is True:
                            break
                        if attempt < 2:
                            await asyncio.sleep(3)
                    else:
                        raise RuntimeError(f'MCP-LSW{index} 到对端 ping 未成功。')
                await call('device_sessions', {'action':'close_all'})
                report['ok'] = True
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        (directory / ('apply-report.json' if apply else 'verify-report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create','run'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='给 create 生成的实验设备下发并保存测试配置')
    parser.add_argument('--username', default=os.environ.get('ENSP_USERNAME',''))
    parser.add_argument('--password-env', default='ENSP_PASSWORD', help='读取登录密码的环境变量名')
    parser.add_argument('--new-password-env', default='ENSP_NEW_PASSWORD', help='读取首次新密码的环境变量名')
    args = parser.parse_args()
    try:
        result = create(args.directory) if args.action == 'create' else asyncio.run(run(args.directory, args.apply, args.username,
            os.environ.get(args.password_env,''), os.environ.get(args.new_password_env,'')))
    except (OSError, ValueError) as exc:
        result = {'ok':False, 'error':str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
