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
    if row.get("kind") != "formula" or not row.get("outdated") or row.get("inventory_stale"):
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
                         execution_supported=True, blockers=[])
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


def collect_candidates(s, snapshot):
    """Run only during an explicit check. Cached state GET never scans providers."""
    rows = snapshot.get("packages", [])
    offers = {o["name"]: o for o in snapshot.get("migrations", [])}
    mapping = json.loads((Path(__file__).with_name("macports-map.json")).read_text())
    candidates = []
    catalog, installed = None, None
    settings = s.load_settings()
    try:
        from update_engine import Engine
        engine = Engine(s)
        catalog, installed = engine.catalog(), engine.installed()
    except Exception:
        pass
    for row in rows:
        provider = row.get("provider") or "homebrew"
        if row.get("excluded") or row.get("name") == "ngrok" or row.get("inventory_stale"):
            continue
        if provider == "homebrew":
            if row.get("kind") == "formula" and row.get("outdated"):
                candidates.append(_homebrew(s, row, catalog, installed, settings))
            offer = offers.get(row.get("name"))
            roots = mapping.get(row.get("name"), [])
            if row.get("kind") == "formula" and roots and offer and offer.get("status") == "binary_available":
                row["classification"] = offer.get("classification") or {}
                candidate = _ports(s, row, roots, "migrate")
                candidate["logical_id"] = row["name"]
                candidate["profile_id"] = row["name"] if row["name"] in (
                    "terminal-notifier", "include-what-you-use") else None
                candidate["profile_version"] = 1 if candidate["profile_id"] else None
                candidates.append(candidate)
        elif provider == "macports" and row.get("kind") == "port" and row.get("outdated"):
            candidates.append(_ports(s, row, [row["name"]], "update"))
        elif provider == "pixi" and row.get("kind") == "tool":
            # Do not mistake cached Homebrew latest_version for a Pixi catalog result.
            candidate = _base_candidate(row, "pixi", "update", version=None)
            candidate["blockers"] = ["pixi_discovery_required"]
            candidates.append(candidate)
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
    plan = read_plan(s)
    if not plan:
        return snapshot
    by_id = {d["source_installation_id"]: d for d in plan["decisions"]}
    for row in snapshot.get("packages", []):
        row["update_decision"] = by_id.get(_ids(row))
        if row["update_decision"] and row["update_decision"]["status"] == "ready":
            row["outdated"] = True
    snapshot["update_decisions"] = plan["decisions"]
    snapshot["unified_plan_id"] = plan["id"]
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
    if plan.get("expires_at", 0) < time.time():
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
            raise PlanError("migration_execution_not_yet_integrated")
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


def execute(s, body, *, all_ready=False):
    """Idempotent, fail-closed execution of approved same-provider routes only.

    Caller must hold the cross-process operation lock. A crash leaves a durable
    'needs_recovery' request rather than silently repeating a package mutation.
    """
    request_id = body.get("request_id") if isinstance(body, dict) else None
    path = _request_path(s, request_id) if isinstance(request_id, str) and re_full_id(request_id) else None
    plan, decisions = validate_request(s, body, all_ready=all_ready)
    signature = _canonical_request(plan["id"], decisions)
    journal = s.read_json(path) if path else None
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
    journal = {"schema_version": 1, "request_id": request_id, "plan_id": plan["id"],
               "request_hash": signature, "status": "running", "results": [],
               "started_at": s.now_iso()}
    provider_ownership.atomic_write_json(path, journal)
    try:
        for decision in decisions:
            s.raise_if_cancel_requested()
            source = rows[decision["source_installation_id"]]
            provider = decision["target_provider"]
            if provider != decision["current_provider"]:
                raise PlanError("review_required")
            candidate = decision["selected_candidate"]
            # Fail closed on changed binary closure; no silent source fallback.
            if provider == "homebrew":
                catalog, installed = None, None
                from update_engine import Engine
                engine = Engine(s)
                catalog, installed = engine.catalog(), engine.installed()
                renewed = _homebrew(s, source, catalog, installed, s.load_settings())
                if not renewed.get("closure_verified") or renewed.get("closure") != candidate.get("closure"):
                    raise PlanError("plan_changed")
                result = s.run_cmd([s.brew_bin(), "upgrade", "--force-bottle", source["name"]],
                                   timeout=s.load_settings().get("upgrade_timeout_seconds") or None,
                                   cancellable=True,
                                   env_override=engine.brew_env)
                outcome = {"ok": result.code == 0, "name": source["name"],
                           "kind": source["kind"], "provider": provider,
                           "code": result.code, "stdout": result.stdout[-3000:],
                           "stderr": result.stderr[-3000:]}
            elif provider == "macports":
                latest = _ports(s, source, candidate["provider_names"], "update")
                if (not latest["execution_supported"] or
                    latest["closure"] != candidate["closure"]):
                    raise PlanError("plan_changed")
                outcome = macports_provider.upgrade(s, source)
            else:
                raise PlanError("provider_route_unavailable")
            journal["results"].append(outcome)
            provider_ownership.atomic_write_json(path, journal)
            if not outcome.get("ok"):
                journal["status"] = "needs_recovery"
                provider_ownership.atomic_write_json(path, journal)
                return {"ok": False, "results": journal["results"],
                        "error": "update_failed", "request_id": request_id}
        journal["status"] = "completed"
        journal["completed_at"] = s.now_iso()
        journal["result"] = {"ok": True, "results": journal["results"],
                             "request_id": request_id}
        provider_ownership.atomic_write_json(path, journal)
        return journal["result"]
    except BaseException:
        journal["status"] = "needs_recovery"
        provider_ownership.atomic_write_json(path, journal)
        raise
