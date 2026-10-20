"""Persisted, conservative unified Intel update decisions and batch execution.

The advertised plan is authoritative. Unsafe/missing migration routes are
visible as blocked alternatives; they can never be executed by this layer.
"""
from __future__ import annotations
import hashlib
import json
import platform
from pathlib import Path
import time
import uuid

import macports_provider
import provider_ownership
import update_routes
import update_selection
import migration_profiles


class PlanError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code


def _plan_dir(s):
    return Path(s.DATA_DIR) / "update-plans"


def _ids(row):
    name = row.get("provider_name") or row.get("name")
    provider = row.get("provider") or "homebrew"
    if provider == "macports" and row.get("kind") == "port":
        return "macports:" + name
    if provider == "pixi" and row.get("kind") == "tool":
        return "pixi:" + name
    if provider == "homebrew" and row.get("kind") in ("formula", "cask"):
        return "homebrew:" + row["kind"] + ":" + name
    return row.get("installation_id")


def _base_candidate(row, provider, action, *, version=None):
    version = version or row.get("latest_version")
    return dict(schema_version=1, logical_id=row.get("logical_id") or row["name"],
                source_installation_id=_ids(row), provider=provider,
                provider_names=[row.get("provider_name") or row["name"]],
                upstream_version=version, package_version=version,
                revision=None, action=action, availability="unverified",
                execution_supported=False, profile_id=None, profile_version=None,
                system={"os": "macos", "arch": "x86_64",
                        "release": platform.mac_ver()[0]},
                closure=[], closure_verified=False, evidence_refs=[],
                checked_at=time.time(), blockers=["binary_preflight_missing"])


def _formula_closure(name, catalog, installed, excluded):
    """Use the same dependency closure that guards the existing binary policy."""
    problems = update_routes.bottle_blockers(name, catalog, installed, excluded)
    if problems:
        return None, problems
    found, visited = [], set()
    def visit(current):
        if current in visited:
            return
        visited.add(current)
        entry = catalog.get(current)
        if not entry or not entry.get("bottle_checksum") or not entry.get("bottle_tag"):
            raise PlanError("binary_closure_unverified")
        found.append({"name": current, "arch": "x86_64", "version": entry.get("stable_version"),
                      "revision": entry.get("revision") or 0,
                      "archive_id": entry["bottle_tag"], "checksum": entry["bottle_checksum"],
                      "tag": entry["bottle_tag"]})
        for dep in entry.get("stable_dependencies", []):
            if isinstance(dep, str):
                visit(dep)
            elif isinstance(dep, dict):
                for dep_name, category in dep.items():
                    if category not in (":build", ":test", ":optional"):
                        visit(dep_name)
    visit(name)
    return found, []


def _homebrew(s, row, catalog, installed, settings):
    candidate = _base_candidate(row, "homebrew", "update")
    if row.get("kind") != "formula" or row.get("inventory_stale"):
        candidate["blockers"] = ["homebrew_candidate_unverified"]
        return candidate
    if not catalog or installed is None:
        candidate["blockers"] = ["homebrew_catalog_unavailable"]
        return candidate
    try:
        closure, issues = _formula_closure(row["name"], catalog, installed,
                                          settings["excluded_packages"])
        if issues:
            candidate["blockers"] = [x.get("reason", "binary_unavailable") for x in issues]
            return candidate
        entry = catalog[row["name"]]
        candidate.update(upstream_version=entry["stable_version"],
                         package_version=str(entry["stable_version"]) +
                         ("_" + str(entry.get("revision")) if entry.get("revision") else ""),
                         revision=int(entry.get("revision") or 0),
                         closure=closure, closure_verified=True, availability="verified",
                         execution_supported=True, provider_outdated=bool(row.get("outdated")),
                         blockers=[])
    except (KeyError, ValueError, PlanError) as error:
        candidate["blockers"] = [str(error)]
    return candidate


