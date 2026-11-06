import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.support import FakePool, ScriptedReader, Writer
from ensp_mcp import console, server


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    def device(self, chunks, prompt='<Core-1>'):
        return console.Console('127.0.0.1', 2000, reader=ScriptedReader(chunks), writer=Writer(), prompt=console.Prompt.parse(prompt))

    async def test_prompt_and_echo(self):
        device = self.device(['display version\r\nHuawei VRP\r\n<Core-1>'])
        self.assertEqual(await device.send('display version'), 'Huawei VRP')
        self.assertEqual(device.prompt.hostname, 'Core-1')

    async def test_split_prompt(self):
        device = self.device(['display version\r\nVRP\r\n<Co', 're-1>'])
        self.assertEqual(await device.send('display version'), 'VRP')

    async def test_timeout_closes_connection_with_partial_output(self):
        device = self.device(['display version\r\npartial'])
        writer = device.writer
        with self.assertRaises(console.ConsoleError) as raised:
            await device.send('display version', timeout=0.02)
        self.assertEqual(raised.exception.code, 'command_timeout')
        self.assertEqual(raised.exception.partial_output, 'partial')
        self.assertTrue(writer.closed)
        self.assertIsNone(device.prompt)

    async def test_eof_is_not_success(self):
        device = self.device([''])
        with self.assertRaises(console.ConsoleError) as raised:
            await device.send('display version')
        self.assertEqual(raised.exception.code, 'disconnected')
        self.assertFalse(device.alive)

    async def test_connection_reset_is_structured_error(self):
        device = self.device([ConnectionResetError('reset')])
        result = await device.send_many(['display version', 'display clock'])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['errorCode'], 'disconnected')

    async def test_cancel_closes_connection(self):
        device = self.device([])
        task = asyncio.create_task(device.send('display version'))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(device.alive)

    async def test_paging_answer_not_repeated(self):
        device = self.device(['display x\r\nrow1\r\n---- More ----', '\r\nrow2', '\r\n<Core-1>'])
        writer = device.writer
        output = await device.send('display x')
        self.assertIn('row2', output)
        self.assertEqual(writer.sent.count(' '), 1)

    async def test_multiple_pages(self):
        device = self.device(['row1\r\n---- More ----', '\r\nrow2\r\n---- More ----', '\r\n<Core-1>'])
        writer = device.writer
        await device.send('display x')
        self.assertEqual(writer.sent.count(' '), 2)

    async def test_confirmation_not_mistaken_for_prompt(self):
        device = self.device(['Proceed? [Y/N]:', '\r\nDone\r\n<Core-1>'])
        writer = device.writer
        await device.send('reset counters', confirm='N')
        self.assertIn('N\r\n', writer.sent)

    async def test_save_confirmation_filename_and_success(self):
        device = self.device(['Save? [Y/N]:', '\r\nPlease input the file name(*.cfg)[vrpcfg.zip]:', '\r\nInfo: Save the configuration successfully.\r\n<Core-1>'])
        writer = device.writer
        result = await device.save_config()
        self.assertIn('successfully', result)
        self.assertEqual(writer.sent, ['save\r\n', 'Y\r\n', '\r\n'])

    async def test_save_error_and_unconfirmed(self):
        for output, expected in [('Error: disk full', 'save_failed'), ('Nothing saved', 'save_unconfirmed')]:
            device = self.device([output + '\r\n<Core-1>'])
            with self.assertRaises(console.ConsoleError) as raised:
                await device.save_config()
            self.assertEqual(raised.exception.code, expected)

    async def test_commit_before_save(self):
        device = self.device(['commit\r\n[~Core-1]', 'return\r\n<Core-1>', 'Configuration saved successfully.\r\n<Core-1>'], '[*Core-1]')
        device.pending_changes = True
        writer = device.writer
        await device.save_config()
        self.assertEqual(writer.sent[0], 'commit\r\n')
        self.assertFalse(device.pending_changes)

    async def test_stop_on_command_error(self):
        device = self.device(['Error: Unrecognized command\r\n<Core-1>'])
        result = await device.send_many(['bad-command', 'sysname changed'], stop_on_error=True)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['error'])

    async def test_invalid_commands_never_sent(self):
        for command in ('', 'display x\nsave', 'display x\r', 'display\x00'):
            device = self.device([])
            with self.assertRaises(ValueError):
                await device.send(command)
            self.assertEqual(device.writer.sent, [])

    async def test_login_username_password(self):
        device = self.device(['Username:', 'Password:', '<Core-1>', 'screen-length 0 temporary\r\n<Core-1>'])
        reader, writer = device.reader, device.writer
        with patch('ensp_mcp.console.telnetlib3.open_connection', new=AsyncMock(return_value=(reader, writer))):
            await device.open(credentials=('admin', 'secret'))
        self.assertEqual(writer.sent, ['admin\r\n', 'secret\r\n', 'screen-length 0 temporary\r\n'])
        self.assertEqual(device.prompt.hostname, 'Core-1')

    async def test_password_only_login(self):
        device = self.device(['Password:', '<Core-1>', '<Core-1>'])
        reader, writer = device.reader, device.writer
        with patch('ensp_mcp.console.telnetlib3.open_connection', new=AsyncMock(return_value=(reader, writer))):
            await device.open(credentials=('', 'secret'))
        self.assertEqual(writer.sent[0], 'secret\r\n')

    async def test_first_password_change(self):
        device = self.device(['Username:', 'Password:', 'Password must be changed. Change password now? [Y/N]:', 'Old password:', 'New password:', 'Confirm password:', '<Core-1>', '<Core-1>'])
        reader, writer = device.reader, device.writer
        with patch('ensp_mcp.console.telnetlib3.open_connection', new=AsyncMock(return_value=(reader, writer))):
            await device.open(credentials=('admin', 'old-secret'), new_password='new-secret')
        self.assertEqual(writer.sent[:6], ['admin\r\n', 'old-secret\r\n', 'Y\r\n', 'old-secret\r\n', 'new-secret\r\n', 'new-secret\r\n'])

    async def test_password_setup_without_old_password(self):
        device = self.device(['Please configure the login password\r\nEnter Password:', 'Confirm Password:', '<Core-1>', '<Core-1>'])
        reader, writer = device.reader, device.writer
        with patch('ensp_mcp.console.telnetlib3.open_connection', new=AsyncMock(return_value=(reader, writer))):
            await device.open(new_password='new-secret')
        self.assertEqual(writer.sent[:2], ['new-secret\r\n', 'new-secret\r\n'])

    async def test_missing_auth_fails_without_paging_command(self):
        device = self.device(['Username:'])
        reader, writer = device.reader, device.writer
        with patch('ensp_mcp.console.telnetlib3.open_connection', new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaises(console.ConsoleError) as raised:
                await device.open()
        self.assertEqual(raised.exception.code, 'authentication_required')
        self.assertEqual(writer.sent, [])
        self.assertTrue(writer.closed)

    async def test_wrong_auth_does_not_expose_password(self):
        device = self.device(['Username:', 'Password:', 'Authentication failed secret\r\nUsername:'])
        reader, writer = device.reader, device.writer
        with patch('ensp_mcp.console.telnetlib3.open_connection', new=AsyncMock(return_value=(reader, writer))):
            with self.assertRaises(console.ConsoleError) as raised:
                await device.open(credentials=('admin', 'secret'))
        self.assertNotIn('secret', str(raised.exception))
        self.assertEqual(raised.exception.partial_output, '')

    async def test_pool_reaps_idle_but_not_busy(self):
        pool = console.ConsolePool()
        device = self.device([])
        device.last_used = time.monotonic() - console.IDLE_TIMEOUT - 1
        pool._consoles[('127.0.0.1', 2000)] = device
        async with pool.lock_for(2000, '127.0.0.1'):
            self.assertEqual(await pool.reap_expired(), 0)
        self.assertEqual(await pool.reap_expired(), 1)
        self.assertFalse(device.alive)

    async def test_mcp_error_preserves_partial_results_and_skips_save(self):
        device = self.device(['display clock\r\n12:00\r\n<Core-1>', ''])
        pool = FakePool(device)
        with patch.object(server, 'pool', pool):
            result = json.loads(await server.device_exec('2000', ['display clock', 'display version', 'save'], save=True))
        self.assertFalse(result['ok'])
        self.assertEqual(result['executedCount'], 2)
        self.assertTrue(result['stoppedEarly'])
        self.assertEqual(result['results'][0]['output'], '12:00')
        self.assertEqual(result['saveStatus']['status'], 'skipped_due_to_errors')

    async def test_device_tool_auth_forwarding(self):
        device = self.device(['<Core-1>'])
        pool = FakePool(device)
        with patch.object(server, 'pool', pool):
            result = json.loads(await server.device_exec('2000', ['display clock'], username='admin', password='secret', new_password='changed'))
        self.assertTrue(result['ok'])
        self.assertEqual(pool.kwargs, {'credentials': ('admin', 'secret'), 'new_password': 'changed'})

    async def test_save_failure_keeps_command_results(self):
        device = self.device(['display clock\r\n12:00\r\n<Core-1>', 'Error: disk full\r\n<Core-1>'])
        with patch.object(server, 'pool', FakePool(device)):
            result = json.loads(await server.device_exec('2000', ['display clock'], save=True))
        self.assertFalse(result['ok'])
        self.assertEqual(result['results'][0]['output'], '12:00')
        self.assertEqual(result['saveStatus']['code'], 'save_failed')

    async def test_fetch_error_does_not_write(self):
        device = self.device(['Error: Unrecognized command\r\n<Core-1>'])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'config.cfg'
            with patch.object(server, 'pool', FakePool(device)):
                result = json.loads(await server.device_fetch_config('2000', save_to=str(target)))
            self.assertFalse(result['ok'])
            self.assertFalse(target.exists())

    async def test_invalid_probe_never_connects(self):
        pool = FakePool(None)
        for destination in ('1.1.1.1\nsave', '-a', 'a b', ''):
            with patch.object(server, 'pool', pool):
                result = json.loads(await server.device_probe('2000', destination=destination))
            self.assertFalse(result['ok'])
        self.assertIsNone(pool.kwargs)

    def test_prompt_hyphens_and_brackets(self):
        self.assertEqual(console.Prompt.parse('<Core-1>').hostname, 'Core-1')
        self.assertEqual(console.Prompt.parse('[Core-1]', 'Core-1').view, 'system')
        self.assertEqual(console.Prompt.parse('[Core-1-GigabitEthernet0/0/1]', 'Core-1').view, 'sub')
        self.assertIsNone(console.Prompt.parse('[Core-1>'))
        self.assertIsNone(console.Prompt.parse('[Y/N]'))

    def test_config_blocks_restore_system_view(self):
        commands = server._config_commands('#\nsysname Test\n#\ninterface GE0/0/1\n port link-type access\n#\ninterface GE0/0/2\n port link-type access\n#\nreturn')
        self.assertEqual(commands, ['sysname Test', 'return', 'system-view', 'interface GE0/0/1', 'port link-type access', 'return', 'system-view', 'interface GE0/0/2', 'port link-type access'])

    def test_config_diff_tracks_context_and_duplicate_lines(self):
        before = 'interface A\n description one\ninterface B\n description two'
        after = 'interface A\n description two\ninterface B\n description one'
        diff = console.compare_configs(before, after)
        self.assertFalse(diff['identical'])
        self.assertTrue(diff['onlyInFile'])
        self.assertIn('interface A', diff['unifiedDiff'])
        self.assertFalse(console.compare_configs('a\na', 'a')['identical'])
        self.assertEqual(console.normalise_config('sysname time-core\nreturn'), ['sysname time-core'])

    def test_ping_parser(self):
        output = '5 packet(s) transmitted\n5 packet(s) received\n0.00% packet loss\nround-trip min/avg/max = 1/2/3 ms'
        parsed = console.parse_ping(output)
        self.assertTrue(parsed['reachable'])
        self.assertEqual(parsed['rttMs']['avg'], 2)
        self.assertIsNone(console.parse_ping('incomplete output')['reachable'])


if __name__ == '__main__':
    unittest.main()
