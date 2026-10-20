import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import update_selection as selection


class ProviderSelection(unittest.TestCase):
    def source(self, **changes):
        base = {"name": "jq", "logical_id": "jq", "provider": "homebrew",
                "kind": "formula", "installed_version": "1.2",
                "discovery_complete": True,
                "classification": {"category": "standalone_candidate",
                                   "gaps": [], "consumers": []}}
        return dict(base, **changes)

    def candidate(self, provider="homebrew", upstream="1.3", action="update", **changes):
        base = {"schema_version": 1, "logical_id": "jq",
                "source_installation_id": "homebrew:formula:jq",
                "provider": provider, "provider_names": ["jq"],
                "upstream_version": upstream, "package_version": upstream,
                "revision": 0, "action": action,
                "availability": "verified", "execution_supported": True,
                "profile_id": "jq", "profile_version": 1,
                "system": {"os": "macos", "arch": "x86_64", "release": "15"},
                "closure": [], "closure_verified": True, "blockers": []}
        if action == "migrate":
            base.update(migration_plan_id="a"*32, migration_fingerprint="b"*64)
        return dict(base, **changes)

    def test_numeric_normalization_and_unknown_suffix(self):
        self.assertEqual(selection.compare_upstream("v1.2", "1.2.0"), 0)
        self.assertIsNone(selection.compare_upstream("1.2rc1", "1.2"))
        self.assertIsNone(selection.compare_upstream("1.2_3", "1.2"))

    def test_selects_newest_verified_route_from_three_providers(self):
        routes = [self.candidate(),
                  self.candidate("macports", "1.4", "migrate"),
                  self.candidate("pixi", "1.5", "migrate")]
        decision, = selection.select([self.source()], routes)
        self.assertEqual((decision["target_provider"], decision["status"]),
                         ("pixi", "ready"))

    def test_migration_is_not_ready_without_verified_transaction_identifiers(self):
        for missing in ("migration_plan_id", "migration_fingerprint"):
            route = self.candidate("pixi", "1.8", "migrate")
            route.pop(missing)
            decision, = selection.select([self.source()], [route])
            self.assertNotEqual(decision["status"], "ready")
            self.assertIn("migration_transaction_plan_missing",
                          decision["alternatives"][0]["reason_codes"])

    def test_migration_rejects_invalid_plan_identifiers(self):
        route = self.candidate("pixi", "1.8", "migrate",
                               migration_plan_id="../../../etc/passwd")
        decision, = selection.select([self.source()], [route])
        self.assertEqual(decision["status"], "blocked")

    def test_tie_keeps_current_provider(self):
        routes = [self.candidate("macports", "1.3", "migrate"), self.candidate()]
        decision, = selection.select([self.source()], routes)
        self.assertEqual(decision["target_provider"], "homebrew")

    def test_newer_blocked_route_does_not_hide_current_update(self):
        routes = [self.candidate(), self.candidate("macports", "9", "migrate",
                                                   closure_verified=False)]
        decision, = selection.select([self.source()], routes)
        self.assertEqual(decision["target_provider"], "homebrew")
        self.assertEqual(len(decision["alternatives"]), 2)

    def test_revision_update_on_same_provider(self):
        source = self.source(installed_version="1.2", revision=0)
        decision, = selection.select([source], [self.candidate(upstream="1.2", revision=1)])
        self.assertEqual(decision["status"], "ready")

    def test_unknown_version_does_not_guess(self):
        decision, = selection.select([self.source()], [self.candidate(upstream="2.0-beta")])
        self.assertEqual(decision["status"], "unverified")

    def test_downgrade_is_not_selected(self):
        decision, = selection.select([self.source()], [self.candidate(upstream="1.1")])
        self.assertEqual(decision["status"], "blocked")

    def test_migration_requires_current_standalone_classification(self):
        decision, = selection.select([self.source(classification={"category": "shared"})],
                                     [self.candidate("macports", "2", "migrate")])
        self.assertEqual(decision["status"], "blocked")

    def test_excluded_package_cannot_execute(self):
        source = self.source(name="ngrok", logical_id="ngrok")
        decision, = selection.select([source], [])
        self.assertEqual(decision["status"], "blocked")

    def test_provider_failure_remains_partial(self):
        failed = self.candidate("pixi", "unknown", "migrate", availability="unverified")
        decision, = selection.select([self.source()], [failed, self.candidate()])
        self.assertEqual(decision["status"], "ready")
        self.assertIn("partial_provider_comparison", decision["reason_codes"])

    def test_fingerprint_ignores_observation_times_not_material_changes(self):
        a = self.candidate(checked_at="first", evidence_refs=["a"])
        b = self.candidate(checked_at="second", evidence_refs=["b"])
        first = selection.material_fingerprint(self.source(), a)
        self.assertEqual(first, selection.material_fingerprint(self.source(), b))
        b["package_version"] = "1.3_2"
        self.assertNotEqual(first, selection.material_fingerprint(self.source(), b))

    def test_cross_provider_revision_does_not_override_current_owner_on_upstream_tie(self):
        source = self.source(installed_upstream_version="1.2", installed_revision=7)
        port = self.candidate("macports", "1.3", "migrate", revision=999)
        home = self.candidate("homebrew", "1.3", "update", revision=0)
        chosen, = selection.select([source], [port, home])
        self.assertEqual(chosen["target_provider"], "homebrew")

    def test_installed_upstream_from_receipt_preserves_revision_update(self):
        source = self.source(installed_version="1.2_5", installed_upstream_version="1.2",
                             installed_revision=5)
        chosen, = selection.select([source], [self.candidate(upstream="1.2", revision=6)])
        self.assertEqual(chosen["status"], "ready")

    def test_cross_provider_lower_upstream_cannot_win_due_to_revision(self):
        source = self.source()
        home = self.candidate(upstream="1.4", revision=0)
        port = self.candidate("macports", "1.3", "migrate", revision=999)
        chosen, = selection.select([source], [home, port])
        self.assertEqual(chosen["target_provider"], "homebrew")

    def test_two_physical_installations_never_merge_by_logical_id(self):
        a = self.source()
        b = self.source(provider="macports", kind="port", installation_id="macports:jq")
        decisions = selection.select([a, b], [self.candidate()])
        self.assertEqual(len(decisions), 2)
        self.assertNotEqual(decisions[0]["source_installation_id"],
                            decisions[1]["source_installation_id"])


if __name__ == "__main__":
    unittest.main()
