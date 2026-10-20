"""Conservative Pixi osx-64 adapter; immutable lock verification before install.

This adapter never installs Pixi, never alters an existing user workspace, and
never uses a locally configured channel or PyPI/build dependencies.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from update_routes import PIXI_TOOLS
from update_selection import normalize_upstream, compare_upstream

TRUSTED_HOST = "conda.anaconda.org"
CHANNEL = "conda-forge"
PLATFORM = "osx-64"
MAX_SOLVE_SECONDS = 180
NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


class PixiUnavailable(RuntimeError):
    pass


def _check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name) or name == "ngrok":
        raise ValueError("Unmapped, excluded, or invalid Pixi tool")
    if name not in PIXI_TOOLS:
        raise ValueError("Pixi tool is not mapped")
    return name


def runtime(service):
    """Use only an already-installed, supported runtime."""
    path = Path(service.DATA_DIR) / "runtime" / "bin" / "pixi"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise PixiUnavailable("pixi_runtime_missing")
    result = service.run_cmd([str(path), "--version"], timeout=30, cancellable=True)
    if result.code or not re.search(r"\bpixi\s+\d+\.\d+", result.stdout + result.stderr):
        raise PixiUnavailable("pixi_runtime_unverified")
    return str(path)


def _env(root):
    root = Path(root)
    return {
        "HOME": str(root), "PIXI_HOME": str(root / "pixi-home"),
        "PIXI_CACHE_DIR": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "xdg"), "XDG_CACHE_HOME": str(root / "cache"),
        "PIXI_NO_INSTALL": "true", "PIXI_PLATFORM": PLATFORM,
    }


def parse_search(payload, mapped_name):
    """Fail closed on unknown Pixi CLI output instead of guessing package versions."""
    if isinstance(payload, dict) and isinstance(payload.get("packages"), list):
        records = payload["packages"]
    elif isinstance(payload, list):
        records = payload
    else:
        raise PixiUnavailable("pixi_search_schema_unsupported")
    versions = set()
    for record in records:
        if not isinstance(record, dict) or record.get("name") != mapped_name:
            continue
        if record.get("subdir", PLATFORM) not in (PLATFORM, "noarch"):
            continue
        version = record.get("version")
        if normalize_upstream(version) is not None:
            versions.add(version)
    return sorted(versions, key=lambda x: tuple(list(normalize_upstream(x)) + [0] * 20), reverse=True)


def manifest_text(name, version):
    _check_name(name)
    if normalize_upstream(version) is None:
        raise ValueError("Pixi version must be a stable numeric version")
    mapped = PIXI_TOOLS[name][0]
    return ('[workspace]\nname = "mac-manager-' + name.replace(".", "-") +
            '"\nchannels = ["conda-forge"]\nplatforms = ["osx-64"]\n\n' +
            '[dependencies]\n' + json.dumps(mapped) + " = " + json.dumps("==" + version) + "\n")


def _artifact(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != TRUSTED_HOST or parsed.username or parsed.password:
        raise PixiUnavailable("untrusted_binary_channel")
    if not parsed.path.startswith("/conda-forge/"):
        raise PixiUnavailable("untrusted_binary_channel")
    pieces = parsed.path.split("/")
    if len(pieces) < 4 or pieces[2] not in (PLATFORM, "noarch") or not pieces[-1].endswith((".conda", ".tar.bz2")):
        raise PixiUnavailable("wrong_platform_or_format")
    if not parsed.fragment or not re.fullmatch(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})", parsed.fragment):
        raise PixiUnavailable("binary_checksum_missing")
    return {"url": url.split("#")[0], "arch": pieces[2], "archive_id": pieces[-1],
            "checksum": parsed.fragment.lower(),
            "checksum_algorithm": "sha256" if len(parsed.fragment) == 64 else "md5"}


def parse_explicit(directory):
    paths = list(Path(directory).glob("*"))
    if not paths:
        raise PixiUnavailable("explicit_export_missing")
    nodes = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            text = line.strip()
            if text.startswith(("https://", "http://")):
                nodes.append(_artifact(text))
            elif text and not text.startswith(("#", "@EXPLICIT")):
                raise PixiUnavailable("unknown_explicit_spec_line")
    if not nodes or not any(node["arch"] == PLATFORM for node in nodes):
        raise PixiUnavailable("native_binary_closure_missing")
    return nodes


def _run(service, command, root, timeout):
    result = service.run_cmd(command, timeout=timeout, cancellable=True, env_override=_env(root))
    if result.code:
        raise PixiUnavailable("pixi_command_failed: " + (result.stderr or result.stdout)[-300:])
    return result


def discover(service, name, *, max_seconds=MAX_SOLVE_SECONDS):
    """Search all versions and find the newest solvable, verified Intel closure.

    A higher unsolved candidate is never silently replaced with a lower version
    when the time budget expires. No real Pixi environments are modified.
    """
    _check_name(name)
    if name in ("node", "deno", "uv"):
        raise PixiUnavailable("existing_independent_provider_preserved")
    pixi = runtime(service)
    with tempfile.TemporaryDirectory(prefix="mac-manager-pixi-") as temp:
        root = Path(temp)
        mapped = PIXI_TOOLS[name][0]
        found = _run(service, [pixi, "search", mapped, "--channel", CHANNEL,
                               "--platform", PLATFORM, "--json", "--limit", "10000",
                               "--no-config"], root, min(max_seconds, 60))
        try:
            versions = parse_search(json.loads(found.stdout), mapped)
        except (ValueError, TypeError) as error:
            raise PixiUnavailable("pixi_search_json_invalid") from error
        if not versions:
            raise PixiUnavailable("no_stable_osx_64_versions")
        deadline = time.monotonic() + max_seconds
        failures = []
        for version in versions:
            service.raise_if_cancel_requested()
            remaining = deadline - time.monotonic()
            if remaining < 10:
                raise PixiUnavailable("pixi_comparison_incomplete")
            manifest = root / "pixi.toml"
            manifest.write_text(manifest_text(name, version))
            lock = root / "pixi.lock"
            lock.unlink(missing_ok=True)
            try:
                _run(service, [pixi, "lock", "--no-install", "--no-config",
                               "--manifest-path", str(manifest)], root, min(remaining, 90))
                if not lock.is_file():
                    raise PixiUnavailable("pixi_lock_missing")
                export = root / "explicit"
                export.mkdir(exist_ok=True)
                _run(service, [pixi, "workspace", "export", "conda-explicit-spec",
                               str(export), "--manifest-path", str(manifest),
                               "--no-config"], root, min(deadline - time.monotonic(), 60))
                nodes = parse_explicit(export)
                return {"provider": "pixi", "upstream_version": version,
                        "package_version": version, "revision": None,
                        "provider_names": [mapped], "system": {
                            "os": "macos", "arch": "x86_64"},
                        "closure": nodes, "closure_verified": True,
                        "manifest": manifest.read_text(), "lock": lock.read_text(),
                        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
                        "availability": "verified", "execution_supported": True,
                        "unsolved_versions": failures}
            except PixiUnavailable as error:
                failures.append({"version": version, "reason": str(error)})
        raise PixiUnavailable("no_verified_binary_lock")


def install_locked(service, candidate, generation):
    """Install into a new empty managed generation only; never modify user data."""
    if candidate.get("provider") != "pixi" or not candidate.get("closure_verified"):
        raise ValueError("Cannot install unverified Pixi candidate")
    if candidate.get("system", {}).get("arch") != "x86_64":
        raise ValueError("Pixi candidate is not Intel")
    manifest = candidate.get("manifest")
    lock = candidate.get("lock")
    if not isinstance(manifest, str) or not isinstance(lock, str):
        raise ValueError("Exact Pixi manifest and lock are required")
    generation = Path(generation)
    if generation.exists():
        raise FileExistsError("Pixi generation must be new")
    if not candidate.get("lock_sha256") == hashlib.sha256(lock.encode()).hexdigest():
        raise ValueError("Pixi lock fingerprint changed")
    pixi = runtime(service)
    generation.mkdir(parents=True)
    (generation / "pixi.toml").write_text(manifest)
    (generation / "pixi.lock").write_text(lock)
    result = service.run_cmd([pixi, "install", "--locked", "--no-config",
                              "--manifest-path", str(generation / "pixi.toml")],
                             timeout=1800, cancellable=True, env_override=_env(generation))
    if result.code:
        raise PixiUnavailable("pixi_locked_install_failed: " + result.stderr[-200:])
    prefix = generation / ".pixi" / "envs" / "default"
    if not (prefix / "conda-meta").is_dir():
        raise PixiUnavailable("pixi_installed_inventory_missing")
    return prefix


def installed_record(prefix, mapped_name, required_version):
    """Read live conda-meta; a declared manifest alone is not installation evidence."""
    records = []
    for path in (Path(prefix) / "conda-meta").glob("*.json"):
        try:
            item = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if item.get("name") == mapped_name:
            records.append(item)
    if len(records) != 1 or compare_upstream(records[0].get("version"), required_version) != 0:
        raise PixiUnavailable("pixi_installed_version_mismatch")
    return records[0]


def verify_generation(prefix, closure):
    """Match every approved archive and published checksum to live conda-meta.

    An existing user-managed environment is never treated as evidence for a
    generated application-owned environment.
    """
    folder = Path(prefix) / "conda-meta"
    if not folder.is_dir() or not isinstance(closure, list) or not closure:
        raise PixiUnavailable("pixi_closure_inventory_missing")
    installed = {}
    for path in folder.glob("*.json"):
        try:
            metadata = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise PixiUnavailable("pixi_invalid_conda_metadata") from error
        name = metadata.get("fn")
        if not isinstance(name, str) or name in installed:
            raise PixiUnavailable("pixi_ambiguous_archive_inventory")
        installed[name] = metadata
    for node in closure:
        archive = node.get("archive_id")
        record = installed.get(archive)
        if record is None or node.get("arch") not in (PLATFORM, "noarch"):
            raise PixiUnavailable("pixi_expected_archive_missing")
        checksum = record.get(node.get("checksum_algorithm"))
        if not isinstance(checksum, str) or checksum.lower() != node.get("checksum"):
            raise PixiUnavailable("pixi_archive_checksum_mismatch")
        if record.get("subdir") != node["arch"]:
            raise PixiUnavailable("pixi_installed_architecture_mismatch")
    if len(installed) != len(closure):
        raise PixiUnavailable("pixi_unapproved_extra_archives")
    return {"verified_archives": len(closure)}
