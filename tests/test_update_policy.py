import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("policy_service", Path(__file__).resolve().parents[1] / "backend/brew-updates-service.py")
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)
import update_routes as routes
from update_engine import Engine


class UpdatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.patches = []
        for name, value in {"DATA_DIR": self.root, "LOG_DIR": self.root / "logs", "CACHE_DIR": self.root / "cache",
                            "SETTINGS_FILE": self.root / "settings.json", "TOOLS_ROOT": self.root / "tools",
                            "STATE_FILE": self.root / "state.json", "RUN_JOURNAL_FILE": self.root / "run.json",
                            "UPDATE_PLAN_FILE": self.root / "plan.json", "CHECK_LOG_FILE": self.root / "logs/check.log",
                            "SERVICE_LOG_FILE": self.root / "logs/service.log"}.items():
            p = patch.object(service, name, value)
            p.start()
            self.patches.append(p)
        service.reset_cancel_state()
        service.reset_check_progress_for_run()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    def test_exclusion_survives_settings_updates_and_blocks_direct_upgrade(self):
        settings = service.settings_defaults()
        settings.update(excluded_packages=["cask:ngrok", "formula:ngrok"], binary_tools=["node"])
        service.save_settings(settings)
        settings = service.load_settings()
        settings["scheduler"]["hour"] = 12
        service.save_settings(settings)
        self.assertEqual(service.load_settings()["binary_tools"], ["node"])
        with patch.object(service, "brew_bin", side_effect=AssertionError("Excluded package reached Homebrew")):
            self.assertEqual(service.run_upgrade("ngrok", "cask")["reason"], "excluded")
        pkg = {"name": "ngrok", "kind": "cask", "outdated": True, "installed_version": "3.38.0"}
        service.apply_update_policy([pkg])
        self.assertFalse(pkg["outdated"])
        self.assertTrue(pkg["excluded"])
        self.assertEqual(service.summarize_counts([pkg])["outdated_total"], 0)
        settings["excluded_packages"] = []
        service.save_settings(settings)
        service.apply_update_policy([pkg])
        self.assertTrue(pkg["outdated"])

    def test_binary_preflight_checks_runtime_closure_and_ignores_build_only(self):
        bottled = {"stable_version": "1.0", "bottle_checksum": "digest", "bottle_tag": ":sonoma"}
        formulae = {"tool": {**bottled, "stable_dependencies": ["runtime", {"llvm": ":build"}]},
                    "runtime": {"stable_version": "2.0"}}
        self.assertEqual(routes.bottle_blockers("tool", formulae, {}, []),
                         [{"name": "runtime", "reason": "source_required"}])
        self.assertEqual(routes.bottle_blockers("tool", formulae, {"runtime": {"versions": ["2.0"]}}, []), [])
        self.assertEqual(routes.bottle_blockers("tool", formulae, {}, ["formula:runtime"])[0]["reason"], "excluded_dependency")
        self.assertEqual(routes.bottle_blockers("unknown", formulae, {}, [])[0]["reason"], "metadata_unavailable")

    def test_failed_shared_build_stops_later_roots_and_journals_outcome(self):
        state = {"packages": [{"name": "first", "kind": "formula", "outdated": True},
                              {"name": "second", "kind": "formula", "outdated": True}]}
        plan = [{"name": name, "kind": "formula", "provider": "homebrew", "ready": True} for name in ("first", "second")]
        engine = Engine(service)
        with patch.object(service, "compute_snapshot", return_value=state), patch.object(engine, "plan", return_value=plan), \
                patch.object(service, "fetch_outdated_index", return_value={("formula", "first"): {}, ("formula", "second"): {}}), \
                patch.object(service, "safe_refresh_snapshot", return_value=(state, None)), \
                patch.object(service, "append_update_history_entries"), \
                patch.object(service, "run_upgrade", return_value={"name": "first", "kind": "formula", "ok": False, "code": 124}) as upgrade:
            result = engine.run(track_progress=True)
        self.assertEqual(upgrade.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["remaining_count"], 1)
        journal = json.loads(service.RUN_JOURNAL_FILE.read_text())
        self.assertEqual(journal["status"], "error")
        self.assertEqual(len(journal["results"]), 1)

    def test_default_timeout_does_not_kill_long_build_at_two_hours(self):
        self.assertEqual(service.settings_defaults()["upgrade_timeout_seconds"], 0)
        settings = service.settings_defaults()
        settings["binary_only"] = False
        service.save_settings(settings)
        with patch.object(service, "brew_bin", return_value="brew"), \
                patch.object(service, "run_cmd", return_value=service.CommandResult(0, "done", "")) as command:
            result = service.run_upgrade("tool", "formula")
        self.assertTrue(result["ok"])
        self.assertIsNone(command.call_args.kwargs["timeout"])
        self.assertEqual(command.call_args.kwargs["env_override"]["HOMEBREW_NO_INSTALL_CLEANUP"], "1")

    def test_cancel_during_preflight_stops_without_installing(self):
        state = {"packages": [{"name": "tool", "kind": "formula", "outdated": True}]}
        engine = Engine(service)
        with patch.object(service, "compute_snapshot", return_value=state), \
                patch.object(engine, "plan", side_effect=service.OperationCancelledError("Canceled")), \
                patch.object(service, "run_upgrade") as install:
            result = engine.run()
        install.assert_not_called()
        self.assertTrue(result["canceled"])
        self.assertEqual(service.read_check_progress()["phase"], "canceled")

    def test_activation_preserves_previous_environment_when_candidate_invalid(self):
        prefix = self.root / "prefix"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin/tool").write_text("#!/bin/sh\necho working\n")
        (prefix / "bin/tool").chmod(0o755)
        routes.activate(service.TOOLS_ROOT, "tool", "pixi", self.root, prefix, "1.0", ["tool"], "working")
        before = (service.TOOLS_ROOT / "active/tool").resolve()
        with self.assertRaises(RuntimeError):
            routes.activate(service.TOOLS_ROOT, "tool", "pixi", self.root, self.root / "invalid", "2.0", ["tool"], "")
        self.assertEqual((service.TOOLS_ROOT / "active/tool").resolve(), before)
        self.assertEqual(routes.read_records(service.TOOLS_ROOT)["tool"]["version"], "1.0")

    def test_managed_inventory_survives_removed_brew_keg(self):
        service.TOOLS_ROOT.mkdir(parents=True, exist_ok=True)
        service.atomic_write_json(service.TOOLS_ROOT / "installed.json", {
            "pandoc": {"version": "3.12.1", "provider": "pixi", "verified_at": 1}})
        replies = [service.CommandResult(0, json.dumps({"formulae": [], "casks": []}), ""),
                   service.CommandResult(0, json.dumps({"formulae": [
                       {"name": "pandoc", "versions": {"stable": "3.12.1"}, "revision": 0}]}), "")]
        with patch.object(service, "brew_bin", return_value="brew"), patch.object(service, "run_cmd", side_effect=replies):
            packages = service.fetch_installed_inventory({})
        service.apply_update_policy(packages)
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["provider"], "pixi")
        self.assertFalse(packages[0]["brew_installation_present"])
        self.assertFalse(packages[0]["outdated"])

    def test_standalone_uv_updates_original_path_without_new_environment(self):
        service.TOOLS_ROOT.mkdir(parents=True, exist_ok=True)
        prefix = self.root / "original-user-location"
        service.atomic_write_json(service.TOOLS_ROOT / "installed.json", {
            "uv": {"version": "0.9.7", "provider": "standalone_uv", "prefix": str(prefix), "verified_at": 1}})
        engine = Engine(service)
        item = {"name": "uv", "provider": "standalone_uv", "version": "0.13.1"}
        with patch.object(service, "run_cmd", return_value=service.CommandResult(0, "uv 0.13.1", "")) as run:
            result = engine.install_tool(item)
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args_list[0].args[0], [str(prefix / "bin/uv"), "self", "update", "0.13.1", "--no-config"])
        self.assertFalse((service.TOOLS_ROOT / "environments").exists())
        self.assertEqual(routes.read_records(service.TOOLS_ROOT)["uv"]["prefix"], str(prefix))

    def test_upgrading_dependency_is_reported_as_dependency(self):
        activity = service.UpgradeActivity("deno")
        activity.observe("==> Upgrading deno dependency: llvm (22 -> 23)", "stdout")
        self.assertEqual(activity.state["subject"], "llvm")
        self.assertEqual(activity.state["stage"], "dependency")

    def test_quiet_stdout_can_still_report_measured_build_tasks(self):
        with patch.object(service.Path, "home", return_value=self.root):
            activity = service.UpgradeActivity("llvm")
            log = self.root / "Library/Logs/Homebrew/llvm/02.cmake.log"
            log.parent.mkdir(parents=True)
            log.write_text("[42/100] Building object.cpp.o\n")
            activity.monitor_build_logs()
            try:
                deadline = time.monotonic() + 4
                while not activity.state.get("build_progress") and time.monotonic() < deadline:
                    time.sleep(.05)
                measured = activity.state["build_progress"]
                self.assertEqual((measured["kind"], measured["done"], measured["total"]), ("tasks", 42, 100))
            finally:
                activity.stop_monitor()


if __name__ == "__main__":
    unittest.main()
