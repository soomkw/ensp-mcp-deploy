"""commandlog：留痕记录、过滤读取和 HTML 报告生成的单元测试。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from ensp_mcp import commandlog


class CommandLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        os.environ["ENSP_MCP_LOG_DIR"] = str(self.dir)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        os.environ.pop("ENSP_MCP_LOG_DIR", None)
        self._tmp.cleanup()

    def _record(self, device="Core1", ok=True, command="sysname Core1", topo=""):
        commandlog.record(
            device=device,
            port=2002,
            tool="device_exec",
            results=[{"command": command, "ok": ok, "error": None if ok else "Error: x", "output": "out", "view": "system"}],
            topo_path=topo,
        )

    def test_record_and_load(self):
        self._record()
        self._record(device="Core2", command="display vlan", ok=False)
        entries = commandlog.load()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["device"], "Core1")
        self.assertTrue(entries[0]["ok"])
        self.assertFalse(entries[1]["ok"])

    def test_load_filters_by_device(self):
        self._record()
        self._record(device="Core2", command="display vlan")
        entries = commandlog.load(device="Core2")
        self.assertEqual([entry["device"] for entry in entries], ["Core2"])

    def test_load_filters_by_topo(self):
        topo = str(self.dir / "lab.topo")
        other = str(self.dir / "other.topo")
        Path(topo).write_text("", encoding="utf-8")
        Path(other).write_text("", encoding="utf-8")
        self._record(topo=topo)
        self._record(device="Core2", command="display vlan", topo=other)
        self.assertEqual(len(commandlog.load(topo_path=topo)), 1)
        self.assertEqual(len(commandlog.load(topo_path=other)), 1)
        # 两份拓扑都没命中时回落到全部记录，避免报告空掉
        self.assertEqual(len(commandlog.load(topo_path=str(self.dir / "missing.topo"))), 2)
        # 没打拓扑标的记录在任何过滤下都保留
        self._record(device="Core3", command="display clock")
        self.assertEqual(len(commandlog.load(topo_path=str(self.dir / "missing.topo"))), 1)

    def test_record_skips_blank_commands(self):
        written = commandlog.record(
            device="Core1", port=2002, tool="device_exec",
            results=[{"command": "  ", "ok": True, "output": ""}],
        )
        self.assertEqual(written, 0)
        self.assertEqual(commandlog.load(), [])

    def test_report_from_entries(self):
        topo = str(self.dir / "lab.topo")
        self._record(topo=topo)
        self._record(device="Core2", command="display vlan", ok=False, topo=topo)
        entries = commandlog.load(topo_path=topo)
        result = commandlog.build_report(topo_path=topo, entries=entries)
        html = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("Core1", html)
        self.assertIn("Core2", html)
        self.assertEqual(result["commandCount"], 2)
        self.assertEqual(result["failedCommands"], 1)

    def test_report_without_topo_uses_grid(self):
        self._record()
        result = commandlog.build_report(entries=commandlog.load())
        html = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("<svg", html)
        self.assertIn("Core1", html)

    def test_report_empty_entries_has_hint(self):
        result = commandlog.build_report()
        html = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("还没有命令记录", html)
        self.assertEqual(result["commandCount"], 0)

    def test_log_is_jsonl(self):
        self._record()
        lines = commandlog.log_path().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["tool"], "device_exec")
        self.assertIn("ts", entry)


if __name__ == "__main__":
    unittest.main()
