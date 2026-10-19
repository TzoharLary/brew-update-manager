"""Offline checks: no Homebrew commands, package changes, or live app data."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "brew_service", Path(__file__).resolve().parents[1] / "backend/brew-updates-service.py"
)
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


class UpgradeActivityTests(unittest.TestCase):
    def setUp(self):
        service.reset_check_progress_for_run()

    def test_dependency_build_and_target_transition(self):
        with patch.object(service, "append_log"):
            activity = service.UpgradeActivity("ada-url")
            activity.observe("==> Fetching downloads for: ada-url\n", "stderr")
            activity.observe("==> Installing ada-url dependency: cmake\n", "stdout")
            activity.observe("\x1b[34m==>\x1b[0m ./bootstrap --parallel=16\n", "stdout")
            state = service.read_check_progress()["package_activity"]
            self.assertEqual((state["stage"], state["subject"], state["target"]),
                             ("building", "cmake", "ada-url"))
            activity.observe("Warning: Your Xcode is outdated\n", "stderr")
            self.assertEqual(service.read_check_progress()["package_activity"]["stage"], "building")
            activity.observe("==> Installing ada-url\n", "stdout")
            activity.observe("==> cmake -S . -B build\n", "stdout")
            state = service.read_check_progress()["package_activity"]
            self.assertEqual((state["subject"], state["stage"]), ("ada-url", "building"))
            for i in range(25):
                activity.observe(str(i), "stdout")
            self.assertEqual(len(service.read_check_progress()["package_activity"]["technical_tail"]), 12)
            service.reset_check_progress_for_run()
            self.assertIsNone(service.read_check_progress()["package_activity"])

    def test_streams_before_command_finishes_and_preserves_both_outputs(self):
        # The child waits for the callback's acknowledgement: buffered-at-exit
        # implementations fail this test rather than accidentally passing it.
        with tempfile.TemporaryDirectory() as directory:
            ack = Path(directory) / "ack"
            seen = []

            def observe(line, source):
                seen.append((source, line.strip()))
                if line.strip() == "ready":
                    ack.touch()

            script = (
                "import pathlib,sys,time; print('ready', flush=True); "
                "p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+3\n"
                "while not p.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                "assert p.exists(), 'output was not delivered live'\n"
                "print('warning', file=sys.stderr, flush=True)\n"
            )
            result = service.run_cmd([sys.executable, "-u", "-c", script, str(ack)],
                                     timeout=5, on_output=observe)
            self.assertEqual(result.code, 0, result.stderr)
            self.assertEqual(result.stdout, "ready")
            self.assertEqual(result.stderr, "warning")
            self.assertCountEqual(seen, [("stdout", "ready"), ("stderr", "warning")])

    def test_streaming_timeout_stops_child_and_keeps_output(self):
        result = service.run_cmd(
            [sys.executable, "-u", "-c", "import time; print('started', flush=True); time.sleep(20)"],
            timeout=1.0, on_output=lambda *_: None,
        )
        self.assertEqual(result.code, 124)
        self.assertEqual(result.stdout, "started")
        self.assertIn("timed out", result.stderr)

    def test_streaming_cancel_keeps_existing_active_process_control(self):
        def cancel(line, source):
            if line.strip() == "started":
                with service.ACTIVE_COMMAND_LOCK:
                    proc = service.ACTIVE_COMMAND["process"]
                service.terminate_process(proc)

        result = service.run_cmd(
            [sys.executable, "-u", "-c", "import time; print('started', flush=True); time.sleep(20)"],
            timeout=5, cancellable=True, on_output=cancel,
        )
        self.assertNotEqual(result.code, 0)
        self.assertEqual(result.stdout, "started")
        self.assertIsNone(service.ACTIVE_COMMAND["process"])


if __name__ == "__main__":
    unittest.main()
