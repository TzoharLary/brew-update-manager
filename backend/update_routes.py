"""Binary tool routes. Never replace libraries inside Homebrew's prefix."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import time


# Deliberately limited to command line tools; databases, compilers and shared
# libraries need a project/data migration rather than an executable replacement.
PIXI_TOOLS = {
    "ansible": ("ansible", ["ansible", "ansible-playbook", "ansible-galaxy", "ansible-vault"], ["ansible", "--version"]),
    "cmake": ("cmake", ["cmake", "ctest", "cpack"], ["cmake", "--version"]),
    "deno": ("deno", ["deno"], ["deno", "--version"]),
    "doxygen": ("doxygen", ["doxygen"], ["doxygen", "--version"]),
    "ffmpeg": ("ffmpeg", ["ffmpeg", "ffprobe"], ["ffmpeg", "-version"]),
    "gh": ("gh", ["gh"], ["gh", "--version"]),
    "git": ("git", ["git", "git-upload-pack", "git-receive-pack"], ["git", "--version"]),
    "jq": ("jq", ["jq"], ["jq", "--version"]),
    "node": ("nodejs", ["node", "npm", "npx"], ["node", "--version"]),
    "pandoc": ("pandoc", ["pandoc"], ["pandoc", "--version"]),
    "poppler": ("poppler", ["pdftotext", "pdfinfo", "pdftoppm", "pdftocairo", "pdfseparate", "pdfunite", "pdfimages", "pdffonts"], ["pdfinfo", "-v"]),
    "tesseract": ("tesseract", ["tesseract"], ["tesseract", "--version"]),
    "uv": ("uv", ["uv", "uvx"], ["uv", "--version"]),
    "whisper.cpp": ("whisper.cpp", ["whisper-cli"], ["whisper-cli", "--help"]),
}


def version_key(value):
    return tuple(int(x) for x in re.findall(r"\d+", str(value).split("_")[0]))


def package_version(value):
    version = str(value).split("_")[0]
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", version):
        raise ValueError(f"Unsupported version constraint: {value}")
    return version


def read_records(root: Path):
    try:
        return json.loads((root / "installed.json").read_text())
    except FileNotFoundError:
        return {}


def route_for(name, kind, settings):
    if kind != "formula":
        return "homebrew"
    enabled = settings.get("binary_tools", [])
    if name not in enabled:
        return "homebrew"
    if name == "azure-cli":
        return "uv"
    if name in {"node", "deno"}:
        return "native"
    return "pixi" if name in PIXI_TOOLS else "homebrew"


def prepare_workspace(root: Path, name, version, platform):
    package_version(version)
    directory = root / "environments" / name / str(time.time_ns())
    directory.mkdir(parents=True)
    conda_name = PIXI_TOOLS[name][0]
    manifest = directory / "pixi.toml"
    manifest.write_text(
        '[workspace]\nname = "managed-tool"\nchannels = ["conda-forge"]\n'
        f'platforms = ["{platform}"]\n\n[dependencies]\n'
        f'{json.dumps(conda_name)} = {json.dumps(">=" + package_version(version))}\n'
        + ('python = "3.14.*"\n' if name == "ansible" else '')
    )
    return directory, manifest


def validate_lock(manifest: Path, python: str, run, env):
    # Pixi can export a machine-readable, explicit set without a YAML dependency.
    export_dir = manifest.parent / "explicit"
    result = run([env["PIXI_BIN"], "workspace", "export", "conda-explicit-spec", str(export_dir),
                  "--manifest-path", str(manifest), "--no-config"], timeout=120, env_override=env)
    if result.code:
        raise RuntimeError(result.stderr or result.stdout)
    urls = [line.strip() for path in export_dir.glob("*") if path.is_file()
            for line in path.read_text().splitlines() if line.startswith("https://")]
    if not urls or any(not url.startswith("https://conda.anaconda.org/conda-forge/")
                       or not any(f"/{subdir}/" in url for subdir in (env["PIXI_PLATFORM"], "noarch"))
                       for url in urls):
        raise RuntimeError("Lock does not consist exclusively of trusted platform binaries")
    return urls


def activate(root: Path, name, provider, directory: Path, prefix: Path, version, executables, probe_output):
    """Publish a complete tool update only after ownership of every shim is proved.

    The installed.json record and the exact generated wrapper bytes form the
    legacy ownership manifest; arbitrary files and broken links are blocked.
    """
    import hashlib
    from provider_ownership import atomic_write_json
    root = Path(root)
    bin_dir = root / "bin"
    active_dir = root / "active"
    link = active_dir / name
    prefix = Path(prefix)
    available = list(dict.fromkeys(executables))
    if not available or not all((prefix / "bin" / command).is_file() for command in available):
        raise RuntimeError("Provider did not supply all mandatory tool commands")
    if name not in PIXI_TOOLS and provider == "pixi":
        raise RuntimeError("Unmapped Pixi package")
    if name == "ngrok":
        raise RuntimeError("Excluded package")
    records = read_records(root)
    previous = records.get(name)
    if os.path.lexists(link):
        if not link.is_symlink() or not previous or os.readlink(link) != previous.get("prefix"):
            raise RuntimeError("Active tool routing is not owned by the manager")
    elif previous and previous.get("prefix"):
        raise RuntimeError("Prior managed active pointer is missing")
    if previous and set(previous.get("executables", [])) != set(available):
        raise RuntimeError("Existing command set differs from required profile")
    def wrapper(command):
        return ('#!/bin/sh\n' +
                f'export CONDA_PREFIX={shlex.quote(str(link))}\n' +
                f'export PATH={shlex.quote(str(link / "bin"))}:{shlex.quote(str(bin_dir))}:"$PATH"\n' +
                f'exec {shlex.quote(str(link / "bin" / command))} "$@"\n')
    import hashlib
    for command in available:
        destination = bin_dir / command
        if not os.path.lexists(destination):
            continue
        if not previous or destination.is_symlink() or not destination.is_file():
            raise RuntimeError("Unmanaged tool shim conflict: " + str(destination))
        raw = destination.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        old_hash = (previous.get("owned_shims") or {}).get(command)
        legacy = hashlib.sha256(wrapper(command).encode()).hexdigest()
        if actual != old_hash and not (old_hash is None and actual == legacy):
            raise RuntimeError("Independently changed managed shim: " + command)
    bin_dir.mkdir(parents=True, exist_ok=True)
    active_dir.mkdir(parents=True, exist_ok=True)
    staged_wrappers = []
    try:
        for command in available:
            destination = bin_dir / command
            staged = destination.parent / ("." + command + "." + str(time.time_ns()) + ".next")
            staged.write_text(wrapper(command))
            staged.chmod(0o755)
            staged_wrappers.append((staged, destination))
        staged_active = active_dir / ("." + name + "." + str(time.time_ns()) + ".next")
        staged_active.symlink_to(prefix)
        # Existing shims execute through link, so switch only after all
        # destinations and preconditions are rechecked.
        for command in available:
            destination = bin_dir / command
            if os.path.lexists(destination):
                prior_hash = (previous or {}).get("owned_shims", {}).get(command)
                actual_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
                if destination.is_symlink() or (prior_hash and actual_hash != prior_hash) or (
                        not prior_hash and actual_hash != hashlib.sha256(wrapper(command).encode()).hexdigest()):
                    raise RuntimeError("Shim changed during installation")
        for staged, destination in staged_wrappers:
            os.replace(staged, destination)
        os.replace(staged_active, link)
        record = {"provider": provider, "version": version, "prefix": str(prefix),
                  "workspace": str(directory), "executables": available,
                  "owned_shims": {c: hashlib.sha256((bin_dir / c).read_bytes()).hexdigest()
                                  for c in available},
                  "verified_at": time.time(), "probe_output": probe_output[-3000:]}
        records[name] = record
        atomic_write_json(root / "installed.json", records)
        return record
    finally:
        for staged, _ in staged_wrappers:
            if os.path.lexists(staged):
                staged.unlink()


def bottle_blockers(name, formulae, installed, excluded):
    """Conservative runtime closure from Homebrew's platform-selected API.

    Missing metadata, non-default options, and unavailable runtime bottles fail
    closed. Build-only dependencies are unnecessary when pouring a bottle.
    """
    blockers = []
    visited = set()

    def visit(current, root=False):
        if current in visited:
            return
        visited.add(current)
        if f"formula:{current}" in excluded:
            blockers.append({"name": current, "reason": "excluded_dependency"})
            return
        row = formulae.get(current)
        if not row:
            blockers.append({"name": current, "reason": "metadata_unavailable"})
            return
        old = installed.get(current, {})
        latest = str(row.get("stable_version", ""))
        revision = int(row.get("revision") or 0)
        if revision:
            latest += f"_{revision}"
        if not root and latest in old.get("versions", []):
            return
        if old.get("options"):
            blockers.append({"name": current, "reason": "custom_build_options"})
        if not row.get("bottle_checksum") or not row.get("bottle_tag"):
            blockers.append({"name": current, "reason": "source_required"})
        for dependency in row.get("stable_dependencies", []):
            if isinstance(dependency, str):
                visit(dependency)
            elif isinstance(dependency, dict):
                for dep, category in dependency.items():
                    if category not in (":build", ":test", ":optional"):
                        visit(dep)

    visit(name, root=True)
    return blockers
