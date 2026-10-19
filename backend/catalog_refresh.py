"""Automatic catalog freshness; Authorization Services remains foreground only."""
import datetime as dt
import time
from pathlib import Path
import macports_provider as mp

INDEX = Path('/opt/local/var/macports/sources/rsync.macports.org/macports/release/tarballs/ports/PortIndex')
MAX_AGE = 24*3600

def recent(value, ttl=MAX_AGE):
    try:
        stamp = float(value) if isinstance(value,(int,float)) else dt.datetime.fromisoformat(value).timestamp()
        return 0 <= time.time()-stamp < ttl
    except (ValueError,TypeError,OverflowError): return False

def fingerprint():
    try:
        stat=INDEX.stat()
        return dict(path=str(INDEX),mtime_ns=stat.st_mtime_ns,size=stat.st_size)
    except OSError: return None

def mark_success(s):
    record=dict(checked_at=s.now_iso(),source=fingerprint())
    s.atomic_write_json(s.DATA_DIR/'macports-catalog.json',record)
    return record

def inspect(s):
    source=fingerprint()
    record=s.read_json(s.DATA_DIR/'macports-catalog.json') or {}
    checked=record.get('checked_at') if source and record.get('source')==source else None
    # A catalog installed/refreshed outside the app also counts as fresh.
    if not recent(checked) and source and recent(source['mtime_ns']/1e9):
        checked=dt.datetime.fromtimestamp(source['mtime_ns']/1e9,dt.timezone.utc).isoformat()
    fresh=bool(source and recent(checked))
    return dict(fresh=fresh,status='fresh' if fresh else 'stale',checked_at=checked,max_age_hours=24)

def ensure(s, refresh=False, interactive=False):
    state=inspect(s)
    if state['fresh'] or not refresh: return state
    if not Path(mp.PORT).is_file():
        return dict(state,status='not_installed')
    if not interactive:
        return dict(state,status='awaiting_authorization')
    s.raise_if_cancel_requested()
    try:
        result=mp.privileged(s,'sync')
        if result.code: raise RuntimeError(result.stderr or result.stdout or 'Catalog sync failed')
        s.raise_if_cancel_requested()
        mark_success(s)
        state=inspect(s)
        if not state['fresh']: raise RuntimeError('Catalog index unavailable after sync')
        return dict(state,status='refreshed')
    except s.OperationCancelledError: raise
    except Exception as error:
        s.append_service_log('MacPorts catalog refresh failed: '+str(error))
        return dict(state,fresh=False,status='refresh_failed',detail=str(error))
