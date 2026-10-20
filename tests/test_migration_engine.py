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
