"""Protected all-or-nothing command routing for native Intel migrations.

This module is deliberately provider-agnostic. A shim can be replaced only
when a recorded owner hash matches its current bytes. New files are staged
while the active generation still points at the verified source.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import uuid

from provider_ownership import atomic_write_json

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")


class RoutingConflict(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic_bytes(path, data, mode=0o755):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".route-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def _atomic_link(destination, target):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / ("." + destination.name + "-" + uuid.uuid4().hex)
    try:
        temp.symlink_to(target)
        os.replace(temp, destination)
    finally:
        if os.path.lexists(temp):
            temp.unlink()


def _owned_file(path, manifest):
    path = Path(path)
    if not os.path.lexists(path):
        return True
    if path.is_symlink() or not path.is_file():
        return False
    return manifest.get(path.name) == digest(path)


def render_wrapper(executable, *, env=None):
    """Use fixed argv, never arbitrary shell strings from manifests/network."""
    executable = str(executable)
    if not Path(executable).is_absolute():
        raise RoutingConflict("Target executable must be absolute")
    assigns = "".join("export " + k + "=" + shlex.quote(str(v)) + "\n"
                      for k, v in sorted((env or {}).items())
                      if NAME.fullmatch(k))
    return ("#!/bin/sh\n" + assigns + "exec " + shlex.quote(executable) + ' "$@"\n').encode()


def _safe_commands(commands):
    if not isinstance(commands, dict) or not commands:
        raise RoutingConflict("Missing mandatory commands")
    for name, binary in commands.items():
        if not NAME.fullmatch(name):
            raise RoutingConflict("Unsafe command name")
        path = Path(binary)
        if not path.is_absolute() or not path.is_file():
            raise RoutingConflict("Mandatory command missing: " + name)
    return commands


def preflight(root, logical_id, commands, *, owned_shims=None):
    """No disk writes. Broken symlinks and other owners block immediately."""
    _safe_commands(commands)
    if not NAME.fullmatch(logical_id):
        raise RoutingConflict("Invalid logical tool identity")
    root = Path(root)
    bin_dir = root / "bin"
    owned_shims = owned_shims or {}
    for name in commands:
        path = bin_dir / name
        if not _owned_file(path, owned_shims):
            raise RoutingConflict("Unmanaged shim or independently edited file: " + str(path))
    active = root / "migrations" / "active" / logical_id
    if os.path.lexists(active) and not active.is_symlink():
        raise RoutingConflict("Active route pointer is not a symlink")
    return {"logical_id": logical_id, "commands": dict(commands),
            "owned_shims": dict(owned_shims), "active": str(active)}


def generate(root, transaction_id, phase, commands):
    """A generation is immutable after creation."""
    _safe_commands(commands)
    if not NAME.fullmatch(transaction_id) or phase not in ("source", "target"):
        raise RoutingConflict("Invalid generation identifier")
    directory = Path(root) / "migrations" / "generations" / transaction_id / phase
    if directory.exists():
        raise RoutingConflict("Generation already exists")
    directory.mkdir(parents=True)
    for name, binary in commands.items():
        _atomic_bytes(directory / name, render_wrapper(binary))
    return directory


def activate(root, logical_id, transaction_id, source_commands, target_commands,
             *, owned_shims=None, progress=None):
    """Stage source/target wrappers and publish with one active-pointer switch.

    Existing shims are validated *before* any write. Unrelated shims are never
    removed during compensation or recovery.
    """
    previous = preflight(root, logical_id, target_commands, owned_shims=owned_shims)
    _safe_commands(source_commands)
    if set(source_commands) != set(target_commands):
        raise RoutingConflict("Source and target must expose all profile commands")
    root = Path(root)
    active = Path(previous["active"])
    before_pointer = os.readlink(active) if active.is_symlink() else None
    source = generate(root, transaction_id, "source", source_commands)
    target = generate(root, transaction_id, "target", target_commands)
    if progress:
        progress({"state": "generations_ready", "active": str(active),
                  "source": str(source), "target": str(target),
                  "previous_pointer": before_pointer, "shims": {}})
    staged = {}
    written = {}
    try:
        for name in target_commands:
            shim = root / "bin" / name
            content = render_wrapper(active / name)
            staged[name] = (shim, content)
        # Source generation must be visible before publishing new shims.
        if before_pointer is None:
            _atomic_link(active, source)
        else:
            if not active.is_symlink() or os.readlink(active) != before_pointer:
                raise RoutingConflict("Active pointer changed during staging")
        for name, (shim, content) in staged.items():
            if not _owned_file(shim, previous["owned_shims"]):
                raise RoutingConflict("Shim ownership changed during staging")
            prior = shim.read_bytes() if shim.exists() else None
            written[name] = {"prior": prior.hex() if prior is not None else None,
                             "prior_mode": shim.stat().st_mode & 0o7777 if prior is not None else None,
                             "after_mode": 0o755,
                             "after_sha256": hashlib.sha256(content).hexdigest()}
            if progress:
                progress({"state": "shim_write_pending", "active": str(active),
                          "source": str(source), "target": str(target),
                          "previous_pointer": before_pointer, "shims": dict(written)})
            _atomic_bytes(shim, content)
            if progress:
                progress({"state": "shim_written", "active": str(active),
                          "source": str(source), "target": str(target),
                          "previous_pointer": before_pointer, "shims": dict(written)})
        if progress:
            progress({"state": "pointer_switch_pending", "active": str(active),
                      "source": str(source), "target": str(target),
                      "previous_pointer": before_pointer, "shims": dict(written)})
        _atomic_link(active, target)
        return {"active": str(active), "source": str(source), "target": str(target),
                "previous_pointer": before_pointer, "shims": written,
                "commands": list(target_commands)}
    except BaseException:
        restore(root, {"state": "activation_pending", "active": str(active),
                       "source": str(source), "target": str(target),
                       "previous_pointer": before_pointer, "shims": written})
        raise


def _revert(root, active, previous_pointer, written, *, check_only=False):
    # Preflight every path before any write. A retry may observe a mixture of
    # restored and un-restored files after interruption of the first attempt.
    pending = []
    for name, item in written.items():
        path = Path(root) / "bin" / name
        prior = bytes.fromhex(item["prior"]) if item["prior"] is not None else None
        absent = not os.path.lexists(path)
        regular = not path.is_symlink() and path.is_file()
        prior_mode = item.get("prior_mode") or 0o755
        if (absent and prior is None) or (regular and prior is not None
                and path.read_bytes() == prior and path.stat().st_mode & 0o7777 == prior_mode):
            continue
        if (not regular or digest(path) != item["after_sha256"]
                or path.stat().st_mode & 0o7777 != item.get("after_mode", 0o755)):
            raise RoutingConflict("Cannot restore changed shim: " + name)
        pending.append((path, prior, prior_mode))
    if check_only:
        return
    if previous_pointer is None:
        if active.is_symlink():
            active.unlink()
    elif not active.is_symlink() or os.readlink(active) != previous_pointer:
        _atomic_link(active, previous_pointer)
    for path, prior, prior_mode in pending:
        if prior is None:
            path.unlink()
        else:
            _atomic_bytes(path, prior, mode=prior_mode)


def restore(root, activation, *, check_only=False):
    active = Path(activation["active"])
    previous = activation["previous_pointer"]
    current = os.readlink(active) if active.is_symlink() else None
    if os.path.lexists(active) and not active.is_symlink():
        raise RoutingConflict("Active route was changed independently")
    # Completed activation, partial publication, and an already restored pointer
    # are all known states. Any other route belongs to another actor.
    allowed = {activation["target"], previous}
    if activation.get("state"):
        allowed.add(activation["source"])
    if current not in allowed:
        raise RoutingConflict("Active route was changed independently")
    _revert(root, active, previous, activation["shims"], check_only=check_only)
    return {"restored": not check_only, "source": activation["source"]}


def owned_shim_hashes(root, activation):
    return {name: digest(Path(root) / "bin" / name)
            for name in activation["commands"]}


def verify_shell(s, root, commands):
    """Check real login-shell resolution; never mask it by prepending PATH."""
    for name in commands:
        if not NAME.fullmatch(name):
            raise RoutingConflict("Unsafe command name")
        result = s.run_cmd(["/bin/zsh", "-lc", "command -v " + shlex.quote(name)],
                           timeout=30, cancellable=True)
        expected = Path(root) / "bin" / name
        if result.code != 0 or result.stdout.strip() != str(expected):
            raise RoutingConflict("Login shell PATH does not resolve managed shim: " + name)
    return True
