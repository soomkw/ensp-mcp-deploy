import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ensp_mcp import preflight, server


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "eNSP"
        (self.home / "res").mkdir(parents=True)
        (self.home / "res" / "items.xml").write_text("<Items/>", encoding="utf-8")
        (self.home / "vboxserver" / "AR_Base" / "Snapshots").mkdir(parents=True)
        (self.home / "vboxserver" / "AR_Base" / "AR_Base.vdi").write_bytes(b"vdi")
        (self.home / "vboxserver" / "AR_Base" / "Snapshots" / "link.vdi").write_bytes(b"snapshot")
        fw = self.home / "vboxserver" / "devices" / "FW" / "USG5500"
        fw.mkdir(parents=True)
        for name in ("eNSP_Firewall.exe", "flash.dat", "web.bin"):
            (fw / name).write_bytes(b"present")

    def ensp_record(self):
        return {
            "found": True,
            "path": str(self.home),
            "source": "test",
            "version": "1.3.00.200T",
            "expectedVirtualBox": "5.2.x",
        }

    @staticmethod
    def vbox_record():
        return {
            "found": True,
            "path": "X:/VirtualBox",
            "source": "test",
            "executable": "X:/VirtualBox/VBoxManage.exe",
        }

    @staticmethod
    def command_result(command, healthy=True):
        joined = " ".join(command)
        if command[-1] == "--version":
            return {"ok": True, "returnCode": 0, "output": "5.2.44r139111\n"}
        if joined.endswith("list hostinfo"):
            value = "yes" if healthy else "no"
            return {
                "ok": True,
                "returnCode": 0,
                "output": f"Processor supports HW virtualization: {value}\nProcessor supports nested paging: {value}\n",
            }
        if joined.endswith("list vms"):
            return {"ok": True, "returnCode": 0, "output": '"AR_Base" {11111111-1111-1111-1111-111111111111}\n'}
        if joined.endswith("list hostonlyifs"):
            return {
                "ok": True,
                "returnCode": 0,
                "output": "Name: VirtualBox Host-Only Ethernet Adapter\nStatus: Up\nIPAddress: 192.168.56.1\nNetworkMask: 255.255.255.0\n\n",
            }
        if joined.endswith("list hdds"):
            return {"ok": True, "returnCode": 0, "output": ""}
        if "showvminfo AR_Base" in joined:
            return {
                "ok": True,
                "returnCode": 0,
                "output": 'hostonlyadapter1="VirtualBox Host-Only Ethernet Adapter"\nCurrentSnapshotName="AR_Base_Link"\n',
            }
        if "showmediuminfo disk" in joined:
            return {"ok": True, "returnCode": 0, "output": "State: created\n"}
        raise AssertionError(command)

    def inspect(self, models, *, healthy, hypervisor):
        with (
            patch("ensp_mcp.preflight.find_ensp", return_value=self.ensp_record()),
            patch("ensp_mcp.preflight.find_vbox", return_value=self.vbox_record()),
            patch("ensp_mcp.preflight._run", side_effect=lambda command, timeout=5.0: self.command_result(command, healthy)),
            patch(
                "ensp_mcp.preflight._windows_virtualization",
                return_value={"hypervisorPresent": hypervisor, "vbsStatus": 2 if hypervisor else 0},
            ),
            patch("ensp_mcp.preflight._recent_hyperv_error", return_value=hypervisor),
        ):
            return preflight.inspect_runtime(models)

    def test_hyperv_and_missing_images_are_precise_blockers(self):
        result = self.inspect(
            ["AR2220", "USG6000V", "USG5500", "NE40E"],
            healthy=False,
            hypervisor=True,
        )
        checks = {item["model"]: item for item in result["modelChecks"]}
        self.assertFalse(result["readyForTopology"])
        self.assertFalse(checks["AR2220"]["ready"])
        self.assertFalse(checks["USG6000V"]["ready"])
        self.assertFalse(checks["NE40E"]["ready"])
        self.assertTrue(checks["USG5500"]["ready"])
        self.assertIn("vfw_usg.vdi", "；".join(checks["USG6000V"]["blockers"]))
        codes = {item["code"] for item in result["issues"]}
        self.assertIn("VBOX_HYPERV_CONFLICT", codes)
        self.assertIn("USG6000V_NOT_READY", codes)
        self.assertIn("SVRP_NOT_READY", codes)

    def test_usg5500_can_be_ready_without_virtual_runtime(self):
        result = self.inspect(["USG5500"], healthy=False, hypervisor=True)
        self.assertTrue(result["readyForTopology"])
        self.assertTrue(result["modelChecks"][0]["ready"])
        self.assertEqual(result["modelChecks"][0]["runtime"], "local_simulator")

    def test_registered_ngfw_image_can_pass_known_preconditions(self):
        database = self.home / "plugin" / "ngfw" / "Database"
        system = self.home / "plugin" / "ngfw" / "System"
        database.mkdir(parents=True)
        system.mkdir(parents=True)
        (database / "vfw_usg.vdi").write_bytes(b"valid image placeholder")
        (system / "system.ini").write_text(
            "[SYSTEM]\nImgFile=image\nRootKey=root\nSupportKey=support\n", encoding="utf-8"
        )
        result = self.inspect(["USG6000V"], healthy=True, hypervisor=False)
        self.assertTrue(result["readyForTopology"])
        check = result["modelChecks"][0]
        self.assertTrue(check["imagePresent"])
        self.assertTrue(check["imageRecognizedByVirtualBox"])
        self.assertTrue(check["registrationFieldsPresent"])
        self.assertTrue(any("实际启动" in item for item in check["warnings"]))

    def test_svrp_requires_image_format_and_registration_fields(self):
        database = self.home / "plugin" / "svrp" / "Database"
        system = self.home / "plugin" / "svrp" / "System"
        database.mkdir(parents=True)
        system.mkdir(parents=True)
        (database / "svrpbox.img").write_bytes(b"valid image placeholder")
        (system / "system.ini").write_text(
            "ImgFile =\nRootKey =\nSupportKey =\n", encoding="utf-8"
        )
        blocked = self.inspect(["NE40E"], healthy=True, hypervisor=False)
        self.assertFalse(blocked["readyForTopology"])
        self.assertIn("注册信息", "；".join(blocked["modelChecks"][0]["blockers"]))

        (system / "system.ini").write_text(
            "ImgFile = image\nRootKey = root\nSupportKey = support\n", encoding="utf-8"
        )
        ready = self.inspect(["NE40E"], healthy=True, hypervisor=False)
        self.assertTrue(ready["readyForTopology"])
        self.assertTrue(ready["modelChecks"][0]["registrationFieldsPresent"])

    def test_runtime_classification(self):
        expected = {
            "AR1220": "virtualbox_ar",
            "AP4050": "virtualbox_wlan_ap",
            "AC6005": "virtualbox_wlan",
            "NE40E": "svrp_image",
            "USG6000V": "ngfw_image",
            "USG5500": "local_simulator",
            "PC": "built_in",
        }
        self.assertEqual({name: preflight.runtime_kind(name) for name in expected}, expected)

    def test_explicit_bad_ensp_path_does_not_fall_back(self):
        result = preflight.find_ensp(str(self.home / "missing"))
        self.assertFalse(result["found"])
        self.assertEqual(result["source"], "argument")

    def test_existing_non_ensp_directory_is_not_accepted(self):
        empty = Path(self.temp.name) / "not-ensp"
        empty.mkdir()
        result = preflight.find_ensp(str(empty))
        self.assertFalse(result["found"])
        self.assertEqual(result["path"], str(empty.resolve()))
        runtime = preflight.inspect_runtime(["PC"], str(empty))
        self.assertFalse(runtime["readyForTopology"])
        self.assertIn("ENSP_NOT_FOUND", {item["code"] for item in runtime["issues"]})

    def test_vbox_directory_without_vboxmanage_is_not_accepted(self):
        with patch.object(Path, "is_file", return_value=False):
            result = preflight.find_vbox()
        self.assertFalse(result["found"])
        self.assertIsNone(result["executable"])

    def test_environment_check_mcp_returns_diagnostic_payload(self):
        fake = {
            "ok": True,
            "version": "1.2.0",
            "readyForTopology": False,
            "runtime": {"issues": [{"code": "example"}]},
        }
        with patch("ensp_mcp.server.doctor.inspect_environment", return_value=fake):
            result = json.loads(server.environment_check("lab.topo", ["AR2220"], "D:/eNSP"))
        self.assertEqual(result, fake)
        self.assertFalse(json.loads(server.environment_check(models="AR2220"))["ok"])
        self.assertFalse(json.loads(server.environment_check(models=[""]))["ok"])


if __name__ == "__main__":
    unittest.main()
