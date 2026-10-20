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
