"""Cross-process lock for mutations sharing a TOOLS_ROOT, not a DATA_DIR."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import threading
import time

_HELD = set()
_GUARD = threading.RLock()


class OperationBusy(RuntimeError):
    pass


class OperationLock:
    def __init__(self, tools_root, operation_id):
        self.path = (Path(tools_root) / "migrations" / "operation.lock").resolve()
        self.operation_id = str(operation_id)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _GUARD:
            if self.path in _HELD:
                raise OperationBusy("Nested or concurrent mutation lock")
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                os.close(fd)
                raise OperationBusy("Another operation holds the package mutation lock") from exc
            _HELD.add(self.path)
            self.fd = fd
            payload = json.dumps({"pid": os.getpid(), "operation_id": self.operation_id,
                                  "acquired_at": time.time()}).encode()
            try:
                os.ftruncate(fd, 0)
                os.write(fd, payload)
                os.fsync(fd)
            except BaseException:
                self.__exit__(None, None, None)
                raise
            return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is None:
            return False
        with _GUARD:
            fd, self.fd = self.fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                _HELD.discard(self.path)
        return False
