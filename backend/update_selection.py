"""Conservative, side-effect-free Intel provider decision contracts.

A candidate is never executable merely because its version looks newer. The
adapter must attest to a verified binary closure and an executable profile.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

SCHEMA_VERSION = 1
TTL_SECONDS = 15 * 60
_PROVIDER_ORDER = {"homebrew": 0, "macports": 1, "pixi": 2}
_STABLE = re.compile(r"v?([0-9]+(?:\.[0-9]+)*)\Z")


def normalize_upstream(value):
    """Return a numeric tuple; ambiguous suffixes/prereleases are UNKNOWN."""
    if not isinstance(value, str):
        return None
    match = _STABLE.fullmatch(value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def compare_upstream(left, right):
    """Compare upstream versions, or return None rather than guessing."""
    a, b = normalize_upstream(left), normalize_upstream(right)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def compare_same_provider_package(current_version, current_revision,
                                  target_version, target_revision,
                                  provider_reports_outdated=False):
    """Upstream order first; packaging revisions only within one provider."""
    result = compare_upstream(target_version, current_version)
    if result is None or result != 0:
        return result
    if current_revision is not None and target_revision is not None:
        try:
            a, b = int(target_revision), int(current_revision)
        except (TypeError, ValueError):
            return None
        return (a > b) - (a < b)
    return 1 if provider_reports_outdated is True else 0


def _installation_id(row):
    known = row.get("installation_id")
    if known:
        return known
    provider = row.get("provider", "homebrew")
    name = row.get("provider_name") or row.get("name")
    kind = row.get("kind", "formula")
    if not name or provider not in _PROVIDER_ORDER:
        return None
    if provider == "homebrew" and kind in ("formula", "cask"):
        return f"homebrew:{kind}:{name}"
    if provider == "macports" and kind == "port":
        return f"macports:{name}"
    if provider == "pixi" and kind == "tool":
        return f"pixi:{name}"
    return None


def _material(value):
    """Remove volatile observations from the fingerprint, recursively."""
    volatile = {"checked_at", "created_at", "expires_at", "plan_id",
                "decision_id", "evidence_path", "evidence_refs", "observed_at"}
    if isinstance(value, dict):
        return {k: _material(v) for k, v in sorted(value.items())
                if k not in volatile}
    if isinstance(value, list):
        return [_material(item) for item in value]
    return value


def material_fingerprint(source, candidate, ownership=None):
    data = {"source": _material(source), "candidate": _material(candidate),
            "ownership": _material(ownership or {})}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _migration_allowed(row):
    cls = row.get("classification") or {}
    if cls.get("category") != "standalone_candidate":
        return False
    if cls.get("gaps") or cls.get("consumers") or cls.get("shared_roles"):
        return False
    if cls.get("role") in ("service", "shared", "library"):
        return False
    return (row.get("provider") or "homebrew") == "homebrew" and row.get("kind") == "formula"


def _candidate_issues(row, candidate):
    issues = []
    source_id = _installation_id(row)
    provider = candidate.get("provider")
    action = candidate.get("action")
    if not source_id or candidate.get("source_installation_id") != source_id:
        issues.append("identity_mismatch")
    if candidate.get("logical_id") != (row.get("logical_id") or row.get("name")):
        issues.append("logical_identity_mismatch")
    if provider not in _PROVIDER_ORDER:
        issues.append("unknown_provider")
    if candidate.get("schema_version") != SCHEMA_VERSION:
        issues.append("unsupported_candidate_schema")
    if candidate.get("availability") != "verified":
        issues.append("provider_unverified")
    if candidate.get("execution_supported") is not True:
        issues.append("execution_unsupported")
    sysinfo = candidate.get("system") or {}
    if sysinfo.get("os") != "macos" or sysinfo.get("arch") != "x86_64":
        issues.append("unsupported_platform")
    if candidate.get("closure_verified") is not True:
        issues.append("binary_closure_unverified")
    closure = candidate.get("closure")
    if not isinstance(closure, list):
        issues.append("binary_closure_unverified")
    else:
        for node in closure:
            if not isinstance(node, dict) or node.get("arch") not in ("x86_64", "osx-64", "noarch"):
                issues.append("invalid_binary_closure")
                break
            if not (node.get("archive_id") or node.get("url")):
                issues.append("invalid_binary_closure")
                break
    if not isinstance(candidate.get("provider_names"), list) or not candidate["provider_names"]:
        issues.append("target_identity_missing")
    if action not in ("update", "migrate"):
        issues.append("unsupported_action")
    current_provider = row.get("provider") or "homebrew"
    if provider != current_provider and action != "migrate":
        issues.append("provider_switch_requires_migration")
    if provider == current_provider and action != "update":
        issues.append("same_provider_not_migration")
    if action == "migrate":
        if not _migration_allowed(row):
            issues.append("migration_classification_blocked")
        if not candidate.get("profile_id") or not isinstance(candidate.get("profile_version"), int):
            issues.append("migration_profile_missing")
    if candidate.get("blockers"):
        issues.extend(str(x) for x in candidate["blockers"])
    current_version = (row.get("installed_upstream_version") or row.get("upstream_version")
                       or row.get("installed_version"))
    cmp = (compare_same_provider_package(
        current_version, row.get("installed_revision", row.get("revision")),
        candidate.get("upstream_version"), candidate.get("revision"),
        candidate.get("provider_outdated", False))
        if provider == current_provider
        else compare_upstream(candidate.get("upstream_version"), current_version))
    if cmp is None:
        issues.append("comparison_unknown")
    elif cmp < 0:
        issues.append("downgrade_forbidden")
    elif cmp == 0:
        issues.append("not_newer")
    return list(dict.fromkeys(issues))


def select(installed, candidates, ownership=None, *, plan_id=None, created_at=None):
    """Return one JSON-compatible decision per physical installation."""
    ownership = ownership or {}
    plan_id = plan_id or uuid.uuid4().hex
    created = time.time() if created_at is None else created_at
    results = []
    for row in installed:
        installation_id = _installation_id(row)
        logical_id = row.get("logical_id") or row.get("name")
        provider = row.get("provider") or "homebrew"
        current = (row.get("installed_upstream_version") or row.get("upstream_version")
                   or row.get("installed_version"))
        relevant = [c for c in candidates
                    if c.get("source_installation_id") == installation_id]
        excluded = row.get("excluded") or logical_id == "ngrok" or row.get("name") == "ngrok"
        alternatives = []
        eligible = []
        incomplete = not row.get("discovery_complete", False)
        for candidate in relevant:
            issues = ["excluded_package"] if excluded else _candidate_issues(row, candidate)
            alternative = dict(candidate)
            alternative["reason_codes"] = issues
            alternatives.append(alternative)
            if "provider_unverified" in issues or "comparison_unknown" in issues:
                incomplete = True
            if not issues:
                eligible.append(candidate)
        def preference(c):
            version = normalize_upstream(c["upstream_version"])
            # Version tuple comparison must pad zeros for 1.2 vs 1.2.0.
            padded = version + (0,) * (20 - len(version)) if len(version) < 20 else version
            return (padded, int(c["provider"] == provider),
                    int(c.get("revision") or 0) if c["provider"] == provider else 0,
                    -_PROVIDER_ORDER[c["provider"]])
        selected = max(eligible, key=preference) if eligible else None
        if selected:
            status = "ready"
            action = selected["action"]
            reasons = ["partial_provider_comparison"] if incomplete else ["newest_verified_compatible"]
        elif excluded:
            status, action, reasons = "blocked", "none", ["excluded_package"]
        elif incomplete:
            status, action, reasons = "unverified", "none", ["discovery_incomplete"]
        elif any(a["reason_codes"] for a in alternatives):
            blocking = [a for a in alternatives if "not_newer" not in a["reason_codes"]]
            status, action, reasons = ("blocked", "none", ["no_executable_binary_route"]) if blocking else ("current", "none", ["already_current"])
        else:
            status, action, reasons = "current", "none", ["already_current"]
        source_ownership = (ownership.get(installation_id) or {}
                            if isinstance(ownership, dict) else {})
        result = {
            "decision_id": uuid.uuid4().hex, "plan_id": plan_id,
            "logical_id": logical_id, "source_installation_id": installation_id,
            "current_provider": provider,
            "target_provider": selected["provider"] if selected else provider,
            "installed_version": current,
            "target_version": selected.get("upstream_version") if selected else None,
            "action": action, "status": status, "selected_candidate": selected,
            "alternatives": alternatives, "reason_codes": reasons, "blockers": [],
            "created_at": created, "expires_at": created + TTL_SECONDS,
            "material_fingerprint": material_fingerprint(row, selected, source_ownership),
        }
        if row.get("needs_recovery"):
            result.update(status="needs_recovery", action="none",
                          selected_candidate=None, reason_codes=["recovery_required"])
        results.append(result)
    return results
