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
import hashlib
from contextlib import contextmanager
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import time
import platform
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_routes as routes
import macports_provider
import mac_manager
import installation_evidence
import unified_updates
import operation_lock
import provider_ownership
import copy

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None

APP_NAME = "Mac Update Manager"
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
BACKEND_STARTUP_SHA = hashlib.sha256(b''.join(
    p.name.encode() + b'\0' + hashlib.sha256(p.read_bytes()).digest()
    for p in sorted((PROJECT_ROOT / 'backend').glob('*.py')))).hexdigest()
PROCESS_STARTED_AT = now_start = time.time()
if (PROJECT_ROOT / "package.json").exists():
    DATA_DIR = PROJECT_ROOT / "data"
else:
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Homebrew Update Manager"
DATA_DIR = Path(os.environ.get("BREW_UPDATE_DATA_DIR", str(DATA_DIR)))
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"

STATE_FILE = DATA_DIR / "state.json"
CHECK_LOG_FILE = LOG_DIR / "brew-check.log"
SERVICE_LOG_FILE = LOG_DIR / "brew-service.log"
RELEASE_CACHE_FILE = CACHE_DIR / "release-dates.json"
DESCRIPTION_HE_CACHE_FILE = CACHE_DIR / "description-he.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
UPDATE_HISTORY_FILE = DATA_DIR / "update-history.json"
RUN_JOURNAL_FILE = DATA_DIR / "last-update-run.json"
UPDATE_PLAN_FILE = DATA_DIR / "update-plan.json"
TOOLS_ROOT = Path.home() / ".local" / "share" / "brew-update-manager"
UPDATE_HISTORY_LOCK_FILE = UPDATE_HISTORY_FILE.with_suffix(UPDATE_HISTORY_FILE.suffix + ".lock")
UPDATE_HISTORY_LOCK_TIMEOUT_SECONDS = 8.0
OPEN_APP_SCRIPT = PROJECT_ROOT / "tools" / "open-brew-updates-app.sh"

LAUNCH_AGENT_LABEL = "com.local.brew-outdated-check"
LAUNCH_AGENT_FILE = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
LAUNCH_AGENT_DOMAIN = f"gui/{os.getuid()}"
LAUNCH_AGENT_SERVICE = f"{LAUNCH_AGENT_DOMAIN}/{LAUNCH_AGENT_LABEL}"

OP_LOCK = threading.Lock()
CURRENT_OPERATION_LOCK = threading.Lock()
CURRENT_OPERATION: dict[str, Any] = {
    "running": False,
    "name": None,
    "started_at": None,
}

CANCEL_STATE_LOCK = threading.Lock()
CANCEL_STATE: dict[str, Any] = {
    "requested": False,
    "requested_at": None,
}

ACTIVE_COMMAND_LOCK = threading.Lock()
ACTIVE_COMMAND: dict[str, Any] = {
    "process": None,
    "args": [],
}

CHECK_PROGRESS_LOCK = threading.Lock()

DEFERRED_CHECK_LOCK = threading.Lock()
DEFERRED_CHECK_STATE: dict[str, Any] = {
    "requested": False,
    "requested_at": None,
    "reason": None,
}

PARALLEL_CHECK_LOCK = threading.Lock()
PARALLEL_CHECK_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "reason": None,
}


