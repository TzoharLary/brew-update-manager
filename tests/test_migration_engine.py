import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import migration_engine as engine

class MigrationContracts(unittest.TestCase):
    def test_backup_manifest_preserves_file_links_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/"source"
            root.mkdir()
            script = root/"command"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o755)
            (root/"linked").symlink_to("command")
            entries = engine._manifest(root)
            self.assertEqual({e["path"] for e in entries}, {"command", "linked"})
            self.assertEqual(next(e for e in entries if e["path"]=="linked")["target"],"command")
            self.assertEqual(next(e for e in entries if e["path"]=="command")["mode"],0o755)
    def test_pixi_material_plan_is_read_only_and_profile_gated(self):
        import hashlib
        from unittest.mock import patch
        from types import SimpleNamespace
        import migration_profiles
        import pixi_provider
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            (source / "bin").mkdir(parents=True)
            (source / "bin" / "pandoc").write_text("executable")
            receipt = source / "INSTALL_RECEIPT.json"
            receipt.write_text("{}")
            candidate = {
                "provider": "pixi", "availability": "verified",
                "execution_supported": True, "closure_verified": True,
                "system": {"os": "macos", "arch": "x86_64"},
                "upstream_version": "3.2", "package_version": "3.2",
                "provider_names": ["pandoc"],
                "closure": [{"arch": "osx-64", "archive_id": "pandoc-3.2.conda",
                             "checksum": "a"*64}],
                "manifest": pixi_provider.manifest_text("pandoc", "3.2"),
                "lock": "fixture-lock",
                "lock_sha256": hashlib.sha256(b"fixture-lock").hexdigest(),
            }
            class Service:
                TOOLS_ROOT = root
                def is_excluded(self, name, kind):
                    return False
                def load_settings(self):
                    return {"excluded_packages": []}
            service = Service()
            audited = {"manager_versions": ["1.0"]}
            source_data = {"installed_version": "1.0",
                           "installed_upstream_version": "1.0"}
            with patch.object(migration_profiles, "intel_only"), \
                 patch.object(engine, "_source",
                              return_value=(source_data, audited, source, receipt)), \
                 patch.object(engine, "_fresh_classification",
                              return_value={"category": "standalone_candidate"}), \
                 patch.object(engine, "_consumers", return_value=[]), \
                 patch.object(engine, "_route_ownership",
                              return_value=(None, {"routes": []})), \
                 patch.object(engine.routing, "preflight"), \
                 patch("update_routes.read_records", return_value={}):
                first, fingerprint = engine._material_pixi(
                    service, "pandoc", candidate=candidate, generation_id="a"*32)
                second, same = engine._material_pixi(
                    service, "pandoc", candidate=candidate, generation_id="a"*32)
            self.assertEqual(first, second)
            self.assertEqual(fingerprint, same)
            self.assertEqual(first["target"]["provider"], "pixi")
            self.assertEqual(first["target"]["installation_id"], "pixi:pandoc")
            self.assertFalse((root / "environments").exists())

    def test_pixi_unlisted_profile_cannot_authorize_migration(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            class Service:
                TOOLS_ROOT = Path(temp)
            with patch.object(engine.profiles, "intel_only"):
                with self.assertRaisesRegex(engine.MigrationConflict, "profile_missing"):
                    engine._material_pixi(Service(), "ffmpeg", candidate={})

    def test_recovery_refuses_invalid_ids(self):
        for value in ("../../../etc/passwd", "bad", "", None):
            with self.assertRaises(engine.MigrationConflict):
                engine._check_id(value)
    def test_profile_unknown_fails_closed(self):
        self.assertIsNone(engine.profiles.get("unknown-tool"))
    def test_backup_hash_changes_if_file_mutated(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)/"x"
            p.write_text("first")
            first = engine._hash(p)
            p.write_text("second")
            self.assertNotEqual(first,engine._hash(p))

if __name__=="__main__":unittest.main()
