"""Fixed, profile-bound probes for standalone Intel migration targets."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import platform
import re
import sys
import tempfile

@dataclass(frozen=True)
class Profile:
    name: str
    ports: tuple[str, ...]
    commands: tuple[str, ...]
    version_args: tuple[str, ...]
    version: int = 1
    provider: str = "macports"

PROFILES = {
    "terminal-notifier": Profile(
        "terminal-notifier", ("terminal-notifier",), ("terminal-notifier",), ("-version",)),
    "include-what-you-use": Profile(
        "include-what-you-use", ("include-what-you-use",),
        ("include-what-you-use", "iwyu_tool.py", "fix_includes.py"), ("--version",)),
}

def get(name):
    return PROFILES.get(name)

def intel_only():
    if (platform.system() != "Darwin" or platform.machine() != "x86_64" or
            os.environ.get("SYSCTL_TRANSLATED") == "1"):
        raise ValueError("Migration is supported only on native Intel macOS")

def command_paths(profile):
    if not isinstance(profile, Profile):
        raise ValueError("Unsupported profile")
    return {name: Path("/opt/local/bin") / name for name in profile.commands}

def version_matches(text, target):
    return re.search(r"(?<![0-9])" + re.escape(str(target).split("_")[0]) +
                     r"(?![0-9])", text or "") is not None

def _check(result, title, required=0):
    if result.code != required:
        raise RuntimeError(title + ": unexpected exit " + str(result.code) +
                           ": " + (result.stderr or result.stdout)[-500:])
    return {"code": result.code, "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-3000:]}

def _python():
    executable = Path(sys.executable)
    if sys.version_info[:2] != (3, 14) or not executable.is_file():
        raise RuntimeError("Verified Python 3.14 is required for IWYU")
    return str(executable)

def smoke(s, profile, target):
    """Run full version + practical checks on target binaries, without source edits."""
    paths = command_paths(profile)
    for name, path in paths.items():
        if not path.is_file():
            raise RuntimeError("Required target command missing: " + name)
    first = s.run_cmd([str(paths[profile.commands[0]]), *profile.version_args],
                      timeout=30, cancellable=True)
    result = {"version": _check(first, "version")}
    if not version_matches(first.stdout + first.stderr, target):
        raise RuntimeError("Target package version mismatch")
    if profile.name == "terminal-notifier":
        # Preserve the existing notification helper, not a replacement identity.
        return result
    if profile.name != "include-what-you-use":
        raise RuntimeError("Unsupported smoke profile")
    python = _python()
    sdk = s.run_cmd(["/usr/bin/xcrun", "--show-sdk-path"], timeout=30, cancellable=True)
    _check(sdk, "Xcode SDK")
    sdk_dir = Path(sdk.stdout.strip())
    if not sdk_dir.is_dir() or not (sdk_dir / "usr" / "include").is_dir():
        raise RuntimeError("SDK path is unavailable")
    with tempfile.TemporaryDirectory(prefix="iwyu acceptance ") as temp:
        root = Path(temp)
        (root / "needed.h").write_text("struct Needed { int value; };\n")
        (root / "unused.h").write_text("struct Unused { int value; };\n")
        source = root / "probe.cc"
        source.write_text('#include "needed.h"\n#include "unused.h"\n#include <vector>\n'
                          'int main(){ Needed x{2}; std::vector<int> v{x.value}; return v[0]; }\n')
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        arguments = ["/usr/bin/clang++", "-std=c++17", "-isysroot",
                     str(sdk_dir), "-I", str(root), str(source)]
        (root / "compile_commands.json").write_text(
            json.dumps([{"directory": str(root), "arguments": arguments,
                         "file": str(source)}]))
        direct = s.run_cmd(
            [str(paths["include-what-you-use"]), "-std=c++17", "-isysroot",
             str(sdk_dir), "-I", str(root), "-Xiwyu", "--error=17",
             str(source)], timeout=90, cancellable=True)
        direct_report = _check(direct, "IWYU analysis", required=17)
        output = direct.stdout + "\n" + direct.stderr
        if not re.search(r"(?m)^\s*-\s*#include\s+[\"<]unused\.h[\">]", output):
            raise RuntimeError("IWYU did not recommend removal of unused.h")
        tool = s.run_cmd(
            [python, str(paths["iwyu_tool.py"]), "-p", str(root), str(source),
             "--", "-Xiwyu", "--error=17"], timeout=90, cancellable=True,
            env_override={"IWYU_BINARY": str(paths["include-what-you-use"])})
        tool_report = _check(tool, "iwyu_tool.py", required=17)
        if not re.search(r"(?m)^\s*-\s*#include\s+[\"<]unused\.h[\">]",
                         tool.stdout + "\n" + tool.stderr):
            raise RuntimeError("iwyu_tool did not report the expected include")
        fix = s.run_cmd(
            [python, str(paths["fix_includes.py"]), "--dry_run", str(source)],
            timeout=90, cancellable=True, input_data=output)
        fix_report = _check(fix, "fix_includes.py", required=1)
        if "unused.h" not in fix.stdout + fix.stderr or "-#include" not in fix.stdout + fix.stderr:
            raise RuntimeError("Fix preview does not remove unused include")
        if hashlib.sha256(source.read_bytes()).hexdigest() != original:
            raise RuntimeError("Dry-run unexpectedly edited a source file")
        result.update(iwyu=direct_report, tool=tool_report, fix=fix_report,
                      source_sha256=original, sdk=str(sdk_dir))
        return result