def new_check_progress_state() -> dict[str, Any]:
    return {
        "running": False,
        "operation_name": None,
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
        "package_activity": None,
        "planned_packages": [],
        "completed_packages": [],
        "failed_packages": [],
        "remaining_packages": [],
        "cancel_requested": False,
        "canceled": False,
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
    "excluded_packages": [],
    "binary_only": True,
    "binary_tools": [],
    "upgrade_timeout_seconds": 0,
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


class OperationCancelledError(RuntimeError):
    """Raised when the current operation was cancelled by user request."""


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


def read_current_operation() -> dict[str, Any]:
    with CURRENT_OPERATION_LOCK:
        return dict(CURRENT_OPERATION)


def read_parallel_check_state() -> dict[str, Any]:
    with PARALLEL_CHECK_LOCK:
        return dict(PARALLEL_CHECK_STATE)


def runtime_status_payload():
    # Same lock order as operation startup: ownership first, then progress.
    # A stale caller snapshot must never terminate a newly started operation.
    with CURRENT_OPERATION_LOCK:
        operation = dict(CURRENT_OPERATION)
        progress = _normalize_stale_progress_locked(operation)
    return {'ok':True,'operation':operation,'progress':progress,'parallel_check':read_parallel_check_state()}


def normalize_stale_progress_state(operation: dict[str, Any] | None = None) -> dict[str, Any]:
    return runtime_status_payload()['progress']


def _normalize_stale_progress_locked(operation: dict[str, Any]) -> dict[str, Any]:
    operation_state = operation if isinstance(operation, dict) else read_current_operation()
    progress = read_check_progress()

    if not progress.get("running"):
        return progress

    if operation_state.get("running"):
        return progress

    done = int(progress.get("done") or 0)
    total = int(progress.get("total") or 0)
    interrupted = total > 0 and done < total

    set_check_progress(
        running=False,
        phase="error" if interrupted else "completed",
        message=(
            "Operation status was stale and has been reset"
            if interrupted
            else "Operation already finished"
        ),
        eta_seconds=0,
        completed_at=now_iso(),
        cancel_requested=False,
        current_package=None,
        canceled=bool(progress.get("canceled")),
        error=(
            str(progress.get("error") or "stale_progress_state_reset")
            if interrupted
            else progress.get("error")
        ),
    )
    append_service_log("Stale progress state detected and normalized")
    return read_check_progress()


def is_cancel_requested() -> bool:
    with CANCEL_STATE_LOCK:
        return bool(CANCEL_STATE.get("requested"))


def reset_cancel_state() -> None:
    with CANCEL_STATE_LOCK:
        CANCEL_STATE["requested"] = False
        CANCEL_STATE["requested_at"] = None


def mark_cancel_requested() -> None:
    with CANCEL_STATE_LOCK:
        if not CANCEL_STATE.get("requested"):
            CANCEL_STATE["requested"] = True
            CANCEL_STATE["requested_at"] = now_iso()


def request_deferred_check(reason: str) -> bool:
    with DEFERRED_CHECK_LOCK:
        already_requested = bool(DEFERRED_CHECK_STATE.get("requested"))
        if not already_requested:
            DEFERRED_CHECK_STATE["requested"] = True
            DEFERRED_CHECK_STATE["requested_at"] = now_iso()
            DEFERRED_CHECK_STATE["reason"] = str(reason or "manual_check")
            return True
        return False


def consume_deferred_check_request() -> dict[str, Any] | None:
    with DEFERRED_CHECK_LOCK:
        if not DEFERRED_CHECK_STATE.get("requested"):
            return None
        payload = {
            "requested_at": str(DEFERRED_CHECK_STATE.get("requested_at") or ""),
            "reason": str(DEFERRED_CHECK_STATE.get("reason") or "manual_check"),
        }
        DEFERRED_CHECK_STATE["requested"] = False
        DEFERRED_CHECK_STATE["requested_at"] = None
        DEFERRED_CHECK_STATE["reason"] = None
        return payload


def _deferred_check_worker(context: dict[str, Any]) -> None:
    reason = str(context.get("reason") or "manual_check")
    append_service_log(f"Starting deferred check after update (reason={reason})")
    append_check_log(f"Starting deferred check after update (reason={reason})")

    for attempt in range(1, 4):
        try:
            run_exclusive(
                "check_now_deferred",
                lambda: compute_snapshot(run_brew_update=True, track_progress=False),
            )
            append_service_log("Deferred check completed successfully")
            append_check_log("Deferred check completed successfully")
            return
        except BusyError as exc:
            append_service_log(f"Deferred check attempt {attempt} busy: {exc}")
            time.sleep(0.8)
        except Exception as exc:
            append_service_log(f"Deferred check failed: {exc}")
            append_check_log(f"Deferred check failed: {exc}")
            return

    append_service_log("Deferred check dropped after busy retries")
    append_check_log("Deferred check dropped after busy retries")


def maybe_schedule_deferred_check(trigger_operation_name: str) -> None:
    name = str(trigger_operation_name or "")
    if not name.startswith("update"):
        return

    context = consume_deferred_check_request()
    if not context:
        return

    worker = threading.Thread(
        target=_deferred_check_worker,
        args=(context,),
        daemon=True,
    )
    worker.start()


def _parallel_check_worker(*, run_brew_update: bool, reason: str) -> None:
    error_text: str | None = None

    append_service_log(f"Parallel check started (reason={reason}, run_brew_update={run_brew_update})")
    append_check_log(f"Parallel check started (reason={reason}, run_brew_update={run_brew_update})")

    try:
        compute_snapshot(run_brew_update=run_brew_update, track_progress=False)
    except Exception as exc:
        error_text = str(exc)
        append_service_log(f"Parallel check failed: {error_text}")
        append_check_log(f"Parallel check failed: {error_text}")
    finally:
        with PARALLEL_CHECK_LOCK:
            PARALLEL_CHECK_STATE["running"] = False
            PARALLEL_CHECK_STATE["finished_at"] = now_iso()
            PARALLEL_CHECK_STATE["last_error"] = error_text

        if not error_text:
            append_service_log("Parallel check completed successfully")
            append_check_log("Parallel check completed successfully")


def start_parallel_check(*, reason: str, run_brew_update: bool) -> tuple[bool, dict[str, Any]]:
    with PARALLEL_CHECK_LOCK:
        if PARALLEL_CHECK_STATE.get("running"):
            return False, dict(PARALLEL_CHECK_STATE)

        PARALLEL_CHECK_STATE["running"] = True
        PARALLEL_CHECK_STATE["started_at"] = now_iso()
        PARALLEL_CHECK_STATE["finished_at"] = None
        PARALLEL_CHECK_STATE["last_error"] = None
        PARALLEL_CHECK_STATE["reason"] = str(reason or "manual_check")

        state_snapshot = dict(PARALLEL_CHECK_STATE)

    worker = threading.Thread(
        target=_parallel_check_worker,
        kwargs={
            "run_brew_update": bool(run_brew_update),
            "reason": str(reason or "manual_check"),
        },
        daemon=True,
    )
    worker.start()
    return True, state_snapshot


def set_check_progress(
    *,
    running: bool | None = None,
    operation_name: str | None | object = UNSET,
    phase: str | None = None,
    message: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    done: int | None = None,
    total: int | None = None,
    eta_seconds: int | None | object = UNSET,
    current_package: str | None | object = UNSET,
    planned_packages: list[dict[str, Any]] | None | object = UNSET,
    completed_packages: list[dict[str, Any]] | None | object = UNSET,
    failed_packages: list[dict[str, Any]] | None | object = UNSET,
    remaining_packages: list[dict[str, Any]] | None | object = UNSET,
    cancel_requested: bool | None | object = UNSET,
    canceled: bool | None | object = UNSET,
    error: str | None | object = UNSET,
) -> None:
    with CHECK_PROGRESS_LOCK:
        if running is not None:
            CHECK_PROGRESS["running"] = bool(running)
        if operation_name is not UNSET:
            CHECK_PROGRESS["operation_name"] = operation_name
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
        if planned_packages is not UNSET:
            CHECK_PROGRESS["planned_packages"] = list(planned_packages or [])
        if completed_packages is not UNSET:
            CHECK_PROGRESS["completed_packages"] = list(completed_packages or [])
        if failed_packages is not UNSET:
            CHECK_PROGRESS["failed_packages"] = list(failed_packages or [])
        if remaining_packages is not UNSET:
            CHECK_PROGRESS["remaining_packages"] = list(remaining_packages or [])
        if cancel_requested is not UNSET:
            CHECK_PROGRESS["cancel_requested"] = bool(cancel_requested)
        if canceled is not UNSET:
            CHECK_PROGRESS["canceled"] = bool(canceled)
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


def estimate_eta_from_completed_packages(
    completed_packages: list[dict[str, Any]],
    total_packages: int,
) -> int | None:
    done = max(0, len(completed_packages or []))
    remaining = max(0, int(total_packages or 0) - done)
    if remaining <= 0:
        return 0
    if done <= 0:
        return None

    durations: list[float] = []
    for item in completed_packages:
        start = parse_iso_datetime(str(item.get("started_at") or ""))
        end = parse_iso_datetime(str(item.get("completed_at") or ""))
        if not start or not end:
            continue
        seconds = (end - start).total_seconds()
        if seconds > 0:
            durations.append(seconds)

    if not durations:
        return None

    avg_seconds = sum(durations) / len(durations)
    return int(round(max(0.0, avg_seconds * remaining)))


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
        settings.update(normalize_update_policy(raw))
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
    normalized.update(normalize_update_policy(settings))
    normalized["brew_path"] = str(settings.get("brew_path") or "").strip()
    normalized["scheduler"] = normalize_scheduler_payload(settings.get("scheduler"))
    atomic_write_json(SETTINGS_FILE, normalized)


def normalize_update_policy(raw: dict[str, Any]) -> dict[str, Any]:
    excluded = raw.get("excluded_packages", [])
    if not isinstance(excluded, list):
        raise ValueError("excluded_packages must be an array")
    for value in excluded:
        if not isinstance(value, str) or not re.fullmatch(r"(?:formula|cask|port):[A-Za-z0-9@._+\-]+", value):
            raise ValueError("Excluded packages must use formula:name or cask:name")
    names = raw.get("binary_tools", [])
    allowed = set(routes.PIXI_TOOLS) | {"azure-cli"}
    if not isinstance(names, list) or any(name not in allowed for name in names):
        raise ValueError("Unsupported binary tool route")
    timeout = int(raw.get("upgrade_timeout_seconds", 0))
    if timeout != 0 and not 60 <= timeout <= 86400:
        raise ValueError("upgrade_timeout_seconds must be 0 or between 60 and 86400")
    return {"excluded_packages": sorted(set(excluded)), "binary_only": bool(raw.get("binary_only", True)),
            "binary_tools": sorted(set(names)), "upgrade_timeout_seconds": timeout}


def is_excluded(name: str, kind: str) -> bool:
    return f"{kind}:{name}" in load_settings()["excluded_packages"]


def apply_update_policy(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = load_settings()
    records = routes.read_records(TOOLS_ROOT)
    for pkg in packages:
        name, kind = pkg.get("name"), pkg.get("kind")
        excluded = f"{'formula' if kind == 'tool' else kind}:{name}" in settings["excluded_packages"] or (kind == "port" and name == "ngrok")
        pkg["excluded"] = excluded
        if kind not in {"port", "tool"}:
            pkg["provider"] = "homebrew"
        if excluded:
            pkg["excluded_outdated"] = bool(pkg.get("excluded_outdated") or pkg.get("outdated"))
            pkg["outdated"] = False
        elif pkg.pop("excluded_outdated", False):
            pkg["outdated"] = True
        record = records.get(name) if kind == "formula" and not pkg.get("installation_id") else None
        if record:
            pkg["brew_installed_version"] = pkg.get("brew_installed_version", pkg["installed_version"])
            pkg["installed_version"] = record["version"]
            pkg["provider"] = record["provider"]
            pkg["provider_verified_at"] = record["verified_at"]
            pkg["outdated"] = not excluded and routes.version_key(record["version"]) < routes.version_key(pkg.get("latest_version"))
        pkg["update_available"] = bool(pkg.get("outdated")) and not excluded
    return packages


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
        "StandardOutPath": str(LOG_DIR / "brew-scheduled-stdout.log"),
        "StandardErrorPath": str(LOG_DIR / "brew-scheduled-stderr.log"),
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
    provider_ownership.atomic_write_json(path, payload)


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
    # Refresh metadata once through Check Now, not once per package. Retain old
    # kegs while a batch is in progress so unrelated tools keep their libraries.
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    env["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"
    return env


def terminate_process(proc: subprocess.Popen[str] | None, *, force_kill_after_seconds: float = 4.0) -> bool:
    if proc is None:
        return False

    try:
        if proc.poll() is not None:
            return False

        used_process_group = False
        if hasattr(os, "killpg"):
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                used_process_group = True
            except Exception:
                used_process_group = False

        if not used_process_group:
            proc.terminate()

        proc.wait(timeout=force_kill_after_seconds)
        return True
    except subprocess.TimeoutExpired:
        used_process_group = False
        if hasattr(os, "killpg"):
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
                used_process_group = True
            except Exception:
                used_process_group = False

        if not used_process_group:
            proc.kill()
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
        return True
    except Exception:
        return False


def run_cmd(args: list[str], timeout: int | None = 1800, *, cancellable: bool = False,
            on_output: Callable[[str, str], None] | None = None,
            env_override: dict[str, str] | None = None) -> CommandResult:
    tracked_scan = getattr(mac_manager.SCAN_CONTEXT, 'active', False)
    if tracked_scan:
        raise_if_cancel_requested()
    result = _run_cmd_impl(args, timeout, cancellable=cancellable or tracked_scan,
                           on_output=on_output, env_override=env_override)
    if tracked_scan:
        raise_if_cancel_requested()
    return result


def _run_cmd_impl(args: list[str], timeout: int | None = 1800, *, cancellable: bool = False,
            on_output: Callable[[str, str], None] | None = None,
            env_override: dict[str, str] | None = None) -> CommandResult:
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**process_env(), **(env_override or {})},
        start_new_session=True,
    )

    if cancellable:
        with ACTIVE_COMMAND_LOCK:
            ACTIVE_COMMAND["process"] = proc
            ACTIVE_COMMAND["args"] = list(args)

    try:
        if on_output is not None:
            # Drain both pipes concurrently so a quiet stdout cannot block stderr.
            captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

            def drain(stream: Any, source: str) -> None:
                try:
                    for line in stream:
                        captured[source].append(line)
                        try:
                            on_output(line, source)
                        except Exception as exc:
                            append_service_log(f"Upgrade progress observer failed: {exc}")
                finally:
                    stream.close()

            readers = [threading.Thread(target=drain, args=(stream, source), daemon=True)
                       for stream, source in ((proc.stdout, "stdout"), (proc.stderr, "stderr"))]
            for reader in readers:
                reader.start()
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_process(proc)
            for reader in readers:
                reader.join()
            stdout = "".join(captured["stdout"]).strip()
            stderr = "".join(captured["stderr"]).strip()
            if timed_out:
                stderr = f"{stderr}\nCommand timed out after {int(timeout)}s".strip()
            return CommandResult(124 if timed_out else proc.returncode, stdout, stderr)
        stdout, stderr = proc.communicate(timeout=timeout)
        return CommandResult(proc.returncode, (stdout or "").strip(), (stderr or "").strip())
    except subprocess.TimeoutExpired:
        terminate_process(proc)
        stdout, stderr = proc.communicate()
        timeout_text = f"Command timed out after {int(timeout)}s"
        merged_stderr = (stderr or "").strip()
        if merged_stderr:
            merged_stderr = f"{merged_stderr}\n{timeout_text}".strip()
        else:
            merged_stderr = timeout_text
        return CommandResult(124, (stdout or "").strip(), merged_stderr)
    finally:
        if cancellable:
            with ACTIVE_COMMAND_LOCK:
                if ACTIVE_COMMAND.get("process") is proc:
                    ACTIVE_COMMAND["process"] = None
                    ACTIVE_COMMAND["args"] = []


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


def raise_if_cancel_requested() -> None:
    if is_cancel_requested():
        raise OperationCancelledError("Operation cancelled by user")


def request_cancel_current_operation() -> dict[str, Any]:
    operation = read_current_operation()
    if not operation.get("running"):
        progress_before = read_check_progress()
        progress = normalize_stale_progress_state(operation)

        if progress_before.get("running") and not progress.get("running"):
            return {
                "ok": True,
                "running": False,
                "message": "No active operation process found; stale progress state was reset",
                "operation": operation,
                "terminated_command": False,
                "stale_progress_cleared": True,
            }

        return {
            "ok": False,
            "running": False,
            "message": "No operation is currently running",
            "operation": operation,
            "terminated_command": False,
        }

    mark_cancel_requested()

    with ACTIVE_COMMAND_LOCK:
        active_proc = ACTIVE_COMMAND.get("process")
        active_args = list(ACTIVE_COMMAND.get("args") or [])

    terminated = terminate_process(active_proc)

    operation_name = str(operation.get("name") or "operation")
    message = f"Cancellation requested for {operation_name}"
    if terminated:
        append_check_log(f"Cancellation requested and active command terminated: {' '.join(active_args) if active_args else '(unknown command)'}")
        append_service_log(f"Cancellation requested for {operation_name}; active command terminated")
    else:
        append_check_log(f"Cancellation requested for {operation_name}")
        append_service_log(f"Cancellation requested for {operation_name}; no active command handle")

    set_check_progress(
        message=message,
        cancel_requested=True,
    )

    return {
        "ok": True,
        "running": True,
        "message": message,
        "operation": operation,
        "terminated_command": terminated,
    }


def run_with_progress(op_name, fn, *, snapshot=False):
    """Capture a deep final progress snapshot before releasing the operation lock."""
    def operation():
        try:
            value = fn()
        except OperationCancelledError:
            set_check_progress(running=False, phase='canceled', canceled=True,
                               current_package=None, eta_seconds=None, completed_at=now_iso())
            return {'ok':False, 'canceled':True, 'snapshot':read_json(STATE_FILE) or {},
                    'progress':copy.deepcopy(read_check_progress())}
        except Exception as error:
            set_check_progress(running=False, phase='error', error=str(error),
                               current_package=None, eta_seconds=None, completed_at=now_iso())
            error.operation_progress=copy.deepcopy(read_check_progress())
            raise
        progress=copy.deepcopy(read_check_progress())
        progress['partial']=bool(value.get('partial')) if snapshot else bool(value.get('inventory_refresh_error'))
        with CHECK_PROGRESS_LOCK:
            CHECK_PROGRESS['partial']=progress['partial']
        return {'ok':True,'snapshot':value,'progress':progress} if snapshot else {**value,'progress':progress}
    return run_exclusive(op_name, operation)


def run_exclusive(op_name: str, fn):
    if not OP_LOCK.acquire(blocking=False):
        active_operation = read_current_operation()
        raise BusyError(
            f"Operation '{active_operation.get('name')}' is already running since {active_operation.get('started_at')}"
        )

    reset_cancel_state()
    with CURRENT_OPERATION_LOCK:
        CURRENT_OPERATION["running"] = True
        CURRENT_OPERATION["name"] = op_name
        CURRENT_OPERATION["started_at"] = now_iso()

    prior_scan = getattr(mac_manager.SCAN_CONTEXT, 'active', False)
    if op_name in {'update_plan', 'migration'}:
        mac_manager.SCAN_CONTEXT.active = True
    try:
        if op_name.startswith(("update", "migration", "macports")) and op_name != "update_plan":
            with operation_lock.OperationLock(TOOLS_ROOT, op_name):
                return fn()
        return fn()
    except OperationCancelledError:
        set_check_progress(running=False, phase='canceled', message='Operation canceled',
                           canceled=True, current_package=None, eta_seconds=None, completed_at=now_iso())
        raise
    finally:
        mac_manager.SCAN_CONTEXT.active = prior_scan
        reset_cancel_state()
        with CURRENT_OPERATION_LOCK:
            CURRENT_OPERATION["running"] = False
            CURRENT_OPERATION["name"] = None
            CURRENT_OPERATION["started_at"] = None
        OP_LOCK.release()
        maybe_schedule_deferred_check(op_name)


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
        raise RuntimeError(f"Cannot verify outdated inventory: {res.stderr or res.stdout}")

    try:
        payload = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        append_check_log("Warning: failed to parse brew outdated JSON")
        raise RuntimeError("Cannot verify outdated inventory: invalid Homebrew JSON")

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
    try:
        evidence_context = installation_evidence.collect(sys.modules[__name__], payload)
        payload.setdefault('formulae', []).extend(evidence_context['omitted'])
    except OperationCancelledError:
        raise
    except Exception as error:
        evidence_context = None
        installation_evidence.reset()
        append_service_log('Installation evidence unavailable: ' + str(error))
    packages: list[dict[str, Any]] = []

    for row in payload.get("formulae", []) or []:
        if not isinstance(row, dict):
            continue

        name = str(row.get("name") or "").strip()
        if not name:
            continue

        installed_entries = row.get("installed") or []
        installed_versions = normalize_versions(installed_entries)
        installation_fields = installation_evidence.inventory_fields(sys.modules[__name__], row, evidence_context)
        installed_version = installation_fields['installed_version']
        latest_version = str(((row.get("versions") or {}).get("stable") or "unknown"))

        outdated_meta = outdated_index.get(("formula", name))
        outdated = bool(outdated_meta) or bool(row.get("outdated", False))
        if outdated_meta:
            installation_fields['manager_outdated_version'] = outdated_meta.get('installed_version')
            latest_version = outdated_meta.get("latest_version") or latest_version

        packages.append(
            {
                "name": name,
                "kind": "formula",
                **installation_fields,
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

    # Managed tools remain installed after their redundant Brew keg is removed.
    # Query catalog metadata separately; never infer "current" from a missing row.
    records = routes.read_records(TOOLS_ROOT)
    existing = {(pkg["kind"], pkg["name"]) for pkg in packages}
    missing = sorted(name for name in records if ("formula", name) not in existing)
    if missing:
        for name in missing:
            validate_package_name(name)
        result = run_cmd([b, "info", "--json=v2", "--formula", *missing], timeout=120)
        if result.code:
            raise RuntimeError("Cannot check catalog metadata for separately installed tools: " + result.stderr)
        catalog = {row["name"]: row for row in json.loads(result.stdout).get("formulae", [])}
        for name in missing:
            meta = catalog.get(name)
            if not meta:
                raise RuntimeError(f"Missing catalog metadata for separately installed tool: {name}")
            latest = str(meta["versions"]["stable"])
            if meta.get("revision"):
                latest += "_" + str(meta["revision"])
            record = records[name]
            packages.append({"name": name, "kind": "formula", "display_name": name,
                             "description": meta.get("desc", ""), "description_he": "",
                             "homepage": meta.get("homepage", ""), "tap": meta.get("tap", "homebrew/core"),
                             "installed_version": record["version"], "installed_versions": [record["version"]],
                             "latest_version": latest, "outdated": False, "brew_installation_present": False,
                             "current_release_date": None, "latest_release_date": None,
                             "outdated_since": None, "days_since_outdated": None,
                             "release_date": None, "release_date_source": None})
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


def compute_snapshot(run_brew_update, track_progress=False, *, enrich_details=True):
    return mac_manager.combined(sys.modules[__name__], compute_homebrew_snapshot,
                                run_brew_update, track_progress, enrich_details)


def summarize_counts(packages: list[dict[str, Any]]) -> dict[str, int]:
    formulas = [pkg for pkg in packages if pkg.get("kind") == "formula"]
    casks = [pkg for pkg in packages if pkg.get("kind") == "cask"]
    outdated = [pkg for pkg in packages if pkg.get("outdated")]
    return {
        "excluded_total": sum(bool(pkg.get("excluded")) for pkg in packages),
        "managed_total": sum(pkg.get("provider", "homebrew") != "homebrew" for pkg in packages),
        "total": len(packages),
        "formulae": len(formulas),
        "ports": sum(p.get("kind") == "port" for p in packages),
        "tools": sum(p.get("kind") == "tool" for p in packages),
        "casks": len(casks),
        "outdated_total": len(outdated),
        "outdated_formulae": sum(1 for pkg in outdated if pkg.get("kind") == "formula"),
        "outdated_casks": sum(1 for pkg in outdated if pkg.get("kind") == "cask"),
    }


def reuse_cached_package_details(packages: list[dict[str, Any]]) -> None:
    previous = read_json(STATE_FILE) or {}
    cached = {(p.get("kind"), p.get("name")): p for p in previous.get("packages", []) if isinstance(p, dict)}
    for pkg in packages:
        old = cached.get((pkg.get("kind"), pkg.get("name")), {})
        same_installed = pkg.get("installed_version") == old.get("installed_version")
        same_latest = pkg.get("latest_version") == old.get("latest_version")
        if same_installed:
            for key in ("current_release_date", "current_release_date_source"):
                if key in old:
                    pkg[key] = old[key]
        if same_latest:
            for key in ("latest_release_date", "latest_release_date_source"):
                if key in old:
                    pkg[key] = old[key]
        if same_installed and same_latest:
            for key in ("release_date", "release_date_source"):
                if key in old:
                    pkg[key] = old[key]
        if pkg.get("description") == old.get("description") and old.get("description_he"):
            pkg["description_he"] = old["description_he"]
        if pkg.get("outdated") and pkg.get("latest_release_date"):
            pkg["outdated_since"] = pkg["latest_release_date"]
            pkg["days_since_outdated"] = compute_days_since(pkg["outdated_since"])


def compute_homebrew_snapshot(run_brew_update: bool, track_progress: bool = False, *, enrich_details: bool = True, complete_progress: bool = True) -> dict[str, Any]:
    append_check_log("Starting Homebrew check")
    errors: list[str] = []

    if track_progress:
        reset_check_progress_for_run()
        set_check_progress(operation_name="check", phase="preparing", message="Preparing Homebrew inventory")

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
            raise_if_cancel_requested()
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

        if enrich_details:
            enrich_release_dates(packages, progress_callback=_release_progress if track_progress else None)
        else:
            reuse_cached_package_details(packages)

        def _translation_progress(done: int, total: int, pkg: dict[str, Any]) -> None:
            if not track_progress:
                return
            raise_if_cancel_requested()
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

        if enrich_details:
            enrich_hebrew_descriptions(packages, progress_callback=_translation_progress if track_progress else None)

        apply_update_policy(packages)
        packages.sort(key=lambda item: (str(item.get("kind")), str(item.get("name"))))

        snapshot = {
            "app": APP_NAME,
            "updated_at": now_iso(),
            "counts": summarize_counts(packages),
            "errors": errors,
            "packages": packages,
        }

        # The combined inventory wrapper persists a complete snapshot.
        append_check_log(
            f"Check completed. total={snapshot['counts']['total']} outdated={snapshot['counts']['outdated_total']}"
        )

        if track_progress and complete_progress:
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
        if track_progress and complete_progress:
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
        if kind not in {"formula", "cask", "port", "tool"}:
            raise ValueError("kind must be 'formula' or 'cask'")

        key = (kind, name)
        if key in seen:
            continue

        seen.add(key)
        normalized.append({"name": name, "kind": kind})

    if not normalized:
        raise ValueError("packages must include at least one valid package")

    return normalized


def progress_package_item(name: str, kind: str) -> dict[str, Any]:
    normalized_name = str(name or "").strip()
    normalized_kind = str(kind or "").strip()
    return {
        "name": normalized_name,
        "kind": normalized_kind,
        "label": f"{normalized_kind}:{normalized_name}",
    }


def summarize_result_error(result: dict[str, Any], limit: int = 220) -> str:
    detail = str(result.get("stderr") or result.get("stdout") or "").strip()
    if not detail:
        return ""
    if len(detail) <= limit:
        return detail
    return f"{detail[: max(0, limit - 1)]}…"


def progress_result_item(result: dict[str, Any], *, started_at: str | None = None) -> dict[str, Any]:
    name = str(result.get("name") or "").strip()
    kind = str(result.get("kind") or "").strip()
    item = {
        "name": name,
        "kind": kind,
        "label": f"{kind}:{name}",
        "ok": bool(result.get("ok", False)),
        "skipped": bool(result.get("skipped", False)),
        "canceled": bool(result.get("canceled", False)),
        "reason": str(result.get("reason") or "").strip(),
        "code": int(result.get("code") or 0),
        "error": summarize_result_error(result),
        "started_at": started_at,
        "completed_at": now_iso(),
    }
    return item

class UpgradeActivity:
    """Translate explicit Homebrew output into conservative UI stage identifiers."""

    def __init__(self, name: str):
        self.lock = threading.Lock()
        self.started_epoch = time.time()
        self.monitor_stop = threading.Event()
        self.state: dict[str, Any] = {
            "target": name, "subject": name, "stage": "preparing",
            "started_at": now_iso(), "stage_started_at": now_iso(),
            "last_output_at": None, "technical_tail": [],
        }
        self.publish()

    def monitor_build_logs(self) -> None:
        def follow():
            last_signature = None
            while not self.monitor_stop.wait(1.5):
                subject = self.state["subject"]
                directory = Path.home() / "Library/Logs/Homebrew" / subject
                candidates = [p for p in directory.glob("*.log") if not p.name.endswith(".cc.log")
                              and p.stat().st_mtime >= self.started_epoch]
                if not candidates:
                    continue
                path = max(candidates, key=lambda p: p.stat().st_mtime)
                signature = (str(path), path.stat().st_size, path.stat().st_mtime_ns)
                if signature == last_signature:
                    continue
                last_signature = signature
                try:
                    with path.open("rb") as stream:
                        stream.seek(max(0, path.stat().st_size - 18000))
                        tail = stream.read().decode(errors="replace")
                    tasks = re.findall(r"\[(\d+)/(\d+)\]", tail)
                    percentages = re.findall(r"\[\s*(\d+)%\]", tail)
                    with self.lock:
                        self.state["last_output_at"] = now_iso()
                        if tasks:
                            done, total = map(int, tasks[-1])
                            self.state["build_progress"] = {"kind": "tasks", "done": done, "total": total, "percent": round(100 * done / max(1, total))}
                            self.state["stage"] = "building"
                        elif percentages:
                            self.state["build_progress"] = {"kind": "reported_percent", "percent": int(percentages[-1])}
                            self.state["stage"] = "building"
                        self.publish()
                except OSError:
                    continue
        self.monitor_thread = threading.Thread(target=follow, daemon=True)
        self.monitor_thread.start()

    def stop_monitor(self) -> None:
        self.monitor_stop.set()
        if hasattr(self, "monitor_thread"):
            self.monitor_thread.join(timeout=2)

    def publish(self) -> None:
        with CHECK_PROGRESS_LOCK:
            CHECK_PROGRESS["package_activity"] = {
                **self.state, "technical_tail": list(self.state["technical_tail"]),
            }

    def observe(self, raw: str, source: str) -> None:
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw).strip()
        if not line:
            return
        with self.lock:
            append_log(LOG_DIR / "brew-upgrades.log", f"[{source}] {line}")
            self.state["last_output_at"] = now_iso()
            self.state["technical_tail"] = (self.state["technical_tail"] + [line[-1000:]])[-12:]
            stage = self.state["stage"]
            subject = self.state["subject"]
            dependency = re.match(r"==> (?:Installing|Upgrading) .+? dependency: (\S+)", line)
            target = re.match(r"==> (?:Installing|Upgrading) (\S+)", line)
            if source == "provider" and line.startswith("Downloading ready-to-use"):
                stage = "downloading"
            elif source == "provider" and line == "Verifying installed commands":
                stage = "finishing"
            elif dependency:
                subject, stage = dependency.group(1), "dependency"
            elif line.startswith("==> Fetching downloads for:") or line.startswith("==> Downloading "):
                stage = "downloading"
            elif line.startswith("==> Pouring "):
                stage = "installing"
            elif line.startswith("==> ") and re.match(
                r"(?:\./bootstrap|\./configure|cmake\b|make\b|gmake\b|ninja\b|meson\b|cargo\b|go build\b)", line[4:]
            ):
                stage = "building"
            elif target and target.group(1) == self.state["target"]:
                subject, stage = self.state["target"], "installing"
            elif line.startswith("==> Caveats") or line.startswith("==> Running `brew cleanup"):
                stage = "finishing"
            if stage != self.state["stage"] or subject != self.state["subject"]:
                self.state["stage_started_at"] = now_iso()
                self.state.pop("build_progress", None)
            self.state.update(stage=stage, subject=subject)
            self.publish()


def run_upgrade(name: str, kind: str) -> dict[str, Any]:
    validate_package_name(name)
    if is_excluded(name, kind):
        return {"ok": False, "skipped": True, "reason": "excluded", "name": name, "kind": kind,
                "code": 78, "stdout": "", "stderr": "Package is excluded from updates"}
    if kind == "tool":
        from update_engine import Engine
        engine = Engine(sys.modules[__name__])
        state = compute_snapshot(False, enrich_details=False)
        pkg = package_from_snapshot(state, name, kind)
        if not pkg:
            raise ValueError("Managed installation not found")
        item = engine.plan([pkg])[0]
        if not item['ready']:
            raise ValueError(item.get('detail', item.get('reason', 'Unavailable')))
        return dict(engine.install_tool(item), kind='tool')
    if kind == "port":
        pkg = next((p for p in macports_provider.inventory(sys.modules[__name__]) if p["name"] == name), None)
        if not pkg:
            raise ValueError("MacPorts package is not installed; use migration approval")
        return macports_provider.upgrade(sys.modules[__name__], pkg)
    settings = load_settings()
    from update_engine import Engine
    engine = Engine(sys.modules[__name__])
    if routes.route_for(name, kind, settings) != "homebrew" and name not in routes.read_records(TOOLS_ROOT):
        state = read_json(STATE_FILE) or compute_snapshot(False, enrich_details=False)
        pkg = package_from_snapshot(state, name, kind)
        if not pkg:
            raise ValueError("Package is not in the installed inventory")
        item = engine.plan([pkg])[0]
        if not item["ready"]:
            return {"ok": False, "name": name, "kind": kind, "code": 78, "stdout": "",
                    "stderr": item.get("detail", item["reason"]), "reason": item["reason"]}
        return engine.install_tool(item)
    if kind == "formula" and settings["binary_only"]:
        blockers = routes.bottle_blockers(name, engine.catalog(), engine.installed(), settings["excluded_packages"])
        if blockers:
            return {"ok": False, "name": name, "kind": kind, "code": 78, "stdout": "", "reason": "source_required",
                    "stderr": "Source build was deferred: " + ", ".join(x["name"] for x in blockers)}
    b = brew_bin()

    if kind == "formula":
        cmd = [b, "upgrade", name]
    elif kind == "cask":
        cmd = [b, "upgrade", "--cask", name]
    else:
        raise ValueError("kind must be 'formula' or 'cask'")

    append_check_log(f"Starting upgrade: {kind}:{name}")
    append_log(LOG_DIR / "brew-upgrades.log", f"Starting upgrade: {kind}:{name}")
    activity = UpgradeActivity(name)
    activity.monitor_build_logs()
    source_fallback = False

    def observe(line: str, source: str) -> None:
        nonlocal source_fallback
        activity.observe(line, source)
        if settings["binary_only"] and activity.state["stage"] == "building":
            source_fallback = True
            with ACTIVE_COMMAND_LOCK:
                proc = ACTIVE_COMMAND.get("process")
            terminate_process(proc)

    try:
        res = run_cmd(cmd, timeout=settings["upgrade_timeout_seconds"] or None, cancellable=True,
                      on_output=observe, env_override=engine.brew_env)
    finally:
        activity.stop_monitor()
        with CHECK_PROGRESS_LOCK:
            CHECK_PROGRESS["package_activity"] = None
    ok = res.code == 0 and not source_fallback
    if source_fallback:
        res.code = 78
        res.stderr += "\nUnexpected source fallback stopped by binary-only policy"
    append_log(LOG_DIR / "brew-upgrades.log", f"Finished upgrade: {kind}:{name}; exit_code={res.code}")
    canceled = bool(is_cancel_requested() and not ok)

    if ok:
        append_check_log(f"Upgrade succeeded: {kind}:{name}")
    elif canceled:
        append_check_log(f"Upgrade cancelled: {kind}:{name}: {res.stderr or res.stdout}")
    else:
        append_check_log(f"Upgrade failed: {kind}:{name}: {res.stderr or res.stdout}")

    return {
        "ok": ok,
        "canceled": canceled,
        "name": name,
        "kind": kind,
        "code": res.code,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "reason": "source_fallback" if source_fallback else "",
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
        refreshed = compute_snapshot(run_brew_update=False, enrich_details=False)
        return refreshed, ("Partial provider inventory" if refreshed.get("partial") else None)
    except Exception as exc:
        detail = str(exc)
        append_check_log(f"Warning: package inventory refresh failed after update: {detail}")
        append_service_log(f"Warning: package inventory refresh failed after update: {detail}")
        return fallback_snapshot_with_error(previous_snapshot, detail), detail


def run_update_all(track_progress: bool = False) -> dict[str, Any]:
    from update_engine import Engine
    return Engine(sys.modules[__name__]).run(track_progress=track_progress)


def run_update_selected(selected_packages: list[dict[str, str]], track_progress: bool = False) -> dict[str, Any]:
    from update_engine import Engine
    return Engine(sys.modules[__name__]).run(selected_packages, track_progress=track_progress)


def escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(snapshot: dict[str, Any], notifier_override=None) -> None:
    outdated = [pkg for pkg in snapshot.get("packages", []) if pkg.get("outdated")]
    if not outdated:
        title = "Mac Update Manager"
        message = "All packages are up to date"
    else:
        names = [str(pkg.get("name")) for pkg in outdated]
        preview = ", ".join(names[:5])
        suffix = "" if len(names) <= 5 else f", +{len(names)-5} more"
        title = "Mac Updates Available"
        message = f"{len(names)} package(s): {preview}{suffix}"

    if snapshot.get("notification_test"):
        title = "Mac Update Manager"
        message = "Notification test: the MacPorts migration was verified"
    helper = Path.home() / "Applications" / "Homebrew Update Notifications.app" / "Contents" / "MacOS" / "terminal-notifier"
    route = read_json(DATA_DIR / 'notifier-route.json') or {}
    selected = route.get('path') if route.get('provider') in {'macports', 'application_helper'} else None
    notifier = notifier_override or selected or (str(helper) if helper.is_file() else shutil.which("terminal-notifier"))
    if not notifier:
        raise RuntimeError("The Homebrew notification helper is not installed")

    # Version 3 uses UserNotifications and stores click actions with macOS,
    # so this command exits without leaving a listener running.
    version = run_cmd([notifier, "-version"], timeout=10)
    version_text = f"{version.stdout} {version.stderr}"
    if not re.search(r"\b[3-9]\.\d+", version_text):
        raise RuntimeError("Homebrew notifications require terminal-notifier 3 or later")

    command = "/bin/bash " + shlex.quote(str(OPEN_APP_SCRIPT))
    result = run_cmd([
        notifier, "-title", title,
        "-subtitle", "Mac Update Check",
        "-message", message,
        "-sound", "default",
        "-group", "homebrew-update-manager",
        "-execute", command,
    ], timeout=60)
    if result.code != 0:
        detail = result.stderr or result.stdout or f"exit code {result.code}"
        append_check_log(f"Notification failed: {detail}")
        raise RuntimeError(f"macOS notification failed: {detail}")
    append_check_log(f"Notification accepted by macOS: {message}")



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

    def _mac_operation(self):
        if self.path not in {"/api/migrations/plan", "/api/migrations/approve", "/api/migrations/recovery-plan", "/api/macports/sync"}:
            return False
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or self.headers.get('Origin'):
            self._send_json(403, {'ok': False, 'detail': 'Desktop JSON requests only'})
            return True
        body = self._read_json_body()
        def operation():
            reset_check_progress_for_run()
            set_check_progress(operation_name="migration", phase="preflight", running=True,
                               message="Checking MacPorts ready packages", eta_seconds=None)
            try:
                if self.path == "/api/migrations/plan":
                    return {"ok": True, "plan": mac_manager.migration_plan(sys.modules[__name__], body.get("name"))}
                if self.path == "/api/migrations/recovery-plan":
                    return {"ok": True, "plan": mac_manager.migration_recovery_plan(sys.modules[__name__], body.get("transaction_id"), body.get("action"))}
                if self.path == "/api/migrations/approve":
                    raise unified_updates.PlanError("review_required", "Legacy migration approval is disabled until transactional recovery is complete")
                    result = mac_manager.approve(sys.modules[__name__], body.get("id"))
                    return {"ok": True, "migration": result, "snapshot": compute_snapshot(False, enrich_details=False)}
                result = macports_provider.privileged(sys.modules[__name__], "sync")
                if result.code: raise RuntimeError(result.stderr)
                mac_manager.catalog_refresh.mark_success(sys.modules[__name__])
                return {"ok": True}
            finally:
                with CHECK_PROGRESS_LOCK:
                    CHECK_PROGRESS["package_activity"] = None
                canceled = isinstance(sys.exc_info()[1], OperationCancelledError)
                set_check_progress(running=False, phase='canceled' if canceled else ('error' if sys.exc_info()[0] else 'completed'),
                                   canceled=canceled, current_package=None,
                                   completed_at=now_iso(), eta_seconds=None if sys.exc_info()[0] else 0)
        try:
            self._send_json(200, run_with_progress("migration", operation))
        except BusyError as exc:
            self._send_json(409, {"ok": False, "detail": str(exc)})
        except Exception as exc:
            self._send_json(400, {"ok": False, "detail": str(exc), "progress": getattr(exc, "operation_progress", None)})
        return True

    def _apply_unified_request(self, body, *, all_ready=False):
        try:
            def operation():
                reset_check_progress_for_run()
                set_check_progress(operation_name="unified_update", phase="preflight",
                                   message="Validating displayed provider decisions",
                                   running=True, eta_seconds=None)
                return unified_updates.execute(sys.modules[__name__], body, all_ready=all_ready)
            response = run_with_progress("update_unified", operation)
            self._send_json(200, response)
        except unified_updates.PlanError as exc:
            code = 400 if exc.code in {"invalid_request", "invalid_request_id", "ambiguous_decisions"} else 409
            self._send_json(code, {"ok": False, "error": exc.code, "detail": str(exc),
                                   "plan": unified_updates.read_plan(sys.modules[__name__])})
        except (BusyError, operation_lock.OperationBusy) as exc:
            self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": "update_failed", "detail": str(exc)})
        return True

    def do_GET(self):
        if self.path == "/health":
            operation = read_current_operation()
            self._send_json(
                200,
                {
                    "ok": True,
                    "app": APP_NAME,
                    "operation": operation,
                    "project_root": str(PROJECT_ROOT),
                    "protocol": 6,
                    "version": "1.3.3",
                    "pid": os.getpid(),
                    "data_dir": str(DATA_DIR),
                    "capabilities": ["migration-v1", "migration-recovery"],
                    "backend_sha256": BACKEND_STARTUP_SHA,
                    "backend_code_sha256": BACKEND_STARTUP_SHA,
                    "process_started_at": PROCESS_STARTED_AT,
                    "build_id": "unified-intel-foundations",
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
                            **settings,
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
            if not state or state.get("app") != APP_NAME or "providers" not in state:
                try:
                    state = run_exclusive("initial_snapshot", lambda: compute_snapshot(run_brew_update=False, enrich_details=False))
                except BusyError as exc:
                    self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": read_current_operation()})
                    return
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": "state_unavailable", "detail": str(exc)})
                    return
            apply_update_policy(state.get("packages", []))
            state["counts"] = summarize_counts(state.get("packages", []))
            state["update_plan"] = read_json(UPDATE_PLAN_FILE)
            blocked = {(x["kind"], x["name"]): x for x in (state["update_plan"] or {}).get("items", []) if not x["ready"]}
            for pkg in state.get("packages", []):
                item = blocked.get((pkg["kind"], pkg["name"]))
                if pkg.get("inventory_stale"):
                    pkg["update_block_reason"] = "inventory_unavailable"
                    continue
                pkg.pop("update_block_reason", None)
                if (pkg.get('kind') == 'formula' and load_settings()['binary_only'] and
                    any(o['name'] == pkg['name'] and o.get('source_blockers') for o in state.get('migrations', []))):
                    pkg['update_block_reason'] = 'source_required'
                if item and pkg.get("outdated") and item["version"] == pkg["latest_version"]:
                    if item["reason"] != "source_required" or load_settings()["binary_only"]:
                        pkg["update_block_reason"] = item["reason"]
            unified_updates.attach_cached(sys.modules[__name__], state)
            self._send_json(200, state)
            return

        if self.path == "/api/update-plan":
            self._send_json(200, {"ok": True, "plan": unified_updates.read_plan(sys.modules[__name__])})
            return

        if self.path == "/api/progress":
            self._send_json(200, runtime_status_payload())
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
        if self._mac_operation():
            return
        if self.path == "/api/settings/update-policy":
            try:
                if read_current_operation().get("running"):
                    raise BusyError("Wait for the active operation before changing update policy")
                body = self._read_json_body()
                settings = load_settings()
                settings.update(normalize_update_policy({**settings, **body}))
                save_settings(settings)
                self._send_json(200, {"ok": True, "settings": settings})
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"ok": False, "detail": str(exc)})
            except BusyError as exc:
                self._send_json(409, {"ok": False, "detail": str(exc)})
            return
        if self.path == "/api/update-plan":
            try:
                from update_engine import Engine
                def prepare():
                    reset_check_progress_for_run()
                    set_check_progress(operation_name="update_plan", phase="preflight", eta_seconds=None)
                    state = compute_snapshot(False, enrich_details=False)
                    items = Engine(sys.modules[__name__]).plan([p for p in state["packages"] if p.get("outdated") and not p.get("excluded")])
                    raise_if_cancel_requested()
                    set_check_progress(running=False, phase="completed", message="Update routes checked", current_package=None, completed_at=now_iso(), eta_seconds=0)
                    return {"ok": True, "items": items}
                self._send_json(200, run_exclusive("update_plan", prepare))
            except BusyError as exc:
                self._send_json(409, {"ok": False, "detail": str(exc)})
            except Exception as exc:
                canceled = isinstance(exc, OperationCancelledError)
                set_check_progress(running=False, phase='canceled' if canceled else 'error',
                                   canceled=canceled, error=None if canceled else str(exc),
                                   current_package=None, eta_seconds=None, completed_at=now_iso())
                self._send_json(500, {"ok": False, "detail": str(exc)})
            return
        if self.path == "/api/cancel-operation":
            payload = request_cancel_current_operation()
            if payload.get("ok"):
                self._send_json(200, payload)
            else:
                self._send_json(409, payload)
            return

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
            operation = read_current_operation()
            operation_name = str(operation.get("name") or "")

            if operation.get("running") and operation_name.startswith("update"):
                started, parallel_state = start_parallel_check(
                    reason="manual_check_during_update",
                    run_brew_update=False,
                )
                detail = "Parallel check started while update is running"
                if not started:
                    detail = "Parallel check is already running"
                self._send_json(
                    202,
                    {
                        "ok": True,
                        "parallel": True,
                        "started": started,
                        "detail": detail,
                        "operation": operation,
                        "parallel_check": parallel_state,
                    },
                )
                return

            active_parallel = read_parallel_check_state()
            if active_parallel.get("running"):
                self._send_json(
                    202,
                    {
                        "ok": True,
                        "parallel": True,
                        "started": False,
                        "detail": "Parallel check is already running",
                        "operation": operation,
                        "parallel_check": active_parallel,
                    },
                )
                return

            try:
                payload = run_with_progress(
                    "check_now",
                    lambda: compute_snapshot(run_brew_update=True, track_progress=True), snapshot=True,
                )
                self._send_json(200, payload)
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": read_current_operation()})
            except OperationCancelledError:
                self._send_json(200, {"ok": True, "canceled": True})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "check_failed", "detail": str(exc), "progress": getattr(exc, "operation_progress", None)})
            return

        if self.path == "/api/update-one":
            body = self._read_json_body()
            if body.get("plan_id"):
                return self._apply_unified_request(body)
            name = str(body.get("name") or "").strip()
            kind = str(body.get("kind") or "").strip()

            try:
                previous_snapshot = read_json(STATE_FILE) or {}

                def _operation():
                    planned = [progress_package_item(name, kind)]
                    completed: list[dict[str, Any]] = []
                    failed: list[dict[str, Any]] = []
                    reset_check_progress_for_run()
                    set_check_progress(
                        operation_name="update_one",
                        phase="updating_packages",
                        message=f"Updating {name}",
                        done=0,
                        total=1,
                        eta_seconds=None,
                        current_package=f"{kind}:{name}",
                        planned_packages=planned,
                        completed_packages=completed,
                        failed_packages=failed,
                        remaining_packages=planned,
                        cancel_requested=False,
                        canceled=False,
                        error=None,
                    )

                    package_started_at = now_iso()
                    result = run_upgrade(name, kind)
                    result_item = progress_result_item(result, started_at=package_started_at)
                    completed.append(result_item)
                    if not result.get("ok"):
                        failed.append(result_item)

                    cancelled = bool(result.get("canceled"))

                    set_check_progress(
                        done=1,
                        total=1,
                        eta_seconds=0,
                        current_package=f"{kind}:{name}",
                        completed_packages=completed,
                        failed_packages=failed,
                        remaining_packages=[],
                        cancel_requested=is_cancel_requested(),
                        canceled=cancelled,
                    )

                    set_check_progress(
                        phase="preparing",
                        message="Refreshing package inventory" if not cancelled else "Refreshing package inventory after cancellation",
                        current_package="",
                        planned_packages=planned,
                        completed_packages=completed,
                        failed_packages=failed,
                        remaining_packages=[],
                        cancel_requested=is_cancel_requested(),
                        canceled=cancelled,
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
                            planned_packages=planned,
                            completed_packages=completed,
                            failed_packages=failed,
                            remaining_packages=[],
                            cancel_requested=is_cancel_requested(),
                            canceled=False,
                            error=None,
                            completed_at=now_iso(),
                        )
                    elif cancelled:
                        set_check_progress(
                            running=False,
                            phase="canceled",
                            message=f"Update canceled for {name}",
                            done=1,
                            total=1,
                            eta_seconds=0,
                            current_package="",
                            planned_packages=planned,
                            completed_packages=completed,
                            failed_packages=failed,
                            remaining_packages=[],
                            cancel_requested=is_cancel_requested(),
                            canceled=True,
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
                            planned_packages=planned,
                            completed_packages=completed,
                            failed_packages=failed,
                            remaining_packages=[],
                            cancel_requested=is_cancel_requested(),
                            canceled=False,
                            error=str(result.get("stderr") or result.get("stdout") or "update failed"),
                            completed_at=now_iso(),
                        )

                    return {
                        "result": result,
                        "snapshot": snapshot,
                        "inventory_refresh_error": refresh_error,
                    }

                payload = run_with_progress(f"update_one:{kind}:{name}", _operation)
                self._send_json(200, {"ok": True, **payload})
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": read_current_operation()})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_failed", "detail": str(exc), "progress": getattr(exc, "operation_progress", None)})
            return

        if self.path == "/api/update-all":
            body = self._read_json_body()
            if body.get("plan_id"):
                return self._apply_unified_request(body, all_ready=True)
            try:
                payload = run_with_progress("update_all", lambda: run_update_all(track_progress=True))
                self._send_json(200, payload)
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": read_current_operation()})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_all_failed", "detail": str(exc), "progress": getattr(exc, "operation_progress", None)})
            return

        if self.path == "/api/update-selected":
            body = self._read_json_body()
            if body.get("plan_id"):
                return self._apply_unified_request(body)
            raw_packages = body.get("packages") if isinstance(body, dict) else None

            try:
                selected_packages = normalize_selected_packages(raw_packages)
                payload = run_with_progress(
                    f"update_selected:{len(selected_packages)}",
                    lambda: run_update_selected(selected_packages, track_progress=True),
                )
                self._send_json(200, payload)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": "invalid_packages", "detail": str(exc)})
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": "busy", "detail": str(exc), "operation": read_current_operation()})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "update_selected_failed", "detail": str(exc), "progress": getattr(exc, "operation_progress", None)})
            return

        self._send_json(404, {"ok": False, "error": "not_found"})


