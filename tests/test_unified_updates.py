import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import unified_updates as u
import update_selection as sel


class FakeService:
    def __init__(self, root):
        self.DATA_DIR = Path(root)
        self.TOOLS_ROOT = Path(root)
    def read_json(self, path):
        import json
        return json.loads(Path(path).read_text()) if Path(path).is_file() else None


class Plans(unittest.TestCase):
    def test_stored_plan_and_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            plan = {"id": "a"*32, "expires_at": time.time()-1, "decisions": []}
            from provider_ownership import atomic_write_json
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"), plan)
            self.assertEqual(u.read_plan(s, "a"*32)["id"], "a"*32)
            with self.assertRaisesRegex(u.PlanError, "plan_expired"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": "a"*32,
                                       "decisions": []})

    def test_request_rejects_changed_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            d = {"decision_id": "c"*32, "material_fingerprint": "hash",
                 "status": "ready", "action": "update"}
            from provider_ownership import atomic_write_json
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"),
                              {"id": "a"*32, "expires_at": time.time()+600,
                               "decisions": [d]})
            with self.assertRaisesRegex(u.PlanError, "plan_changed"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": "a"*32,
                                       "decisions": [{"decision_id": "c"*32,
                                                      "material_fingerprint": "wrong"}]})

    def test_legacy_never_authorizes_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            from provider_ownership import atomic_write_json
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"),
                              {"id": "a"*32, "expires_at": time.time()+600,
                               "decisions": []})
            with self.assertRaisesRegex(u.PlanError, "review_required"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": "a"*32,
                                       "name": "include-what-you-use", "kind": "formula"})

    def test_installation_ids_remain_provider_specific(self):
        self.assertEqual(u._ids({"name": "jq", "kind": "port", "provider": "macports"}),
                         "macports:jq")
        self.assertEqual(u._ids({"name": "jq", "kind": "formula", "provider": "homebrew"}),
                         "homebrew:formula:jq")


    def test_cached_supplemental_classification_is_preserved_for_exact_source(self):
        from datetime import datetime, timezone, timedelta
        from provider_ownership import atomic_write_json
        import installation_evidence as evidence
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.STATE_FILE = Path(temp) / "state.json"
            s.raise_if_cancel_requested = lambda: None
            s.is_excluded = lambda name, kind: False
            audit = {"status": "verified", "active_keg": "/fixture/jq"}
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installation_id": "homebrew:formula:jq", "installed_version": "1.7",
                   "inventory_verification": audit}
            classification = {"category": "standalone_candidate",
                              "inventory_fingerprint": evidence.fingerprint(audit),
                              "checked_at": datetime.now(timezone.utc).isoformat(),
                              "gaps": [], "consumers": [], "shared_roles": []}
            atomic_write_json(s.STATE_FILE, {
                "packages": [dict(row, classification=classification)], "migrations": []})
            offers = [{"name": "jq", "status": "classification_only"}]
            annotated = evidence.annotate(s, offers, [dict(row)], False)
            self.assertEqual(annotated[0]["classification"], classification)
            self.assertIsNot(annotated[0]["classification"], classification)

            changed = dict(row, inventory_verification={"status": "verified", "active_keg": "/other"})
            self.assertEqual(evidence.annotate(s, [{"name": "jq"}], [changed], False)[0]
                             ["classification"]["category"], "unclassified")
            source_changed = dict(row, installed_version="1.8")
            self.assertEqual(evidence.annotate(s, [{"name": "jq"}], [source_changed], False)[0]
                             ["classification"]["category"], "unclassified")
            stale = dict(classification, checked_at=(datetime.now(timezone.utc) -
                        timedelta(seconds=950)).isoformat())
            atomic_write_json(s.STATE_FILE, {
                "packages": [dict(row, classification=stale)], "migrations": []})
            self.assertEqual(evidence.annotate(s, [{"name": "jq"}], [dict(row)], False)[0]
                             ["classification"]["category"], "unclassified")
            atomic_write_json(s.STATE_FILE, {
                "packages": [dict(row, classification=classification)], "migrations": []})
            self.assertEqual(evidence.annotate(s, [{"name": "jq"}],
                             [dict(row, inventory_stale=True)], False)[0]
                             ["classification"]["category"], "unclassified")

    def test_homebrew_revision_update_discovered_when_outdated_flag_is_false(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            service = FakeService(temp)
            service.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "example-portless", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.2_1", "installed_upstream_version": "1.2",
                   "installed_revision": 1, "outdated": False}
            ready = dict(u._base_candidate(row, "homebrew", "update"),
                         availability="verified", upstream_version="1.2",
                         revision=2, execution_supported=True,
                         closure_verified=True, blockers=[])
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"example-portless": {"stable_version": "1.2", "revision": 2}}, {})), \
                 patch.object(u, "_homebrew", return_value=ready) as probe:
                candidates = u.collect_candidates(service, {"packages": [row]})
            probe.assert_called_once()
            decision, = sel.select([row], candidates)
            self.assertEqual(decision["status"], "ready")

    def test_cross_provider_discovery_when_brew_is_current(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "include-what-you-use", "kind": "formula",
                   "provider": "homebrew", "installed_version": "1.0",
                   "outdated": False, "classification": {"category": "standalone_candidate"}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"include-what-you-use": {"stable_version": "1.0"}}, {})), \
                 patch.object(u, "_ports", return_value=dict(
                     u._base_candidate(row, "macports", "migrate"),
                     availability="verified", upstream_version="2.0",
                     closure_verified=True, execution_supported=False, blockers=[])), \
                 patch("migration_engine.plan", return_value={"id": "a"*32, "fingerprint": "f"}):
                candidates = u.collect_candidates(s, {"packages": [row]})
            self.assertTrue(row["discovery_complete"])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["provider"], "macports")
            self.assertTrue(candidates[0]["execution_supported"])
            self.assertEqual(candidates[0]["migration_plan_id"], "a"*32)

    def test_unsupported_mapping_never_calls_port_preflight(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "cryptography", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": False,
                   "classification": {"category": "standalone_candidate"}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"cryptography": {"stable_version": "1.0"}}, {})), \
                 patch.object(u, "_ports", side_effect=AssertionError("unsupported probe")):
                candidates = u.collect_candidates(s, {"packages": [row]})
            self.assertEqual(len(candidates), 1)
            self.assertFalse(candidates[0]["execution_supported"])
            self.assertIn("migration_profile_missing", candidates[0]["blockers"])

    def test_pixi_total_time_budget_blocks_without_network(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            service = FakeService(temp)
            service.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": False,
                   "classification": {"category": "standalone_candidate"}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"jq": {"stable_version": "1.0"}}, {})), \
                 patch("pixi_provider.discover", side_effect=AssertionError("must not contact Pixi")):
                candidates = u.collect_candidates(
                    service, {"packages": [row]}, pixi_scan_budget_seconds=0)
            self.assertEqual(len(candidates), 1)
            self.assertIn("pixi_scan_budget_exhausted", candidates[0]["blockers"])
            self.assertFalse(row["discovery_complete"])

    def test_incomplete_t02_blocks_pixi_without_discovery(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            service = FakeService(temp)
            service.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": False,
                   "classification": {"category": "standalone_candidate",
                                      "gaps": ["service_registry_unavailable"]}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"jq": {"stable_version": "1.0"}}, {})), \
                 patch("pixi_provider.discover", side_effect=AssertionError("not standalone")):
                candidates = u.collect_candidates(service, {"packages": [row]})
            self.assertEqual(len(candidates), 1)
            self.assertIn("fresh_classification_required", candidates[0]["blockers"])

    def test_macports_source_has_blocked_pixi_bridge_and_can_still_update(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            service = FakeService(temp)
            service.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "tesseract", "kind": "port", "provider": "macports",
                   "installed_version": "5.3", "installed_upstream_version": "5.3",
                   "outdated": True}
            with patch.object(u, "_load_homebrew_catalog", return_value=(None, None)), \
                 patch.object(u, "_ports", return_value=dict(
                     u._base_candidate(row, "macports", "update"),
                     availability="verified", upstream_version="5.4",
                     closure_verified=True, execution_supported=True, blockers=[])):
                candidates = u.collect_candidates(service, {"packages": [row]})
            self.assertEqual(len(candidates), 2)
            chosen, = sel.select([row], candidates)
            self.assertEqual(chosen["target_provider"], "macports")
            self.assertIn("migration_source_provider_unsupported",
                          candidates[0]["blockers"])

    def test_empty_port_mapping_has_no_phantom_port_candidate(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "libtensorflow", "kind": "formula",
                   "provider": "homebrew", "installed_version": "1.0",
                   "outdated": False}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"libtensorflow": {"stable_version": "1.0"}}, {})), \
                 patch.object(u, "_ports", side_effect=AssertionError("empty mapping")):
                result = u.collect_candidates(s, {"packages": [row]})
            self.assertEqual(result, [])

    def test_pixi_bridge_only_allows_exact_profile_with_plan(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.7",
                   "classification": {"category": "standalone_candidate",
                                      "gaps": [], "consumers": [], "shared_roles": []}}
            verified = {"provider": "pixi", "availability": "verified",
                        "execution_supported": True, "closure_verified": True,
                        "upstream_version": "1.8", "package_version": "1.8",
                        "provider_names": ["jq"],
                        "system": {"os": "macos", "arch": "x86_64"},
                        "closure": [{"arch": "osx-64", "archive_id": "jq-fixture",
                                     "checksum": "f"*64}],
                        "manifest": "locked", "lock": "pinned", "lock_sha256": "f"*64}
            plan = {"id": "a"*32, "fingerprint": "b"*64,
                    "target": {"provider": "pixi", "candidate": dict(verified),
                               "version": "1.8"}, "profile": "jq", "profile_version": 1}
            with patch("pixi_provider.discover", return_value=verified) as discover, \
                 patch("migration_engine.plan", return_value=plan) as make_plan:
                candidate = u._pixi_bridge(s, row)
            discover.assert_called_once()
            self.assertEqual(make_plan.call_args.kwargs["provider"], "pixi")
            self.assertIs(make_plan.call_args.kwargs["pixi_candidate"], verified)
            self.assertEqual(candidate["migration_plan_id"], "a"*32)
            self.assertEqual(candidate["migration_fingerprint"], "b"*64)
            self.assertTrue(candidate["execution_supported"])
            selected, = sel.select([dict(row, discovery_complete=True)], [candidate])
            self.assertEqual((selected["target_provider"], selected["status"]), ("pixi", "ready"))

    def test_pixi_bridge_failed_transaction_is_blocked(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.7",
                   "classification": {"category": "standalone_candidate"}}
            with patch("pixi_provider.discover", return_value={
                    "provider": "pixi", "availability": "verified",
                    "upstream_version": "1.9"}) as discover, \
                 patch("migration_engine.plan", side_effect=ValueError("unverified")):
                candidate = u._pixi_bridge(s, row)
            discover.assert_called_once()
            self.assertFalse(candidate["execution_supported"])
            self.assertNotIn("migration_plan_id", candidate)
            self.assertIn("pixi_migration_preflight_failed:ValueError",
                          candidate["blockers"])

    def test_pixi_bridge_unlisted_profile_skips_discovery(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            row = {"name": "ffmpeg", "kind": "formula", "provider": "homebrew",
                   "classification": {"category": "standalone_candidate"}}
            with patch("pixi_provider.discover", side_effect=AssertionError("network forbidden")):
                candidate = u._pixi_bridge(s, row)
            self.assertFalse(candidate["execution_supported"])
            self.assertEqual(candidate["blockers"], ["pixi_migration_profile_missing"])

    def test_revalidate_migration_binds_provider_generation_and_lock(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            route = {"provider": "pixi", "availability": "verified",
                     "execution_supported": True, "closure_verified": True,
                     "upstream_version": "1.8", "package_version": "1.8",
                     "revision": None, "provider_names": ["jq"],
                     "system": {"os": "macos", "arch": "x86_64"},
                     "closure": [{"arch": "osx-64", "archive_id": "jq-fixture"}],
                     "manifest": "text", "lock": "bytes", "lock_sha256": "b"*64,
                     "migration_plan_id": "a"*32, "migration_fingerprint": "c"*64,
                     "profile_id": "jq", "profile_version": 1}
            decision = {"logical_id": "jq",
                        "source_installation_id": "homebrew:formula:jq",
                        "current_provider": "homebrew", "target_provider": "pixi",
                        "installed_version": "1.7", "selected_candidate": route}
            keys = ("provider", "availability", "execution_supported",
                    "closure_verified", "upstream_version", "package_version",
                    "revision", "provider_names", "system", "closure",
                    "manifest", "lock", "lock_sha256")
            stored = {"action": "migrate", "fingerprint": "c"*64,
                      "profile": "jq", "profile_version": 1,
                      "source": {"provider": "homebrew",
                                 "installation_id": "homebrew:formula:jq",
                                 "version": "1.7"},
                      "target": {"provider": "pixi", "version": "1.8",
                                 "mapped_name": "jq", "generation_id": "d"*32,
                                 "candidate": {k: route[k] for k in keys}}}
            with patch("migration_engine._load_plan", return_value=stored), \
                 patch("migration_engine._material", return_value=({}, "c"*64)) as material:
                u._revalidate_migration(s, decision)
            self.assertEqual(material.call_args.kwargs["provider"], "pixi")
            self.assertEqual(material.call_args.kwargs["planned_generation"], "d"*32)
            self.assertEqual(material.call_args.kwargs["pixi_candidate"]["lock"], "bytes")
            with patch("migration_engine._load_plan", return_value=stored), \
                 patch("migration_engine._material") as material:
                with self.assertRaisesRegex(u.PlanError, "plan_changed"):
                    u._revalidate_migration(s, dict(decision,
                                                   selected_candidate=dict(route, lock="tampered")))
                material.assert_not_called()
            with self.assertRaisesRegex(u.PlanError, "migration_route_unverified"):
                u._revalidate_migration(s, dict(decision, target_provider="macports"))

    def test_mapped_pixi_candidate_is_discovered_but_not_executable(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": False,
                   "classification": {"category": "standalone_candidate"}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"jq": {"stable_version": "1.0"}}, {})), \
                 patch("pixi_provider.discover", return_value={
                     "availability": "verified", "upstream_version": "2.0",
                     "system": {"os": "macos", "arch": "x86_64"},
                     "closure": [{"arch": "osx-64", "archive_id": "verified-fixture"}],
                     "closure_verified": True}):
                candidates = u.collect_candidates(s, {"packages": [row]})
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["provider"], "pixi")
            self.assertFalse(candidates[0]["execution_supported"])
            decision, = sel.select([row], candidates)
            self.assertNotEqual(decision["status"], "ready")
            self.assertTrue(any(
                reason.startswith("pixi_migration_preflight_failed:")
                for reason in decision["alternatives"][0]["reason_codes"]))

    def test_blocked_pixi_does_not_hide_working_homebrew(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": True,
                   "classification": {"category": "standalone_candidate"}}
            home = dict(u._base_candidate(row, "homebrew", "update"),
                        availability="verified", upstream_version="1.2",
                        execution_supported=True, closure_verified=True,
                        blockers=[])
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"jq": {"stable_version": "1.2"}}, {})), \
                 patch.object(u, "_homebrew", return_value=home), \
                 patch("pixi_provider.discover", return_value={
                     "availability": "verified", "upstream_version": "2.0",
                     "system": {"os": "macos", "arch": "x86_64"},
                     "closure_verified": True, "closure": []}):
                candidates = u.collect_candidates(s, {"packages": [row]})
            selected, = sel.select([row], candidates)
            self.assertEqual(selected["target_provider"], "homebrew")

    def test_expired_cached_plan_cannot_be_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            from provider_ownership import atomic_write_json
            s = FakeService(temp)
            ident = "a" * 32
            decision = {"decision_id": "c"*32, "source_installation_id": "homebrew:formula:jq",
                        "status": "ready", "action": "update", "material_fingerprint": "hash",
                        "selected_candidate": {"provider": "homebrew"}}
            atomic_write_json(Path(temp)/"update-plans"/(ident+".json"),
                              {"id": ident, "expires_at": time.time()-5,
                               "decisions": [decision]})
            atomic_write_json(Path(temp)/"update-plans"/"latest.json", {"plan_id": ident})
            snapshot = {"packages": [{"name": "jq", "kind": "formula", "outdated": True}]}
            u.attach_cached(s, snapshot)
            self.assertTrue(snapshot["unified_plan_stale"])
            self.assertEqual(snapshot["unified_plan_id"], ident)
            self.assertEqual(snapshot["update_decisions"][0]["status"], "unverified")
            self.assertEqual(snapshot["update_decisions"][0]["reason_codes"], ["plan_expired"])
            self.assertIsNone(snapshot["packages"][0]["update_decision"]["selected_candidate"])
            self.assertEqual(u.read_plan(s, ident)["decisions"][0], decision)
            with self.assertRaisesRegex(u.PlanError, "plan_expired"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": ident,
                                       "decisions": [{"decision_id": "c"*32,
                                                      "material_fingerprint": "hash"}]})

    def test_fresh_cached_plan_remains_ready_with_expiry_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            from provider_ownership import atomic_write_json
            s = FakeService(temp)
            expiry = time.time()+500
            plan = {"id": "a"*32, "expires_at": expiry,
                    "decisions": [{"source_installation_id": "homebrew:formula:jq",
                                   "status": "ready", "action": "update"}]}
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"), plan)
            atomic_write_json(Path(temp)/"update-plans"/"latest.json", {"plan_id": "a"*32})
            snapshot = {"packages": [{"name": "jq", "kind": "formula", "outdated": False}]}
            u.attach_cached(s, snapshot)
            self.assertFalse(snapshot["unified_plan_stale"])
            self.assertEqual(snapshot["unified_plan_expires_at"], expiry)
            self.assertTrue(snapshot["packages"][0]["outdated"])
            self.assertEqual(snapshot["packages"][0]["update_decision"]["status"], "ready")

    def test_missing_cached_plan_clears_historical_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = {"unified_plan_id": "a"*32,
                        "update_decisions": [{"status": "ready"}],
                        "packages": [{"name": "jq", "kind": "formula",
                                      "update_decision": {"status": "ready"}}]}
            u.attach_cached(FakeService(temp), snapshot)
            self.assertTrue(snapshot["unified_plan_stale"])
            self.assertEqual(snapshot["update_decisions"], [])
            self.assertNotIn("update_decision", snapshot["packages"][0])

    def test_initial_state_without_plan_is_unmodified(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = {"packages": [{"name": "jq", "kind": "formula"}]}
            self.assertIs(u.attach_cached(FakeService(temp), snapshot), snapshot)
            self.assertNotIn("unified_plan_stale", snapshot)

    def test_pixi_user_workspace_cannot_be_upgraded(self):
        with tempfile.TemporaryDirectory() as temp:
            from unittest.mock import patch
            service = FakeService(temp)
            service.TOOLS_ROOT = Path(temp)
            service.DATA_DIR = Path(temp)
            row = {"name": "jq", "provider": "pixi", "kind": "tool",
                   "installed_version": "1.2", "installation_id": "pixi:jq"}
            with patch.object(u.update_routes, "read_records",
                              return_value={"jq": {"provider": "pixi",
                                                    "prefix": "/tmp/unmanaged-user-pixi"}}):
                candidate = u._pixi(service, row)
            self.assertFalse(candidate["execution_supported"])
            self.assertIn("user_environment_not_managed", candidate["blockers"])

    def test_pixi_missing_owner_blocks_update(self):
        with tempfile.TemporaryDirectory() as temp:
            from unittest.mock import patch
            service = FakeService(temp)
            row = {"name": "jq", "provider": "pixi", "kind": "tool",
                   "installed_version": "1.2", "installation_id": "pixi:jq"}
            with patch.object(u.update_routes, "read_records", return_value={}):
                candidate = u._pixi(service, row)
            self.assertIn("pixi_ownership_unverified", candidate["blockers"])


if __name__ == "__main__":
    unittest.main()
