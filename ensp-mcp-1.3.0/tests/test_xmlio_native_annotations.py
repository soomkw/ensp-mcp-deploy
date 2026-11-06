import tempfile
import unittest
from pathlib import Path

from ensp_mcp import xmlio
from ensp_mcp.topo import Topology


SHAPE_ATTRS = {
    "type": "1",
    "filloption": "1",
    "color": "16777088",
    "upleftcorner": "21,205",
    "width": "356",
    "height": "364",
    "startpt": "0,0",
    "endpt": "0,0",
    "borderStyle": "5",
    "borderWidth": "1",
}

TXTTIP_ATTRS = {
    "left": "33",
    "top": "213",
    "right": "123",
    "bottom": "245",
    "content": "核心区\r\nArea 0",
    "fontname": "Consolas",
    "fontstyle": "0",
    "editsize": "100",
    "txtcolor": "-16777216",
    "txtbkcolor": "-7278960",
    "charset": "1",
}


class NativeAnnotationXmlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cp936-annotations.topo"

    def test_cp936_native_shape_and_txttip_roundtrip(self):
        source = """<?xml version="1.0" encoding="UNICODE" ?>
<topo version="1.2.00.390">
    <devices />
    <lines />
    <shapes>
        <shape type="1" filloption="1" color="16777088" upleftcorner="21,205" width="356" height="364" startpt="0,0" endpt="0,0" borderStyle="5" borderWidth="1" />
    </shapes>
    <txttips>
        <txttip left="33" top="213" right="123" bottom="245" content="核心区&#x0D;&#x0A;Area 0" fontname="Consolas" fontstyle="0" editsize="100" txtcolor="-16777216" txtbkcolor="-7278960" charset="1" />
    </txttips>
</topo>
"""
        self.path.write_bytes(source.encode("cp936"))

        root = xmlio.parse(str(self.path))
        self.assertEqual([child.tag for child in root], ["devices", "lines", "shapes", "txttips"])
        self.assertEqual(root.find("./shapes/shape").attrib, SHAPE_ATTRS)
        self.assertEqual(root.find("./txttips/txttip").attrib, TXTTIP_ATTRS)

        topology = Topology.load(str(self.path))
        topology.save(str(self.path))
        after = xmlio.parse(str(self.path))
        self.assertEqual(after.find("./shapes/shape").attrib, SHAPE_ATTRS)
        self.assertEqual(after.find("./txttips/txttip").attrib, TXTTIP_ATTRS)


if __name__ == "__main__":
    unittest.main()