def _ports(s, row, roots, action):
    candidate = _base_candidate(row, "macports", action)
    candidate["provider_names"] = list(roots)
    if not roots:
        candidate["blockers"] = ["port_mapping_missing"]
        return candidate
    try:
        result = macports_provider.preflight(s, list(roots))
        nodes = result.get("nodes", [])
        node = next((n for n in nodes if n["name"] == roots[0]), None)
        if node is None:
            raise PlanError("port_root_unverified")
        closure = [dict(x, arch="x86_64", archive_id=x.get("archive"))
                   for x in nodes]
        candidate.update(upstream_version=node["version"], package_version=node["version"],
                         revision=node.get("revision"), closure=closure,
                         closure_verified=True, availability="verified",
                         execution_supported=(action == "update"),
                         blockers=[] if action == "update" else ["migration_execution_not_yet_integrated"],
                         graph=result)
    except Exception as exc:
        if isinstance(exc, getattr(s, "OperationCancelledError", ())):
            raise
        candidate["blockers"] = ["macports_preflight_failed: " + str(exc)[-200:]]
    return candidate



def _pixi(s, row):
    """Only app-owned Pixi tools can receive an in-place provider update."""
    candidate = _base_candidate(row, "pixi", "update", version=None)
    name = row.get("name")
    if name not in update_routes.PIXI_TOOLS:
        candidate["blockers"] = ["unmapped_pixi_tool"]
        return candidate
    managed = update_routes.read_records(s.TOOLS_ROOT).get(name, {})
    if managed.get("provider") != "pixi":
        candidate["blockers"] = ["pixi_ownership_unverified"]
        return candidate
    import pixi_provider
    prefix = Path(managed.get("prefix") or "/nonexistent")
    allowed = Path(s.TOOLS_ROOT) / "environments" / name
    if not prefix.resolve().is_relative_to(allowed.resolve()):
        candidate["blockers"] = ["user_environment_not_managed"]
        return candidate
    try:
        evidence = pixi_provider.installed_record(
            prefix, update_routes.PIXI_TOOLS[name][0], row["installed_version"])
        if evidence.get("version") != managed.get("version"):
            raise pixi_provider.PixiUnavailable("managed_pixi_version_mismatch")
        route = pixi_provider.discover(s, name)
        candidate.update(route, schema_version=1, logical_id=name,
                         source_installation_id=_ids(row), action="update",
                         profile_id=name, profile_version=1, blockers=[],
                         checked_at=time.time())
    except Exception as error:
        if isinstance(error, getattr(s, "OperationCancelledError", ())):
            raise
        candidate["blockers"] = ["pixi_discovery_unverified:" + str(error)[-200:]]
    return candidate


def _pixi_update(s, source, candidate):
    """Install an immutable managed Pixi generation and check its live metadata."""
    import pixi_provider
    name = source["name"]
    fresh = _pixi(s, source)
    for key in ("upstream_version", "lock_sha256", "closure", "manifest", "lock"):
        if not fresh.get("execution_supported") or fresh.get(key) != candidate.get(key):
            raise PlanError("plan_changed")
    directory = (Path(s.TOOLS_ROOT) / "environments" / name /
                 ("generation-" + uuid.uuid4().hex))
    prefix = pixi_provider.install_locked(s, candidate, directory)
    mapped, commands, probe = update_routes.PIXI_TOOLS[name]
    pixi_provider.installed_record(prefix, mapped, candidate["upstream_version"])
    pixi_provider.verify_generation(prefix, candidate["closure"])
    binaries = {command: prefix / "bin" / command for command in commands}
    if not all(p.is_file() for p in binaries.values()):
        raise PlanError("pixi_missing_mandatory_commands")
    environment = {"CONDA_PREFIX": str(prefix), "PATH": str(prefix / "bin") + ":/usr/bin:/bin"}
    check = s.run_cmd([str(prefix / "bin" / probe[0]), *probe[1:]],
                      timeout=90, cancellable=True, env_override=environment)
    if check.code:
        raise PlanError("pixi_smoke_failed", check.stderr or check.stdout)
    record = update_routes.activate(
        s.TOOLS_ROOT, name, "pixi", directory, prefix,
        candidate["upstream_version"], commands, check.stdout + check.stderr)
    return {"ok": True, "name": name, "kind": "tool",
            "code": 0, "provider": "pixi", "action": "update",
            "installed_version": record["version"],
            "stdout": "Exact locked Pixi generation installed and verified",
            "stderr": ""}

def _standalone_classified(row):
    cls = row.get("classification") or {}
    return (cls.get("category") == "standalone_candidate"
            and not cls.get("gaps") and not cls.get("consumers")
            and not cls.get("shared_roles") and cls.get("role") not in (
                "service", "shared", "library"))


