import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import routing_activation as route


class RoutingTests(unittest.TestCase):
    def setup_dirs(self, root):
        binpath = Path(root) / "bin"
        binpath.mkdir()
        a = binpath / "tool"
        a.write_text("#!/bin/sh\nexit 0\n")
        a.chmod(0o755)
        b = binpath / "replacement"
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        return a, b

    def test_unmanaged_file_and_broken_link_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b = self.setup_dirs(temp)
            with self.assertRaises(route.RoutingConflict):
                route.preflight(temp, "jq", {"tool": str(a)})
            a.unlink()
            a.symlink_to("/no/such/path")
            with self.assertRaises(route.RoutingConflict):
                route.preflight(temp, "jq", {"tool": str(b)})

    def test_all_commands_switch_atomically_and_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b = self.setup_dirs(temp)
            shim = Path(temp)/"bin"/"newshim"
            out = route.activate(temp, "jq", "tx1", {"newshim":str(a)},
                                 {"newshim":str(b)})
            self.assertTrue(shim.is_file())
            self.assertEqual(os.readlink(out["active"]), out["target"])
            route.restore(temp,out)
            self.assertFalse(os.path.lexists(shim))
            self.assertFalse(os.path.lexists(out["active"]))
            self.assertTrue(a.is_file())

    def test_independent_edit_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b = self.setup_dirs(temp)
            out = route.activate(temp, "jq", "tx2", {"newshim":str(a)},
                                 {"newshim":str(b)})
            shim = Path(temp)/"bin"/"newshim"
            shim.write_text("independent edit")
            with self.assertRaises(route.RoutingConflict):
                route.restore(temp,out)
            self.assertEqual(shim.read_text(), "independent edit")


if __name__ == "__main__":
    unittest.main()
