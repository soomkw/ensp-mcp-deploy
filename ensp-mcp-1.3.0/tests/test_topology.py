import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from tests.support import ROOT
from ensp_mcp import catalog, export, layout, render, server, validate, xmlio
from ensp_mcp.topo import DYNAMIC_PORT_LIMIT, Link, Topology, TopologyError
from PIL import Image


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'network.topo')

    def make(self):
        topo = Topology()
        topo.add_device('Router', name='R1')
        topo.add_device('S5700', name='Core-1')
        topo.add_device('PC', name='PC1')
        return topo

    def tool(self, name, **kwargs):
        result = json.loads(getattr(server, name)(**kwargs))
        self.assertTrue(result['ok'], result)
        return result

    def test_all_models_roundtrip(self):
        topo = Topology()
        for model in catalog.MODELS:
            topo.add_device(model)
        layout.apply(topo, mode='grid')
        topo.save(self.path)
        loaded = Topology.load(self.path)
        self.assertEqual(len(loaded.devices), 43)
        self.assertEqual(validate.check(loaded)['errorCount'], 0)
        for a, b in zip(topo.devices, loaded.devices):
            self.assertEqual(a.spec.ports, b.spec.ports, a.model)

    def test_sample_topologies(self):
        for path in (ROOT / 'test').glob('*.topo'):
            topo = Topology.load(str(path))
            self.assertEqual(validate.check(topo)['errors'], [], path)
            topo.save(self.path)
            loaded = Topology.load(self.path)
            self.assertEqual(topo.summary(), loaded.summary())

    def test_preserve_extensions_annotations_and_text(self):
        topo = self.make()
        topo.connect('R1', 'Core-1')
        topo.save(self.path)
        root = xmlio.parse(self.path)
        root.set('version', 'custom-version')
        root.set('custom', 'root-data')
        note = ET.SubElement(root.find('txttips'), 'note', {'text': 'A"B\nC'})
        note.text = '内容 & <tag>'
        ET.SubElement(root.find('shapes'), 'shape', {'colour': 'red'})
        root.append(ET.Comment('keep me'))
        extension = ET.SubElement(root, 'extension')
        extension.text = 'before '
        child = ET.SubElement(extension, 'b')
        child.text = 'bold'
        child.tail = ' after'
        dev = root.find('./devices/dev')
        dev.set('vendor', 'keep')
        ET.SubElement(dev, 'custom-child', {'value': 'keep'})
        pair = root.find('./lines/line/interfacePair')
        pair.set('srcBoundRectIsMoved', '1')
        pair.set('srcOffset_X', '22')
        pair.set('custom', 'keep')
        xmlio.write(self.path, root)
        root = xmlio.parse(self.path)
        topo = Topology.load(self.path)
        topo.add_device('PC')
        topo.save(self.path)
        after = xmlio.parse(self.path)
        self.assertEqual(after.attrib, root.attrib)
        for tag in ('shapes', 'txttips', 'extension'):
            self.assertEqual(ET.tostring(after.find(tag)), ET.tostring(root.find(tag)))
        self.assertTrue(any(n.tag is ET.Comment for n in after))
        self.assertEqual(after.find('./devices/dev').get('vendor'), 'keep')
        self.assertIsNotNone(after.find('./devices/dev/custom-child'))
        self.assertEqual(after.find('./lines/line/interfacePair').attrib, pair.attrib)

    def test_actual_card_ports_preserved_and_used(self):
        topo = Topology()
        topo.add_device('CE6800', name='CE1')
        topo.save(self.path)
        root = xmlio.parse(self.path)
        slot = root.find('./devices/dev/slot')
        slot[:] = [ET.Element('interface', {'category':'Ethernet', 'type':'GE', 'slotIndex':'3', 'cardIndex':'0', 'interfaceIndex':'8'})]
        xmlio.write(self.path, root)
        topo = Topology.load(self.path)
        self.assertEqual(topo.find('CE1').spec.port_count, 1)
        self.assertEqual(topo.resolve_port(topo.find('CE1'), 'GE3/0/8').index, 0)
        topo.save(self.path)
        self.assertEqual(xmlio.parse(self.path).find('./devices/dev/slot/interface').get('slotIndex'), '3')

    def test_multiple_interface_pairs_not_lost(self):
        topo = self.make()
        topo.connect('R1', 'Core-1')
        topo.save(self.path)
        root = xmlio.parse(self.path)
        line = root.find('./lines/line')
        pair = copy.deepcopy(line.find('interfacePair'))
        pair.set('srcIndex', '1')
        pair.set('tarIndex', '1')
        line.append(pair)
        xmlio.write(self.path, root)
        topo = Topology.load(self.path)
        self.assertEqual(len(topo.links), 2)
        topo.save(self.path)
        self.assertEqual(len(Topology.load(self.path).links), 2)

    def test_external_edit_rejected(self):
        topo = self.make()
        topo.save(self.path)
        original = Path(self.path).read_bytes() + b'\r\n'
        Path(self.path).write_bytes(original)
        with self.assertRaisesRegex(TopologyError, '其他程序'):
            topo.save(self.path)
        self.assertEqual(Path(self.path).read_bytes(), original)

    def test_failed_atomic_replace_preserves_file(self):
        xmlio.atomic_write(self.path, 'original')
        with patch('ensp_mcp.xmlio.os.replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                xmlio.atomic_write(self.path, 'new')
        self.assertEqual(Path(self.path).read_text(), 'original')
        self.assertEqual(list(Path(self.temp.name).glob('.ensp-*')), [])

    def test_utf16_and_utf8(self):
        for encoding in ('utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be'):
            Path(self.path).write_bytes((xmlio.XML_DECLARATION + '<topo><devices/><lines/><shapes/><txttips/></topo>').encode(encoding))
            self.assertEqual(Topology.load(self.path).summary()['deviceCount'], 0)

    def test_invalid_root_rejected(self):
        Path(self.path).write_text('<document/>')
        with self.assertRaises(ValueError):
            Topology.load(self.path)

    def test_serial_to_ethernet_rejected(self):
        topo = self.make()
        with self.assertRaises(TopologyError):
            topo.connect('R1', 'PC1', 'Serial0/0/0')
        self.assertEqual(len(topo.links), 0)

    def test_serial_connection_and_wrong_cable(self):
        topo = self.make()
        topo.add_device('Router', name='R2')
        link, _, _ = topo.connect('R1', 'R2', 'Serial0/0/0', 'S0/0/0')
        self.assertEqual(link.line_type, 'Serial')
        with self.assertRaises(TopologyError):
            topo.connect('R1', 'R2', 'S0/0/1', 'S0/0/1', 'Copper')

    def test_invalid_links_detected_on_load(self):
        topo = self.make()
        topo.links.append(Link(topo.find('R1').dev_id, topo.find('PC1').dev_id, 6, 0, 'Serial'))
        self.assertGreater(validate.check(topo)['errorCount'], 0)

    def test_existing_auto_line_type_is_accepted(self):
        topo = self.make()
        topo.connect('Core-1', 'PC1')
        topo.links[0].line_type = 'Auto'
        self.assertEqual(validate.check(topo)['errorCount'], 0)

    def test_cloud_exhaustion_and_negative_port(self):
        topo = Topology()
        cloud = topo.add_device('Cloud')
        for i in range(DYNAMIC_PORT_LIMIT):
            name = topo.add_device('PC').name
            topo.connect(cloud.name, name)
        with self.assertRaises(TopologyError):
            topo.resolve_port(cloud, None)
        with self.assertRaises(TopologyError):
            topo.resolve_port(cloud, '-1')

    def test_duplicate_ids_and_invalid_console(self):
        topo = self.make()
        topo.devices[1].dev_id = topo.devices[0].dev_id
        topo.devices[0].com_port = 70000
        self.assertGreaterEqual(validate.check(topo)['errorCount'], 2)
        with self.assertRaises(TopologyError):
            topo.save(self.path)

    def test_mac_override_consistent(self):
        topo = Topology()
        device = topo.add_device('AP4050', settings={'mac': '00:11:22:33:44:55'})
        self.assertEqual(device.system_mac, '00-11-22-33-44-55')
        self.assertIn(device.system_mac, device.settings)

    def test_bad_settings_rejected(self):
        for settings in ({'ip':'999.1.1.1'}, {'typo':1}, {'mac':'bad'}, {'mask':'255.0.255.0'}):
            with self.assertRaises(ValueError):
                Topology().add_device('PC', settings=settings)

    def test_drawio_quotes_and_newlines(self):
        topo = Topology()
        topo.add_device('S5700', name='Core"A&<B>')
        root = ET.fromstring(export.to_drawio(topo))
        cell = root.find('.//mxCell[@vertex="1"]')
        self.assertEqual(cell.get('value'), 'Core"A&<B>\nS5700')

    def test_mermaid_reserved_names_and_escaping(self):
        topo = Topology()
        topo.add_device('S5700', name='end')
        topo.add_device('PC', name='A"[x]')
        content = export.to_mermaid(topo)
        self.assertIn('n_end[', content)
        self.assertIn('#34;', content)
        self.assertIn('#91;', content)

    def test_batch_failure_does_not_partially_save(self):
        self.tool('topo_create', path=self.path)
        before = Path(self.path).read_bytes()
        result = json.loads(server.topo_add_devices(self.path, [{'model':'PC'}, {'model':'unknown'}]))
        self.assertFalse(result['ok'])
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_invalid_counts(self):
        self.tool('topo_create', path=self.path)
        for count in (0, -1, 1.5, True, 1001):
            result = json.loads(server.topo_add_devices(self.path, [{'model':'PC', 'count':count}]))
            self.assertFalse(result['ok'], count)

    def test_full_offline_flow(self):
        self.tool('topo_create', path=self.path)
        self.tool('topo_add_devices', path=self.path, devices=[{'model':'S5700','name':'Core'}, {'model':'PC','count':2}])
        self.tool('topo_connect', path=self.path, links=[{'from':'Core','to':'PC1','fromPort':'Gi0/0/1'}, {'from':'Core','to':'PC2'}])
        for mode in layout.MODES:
            self.tool('topo_layout', path=self.path, mode=mode)
        self.assertEqual(self.tool('topo_validate', path=self.path)['errorCount'], 0)
        image = self.tool('topo_render', path=self.path)
        with Image.open(image['path']) as im:
            im.verify()
        for fmt in export.FORMATS:
            self.tool('topo_export', path=self.path, format=fmt, out_path=str(Path(self.temp.name) / 'nested' / ('export.' + fmt)))
        self.tool('topo_remove', path=self.path, devices=['PC1'])
        self.assertEqual(self.tool('topo_inspect', path=self.path)['linkCount'], 1)

    def test_invalid_layout_and_scale(self):
        topo = self.make()
        with self.assertRaises(ValueError):
            layout.apply(topo, 'grid', columns=-1)
        for scale in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                render.render(topo, str(Path(self.temp.name) / 'x.png'), scale)


if __name__ == '__main__':
    unittest.main()
