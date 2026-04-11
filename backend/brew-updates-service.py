#!/usr/local/bin/python3
"""Homebrew Update Manager backend service.

Features:
- Collect installed + outdated inventory (formulae + casks)
- Compute both current-version and latest-version release dates
- Optional EN->HE package description translation with local cache
- Local API for desktop app actions
- Daily check mode with clickable macOS notification
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None

APP_NAME = "Homebrew Update Manager"
REQUIRED_PYTHON = (3, 14)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
RELEASE_CACHE_TTL_DAYS = 7
MAX_TRANSLATIONS_PER_RUN = 40
GITHUB_COMMITS_PER_FILE = 80
MAX_GITHUB_FALLBACK_PER_RUN = 25
VERSION_SEARCH_COMMITS_LIMIT = 60
GITHUB_PATH_CANDIDATES_LIMIT = 5
BREW_SCAN_TIMEOUT_SECONDS = 20
BREW_SCAN_MAX_RESULTS = 50

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if (PROJECT_ROOT / "package.json").exists():
    DATA_DIR = PROJECT_ROOT / "data"
else:
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Homebrew Update Manager"
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"

STATE_FILE = DATA_DIR / "state.json"
CHECK_LOG_FILE = LOG_DIR / "brew-check.log"
SERVICE_LOG_FILE = LOG_DIR / "brew-service.log"
RELEASE_CACHE_FILE = CACHE_DIR / "release-dates.json"
DESCRIPTION_HE_CACHE_FILE = CACHE_DIR / "description-he.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
UPDATE_HISTORY_FILE = DATA_DIR / "update-history.json"
UPDATE_HISTORY_LOCK_FILE = UPDATE_HISTORY_FILE.with_suffix(UPDATE_HISTORY_FILE.suffix + ".lock")
UPDATE_HISTORY_LOCK_TIMEOUT_SECONDS = 8.0
OPEN_APP_SCRIPT = PROJECT_ROOT / "tools" / "open-brew-updates-app.sh"

LAUNCH_AGENT_LABEL = "com.local.brew-outdated-check"
LAUNCH_AGENT_FILE = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
LAUNCH_AGENT_DOMAIN = f"gui/{os.getuid()}"
LAUNCH_AGENT_SERVICE = f"{LAUNCH_AGENT_DOMAIN}/{LAUNCH_AGENT_LABEL}"

OP_LOCK = threading.Lock()
CURRENT_OPERATION: dict[str, Any] = {
    "running": False,
    "name": None,
    "started_at": None,
}

CHECK_PROGRESS_LOCK = threading.Lock()


def new_check_progress_state() -> dict[str, Any]:
    return {
        "running": False,
        "phase": "idle",
        "message": "idle",
        "started_at": None,
        "updated_at": None,
        "completed_at": None,
        "done": 0,
        "total": 0,
        "percent": 0,
        "eta_seconds": None,
        "current_package": None,
        "error": None,
    }


CHECK_PROGRESS: dict[str, Any] = new_check_progress_state()
UNSET = object()

REPO_CACHE: dict[str, Path] = {}

TAP_GITHUB_REPO = {
    "homebrew/core": "Homebrew/homebrew-core",
    "homebrew/cask": "Homebrew/homebrew-cask",
}

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9@._+\-]+$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "brew_path": "",
    "scheduler": {
        "enabled": False,
        "frequency": "daily",  # daily | weekly | interval
        "hour": 9,
        "minute": 0,
        "weekday": 1,  # 0=Sun .. 6=Sat
        "interval_hours": 24,
    },
}

VALID_SCHEDULER_FREQUENCIES = {"daily", "weekly", "interval"}
MAX_UPDATE_HISTORY_ITEMS = 100


@dataclass
class CommandResult:
    code: int
    stdout: str
    stderr: str


class BusyError(RuntimeError):
    """Raised when another mutating operation is already running."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def read_check_progress() -> dict[str, Any]:
    with CHECK_PROGRESS_LOCK:
        return dict(CHECK_PROGRESS)


def set_check_progress(
    *,
    running: bool | None = None,
    phase: str | None = None,
    message: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    done: int | None = None,
    total: int | None = None,
    eta_seconds: int | None | object = UNSET,
    current_package: str | None | object = UNSET,
    error: str | None | object = UNSET,
) -> None:
    with CHECK_PROGRESS_LOCK:
        if running is not None:
            CHECK_PROGRESS["running"] = bool(running)
        if phase is not None:
            CHECK_PROGRESS["phase"] = phase
        if message is not None:
            CHECK_PROGRESS["message"] = message
        if started_at is not None:
            CHECK_PROGRESS["started_at"] = started_at
        if completed_at is not None:
            CHECK_PROGRESS["completed_at"] = completed_at
        if done is not None:
            CHECK_PROGRESS["done"] = max(0, int(done))
        if total is not None:
            CHECK_PROGRESS["total"] = max(0, int(total))
        if eta_seconds is not UNSET:
            if eta_seconds is None:
                CHECK_PROGRESS["eta_seconds"] = None
            else:
                CHECK_PROGRESS["eta_seconds"] = max(0, int(eta_seconds))
        if current_package is not UNSET:
            CHECK_PROGRESS["current_package"] = current_package
        if error is not UNSET:
            CHECK_PROGRESS["error"] = error

        progress_total = int(CHECK_PROGRESS.get("total") or 0)
        progress_done = int(CHECK_PROGRESS.get("done") or 0)
        if progress_total > 0:
            percent = int(round(min(100.0, max(0.0, (progress_done / progress_total) * 100.0))))
        else:
            percent = 0
        CHECK_PROGRESS["percent"] = percent
        CHECK_PROGRESS["updated_at"] = now_iso()


def reset_check_progress_for_run() -> None:
    started = now_iso()
    with CHECK_PROGRESS_LOCK:
        CHECK_PROGRESS.clear()
        CHECK_PROGRESS.update(new_check_progress_state())
        CHECK_PROGRESS["running"] = True
        CHECK_PROGRESS["phase"] = "starting"
        CHECK_PROGRESS["message"] = "Starting check"
        CHECK_PROGRESS["started_at"] = started
        CHECK_PROGRESS["updated_at"] = started


def estimate_eta_seconds(started_at_iso: str | None, done: int, total: int) -> int | None:
    started_at = parse_iso_datetime(started_at_iso)
    if not started_at or done <= 0 or total <= 0 or done >= total:
        return 0 if total > 0 and done >= total else None

    elapsed = (dt.datetime.now(dt.timezone.utc) - started_at).total_seconds()
    if elapsed <= 0:
        return None

    rate = done / elapsed
    if rate <= 0:
        return None

    remaining = max(0.0, (total - done) / rate)
    return int(round(remaining))


def compute_days_since(iso_value: str | None) -> int | None:
    parsed = parse_iso_datetime(iso_value)
    if not parsed:
        return None
    delta = dt.datetime.now(dt.timezone.utc) - parsed
    if delta.total_seconds() <= 0:
        return 0
    return int(delta.days)


