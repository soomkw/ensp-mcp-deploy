import concurrent.futures
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET

from tests.support import ROOT
from ensp_mcp import catalog, server, xmlio
from ensp_mcp.doctor import inspect_environment
from ensp_mcp.topo import Topology


class DiagnosticTests(unittest.TestCase):
    def test_doctor_module_entrypoint(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'ensp_mcp.doctor'],
            env={**os.environ, 'PYTHONPATH':str(ROOT / 'src'), 'PYTHONIOENCODING':'utf-8'},
            capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['version'], '1.2.0')

    def test_doctor_missing_topology_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-B', '-m', 'ensp_mcp.doctor', '--topo', str(Path(directory) / 'missing.topo')],
                env={**os.environ, 'PYTHONPATH':str(ROOT / 'src'), 'PYTHONIOENCODING':'utf-8'},
                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['ok'])

    def test_runtime_and_version(self):
        status = inspect_environment()
        self.assertTrue(status['ok'])
        self.assertEqual(status['version'], '1.2.0')
        self.assertEqual(status['modelCount'], 43)

    def test_same_count_different_card_slot_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'custom.topo')
            topo = Topology()
            topo.add_device('CE6800')
            topo.save(path)
            root = xmlio.parse(path)
            root.find('./devices/dev/slot/interface').set('slotIndex', '4')
            xmlio.write(path, root)
            self.assertTrue(catalog.diff_against_builtin(catalog.learn_models_from_topo(path)))
            result = json.loads(server.list_models(model='CE6800', topo_path=path))
            self.assertTrue(result['catalogCheck']['differences'])

    def test_same_model_second_variant_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'variants.topo')
            topo = Topology()
            topo.add_device('S5700')
            topo.add_device('S5700')
            topo.save(path)
            root = xmlio.parse(path)
            root.findall('./devices/dev')[1].find('slot/interface').set('count', '4')
            xmlio.write(path, root)
            learned = catalog.learn_models_from_topo(path)
            self.assertEqual(len(learned['S5700']['interfaceVariants']), 2)
            self.assertTrue(catalog.diff_against_builtin(learned))

    def test_concurrent_mcp_edits_do_not_lose_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'concurrent.topo')
            server.topo_create(path)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda index: json.loads(server.topo_add_devices(path, [{'model':'PC', 'name':f'PC{index}'}])), range(20)))
            self.assertTrue(all(result['ok'] for result in results), results)
            self.assertEqual(len(Topology.load(path).devices), 20)

    def test_e2e_lab_generation_and_overwrite_protection(self):
        spec = importlib.util.spec_from_file_location('lab_example', ROOT / 'examples/e2e_lab.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            result = module.create(Path(directory))
            self.assertTrue(result['ok'])
            self.assertEqual(inspect_environment(result['topology'])['topology']['deviceCount'], 2)
            self.assertTrue((Path(directory) / 'MCP-LSW1.cfg').exists())
            with self.assertRaises(ValueError):
                module.create(Path(directory))
