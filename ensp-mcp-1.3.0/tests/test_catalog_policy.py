import json
import tempfile
import unittest
from pathlib import Path

from ensp_mcp import catalog, server
from ensp_mcp.topo import Topology


class CatalogPolicyTests(unittest.TestCase):
    def test_firewall_default_and_selection_tiers(self):
        self.assertEqual(catalog.CATEGORY_DEFAULTS["FW"], "USG5500")

        preferred = catalog.advisory("USG5500")
        self.assertTrue(preferred["defaultForCategory"])
        self.assertEqual(preferred["categoryDefault"], "USG5500")
        self.assertEqual(preferred["selectionTier"], "preferred")
        self.assertFalse(preferred["externalImageRequired"])
        self.assertEqual(preferred["startupRisk"], "normal")
        self.assertIsNone(preferred["fallbackModel"])

        conditional = catalog.advisory("USG6000V")
        self.assertFalse(conditional["defaultForCategory"])
        self.assertEqual(conditional["selectionTier"], "conditional")
        self.assertTrue(conditional["externalImageRequired"])
        self.assertEqual(conditional["startupRisk"], "high")
        self.assertEqual(conditional["fallbackModel"], "USG5500")
        self.assertEqual(conditional["recommendedAlternative"], "USG5500")
        self.assertTrue(conditional["runtimePrerequisites"])
        self.assertEqual(conditional["runtimeType"], "ngfw_image")

    def test_advisory_is_case_insensitive_and_returns_fresh_data(self):
        first = catalog.advisory("usg6000v")
        first["runtimePrerequisites"].append("mutated by caller")
        second = catalog.advisory("USG6000V")

        self.assertEqual(first["model"], "USG6000V")
        self.assertNotIn("mutated by caller", second["runtimePrerequisites"])

    def test_unclassified_model_uses_conservative_defaults(self):
        policy = catalog.advisory("S5700")
        self.assertEqual(policy["selectionTier"], "standard")
        self.assertIsNone(policy["externalImageRequired"])
        self.assertEqual(policy["startupRisk"], "unknown")
        self.assertIsNone(policy["categoryDefault"])
        self.assertFalse(policy["defaultForCategory"])

        router = catalog.advisory("AR2220")
        self.assertEqual(router["runtimeType"], "virtualbox_ar")
        self.assertEqual(router["startupRisk"], "environment-dependent")
        self.assertTrue(router["runtimePrerequisites"])

        svrp = catalog.advisory("NE40E")
        self.assertTrue(svrp["externalImageRequired"])
        self.assertEqual(svrp["runtimeType"], "svrp_image")

    def test_sort_helpers_put_firewall_default_first(self):
        names = [model.name for model in catalog.sort_models(["USG6000V", "USG5500"])]
        self.assertEqual(names, ["USG5500", "USG6000V"])

        firewalls = [model for model in catalog.sort_models() if model.category == "FW"]
        self.assertEqual([model.name for model in firewalls], ["USG5500", "USG6000V"])
        self.assertLess(
            catalog.model_sort_key("USG5500"),
            catalog.model_sort_key(catalog.get_model("USG6000V")),
        )

    def test_category_defaults_reference_real_models(self):
        for category, name in catalog.CATEGORY_DEFAULTS.items():
            model = catalog.get_model(name)
            self.assertEqual(model.category, category)
            self.assertTrue(catalog.advisory(name)["defaultForCategory"])

    def test_existing_model_api_and_fields_remain_compatible(self):
        custom = catalog.Model("Example", "OTHER", "legacy", (), None, False, None)
        self.assertEqual(custom.groups, ())
        self.assertEqual(custom.cards, ())
        self.assertEqual(custom.note, "")

        described = catalog.get_model("USG5500").describe()
        for key in (
            "model",
            "category",
            "categoryLabel",
            "portCount",
            "hasConsole",
            "slotFormat",
            "ports",
            "note",
        ):
            self.assertIn(key, described)
        self.assertNotIn("advisory", described)
        self.assertNotIn("建议改用 USG6000V", described["note"])

    def test_invalid_advisory_model_matches_catalog_lookup_errors(self):
        with self.assertRaises(KeyError):
            catalog.advisory("not-a-model")
        with self.assertRaises(ValueError):
            catalog.advisory(None)

    def test_mcp_catalog_and_explicit_conditional_model(self):
        listed = json.loads(server.list_models(category="FW"))
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["categoryDefaults"], {"FW": "USG5500"})
        self.assertEqual([item["model"] for item in listed["models"]], ["USG5500", "USG6000V"])
        self.assertEqual(listed["models"][1]["advisory"]["selectionTier"], "conditional")

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "firewall.topo")
            server.topo_create(path)
            created = json.loads(
                server.topo_add_devices(path, [{"model": "USG6000V", "name": "FW1"}])
            )
            self.assertTrue(created["ok"], created)
            self.assertEqual(created["added"][0]["model"], "USG6000V")
            self.assertEqual(created["advisories"][0]["recommendedAlternative"], "USG5500")
            loaded = Topology.load(path)
            self.assertEqual(loaded.find("FW1").spec.slot_format, "card")
            self.assertEqual(loaded.find("FW1").spec.port_count, 8)
            checked = json.loads(server.topo_validate(path))
            self.assertTrue(checked["ok"])
            self.assertEqual(len(checked["advisories"]), 1)


if __name__ == "__main__":
    unittest.main()