def ensure_python_314() -> None:
    current = (sys.version_info.major, sys.version_info.minor)
    if current != REQUIRED_PYTHON:
        raise RuntimeError(
            "This service must run with Python 3.14. "
            f"Current interpreter: {sys.executable} ({sys.version.split()[0]})."
        )


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def settings_defaults() -> dict[str, Any]:
    # cheap deep-copy for nested defaults without importing copy
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def normalize_scheduler_payload(raw: Any) -> dict[str, Any]:
    base = settings_defaults()["scheduler"]
    source = raw if isinstance(raw, dict) else {}

    enabled = bool(source.get("enabled", base["enabled"]))
    frequency = str(source.get("frequency", base["frequency"]) or base["frequency"]).strip().lower()
    if frequency not in VALID_SCHEDULER_FREQUENCIES:
        raise ValueError("scheduler frequency must be one of: daily, weekly, interval")

    try:
        hour = int(source.get("hour", base["hour"]))
        minute = int(source.get("minute", base["minute"]))
        weekday = int(source.get("weekday", base["weekday"]))
        interval_hours = int(source.get("interval_hours", base["interval_hours"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("scheduler numeric fields must be integers") from exc

    if hour < 0 or hour > 23:
        raise ValueError("scheduler hour must be between 0 and 23")
    if minute < 0 or minute > 59:
        raise ValueError("scheduler minute must be between 0 and 59")
    if weekday < 0 or weekday > 6:
        raise ValueError("scheduler weekday must be between 0 (Sunday) and 6 (Saturday)")
    if interval_hours < 1 or interval_hours > 168:
        raise ValueError("scheduler interval_hours must be between 1 and 168")

    return {
        "enabled": enabled,
        "frequency": frequency,
        "hour": hour,
        "minute": minute,
        "weekday": weekday,
        "interval_hours": interval_hours,
    }


def read_launch_agent_scheduler() -> dict[str, Any] | None:
    if not LAUNCH_AGENT_FILE.exists():
        return None

    try:
        payload = plistlib.loads(LAUNCH_AGENT_FILE.read_bytes())
    except Exception:
        return None

    schedule = settings_defaults()["scheduler"]
    schedule["enabled"] = True

    start_interval = payload.get("StartInterval") if isinstance(payload, dict) else None
    if isinstance(start_interval, int) and start_interval > 0:
        schedule["frequency"] = "interval"
        schedule["interval_hours"] = max(1, min(168, int(round(start_interval / 3600))))
        return schedule

    start_calendar = payload.get("StartCalendarInterval") if isinstance(payload, dict) else None
    if isinstance(start_calendar, dict):
        try:
            schedule["hour"] = int(start_calendar.get("Hour", schedule["hour"]))
            schedule["minute"] = int(start_calendar.get("Minute", schedule["minute"]))
        except (TypeError, ValueError):
            pass

        if "Weekday" in start_calendar:
            schedule["frequency"] = "weekly"
            try:
                wd = int(start_calendar.get("Weekday", schedule["weekday"]))
                schedule["weekday"] = wd if 0 <= wd <= 6 else schedule["weekday"]
            except (TypeError, ValueError):
                pass
        else:
            schedule["frequency"] = "daily"

    return schedule


def load_settings() -> dict[str, Any]:
    ensure_dirs()
    settings = settings_defaults()
    raw = read_json(SETTINGS_FILE)

    if isinstance(raw, dict):
        settings["brew_path"] = str(raw.get("brew_path") or "").strip()
        try:
            settings["scheduler"] = normalize_scheduler_payload(raw.get("scheduler"))
        except ValueError:
            settings["scheduler"] = settings_defaults()["scheduler"]
        return settings

    # Backward compatibility: if we have no settings file but an existing launch agent,
    # we infer scheduler defaults from that agent.
    inferred = read_launch_agent_scheduler()
    if inferred:
        settings["scheduler"] = inferred

    return settings


def save_settings(settings: dict[str, Any]) -> None:
    ensure_dirs()
    normalized = settings_defaults()
    normalized["brew_path"] = str(settings.get("brew_path") or "").strip()
    normalized["scheduler"] = normalize_scheduler_payload(settings.get("scheduler"))
    atomic_write_json(SETTINGS_FILE, normalized)


def load_update_history() -> list[dict[str, Any]]:
    with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):
        return load_update_history_unlocked()


def load_update_history_unlocked() -> list[dict[str, Any]]:
    raw = read_json(UPDATE_HISTORY_FILE)
    if raw is None:
        if UPDATE_HISTORY_FILE.exists():
            quarantine_corrupted_json_file(UPDATE_HISTORY_FILE, reason="invalid_json")
        return []

    items = raw.get("items") if isinstance(raw, dict) else []
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "timestamp": str(item.get("timestamp") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "ok": bool(item.get("ok", False)),
                "verified_latest": bool(item.get("verified_latest", False)),
                "installed_version": str(item.get("installed_version") or "").strip(),
                "latest_version": str(item.get("latest_version") or "").strip(),
                "note": str(item.get("note") or "").strip(),
            }
        )
    return out


def save_update_history(items: list[dict[str, Any]], *, lock: bool = True) -> None:
    ensure_dirs()
    payload = {"items": items[:MAX_UPDATE_HISTORY_ITEMS]}
    if lock:
        with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):
            atomic_write_json(UPDATE_HISTORY_FILE, payload)
        return
    atomic_write_json(UPDATE_HISTORY_FILE, payload)


def append_update_history_entries(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):
        current = load_update_history_unlocked()
        merged = list(entries) + current
        save_update_history(merged, lock=False)


@contextmanager
def acquire_file_lock(lock_path: Path, timeout_seconds: float = UPDATE_HISTORY_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    ensure_dirs()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fp:
        if fcntl is None:
            yield
            return

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while True:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Timed out acquiring lock for {lock_path}")
                time.sleep(0.05)

        try:
            yield
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def quarantine_corrupted_json_file(path: Path, *, reason: str) -> None:
    if not path.exists():
        return

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_path = path.with_suffix(path.suffix + f".corrupt-{stamp}")

    try:
        path.replace(quarantine_path)
        append_service_log(
            f"Corrupted JSON moved to quarantine ({reason}): {path} -> {quarantine_path}"
        )
    except Exception as exc:
        append_service_log(
            f"Failed to quarantine corrupted JSON ({reason}) for {path}: {exc}"
        )


def detect_brew_candidates() -> list[str]:
    quick_hints = [
        os.environ.get("BREW_BIN") or "",
        "/opt/homebrew/bin/brew",
        "/usr/local/bin/brew",
        shutil.which("brew") or "",
    ]

    path_hints = []
    for segment in str(process_env().get("PATH") or "").split(":"):
        seg = str(segment or "").strip()
        if seg:
            path_hints.append(str(Path(seg) / "brew"))

    which_hints: list[str] = []
    which_all = run_cmd(["/usr/bin/which", "-a", "brew"], timeout=10)
    if which_all.stdout:
        which_hints.extend(line.strip() for line in which_all.stdout.splitlines() if line.strip())

    scan_roots: list[Path] = [
        Path("/opt/homebrew"),
        Path("/usr/local"),
        Path("/opt"),
    ]
    existing_roots = [root for root in scan_roots if root.exists()]

    scanned_hints: list[str] = []
    if existing_roots:
        find_cmd = [
            "/usr/bin/find",
            *[str(root) for root in existing_roots],
            "-type",
            "f",
            "-name",
            "brew",
        ]
        find_res = run_cmd(find_cmd, timeout=BREW_SCAN_TIMEOUT_SECONDS)
        scanned_hints = [
            line.strip()
            for line in (find_res.stdout or "").splitlines()
            if line.strip()
        ]
        if find_res.code != 0 and find_res.stderr:
            append_service_log(f"brew auto-detect find warning: {find_res.stderr}")

    combined = quick_hints + path_hints + which_hints + scanned_hints

    unique_candidates: list[str] = []
    seen: set[str] = set()
    for item in combined:
        raw = str(item or "").strip()
        if not raw:
            continue
        try:
            normalized = str(Path(raw).expanduser().resolve())
        except Exception:
            normalized = str(Path(raw).expanduser())
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_candidates.append(normalized)

    verified: list[str] = []
    for candidate in unique_candidates:
        path_obj = Path(candidate)
        if not path_obj.exists() or not os.access(candidate, os.X_OK):
            continue
        probe = run_cmd([candidate, "--version"], timeout=20)
        text = f"{probe.stdout}\n{probe.stderr}".lower()
        if probe.code == 0 and "homebrew" in text:
            verified.append(candidate)
        if len(verified) >= BREW_SCAN_MAX_RESULTS:
            break

    preferred_order = {
        "/opt/homebrew/bin/brew": 0,
        "/usr/local/bin/brew": 1,
    }
    verified.sort(key=lambda item: (preferred_order.get(item, 9), len(item), item))
    return verified


def normalize_brew_path(raw_path: Any) -> str:
    value = str(raw_path or "").strip()
    if not value:
        return ""

    candidate = str(Path(value).expanduser())
    if not Path(candidate).exists() or not os.access(candidate, os.X_OK):
        raise ValueError(f"Provided brew path is not executable: {candidate}")

    probe = run_cmd([candidate, "--version"], timeout=20)
    if probe.code != 0:
        raise ValueError(f"Provided brew path is not valid: {candidate}")

    return candidate


def launch_agent_payload_for_scheduler(scheduler: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(sys.executable),
            str(Path(__file__).resolve()),
            "--check-only",
            "--notify",
        ],
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "WorkingDirectory": str(Path.home()),
        "KeepAlive": False,
        "RunAtLoad": False,
    }

    frequency = str(scheduler.get("frequency") or "daily")
    if frequency == "interval":
        payload["StartInterval"] = int(scheduler.get("interval_hours", 24)) * 3600
        return payload

    start = {
        "Hour": int(scheduler.get("hour", 9)),
        "Minute": int(scheduler.get("minute", 0)),
    }
    if frequency == "weekly":
        start["Weekday"] = int(scheduler.get("weekday", 1))

    payload["StartCalendarInterval"] = start
    return payload