def _load_homebrew_catalog(s):
    from update_engine import Engine
    engine = Engine(s)
    return engine.catalog(), engine.installed()


def _unavailable_bridge(row, provider, target_names, reason):
    """A mapped package is not necessarily a verified executable route."""
    action = "update" if provider == (row.get("provider") or "homebrew") else "migrate"
    candidate = _base_candidate(row, provider, action)
    candidate["provider_names"] = list(target_names)
    candidate["blockers"] = [reason]
    return candidate


def _pixi_bridge(s, row, *, timeout=60):
    """Bind a verified Intel route to a fixed-profile migration transaction."""
    name = row["name"]
    mapped = update_routes.PIXI_TOOLS[name][0]
    candidate = _unavailable_bridge(row, "pixi", [mapped],
                                    "pixi_migration_transaction_unavailable")
    profile = migration_profiles.pixi_get(name)
    if not profile or profile.provider != "pixi":
        candidate["blockers"] = ["pixi_migration_profile_missing"]
        return candidate
    if (row.get("kind") != "formula" or
            (row.get("provider") or "homebrew") != "homebrew" or
            not _standalone_classified(row)):
        candidate["blockers"] = ["fresh_classification_required"]
        return candidate
    import pixi_provider
    try:
        verified = pixi_provider.discover(s, name, max_seconds=timeout)
        candidate.update(verified, schema_version=1, logical_id=name,
                         source_installation_id=_ids(row), action="migrate",
                         execution_supported=False, profile_id=profile.name,
                         profile_version=profile.version,
                         blockers=["pixi_migration_transaction_unavailable"],
                         checked_at=time.time())
        import migration_engine
        plan = migration_engine.plan(
            s, name, provider="pixi", classification=row["classification"],
            pixi_candidate=verified)
        if (plan["target"]["provider"] != "pixi"
                or plan["target"]["candidate"]["lock_sha256"] != verified.get("lock_sha256")
                or plan["target"]["candidate"]["closure"] != verified.get("closure")
                or plan["target"]["version"] != verified.get("upstream_version")
                or plan["profile"] != profile.name
                or plan["profile_version"] != profile.version):
            raise PlanError("pixi_migration_plan_mismatch")
        candidate.update(migration_plan_id=plan["id"],
                         migration_fingerprint=plan["fingerprint"],
                         execution_supported=True, blockers=[])
    except Exception as error:
        if isinstance(error, getattr(s, "OperationCancelledError", ())):
            raise
        candidate["execution_supported"] = False
        code = getattr(error, "code", None) or type(error).__name__
        candidate["blockers"] = ["pixi_migration_preflight_failed:" + str(code)[-160:]]
    return candidate

