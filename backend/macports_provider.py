"""MacPorts provider: exact archive preflight; no implicit cross-provider migration."""
from __future__ import annotations
import concurrent.futures
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import time

PORT = '/opt/local/bin/port'
TCL = '/opt/local/libexec/macports/bin/tclsh8.6'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]*\Z')
ROOT = Path(__file__).resolve().parents[1]

def valid(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('Invalid MacPorts package identity')
    return name

def checked(s, args, **kwargs):
    result = s.run_cmd(args, **kwargs)
    if result.code:
        raise RuntimeError(result.stderr or result.stdout or f'Command failed: {args[0]}')
    return result.stdout

def inventory(s):
    if not Path(PORT).is_file():
        raise RuntimeError('MacPorts is not installed at /opt/local/bin/port')
    raw = checked(s, [PORT, '-q', 'installed', 'active'], timeout=90)
    outdated = checked(s, [PORT, '-q', 'outdated'], timeout=90)
    if raw.strip() and not re.search(r'@\S+\s+\(active\)',raw):
        raise RuntimeError('Unrecognized MacPorts installed inventory')
    if outdated.strip() and not re.search(r'\s<\s',outdated):
        raise RuntimeError('Unrecognized MacPorts outdated inventory')
    old = {m[0]: m[1] for m in re.findall(r'^\s*(\S+)\s+\S+\s+<\s+(\S+)', outdated, re.M)}
    packages = []
    for name, version, variants in re.findall(r'^\s*(\S+)\s+@([^\s+]+)((?:\+[^\s]+)?)\s+\(active\)', raw, re.M):
        valid(name)
        packages.append(dict(name=name, kind='port', provider='macports', provider_name=name,
            installation_id=f'macports:{name}', display_name=name, description='MacPorts',
            description_he='', installed_version=version, latest_version=old.get(name, version),
            installed_variants=variants, outdated=name in old, excluded=False,
            route_reason='installed_owner', homepage='', current_release_date=None,
            latest_release_date=None, days_since_outdated=None))
    return packages

def load_probe():
    spec = importlib.util.spec_from_file_location('mac_archive_probe', ROOT / 'tools/check-macports-archives.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def preflight(s, names, installed=None):
    names = [valid(n) for n in names]
    if not names:
        raise ValueError('No verified MacPorts mapping')
    if any(n == 'ngrok' for n in names):
        raise ValueError('ngrok is excluded')
    probe = load_probe()
    with tempfile.TemporaryDirectory(prefix='mac-manager-resolve-') as directory:
        checked(s, [TCL, str(ROOT / 'tools/macports-resolve.tcl'), directory, *names],
                timeout=300, cancellable=True)
        def rows(file):
            with (Path(directory) / file).open() as stream:
                return list(csv.DictReader(stream, delimiter='\t'))
        results = rows('root-results.tsv')
        if len(results) != len(names) or any(r['status'] != '0' for r in results):
            raise RuntimeError('MacPorts dependency resolution failed: ' + json.dumps(results))
        nodes = rows('nodes.tsv')
        if not nodes or not set(names).issubset({r['name'] for r in nodes}):
            raise RuntimeError('Incomplete MacPorts dependency graph')
        by_name = {r['name']: r for r in nodes}
        for row in nodes:
            valid(row['name'])
            if row['name'] == 'ngrok' or s.is_excluded(row['name'], 'port'):
                raise RuntimeError('Excluded MacPorts dependency: ' + row['name'])
            if not re.fullmatch(r'[A-Za-z0-9_.+-]+', row['archive']):
                raise RuntimeError('Invalid archive identity')
            missing = set(filter(None, row['requires'].split(','))) - by_name.keys()
            if missing:
                raise RuntimeError('Incomplete runtime closure: ' + str(missing))
        # The metadata resolver evaluates default variants. Never install a different
        # installed variant set on the strength of those observations.
        for pkg in installed or inventory(s):
            if pkg['name'] in by_name and pkg.get('installed_variants', '') != by_name[pkg['name']]['variants']:
                raise RuntimeError('Installed variants require a separate migration: ' + pkg['name'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            checked_nodes = list(pool.map(probe.check_node, nodes))
        s.raise_if_cancel_requested()
        missing = [r['archive'] for r in checked_nodes if r['archive_status'] != 'available']
        if missing:
            raise RuntimeError('Ready archives unavailable: ' + ', '.join(missing))
        return dict(names=names, nodes=nodes, checked_at=s.now_iso(),
                    signatures_verified=False, archive_count=len(nodes))

def applescript(s, command):
    # Fixed executable and validated argv only. No user-provided shell programs.
    source = 'do shell script ' + json.dumps(command) + ' with administrator privileges'
    return checked(s, ['/usr/bin/osascript', '-e', source], timeout=None)

def privileged(s, action, names=(), activity=None):
    if action not in {'install', 'upgrade', 'sync'}:
        raise ValueError('Unsupported privileged operation')
    names = [valid(n) for n in names]
    if 'ngrok' in names or (action != 'sync' and not names):
        raise ValueError('Excluded or empty privileged operation')
    if s.is_cancel_requested():
        raise s.OperationCancelledError('Canceled before authorization')
    s.set_check_progress(phase='updating_packages', message='Waiting for macOS administrator authorization', eta_seconds=None)
    argv = [PORT, '-N', '-b', action, *names]
    # mktemp is executed AS ROOT. All redirected files live in the resulting
    # root-owned directory; never redirect privileged output into user files.
    worker = ('umask 022; ' + shlex.join(argv) + ' > "$1/output" 2>&1 & child=$!; '
              'echo "$child" > "$1/pid"; wait "$child"; code=$?; '
              'echo "$code" > "$1/status"')
    # Keep the authorized shell alive until port finishes. Detached processes
    # launched by Authorization Services may disappear when that shell exits.
    directory = Path(applescript(s, 'd=$(/usr/bin/mktemp -d /private/tmp/mac-manager.XXXXXXXX); '
                                '/bin/chmod 755 "$d"; echo "$d"').strip())
    if not re.fullmatch(r'/private/tmp/mac-manager\.[A-Za-z0-9]{8}', str(directory)):
        raise RuntimeError('Invalid privileged operation receipt')
    s.append_service_log(f'MacPorts authorized receipt: {directory}')
    s.raise_if_cancel_requested()
    foreground = '/bin/sh -c ' + shlex.quote(worker) + ' worker ' + shlex.quote(str(directory))
    offset, canceled, started = 0, False, False
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
      command = executor.submit(applescript, s, foreground)
      while not command.done():
        if (directory / 'pid').exists() and not started:
            started = True
            s.set_check_progress(message='Refreshing MacPorts catalog' if action=='sync' else 'MacPorts is installing verified ready packages', eta_seconds=None)
        if s.is_cancel_requested() and not canceled and (directory / 'pid').exists() and not (directory / 'status').exists():
            # The receipt directory and PID were created by root, not supplied
            # by the renderer. Kill descendants before the root port process.
            cancel = ('[ -f ' + shlex.quote(str(directory / 'status')) + ' ] && exit 0; '
                      'p=$(/bin/cat ' + shlex.quote(str(directory / 'pid')) + '); '
                      'case "$(/bin/ps -p "$p" -o command=)" in *"/opt/local/bin/port"*) ;; *) exit 0;; esac; '
                      'tree="$p"; frontier="$p"; '
                      'while [ -n "$frontier" ]; do next=""; for parent in $frontier; do '
                      'children=$(/bin/ps -axo pid=,ppid= | /usr/bin/awk -v p="$parent" \'$2==p {print $1}\'); '
                      'next="$next $children"; done; [ -z "$(echo "$next" | /usr/bin/tr -d " ")" ] && break; '
                      'tree="$next $tree"; frontier="$next"; done; /bin/kill -TERM $tree 2>/dev/null || true')
            applescript(s, cancel)
            canceled = True
        output = (directory / 'output').read_text(errors='replace') if (directory / 'output').exists() else ''
        if activity and len(output) > offset:
            latest=output[offset:]
            for marker,stage in [('Fetching archive','downloading'),('Fetching distfiles','downloading'),
                                  ('Verifying','verifying'),('Installing','installing'),
                                  ('Activating','installing'),('Cleaning','finishing')]:
                if marker in latest: activity.state['stage']=stage
            activity.observe(output[offset:], 'macports')
        offset = len(output)
        time.sleep(.25)
      command.result()  # Authorization/launch errors must never become a polling loop.
    if not (directory / 'status').is_file():
        raise RuntimeError(f'MacPorts worker ended without a completion receipt: {directory}')
    output = (directory / 'output').read_text(errors='replace')
    code = int((directory / 'status').read_text().strip())
    s.append_service_log(f'MacPorts {action}: receipt={directory} exit={code}')
    if canceled:
        raise s.OperationCancelledError('MacPorts operation canceled')
    return s.CommandResult(code, output, '' if code == 0 else output)

def plan(s, pkg):
    item = {**pkg, 'provider': 'macports', 'version': pkg['latest_version'], 'ready': False}
    if pkg.get('inventory_stale') or s.is_excluded(pkg['name'], 'port') or pkg['name'] == 'ngrok':
        return {**item, 'reason': 'excluded_or_unverified'}
    try:
        current = inventory(s)
        row = next((p for p in current if p['name'] == pkg['name']), None)
        if not row:
            raise RuntimeError('Package is not installed in MacPorts; use migration approval')
        item.update(version=row['latest_version'], preflight=preflight(s, [pkg['name']], current), ready=True)
    except s.OperationCancelledError:
        raise
    except Exception as error:
        item.update(reason='binary_route_unavailable', detail=str(error))
    return item

def upgrade(s, item):
    activity = s.UpgradeActivity(item['name'])
    activity.state.update(provider='macports', stage='downloading')
    activity.observe('Checking ready archives before MacPorts update', 'macports')
    fresh = plan(s, item)
    if not fresh['ready']:
        return dict(name=item['name'], kind='port', provider='macports', ok=False, code=78,
                    stdout='', stderr=fresh.get('detail', fresh.get('reason', 'Blocked')))
    result = privileged(s, 'upgrade', [item['name']], activity)
    after = next((p for p in inventory(s) if p['name'] == item['name']), None)
    graph = fresh['preflight'].get('nodes', [])
    root = next((n for n in graph if n.get('name') == item['name']), None)
    expected = None if root is None else str(root['version']) + (
        '_' + str(root['revision']) if root.get('revision') not in ('', '0', None) else '')
    exact = (after is not None and root is not None
             and after.get('installed_version') == expected
             and after.get('installed_variants', '') == root.get('variants', ''))
    ok = result.code == 0 and exact
    smoke_error = None
    if ok:
        import provider_ownership
        import migration_profiles
        registry = provider_ownership.load_routes(s.TOOLS_ROOT)
        if registry.get('error'):
            ok = False
            smoke_error = 'Ownership registry could not be verified'
        else:
            owner = next((r for r in registry.get('routes', [])
                          if r.get('target_installation_id') == f"macports:{item['name']}"
                          and r.get('status') == 'committed'), None)
            if owner:
                profile = migration_profiles.get(owner.get('profile_id'))
                if not profile or owner.get('profile_version') != profile.version:
                    ok = False
                    smoke_error = 'Managed profile missing or changed'
                else:
                    try:
                        migration_profiles.smoke(s, profile, root['version'])
                    except Exception as error:
                        ok = False
                        smoke_error = 'Updated package failed usage verification: ' + str(error)
    return dict(name=item['name'], kind='port', provider='macports', ok=ok,
                installed_version=after.get('installed_version') if after else None,
                code=result.code if result.code else (0 if ok else 1), stdout=result.stdout,
                stderr=smoke_error or (result.stderr if result.code else
                       '' if ok else 'Exact binary version or variants verification failed'))
