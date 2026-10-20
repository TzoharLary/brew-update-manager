#!/usr/bin/env python3
"""User-operated 1.3.4 Intel code checks, packaged-app and update acceptance.

The agent does not run this command. --full is authorization to replace only
the local Mac Update Manager app; package mutations still need one explicit
YES tied to the displayed immutable plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
RECOVERY = Path.home() / ".local/share/brew-update-manager/recovery/unified-intel-1.3.4"
APPLICATION = Path("/Applications/Mac Update Manager.app")
BUILT = ROOT / "dist/mac/Mac Update Manager.app"
APP_ID = "com.tzoharlary.brewupdatemanager"
VERSION = "1.3.4"


class Blocked(RuntimeError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def backend_digest(directory):
    return hashlib.sha256(b"".join(
        path.name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest()
        for path in sorted(Path(directory).glob("*.py")))).hexdigest()


def signature():
    files = sorted(
        path for folder in ("backend", "electron", "renderer", "tests", "tools")
        for path in (ROOT / folder).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and path.suffix in (".py", ".js", ".cjs", ".json", ".html", ".css", ".tcl"))
    files += [ROOT / "package.json", ROOT / "package-lock.json"]
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(set(files))}


def unique_report():
    path = RECOVERY / "runs" / ("run-" + str(time.time_ns()))
    path.mkdir(parents=True)
    return path


class Report:
    def __init__(self, mode):
        self.dir = unique_report()
        self.data = {
            "schema_version": 1, "mode": mode,
            "started_at": time.time(), "version": VERSION,
            "code_checks": "not_run", "packaged_app": "not_run",
            "simulated_scenarios": "not_run", "live_migration": "not_run",
            "restart_verification": "not_run", "steps": [],
        }
        self.save()

    def save(self):
        (self.dir / "summary.json").write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, default=str) + "\n")
        lines = ["# דוח בדיקת מנהל עדכוני Mac Intel", "",
                 "גרסה: " + VERSION, "תוצאה: " + self.data.get("result", "running"), ""]
        for key in ("code_checks", "packaged_app", "simulated_scenarios",
                    "live_migration", "restart_verification"):
            lines.append("- " + key + ": " + str(self.data[key]))
        lines += ["", "## שלבים", ""]
        lines += ["- " + x["name"] + ": " + x["status"] +
                  (" — " + x["detail"] if x.get("detail") else "")
                  for x in self.data["steps"]]
        if self.data.get("error"):
            lines += ["", "שגיאה: " + self.data["error"]]
        (self.dir / "summary.md").write_text("\n".join(lines) + "\n")

    def step(self, name, status, detail=None, **extra):
        self.data["steps"].append(
            {"name": name, "status": status, "at": time.time(),
             **({"detail": detail} if detail else {}), **extra})
        self.save()
        print(name + ": " + status + ((" — " + detail) if detail else ""), flush=True)


def command(args, report, name, timeout=1800):
    destination = report.dir / (name + ".log")
    report.step(name, "running", argv=args)
    with destination.open("wb") as output:
        try:
            result = subprocess.run(args, cwd=ROOT, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            report.step(name, "unverified", "Timed out without assuming rollback",
                        log=str(destination))
            raise Blocked("Timed out: " + name) from error
    if result.returncode:
        report.step(name, "fail", "See " + str(destination),
                    returncode=result.returncode)
        raise RuntimeError(name + " failed")
    report.step(name, "pass", log=str(destination))


def api(port, path, body=None, timeout=4):
    url = "http://127.0.0.1:" + str(port) + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data is not None else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def find_service():
    found = []
    for port in range(8765, 8796):
        try:
            health = api(port, "/health", timeout=0.3)
            if health.get("app") == "Mac Update Manager" and health.get("pid"):
                found.append((port, health))
        except (OSError, ValueError, urllib.error.URLError):
            continue
    if len(found) > 1:
        raise Blocked("Multiple Mac Update Manager backends; cannot select one")
    return found[0] if found else None


def prerequisites(report):
    if platform.system() != "Darwin" or platform.machine() != "x86_64":
        raise Blocked("Native macOS Intel is required")
    if os.environ.get("SYSCTL_TRANSLATED") == "1":
        raise Blocked("Rosetta is not an Intel migration target")
    if sys.version_info[:2] != (3, 14):
        raise Blocked("Run under verified Python 3.14")
    for program in ("node", "git", "/usr/bin/ditto", "/usr/bin/open", "/usr/bin/osascript"):
        if not (shutil.which(program) if not program.startswith("/") else Path(program).is_file()):
            raise Blocked("Required existing executable missing: " + program)
    if not (ROOT / "node_modules/.bin/electron-builder").is_file():
        raise Blocked("electron-builder is not installed; no dependencies were installed")
    report.step("prerequisites", "pass", "macOS Intel and existing local tooling")


def matching_code_report(wanted):
    directory = RECOVERY / "runs"
    for path in sorted(directory.glob("*/summary.json"), reverse=True):
        try:
            report = json.loads(path.read_text())
            if (report.get("mode") == "isolated_code_checks" and
                    report.get("result") == "pass" and
                    report.get("code_sha256") == wanted and
                    all(step.get("status") == "pass" for step in report.get("steps", []))):
                return path
        except (OSError, ValueError):
            continue
    return None


def isolated_checks(report, *, reuse=True):
    wanted = signature()
    target = report.dir / "code-manifest.json"
    target.write_text(json.dumps(wanted, indent=2) + "\n")
    matched = matching_code_report(wanted) if reuse else None
    if matched is None:
        command([sys.executable, str(ROOT / "tools/verify-current-code.py")],
                report, "isolated-code-suite", timeout=1800)
        matched = matching_code_report(wanted)
        if matched is None:
            raise Blocked("Code check results are missing or signed for different source files")
    report.data["code_checks"] = "pass"
    report.data["checks_report"] = str(matched)
    report.save()
    return wanted


def metadata(app):
    path = Path(app) / "Contents/Info.plist"
    if not path.is_file():
        raise Blocked("Application Info.plist is missing")
    with path.open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != APP_ID:
        raise Blocked("Unexpected application identifier")
    return info


def package_application(report, hashes):
    command([str(ROOT / "node_modules/.bin/electron-builder"),
             "--dir", "--mac", "--x64", "--publish", "never"],
            report, "intel-package", timeout=1800)
    info = metadata(BUILT)
    if str(info.get("CFBundleShortVersionString")) != VERSION:
        raise Blocked("Packaged application version is not " + VERSION)
    backend = BUILT / "Contents/Resources/app.asar.unpacked/backend"
    if not backend.is_dir():
        raise Blocked("Packaged backend files are unavailable")
    for relative, expected in hashes.items():
        if relative.startswith("backend/"):
            candidate = backend / relative.removeprefix("backend/")
            if not candidate.is_file() or sha(candidate) != expected:
                raise Blocked("Packaged code differs: " + relative)
    product = {"version": VERSION,
               "backend_code_sha256": backend_digest(backend),
               "packaged_path": str(BUILT), "application_id": APP_ID,
               "build_id": "unified-intel-1.3.4"}
    (report.dir / "build-manifest.json").write_text(json.dumps(product, indent=2) + "\n")
    report.data["packaged_app"] = "pass"
    report.save()
    return product


def check_idle(service):
    if service is None:
        return
    port, health = service
    if health.get("operation", {}).get("running"):
        raise Blocked("App service has an active operation; replacement is deferred")
    progress = api(port, "/api/progress")
    if progress.get("parallel_check", {}).get("running"):
        raise Blocked("A parallel scan is active; replacement is deferred")
    if progress.get("progress", {}).get("running"):
        raise Blocked("An update is still running")
    if health.get("capabilities") is None:
        raise Blocked("Running service identity could not be verified")


def backup_user_state(report, service):
    if service:
        data = Path(service[1].get("data_dir") or "/nonexistent")
    else:
        data = ROOT / "data" if (ROOT / "data").is_dir() else Path.home() / "Library/Application Support/Homebrew Update Manager"
    paths = [data / name for name in ("settings.json", "update-history.json",
                                      "last-update-run.json", "state.json")]
    paths += [Path.home() / ".local/share/brew-update-manager/migrations/routes.json",
              Path.home() / ".local/share/brew-update-manager/installed.json"]
    snapshots = {}
    for original in paths:
        if original.is_file():
            relative = original.relative_to(Path.home()) if original.is_relative_to(Path.home()) else Path("external") / original.name
            saved = report.dir / "backup" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, saved)
            snapshots[str(original)] = {"path": str(saved), "sha256": sha(saved)}
    (report.dir / "backed-up-state.json").write_text(
        json.dumps(snapshots, ensure_ascii=False, indent=2) + "\n")
    report.step("state-backup", "pass", str(len(snapshots)) + " existing files")


def is_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def install_packaged(report, service):
    check_idle(service)
    if APPLICATION.exists() and service is None:
        check = subprocess.run(["/usr/bin/osascript", "-e",
                                'application id "' + APP_ID + '" is running'],
                               capture_output=True, text=True, timeout=15)
        if check.returncode or check.stdout.strip().lower() != "false":
            raise Blocked("Existing application status is unknown or it is still running")
    backup_user_state(report, service)
    if APPLICATION.exists():
        metadata(APPLICATION)
        command(["/usr/bin/ditto", str(APPLICATION),
                 str(report.dir / "backup" / "Mac Update Manager.app")],
                report, "application-backup", timeout=180)
    if service:
        old_pid = service[1]["pid"]
        command(["/usr/bin/osascript", "-e",
                 'tell application id "' + APP_ID + '" to quit'],
                report, "close-owned-application", timeout=45)
        deadline = time.time() + 45
        while time.time() < deadline and is_alive(old_pid):
            time.sleep(1)
        if is_alive(old_pid):
            raise Blocked("Old backend is still running; no app swap")
    staging = APPLICATION.parent / (".mac-manager-next-" + uuid.uuid4().hex + ".app")
    retired = APPLICATION.parent / (".mac-manager-previous-" + uuid.uuid4().hex + ".app")
    command(["/usr/bin/ditto", str(BUILT), str(staging)], report,
            "stage-application", timeout=180)
    metadata(staging)
    try:
        if APPLICATION.exists():
            os.replace(APPLICATION, retired)
        os.replace(staging, APPLICATION)
    except BaseException:
        if not APPLICATION.exists() and retired.exists():
            os.replace(retired, APPLICATION)
        raise
    report.data["old_app_retained_at"] = str(retired) if retired.exists() else None
    report.step("application-swap", "pass", "Local app replaced; package installations unchanged")


def wait_for_health(report, product, previous_pid=None):
    deadline = time.time() + 90
    last = None
    while time.time() < deadline:
        try:
            service = find_service()
            if service is not None:
                port, health = service
                last = health
                if (health.get("version") == VERSION and
                    health.get("backend_code_sha256") == product["backend_code_sha256"] and
                    health.get("build_id") == product["build_id"] and
                    health.get("pid") != previous_pid):
                    report.step("service-identity", "pass", "Live process matches packaged source")
                    return service
        except Exception:
            pass
        time.sleep(1)
    raise Blocked("Unable to identify a running packaged backend: " + str(last))


def run_update_request(report, port, payload):
    response = {}
    error = []
    done = threading.Event()
    def worker():
        try:
            response.update(api(port, "/api/update-selected", payload, timeout=7200))
        except BaseException as exc:
            error.append(str(exc))
        finally:
            done.set()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while not done.wait(timeout=8):
        try:
            current = api(port, "/api/progress", timeout=5)
            progress = current.get("progress") or {}
            print("עדכון:", progress.get("phase") or "running",
                  progress.get("current_package") or "", flush=True)
        except Exception:
            print("אין כרגע חיווי מהשירות; הבקשה לא נשלחת שוב.", flush=True)
    if error:
        try:
            journal = api(port, "/api/update-requests/" + payload["request_id"])
            report.data["request_state_after_disconnect"] = journal
            report.save()
        except Exception:
            pass
        raise Blocked("Request status cannot be confirmed; do not retry mutation: " + error[0])
    return response


def chosen_actions(report, plan, pilot):
    decisions = [d for d in plan.get("decisions", [])
                 if d.get("status") == "ready"]
    if pilot:
        decisions = [d for d in decisions if d["logical_id"] == pilot]
    for idx, d in enumerate(decisions, 1):
        print(f"{idx}. {d['logical_id']}  {d['installed_version']} → "
              f"{d['target_version']}  {d['current_provider']} → "
              f"{d['target_provider']} ({d['action']})")
    iw = next((d for d in decisions
               if d["logical_id"] == "include-what-you-use" and
               d["action"] == "migrate"), None)
    if not iw:
        raise Blocked("IWYU migration is not ready; never pick a substitute pilot automatically")
    if pilot:
        return [iw]
    raw = input("מספרי הפעולות הרצויות, מופרדים בפסיק (ברירת מחדל: IWYU בלבד): ").strip()
    if not raw:
        return [iw]
    try:
        indices = {int(x.strip()) for x in raw.split(",")}
    except ValueError as error:
        raise Blocked("Invalid selection") from error
    if not indices or any(x < 1 or x > len(decisions) for x in indices):
        raise Blocked("Invalid selection indices")
    return [d for index, d in enumerate(decisions, 1) if index in indices]


def verify_migration(report, service, decisions):
    state = api(service[0], "/api/state")
    report.data["after_inventory"] = state
    for decision in decisions:
        if decision["action"] != "migrate":
            continue
        source_id = decision["source_installation_id"]
        target_provider = decision["target_provider"]
        target_name = decision["selected_candidate"]["provider_names"][0]
        rows = state.get("packages", [])
        if any(x.get("installation_id") == source_id and
               not x.get("inventory_stale") for x in rows):
            raise Blocked("Source installation still appears in verified inventory")
        expected_id = "macports:" + target_name if target_provider == "macports" else "pixi:" + target_name
        if not any(x.get("installation_id") == expected_id and
                   not x.get("inventory_stale") for x in rows):
            raise Blocked("Migrated provider installation is not verified")
    report.step("live-migration-state", "pass", "No synthetic old installation observed")
    return state


def perform_full(report, pilot):
    prerequisites(report)
    matched = isolated_checks(report)
    product = package_application(report, matched)
    previous = find_service()
    check_idle(previous)
    old_pid = previous[1]["pid"] if previous else None
    install_packaged(report, previous)
    command(["/usr/bin/open", str(APPLICATION)], report, "open-new-application", timeout=30)
    live = wait_for_health(report, product, previous_pid=old_pid)
    report.data["installed_service"] = live[1]
    report.save()
    scanned = api(live[0], "/api/check", {}, timeout=1800)
    report.data["before_inventory"] = scanned.get("snapshot", scanned)
    plan_result = api(live[0], "/api/update-plan")
    plan = plan_result.get("plan")
    if not isinstance(plan, dict):
        raise Blocked("No immutable plan was produced by the installed application")
    report.data["displayed_plan"] = plan
    report.save()
    selected = chosen_actions(report, plan, pilot)
    approved = [{"decision_id": d["decision_id"],
                 "material_fingerprint": d["material_fingerprint"]} for d in selected]
    request = {"request_id": uuid.uuid4().hex, "plan_id": plan["id"],
               "decisions": approved}
    report.data["request"] = request
    report.step("approval-required", "blocked", "No package will be changed before YES")
    chosen_names = ", ".join(d["logical_id"] for d in selected)
    if input("לאשר את הפעולות " + chosen_names + " עבור plan " + plan["id"] + " ? הקלד YES: ").strip() != "YES":
        raise Blocked("User did not approve the displayed operations")
    response = run_update_request(report, live[0], request)
    report.data["mutation_response"] = response
    report.save()
    if not response.get("ok") or not all(x.get("ok") for x in response.get("results", [])):
        raise RuntimeError("An approved update failed; examine its transaction journal")
    report.data["live_migration"] = "pass"
    report.save()
    verify_migration(report, live, selected)
    check_idle(find_service())
    command(["/usr/bin/osascript", "-e",
             'tell application id "' + APP_ID + '" to quit'], report,
            "restart-close-owned-app", timeout=45)
    deadline = time.time() + 45
    while is_alive(live[1]["pid"]) and time.time() < deadline:
        time.sleep(1)
    if is_alive(live[1]["pid"]):
        raise Blocked("Old app PID remains active; restart deferred")
    command(["/usr/bin/open", str(APPLICATION)], report, "restart-open-app", timeout=30)
    restarted = wait_for_health(report, product, previous_pid=live[1]["pid"])
    report.data["restarted_service"] = restarted[1]
    verify_migration(report, restarted, selected)
    report.data["restart_verification"] = "pass"
    report.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checks", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--pilot", default=None)
    args = parser.parse_args()
    if not args.checks and not (args.full or args.acceptance):
        parser.error("Use --checks or --full")
    if args.pilot and args.pilot != "include-what-you-use":
        parser.error("Only the IWYU migration pilot is accepted automatically")
    report = Report("isolated_code_checks" if args.checks else "full_local_acceptance")
    code = 2
    try:
        if args.checks:
            prerequisites(report)
            isolated_checks(report, reuse=False)
        else:
            print("הפקודה עשויה להחליף את אפליקציית Mac Update Manager המקומית לפני אישור חבילות.", flush=True)
            perform_full(report, args.pilot)
        code = 0
        report.data["result"] = "pass"
    except Blocked as exc:
        report.data.update(result="blocked", error=str(exc))
        code = 2
    except (KeyboardInterrupt, Exception) as exc:
        report.data.update(result="fail", error=str(exc))
        code = 1
    finally:
        report.data["finished_at"] = time.time()
        report.data["exit_code"] = code
        report.save()
        print("דוח:", report.dir, flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