def collect_candidates(s, snapshot, *, pixi_scan_budget_seconds=180):
    """Collect all *mapped* provider alternatives, regardless of brew.outdated.

    Classifications originate in the common T02 inventory scan. A missing
    migration profile remains a visible blocker, not a runnable alternative.
    """
    rows = snapshot.get("packages", [])
    mapping = json.loads((Path(__file__).with_name("macports-map.json")).read_text())
    candidates = []
    # Bounding aggregate solver work prevents many mapped applications from
    # turning a normal check into an unbounded series of Pixi searches.
    pixi_deadline = time.monotonic() + max(0, pixi_scan_budget_seconds)
    settings = s.load_settings()
    try:
        catalog, installed = _load_homebrew_catalog(s)
    except Exception:
        catalog, installed = None, None
    for row in rows:
        provider = row.get("provider") or "homebrew"
        name = row.get("name")
        if (not name or row.get("excluded") or name == "ngrok"
                or row.get("inventory_stale")):
            row["discovery_complete"] = False
            continue
        row["discovery_complete"] = True
        if provider == "homebrew" and row.get("kind") == "formula":
            if catalog is None or installed is None:
                candidates.append(_unavailable_bridge(
                    row, "homebrew", [name], "homebrew_catalog_unavailable"))
                row["discovery_complete"] = False
            else:
                entry = catalog.get(name)
                current = row.get("installed_upstream_version") or row.get("installed_version")
                version = entry.get("stable_version") if isinstance(entry, dict) else None
                comparison = (update_selection.compare_same_provider_package(
                    str(current), row.get("installed_revision"),
                    str(version), entry.get("revision", 0), row.get("outdated", False))
                    if version is not None else None)
                # No closure inspection is needed if a known stable upstream
                # cannot be an update; other mapped providers are still probed.
                if row.get("outdated") or comparison is None or comparison > 0:
                    candidate = _homebrew(s, row, catalog, installed, settings)
                    candidates.append(candidate)
                    if candidate.get("availability") != "verified":
                        row["discovery_complete"] = False
            roots = mapping.get(name)
            if roots:
                profile = migration_profiles.get(name)
                classification = row.get("classification") or {}
                if not profile or profile.provider != "macports":
                    candidates.append(_unavailable_bridge(
                        row, "macports", roots, "migration_profile_missing"))
                elif not _standalone_classified(row):
                    candidates.append(_unavailable_bridge(
                        row, "macports", roots, "fresh_classification_required"))
                else:
                    offer = _ports(s, row, roots, "migrate")
                    offer["profile_id"] = profile.name
                    offer["profile_version"] = profile.version
                    if offer["availability"] == "verified":
                        try:
                            import migration_engine
                            plan = migration_engine.plan(s, name, classification=classification)
                            offer.update(migration_plan_id=plan["id"],
                                         migration_fingerprint=plan["fingerprint"],
                                         execution_supported=True, blockers=[])
                        except Exception as error:
                            if isinstance(error, getattr(s, "OperationCancelledError", ())):
                                raise
                            offer["blockers"] = ["migration_preflight_failed:" + str(error)[-180:]]
                    else:
                        row["discovery_complete"] = False
                    candidates.append(offer)
            if name in update_routes.PIXI_TOOLS:
                classification = row.get("classification") or {}
                if not _standalone_classified(row):
                    candidates.append(_unavailable_bridge(
                        row, "pixi", [update_routes.PIXI_TOOLS[name][0]],
                        "fresh_classification_required"))
                elif (remaining := pixi_deadline - time.monotonic()) < 10:
                    candidates.append(_unavailable_bridge(
                        row, "pixi", [update_routes.PIXI_TOOLS[name][0]],
                        "pixi_scan_budget_exhausted"))
                    row["discovery_complete"] = False
                else:
                    candidate = _pixi_bridge(s, row, timeout=min(60, remaining))
                    candidates.append(candidate)
                    if candidate.get("availability") != "verified":
                        row["discovery_complete"] = False
        elif provider == "homebrew" and row.get("kind") == "cask":
            candidates.append(_unavailable_bridge(
                row, "homebrew", [name], "cask_binary_adapter_not_verified"))
        elif provider == "macports" and row.get("kind") == "port":
            if name in update_routes.PIXI_TOOLS:
                candidates.append(_unavailable_bridge(
                    row, "pixi", [update_routes.PIXI_TOOLS[name][0]],
                    "migration_source_provider_unsupported"))
            if row.get("outdated"):
                candidate = _ports(s, row, [name], "update")
                candidates.append(candidate)
                if candidate.get("availability") != "verified":
                    row["discovery_complete"] = False
        elif provider == "pixi" and row.get("kind") == "tool":
            if catalog is not None and name in catalog:
                candidates.append(_unavailable_bridge(
                    row, "homebrew", [name],
                    "migration_source_provider_unsupported"))
            candidate = _pixi(s, row)
            candidates.append(candidate)
            if candidate.get("availability") != "verified":
                row["discovery_complete"] = False
    return candidates

def _safe_snapshot(s, snapshot):
    rows = snapshot.get("packages", [])
    normalized = []
    for item in rows:
        row = dict(item)
        row["installation_id"] = _ids(row)
        normalized.append(row)
    registry = provider_ownership.load_routes(s.TOOLS_ROOT)
    merged, issues = provider_ownership.merge_inventory(normalized, registry)
    return merged, registry, issues


