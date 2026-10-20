import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import macports_provider as mp


class MacPortsVersionFixtures(unittest.TestCase):
    def test_separates_upstream_and_packaging_revision(self):
        self.assertEqual(mp.version_parts("1.8.1_0"), ("1.8.1", 0))
        self.assertEqual(mp.version_parts("1.8.2_12"), ("1.8.2", 12))
        self.assertEqual(mp.version_parts("2.3.0"), ("2.3.0", 0))
        self.assertEqual(mp.version_parts("2.0rc1"), ("2.0rc1", 0))

    def test_active_port_metadata_supports_numeric_selection(self):
        class Service:
            def run_cmd(self, argv, **kwargs):
                text = (" jq @1.8.1_0 (active)\n" if "installed" in argv
                        else "jq @1.8.1_0 < 1.8.2_1\n")
                return SimpleNamespace(code=0, stdout=text, stderr="")
        with tempfile.TemporaryDirectory() as temp:
            port = Path(temp) / "port"
            port.write_text("not executed")
            with patch.object(mp, "PORT", str(port)):
                packages = mp.inventory(Service())
            self.assertEqual(len(packages), 1)
            row = packages[0]
            self.assertEqual((row["installed_upstream_version"], row["installed_revision"]),
                             ("1.8.1", 0))
            self.assertEqual((row["latest_upstream_version"], row["latest_revision"]),
                             ("1.8.2", 1))
            self.assertTrue(row["outdated"])

    def test_malformed_suffix_does_not_invent_numeric_version(self):
        from update_selection import compare_upstream
        upstream, _ = mp.version_parts("2.0_rc")
        self.assertIsNone(compare_upstream(upstream, "1.9"))


if __name__ == "__main__":
    unittest.main()
