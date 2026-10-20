import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import provider_ownership as owner


class Ownership(unittest.TestCase):
    def test_no_synthetic_port_or_duplicate_tool(self):
        rows = [{"name": "jq", "provider": "macports", "kind": "port",
                 "installation_id": "macports:jq"}]
        registry = {"routes": [{"logical_id": "jq", "source_installation_id": "homebrew:formula:jq",
                                "target_installation_id": "macports:jq", "status": "committed"}],
                    "legacy": [], "error": None}
        merged, issues = owner.merge_inventory(rows, registry)
        self.assertFalse(issues)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["kind"], "port")

    def test_old_route_is_preserved_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migrations" / "routes.json"
            owner.atomic_write_json(path, {"jq": {"provider": "macports"}})
            state = owner.load_routes(temp)
            self.assertEqual(len(state["legacy"]), 1)
            self.assertEqual(state["routes"], [])

    def test_broken_file_does_not_wipe_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migrations" / "routes.json"
            path.parent.mkdir(parents=True)
            path.write_text("{ invalid")
            state = owner.load_routes(temp)
            merged, issues = owner.merge_inventory([{"name": "jq", "kind": "formula"}], state)
            self.assertEqual(len(merged), 1)
            self.assertTrue(issues)

    def test_route_commit_requires_journal_and_physical_target(self):
        with tempfile.TemporaryDirectory() as temp:
            route = {"logical_id": "jq", "source_installation_id": "homebrew:formula:jq",
                     "target_installation_id": "macports:jq", "status": "committed",
                     "transaction_id": "transaction1"}
            journal = Path(temp) / "journal.json"
            journal.write_text("{}")
            with self.assertRaises(ValueError):
                owner.commit_route(temp, route, journal, [])
            row = {"name": "jq", "provider": "macports", "kind": "port"}
            saved = owner.commit_route(temp, route, journal, [row])
            self.assertEqual(saved["generation"], 1)
            self.assertEqual(len(owner.load_routes(temp)["routes"]), 1)


if __name__ == "__main__":
    unittest.main()