def run_server(host: str, port: int) -> None:
    ensure_dirs()
    journal = read_json(RUN_JOURNAL_FILE)
    if journal:
        plan = journal.get("plan", [])
        results = journal.get("results", [])
        ready = [progress_package_item(p["name"], p["kind"]) for p in plan if p.get("ready")]
        completed = [{**progress_result_item(r, started_at=r.get("started_at")), "completed_at": r.get("completed_at")}
                     for r in results]
        phase = journal.get("status", "error")
        interrupted = phase == "running"
        set_check_progress(running=False, operation_name="update_all", phase="error" if interrupted else phase,
                           message="Previous run was interrupted; refresh installed versions before retrying" if interrupted else "Previous update results restored",
                           started_at=journal.get("started_at"), completed_at=journal.get("completed_at"),
                           done=len(results), total=len(ready), planned_packages=ready, completed_packages=completed,
                           failed_packages=[r for r in completed if not r["ok"]], remaining_packages=ready[len(results):],
                           eta_seconds=None if interrupted else 0, error="interrupted_run" if interrupted else None)
        with CHECK_PROGRESS_LOCK:
            CHECK_PROGRESS["deferred_packages"] = [p for p in plan if not p.get("ready")]
    append_service_log(f"Starting HTTP server on http://{host}:{port}")
    server = ThreadingHTTPServer((host, port), BrewUpdatesHandler)
    shutdown_requested = threading.Event()

    def request_shutdown(_signum, _frame) -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        append_service_log("App closed; finishing active work before backend shutdown")

        def finish_then_stop() -> None:
            while read_current_operation().get("running") or read_parallel_check_state().get("running"):
                time.sleep(0.25)
            # shutdown() must run outside serve_forever()'s thread.
            server.shutdown()

        threading.Thread(target=finish_then_stop, daemon=True).start()

    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
    try:
        server.serve_forever()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        server.server_close()
        append_service_log("HTTP server stopped")


def run_check_only(notify: bool) -> int:
    ensure_dirs()
    snapshot = compute_snapshot(run_brew_update=True, enrich_details=False)
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
