"""Preflighted updates with durable outcomes and isolated binary tool installs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tarfile
import time
from urllib.request import Request, urlopen
import zipfile

import update_routes as routes
import macports_provider
import installation_evidence


class Engine:
    def __init__(self, service):
        self.s = service
        self.settings = service.load_settings()
        self.platform = "osx-arm64" if platform.machine() == "arm64" else "osx-64"
        self.brew_env = {"HOMEBREW_NO_AUTOREMOVE": "1", "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1",
                         "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1"}
        self.env = {"PIXI_HOME": str(service.TOOLS_ROOT / "pixi"),
                    "PIXI_CACHE_DIR": str(Path.home() / "Library/Caches/Homebrew Update Manager/pixi"),
                    "PIXI_BIN": str(service.DATA_DIR / "runtime/bin/pixi"),
                    "PIXI_PLATFORM": self.platform}

    def result(self, name, kind, **values):
        return {"name": name, "kind": kind, "ok": False, "code": 0, "stdout": "", "stderr": "", **values}

    def catalog(self):
        major = int(platform.mac_ver()[0].split(".")[0])
        codename = {11: "big_sur", 12: "monterey", 13: "ventura", 14: "sonoma", 15: "sequoia", 26: "tahoe", 27: "golden_gate"}.get(major)
        tag = ("arm64_" if self.platform == "osx-arm64" else "") + str(codename)
        path = Path.home() / f"Library/Caches/Homebrew/api/internal/packages.{tag}.jws.json.payload"
        with path.open() as stream:
            next(stream)
            return json.load(stream)["formulae"]

    def installed(self):
        result = self.s.run_cmd([self.s.brew_bin(), "info", "--json=v2", "--installed"], timeout=300,
                               env_override=self.brew_env)
        if result.code:
            raise RuntimeError(result.stderr or result.stdout)
        return installation_evidence.verified_dependencies(self.s, json.loads(result.stdout))

    def check_cancel(self):
        if self.s.is_cancel_requested():
            raise self.s.OperationCancelledError("Update canceled")

    def native_asset(self, name, version):
        version = routes.package_version(version)
        arch = "arm64" if self.platform == "osx-arm64" else "x64"
        def fetch(url):
            with urlopen(Request(url, headers={"User-Agent": "Homebrew-Update-Manager"}), timeout=30) as response:
                return response.read()
        if name == "node":
            filename = f"node-v{version}-darwin-{arch}.tar.xz"
            base = f"https://nodejs.org/dist/v{version}/"
            sums = fetch(base + "SHASUMS256.txt").decode()
            digest = next((line.split()[0] for line in sums.splitlines() if line.split()[-1] == filename), None)
            url = base + filename
        else:
            arch = "aarch64" if arch == "arm64" else "x86_64"
            filename = f"deno-{arch}-apple-darwin.zip"
            data = json.loads(fetch(f"https://api.github.com/repos/denoland/deno/releases/tags/v{version}"))
            asset = next(x for x in data["assets"] if x["name"] == filename)
            url = asset["browser_download_url"]
            digest = str(asset.get("digest", "")).removeprefix("sha256:")
            if not digest:
                checksum = next(x for x in data["assets"] if x["name"] == filename + ".sha256sum")
                digest = fetch(checksum["browser_download_url"]).decode().split()[0]
        if not digest or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("Publisher did not supply an SHA-256 checksum")
        return {"url": url, "sha256": digest, "filename": filename}

    def plan(self, packages):
        self.s.set_check_progress(phase="preflight", message="Checking binary packages and required dependencies", eta_seconds=None)
        try:
            catalog, installed = self.catalog(), self.installed()
        except Exception as error:
            catalog, installed = {}, {}
            self.s.append_service_log(f"Binary metadata unavailable: {error}")
        plan = []
        records = routes.read_records(self.s.TOOLS_ROOT)
        for pkg in packages:
            self.check_cancel()
            name, kind = pkg["name"], pkg["kind"]
            if pkg.get("inventory_stale"):
                plan.append({**pkg, "version": pkg.get("latest_version", "unknown"), "ready": False, "reason": "inventory_unavailable"})
                continue
            if kind == "port":
                plan.append(macports_provider.plan(self.s, pkg))
                continue
            item = {"name": name, "kind": kind, "provider": routes.route_for(name, "formula" if kind == "tool" else kind, self.settings),
                    "version": pkg.get("latest_version", ""), "ready": True}
            if kind == "formula" and name in records:
                item["provider"] = "homebrew"
            if kind == "tool" and name in records:
                item["provider"] = records[name]["provider"]
            if kind != "formula" and records.get(name, {}).get("provider") == "standalone_uv":
                item["provider"] = "standalone_uv"
            if self.s.is_excluded(name, kind):
                item.update(ready=False, reason="excluded", blockers=[])
            elif item["provider"] == "standalone_uv":
                executable = Path(records[name]["prefix"]) / "bin/uv"
                checked = self.s.run_cmd([str(executable), "self", "update", routes.package_version(item["version"]),
                                          "--dry-run", "--no-config"], timeout=60, cancellable=True)
                if checked.code:
                    item.update(ready=False, reason="binary_route_unavailable", detail=checked.stderr)
            elif item["provider"] == "native":
                try:
                    item["asset"] = self.native_asset(name, item["version"])
                except Exception as error:
                    item.update(ready=False, reason="binary_route_unavailable", detail=str(error))
            elif item["provider"] == "pixi":
                try:
                    if not Path(self.env["PIXI_BIN"]).is_file():
                        raise RuntimeError("Pixi runtime is not installed")
                    directory, manifest = routes.prepare_workspace(self.s.TOOLS_ROOT, name, item["version"], self.platform)
                    item.update(workspace=str(directory), manifest=str(manifest))
                    result = self.s.run_cmd([self.env["PIXI_BIN"], "lock", "--no-install", "--no-config",
                                             "--concurrent-downloads", "3", "--concurrent-solves", "1",
                                             "--manifest-path", str(manifest)], timeout=180, cancellable=True,
                                            env_override=self.env)
                    self.check_cancel()
                    if result.code:
                        raise RuntimeError(result.stderr or result.stdout)
                    urls = routes.validate_lock(manifest, "", self.s.run_cmd, self.env)
                    item["binary_count"] = len(urls)
                except self.s.OperationCancelledError:
                    raise
                except Exception as error:
                    item.update(ready=False, reason="binary_route_unavailable", detail=str(error)[-1800:])
            elif item["provider"] == "uv":
                if not (self.s.DATA_DIR / "runtime/bin/uv").exists():
                    item.update(ready=False, reason="binary_route_unavailable", detail="uv runtime is not installed")
            elif kind == "formula" and self.settings["binary_only"]:
                blockers = routes.bottle_blockers(name, catalog, installed, self.settings["excluded_packages"])
                if blockers:
                    item.update(ready=False, reason="source_required", blockers=blockers)
            plan.append(item)
            self.s.set_check_progress(current_package=f"{kind}:{name}", message=f"Checking update route for {name}")
        self.s.atomic_write_json(self.s.UPDATE_PLAN_FILE, {"created_at": self.s.now_iso(), "items": plan})
        return plan

    def install_tool(self, item):
        if item["provider"] == "macports":
            return macports_provider.upgrade(self.s, item)
        name, provider = item["name"], item["provider"]
        activity = self.s.UpgradeActivity(name)
        activity.state["provider"] = provider
        activity.observe(f"Downloading ready-to-use {name} packages ({provider})", "provider")
        if provider == "standalone_uv":
            record = routes.read_records(self.s.TOOLS_ROOT)[name]
            executable = Path(record["prefix"]) / "bin/uv"
            result = self.s.run_cmd([str(executable), "self", "update", routes.package_version(item["version"]),
                                     "--no-config"], timeout=None, cancellable=True, on_output=activity.observe)
            if result.code:
                return self.result(name, "formula", code=result.code, stderr=result.stderr, stdout=result.stdout)
            checked = self.s.run_cmd([str(executable), "--version"], timeout=30)
            version = re.search(r"uv (\S+)", checked.stdout)
            if checked.code or not version or routes.version_key(version[1]) < routes.version_key(item["version"]):
                return self.result(name, "formula", code=1, stderr="Standalone uv verification failed")
            record.update(version=version[1], verified_at=time.time(), probe_output=checked.stdout)
            records = routes.read_records(self.s.TOOLS_ROOT)
            records[name] = record
            self.s.atomic_write_json(self.s.TOOLS_ROOT / "installed.json", records)
            return self.result(name, "formula", ok=True, provider=provider, installed_version=version[1], stdout=checked.stdout)
        if provider == "pixi":
            directory = Path(item["workspace"])
            result = self.s.run_cmd([self.env["PIXI_BIN"], "install", "--frozen", "--no-config", "--concurrent-downloads", "3",
                                     "--manifest-path", item["manifest"]], timeout=None, cancellable=True,
                                    env_override=self.env, on_output=activity.observe)
            prefix = directory / ".pixi/envs/default"
            conda_name, executables, probe = routes.PIXI_TOOLS[name]
            version = None
            if result.code == 0:
                for record in (prefix / "conda-meta").glob("*.json"):
                    data = json.loads(record.read_text())
                    if data.get("name") == conda_name:
                        version = data["version"]
                        break
        elif provider == "native":
            directory = self.s.TOOLS_ROOT / "environments" / name / str(time.time_ns())
            directory.mkdir(parents=True)
            asset = item["asset"]
            archive = directory / asset["filename"]
            digest = hashlib.sha256()
            with urlopen(Request(asset["url"], headers={"User-Agent": "Homebrew-Update-Manager"}), timeout=30) as response, archive.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    self.check_cancel()
                    digest.update(chunk)
                    stream.write(chunk)
            if digest.hexdigest() != asset["sha256"]:
                raise RuntimeError("Publisher checksum mismatch")
            prefix = directory / "prefix"
            if name == "node":
                with tarfile.open(archive) as tar:
                    tar.extractall(directory / "unpacked", filter="data")
                source = next((directory / "unpacked").iterdir())
                source.rename(prefix)
                executables, probe = ["node", "npm", "npx"], ["node", "--version"]
            else:
                (prefix / "bin").mkdir(parents=True)
                with zipfile.ZipFile(archive) as zipped:
                    with zipped.open("deno") as source, (prefix / "bin/deno").open("wb") as target:
                        shutil.copyfileobj(source, target)
                (prefix / "bin/deno").chmod(0o755)
                executables, probe = ["deno"], ["deno", "--version"]
            version, result = item["version"], self.s.CommandResult(0, "Checksum verified", "")
        else:
            directory = self.s.TOOLS_ROOT / "environments" / name / str(time.time_ns())
            prefix = directory / "venv"
            uv = str(self.s.DATA_DIR / "runtime/bin/uv")
            result = self.s.run_cmd([uv, "venv", "--python", str(Path(os.sys.executable).resolve()), str(prefix)],
                                    timeout=60, cancellable=True, on_output=activity.observe)
            if result.code == 0:
                result = self.s.run_cmd([uv, "pip", "install", "--python", str(prefix / "bin/python"),
                                         "--only-binary", ":all:", "--prerelease", "allow",
                                         f"azure-cli=={routes.package_version(item['version'])}"],
                                        timeout=None, cancellable=True, on_output=activity.observe)
            executables, probe, version = ["az"], ["az", "version", "--output", "json"], item["version"]
        self.check_cancel()
        if result.code:
            return self.result(name, "formula", code=result.code, stderr=result.stderr, stdout=result.stdout, provider=provider)
        if not version or routes.version_key(version) < routes.version_key(item["version"]):
            return self.result(name, "formula", code=1, stderr="Installed provider version is below the requested version")
        activity.observe("Verifying installed commands", "provider")
        probe_env = {"PATH": str(prefix / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin", "CONDA_PREFIX": str(prefix)}
        checked = self.s.run_cmd([str(prefix / "bin" / probe[0]), *probe[1:]], timeout=90, env_override=probe_env)
        if checked.code:
            return self.result(name, "formula", code=checked.code, stderr=checked.stderr or checked.stdout)
        checks = {"node": ["node", "-e", "if(new URL('https://example.com/a').hostname!=='example.com')process.exit(1)"],
                  "deno": ["deno", "eval", "if(new URL('https://example.com').hostname!=='example.com')Deno.exit(1)"],
                  "ffmpeg": ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:rate=1", "-frames:v", "1", "-f", "null", "-"],
                  "jq": ["jq", "-n", "{verified:true}"],
                  "uv": ["uv", "python", "list", "--only-installed"]}
        if name in checks:
            args = checks[name]
            smoke = self.s.run_cmd([str(prefix / "bin" / args[0]), *args[1:]], timeout=90, env_override=probe_env)
            if smoke.code:
                return self.result(name, "formula", code=smoke.code, stderr=smoke.stderr or smoke.stdout)
        if name == "node":
            npm = self.s.run_cmd([str(prefix / "bin/npm"), "--version"], timeout=60, env_override=probe_env)
            if npm.code:
                return self.result(name, "formula", code=npm.code, stderr=npm.stderr or npm.stdout)
        record = routes.activate(self.s.TOOLS_ROOT, name, provider, directory, prefix, version,
                                  executables, checked.stdout + checked.stderr)
        return self.result(name, "formula", ok=True, provider=provider, installed_version=record["version"],
                           stdout=f"Installed and verified {name} {version} using {provider}")

    def run(self, selected=None, track_progress=False):
        s = self.s
        s.reset_check_progress_for_run()
        s.set_check_progress(operation_name="update_selected" if selected is not None else "update_all",
                             phase="preflight", message="Refreshing installed versions before planning", eta_seconds=None)
        state = s.compute_snapshot(False, enrich_details=False)
        wanted = None if selected is None else {(p["kind"], p["name"]) for p in selected}
        packages = [p for p in state["packages"] if p.get("outdated") and not p.get("excluded")
                    and (wanted is None or (p["kind"], p["name"]) in wanted)]
        try:
            plan = self.plan(packages)
        except s.OperationCancelledError:
            s.set_check_progress(running=False, phase="canceled", message="Update planning canceled",
                                 current_package="", canceled=True, completed_at=s.now_iso(), eta_seconds=0)
            return {"ok": False, "canceled": True, "attempted_count": 0, "updated_count": 0,
                    "failed_count": 0, "deferred_count": 0, "remaining_count": len(packages),
                    "results": [], "deferred_packages": [], "snapshot": state, "inventory_refresh_error": None}
        ready, deferred = [p for p in plan if p["ready"]], [p for p in plan if not p["ready"]]
        completed, failed, results = [], [], []
        journal = {"started_at": s.now_iso(), "plan": plan, "results": results, "status": "running"}
        s.atomic_write_json(s.RUN_JOURNAL_FILE, journal)
        with s.CHECK_PROGRESS_LOCK:
            s.CHECK_PROGRESS["deferred_packages"] = deferred
        total = len(ready)
        planned = [s.progress_package_item(p["name"], p["kind"]) for p in ready]
        stopped, canceled = False, False
        for idx, item in enumerate(ready):
            if s.is_cancel_requested():
                canceled = True
                break
            started = s.now_iso()
            s.set_check_progress(phase="updating_packages", message=f"Updating {item['name']} using {item['provider']}",
                                 done=idx, total=total, eta_seconds=None, current_package=f"{item['kind']}:{item['name']}",
                                 planned_packages=planned, completed_packages=completed, failed_packages=failed,
                                 remaining_packages=planned[idx:])
            try:
                if item["provider"] == "homebrew" and (item["kind"], item["name"]) not in s.fetch_outdated_index():
                    result = self.result(item["name"], item["kind"], ok=True, skipped=True, reason="not_outdated")
                else:
                    result = (self.install_tool(item) if item["provider"] != "homebrew"
                              else s.run_upgrade(item["name"], item["kind"]))
            except s.OperationCancelledError:
                result = self.result(item["name"], item["kind"], canceled=True, code=130, stderr="Canceled")
            except Exception as error:
                result = self.result(item["name"], item["kind"], code=1, stderr=str(error))
            finally:
                with s.CHECK_PROGRESS_LOCK:
                    s.CHECK_PROGRESS["package_activity"] = None
            result["kind"] = item["kind"]
            result["started_at"], result["completed_at"] = started, s.now_iso()
            results.append(result)
            progress = s.progress_result_item(result, started_at=started)
            completed.append(progress)
            s.atomic_write_json(s.RUN_JOURNAL_FILE, journal)
            if not result.get("ok"):
                failed.append(progress)
                canceled = bool(result.get("canceled") or s.is_cancel_requested())
                stopped = not canceled
                # Fail closed: never let later roots reattempt a failed shared
                # dependency during this run, or conceal a broken linkage.
                break
        s.set_check_progress(phase="preparing", message="Verifying installed versions", eta_seconds=None, current_package="")
        refreshed, refresh_error = s.safe_refresh_snapshot(state)
        refreshed["update_plan"] = s.read_json(s.UPDATE_PLAN_FILE)
        s.append_update_history_entries(s.history_entries_from_results(results, refreshed, snapshot_verified=refresh_error is None,
                                                                      snapshot_error=refresh_error))
        remaining = planned[len(results):]
        phase = "canceled" if canceled else "error" if stopped or refresh_error else "completed"
        updated = sum(bool(r.get("ok")) and not r.get("skipped") for r in results)
        message = ("Update canceled" if canceled else "Updates stopped after a failure; later packages were preserved" if stopped
                   else f"Finished {updated} updates; {len(deferred)} need another installation route")
        journal.update(status=phase, completed_at=s.now_iso(), deferred_count=len(deferred), inventory_refresh_error=refresh_error)
        s.atomic_write_json(s.RUN_JOURNAL_FILE, journal)
        s.set_check_progress(running=False, phase=phase, message=message, done=len(results), total=total,
                             eta_seconds=0, current_package="", completed_packages=completed,
                             failed_packages=failed, remaining_packages=remaining, canceled=canceled,
                             error=refresh_error or ("stopped_after_failure" if stopped else None), completed_at=s.now_iso())
        return {"ok": not (stopped or canceled or refresh_error), "canceled": canceled, "attempted_count": len(results),
                "updated_count": updated, "failed_count": len(failed),
                "deferred_count": len(deferred), "skipped_count": sum(bool(r.get("skipped")) for r in results) + len(deferred),
                "remaining_count": len(remaining), "results": results,
                "deferred_packages": deferred, "snapshot": refreshed, "inventory_refresh_error": refresh_error}