def generate_plan(s, snapshot):
    rows, registry, issues = _safe_snapshot(s, snapshot)
    candidates = collect_candidates(s, dict(snapshot, packages=rows))
    pid = uuid.uuid4().hex
    decisions = update_selection.select(rows, candidates, plan_id=pid)
    if registry.get("error"):
        for decision in decisions:
            decision.update(status="unverified", action="none", selected_candidate=None,
                            reason_codes=["ownership_unreadable"], blockers=[registry["error"]])
    by_id = {d["source_installation_id"]: d for d in decisions}
    for row in rows:
        decision = by_id.get(row["installation_id"])
        row["update_decision"] = decision
        if decision and decision["status"] == "ready":
            row["outdated"] = True
    created = time.time()
    plan = {"schema_version": 1, "id": pid, "created_at": created,
            "expires_at": created + update_selection.TTL_SECONDS,
            "decisions": decisions, "ownership_issues": issues}
    provider_ownership.atomic_write_json(_plan_dir(s) / (pid + ".json"), plan)
    provider_ownership.atomic_write_json(_plan_dir(s) / "latest.json", {"plan_id": pid})
    snapshot["packages"] = rows
    snapshot["update_decisions"] = decisions
    snapshot["unified_plan_id"] = pid
    snapshot["unified_plan_expires_at"] = plan["expires_at"]
    snapshot["unified_plan_stale"] = False
    return plan


def read_plan(s, plan_id=None):
    if plan_id is None:
        pointer = s.read_json(_plan_dir(s) / "latest.json") or {}
        plan_id = pointer.get("plan_id")
    if not isinstance(plan_id, str) or not re_full_id(plan_id):
        return None
    plan = s.read_json(_plan_dir(s) / (plan_id + ".json"))
    return plan if isinstance(plan, dict) and plan.get("id") == plan_id else None


def re_full_id(value):
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def attach_cached(s, snapshot):
    """After expiry, present the plan as stale without offering executable routes."""
    plan = read_plan(s)
    if not plan:
        if snapshot.get("unified_plan_id"):
            snapshot["unified_plan_stale"] = True
            snapshot["unified_plan_expires_at"] = 0
            snapshot["update_decisions"] = []
            for row in snapshot.get("packages", []):
                row.pop("update_decision", None)
        return snapshot
    expiry = plan.get("expires_at")
    fresh = isinstance(expiry, (int, float)) and expiry > time.time()
    stored = plan.get("decisions") or []
    decisions = stored if fresh else [
        dict(d, status="unverified", action="none",
             selected_candidate=None, reason_codes=["plan_expired"],
             blockers=["plan_expired"]) for d in stored
    ]
    by_id = {d["source_installation_id"]: d for d in decisions}
    for row in snapshot.get("packages", []):
        row["update_decision"] = by_id.get(_ids(row))
        if fresh and row["update_decision"] and row["update_decision"]["status"] == "ready":
            row["outdated"] = True
    snapshot["update_decisions"] = decisions
    snapshot["unified_plan_id"] = plan["id"]
    snapshot["unified_plan_expires_at"] = expiry
    snapshot["unified_plan_stale"] = not fresh
    return snapshot

def validate_request(s, body, *, all_ready=False):
    if not isinstance(body, dict):
        raise PlanError("invalid_request")
    request_id, plan_id = body.get("request_id"), body.get("plan_id")
    if not isinstance(request_id, str) or not re_full_id(request_id):
        raise PlanError("invalid_request_id")
    plan = read_plan(s, plan_id)
    if not plan:
        raise PlanError("plan_not_found")
    if plan.get("expires_at", 0) <= time.time():
        raise PlanError("plan_expired")
    submitted = body.get("decisions")
    if not isinstance(submitted, list):
        if body.get("decision_id") and body.get("material_fingerprint"):
            submitted = [{"decision_id": body["decision_id"],
                          "material_fingerprint": body["material_fingerprint"]}]
        else:
            raise PlanError("review_required")
    identifiers = [x.get("decision_id") for x in submitted if isinstance(x, dict)]
    if len(identifiers) != len(submitted) or len(identifiers) != len(set(identifiers)):
        raise PlanError("ambiguous_decisions")
    known = {x["decision_id"]: x for x in plan["decisions"]}
    items = []
    for raw in submitted:
        decision = known.get(raw["decision_id"])
        if decision is None or raw.get("material_fingerprint") != decision["material_fingerprint"]:
            raise PlanError("plan_changed")
        if decision["status"] != "ready":
            raise PlanError("decision_not_ready")
        if decision["action"] == "migrate":
            candidate = decision["selected_candidate"] or {}
            if not candidate.get("migration_plan_id") or not candidate.get("migration_fingerprint"):
                raise PlanError("migration_route_unverified")
        items.append(decision)
    if all_ready and set(identifiers) != {
        d["decision_id"] for d in plan["decisions"] if d["status"] == "ready"}:
        raise PlanError("all_ready_mismatch")
    return plan, items