def is_launch_agent_loaded() -> bool:
    res = run_cmd(["launchctl", "print", LAUNCH_AGENT_SERVICE], timeout=20)
    return res.code == 0


def apply_scheduler_settings(scheduler: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_scheduler_payload(scheduler)

    # Always attempt to unload existing service first; ignore failures.
    run_cmd(["launchctl", "bootout", LAUNCH_AGENT_DOMAIN, str(LAUNCH_AGENT_FILE)], timeout=20)

    if not normalized["enabled"]:
        try:
            if LAUNCH_AGENT_FILE.exists():
                LAUNCH_AGENT_FILE.unlink()
        except Exception as exc:
            raise RuntimeError(f"Failed to disable scheduler: {exc}") from exc

        return {
            **normalized,
            "active": False,
            "launch_agent_path": str(LAUNCH_AGENT_FILE),
        }

    try:
        LAUNCH_AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = launch_agent_payload_for_scheduler(normalized)
        with LAUNCH_AGENT_FILE.open("wb") as fp:
            plistlib.dump(payload, fp)
    except Exception as exc:
        raise RuntimeError(f"Failed to write LaunchAgent file: {exc}") from exc

    bootstrap = run_cmd(["launchctl", "bootstrap", LAUNCH_AGENT_DOMAIN, str(LAUNCH_AGENT_FILE)], timeout=20)
    if bootstrap.code != 0:
        fallback = run_cmd(["launchctl", "load", "-w", str(LAUNCH_AGENT_FILE)], timeout=20)
        if fallback.code != 0:
            detail = bootstrap.stderr or bootstrap.stdout or fallback.stderr or fallback.stdout or "unknown error"
            raise RuntimeError(f"Failed to enable scheduler via launchctl: {detail}")

    return {
        **normalized,
        "active": is_launch_agent_loaded(),
        "launch_agent_path": str(LAUNCH_AGENT_FILE),
    }


def scheduler_status_payload(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = settings if isinstance(settings, dict) else load_settings()
    scheduler = normalize_scheduler_payload(raw.get("scheduler"))
    return {
        **scheduler,
        "active": is_launch_agent_loaded(),
        "launch_agent_path": str(LAUNCH_AGENT_FILE),
    }


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_log(path: Path, message: str) -> None:
    ensure_dirs()
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as fp:
        fp.write(f"[{ts}] {message}\n")


def append_check_log(message: str) -> None:
    append_log(CHECK_LOG_FILE, message)


def append_service_log(message: str) -> None:
    append_log(SERVICE_LOG_FILE, message)


def process_env() -> dict[str, str]:
    env = {
        "HOME": str(Path.home()),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    for key, value in os.environ.items():
        if key.startswith("HOMEBREW_"):
            env[key] = value
    return env


def run_cmd(args: list[str], timeout: int = 1800) -> CommandResult:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=process_env(),
    )
    return CommandResult(proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip())


def brew_bin() -> str:
    settings = load_settings()
    raw_path = str(settings.get("brew_path") or "").strip()
    if not raw_path:
        raise RuntimeError(
            "Homebrew path is not configured yet. Open app settings, detect/select a brew path, then try again."
        )

    try:
        return normalize_brew_path(raw_path)
    except ValueError as exc:
        raise RuntimeError(
            f"Configured Homebrew path is invalid: {exc}. Update it in app settings."
        ) from exc


def run_exclusive(op_name: str, fn):
    if not OP_LOCK.acquire(blocking=False):
        raise BusyError(
            f"Operation '{CURRENT_OPERATION.get('name')}' is already running since {CURRENT_OPERATION.get('started_at')}"
        )

    CURRENT_OPERATION["running"] = True
    CURRENT_OPERATION["name"] = op_name
    CURRENT_OPERATION["started_at"] = now_iso()

    try:
        return fn()
    finally:
        CURRENT_OPERATION["running"] = False
        CURRENT_OPERATION["name"] = None
        CURRENT_OPERATION["started_at"] = None
        OP_LOCK.release()


def normalize_versions(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                version = item.get("version")
                if version:
                    out.append(str(version))
        return out
    return []


def fetch_outdated_index() -> dict[tuple[str, str], dict[str, str]]:
    b = brew_bin()
    res = run_cmd([b, "outdated", "--json=v2"], timeout=300)
    if res.code != 0:
        append_check_log(f"Warning: brew outdated failed: {res.stderr or res.stdout}")
        return {}

    try:
        payload = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        append_check_log("Warning: failed to parse brew outdated JSON")
        return {}

    index: dict[tuple[str, str], dict[str, str]] = {}

    for row in payload.get("formulae", []) or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        installed_versions = normalize_versions(row.get("installed_versions"))
        index[("formula", name)] = {
            "installed_version": installed_versions[-1] if installed_versions else "unknown",
            "latest_version": str(row.get("current_version") or row.get("version") or "unknown"),
        }

    for row in payload.get("casks", []) or []:
        if not isinstance(row, dict):
            continue
        token = str(row.get("name") or row.get("token") or "").strip()
        if not token:
            continue
        installed_versions = normalize_versions(row.get("installed_versions") or row.get("installed"))
        index[("cask", token)] = {
            "installed_version": installed_versions[-1] if installed_versions else "unknown",
            "latest_version": str(row.get("current_version") or row.get("version") or "unknown"),
        }

    return index


def fetch_installed_inventory(outdated_index: dict[tuple[str, str], dict[str, str]]) -> list[dict[str, Any]]:
    b = brew_bin()
    res = run_cmd([b, "info", "--json=v2", "--installed"], timeout=1800)
    if res.code != 0:
        raise RuntimeError(f"brew info --installed failed: {res.stderr or res.stdout}")

    payload = json.loads(res.stdout or "{}")
    packages: list[dict[str, Any]] = []

    for row in payload.get("formulae", []) or []:
        if not isinstance(row, dict):
            continue

        name = str(row.get("name") or "").strip()
        if not name:
            continue

        installed_entries = row.get("installed") or []
        installed_versions = normalize_versions(installed_entries)
        installed_version = installed_versions[-1] if installed_versions else str(row.get("linked_keg") or "unknown")
        latest_version = str(((row.get("versions") or {}).get("stable") or "unknown"))

        outdated_meta = outdated_index.get(("formula", name))
        outdated = bool(outdated_meta) or bool(row.get("outdated", False))
        if outdated_meta:
            installed_version = outdated_meta.get("installed_version") or installed_version
            latest_version = outdated_meta.get("latest_version") or latest_version

        packages.append(
            {
                "name": name,
                "kind": "formula",
                "display_name": str(row.get("full_name") or name),
                "description": str(row.get("desc") or ""),
                "description_he": "",
                "homepage": str(row.get("homepage") or ""),
                "installed_version": installed_version,
                "latest_version": latest_version,
                "installed_versions": installed_versions,
                "outdated": outdated,
                "current_release_date": None,
                "current_release_date_source": None,
                "latest_release_date": None,
                "latest_release_date_source": None,
                "outdated_since": None,
                "days_since_outdated": None,
                "release_date": None,
                "release_date_source": None,
                "tap": str(row.get("tap") or "homebrew/core"),
                "ruby_source_path": str(row.get("ruby_source_path") or ""),
            }
        )

    for row in payload.get("casks", []) or []:
        if not isinstance(row, dict):
            continue

        token = str(row.get("token") or row.get("name") or "").strip()
        if not token:
            continue

        installed_versions = normalize_versions(row.get("installed"))
        installed_version = installed_versions[-1] if installed_versions else "unknown"
        latest_version = str(row.get("version") or "unknown")

        outdated_meta = outdated_index.get(("cask", token))
        outdated = bool(outdated_meta) or bool(row.get("outdated", False))
        if outdated_meta:
            installed_version = outdated_meta.get("installed_version") or installed_version
            latest_version = outdated_meta.get("latest_version") or latest_version

        desc = row.get("desc")
        if isinstance(desc, list):
            description = " • ".join(str(item) for item in desc if str(item).strip())
        else:
            description = str(desc or "")

        packages.append(
            {
                "name": token,
                "kind": "cask",
                "display_name": token,
                "description": description,
                "description_he": "",
                "homepage": str(row.get("homepage") or ""),
                "installed_version": installed_version,
                "latest_version": latest_version,
                "installed_versions": installed_versions,
                "outdated": outdated,
                "current_release_date": None,
                "current_release_date_source": None,
                "latest_release_date": None,
                "latest_release_date_source": None,
                "outdated_since": None,
                "days_since_outdated": None,
                "release_date": None,
                "release_date_source": None,
                "tap": str(row.get("tap") or "homebrew/cask"),
                "ruby_source_path": str(row.get("ruby_source_path") or ""),
            }
        )

    known = {(pkg["kind"], pkg["name"]) for pkg in packages}
    for (kind, name), meta in outdated_index.items():
        if (kind, name) in known:
            continue
        packages.append(
            {
                "name": name,
                "kind": kind,
                "display_name": name,
                "description": "",
                "description_he": "",
                "homepage": "",
                "installed_version": meta.get("installed_version") or "unknown",
                "latest_version": meta.get("latest_version") or "unknown",
                "installed_versions": [],
                "outdated": True,
                "current_release_date": None,
                "current_release_date_source": None,
                "latest_release_date": None,
                "latest_release_date_source": None,
                "outdated_since": None,
                "days_since_outdated": None,
                "release_date": None,
                "release_date_source": None,
                "tap": "homebrew/core" if kind == "formula" else "homebrew/cask",
                "ruby_source_path": "",
            }
        )

    return packages


def normalize_tap(tap: str, kind: str) -> str:
    value = str(tap or "").strip()
    if value:
        return value
    return "homebrew/core" if kind == "formula" else "homebrew/cask"


def resolve_repo_path(tap: str) -> Path | None:
    key = tap.strip()
    if not key:
        return None
    if key in REPO_CACHE:
        return REPO_CACHE[key]

    b = brew_bin()
    res = run_cmd([b, "--repo", key], timeout=20)
    if res.code != 0 or not res.stdout:
        return None

    path = Path(res.stdout.strip())
    if not path.exists():
        return None

    REPO_CACHE[key] = path
    return path


def find_formula_or_cask_file(repo: Path, pkg: dict[str, Any]) -> Path | None:
    ruby_source_path = str(pkg.get("ruby_source_path") or "").strip()
    if ruby_source_path:
        src = Path(ruby_source_path)
        if src.is_absolute() and src.exists():
            return src
        candidate = repo / ruby_source_path
        if candidate.exists():
            return candidate

    name = str(pkg.get("name") or "").strip()
    if not name:
        return None

    if str(pkg.get("kind")) == "formula":
        candidates = [
            repo / "Formula" / f"{name}.rb",
            repo / "Formula" / name[:1] / f"{name}.rb",
            repo / "HomebrewFormula" / f"{name}.rb",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        for candidate in (repo / "Formula").glob(f"**/{name}.rb"):
            if candidate.exists():
                return candidate
        return None

    candidates = [
        repo / "Casks" / f"{name}.rb",
        repo / "Casks" / name[:1] / f"{name}.rb",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    for candidate in (repo / "Casks").glob(f"**/{name}.rb"):
        if candidate.exists():
            return candidate

    return None


def git_last_commit_iso(repo: Path, target_file: Path) -> str | None:
    if not target_file.exists():
        return None

    res = run_cmd(["git", "-C", str(repo), "log", "-1", "--format=%cI", "--", str(target_file)], timeout=20)
    if res.code != 0 or not res.stdout:
        return None
    return res.stdout.splitlines()[0].strip()


def repo_relative_file_path(repo: Path, target_file: Path) -> str | None:
    try:
        return target_file.resolve().relative_to(repo.resolve()).as_posix()
    except Exception:
        return None


def version_in_text(text: str, version: str) -> bool:
    source = str(version or "").strip()
    if not source:
        return False
    pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(source)}(?![0-9A-Za-z])")
    return bool(pattern.search(text))


def git_file_contains_version_at_commit(repo: Path, relative_path: str, commit_hash: str, version: str) -> bool:
    if not relative_path or not commit_hash:
        return False
    res = run_cmd(
        ["git", "-C", str(repo), "show", f"{commit_hash}:{relative_path}"],
        timeout=20,
    )
    if res.code != 0:
        return False
    return version_in_text(res.stdout, version)


def git_pickaxe_commits(repo: Path, target_file: Path, needle: str) -> list[tuple[str, str]]:
    if not target_file.exists() or not needle:
        return []
    res = run_cmd(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "--format=%H|%cI",
            "--reverse",
            f"-S{needle}",
            "--",
            str(target_file),
        ],
        timeout=40,
    )
    if res.code != 0 or not res.stdout:
        return []

    commits: list[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        if "|" not in line:
            continue
        commit_hash, commit_iso = line.split("|", 1)
        commit_hash = commit_hash.strip()
        commit_iso = commit_iso.strip()
        if commit_hash and commit_iso:
            commits.append((commit_hash, commit_iso))

    if len(commits) > VERSION_SEARCH_COMMITS_LIMIT:
        return commits[:VERSION_SEARCH_COMMITS_LIMIT]
    return commits


def git_version_intro_iso(repo: Path, target_file: Path, version: str) -> str | None:
    version = str(version or "").strip()
    if not target_file.exists() or not version or version in {"unknown", "latest"}:
        return None

    relative_path = repo_relative_file_path(repo, target_file)
    if relative_path:
        for commit_hash, commit_iso in git_pickaxe_commits(repo, target_file, version):
            if git_file_contains_version_at_commit(repo, relative_path, commit_hash, version):
                return commit_iso

    pattern = re.escape(version)
    res = run_cmd(
        ["git", "-C", str(repo), "log", "-1", "--format=%cI", "-G", pattern, "--", str(target_file)],
        timeout=30,
    )
    if res.code != 0 or not res.stdout:
        return None
    return res.stdout.splitlines()[0].strip()


def github_commits_for_path(repo_slug: str, relative_path: str) -> list[dict[str, Any]]:
    encoded_path = quote(relative_path, safe="")
    url = (
        f"https://api.github.com/repos/{repo_slug}/commits"
        f"?path={encoded_path}&per_page={GITHUB_COMMITS_PER_FILE}"
    )
    req = Request(url, headers={"User-Agent": "brew-update-manager"})
    with urlopen(req, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def github_candidate_paths(
    pkg: dict[str, Any],
    repo: Path | None,
    target_file: Path | None,
) -> list[str]:
    name = str(pkg.get("name") or "").strip()
    kind = str(pkg.get("kind") or "").strip()

    candidates: list[str] = []

    ruby_source_path = str(pkg.get("ruby_source_path") or "").strip()
    if ruby_source_path:
        candidates.append(ruby_source_path)

    if repo and target_file:
        rel = repo_relative_file_path(repo, target_file)
        if rel:
            candidates.append(rel)

    if name:
        if kind == "formula":
            candidates.extend(
                [
                    f"Formula/{name}.rb",
                    f"Formula/{name[:1]}/{name}.rb",
                ]
            )
        elif kind == "cask":
            candidates.extend(
                [
                    f"Casks/{name}.rb",
                    f"Casks/{name[:1]}/{name}.rb",
                ]
            )

    unique: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        normalized = str(path or "").strip().lstrip("./")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)

    return unique[:GITHUB_PATH_CANDIDATES_LIMIT]


def github_release_dates_for_package(
    tap: str,
    relative_paths: list[str],
    installed_version: str,
    latest_version: str,
) -> tuple[str | None, str, str | None, str]:
    repo_slug = TAP_GITHUB_REPO.get(tap)
    if not repo_slug or not relative_paths:
        return None, "unavailable", None, "unavailable"

    commits: list[dict[str, Any]] = []
    chosen_path: str | None = None

    for rel_path in relative_paths:
        try:
            candidate_commits = github_commits_for_path(repo_slug, rel_path)
        except Exception as exc:
            append_service_log(f"GitHub fallback warning ({tap} {rel_path}): {exc}")
            continue

        if candidate_commits:
            commits = candidate_commits
            chosen_path = rel_path
            break

    if not commits:
        return None, "unavailable", None, "unavailable"

    head = commits[0]
    latest_date = str((((head.get("commit") or {}).get("committer") or {}).get("date") or "")).strip() or None
    latest_source = f"github_commits_head:{chosen_path}" if latest_date and chosen_path else "unavailable"

    installed = str(installed_version or "").strip()
    latest = str(latest_version or "").strip()

    if latest_date and installed and latest and installed == latest:
        return latest_date, "same_as_latest", latest_date, latest_source

    if installed and installed not in {"unknown", "latest"}:
        for commit in commits:
            message = str(((commit.get("commit") or {}).get("message") or ""))
            if installed in message:
                commit_date = str((((commit.get("commit") or {}).get("committer") or {}).get("date") or "")).strip()
                if commit_date:
                    return (
                        commit_date,
                        f"github_commit_message_match:{chosen_path}",
                        latest_date,
                        latest_source,
                    )

    if latest_date:
        return (
            latest_date,
            f"github_fallback_latest:{chosen_path}",
            latest_date,
            latest_source,
        )

    return None, "unavailable", None, "unavailable"


def load_release_cache() -> dict[str, Any]:
    return read_json(RELEASE_CACHE_FILE) or {}


def save_release_cache(cache: dict[str, Any]) -> None:
    atomic_write_json(RELEASE_CACHE_FILE, cache)


def release_cache_key(pkg: dict[str, Any]) -> str:
    return "|".join(
        [
            str(pkg.get("kind") or ""),
            str(pkg.get("name") or ""),
            str(pkg.get("installed_version") or ""),
            str(pkg.get("latest_version") or ""),
            str(pkg.get("tap") or ""),
        ]
    )


def cache_entry_fresh(entry: dict[str, Any]) -> bool:
    if not entry.get("current_release_date") and not entry.get("latest_release_date"):
        return False

    checked_at = str(entry.get("checked_at") or "").strip()
    if not checked_at:
        return False

    try:
        ts = dt.datetime.fromisoformat(checked_at)
    except ValueError:
        return False

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)

    age = dt.datetime.now(dt.timezone.utc) - ts.astimezone(dt.timezone.utc)
    return age <= dt.timedelta(days=RELEASE_CACHE_TTL_DAYS)


def resolve_release_dates(
    pkg: dict[str, Any],
    cache: dict[str, Any],
    allow_github_fallback: bool,
) -> tuple[str | None, str, str | None, str]:
    key = release_cache_key(pkg)
    cached = cache.get(key)
    if isinstance(cached, dict) and cache_entry_fresh(cached):
        return (
            cached.get("current_release_date"),
            str(cached.get("current_release_date_source") or "unavailable"),
            cached.get("latest_release_date"),
            str(cached.get("latest_release_date_source") or "unavailable"),
        )

    tap = normalize_tap(str(pkg.get("tap") or ""), str(pkg.get("kind") or ""))
    repo = resolve_repo_path(tap)

    current_release_date: str | None = None
    latest_release_date: str | None = None
    current_source = "unavailable"
    latest_source = "unavailable"
    target_file: Path | None = None

    if repo:
        target_file = find_formula_or_cask_file(repo, pkg)
        if target_file:
            latest_release_date = git_version_intro_iso(repo, target_file, str(pkg.get("latest_version") or ""))
            if latest_release_date:
                latest_source = "tap_git_latest_version_intro"

            if not latest_release_date:
                latest_release_date = git_last_commit_iso(repo, target_file)
                if latest_release_date:
                    latest_source = "tap_git_last_change"

            current_release_date = git_version_intro_iso(repo, target_file, str(pkg.get("installed_version") or ""))
            if current_release_date:
                current_source = "tap_git_version_intro"

    if (not latest_release_date or not current_release_date) and allow_github_fallback and bool(pkg.get("outdated")):
        candidate_paths = github_candidate_paths(pkg, repo, target_file)
        gh_current, gh_current_source, gh_latest, gh_latest_source = github_release_dates_for_package(
            tap,
            candidate_paths,
            str(pkg.get("installed_version") or ""),
            str(pkg.get("latest_version") or ""),
        )
        if not latest_release_date and gh_latest:
            latest_release_date = gh_latest
            latest_source = gh_latest_source
        if not current_release_date and gh_current:
            current_release_date = gh_current
            current_source = gh_current_source

    if not current_release_date and str(pkg.get("installed_version") or "") == str(pkg.get("latest_version") or ""):
        current_release_date = latest_release_date
        if current_release_date:
            current_source = "same_as_latest"

    if not current_release_date and latest_release_date:
        current_release_date = latest_release_date
        current_source = "fallback_latest"

    cache[key] = {
        "checked_at": now_iso(),
        "current_release_date": current_release_date,
        "current_release_date_source": current_source,
        "latest_release_date": latest_release_date,
        "latest_release_date_source": latest_source,
    }

    return current_release_date, current_source, latest_release_date, latest_source


def enrich_release_dates(
    packages: list[dict[str, Any]],
    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> None:
    cache = load_release_cache()
    changed_cache = False

    outdated_keys: list[tuple[str, str]] = []
    for pkg in packages:
        if pkg.get("outdated"):
            outdated_keys.append((str(pkg.get("kind") or ""), str(pkg.get("name") or "")))

    github_budget_set = set(outdated_keys[:MAX_GITHUB_FALLBACK_PER_RUN])

    total = len(packages)

    for index, pkg in enumerate(packages, start=1):
        key = (str(pkg.get("kind") or ""), str(pkg.get("name") or ""))
        allow_github = key in github_budget_set

        current_release_date, current_source, latest_release_date, latest_source = resolve_release_dates(
            pkg,
            cache,
            allow_github,
        )
        pkg["current_release_date"] = current_release_date
        pkg["current_release_date_source"] = current_source
        pkg["latest_release_date"] = latest_release_date
        pkg["latest_release_date_source"] = latest_source

        if pkg.get("outdated"):
            pkg["outdated_since"] = latest_release_date
            pkg["days_since_outdated"] = compute_days_since(latest_release_date)
        else:
            pkg["outdated_since"] = None
            pkg["days_since_outdated"] = None

        # Backward-compatible fields for older UI clients.
        pkg["release_date"] = latest_release_date
        pkg["release_date_source"] = latest_source
        changed_cache = True

        if progress_callback:
            progress_callback(index, total, pkg)

    if changed_cache:
        save_release_cache(cache)


def _translate_to_he(text: str) -> str | None:
    source = str(text or "").strip()
    if not source:
        return ""

    try:
        url = (
            "https://translate.googleapis.com/translate_a/single"
            f"?client=gtx&sl=en&tl=he&dt=t&q={quote(source)}"
        )
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))

        parts: list[str] = []
        for chunk in payload[0] if isinstance(payload, list) and payload else []:
            if isinstance(chunk, list) and chunk:
                piece = str(chunk[0] or "")
                if piece:
                    parts.append(piece)

        translated = "".join(parts).strip()
        return translated or None
    except Exception as exc:
        append_service_log(f"Translation warning: {exc}")
        return None


def enrich_hebrew_descriptions(
    packages: list[dict[str, Any]],
    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> None:
    cache = read_json(DESCRIPTION_HE_CACHE_FILE) or {}
    if not isinstance(cache, dict):
        cache = {}

    translations_done = 0
    cache_changed = False

    total = len(packages)

    for index, pkg in enumerate(packages, start=1):
        desc_en = str(pkg.get("description") or "").strip()
        if not desc_en:
            pkg["description_he"] = ""
            if progress_callback:
                progress_callback(index, total, pkg)
            continue

        cached = cache.get(desc_en)
        if isinstance(cached, str) and cached.strip():
            pkg["description_he"] = cached
            if progress_callback:
                progress_callback(index, total, pkg)
            continue

        if translations_done >= MAX_TRANSLATIONS_PER_RUN:
            pkg["description_he"] = desc_en
            if progress_callback:
                progress_callback(index, total, pkg)
            continue

        translated = _translate_to_he(desc_en)
        if translated:
            cache[desc_en] = translated
            pkg["description_he"] = translated
            cache_changed = True
        else:
            pkg["description_he"] = desc_en

        translations_done += 1

        if progress_callback:
            progress_callback(index, total, pkg)

    if cache_changed:
        atomic_write_json(DESCRIPTION_HE_CACHE_FILE, cache)


def summarize_counts(packages: list[dict[str, Any]]) -> dict[str, int]:
    formulas = [pkg for pkg in packages if pkg.get("kind") == "formula"]
    casks = [pkg for pkg in packages if pkg.get("kind") == "cask"]
    outdated = [pkg for pkg in packages if pkg.get("outdated")]
    return {
        "total": len(packages),
        "formulae": len(formulas),
        "casks": len(casks),
        "outdated_total": len(outdated),
        "outdated_formulae": sum(1 for pkg in outdated if pkg.get("kind") == "formula"),
        "outdated_casks": sum(1 for pkg in outdated if pkg.get("kind") == "cask"),
    }


def compute_snapshot(run_brew_update: bool, track_progress: bool = False) -> dict[str, Any]:
    append_check_log("Starting Homebrew check")
    errors: list[str] = []

    if track_progress:
        reset_check_progress_for_run()
        set_check_progress(phase="preparing", message="Preparing Homebrew inventory")

    progress_started_at = read_check_progress().get("started_at") if track_progress else None

    b = brew_bin()

    try:
        if run_brew_update:
            if track_progress:
                set_check_progress(
                    phase="brew_update",
                    message="Running brew update",
                    done=0,
                    total=1,
                    eta_seconds=None,
                    current_package="brew update",
                )

            append_check_log("Running brew update...")
            upd = run_cmd([b, "update"], timeout=3600)
            if upd.code != 0:
                err = upd.stderr or upd.stdout or "unknown error"
                errors.append(f"brew update failed: {err}")
                append_check_log(f"Warning: brew update failed: {err}")
            else:
                append_check_log("brew update completed")

            if track_progress:
                set_check_progress(done=1, total=1, eta_seconds=0)

        if track_progress:
            set_check_progress(
                phase="collecting_outdated",
                message="Collecting outdated index",
                done=0,
                total=1,
                eta_seconds=None,
                current_package="brew outdated --json=v2",
            )

        outdated_index = fetch_outdated_index()

        if track_progress:
            set_check_progress(done=1, total=1, eta_seconds=0)
            set_check_progress(
                phase="collecting_installed",
                message="Collecting installed packages",
                done=0,
                total=1,
                eta_seconds=None,
                current_package="brew info --json=v2 --installed",
            )

        packages = fetch_installed_inventory(outdated_index)

        if track_progress:
            set_check_progress(done=1, total=1, eta_seconds=0)

        total_packages = len(packages)

        def _release_progress(done: int, total: int, pkg: dict[str, Any]) -> None:
            if not track_progress:
                return
            eta = estimate_eta_seconds(progress_started_at, done, total)
            current = f"{pkg.get('kind')}:{pkg.get('name')}"
            set_check_progress(
                phase="resolving_dates",
                message=f"Resolving release dates ({done}/{total})",
                done=done,
                total=total,
                eta_seconds=eta,
                current_package=current,
            )

        if track_progress:
            set_check_progress(
                phase="resolving_dates",
                message="Resolving release dates",
                done=0,
                total=total_packages,
                eta_seconds=None,
                current_package="",
            )

        enrich_release_dates(packages, progress_callback=_release_progress if track_progress else None)

        def _translation_progress(done: int, total: int, pkg: dict[str, Any]) -> None:
            if not track_progress:
                return
            eta = estimate_eta_seconds(progress_started_at, done, total)
            current = f"{pkg.get('kind')}:{pkg.get('name')}"
            set_check_progress(
                phase="translating_descriptions",
                message=f"Translating descriptions ({done}/{total})",
                done=done,
                total=total,
                eta_seconds=eta,
                current_package=current,
            )

        if track_progress:
            set_check_progress(
                phase="translating_descriptions",
                message="Translating descriptions",
                done=0,
                total=total_packages,
                eta_seconds=None,
                current_package="",
            )

        enrich_hebrew_descriptions(packages, progress_callback=_translation_progress if track_progress else None)

        packages.sort(key=lambda item: (str(item.get("kind")), str(item.get("name"))))

        snapshot = {
            "app": APP_NAME,
            "updated_at": now_iso(),
            "counts": summarize_counts(packages),
            "errors": errors,
            "packages": packages,
        }

        atomic_write_json(STATE_FILE, snapshot)
        append_check_log(
            f"Check completed. total={snapshot['counts']['total']} outdated={snapshot['counts']['outdated_total']}"
        )

        if track_progress:
            set_check_progress(
                running=False,
                phase="completed",
                message="Check completed",
                done=total_packages,
                total=total_packages,
                eta_seconds=0,
                current_package="",
                error=None,
                completed_at=now_iso(),
            )

        return snapshot
    except Exception as exc:
        if track_progress:
            set_check_progress(
                running=False,
                phase="error",
                message=f"Check failed: {exc}",
                eta_seconds=0,
                error=str(exc),
                completed_at=now_iso(),
            )
        raise


def validate_package_name(name: str) -> None:
    if not name or not SAFE_NAME_RE.match(name):
        raise ValueError("Invalid package name")

def normalize_selected_packages(raw_packages: Any) -> list[dict[str, str]]:
    if not isinstance(raw_packages, list) or not raw_packages:
        raise ValueError("packages must be a non-empty array")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for item in raw_packages:
        if not isinstance(item, dict):
            raise ValueError("each packages item must be an object")

        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "").strip()

        validate_package_name(name)
        if kind not in {"formula", "cask"}:
            raise ValueError("kind must be 'formula' or 'cask'")

        key = (kind, name)
        if key in seen:
            continue

        seen.add(key)
        normalized.append({"name": name, "kind": kind})

    if not normalized:
        raise ValueError("packages must include at least one valid package")

    return normalized

def run_upgrade(name: str, kind: str) -> dict[str, Any]:
    validate_package_name(name)
    b = brew_bin()

    if kind == "formula":
        cmd = [b, "upgrade", name]
    elif kind == "cask":
        cmd = [b, "upgrade", "--cask", name]
    else:
        raise ValueError("kind must be 'formula' or 'cask'")

    append_check_log(f"Starting upgrade: {kind}:{name}")
    res = run_cmd(cmd, timeout=7200)
    ok = res.code == 0

    if ok:
        append_check_log(f"Upgrade succeeded: {kind}:{name}")
    else:
        append_check_log(f"Upgrade failed: {kind}:{name}: {res.stderr or res.stdout}")

    return {
        "ok": ok,
        "name": name,
        "kind": kind,
        "code": res.code,
        "stdout": res.stdout,
        "stderr": res.stderr,
    }


def package_from_snapshot(snapshot: dict[str, Any], name: str, kind: str) -> dict[str, Any] | None:
    for pkg in snapshot.get("packages", []) if isinstance(snapshot, dict) else []:
        if str(pkg.get("name") or "") == str(name or "") and str(pkg.get("kind") or "") == str(kind or ""):
            return pkg
    return None


def history_entries_from_results(
    results: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    snapshot_verified: bool = True,
    snapshot_error: str | None = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in results:
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not name or not kind:
            continue

        pkg = package_from_snapshot(snapshot, name, kind) or {}
        latest_version = str(pkg.get("latest_version") or "")
        installed_version = str(pkg.get("installed_version") or "")
        verified_latest = bool(
            item.get("ok") and snapshot_verified and pkg and not bool(pkg.get("outdated"))
        )

        note = ""
        if snapshot_error:
            note = f"inventory_refresh_failed: {snapshot_error}"

        entries.append(
            {
                "timestamp": now_iso(),
                "name": name,
                "kind": kind,
                "ok": bool(item.get("ok", False)),
                "verified_latest": verified_latest,
                "installed_version": installed_version,
                "latest_version": latest_version,
                "note": note,
            }
        )

    return entries


def fallback_snapshot_with_error(previous_snapshot: dict[str, Any] | None, error: str) -> dict[str, Any]:
    payload = dict(previous_snapshot) if isinstance(previous_snapshot, dict) else {}

    packages = payload.get("packages")
    if not isinstance(packages, list):
        packages = []
    payload["packages"] = packages

    counts = payload.get("counts")
    if not isinstance(counts, dict):
        counts = summarize_counts(packages)
    payload["counts"] = counts

    existing_errors = payload.get("errors")
    errors_list = list(existing_errors) if isinstance(existing_errors, list) else []
    errors_list.append(f"snapshot refresh failed: {error}")
    payload["errors"] = errors_list

    payload["app"] = str(payload.get("app") or APP_NAME)
    payload["updated_at"] = now_iso()
    return payload


def safe_refresh_snapshot(previous_snapshot: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:
    try:
        refreshed = compute_snapshot(run_brew_update=False)
        return refreshed, None
    except Exception as exc:
        detail = str(exc)
        append_check_log(f"Warning: package inventory refresh failed after update: {detail}")
        append_service_log(f"Warning: package inventory refresh failed after update: {detail}")
        return fallback_snapshot_with_error(previous_snapshot, detail), detail


def run_update_all(track_progress: bool = False) -> dict[str, Any]:
    state = read_json(STATE_FILE) or compute_snapshot(run_brew_update=False)
    outdated = [pkg for pkg in state.get("packages", []) if pkg.get("outdated")]
    total = len(outdated)

    if track_progress:
        reset_check_progress_for_run()
        set_check_progress(
            phase="brew_update",
            message="Starting package updates",
            done=0,
            total=total,
            eta_seconds=None,
            current_package="",
        )

    results: list[dict[str, Any]] = []
    started = read_check_progress().get("started_at") if track_progress else None

    for idx, pkg in enumerate(outdated, start=1):
        name = str(pkg.get("name") or "")
        kind = str(pkg.get("kind") or "")

        if track_progress:
            eta = estimate_eta_seconds(started, idx - 1, total)
            set_check_progress(
                phase="brew_update",
                message=f"Updating {name}",
                done=idx - 1,
                total=total,
                eta_seconds=eta,
                current_package=f"{kind}:{name}",
            )

        result = run_upgrade(name, kind)
        results.append(result)

        if track_progress:
            eta = estimate_eta_seconds(started, idx, total)
            set_check_progress(
                done=idx,
                total=total,
                eta_seconds=eta,
                current_package=f"{kind}:{name}",
            )

    if track_progress:
        set_check_progress(
            phase="preparing",
            message="Refreshing package inventory",
            eta_seconds=None,
            current_package="",
        )

    refreshed, refresh_error = safe_refresh_snapshot(previous_snapshot=state)

    entries = history_entries_from_results(
        results,
        refreshed,
        snapshot_verified=refresh_error is None,
        snapshot_error=refresh_error,
    )
    append_update_history_entries(entries)

    if track_progress:
        completion_message = "All package updates completed"
        if refresh_error:
            completion_message = "All package updates completed (inventory refresh warning)"
        set_check_progress(
            running=False,
            phase="completed",
            message=completion_message,
            done=total,
            total=total,
            eta_seconds=0,
            current_package="",
            error=None,
            completed_at=now_iso(),
        )

    return {
        "ok": all(item.get("ok") for item in results) if results else True,
        "updated_count": sum(1 for item in results if item.get("ok")),
        "failed_count": sum(1 for item in results if not item.get("ok")),
        "results": results,
        "inventory_refresh_error": refresh_error,
        "snapshot": refreshed,
    }

def run_update_selected(selected_packages: list[dict[str, str]], track_progress: bool = False) -> dict[str, Any]:
    state = read_json(STATE_FILE) or compute_snapshot(run_brew_update=False)

    outdated_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for pkg in state.get("packages", []) if isinstance(state, dict) else []:
        if not isinstance(pkg, dict) or not pkg.get("outdated"):
            continue
        name = str(pkg.get("name") or "").strip()
        kind = str(pkg.get("kind") or "").strip()
        if not name or kind not in {"formula", "cask"}:
            continue
        outdated_lookup[(kind, name)] = pkg

    targets: list[dict[str, str]] = []
    skipped: list[dict[str, Any]] = []

    for item in selected_packages:
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "").strip()
        key = (kind, name)

        if key in outdated_lookup:
            targets.append({"name": name, "kind": kind})
            continue

        skipped.append(
            {
                "ok": True,
                "name": name,
                "kind": kind,
                "code": 0,
                "stdout": "",
                "stderr": "",
                "skipped": True,
                "reason": "not_outdated",
            }
        )

    total = len(targets)

    if track_progress:
        reset_check_progress_for_run()
        set_check_progress(
            phase="brew_update",
            message="Starting selected package updates" if total > 0 else "No selected packages require update",
            done=0,
            total=total,
            eta_seconds=0 if total == 0 else None,
            current_package="",
        )

    results: list[dict[str, Any]] = []
    started = read_check_progress().get("started_at") if track_progress else None

    for idx, pkg in enumerate(targets, start=1):
        name = str(pkg.get("name") or "")
        kind = str(pkg.get("kind") or "")

        if track_progress:
            eta = estimate_eta_seconds(started, idx - 1, total)
            set_check_progress(
                phase="brew_update",
                message=f"Updating {name}",
                done=idx - 1,
                total=total,
                eta_seconds=eta,
                current_package=f"{kind}:{name}",
            )

        result = run_upgrade(name, kind)
        results.append(result)

        if track_progress:
            eta = estimate_eta_seconds(started, idx, total)
            set_check_progress(
                done=idx,
                total=total,
                eta_seconds=eta,
                current_package=f"{kind}:{name}",
            )

    refresh_error: str | None = None
    refreshed = state

    if total > 0:
        if track_progress:
            set_check_progress(
                phase="preparing",
                message="Refreshing package inventory",
                eta_seconds=None,
                current_package="",
            )

        refreshed, refresh_error = safe_refresh_snapshot(previous_snapshot=state)

        entries = history_entries_from_results(
            results,
            refreshed,
            snapshot_verified=refresh_error is None,
            snapshot_error=refresh_error,
        )
        append_update_history_entries(entries)

    combined_results = [*results, *skipped]

    if track_progress:
        completion_message = "Selected package updates completed"
        if total == 0:
            completion_message = "Selected packages are already up to date"
        elif refresh_error:
            completion_message = "Selected package updates completed (inventory refresh warning)"

        set_check_progress(
            running=False,
            phase="completed",
            message=completion_message,
            done=total,
            total=total,
            eta_seconds=0,
            current_package="",
            error=None,
            completed_at=now_iso(),
        )

    return {
        "ok": all(item.get("ok") for item in results) if results else True,
        "selected_count": len(selected_packages),
        "attempted_count": len(results),
        "skipped_count": len(skipped),
        "updated_count": sum(1 for item in results if item.get("ok")),
        "failed_count": sum(1 for item in results if not item.get("ok")),
        "results": combined_results,
        "inventory_refresh_error": refresh_error,
        "snapshot": refreshed,
    }

def escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(snapshot: dict[str, Any]) -> None:
    outdated = [pkg for pkg in snapshot.get("packages", []) if pkg.get("outdated")]

    if not outdated:
        title = "Homebrew"
        message = "✅ All packages are up to date"
    else:
        names = [str(pkg.get("name")) for pkg in outdated]
        preview = ", ".join(names[:5])
        suffix = "" if len(names) <= 5 else f", +{len(names)-5} more"
        title = "Homebrew Updates Available"
        message = f"{len(names)} package(s): {preview}{suffix}"

    notifier = shutil.which("terminal-notifier")
    if notifier:
        cmd = [
            notifier,
            "-title",
            title,
            "-subtitle",
            "Homebrew Update Check",
            "-message",
            message,
            "-execute",
            str(OPEN_APP_SCRIPT),
        ]
        res = run_cmd(cmd, timeout=15)
        if res.code == 0:
            append_check_log(f"Notification sent (clickable): {message}")
        else:
            append_check_log(f"Warning: terminal-notifier failed: {res.stderr or res.stdout}")
        return

    script = (
        f'display notification "{escape_applescript(message)}" '
        f'with title "{escape_applescript(title)}" '
        'subtitle "Homebrew Update Check"'
    )
    res = run_cmd(["osascript", "-e", script], timeout=10)
    if res.code == 0:
        append_check_log(f"Notification sent (fallback): {message}")
    else:
        append_check_log(f"Warning: osascript notification failed: {res.stderr or res.stdout}")


class BrewUpdatesHandler(BaseHTTPRequestHandler):
    server_version = "BrewUpdatesHTTP/2.0"

    def log_message(self, fmt: str, *args):
        append_service_log(fmt % args)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "operation": CURRENT_OPERATION,
                    "project_root": str(PROJECT_ROOT),
                },
            )
            return

        if self.path == "/api/settings":
            try:
                settings = load_settings()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "settings": {
                            "brew_path": str(settings.get("brew_path") or ""),
                        },
                        "scheduler": scheduler_status_payload(settings),
                    },
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "settings_unavailable", "detail": str(exc)})
            return

        if self.path == "/api/brew/auto-detect":
            try:
                settings = load_settings()
                candidates = detect_brew_candidates()
                self._send_json(
                    200,
                    {
                        "ok": bool(candidates),
                        "recommended_path": candidates[0] if candidates else None,
                        "candidates": candidates,
                        "scan_performed": True,
                        "scan_timestamp": now_iso(),
                        "current_brew_path": str(settings.get("brew_path") or ""),
                    },
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "brew_auto_detect_failed", "detail": str(exc)})
            return

        if self.path == "/api/state":
            state = read_json(STATE_FILE)
            if not state:
                try:
                    state = run_exclusive("initial_snapshot", lambda: compute_snapshot(run_brew_update=False))
                except BusyError as exc:
                    self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": CURRENT_OPERATION})
                    return
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": "state_unavailable", "detail": str(exc)})
                    return
            self._send_json(200, state)
            return

        if self.path == "/api/progress":
            self._send_json(
                200,
                {
                    "ok": True,
                    "progress": read_check_progress(),
                    "operation": CURRENT_OPERATION,
                },
            )
            return

        if self.path == "/api/update-history":
            try:
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "items": load_update_history(),
                    },
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_history_unavailable", "detail": str(exc)})
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path == "/api/settings/scheduler":
            body = self._read_json_body()
            incoming = body.get("scheduler") if isinstance(body, dict) else None

            try:
                scheduler = normalize_scheduler_payload(incoming if incoming is not None else body)
                settings = load_settings()
                settings["scheduler"] = scheduler
                save_settings(settings)
                applied = apply_scheduler_settings(scheduler)

                self._send_json(
                    200,
                    {
                        "ok": True,
                        "scheduler": applied,
                    },
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "scheduler_update_failed", "detail": str(exc)})
            return

        if self.path == "/api/settings/brew-path":
            body = self._read_json_body()
            raw_path = body.get("brew_path") if isinstance(body, dict) else ""

            try:
                normalized = normalize_brew_path(raw_path)
                settings = load_settings()
                settings["brew_path"] = normalized
                save_settings(settings)

                resolved = brew_bin()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "brew_path": normalized,
                        "resolved_brew_path": resolved,
                    },
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "brew_path_update_failed", "detail": str(exc)})
            return

        if self.path == "/api/check":
            try:
                snapshot = run_exclusive(
                    "check_now",
                    lambda: compute_snapshot(run_brew_update=True, track_progress=True),
                )
                self._send_json(200, {"ok": True, "snapshot": snapshot})
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": CURRENT_OPERATION})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "check_failed", "detail": str(exc)})
            return

        if self.path == "/api/update-one":
            body = self._read_json_body()
            name = str(body.get("name") or "").strip()
            kind = str(body.get("kind") or "").strip()

            try:
                previous_snapshot = read_json(STATE_FILE) or {}

                def _operation():
                    reset_check_progress_for_run()
                    set_check_progress(
                        phase="brew_update",
                        message=f"Updating {name}",
                        done=0,
                        total=1,
                        eta_seconds=None,
                        current_package=f"{kind}:{name}",
                        error=None,
                    )

                    result = run_upgrade(name, kind)

                    set_check_progress(
                        done=1,
                        total=1,
                        eta_seconds=0,
                        current_package=f"{kind}:{name}",
                    )

                    set_check_progress(
                        phase="preparing",
                        message="Refreshing package inventory",
                        current_package="",
                    )

                    snapshot, refresh_error = safe_refresh_snapshot(previous_snapshot=previous_snapshot)

                    entries = history_entries_from_results(
                        [result],
                        snapshot,
                        snapshot_verified=refresh_error is None,
                        snapshot_error=refresh_error,
                    )
                    append_update_history_entries(entries)

                    if result.get("ok"):
                        completed_message = f"Update completed for {name}"
                        if refresh_error:
                            completed_message = f"Update completed for {name} (inventory refresh warning)"
                        set_check_progress(
                            running=False,
                            phase="completed",
                            message=completed_message,
                            done=1,
                            total=1,
                            eta_seconds=0,
                            current_package="",
                            error=None,
                            completed_at=now_iso(),
                        )
                    else:
                        set_check_progress(
                            running=False,
                            phase="error",
                            message=f"Update failed for {name}",
                            done=1,
                            total=1,
                            eta_seconds=0,
                            current_package="",
                            error=str(result.get("stderr") or result.get("stdout") or "update failed"),
                            completed_at=now_iso(),
                        )

                    return {
                        "result": result,
                        "snapshot": snapshot,
                        "inventory_refresh_error": refresh_error,
                    }

                payload = run_exclusive(f"update_one:{kind}:{name}", _operation)
                self._send_json(200, {"ok": True, **payload})
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": CURRENT_OPERATION})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_failed", "detail": str(exc)})
            return

        if self.path == "/api/update-all":
            try:
                payload = run_exclusive("update_all", lambda: run_update_all(track_progress=True))
                self._send_json(200, payload)
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": CURRENT_OPERATION})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_all_failed", "detail": str(exc)})
            return

        if self.path == "/api/update-selected":
            body = self._read_json_body()
            raw_packages = body.get("packages") if isinstance(body, dict) else None

            try:
                selected_packages = normalize_selected_packages(raw_packages)
                payload = run_exclusive(
                    f"update_selected:{len(selected_packages)}",
                    lambda: run_update_selected(selected_packages, track_progress=True),
                )
                self._send_json(200, payload)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "invalid_packages", "detail": str(exc)})
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": CURRENT_OPERATION})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_selected_failed", "detail": str(exc)})
            return

        self._send_json(404, {"ok": False, "error": "not_found"})


def run_server(host: str, port: int) -> None:
    ensure_dirs()
    append_service_log(f"Starting HTTP server on http://{host}:{port}")
    server = ThreadingHTTPServer((host, port), BrewUpdatesHandler)
    try:
        server.serve_forever()
    finally:
        append_service_log("HTTP server stopped")


def run_check_only(notify: bool) -> int:
    ensure_dirs()
    snapshot = compute_snapshot(run_brew_update=True)
    if notify:
        send_notification(snapshot)

    counts = snapshot.get("counts", {})
    print(
        json.dumps(
            {
                "ok": True,
                "updated_at": snapshot.get("updated_at"),
                "outdated_total": counts.get("outdated_total", 0),
                "total": counts.get("total", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Homebrew Update Manager backend")
    parser.add_argument("--serve", action="store_true", help="Run HTTP service")
    parser.add_argument("--check-only", action="store_true", help="Run check once and exit")
    parser.add_argument("--notify", action="store_true", help="Send macOS notification after check")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"HTTP host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT})")
    return parser.parse_args()


def main() -> int:
    ensure_python_314()
    ensure_dirs()

    args = parse_args()

    if not args.serve and not args.check_only:
        args.serve = True

    if args.check_only:
        return run_check_only(notify=args.notify)

    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
