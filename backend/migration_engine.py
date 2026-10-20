"""Journalled Intel Homebrew->MacPorts migrations with guarded recovery.

No source removal is attempted until the source is backed up, binary graph and
smoke probes have passed, and owned command routing has been verified.
Unresolved transactions never automatically resume on process startup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

import installation_evidence
import macports_provider as mp
import migration_profiles as profiles
import provider_ownership as ownership
import routing_activation as routing
from update_selection import compare_upstream, material_fingerprint

SCHEMA = 2
FINISHED = {"completed", "rolled_back", "canceled"}


class MigrationConflict(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code


def _root(s):
    return Path(s.DATA_DIR) / "migrations"


def _plan_path(s, plan_id):
    return _root(s) / "plans" / (plan_id + ".json")


def _transaction_path(s, transaction_id):
    return _root(s) / "transactions" / transaction_id


def _write(path, data):
    ownership.atomic_write_json(path, data)


def _read(s, path):
    return s.read_json(path)


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _check_id(value):
    if not isinstance(value, str) or len(value) != 32 or any(
            char not in "0123456789abcdef" for char in value):
        raise MigrationConflict("invalid_identifier")
    return value


def _source(s, name):
    inventory = s.fetch_installed_inventory(s.fetch_outdated_index())
    rows = [r for r in inventory if r.get("name") == name
            and r.get("kind") == "formula"
            and r.get("brew_installation_present") is not False]
    if len(rows) != 1:
        raise MigrationConflict("source_installation_ambiguous")
    row = rows[0]
    audit = row.get("inventory_verification") or {}
    keg = Path(audit.get("active_keg") or "/nonexistent")
    receipt = keg / "INSTALL_RECEIPT.json"
    if (audit.get("status") != "verified" or not keg.is_dir() or
            keg.is_symlink() or not receipt.is_file() or
            len(audit.get("manager_versions") or []) != 1):
        raise MigrationConflict("source_evidence_unverified")
    return row, audit, keg, receipt


def _source_commands(keg, profile):
    commands = {}
    for command in profile.commands:
        path = keg / "bin" / command
        if not path.is_file():
            raise MigrationConflict("source_command_missing:" + command)
        commands[command] = str(path)
    return commands


def _fresh_classification(s, name, row, *, provided=None):
    """Validate the same T02 classification against a fresh source audit."""
    if provided is None:
        state = s.read_json(s.STATE_FILE) or {}
        entries = [x for x in state.get("packages", [])
                   if x.get("provider") == "homebrew"
                   and x.get("kind") == "formula" and x.get("name") == name]
        provided = entries[0].get("classification") if len(entries) == 1 else None
        if provided is None:
            offer = next((x for x in state.get("migrations", [])
                          if x.get("name") == name), None)
            provided = (offer or {}).get("classification")
    cls = provided if isinstance(provided, dict) else {}
    audit = row.get("inventory_verification") or {}
    if (cls.get("category") != "standalone_candidate" or cls.get("gaps")
            or cls.get("consumers") or cls.get("shared_roles")
            or cls.get("inventory_fingerprint") != installation_evidence.fingerprint(audit)):
        raise MigrationConflict("standalone_classification_blocked")
    checked = cls.get("checked_at")
    if not isinstance(checked, str):
        raise MigrationConflict("classification_timestamp_missing")
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(checked.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timezone is required")
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
    except (ValueError, TypeError) as error:
        raise MigrationConflict("classification_timestamp_invalid") from error
    if age < -60 or age >= 900:
        raise MigrationConflict("classification_stale")
    return cls

def _consumers(s, name):
    check = s.run_cmd([s.brew_bin(), "uses", "--installed", name],
                      timeout=90, cancellable=True)
    if check.code:
        raise MigrationConflict("consumer_check_failed", check.stderr or check.stdout)
    items = [x.strip() for x in check.stdout.splitlines() if x.strip()]
    if items:
        raise MigrationConflict("source_has_consumers", ", ".join(items))
    return items


def _target_graph(s, profile):
    graph = mp.preflight(s, list(profile.ports))
    nodes = graph.get("nodes") or []
    root = next((x for x in nodes if x.get("name") == profile.ports[0]), None)
    if root is None:
        raise MigrationConflict("target_graph_missing_root")
    return graph, root


def _route_ownership(s, name):
    registry = ownership.load_routes(s.TOOLS_ROOT)
    if registry.get("error"):
        raise MigrationConflict("route_registry_unverified")
    if any(x.get("logical_id") == name for x in registry.get("legacy", [])):
        raise MigrationConflict("legacy_route_requires_review")
    prior = next((x for x in registry["routes"] if x.get("logical_id") == name
                  and x.get("status") == "committed"), None)
    return prior, registry


def _material_pixi(s, name, *, classification=None, candidate=None,
                   generation_id=None):
    """Read-only Intel preflight for a fixed-profile Homebrew-to-Pixi route."""
    from pathlib import Path
    import pixi_provider
    import update_routes
    profiles.intel_only()
    profile = profiles.pixi_get(name)
    if not profile:
        raise MigrationConflict("pixi_migration_profile_missing")
    if name == "ngrok" or s.is_excluded(name, "formula"):
        raise MigrationConflict("package_excluded")
    if update_routes.read_records(s.TOOLS_ROOT).get(name):
        raise MigrationConflict("existing_managed_tool_conflict")
    row, audit, keg, receipt = _source(s, name)
    cls = _fresh_classification(s, name, row, provided=classification)
    _consumers(s, name)
    fresh = candidate if candidate is not None else pixi_provider.discover(s, name)
    if (not isinstance(fresh, dict) or fresh.get("provider") != "pixi"
            or fresh.get("availability") != "verified"
            or fresh.get("execution_supported") is not True
            or fresh.get("closure_verified") is not True
            or fresh.get("system", {}).get("arch") != "x86_64"
            or fresh.get("system", {}).get("os") != "macos"):
        raise MigrationConflict("pixi_binary_route_unverified")
    import update_routes
    if fresh.get("provider_names") != [update_routes.PIXI_TOOLS[name][0]]:
        raise MigrationConflict("pixi_package_identity_mismatch")
    version = fresh.get("upstream_version")
    current = row.get("installed_upstream_version") or row.get("installed_version")
    if compare_upstream(version, current) != 1:
        raise MigrationConflict("migration_downgrade_or_unknown")
    lock = fresh.get("lock")
    manifest = fresh.get("manifest")
    if (not isinstance(lock, str) or not isinstance(manifest, str)
            or hashlib.sha256(lock.encode()).hexdigest() != fresh.get("lock_sha256")
            or manifest != pixi_provider.manifest_text(name, version)):
        raise MigrationConflict("pixi_lock_or_manifest_unverified")
    closure = fresh.get("closure")
    if (not isinstance(closure, list) or not closure
            or any(not isinstance(node, dict)
                   or node.get("arch") not in ("osx-64", "noarch")
                   or not node.get("archive_id") or not node.get("checksum")
                   for node in closure)
            or not any(n.get("arch") == "osx-64" for n in closure)):
        raise MigrationConflict("pixi_binary_closure_unverified")
    generation_id = _check_id(generation_id or uuid.uuid4().hex)
    directory = Path(s.TOOLS_ROOT) / "environments" / name / ("generation-" + generation_id)
    if os.path.lexists(directory):
        raise MigrationConflict("pixi_generation_conflict")
    prefix = directory / ".pixi" / "envs" / "default"
    target_commands = {command: str(prefix / "bin" / command)
                       for command in profile.commands}
    previous, registry = _route_ownership(s, name)
    source_commands = _source_commands(keg, profile)
    owned_shims = (previous or {}).get("owned_shims", {})
    routing.preflight(s.TOOLS_ROOT, name, target_commands, owned_shims=owned_shims)
    route = {key: fresh.get(key) for key in (
        "provider", "availability", "execution_supported", "closure_verified",
        "upstream_version", "package_version", "revision", "provider_names",
        "system", "closure", "manifest", "lock", "lock_sha256")}
    source = {"name": name, "kind": "formula", "provider": "homebrew",
              "installation_id": "homebrew:formula:" + name,
              "version": row["installed_version"], "keg": str(keg),
              "receipt_sha256": _hash(receipt),
              "manager_versions": audit.get("manager_versions"),
              "commands": source_commands}
    target = {"provider": "pixi", "installation_id": "pixi:" + name,
              "version": version, "revision": None,
              "generation_id": generation_id, "directory": str(directory),
              "prefix": str(prefix), "candidate": route,
              "mapped_name": fresh["provider_names"][0],
              "commands": target_commands, "ports": []}
    core = {"source": source, "target": target, "profile": name,
            "profile_version": profile.version, "classification": cls,
            "consumers": [], "route_generation": (previous or {}).get("generation", 0),
            "owned_shims": owned_shims, "system": fresh["system"],
            "exclusions": s.load_settings().get("excluded_packages", [])}
    return core, material_fingerprint(source, core, previous or {})


def _material(s, name, *, classification=None, provider="macports",
              pixi_candidate=None, planned_generation=None):
    if provider == "pixi":
        return _material_pixi(s, name, classification=classification,
                              candidate=pixi_candidate,
                              generation_id=planned_generation)
    if provider != "macports":
        raise MigrationConflict("unsupported_target_provider")
    profiles.intel_only()
    if name == "ngrok" or s.is_excluded(name, "formula"):
        raise MigrationConflict("package_excluded")
    profile = profiles.get(name)
    if not profile or profile.provider != "macports":
        raise MigrationConflict("unsupported_migration_profile")
    row, audit, keg, receipt = _source(s, name)
    cls = _fresh_classification(s, name, row, provided=classification)
    _consumers(s, name)
    graph, target = _target_graph(s, profile)
    installed_upstream = row.get("installed_upstream_version") or row["installed_version"]
    if compare_upstream(target["version"], installed_upstream) != 1:
        raise MigrationConflict("migration_downgrade_or_unknown")
    existing = {x["name"]: x for x in mp.inventory(s)}
    for node in graph["nodes"]:
        old = existing.get(node["name"])
        if old:
            expected = str(node["version"]) + (
                "_" + str(node["revision"]) if node.get("revision") not in ("", "0", None)
                else "")
            if old["installed_version"] != expected or old.get("installed_variants", "") != node.get("variants", ""):
                raise MigrationConflict("existing_port_variant_or_version_conflict:" + node["name"])
    prior, registry = _route_ownership(s, name)
    source_commands = _source_commands(keg, profile)
    target_commands = {key: str(path) for key, path in profiles.command_paths(profile).items()}
    owned_shims = (prior or {}).get("owned_shims", {})
    routing.preflight(s.TOOLS_ROOT, name, target_commands, owned_shims=owned_shims)
    source = {
        "name": name, "kind": "formula", "provider": "homebrew",
        "installation_id": "homebrew:formula:" + name,
        "version": row["installed_version"], "keg": str(keg),
        "receipt_sha256": _hash(receipt),
        "manager_versions": audit.get("manager_versions"),
        "commands": source_commands,
    }
    target_data = {
        "provider": "macports", "installation_id": "macports:" + profile.ports[0],
        "version": target["version"], "revision": target.get("revision"),
        "graph": graph["nodes"], "commands": target_commands,
        "ports": list(profile.ports),
    }
    core = {"source": source, "target": target_data,
            "classification": cls, "consumers": [], "profile": name,
            "profile_version": profile.version,
            "route_generation": (prior or {}).get("generation", 0),
            "owned_shims": owned_shims, "system": graph.get("system"),
            "exclusions": s.load_settings().get("excluded_packages", [])}
    return core, material_fingerprint(source, core, prior or {})


def plan(s, name, action="migrate", transaction_id=None, *,
         classification=None, provider="macports", pixi_candidate=None):
    if action != "migrate":
        return recovery_plan(s, transaction_id, action)
    core, fingerprint = _material(s, name, classification=classification,
                                  provider=provider, pixi_candidate=pixi_candidate)
    plan_id = uuid.uuid4().hex
    created = time.time()
    data = {"schema_version": SCHEMA, "id": plan_id,
            "action": "migrate", "created_at": created,
            "expires_at": created + 900,
            "fingerprint": fingerprint, **core}
    _write(_plan_path(s, plan_id), data)
    return data


def _load_plan(s, plan_id):
    plan_id = _check_id(plan_id)
    record = _read(s, _plan_path(s, plan_id))
    if not record or record.get("schema_version") != SCHEMA:
        raise MigrationConflict("migration_plan_not_found")
    if record["expires_at"] < time.time():
        raise MigrationConflict("migration_plan_expired")
    return record


def _manifest(root):
    result = []
    root = Path(root)
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        mode = path.lstat().st_mode
        kind = "link" if stat.S_ISLNK(mode) else "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "special"
        if kind == "special":
            raise MigrationConflict("unsupported_source_file_type:" + name)
        item = {"path": name, "kind": kind, "mode": stat.S_IMODE(mode)}
        if kind == "link":
            item["target"] = os.readlink(path)
        if kind == "file":
            item["sha256"] = _hash(path)
        result.append(item)
    return result


def _backup(s, journal, directory):
    source = Path(journal["plan"]["source"]["keg"])
    if not source.is_dir():
        raise MigrationConflict("source_keg_disappeared")
    target = directory / "backup" / "keg"
    if target.exists():
        raise MigrationConflict("backup_already_exists")
    initial = _manifest(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
    if _manifest(target) != initial:
        raise MigrationConflict("backup_integrity_failed")
    manifest_path = directory / "backup" / "manifest.json"
    _write(manifest_path, {"entries": initial, "source": str(source)})
    journal["backup"] = {"path": str(target), "manifest": str(manifest_path),
                         "manifest_sha256": _hash(manifest_path)}
    _write(directory / "journal.json", journal)


def _phase(journal, directory, phase, state, **values):
    if state not in ("started", "finished"):
        raise ValueError("Invalid phase status")
    journal["stage"] = phase
    journal.setdefault("phases", {})[phase] = {
        "status": state, "timestamp": time.time(), **values}
    journal["updated_at"] = time.time()
    _write(directory / "journal.json", journal)


def _verify_backup(journal):
    backup = journal.get("backup") or {}
    manifest_path = Path(backup.get("manifest") or "/nonexistent")
    saved = Path(backup.get("path") or "/nonexistent")
    if not manifest_path.is_file() or _hash(manifest_path) != backup.get("manifest_sha256"):
        raise MigrationConflict("backup_manifest_changed")
    data = json.loads(manifest_path.read_text())
    if not saved.is_dir() or _manifest(saved) != data["entries"]:
        raise MigrationConflict("backup_contents_changed")
    return saved, data["entries"]


def _target_ready(s, plan):
    target = plan["target"]
    if target["provider"] == "pixi":
        import pixi_provider
        profile = profiles.pixi_get(plan["profile"])
        if not profile:
            raise MigrationConflict("pixi_migration_profile_missing")
        prefix = Path(target["prefix"])
        if (not prefix.is_dir() or prefix.is_symlink()
                or not prefix.resolve().is_relative_to(
                    (Path(s.TOOLS_ROOT) / "environments" / plan["profile"]).resolve())):
            raise MigrationConflict("pixi_target_prefix_unverified")
        pixi_provider.installed_record(prefix, target["mapped_name"], target["version"])
        pixi_provider.verify_generation(prefix, target["candidate"]["closure"])
        return profiles.smoke_pixi(s, profile, prefix, target["version"])
    profile = profiles.get(plan["profile"])
    current = {p["name"]: p for p in mp.inventory(s)}
    root = current.get(profile.ports[0])
    if not root:
        raise MigrationConflict("target_installation_missing")
    expected = plan["target"]["version"]
    observed = root["installed_version"]
    expected_revision = str(plan["target"].get("revision") or "0")
    wanted = expected + ("_" + expected_revision if expected_revision != "0" else "")
    if (observed != wanted or root.get("installed_variants", "") !=
            next((n.get("variants", "") for n in plan["target"]["graph"]
                  if n["name"] == profile.ports[0]), "")):
        raise MigrationConflict("target_version_or_variant_mismatch")
    smoke = profiles.smoke(s, profile, expected)
    return smoke


def _source_still_safe(s, plan):
    name = plan["source"]["name"]
    current, _, keg, receipt = _source(s, name)
    if str(keg) != plan["source"]["keg"] or _hash(receipt) != plan["source"]["receipt_sha256"]:
        raise MigrationConflict("source_installation_changed")
    _consumers(s, name)
    if s.is_excluded(name, "formula"):
        raise MigrationConflict("package_excluded")
    return current


def _target_postcheck(s, journal):
    target = journal["plan"]["target"]
    name = journal["plan"]["source"]["name"]
    activation = journal.get("activation") or {}
    if not activation:
        raise MigrationConflict("routing_not_activated")
    routing.verify_shell(s, s.TOOLS_ROOT, target["commands"])
    smoke = _target_ready(s, journal["plan"])
    primary = name
    managed = Path(s.TOOLS_ROOT) / "bin" / primary
    profile = (profiles.pixi_get(name) if target["provider"] == "pixi"
               else profiles.get(name))
    result = s.run_cmd([str(managed), *profile.version_args],
                       timeout=30, cancellable=True)
    if result.code or not profiles.version_matches(result.stdout + result.stderr,
                                                    target["version"]):
        raise MigrationConflict("managed_command_postcheck_failed")
    return smoke


def _compensate(s, journal, directory):
    """Compensate without observing the original cancel flag."""
    activation = journal.get("activation")
    if activation:
        routing.restore(s.TOOLS_ROOT, activation)
        journal["activation_rolled_back"] = True
    source = Path(journal["plan"]["source"]["keg"])
    if not source.is_dir():
        backup, _ = _verify_backup(journal)
        if os.path.lexists(source):
            raise MigrationConflict("source_path_changed_independently")
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup, source, symlinks=True, copy_function=shutil.copy2)
        if _manifest(source) != _manifest(backup):
            raise MigrationConflict("restored_source_hash_mismatch")
        linked = s.run_cmd([s.brew_bin(), "link", journal["plan"]["source"]["name"]],
                           timeout=180, cancellable=False,
                           env_override={"HOMEBREW_NO_AUTO_UPDATE": "1",
                                         "HOMEBREW_NO_AUTOREMOVE": "1",
                                         "HOMEBREW_NO_INSTALL_CLEANUP": "1"})
        if linked.code:
            raise MigrationConflict("source_relink_failed", linked.stderr)
    if journal["plan"]["target"]["provider"] == "pixi":
        _remove_pixi_target_record(s, journal)
    return True


def _register_pixi_target(s, journal, directory):
    """Publish physical Pixi evidence only after verified route postcheck."""
    import pixi_provider
    import update_routes
    from provider_ownership import atomic_write_json
    plan = journal["plan"]
    target = plan["target"]
    prefix = Path(target["prefix"])
    evidence = pixi_provider.installed_record(
        prefix, target["mapped_name"], target["version"])
    pixi_provider.verify_generation(prefix, target["candidate"]["closure"])
    if not evidence.get("name"):
        raise MigrationConflict("pixi_installed_record_invalid")
    name = plan["profile"]
    records = update_routes.read_records(s.TOOLS_ROOT)
    existing = records.get(name)
    if existing and (existing.get("transaction_id") != journal["transaction_id"]
                     or existing.get("prefix") != str(prefix)):
        raise MigrationConflict("pixi_managed_record_conflict")
    record = {"provider": "pixi", "version": target["version"],
              "prefix": str(prefix), "workspace": target["directory"],
              "executables": list(target["commands"]),
              "owned_shims": routing.owned_shim_hashes(
                  s.TOOLS_ROOT, journal["activation"]),
              "transaction_id": journal["transaction_id"],
              "verified_at": time.time(), "probe_output": "transactional migration"}
    journal["pixi_managed_record"] = record
    _write(directory / "journal.json", journal)
    if existing != record:
        records[name] = record
        atomic_write_json(Path(s.TOOLS_ROOT) / "installed.json", records)
    journal["pixi_managed_record_written"] = True
    _write(directory / "journal.json", journal)
    return [{"name": name, "kind": "tool", "provider": "pixi",
             "installation_id": target["installation_id"],
             "installed_version": target["version"]}]


def _remove_pixi_target_record(s, journal):
    """Rollback only metadata written by this exact transaction."""
    import update_routes
    from provider_ownership import atomic_write_json
    name = journal["plan"]["profile"]
    target = journal["plan"]["target"]
    if target["provider"] != "pixi":
        return
    registry = ownership.load_routes(s.TOOLS_ROOT)
    if registry.get("error"):
        raise MigrationConflict("pixi_ownership_registry_unreadable")
    for route in registry.get("routes", []):
        if route.get("logical_id") == name:
            if route.get("transaction_id") != journal["transaction_id"]:
                raise MigrationConflict("pixi_ownership_changed_independently")
    kept = [r for r in registry["routes"]
            if r.get("transaction_id") != journal["transaction_id"]]
    if len(kept) != len(registry["routes"]):
        atomic_write_json(Path(s.TOOLS_ROOT) / "migrations" / "routes.json",
                          dict(registry, routes=kept))
    records = update_routes.read_records(s.TOOLS_ROOT)
    managed = records.get(name)
    if managed:
        if (managed.get("transaction_id") != journal["transaction_id"]
                or managed.get("prefix") != target["prefix"]):
            raise MigrationConflict("pixi_managed_record_changed_independently")
        records.pop(name)
        atomic_write_json(Path(s.TOOLS_ROOT) / "installed.json", records)


def _run(s, journal, directory, *, resume=False):
    plan_data = journal["plan"]
    provider = plan_data["target"]["provider"]
    profile = (profiles.pixi_get(plan_data["profile"]) if provider == "pixi"
               else profiles.get(plan_data["profile"]))
    if not profile:
        raise MigrationConflict("unsupported_migration_profile")
    try:
        if not resume:
            _phase(journal, directory, "backup", "started")
            _backup(s, journal, directory)
            _phase(journal, directory, "backup", "finished")
            _phase(journal, directory, "install", "started")
            if provider == "pixi":
                import pixi_provider
                target = plan_data["target"]
                prefix = pixi_provider.install_locked(
                    s, target["candidate"], Path(target["directory"]))
                if str(prefix) != target["prefix"]:
                    raise MigrationConflict("pixi_target_prefix_changed")
                _phase(journal, directory, "install", "finished", code=0)
            else:
                result = mp.privileged(s, "install", plan_data["target"]["ports"])
                if result.code:
                    raise MigrationConflict("target_install_failed", result.stderr or result.stdout)
                _phase(journal, directory, "install", "finished", code=result.code)
        else:
            _verify_backup(journal)
        _phase(journal, directory, "verify", "started")
        journal["smoke"] = _target_ready(s, plan_data)
        _phase(journal, directory, "verify", "finished")
        if not journal.get("activation"):
            _source_still_safe(s, plan_data)
            _phase(journal, directory, "activate", "started")
            def routing_progress(state):
                journal["activation_pending"] = state
                _write(directory / "journal.json", journal)
            journal["activation"] = routing.activate(
                s.TOOLS_ROOT, profile.name, journal["transaction_id"],
                plan_data["source"]["commands"], plan_data["target"]["commands"],
                owned_shims=plan_data.get("owned_shims"),
                progress=routing_progress)
            journal.pop("activation_pending", None)
            _phase(journal, directory, "activate", "finished")
        _phase(journal, directory, "cleanup", "started")
        source_keg = Path(plan_data["source"]["keg"])
        if source_keg.is_dir():
            _source_still_safe(s, plan_data)
            routing.verify_shell(s, s.TOOLS_ROOT, plan_data["target"]["commands"])
            _verify_backup(journal)
            result = s.run_cmd(
                [s.brew_bin(), "uninstall", "--formula", profile.name],
                timeout=300, cancellable=True,
                env_override={"HOMEBREW_NO_AUTO_UPDATE": "1",
                              "HOMEBREW_NO_AUTOREMOVE": "1",
                              "HOMEBREW_NO_INSTALL_CLEANUP": "1"})
            if result.code:
                raise MigrationConflict("source_uninstall_failed", result.stderr or result.stdout)
        _phase(journal, directory, "cleanup", "finished")
        _phase(journal, directory, "postcheck", "started")
        journal["postcheck"] = _target_postcheck(s, journal)
        _phase(journal, directory, "postcheck", "finished")
        _phase(journal, directory, "ownership", "started")
        route = {
            "logical_id": profile.name, "status": "committed",
            "source_installation_id": plan_data["source"]["installation_id"],
            "source_name": profile.name,
            "target_provider": provider,
            "target_installation_id": plan_data["target"]["installation_id"],
            "profile_id": profile.name, "profile_version": profile.version,
            "transaction_id": journal["transaction_id"],
            "owned_shims": routing.owned_shim_hashes(s.TOOLS_ROOT, journal["activation"]),
            "active": journal["activation"]["active"],
            "evidence_path": str(directory / "journal.json"),
        }
        installed_targets = (_register_pixi_target(s, journal, directory)
                             if provider == "pixi" else mp.inventory(s))
        ownership.commit_route(s.TOOLS_ROOT, route, directory / "journal.json",
                               installed_targets)
        _phase(journal, directory, "ownership", "finished")
        journal.update(status="completed", completed_at=time.time())
        _phase(journal, directory, "completed", "finished")
        return journal
    except BaseException as error:
        journal.update(status="needs_attention", error=str(error))
        _write(directory / "journal.json", journal)
        # Do not automatically destroy a potentially changed source after cleanup.
        # Before cleanup, compensate only ownership-controlled route files.
        if journal.get("stage") in ("backup", "install", "verify", "activate"):
            try:
                activation = journal.get("activation")
                if not activation:
                    pending = journal.get("activation_pending") or {}
                    if pending.get("state") == "pointer_switch_pending":
                        activation = pending
                if activation:
                    routing.restore(s.TOOLS_ROOT, activation)
                    journal["activation_rolled_back"] = True
                elif journal.get("activation_pending"):
                    raise MigrationConflict("activation_interrupted_needs_manual_recovery")
                journal["status"] = "rolled_back"
            except BaseException as rollback:
                journal["compensation_error"] = str(rollback)
                journal["status"] = "needs_attention"
            _write(directory / "journal.json", journal)
        raise


def approve(s, plan_id, request_id=None):
    old = _load_plan(s, plan_id)
    if old.get("action") in ("resume", "rollback"):
        return approve_recovery(s, old)
    if old.get("action") != "migrate":
        raise MigrationConflict("unsupported_migration_action")
    target_provider = old["target"]["provider"]
    fresh, fingerprint = _material(
        s, old["profile"], provider=target_provider,
        planned_generation=old["target"].get("generation_id"))
    if fingerprint != old.get("fingerprint"):
        raise MigrationConflict("plan_changed")
    tid = uuid.uuid4().hex
    directory = _transaction_path(s, tid)
    directory.mkdir(parents=True)
    journal = {"schema_version": SCHEMA, "transaction_id": tid,
               "plan_id": plan_id, "request_id": request_id,
               "status": "running", "stage": "preflight",
               "started_at": time.time(), "plan": old, "phases": {}}
    _write(directory / "journal.json", journal)
    return _run(s, journal, directory)


def recovery_plan(s, transaction_id, action):
    tid = _check_id(transaction_id)
    if action not in ("resume", "rollback"):
        raise MigrationConflict("unsupported_recovery_action")
    journal = _read(s, _transaction_path(s, tid) / "journal.json")
    if not journal or journal.get("schema_version") != SCHEMA:
        raise MigrationConflict("transaction_not_found")
    if journal.get("status") in FINISHED:
        raise MigrationConflict("transaction_already_resolved")
    source = Path(journal["plan"]["source"]["keg"])
    preview = {"source_present": source.is_dir(),
               "routing_active": bool(journal.get("activation")),
               "last_stage": journal.get("stage"),
               "backup_verified": False}
    try:
        _verify_backup(journal)
        preview["backup_verified"] = True
    except Exception as error:
        preview["backup_error"] = str(error)
    pid = uuid.uuid4().hex
    created = time.time()
    data = {"schema_version": SCHEMA, "id": pid, "action": action,
            "transaction_id": tid, "created_at": created,
            "expires_at": created + 900, "preview": preview,
            "journal_sha256": _hash(_transaction_path(s, tid) / "journal.json")}
    _write(_plan_path(s, pid), data)
    return data


def approve_recovery(s, approved):
    tid = approved["transaction_id"]
    directory = _transaction_path(s, tid)
    journal_path = directory / "journal.json"
    if _hash(journal_path) != approved["journal_sha256"]:
        raise MigrationConflict("recovery_plan_changed")
    journal = _read(s, journal_path)
    _verify_backup(journal)
    if approved["action"] == "rollback":
        try:
            _compensate(s, journal, directory)
            journal.update(status="rolled_back", recovery_completed_at=time.time())
            _write(journal_path, journal)
            return journal
        except Exception as exc:
            journal.update(status="needs_attention", recovery_error=str(exc))
            _write(journal_path, journal)
            raise
    if approved["action"] == "resume":
        return _run(s, journal, directory, resume=True)
    raise MigrationConflict("unsupported_recovery_action")
