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

    def test_multi_command_restore_is_idempotent_and_preserves_prior_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = self.setup_dirs(temp)
            first = Path(temp)/'bin/first'
            first.write_text('previous owned shim')
            first.chmod(0o644)
            commands = ('first', 'second', 'third')
            out = route.activate(temp, 'jq', 'tx3',
                {name: str(source) for name in commands},
                {name: str(target) for name in commands},
                owned_shims={'first': route.digest(first)})
            route.restore(temp, out)
            route.restore(temp, out)
            self.assertEqual(first.read_text(), 'previous owned shim')
            self.assertEqual(first.stat().st_mode & 0o777, 0o644)
            self.assertFalse((Path(temp)/'bin/second').exists())
            self.assertFalse((Path(temp)/'bin/third').exists())

    def test_independent_shim_mode_change_blocks_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = self.setup_dirs(temp)
            out = route.activate(temp, 'jq', 'tx4', {'shim': str(source)}, {'shim': str(target)})
            shim = Path(temp)/'bin/shim'
            shim.chmod(0o700)
            with self.assertRaises(route.RoutingConflict):
                route.restore(temp, out)
            self.assertEqual(shim.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
