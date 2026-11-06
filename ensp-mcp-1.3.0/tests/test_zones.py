import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from ensp_mcp import annotations, server, validate, xmlio
from ensp_mcp.topo import Topology, TopologyError


class ZoneLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "zones.topo")

    def make(self) -> Topology:
        topo = Topology()
        for name, model in (
            ("总部核心交换机-Primary-Long-Name", "S5700"),
            ("总部办公终端一", "PC"),
            ("总部办公终端二", "PC"),
            ("分支核心交换机", "S5700"),
            ("分支业务终端-A", "PC"),
            ("临时测试终端", "PC"),
        ):
            topo.add_device(model, name=name)
        return topo

    def zones(self):
        return [
            {
                "title": '总部核心区 & "Campus" <A>',
                "devices": ["总部核心交换机-Primary-Long-Name", "总部办公终端一", "总部办公终端二"],
                "color": "#E8F1FF",
                "columns": 2,
            },
            {
                "name": "分支区",
                "devices": ["分支核心交换机", "分支业务终端-A"],
                "color": "#EAf8eE",
            },
        ]

    def test_native_zone_schema_geometry_and_roundtrip(self):
        topo = self.make()
        result = annotations.apply_zones(topo, self.zones())
        self.assertEqual(result["zoneCount"], 3)  # 自动加入未分区
        self.assertEqual(result["zones"][1]["color"], "#EAF8EE")
        self.assertEqual(result["unassigned"], [])
        self.assertEqual(validate.check(topo)["errors"], [])
        collision_warnings = [
            item for item in validate.check(topo)["warnings"] if "重叠" in item
        ]
        self.assertEqual(collision_warnings, [])

        topo.save(self.path)
        root = xmlio.parse(self.path)
        shapes = root.findall("./shapes/shape")
        tips = root.findall("./txttips/txttip")
        self.assertEqual(len(shapes), 3)
        self.assertEqual(len(tips), 3)
        self.assertEqual(
            set(shapes[0].attrib),
            {
                "type", "filloption", "color", "upleftcorner", "width", "height",
                "startpt", "endpt", "borderStyle", "borderWidth",
            },
        )
        self.assertEqual(shapes[0].get("color"), str(0x00FFF1E8))
        self.assertEqual(shapes[0].get("borderStyle"), "5")
        self.assertEqual(
            set(tips[0].attrib),
            {
                "left", "top", "right", "bottom", "content", "fontname", "fontstyle",
                "editsize", "txtcolor", "txtbkcolor", "charset",
            },
        )
        self.assertEqual(tips[0].get("content"), '总部核心区 & "Campus" <A>')
        self.assertEqual(tips[0].get("fontname"), "Consolas")

        first = Path(self.path).read_bytes()
        loaded = Topology.load(self.path)
        annotations.apply_zones(loaded, self.zones(), replace_annotations=True)
        loaded.save(self.path)
        self.assertEqual(Path(self.path).read_bytes(), first)

    def test_existing_annotations_require_explicit_replacement(self):
        topo = self.make()
        topo.save(self.path)
        root = xmlio.parse(self.path)
        ET.SubElement(root.find("shapes"), "shape", {"type": "2", "filloption": "0"})
        xmlio.write(self.path, root)
        before = Path(self.path).read_bytes()
        result = json.loads(server.topo_layout(self.path, zones=self.zones()))
        self.assertFalse(result["ok"])
        self.assertIn("replace_annotations", result["error"])
        self.assertEqual(Path(self.path).read_bytes(), before)

        result = json.loads(
            server.topo_layout(self.path, zones=self.zones(), replace_annotations=True)
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(xmlio.parse(self.path).findall("./shapes/shape")), 3)

    def test_plain_layout_protects_existing_annotations(self):
        topo = self.make()
        annotations.apply_zones(topo, self.zones())
        topo.save(self.path)
        before = Path(self.path).read_bytes()
        refused = json.loads(server.topo_layout(self.path, mode="grid"))
        self.assertFalse(refused["ok"])
        self.assertIn("标注错位", refused["error"])
        self.assertEqual(Path(self.path).read_bytes(), before)
        allowed = json.loads(
            server.topo_layout(self.path, mode="grid", allow_annotation_desync=True)
        )
        self.assertTrue(allowed["ok"], allowed)

    def test_duplicate_membership_bad_values_and_no_partial_write(self):
        topo = self.make()
        topo.save(self.path)
        cases = [
            [
                {"title": "甲", "devices": ["总部办公终端一"]},
                {"title": "乙", "devices": ["总部办公终端一"]},
            ],
            [{"title": "甲", "devices": ["不存在"]}],
            [{"title": "甲", "devices": ["总部办公终端一"], "color": "red"}],
            [{"title": "\n", "devices": ["总部办公终端一"]}],
            [{"title": "甲", "devices": ["总部办公终端一"], "columns": -1}],
        ]
        for zones in cases:
            before = Path(self.path).read_bytes()
            result = json.loads(server.topo_layout(self.path, zones=zones))
            self.assertFalse(result["ok"], zones)
            self.assertEqual(Path(self.path).read_bytes(), before)

    def test_unassigned_can_stay_outside_panels_without_overlap(self):
        topo = self.make()
        result = annotations.apply_zones(
            topo, self.zones(), include_unassigned=False
        )
        self.assertEqual(result["zoneCount"], 2)
        self.assertEqual(result["unassigned"], ["临时测试终端"])
        self.assertFalse(any("重叠" in warning for warning in validate.check(topo)["warnings"]))

    def test_layout_resets_stale_interface_label_offsets(self):
        topo = Topology()
        topo.add_device("S5700", name="A")
        topo.add_device("S5700", name="B")
        topo.connect("A", "B")
        topo.save(self.path)
        root = xmlio.parse(self.path)
        pair = root.find("./lines/line/interfacePair")
        pair.set("srcBoundRectIsMoved", "1")
        pair.set("srcOffset_X", "999")
        pair.set("srcOffset_Y", "999")
        pair.set("tarBoundRectIsMoved", "1")
        pair.set("tarOffset_X", "999")
        pair.set("tarOffset_Y", "999")
        xmlio.write(self.path, root)
        result = json.loads(server.topo_layout(self.path, mode="grid"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["linkLabelsReset"], 1)
        pair = xmlio.parse(self.path).find("./lines/line/interfacePair")
        self.assertEqual(pair.get("srcBoundRectIsMoved"), "0")
        self.assertEqual(pair.get("tarBoundRectIsMoved"), "0")
        self.assertEqual(pair.get("srcOffset_X"), "0.000000")
        self.assertNotEqual(pair.get("srcBoundRect_X"), "0.000000")

    def test_invalid_native_annotation_is_validation_error(self):
        topo = self.make()
        ET.SubElement(
            topo.root.find("shapes"),
            "shape",
            {"type": "1", "upleftcorner": "bad", "width": "10", "height": "20"},
        )
        result = validate.check(topo)
        self.assertFalse(result["ok"])
        self.assertTrue(any("矩形标注" in error for error in result["errors"]))

    def test_zero_area_native_rectangle_is_only_a_warning(self):
        topo = self.make()
        ET.SubElement(
            topo.root.find("shapes"),
            "shape",
            {"type": "1", "upleftcorner": "10,20", "width": "0", "height": "0"},
        )
        result = validate.check(topo)
        self.assertTrue(result["ok"])
        self.assertTrue(any("面积为零" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
