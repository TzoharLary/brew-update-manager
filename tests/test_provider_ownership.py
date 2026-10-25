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

    def test_rollback_restores_previous_route_and_preserves_other_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            source_id = 'homebrew:formula:jq'
            before = {'logical_id': 'jq', 'source_installation_id': source_id,
                      'transaction_id': 'old', 'generation': 4}
            other = {'logical_id': 'other', 'source_installation_id': 'other'}
            owner.atomic_write_json(Path(temp)/'migrations/routes.json', {
                'schema_version': 1, 'routes': [before, other]})
            journal_path = Path(temp)/'journal.json'
            owner.atomic_write_json(journal_path, {'transaction_id': 'new', 'plan': {
                'profile': 'jq', 'source': {'installation_id': source_id}}})
            route = {'logical_id': 'jq', 'source_installation_id': source_id,
                     'target_installation_id': 'pixi:jq', 'status': 'committed',
                     'transaction_id': 'new'}
            owner.commit_route(temp, route, journal_path, [{'installation_id': 'pixi:jq'}])
            journal = json.loads(journal_path.read_text())
            owner.rollback_route(temp, journal)
            owner.rollback_route(temp, journal)
            rows = owner.load_routes(temp)['routes']
            self.assertEqual(rows, [other, before])

    def test_rollback_refuses_a_changed_committed_record(self):
        with tempfile.TemporaryDirectory() as temp:
            after = {'logical_id': 'jq', 'source_installation_id': 'homebrew:formula:jq',
                     'transaction_id': 'new', 'generation': 1}
            path = Path(temp)/'migrations/routes.json'
            owner.atomic_write_json(path, {'schema_version': 1, 'routes': [dict(after, generation=2)]})
            journal = {'transaction_id': 'new', 'ownership_before': None, 'ownership_after': after,
                       'plan': {'profile': 'jq', 'source': {'installation_id': 'homebrew:formula:jq'}}}
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'changed_independently'):
                owner.rollback_route(temp, journal)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