def _request_path(s, request_id):
    return Path(s.DATA_DIR) / "update-requests" / (request_id + ".json")


def _canonical_request(plan_id, items):
    return hashlib.sha256(json.dumps(
        {"plan_id": plan_id, "decisions": sorted(
            (x["decision_id"], x["material_fingerprint"]) for x in items)},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _revalidate_migration(s, decision):
    """Reject a proposal unless the exact displayed provider transaction matches."""
    import migration_engine
    candidate = decision.get("selected_candidate") or {}
    provider = decision.get("target_provider")
    if (provider not in ("macports", "pixi")
            or candidate.get("provider") != provider):
        raise PlanError("migration_route_unverified")
    try:
        plan = migration_engine._load_plan(s, candidate["migration_plan_id"])
        source, target = plan["source"], plan["target"]
        if (plan.get("action") != "migrate"
                or plan.get("fingerprint") != candidate.get("migration_fingerprint")
                or plan.get("profile") != decision["logical_id"]
                or plan.get("profile") != candidate.get("profile_id")
                or plan.get("profile_version") != candidate.get("profile_version")
                or source.get("installation_id") != decision["source_installation_id"]
                or source.get("provider") != decision["current_provider"]
                or source.get("version") != decision["installed_version"]
                or target.get("provider") != provider
                or target.get("version") != candidate.get("upstream_version")):
            raise PlanError("plan_changed")
        if provider == "pixi":
            expected = target.get("candidate") or {}
            keys = ("provider", "availability", "execution_supported",
                    "closure_verified", "upstream_version", "package_version",
                    "revision", "provider_names", "system", "closure",
                    "manifest", "lock", "lock_sha256")
            if any(candidate.get(k) != expected.get(k) for k in keys):
                raise PlanError("plan_changed")
            if target.get("mapped_name") != candidate.get("provider_names", [None])[0]:
                raise PlanError("plan_changed")
            pinned, generation = expected, target.get("generation_id")
        else:
            if candidate.get("provider_names") != target.get("ports"):
                raise PlanError("plan_changed")
            pinned, generation = None, None
        _, fingerprint = migration_engine._material(
            s, decision["logical_id"], provider=provider,
            pixi_candidate=pinned, planned_generation=generation)
        if fingerprint != plan["fingerprint"]:
            raise PlanError("plan_changed")
        return plan
    except (KeyError, IndexError, TypeError, ValueError) as error:
        if isinstance(error, PlanError):
            raise
        raise PlanError("plan_changed") from error


def execute(s, body, *, all_ready=False):
    """Idempotent, fail-closed execution of approved same-provider routes only.

    Caller must hold the cross-process operation lock. A crash leaves a durable
    'needs_recovery' request rather than silently repeating a package mutation.
    """
    request_id = body.get("request_id") if isinstance(body, dict) else None
    path = _request_path(s, request_id) if isinstance(request_id, str) and re_full_id(request_id) else None
    journal = s.read_json(path) if path else None
    if journal and journal.get("status") == "completed":
        supplied = body.get("decisions")
        if supplied is None and body.get("decision_id"):
            supplied = [{"decision_id": body.get("decision_id"),
                         "material_fingerprint": body.get("material_fingerprint")}]
        fingerprint = hashlib.sha256(json.dumps({
            "plan_id": body.get("plan_id"), "decisions": sorted(
                (x.get("decision_id"), x.get("material_fingerprint")) for x in (supplied or []))},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fingerprint != journal.get("request_hash"):
            raise PlanError("request_conflict")
        return journal["result"]
    plan, decisions = validate_request(s, body, all_ready=all_ready)
    signature = _canonical_request(plan["id"], decisions)
    if journal:
        if journal.get("request_hash") != signature:
            raise PlanError("request_conflict")
        if journal.get("status") == "completed":
            return journal["result"]
        raise PlanError("needs_recovery")
    # Before the first mutation, refresh all changed installation metadata.
    current = s.compute_snapshot(False, enrich_details=False)
    rows = {_ids(p): p for p in current.get("packages", [])}
    for decision in decisions:
        item = rows.get(decision["source_installation_id"])
        if not item or item.get("installed_version") != decision["installed_version"]:
            raise PlanError("plan_changed")
        if (item.get("provider") or "homebrew") != decision["current_provider"]:
            raise PlanError("plan_changed")
        if decision["action"] == "migrate":
            _revalidate_migration(s, decision)
    journal = {"schema_version": 1, "request_id": request_id, "plan_id": plan["id"],
               "request_hash": signature, "status": "running", "results": [],
               "started_at": s.now_iso()}
    provider_ownership.atomic_write_json(path, journal)
    try:
        for decision in decisions:
            s.raise_if_cancel_requested()
            source = rows[decision["source_installation_id"]]
            provider = decision["target_provider"]
            candidate = decision["selected_candidate"]
            if decision["action"] == "migrate":
                import migration_engine
                _revalidate_migration(s, decision)
                transaction = migration_engine.approve(
                    s, candidate["migration_plan_id"], request_id=request_id)
                outcome = {
                    "ok": transaction.get("status") == "completed",
                    "name": source["name"], "kind": source["kind"],
                    "provider": provider, "action": "migrate",
                    "transaction_id": transaction["transaction_id"],
                    "code": 0 if transaction.get("status") == "completed" else 1,
                    "stdout": "Source removed and new provider verified",
                    "stderr": "",
                }
            elif provider != decision["current_provider"]:
                raise PlanError("review_required")
            # Fail closed on changed binary closure; no silent source fallback.
            elif provider == "homebrew":
                catalog, installed = None, None
                from update_engine import Engine
                engine = Engine(s)
                catalog, installed = engine.catalog(), engine.installed()
                renewed = _homebrew(s, source, catalog, installed, s.load_settings())
                if not renewed.get("closure_verified") or renewed.get("closure") != candidate.get("closure"):
                    raise PlanError("plan_changed")
                # Delegate to the same source-fallback detector and binary-only
                # preflight used by legacy updates; do not start a second
                # Homebrew installer without those safeguards.
                outcome = s.run_upgrade(source["name"], source["kind"])
                outcome["provider"] = "homebrew"
            elif provider == "macports":
                latest = _ports(s, source, candidate["provider_names"], "update")
                if (not latest["execution_supported"] or
                    latest["closure"] != candidate["closure"]):
                    raise PlanError("plan_changed")
                outcome = macports_provider.upgrade(s, source)
            elif provider == "pixi":
                outcome = _pixi_update(s, source, candidate)
            else:
                raise PlanError("provider_route_unavailable")
            journal["results"].append(outcome)
            provider_ownership.atomic_write_json(path, journal)
            if not outcome.get("ok"):
                journal["status"] = "needs_recovery"
                provider_ownership.atomic_write_json(path, journal)
                raise PlanError("needs_recovery")
        journal["status"] = "completed"
        journal["completed_at"] = s.now_iso()
        refreshed, refresh_error = s.safe_refresh_snapshot(current)
        refreshed.pop("update_decisions", None)
        refreshed.pop("unified_plan_id", None)
        provider_ownership.atomic_write_json(_plan_dir(s) / "latest.json", {"plan_id": None})
        journal["result"] = {
            "ok": refresh_error is None, "results": journal["results"],
            "updated_count": sum(bool(r.get("ok")) for r in journal["results"]),
            "migrated_count": sum(bool(r.get("ok")) and r.get("action") == "migrate"
                                  for r in journal["results"]),
            "failed_count": sum(not bool(r.get("ok")) for r in journal["results"]),
            "attempted_count": len(journal["results"]),
            "deferred_count": 0, "skipped_count": 0, "remaining_count": 0,
            "snapshot": refreshed, "inventory_refresh_error": refresh_error,
            "request_id": request_id,
        }
        journal["status"] = "completed"
        provider_ownership.atomic_write_json(path, journal)
        s.set_check_progress(running=False, phase="completed", completed_at=s.now_iso(),
                             message="Verified update batch completed", eta_seconds=0)
        return journal["result"]
    except BaseException:
        journal["status"] = "needs_recovery"
        provider_ownership.atomic_write_json(path, journal)
        raise
