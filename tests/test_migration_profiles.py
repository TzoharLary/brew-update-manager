import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import migration_profiles as p

class Profiles(unittest.TestCase):
    def test_two_fixed_profiles_and_commands(self):
        self.assertEqual(set(p.PROFILES), {"terminal-notifier", "include-what-you-use"})
        self.assertEqual(p.get("include-what-you-use").commands,
                         ("include-what-you-use", "iwyu_tool.py", "fix_includes.py"))
    def test_arm_or_rosetta_rejected(self):
        with patch("migration_profiles.platform.system", return_value="Darwin"),\
             patch("migration_profiles.platform.machine", return_value="arm64"):
            with self.assertRaises(ValueError): p.intel_only()
        with patch("migration_profiles.platform.system", return_value="Darwin"),\
             patch("migration_profiles.platform.machine", return_value="x86_64"),\
             patch.dict("migration_profiles.os.environ", {"SYSCTL_TRANSLATED":"1"}):
            with self.assertRaises(ValueError): p.intel_only()
    def test_version_parser_not_fooled_by_minor_digits(self):
        self.assertTrue(p.version_matches("IWYU 0.24", "0.24"))
        self.assertFalse(p.version_matches("IWYU 0.241", "0.24"))
    def test_paths_use_real_port_location(self):
        self.assertTrue(all(str(v).startswith("/opt/local/bin/")
                            for v in p.command_paths(p.get("include-what-you-use")).values()))

if __name__=="__main__": unittest.main()
