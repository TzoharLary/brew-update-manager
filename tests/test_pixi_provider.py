import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import pixi_provider as p


class PixiContract(unittest.TestCase):
    def test_exact_manifest_and_no_inherited_channels(self):
        text = p.manifest_text("jq", "1.7")
        self.assertIn('channels = ["conda-forge"]', text)
        self.assertIn('platforms = ["osx-64"]', text)
        self.assertIn('"jq" = "==1.7"', text)
        self.assertNotIn(">=", text)

    def test_search_rejects_unknown_and_prerelease(self):
        with self.assertRaises(p.PixiUnavailable):
            p.parse_search({"unexpected": []}, "jq")
        found = p.parse_search([{"name":"jq","version":"1.2"},{"name":"jq","version":"2.0rc1"},
                                {"name":"git","version":"100"}], "jq")
        self.assertEqual(found, ["1.2"])

    def test_explicit_rejects_foreign_channel_and_missing_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "osx-64.txt"
            path.write_text("@EXPLICIT\nhttps://other.example/a.conda#abcdef\n")
            with self.assertRaises(p.PixiUnavailable):
                p.parse_explicit(temp)
            path.write_text("@EXPLICIT\nhttps://conda.anaconda.org/conda-forge/osx-64/a.conda\n")
            with self.assertRaises(p.PixiUnavailable):
                p.parse_explicit(temp)

    def test_explicit_accepts_verified_intel_and_noarch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "osx-64.txt"
            path.write_text("@EXPLICIT\nhttps://conda.anaconda.org/conda-forge/osx-64/jq-1.2.conda#" + "a"*64 + "\n"
                            "https://conda.anaconda.org/conda-forge/noarch/foo-1.0.conda#" + "b"*32 + "\n")
            nodes = p.parse_explicit(temp)
            self.assertEqual({x["arch"] for x in nodes}, {"osx-64", "noarch"})

    def test_no_existing_workspace_overwrite(self):
        class S:
            pass
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                p.install_locked(S(), {"provider":"pixi", "closure_verified":False}, temp)

    def test_locked_install_enables_install_even_when_user_env_disables_it(self):
        import hashlib
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generation = root / "generation"
            manifest = p.manifest_text("jq", "1.2")
            lock = "verified fixture lock"
            candidate = {"provider": "pixi", "closure_verified": True,
                         "system": {"arch": "x86_64"},
                         "manifest": manifest, "lock": lock,
                         "lock_sha256": hashlib.sha256(lock.encode()).hexdigest()}
            class Fake:
                def run_cmd(self, argv, **kwargs):
                    self.args = argv
                    self.env = kwargs["env_override"]
                    (generation / ".pixi" / "envs" / "default" / "conda-meta").mkdir(
                        parents=True)
                    return SimpleNamespace(code=0, stdout="", stderr="")
            service = Fake()
            with patch.object(p, "runtime", return_value="/verified/pixi"):
                installed = p.install_locked(service, candidate, generation)
            self.assertIn("--locked", service.args)
            self.assertEqual(service.env["PIXI_NO_INSTALL"], "false")
            self.assertEqual(installed, generation / ".pixi" / "envs" / "default")
            self.assertEqual(p._env(root)["PIXI_NO_INSTALL"], "true")

    def test_ngrok_and_unmapped_blocked(self):
        for name in ("ngrok", "not-mapped", "../evil"):
            with self.assertRaises(ValueError):
                p.manifest_text(name, "1.2")

    def test_conda_meta_must_match_one_exact_version(self):
        with tempfile.TemporaryDirectory() as temp:
            meta = Path(temp) / "conda-meta"
            meta.mkdir()
            (meta / "jq-1.json").write_text('{"name":"jq","version":"1.2"}')
            self.assertEqual(p.installed_record(temp, "jq", "1.2.0")["version"], "1.2")
            with self.assertRaises(p.PixiUnavailable):
                p.installed_record(temp, "jq", "1.3")


    def test_pixi_full_closure_must_match_conda_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            meta = Path(root)/"conda-meta"
            meta.mkdir()
            (meta/"jq.json").write_text('{"fn":"jq-1.2.conda", "name":"jq",'
                                         '"version":"1.2", "subdir":"osx-64",'
                                         '"sha256":"' + "a"*64 + '"}')
            closure = [{"archive_id":"jq-1.2.conda","arch":"osx-64",
                        "checksum_algorithm":"sha256","checksum":"a"*64}]
            self.assertEqual(p.verify_generation(root,closure)["verified_archives"],1)
            closure[0]["checksum"] = "b"*64
            with self.assertRaises(p.PixiUnavailable):
                p.verify_generation(root,closure)


if __name__ == "__main__":
    unittest.main()
