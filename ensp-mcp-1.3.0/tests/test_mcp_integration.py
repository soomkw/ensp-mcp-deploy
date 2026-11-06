"""Real stdio MCP and local TCP/Telnet integration. No eNSP instance is modified."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.support import ROOT
import telnetlib3
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class FakeVRP:
    """Small protocol fixture, deliberately not presented as a Huawei emulator."""
    def __init__(self):
        self.commands = []
        self.config = ['sysname Lab', 'interface GigabitEthernet0/0/1', ' description test']
        self.authenticated = False
        self.clients = set()

    async def shell(self, reader, writer):
        self.clients.add(writer)
        try:
            writer.write('Username:')
            if (await reader.readline()).strip() != 'admin':
                return
            writer.write('Password:')
            if (await reader.readline()).strip() != 'test-secret':
                return
            self.authenticated = True
            hostname, view = 'Lab', 'user'
            writer.write('\r\n<Lab>')
            while True:
                line = await reader.readline()
                if not line:
                    return
                command = line.strip()
                if not command:
                    continue
                self.commands.append(command)
                writer.write(command + '\r\n')
                if command == 'system-view':
                    view = 'system'
                elif command in ('return', 'quit'):
                    view = 'user'
                elif command.startswith('sysname '):
                    hostname = command.split(' ', 1)[1]
                    self.config[0] = command
                elif command == 'save':
                    writer.write('Save configuration? [Y/N]:')
                    answer = (await reader.readline()).strip()
                    writer.write('\r\nConfiguration saved successfully.\r\n' if answer.upper() == 'Y' else '\r\nCancelled.\r\n')
                elif command == 'display current-configuration':
                    writer.write('#\r\n' + '\r\n#\r\n'.join(self.config) + '\r\n#\r\nreturn\r\n')
                elif command.startswith('ping '):
                    writer.write('5 packet(s) transmitted\r\n5 packet(s) received\r\n0.00% packet loss\r\nround-trip min/avg/max = 1/2/3 ms\r\n')
                elif command == 'test-disconnect':
                    return
                elif command == 'test-error':
                    writer.write('Error: Unrecognized command found at position.\r\n')
                elif command == 'display version':
                    writer.write('Protocol test fixture version 1\r\n')
                writer.write(f'<{hostname}>' if view == 'user' else f'[{hostname}]')
        finally:
            writer.close()
            self.clients.discard(writer)


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_tools_over_stdio_with_real_telnet(self):
        fixture = FakeVRP()
        tcp = await telnetlib3.create_server(host='127.0.0.1', port=0, shell=fixture.shell, connect_maxwait=0.05)
        port = tcp.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                topo = str(Path(directory) / 'mcp.topo')
                params = StdioServerParameters(command=sys.executable, args=['-B', '-m', 'ensp_mcp.server'],
                                               env={**os.environ, 'PYTHONPATH':str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE':'1'}, cwd=str(ROOT))
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as client:
                        await client.initialize()
                        tools = await client.list_tools()
                        self.assertEqual(len(tools.tools), 19)
                        self.assertIn("environment_check", {tool.name for tool in tools.tools})
                        self.assertIn("command_trace", {tool.name for tool in tools.tools})
                        self.assertIn("device_batch", {tool.name for tool in tools.tools})
                        self.assertIn("lab_verify", {tool.name for tool in tools.tools})
                        for tool in tools.tools:
                            if tool.name in ('device_exec', 'device_push_config', 'device_fetch_config', 'device_probe', 'device_batch', 'lab_verify'):
                                self.assertIn('password', tool.inputSchema['properties'])
                            if tool.name == 'device_batch':
                                self.assertIn('configs', tool.inputSchema['properties'])
                                self.assertIn('concurrency', tool.inputSchema['properties'])
                            if tool.name == 'lab_verify':
                                self.assertIn('suite', tool.inputSchema['properties'])
                                self.assertIn('checks', tool.inputSchema['properties'])
                            if tool.name == 'topo_layout':
                                self.assertIn('zones', tool.inputSchema['properties'])
                                self.assertIn('replace_annotations', tool.inputSchema['properties'])
                            if tool.name == 'environment_check':
                                self.assertIn('models', tool.inputSchema['properties'])

                        async def call(name, args, ok=True):
                            response = await client.call_tool(name, args)
                            self.assertFalse(response.isError, response)
                            payload = json.loads(response.content[0].text)
                            self.assertEqual(payload['ok'], ok, payload)
                            return payload

                        self.assertEqual((await call('list_models', {}))['count'], 43)
                        await call('topo_create', {'path':topo})
                        await call('topo_add_devices', {'path':topo, 'devices':[{'model':'S5700','name':'Core'}, {'model':'PC','name':'PC1'}]})
                        await call('topo_connect', {'path':topo, 'links':[{'from':'Core','to':'PC1'}]})
                        await call('topo_layout', {'path':topo})
                        await call('topo_validate', {'path':topo})
                        self.assertEqual((await call('topo_inspect', {'path':topo}))['linkCount'], 1)
                        await call('topo_render', {'path':topo})
                        for fmt in ('mermaid', 'drawio', 'csv'):
                            await call('topo_export', {'path':topo, 'format':fmt})

                        auth = {'target':str(port), 'username':'admin', 'password':'test-secret'}
                        executed = await call('device_exec', {**auth, 'commands':['display version']})
                        self.assertIn('Protocol test fixture', executed['results'][0]['output'])
                        self.assertTrue(fixture.authenticated)
                        pushed = await call('device_push_config', {**auth, 'config':'#\nsysname Lab-1\n#\ninterface GigabitEthernet0/0/1\n description test\n#\nreturn'})
                        self.assertEqual(pushed['saveStatus']['status'], 'saved')
                        cfg = str(Path(directory) / 'saved.cfg')
                        await call('device_fetch_config', {**auth, 'save_to':cfg})
                        compared = await call('device_fetch_config', {**auth, 'compare_with':cfg})
                        self.assertTrue(compared['diff']['identical'])
                        ping = await call('device_probe', {**auth, 'destination':'192.0.2.1'})
                        self.assertTrue(ping['parsed']['reachable'])
                        sessions = await call('device_sessions', {})
                        self.assertEqual(len(sessions['sessions']), 1)
                        await call('device_exec', {**auth, 'commands':['test-error', 'sysname should-not-run']}, ok=False)
                        self.assertNotIn('sysname should-not-run', fixture.commands)
                        failed = await call('device_exec', {**auth, 'commands':['test-disconnect']}, ok=False)
                        self.assertEqual(failed['results'][0]['errorCode'], 'disconnected')
                        await call('device_exec', {**auth, 'commands':['display version']})
                        await call('device_sessions', {'action':'close_all'})
                        await call('topo_remove', {'path':topo, 'devices':['PC1']})
        finally:
            for writer in list(fixture.clients):
                writer.close()
            tcp.close()
            await tcp.wait_closed()


if __name__ == '__main__':
    unittest.main()
