import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import migration_profiles as p

class Profiles(unittest.TestCase):
    def test_pixi_profiles_are_strictly_allowlisted(self):
        self.assertEqual(set(p.PIXI_MIGRATION_PROFILES), {"jq", "pandoc"})
        self.assertIsNone(p.pixi_get("ffmpeg"))
        self.assertIsNone(p.pixi_get("ngrok"))

    def test_pixi_smoke_runs_real_paths_and_functional_check(self):
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "bin" / "jq"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            class Fake:
                def run_cmd(self, argv, **kwargs):
                    self.calls.append((argv, kwargs))
                    value = "jq-1.8.1\n" if "--version" in argv else '{"verified":true}\n'
                    return SimpleNamespace(code=0, stdout=value, stderr="")
                def __init__(self):
                    self.calls = []
            fake = Fake()
            result = p.smoke_pixi(fake, p.pixi_get("jq"), root, "1.8.1")
            self.assertIn("functional", result)
            self.assertEqual(len(fake.calls), 2)
            self.assertTrue(all(call[0][0] == str(binary) for call in fake.calls))
            self.assertTrue(all(call[1]["cancellable"] for call in fake.calls))

    def test_pixi_version_or_missing_executable_blocks(self):
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(RuntimeError):
                p.smoke_pixi(None, p.pixi_get("jq"), root, "1.8")
            binary = root / "bin" / "jq"
            binary.parent.mkdir()
            binary.write_text("x")
            binary.chmod(0o755)
            class Wrong:
                def run_cmd(self, argv, **kwargs):
                    return SimpleNamespace(code=0, stdout="jq-1.7", stderr="")
            with self.assertRaisesRegex(RuntimeError, "version mismatch"):
                p.smoke_pixi(Wrong(), p.pixi_get("jq"), root, "1.8")

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
