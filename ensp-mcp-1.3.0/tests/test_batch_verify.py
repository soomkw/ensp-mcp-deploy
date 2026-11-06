"""device_batch（多设备并发）与 lab_verify（自动验收）的测试。

并发部分用本机真实 TCP/Telnet 桩服务验证，不依赖 eNSP。
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT  # noqa: F401  （把 src 加进 sys.path）

import telnetlib3

from ensp_mcp import server, verify
from ensp_mcp.topo import Topology


# ---------------------------------------------------------------------------
# 验收规则
# ---------------------------------------------------------------------------


class EvaluateTests(unittest.TestCase):
    def test_empty_expect_passes_when_command_ok(self):
        passed, problems = verify.evaluate({}, "任意回显")
        self.assertTrue(passed)
        self.assertEqual(problems, [])

    def test_contains_and_not_contains(self):
        passed, _ = verify.evaluate({"contains": ["State : Master", "VRID"]}, "VRID 1\n  State : Master")
        self.assertTrue(passed)
        passed, problems = verify.evaluate({"contains": ["State : Backup"]}, "State : Master")
        self.assertFalse(passed)
        self.assertIn("State : Backup", problems[0])
        passed, problems = verify.evaluate({"notContains": ["No BFD session"]}, "Info: No BFD session exists.")
        self.assertFalse(passed)

    def test_contains_accepts_list_and_all_must_match(self):
        passed, problems = verify.evaluate({"contains": ["A", "B"]}, "只有 A")
        self.assertFalse(passed)
        self.assertEqual(len(problems), 1)

    def test_regex_and_not_regex(self):
        passed, _ = verify.evaluate({"regex": r"Role\s*[:：]\s*active"}, "Role: active")
        self.assertTrue(passed)
        passed, problems = verify.evaluate({"notRegex": r"\bDown\b"}, "Interface  State\n GE0/0/1  Down")
        self.assertFalse(passed)
        self.assertIn("Down", problems[0])

    def test_min_count(self):
        text = "State : Master\nVRID 2\n  State : Backup"
        passed, _ = verify.evaluate({"minCount": {"pattern": r"State\s*:\s*(?:Master|Backup)", "atLeast": 2}}, text)
        self.assertTrue(passed)
        passed, problems = verify.evaluate(
            {"minCount": {"pattern": r"State\s*:\s*(?:Master|Backup)", "atLeast": 3}}, text
        )
        self.assertFalse(passed)
        self.assertIn("少于要求的 3 次", problems[0])

    def test_reachable_uses_ping_parser(self):
        ok = "5 packet(s) transmitted\n5 packet(s) received\n0.00% packet loss"
        bad = "5 packet(s) transmitted\n0 packet(s) received\n100.00% packet loss"
        self.assertTrue(verify.evaluate({"reachable": True}, ok)[0])
        passed, problems = verify.evaluate({"reachable": True}, bad)
        self.assertFalse(passed)
        self.assertIn("不通", problems[0])
        self.assertTrue(verify.evaluate({"reachable": False}, bad)[0])

    def test_max_loss(self):
        bad = "5 packet(s) transmitted\n3 packet(s) received\n40.00% packet loss"
        passed, problems = verify.evaluate({"maxLoss": 20}, bad)
        self.assertFalse(passed)
        self.assertIn("超过上限", problems[0])
        self.assertTrue(verify.evaluate({"maxLoss": 50}, bad)[0])

    def test_command_error_overrides_expect(self):
        passed, problems = verify.evaluate({"contains": ["Error:"]}, "Error: Unrecognized command found")
        self.assertFalse(passed)
        self.assertIn("命令执行报错", problems[0])

    def test_unknown_expect_key_rejected(self):
        with self.assertRaises(ValueError) as caught:
            verify.evaluate({"contians": ["x"]}, "x")
        self.assertIn("不认识", str(caught.exception))

    def test_broken_regex_rejected(self):
        with self.assertRaises(ValueError):
            verify.evaluate({"regex": "(["}, "x")


class ExpandTests(unittest.TestCase):
    devices = [
        {"name": "FW1", "category": "FW", "consolePort": 2000},
        {"name": "FW2", "category": "FW", "consolePort": 2001},
        {"name": "Core1", "category": "LSW", "consolePort": 2002},
        {"name": "PC1", "category": "CLIENT", "consolePort": None},
    ]

    def test_target_all_skips_devices_without_console(self):
        tasks, skipped = verify.expand([{"command": "display clock", "target": "all"}], self.devices)
        self.assertEqual([task["device"] for task in tasks], ["FW1", "FW2", "Core1"])
        self.assertEqual(skipped, [])

    def test_target_category_expands(self):
        tasks, _ = verify.expand([{"command": "display hrp state", "target": "FW"}], self.devices)
        self.assertEqual([task["device"] for task in tasks], ["FW1", "FW2"])

    def test_target_device_name(self):
        tasks, _ = verify.expand([{"command": "display vlan", "target": "Core1"}], self.devices)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["consolePort"], 2002)

    def test_missing_category_becomes_skip(self):
        tasks, skipped = verify.expand([{"command": "display bfd session all", "target": "WLAN"}], self.devices)
        self.assertEqual(tasks, [])
        self.assertEqual(skipped[0]["status"], verify.SKIP)
        self.assertIn("无线", skipped[0]["reason"])

    def test_unknown_target_becomes_skip(self):
        tasks, skipped = verify.expand([{"command": "display vlan", "target": "NoSuchDevice"}], self.devices)
        self.assertEqual(tasks, [])
        self.assertIn("NoSuchDevice", skipped[0]["reason"])

    def test_command_required(self):
        with self.assertRaises(ValueError):
            verify.expand([{"target": "all"}], self.devices)


class SuiteTests(unittest.TestCase):
    def test_known_suites_resolve(self):
        for name in verify.suite_names():
            checks = verify.resolve_suite(name)
            self.assertTrue(checks)
            for check in checks:
                self.assertIn("command", check)
                verify.evaluate(check.get("expect"), "x")

    def test_unknown_suite_lists_alternatives(self):
        with self.assertRaises(ValueError) as caught:
            verify.resolve_suite("nope")
        self.assertIn("fw_ha", str(caught.exception))

    def test_fw_ha_covers_hrp_and_vrrp(self):
        commands = " ".join(check["command"] for check in verify.resolve_suite("fw_ha"))
        self.assertIn("display hrp state", commands)
        self.assertIn("display vrrp", commands)


# ---------------------------------------------------------------------------
# 并发 / 验收（真实 Telnet 桩）
# ---------------------------------------------------------------------------


class FakeNode:
    """最小的 VRP 桩：够 MCP 的连接、执行、判定链路用。"""

    def __init__(self, name: str, delay: float = 0.0):
        self.name = name
        self.delay = delay
        self.commands: list[str] = []

    async def shell(self, reader, writer):
        try:
            writer.write("Username:")
            if (await reader.readline()).strip() != "admin":
                return
            writer.write("Password:")
            if (await reader.readline()).strip() != "secret":
                return
            if self.delay:
                await asyncio.sleep(self.delay)
            hostname, view = self.name, "user"
            writer.write(f"\r\n<{hostname}>")
            while True:
                line = await reader.readline()
                if not line:
                    return
                command = line.strip()
                if not command:
                    continue
                self.commands.append(command)
                writer.write(command + "\r\n")
                if command == "system-view":
                    view = "system"
                elif command in ("return", "quit"):
                    view = "user"
                elif command == "save":
                    writer.write("Save configuration? [Y/N]:")
                    answer = (await reader.readline()).strip()
                    writer.write(
                        "\r\nConfiguration saved successfully.\r\n"
                        if answer.upper() == "Y"
                        else "\r\nCancelled.\r\n"
                    )
                elif command == "display vrrp":
                    writer.write("VRID 1\n  State : Master\n  Virtual IP : 10.0.0.1\n")
                elif command == "display bfd session all":
                    writer.write("Info: No BFD session exists.\n")
                elif command == "display version":
                    writer.write(f"FakeVRP {self.name}\n")
                writer.write(f"<{hostname}>" if view == "user" else f"[{hostname}]")
        finally:
            writer.close()


class BatchVerifyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._old_log = os.environ.get("ENSP_MCP_LOG_DIR")
        os.environ["ENSP_MCP_LOG_DIR"] = str(self.dir / "logs")
        self.servers = []
        self.nodes = {}

    async def asyncTearDown(self):
        await server.pool.close_all()
        for srv in self.servers:
            srv.close()
            await srv.wait_closed()
        if self._old_log is None:
            os.environ.pop("ENSP_MCP_LOG_DIR", None)
        else:
            os.environ["ENSP_MCP_LOG_DIR"] = self._old_log
        server._last_topo = None
        self._tmp.cleanup()

    async def _start(self, name, delay=0.0):
        node = FakeNode(name, delay=delay)
        srv = await telnetlib3.create_server(
            host="127.0.0.1", port=0, shell=node.shell, connect_maxwait=0.05
        )
        self.servers.append(srv)
        self.nodes[name] = node
        return srv.sockets[0].getsockname()[1]

    async def _topo(self, entries):
        topo = Topology()
        for name, model, port in entries:
            device = topo.add_device(model_name=model, name=name)
            device.com_port = port
        path = str(self.dir / "lab.topo")
        topo.save(path)
        return path

    async def test_batch_runs_concurrently_on_all_devices(self):
        ports = [await self._start(f"AR{i}", delay=0.5) for i in (1, 2, 3)]
        topo = await self._topo([(f"AR{i}", "AR2220", ports[i - 1]) for i in (1, 2, 3)])

        async def run(concurrency):
            await server.pool.close_all()
            return json.loads(
                await server.device_batch(
                    topo_path=topo,
                    devices=["all"],
                    commands=["display version"],
                    concurrency=concurrency,
                    username="admin",
                    password="secret",
                )
            )

        serial = await run(1)
        parallel = await run(3)

        self.assertTrue(parallel["ok"], parallel)
        self.assertEqual(parallel["deviceCount"], 3)
        self.assertEqual(parallel["succeeded"], 3)
        self.assertEqual(parallel["mode"], "exec")
        self.assertEqual({item["device"] for item in parallel["devices"]}, {"AR1", "AR2", "AR3"})
        self.assertTrue(all(item["executedCount"] == 1 for item in parallel["devices"]))
        self.assertTrue(all(item["durationMs"] >= 0 for item in parallel["devices"]))
        self.assertEqual(parallel["devices"][0]["saveStatus"], "not_requested")
        # 并发要真的比串行快（每台 0.5s 延迟，串行约 3 倍）
        self.assertLess(parallel["elapsedSeconds"], serial["elapsedSeconds"] * 0.7)
        self.assertGreater(serial["elapsedSeconds"] - parallel["elapsedSeconds"], 0.8)

    async def test_batch_isolates_single_device_failure(self):
        port1 = await self._start("AR1")
        port2 = await self._start("AR2")
        topo = await self._topo(
            [("AR1", "AR2220", port1), ("AR2", "AR2220", port2), ("AR9", "AR2220", 1)]
        )

        payload = json.loads(
            await server.device_batch(
                topo_path=topo,
                devices=["AR1", "AR2", "AR9"],
                commands=["display version"],
                concurrency=3,
                username="admin",
                password="secret",
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["succeeded"], 2)
        self.assertEqual(payload["failed"], 1)
        broken = next(item for item in payload["devices"] if item["device"] == "AR9")
        self.assertFalse(broken["ok"])
        self.assertIn("error", broken)
        # 成功的两台仍然完成了任务
        self.assertIn("display version", self.nodes["AR1"].commands)

    async def test_batch_config_mode_pushes_per_device_config(self):
        port1 = await self._start("Core1")
        port2 = await self._start("Core2")
        topo = await self._topo([("Core1", "S5700", port1), ("Core2", "S5700", port2)])

        payload = json.loads(
            await server.device_batch(
                topo_path=topo,
                configs={"Core1": "sysname Core1\nvlan 10\n", "Core2": "sysname Core2\nvlan 20\n"},
                concurrency=2,
                username="admin",
                password="secret",
            )
        )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["mode"], "config")
        self.assertTrue(all(item["saveStatus"] == "saved" for item in payload["devices"]))
        self.assertIn("vlan 10", self.nodes["Core1"].commands)
        self.assertIn("vlan 20", self.nodes["Core2"].commands)

    async def test_batch_rejects_unknown_device_and_step_mode_conflict(self):
        port1 = await self._start("AR1")
        topo = await self._topo([("AR1", "AR2220", port1)])

        missing = json.loads(
            await server.device_batch(topo_path=topo, devices=["AR9"], commands=["display version"])
        )
        self.assertFalse(missing["ok"])
        self.assertIn("AR9", missing["error"])

        conflict = json.loads(
            await server.device_batch(topo_path=topo, commands=["display version"], configs={"AR1": "x"})
        )
        self.assertFalse(conflict["ok"])
        self.assertIn("只给一个", conflict["error"])

    async def test_lab_verify_passes_and_fails_by_rule(self):
        port1 = await self._start("FW1")
        port2 = await self._start("FW2")
        topo = await self._topo([("FW1", "USG5500", port1), ("FW2", "USG5500", port2)])

        passing = json.loads(
            await server.lab_verify(
                topo_path=topo,
                checks=[
                    {
                        "name": "VRRP 主备",
                        "target": "FW",
                        "command": "display vrrp",
                        "expect": {"minCount": {"pattern": r"State\s*:\s*Master", "atLeast": 1}},
                    }
                ],
                username="admin",
                password="secret",
            )
        )
        self.assertTrue(passing["ok"], passing)
        self.assertEqual(passing["verdict"], verify.PASS)
        self.assertEqual(passing["passed"], 2)
        self.assertEqual(passing["failed"], 0)

        failing = json.loads(
            await server.lab_verify(
                topo_path=topo,
                suite="bfd",
                username="admin",
                password="secret",
            )
        )
        self.assertFalse(failing["ok"])
        self.assertEqual(failing["verdict"], verify.FAIL)
        self.assertEqual(failing["failed"], 2)
        self.assertIn("No BFD session", " ".join(
            problem for check in failing["checks"] for problem in check.get("problems", [])
        ))

    async def test_lab_verify_skips_when_no_matching_device(self):
        port1 = await self._start("AR1")
        topo = await self._topo([("AR1", "AR2220", port1)])

        payload = json.loads(
            await server.lab_verify(topo_path=topo, suite="fw_ha", username="admin", password="secret")
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["verdict"], verify.SKIP)
        self.assertEqual(len(payload["skippedChecks"]), len(verify.resolve_suite("fw_ha")))
        self.assertIn("防火墙", payload["skippedChecks"][0]["reason"])

    async def test_lab_verify_unknown_suite(self):
        port1 = await self._start("AR1")
        topo = await self._topo([("AR1", "AR2220", port1)])
        payload = json.loads(await server.lab_verify(topo_path=topo, suite="nope"))
        self.assertFalse(payload["ok"])
        self.assertIn("bfd", payload["error"])

    async def test_verify_results_are_logged(self):
        port1 = await self._start("AR1")
        topo = await self._topo([("AR1", "AR2220", port1)])
        await server.lab_verify(
            topo_path=topo,
            checks=[{"name": "版本", "target": "AR1", "command": "display version", "expect": {}}],
            username="admin",
            password="secret",
        )
        from ensp_mcp import commandlog

        entries = commandlog.load(topo_path=topo)
        self.assertTrue(any(entry["tool"] == "lab_verify" for entry in entries))
        self.assertTrue(any(entry["command"] == "display version" for entry in entries))


if __name__ == "__main__":
    unittest.main()
