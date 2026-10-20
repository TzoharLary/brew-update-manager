"""Durable provider ownership: overlay verified physical inventory only.

Legacy routes are preserved as unverifiable until their exact target is checked.
No port installation is ever represented as a synthetic tool.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

SCHEMA_VERSION = 1


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       indent=2) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def load_routes(root):
    path = Path(root) / "migrations" / "routes.json"
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": f"ownership_unreadable: {exc}"}
    if not isinstance(raw, dict):
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": "ownership_invalid"}
    if "schema_version" not in raw:
        return {"schema_version": SCHEMA_VERSION, "routes": [],
                "legacy": [{"logical_id": k, "record": v, "verified": False}
                           for k, v in raw.items()],
                "error": None}
    if raw.get("schema_version") != SCHEMA_VERSION or not isinstance(raw.get("routes"), list):
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": "ownership_schema_unsupported"}
    return {"schema_version": SCHEMA_VERSION, "routes": raw["routes"],
            "legacy": raw.get("legacy", []), "error": None}


def _installation_id(row):
    if row.get("installation_id"):
        return row["installation_id"]
    if row.get("provider") == "macports" and row.get("kind") == "port":
        return "macports:" + row["name"]
    if row.get("provider") == "pixi" and row.get("kind") == "tool":
        return "pixi:" + row["name"]
    if row.get("provider") in (None, "homebrew") and row.get("kind") in ("formula", "cask"):
        return "homebrew:" + row["kind"] + ":" + row["name"]
    return None


def merge_inventory(installed, registry):
    """Never fabricate an installation; attach routes only to observed targets."""
    physical = {key: row for row in installed if (key := _installation_id(row))}
    results = [dict(row) for row in installed]
    by_id = {key: i for i, row in enumerate(results)
             if (key := _installation_id(row))}
    issues = []
    if registry.get("error"):
        issues.append(registry["error"])
    for route in registry.get("routes", []):
        if not isinstance(route, dict):
            issues.append("ownership_record_invalid")
            continue
        target_id = route.get("target_installation_id")
        if target_id not in physical:
            issues.append("ownership_target_missing:" + str(target_id))
            continue
        if route.get("status") != "committed":
            issues.append("ownership_not_committed:" + str(target_id))
            continue
        at = by_id[target_id]
        results[at]["ownership_route"] = route
        results[at]["logical_id"] = route.get("logical_id") or results[at].get("logical_id")
        # Keep physical provider and kind; never relabel a MacPorts port as tool.
    for legacy in registry.get("legacy", []):
        issues.append("legacy_ownership_unverified:" + str(legacy.get("logical_id")))
    return results, issues


def commit_route(root, route, journal_path, installed):
    """Journal changes to route registry; refuses unobserved or ambiguous targets."""
    root = Path(root)
    target = route.get("target_installation_id")
    observed = [row for row in installed if _installation_id(row) == target]
    if len(observed) != 1 or not route.get("transaction_id"):
        raise ValueError("Route ownership requires one verified physical target")
    if route.get("status") != "committed" or not route.get("logical_id"):
        raise ValueError("Uncommitted route cannot become durable ownership")
    journal_path = Path(journal_path)
    if not journal_path.is_file():
        raise ValueError("Recovery journal must exist before ownership is written")
    registry = load_routes(root)
    if registry["error"]:
        raise ValueError(registry["error"])
    entries = [x for x in registry["routes"]
               if x.get("source_installation_id") != route.get("source_installation_id")]
    previous = next((x for x in registry["routes"]
                     if x.get("source_installation_id") == route.get("source_installation_id")), None)
    route = dict(route, generation=int((previous or {}).get("generation") or 0) + 1,
                 verified_at=route.get("verified_at") or time.time())
    entries.append(route)
    target_state = dict(registry, routes=entries)
    digest = hashlib.sha256(json.dumps(target_state, sort_keys=True,
                                     default=str).encode()).hexdigest()
    journal = json.loads(journal_path.read_text())
    journal["ownership_pending_sha256"] = digest
    atomic_write_json(journal_path, journal)
    atomic_write_json(root / "migrations" / "routes.json", target_state)
    journal["ownership_committed_sha256"] = digest
    atomic_write_json(journal_path, journal)
    return route
