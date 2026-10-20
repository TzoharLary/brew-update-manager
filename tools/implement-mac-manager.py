#!/usr/bin/env python3
"""Repeatable Mac Update Manager implementation, verification and local rollout.

All product changes are embedded below. check never changes the checkout.
apply preserves the exact pre-existing dirty files; restore refuses later edits.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RECOVERY = Path(os.environ.get('MAC_MANAGER_RECOVERY', str(Path.home() / '.local/share/brew-update-manager/recovery/implementation-v2')))
FILES = {}

FILES['backend/macports_provider.py'] = r'''"""MacPorts provider: exact archive preflight; no implicit cross-provider migration."""
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
            s.set_check_progress(message='MacPorts is installing verified ready packages', eta_seconds=None)
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
    ok = result.code == 0 and after is not None and not after['outdated']
    return dict(name=item['name'], kind='port', provider='macports', ok=ok,
                code=result.code if result.code else (0 if ok else 1), stdout=result.stdout,
                stderr=result.stderr if ok or result.code else 'Installed version verification failed')
'''

FILES['backend/mac_manager.py'] = r'''"""Combined inventory and explicit one-time migration transactions."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
import shutil
import tempfile
import time
import macports_provider as mp
import update_routes as routes

def mapping():
    return json.loads((Path(__file__).with_name('macports-map.json')).read_text())

def combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):
    previous = s.read_json(s.STATE_FILE) or {}
    status, errors = {}, []
    try:
        state = brew_scan(refresh, track_progress=False, enrich_details=enrich_details)
        packages = state['packages']
        errors.extend(state.get('errors', []))
        status['homebrew'] = {'ok': True, 'checked_at': s.now_iso()}
    except Exception as error:
        errors.append('Homebrew: ' + str(error))
        packages = [dict(p, inventory_stale=True, update_block_reason='inventory_unavailable')
                    for p in previous.get('packages', []) if p.get('provider') != 'macports']
        status['homebrew'] = {'ok': False, 'error': str(error),
            'checked_at': previous.get('providers', {}).get('homebrew', {}).get('checked_at')}
    # Existing independently managed tools and their Brew copies are separate
    # installations. Keep legacy formula requests tied to the Brew copy.
    extras=[]
    for pkg in packages:
        if pkg.get('provider','homebrew') not in {'homebrew','macports'}:
            if pkg['kind'] != 'tool' and pkg.get('brew_installation_present') is not False and pkg.get('brew_installed_version'):
                copy=dict(pkg,provider='homebrew',installed_version=pkg['brew_installed_version'],
                          outdated=routes.version_key(pkg['brew_installed_version']) < routes.version_key(pkg['latest_version']))
                extras.append(copy)
            pkg['kind']='tool'
    packages.extend(extras)
    # Even without Brew, retain registered tools, but block updates until their
    # target metadata can be verified. Never call an unknown target current.
    present={p['name'] for p in packages if p.get('kind')=='tool'}
    for name,record in routes.read_records(s.TOOLS_ROOT).items():
        if name not in present:
            packages.append(dict(name=name,kind='tool',provider=record['provider'],
                installed_version=record['version'],latest_version='unknown',outdated=False,
                inventory_stale=True,update_block_reason='inventory_unavailable',description='Managed tool'))
    try:
        ports = mp.inventory(s)
        packages.extend(ports)
        status['macports'] = {'ok': True, 'checked_at': s.now_iso(),
                             'catalog_note': 'Local catalog; refresh MacPorts catalog explicitly'}
    except Exception as error:
        errors.append('MacPorts: ' + str(error))
        packages.extend(dict(p, inventory_stale=True, update_block_reason='inventory_unavailable')
                        for p in previous.get('packages', []) if p.get('provider') == 'macports')
        status['macports'] = {'ok': False, 'error': str(error),
            'checked_at': previous.get('providers', {}).get('macports', {}).get('checked_at')}
    for pkg in packages:
        provider = pkg.get('provider', 'homebrew')
        pkg.update(provider=provider, provider_name=pkg['name'], installation_id=f"{provider}:{pkg['kind']}:{pkg['name']}")
        if provider == 'macports':
            pkg['excluded'] = pkg['name'] == 'ngrok' or s.is_excluded(pkg['name'], 'port')
            if pkg['excluded']: pkg['outdated'] = False
    links = mapping()
    for pkg in packages:
        if pkg['provider'] != 'macports':
            pkg['parallel_installations'] = [p['installation_id'] for p in packages
                if p['provider'] == 'macports' and p['name'] in links.get(pkg['name'], [])]
    offers = []
    try:
        from update_engine import Engine
        engine = Engine(s)
        catalog, installed = engine.catalog(), engine.installed()
        for p in packages:
            if p['provider'] == 'homebrew' and p['kind'] == 'formula' and p.get('outdated') and not p.get('excluded'):
                blockers = routes.bottle_blockers(p['name'], catalog, installed, s.load_settings()['excluded_packages'])
                if blockers:
                    offers.append(dict(name=p['name'], ports=links.get(p['name'], []),
                        status='requires_live_check' if links.get(p['name']) else 'mapping_unverified',
                        executable=p['name']=='terminal-notifier', source_blockers=blockers))
    except Exception as error:
        errors.append('Migration recommendations: ' + str(error))
    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')
    state = dict(app=s.APP_NAME, updated_at=s.now_iso(), packages=packages,
                 counts=s.summarize_counts(packages), errors=errors, providers=status,
                 partial=not all(p['ok'] for p in status.values()), migrations=offers,
                 migration_journal=journal)
    s.atomic_write_json(s.STATE_FILE, state)
    if track_progress:
        s.set_check_progress(running=False, phase='completed', message='Package inventories checked',
                             completed_at=s.now_iso(), eta_seconds=0)
    return state

def migration_plan(s, name):
    if name == 'ngrok' or s.is_excluded(name, 'formula'):
        raise ValueError('Package is excluded')
    roots = mapping().get(name, [])
    if not roots:
        raise ValueError('No verified package mapping')
    current = s.fetch_installed_inventory(s.fetch_outdated_index())
    pkg = next((p for p in current if p['name'] == name and p['kind'] == 'formula'), None)
    if not pkg or pkg.get('brew_installation_present') is False:
        raise ValueError('The Homebrew installation is not present')
    from update_engine import Engine
    engine = Engine(s)
    blockers = routes.bottle_blockers(name, engine.catalog(), engine.installed(), s.load_settings()['excluded_packages'])
    if not blockers:
        raise ValueError('Homebrew has a binary route; migration is unnecessary')
    graph = mp.preflight(s, roots)
    target = next(n['version'] for n in graph['nodes'] if n['name'] == roots[0])
    probe = mp.load_probe()
    # Cross-manager comparison is strictly upstream only, never revisions.
    need, have = probe.version(pkg['latest_version']), probe.version(target)
    if need is None or have is None or have < need:
        raise ValueError('MacPorts upstream version is older or cannot be compared safely')
    consumers = mp.checked(s, [s.brew_bin(), 'uses', '--installed', name], timeout=90).splitlines()
    plan = dict(name=name, roots=roots, installed_version=pkg['installed_version'], target=target,
        graph=graph, consumers=consumers, executable=name=='terminal-notifier',
        paths=['/opt/local', '/usr/local'], remove_brew=not consumers,
        requires='Notification integration test' if name=='terminal-notifier' else 'Consumer/project/data migration plan',
        created_at=time.time())
    plan['id'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    s.atomic_write_json(s.DATA_DIR / 'migration-plan.json', plan)
    return plan

def approve(s, plan_id):
    old = s.read_json(s.DATA_DIR / 'migration-plan.json') or {}
    if old.get('id') != plan_id or time.time()-old.get('created_at', 0)>900:
        raise ValueError('Migration plan expired; request a fresh plan')
    if not old.get('executable') or old.get('name') != 'terminal-notifier':
        raise ValueError('This package requires its own consumer migration plan')
    s.raise_if_cancel_requested()
    fresh = migration_plan(s, old['name'])
    for key in ('installed_version', 'target', 'consumers', 'remove_brew'):
        if old[key] != fresh[key]:
            raise ValueError('Migration changed; review a new plan')
    if old['graph']['nodes'] != fresh['graph']['nodes']:
        raise ValueError('Dependency graph changed; review a new plan')
    journal = dict(plan=fresh, status='running', stage='backup', started_at=s.now_iso())
    dest = s.DATA_DIR / 'migration-journal.json'
    def stage(value):
        journal['stage']=value
        s.atomic_write_json(dest, journal)
    stage('backup')
    try:
        # Preserve the whole original keg, its symlinks and metadata before uninstall.
        prefix = Path(mp.checked(s, [s.brew_bin(), '--prefix', 'terminal-notifier'], timeout=30).strip()).resolve()
        cellar = Path(mp.checked(s, [s.brew_bin(), '--cellar'], timeout=30).strip()).resolve()
        if not prefix.is_relative_to(cellar / 'terminal-notifier') or not prefix.is_dir():
            raise RuntimeError('Unexpected terminal-notifier keg path')
        backup = s.TOOLS_ROOT / 'recovery' / ('notifier-' + str(time.time_ns()))
        backup.mkdir(parents=True)
        shutil.copytree(prefix, backup / prefix.name, symlinks=True)
        for path in prefix.rglob('*'):
            relative = path.relative_to(prefix)
            copy = backup / prefix.name / relative
            if path.is_symlink():
                if path.readlink() != copy.readlink(): raise RuntimeError('Backup link mismatch')
            elif path.is_file() and hashlib.sha256(path.read_bytes()).digest()!=hashlib.sha256(copy.read_bytes()).digest():
                raise RuntimeError('Backup content mismatch')
        journal['backup']=str(backup)
        stage('install')
        activity = s.UpgradeActivity('terminal-notifier')
        activity.state['provider']='macports'
        current = next((p for p in mp.inventory(s) if p['name']=='terminal-notifier'), None)
        root = next(n for n in fresh['graph']['nodes'] if n['name']=='terminal-notifier')
        expected = root['version'] + '_' + root['revision']
        if not current or current['installed_version'] != expected or current['outdated']:
            result = mp.privileged(s, 'install', ['terminal-notifier'], activity)
            if result.code: raise RuntimeError(result.stderr)
        else:
            journal['installation_reused'] = True
        s.raise_if_cancel_requested()
        stage('verify')
        installed = next((p for p in mp.inventory(s) if p['name']=='terminal-notifier'), None)
        if not installed or installed['outdated']:
            raise RuntimeError('MacPorts installation verification failed')
        binary = notifier_path()
        if not binary: raise RuntimeError('MacPorts notification executable is absent')
        version = mp.checked(s, [str(binary), '-version'], timeout=30)
        if not re.search(r'(?<![0-9])'+re.escape(fresh['target'])+r'(?![0-9])',version):
            raise RuntimeError('Notifier version mismatch')
        # The CLI package and the application's notification delivery identity
        # are separate. Preserve the existing app helper and its user permission.
        # Running MacPorts' default bundle ID would change that identity.
        helper = app_notification_path()
        s.send_notification({'packages': [], 'notification_test': True}, notifier_override=str(helper))
        stage('activate')
        s.atomic_write_json(s.DATA_DIR / 'notifier-route.json', {'provider':'application_helper',
            'path':str(helper), 'cli_provider':'macports', 'cli_path':str(binary)})
        s.raise_if_cancel_requested()
        stage('cleanup')
        consumers = mp.checked(s, [s.brew_bin(), 'uses', '--installed', 'terminal-notifier'], timeout=60).splitlines()
        if not consumers:
            journal['brew_uninstall_output'] = mp.checked(s, [s.brew_bin(), 'uninstall', '--formula', 'terminal-notifier'],
                timeout=120, env_override={'HOMEBREW_NO_AUTOREMOVE':'1',
                    'HOMEBREW_NO_INSTALL_CLEANUP':'1', 'HOMEBREW_NO_AUTO_UPDATE':'1'})
        journal.update(status='completed', completed_at=s.now_iso(), preserved_for_consumers=consumers)
        stage('completed')
        return journal
    except Exception as error:
        journal.update(status='needs_attention', error=str(error), completed_at=s.now_iso())
        s.atomic_write_json(dest, journal)
        raise

def app_notification_path():
    helper = Path.home() / 'Applications/Homebrew Update Notifications.app/Contents/MacOS/terminal-notifier'
    if not helper.is_file():
        raise RuntimeError('The application notification helper is absent; configure its notification identity before migrating')
    return helper

def notifier_path():
    candidates = [Path('/opt/local/bin/terminal-notifier'),
        Path('/opt/local/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier'),
        Path('/Applications/MacPorts/terminal-notifier.app/Contents/MacOS/terminal-notifier')]
    return next((p for p in candidates if p.is_file()), None)
'''

FILES['renderer/mac-manager.js'] = r'''// Provider status and migration review are independent of normal update buttons.
function renderMacManager() {
  let panel = document.getElementById('macManagerPanel');
  if (!panel) {
    panel = document.createElement('details'); panel.id = 'macManagerPanel'; panel.className = 'panel';
    panel.style.padding = '16px'; panel.style.marginBlock = '16px';
    document.querySelector('.tabs').before(panel);
  }
  panel.replaceChildren();
  const he = i18n.lang === 'he';
  const title = document.createElement('summary');
  title.textContent = (he ? 'מנהלי חבילות והצעות מעבר' : 'Package managers and migration proposals') +
    ` (${snapshot?.migrations?.length || 0}) · ` + Object.entries(snapshot?.providers || {}).map(([name,p]) => `${name} ${p.ok ? '✓' : '⚠'}`).join(' · ');
  panel.appendChild(title);
  const status = document.createElement('p');
  status.textContent = Object.entries(snapshot?.providers || {}).map(([name, p]) =>
    `${name}: ${p.ok ? (he ? 'נבדק' : 'checked') : (he ? 'סריקה נכשלה' : 'scan failed')} · ${p.checked_at || '—'}`).join(' | ');
  panel.appendChild(status);
  const sync = document.createElement('button');
  sync.textContent = he ? 'רענון קטלוג MacPorts (דורש הרשאה)' : 'Refresh MacPorts catalog (authorization required)';
  sync.disabled = !!busyLoading || !!lastKnownOperationRunning;
  sync.onclick = async () => {
    sync.disabled = true; setLoading(true, 'message.loadingState'); startProgressPolling();
    try { await window.brewApp.macportsSync(); showMessage(he ? 'הקטלוג רוענן; ניתן לסרוק שוב' : 'Catalog refreshed; scan again'); }
    catch (e) { showMessage(e.message, true); }
    finally { stopProgressPolling(); setLoading(false, 'message.loadingState'); sync.disabled = false; }
  };
  panel.appendChild(sync);
  if (snapshot?.migration_journal?.status === 'needs_attention' || snapshot?.migration_journal?.status === 'running') {
    const warning = document.createElement('p');
    warning.textContent = (he ? 'מעבר דורש בדיקה: ' : 'Migration needs review: ') + JSON.stringify(snapshot.migration_journal);
    panel.appendChild(warning);
  }
  for (const offer of snapshot?.migrations || []) {
    const row = document.createElement('p');
    row.textContent = `${offer.name} → ${offer.ports.join(', ') || (he ? 'אין מיפוי מאומת' : 'no verified mapping')} `;
    const btn = document.createElement('button');
    btn.textContent = he ? 'בדיקת חבילה מוכנה ותוכנית מעבר' : 'Check ready packages and migration plan';
    btn.disabled = !offer.ports.length || !!busyLoading || !!lastKnownOperationRunning;
    btn.onclick = async () => {
      btn.disabled = true; startProgressPolling();
      try {
        const { plan } = await window.brewApp.migrationPlan(offer.name);
        const detail = document.createElement('pre'); detail.style.whiteSpace = 'pre-wrap';
        detail.textContent = `${he ? 'גרסה' : 'Version'}: ${plan.installed_version} → ${plan.target}\n` +
          `${he ? 'חבילות' : 'Packages'}: ${plan.graph.nodes.map(n => `${n.name}@${n.version}_${n.revision}${n.variants}`).join(', ')}\n` +
          `${he ? 'צרכני Homebrew' : 'Homebrew consumers'}: ${plan.consumers.join(', ') || '—'}\n` +
          `${he ? 'נתיבים' : 'Paths'}: ${plan.paths.join(', ')}\n${plan.requires}\n` +
          (he ? 'זמינות נבדקה כעת; החתימות יאומתו בהתקנה.' : 'Availability checked now; signatures are verified during installation.');
        row.appendChild(detail);
        if (plan.executable) {
          const approve = document.createElement('button');
          approve.textContent = he ? 'אישור מעבר חד־פעמי' : 'Approve this migration once';
          approve.onclick = async () => {
            if (!confirm(he ? 'להתקין דרך MacPorts, לבדוק התראה ולהסיר את עותק Homebrew אם אין לו תלויים?' : 'Install with MacPorts, test a notification, then remove Homebrew copy if unused?')) return;
            approve.disabled = true; setLoading(true, 'message.loadingState'); startProgressPolling();
            try {
              await window.brewApp.migrationApprove(plan.id);
            const response = await window.brewApp.getState(); snapshot = response.snapshot || response;
              renderCards(snapshot); renderTable();
            } catch (e) { showMessage(e.message, true); }
            finally { stopProgressPolling(); setLoading(false, 'message.loadingState'); }
          };
          row.appendChild(approve);
        }
      } catch (e) { showMessage(e.message, true); }
      finally { stopProgressPolling(); btn.disabled = false; }
    };
    row.appendChild(btn); panel.appendChild(row);
  }
}
'''

FILES['tests/test_mac_manager.py'] = r'''import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import macports_provider as mp
import mac_manager as mm
import update_routes as routes
from test_update_policy import service

class MacTests(unittest.TestCase):
    def test_inventory_identity_revision_and_variants(self):
        responses = [service.CommandResult(0, '  wget @1.0_2+ssl (active)\n  foo @2.0_0 (active)', ''),
                     service.CommandResult(0, 'wget 1.0_2 < 1.0_3', '')]
        with patch.object(service, 'run_cmd', side_effect=responses), patch.object(Path, 'is_file', return_value=True):
            rows=mp.inventory(service)
        self.assertEqual(rows[0]['kind'], 'port')
        self.assertEqual(rows[0]['installed_variants'], '+ssl')
        self.assertEqual(rows[0]['latest_version'], '1.0_3')
        self.assertTrue(rows[0]['outdated'])
        self.assertFalse(rows[1]['outdated'])

    def test_privileged_validation_precedes_authorization(self):
        with patch.object(mp, 'applescript', side_effect=AssertionError('Unauthorized command reached admin')):
            for action, names in [('uninstall',['wget']), ('upgrade',['ngrok']), ('install',['x;whoami']), ('install',[])]:
                with self.assertRaises(ValueError): mp.privileged(service,action,names)

    def test_exclusion_and_absent_mapping(self):
        with patch.object(service, 'is_excluded', return_value=False):
            with self.assertRaises(ValueError): mm.migration_plan(service,'ngrok')
            with self.assertRaises(ValueError): mm.migration_plan(service,'unmapped')

    def test_selection_accepts_explicit_port_and_preserves_old_requests(self):
        self.assertEqual(service.normalize_selected_packages([{'kind':'port','name':'wget'}])[0]['kind'],'port')
        self.assertEqual(service.normalize_selected_packages([{'kind':'formula','name':'wget'}])[0]['kind'],'formula')

    def test_failed_brew_scan_preserves_macports(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service,'read_json',return_value={}), \
             patch.object(service,'STATE_FILE',Path(tmp)/'state.json'), patch.object(service,'DATA_DIR',Path(tmp)), \
             patch.object(mp,'inventory',return_value=[dict(name='wget',kind='port',provider='macports',outdated=False)]), \
             patch.object(service,'is_excluded',return_value=False),patch.object(routes,'read_records',return_value={}):
            state=mm.combined(service,lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Brew missing')),False,False,False)
        self.assertTrue(state['partial'])
        self.assertEqual(state['packages'][0]['provider'],'macports')

    def test_expired_and_complex_migration_cannot_execute(self):
        with patch.object(service,'read_json',return_value={'id':'old','created_at':0}):
            with self.assertRaises(ValueError): mm.approve(service,'old')
        with patch.object(service,'read_json',return_value={'id':'x','created_at':__import__('time').time(),'executable':False}):
            with self.assertRaises(ValueError): mm.approve(service,'x')

    def test_signature_or_install_failure_is_not_success(self):
        with patch.object(mp,'plan',return_value={'ready':True}), \
             patch.object(mp,'privileged',return_value=service.CommandResult(1,'bad signature','bad signature')), \
             patch.object(mp,'inventory',return_value=[]):
            result=mp.upgrade(service,{'name':'wget','latest_version':'1'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'],1)

    def test_binary_preflight_blocks_missing_dependencies_and_variants(self):
        import csv
        def resolve(args,**kw):
            out=Path(args[2])
            (out/'root-results.tsv').write_text('root\tstatus\terror\nwget\t0\t\n')
            (out/'nodes.tsv').write_text('name\tversion\trevision\tvariants\tarch\tarchive\trequires\tporturl\n'
                'wget\t1\t0\t\tx86_64\twget-1_0.darwin_25.x86_64.tbz2\t\tfile:///fixture\n')
            return service.CommandResult(0,'','')
        fake=type('Probe',(),{'check_node':staticmethod(lambda n:{**n,'archive_status':'missing'})})
        with patch.object(service,'run_cmd',side_effect=resolve),patch.object(mp,'load_probe',return_value=fake), \
             patch.object(service,'is_excluded',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'Ready archives'):
                mp.preflight(service,['wget'],[{'name':'wget','installed_variants':''}])
            with self.assertRaisesRegex(RuntimeError,'variants'):
                mp.preflight(service,['wget'],[{'name':'wget','installed_variants':'+ssl'}])

    def test_both_copies_remain_separate(self):
        pkg=dict(name='node',kind='formula',provider='native',installed_version='26.12',
            latest_version='26.12',brew_installed_version='26.0',outdated=False)
        with tempfile.TemporaryDirectory() as tmp,patch.object(service,'read_json',return_value={}), \
             patch.object(service,'STATE_FILE',Path(tmp)/'state.json'),patch.object(service,'DATA_DIR',Path(tmp)), \
             patch.object(mp,'inventory',return_value=[]),patch.object(routes,'read_records',return_value={}), \
             patch.object(service,'is_excluded',return_value=False):
            state=mm.combined(service,lambda *a,**k:{'packages':[pkg]},False,False,False)
        rows=state['packages']
        self.assertEqual({p['kind'] for p in rows},{'formula','tool'})
        self.assertEqual(len({p['installation_id'] for p in rows}),2)

    def test_cached_policy_keeps_macports_and_tool_ownership(self):
        pkgs=[dict(name='wget',kind='port',provider='macports',outdated=True,installed_version='1',latest_version='2',installation_id='macports:port:wget'),
              dict(name='node',kind='tool',provider='native',outdated=False,installed_version='26',latest_version='26',installation_id='native:tool:node'),
              dict(name='node',kind='formula',provider='homebrew',outdated=True,installed_version='25',latest_version='26',installation_id='homebrew:formula:node')]
        with patch.object(routes,'read_records',return_value={'node':{'version':'26','provider':'native','verified_at':1}}), \
             patch.object(service,'load_settings',return_value={'excluded_packages':[]}):
            service.apply_update_policy(pkgs)
        self.assertEqual([p['provider'] for p in pkgs],['macports','native','homebrew'])
        self.assertEqual(pkgs[2]['installed_version'],'25')

    def test_admin_prompt_rejection_is_propagated(self):
        with patch.object(service,'run_cmd',return_value=service.CommandResult(1,'','User canceled (-128)')):
            with self.assertRaisesRegex(RuntimeError,'canceled'): mp.applescript(service,'fixed command')

    def test_script_replay_and_restore_preserve_later_edits(self):
        spec=importlib.util.spec_from_file_location('implementation',Path(__file__).resolve().parents[1]/'tools/implement-mac-manager.py')
        impl=importlib.util.module_from_spec(spec);spec.loader.exec_module(impl)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'repo';root.mkdir();(root/'dirty.txt').write_text('original dirty content')
            impl.ROOT=root;impl.RECOVERY=Path(tmp)/'recovery'
            with patch.object(impl,'transforms',return_value={'dirty.txt':'new content','created.txt':'new file'}):
                impl.check_or_apply(True);impl.check_or_apply(True)
                (root/'dirty.txt').write_text('later edit')
                with self.assertRaises(RuntimeError): impl.check_or_apply(True)
                with self.assertRaises(RuntimeError): impl.restore()
                self.assertEqual((root/'dirty.txt').read_text(),'later edit')
                (root/'dirty.txt').write_text('new content');impl.restore()
                self.assertEqual((root/'dirty.txt').read_text(),'original dirty content')
                self.assertFalse((root/'created.txt').exists())

    def test_migration_http_plan_and_origin_guard(self):
        import threading
        import urllib.request
        import urllib.error
        server=service.ThreadingHTTPServer(('127.0.0.1',0),service.BrewUpdatesHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_address[1]}/api/migrations/plan'
        try:
            with patch.object(mm,'migration_plan',return_value={'id':'review-only'}), \
                 patch.object(service,'append_service_log'):
                request=urllib.request.Request(url,data=b'{"name":"terminal-notifier"}',headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request) as response: result=json.load(response)
                self.assertEqual(result['plan']['id'],'review-only')
                request=urllib.request.Request(url,data=b'{}',headers={'Content-Type':'application/json','Origin':'https://example.com'})
                with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code,403)
                caught.exception.close()
        finally: server.shutdown();server.server_close();thread.join()

    def test_cancel_before_admin_prompt(self):
        with patch.object(service,'is_cancel_requested',return_value=True), \
             patch.object(mp,'applescript',side_effect=AssertionError('Prompt opened after cancel')):
            with self.assertRaises(service.OperationCancelledError): mp.privileged(service,'upgrade',['wget'])

    def test_privileged_worker_cannot_silently_disappear(self):
        import shutil, subprocess
        directory=Path(subprocess.check_output(['/usr/bin/mktemp','-d','/private/tmp/mac-manager.XXXXXXXX'],text=True).strip())
        try:
            with patch.object(mp,'applescript',side_effect=[str(directory),'']), \
                 patch.object(service,'is_cancel_requested',return_value=False):
                with self.assertRaisesRegex(RuntimeError,'without a completion receipt'):
                    mp.privileged(service,'install',['terminal-notifier'])
        finally: shutil.rmtree(directory)

    def test_privileged_shell_waits_and_returns_real_exit(self):
        import shutil, subprocess
        directory=Path(subprocess.check_output(['/usr/bin/mktemp','-d','/private/tmp/mac-manager.XXXXXXXX'],text=True).strip())
        calls=[]
        def authorized(s,command):
            calls.append(command)
            if len(calls)==1: return str(directory)
            self.assertNotIn('nohup',command)
            self.assertIn('wait',command)
            (directory/'output').write_text('signature verification failed')
            (directory/'status').write_text('1')
            return ''
        try:
            with patch.object(mp,'applescript',side_effect=authorized), \
                 patch.object(service,'is_cancel_requested',return_value=False):
                result=mp.privileged(service,'install',['terminal-notifier'])
            self.assertEqual(result.code,1)
            self.assertIn('signature',result.stderr)
        finally: shutil.rmtree(directory)

    def test_resume_reuses_installed_port_and_preserves_app_notification_identity(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=root/'data';data.mkdir()
            cellar=root/'Cellar';keg=cellar/'terminal-notifier/2.0';keg.mkdir(parents=True)
            (keg/'receipt').write_text('original')
            plan={'id':'review','created_at':time.time(),'executable':True,'name':'terminal-notifier',
                'installed_version':'2.0','target':'3.1.0','consumers':[],'remove_brew':True,
                'graph':{'nodes':[{'name':'terminal-notifier','version':'3.1.0','revision':'0'}]}}
            helper=root/'existing-app-helper';binary=root/'macports-cli'
            def command(s,args,**kw):
                if '--prefix' in args: return str(keg)
                if '--cellar' in args: return str(cellar)
                if '-version' in args: return 'terminal-notifier 3.1.0.'
                if 'uninstall' in args:
                    self.assertEqual(kw['env_override']['HOMEBREW_NO_AUTOREMOVE'],'1')
                return ''
            with patch.object(service,'read_json',return_value=plan),patch.object(service,'DATA_DIR',data), \
                 patch.object(service,'TOOLS_ROOT',root/'tools'),patch.object(service,'brew_bin',return_value='/fixture/brew'), \
                 patch.object(service,'is_cancel_requested',return_value=False),patch.object(mm,'migration_plan',return_value=plan), \
                 patch.object(mp,'checked',side_effect=command),patch.object(mm,'notifier_path',return_value=binary), \
                 patch.object(mm,'app_notification_path',return_value=helper), \
                 patch.object(mp,'inventory',return_value=[{'name':'terminal-notifier','installed_version':'3.1.0_0','outdated':False}]), \
                 patch.object(mp,'privileged',side_effect=AssertionError('An already installed port was reinstalled')), \
                 patch.object(service,'send_notification') as notify:
                result=mm.approve(service,'review')
            self.assertEqual(result['status'],'completed')
            self.assertTrue(result['installation_reused'])
            self.assertEqual(notify.call_args.kwargs['notifier_override'],str(helper))
            route=json.loads((data/'notifier-route.json').read_text())
            self.assertEqual(route['provider'],'application_helper')
            self.assertEqual(route['cli_provider'],'macports')

if __name__=='__main__': unittest.main()
'''

FILES['docs/MAC_MANAGER.md'] = '''# Mac Update Manager

Homebrew formulae/casks, MacPorts active ports and existing isolated tool installs
share one inventory. `kind=port` unambiguously identifies MacPorts in the existing
selection API; legacy formula/cask requests remain Homebrew identities.

MacPorts executes only binary installs/upgrades. Every action resolves exact
runtime archives on the current OS/architecture and checks archive plus signature
availability. MacPorts verifies downloaded signatures. Missing archives, unknown
variants and uncertain metadata block the action. A scan uses the local catalog;
the explicit MacPorts refresh button requests macOS admin authorization to sync.

POST /api/migrations/plan {name} returns a 15-minute, exact-graph approval plan.
POST /api/migrations/approve {id} revalidates and executes terminal-notifier only.
Other mapped tools require project/consumer migration; update-all never migrates.
POST /api/macports/sync refreshes the catalog with administrator authorization.
All operations share the existing exclusive-operation lock and cancellation state.

The application keeps its existing dedicated notification helper and notification
permission identity. MacPorts owns the terminal-notifier CLI; its default bundle
ID is not substituted for the application's already authorized delivery helper.
Retrying a failed verification reuses an exact, already installed MacPorts port.

The app ID, legacy data/profile paths, updater repository, notification group and
launch-agent identity remain stable. The visible product name is Mac Update Manager.
ngrok stays excluded. User settings, data and unmanaged paths are preserved.

Run tools/implement-mac-manager.py check|apply|verify|pack|install|pilot|restore.
All source changes are embedded in that script. Its recovery manifest preserves
the exact previous dirty files, and restore refuses files edited after application.
The install and pilot stages require successful verification in the same process.
No system package or service is migrated by apply/verify/pack.

Root operations create receipts and logs in root-owned /private/tmp/mac-manager.*
directories. A canceled admin prompt fails without installation. Cancellation of
an active root port command uses a separate macOS authorization to stop its tree.
Incomplete migration journals remain visible for review; they never auto-resume.
'''

def digest(data):
    return hashlib.sha256(data).hexdigest()

def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise RuntimeError(f'Precondition mismatch ({text.count(old)} != {count}): {old[:90]!r}')
    return text.replace(old, new)

def transforms():
    changes = dict(FILES)
    changes['backend/macports-map.json'] = (ROOT / 'docs/macports-binary-coverage-2026-10-19/root-map.json').read_text()
    changes['tools/macports-resolve.tcl'] = (ROOT / 'docs/macports-binary-coverage-2026-10-19/probe-research/resolve.tcl').read_text()
    # Fresh, private scratch path is removed after mportshutdown closes workers.
    changes['tools/macports-resolve.tcl'] = changes['tools/macports-resolve.tcl'].replace(
        'set scratch [file normalize [file join /tmp macports-metadata-[pid]]]',
        'set scratch [file normalize [file join $output_dir scratch]]') + '\nmportshutdown\nfile delete -force $scratch\n'
    p='backend/brew-updates-service.py'; t=source(p)
    t=replace(t,'import update_routes as routes','import update_routes as routes\nimport macports_provider\nimport mac_manager')
    t=replace(t,'APP_NAME = "Homebrew Update Manager"','APP_NAME = "Mac Update Manager"')
    t=replace(t,'(?:formula|cask):','(?:formula|cask|port):')
    t=replace(t,'        pkg["provider"] = "homebrew"', '        if kind not in {"port", "tool"}:\n            pkg["provider"] = "homebrew"')
    t=replace(t,'        record = records.get(name) if kind == "formula" else None', '        record = records.get(name) if kind == "formula" and not pkg.get("installation_id") else None')
    t=replace(t,'        excluded = f"{kind}:{name}" in settings["excluded_packages"]', '        excluded = f"{\'formula\' if kind == \'tool\' else kind}:{name}" in settings["excluded_packages"] or (kind == "port" and name == "ngrok")')
    t=replace(t,'            if not state:\n                try:', '            if not state or state.get("app") != APP_NAME or "providers" not in state:\n                try:')
    t=replace(t,'lambda: compute_snapshot(run_brew_update=False))','lambda: compute_snapshot(run_brew_update=False, enrich_details=False))')
    t=replace(t,'                pkg.pop("update_block_reason", None)', '                if pkg.get("inventory_stale"):\n                    pkg["update_block_reason"] = "inventory_unavailable"\n                    continue\n                pkg.pop("update_block_reason", None)')
    t=replace(t,'        "formulae": len(formulas),','        "formulae": len(formulas),\n        "ports": sum(p.get("kind") == "port" for p in packages),\n        "tools": sum(p.get("kind") == "tool" for p in packages),')
    t=replace(t,'def compute_snapshot(run_brew_update:', 'def compute_homebrew_snapshot(run_brew_update:')
    marker='def summarize_counts(packages:'
    t=replace(t,marker,'''def compute_snapshot(run_brew_update, track_progress=False, *, enrich_details=True):
    return mac_manager.combined(sys.modules[__name__], compute_homebrew_snapshot,
                                run_brew_update, track_progress, enrich_details)


'''+marker)
    t=replace(t,'if kind not in {"formula", "cask"}:','if kind not in {"formula", "cask", "port", "tool"}:')
    t=replace(t,'        atomic_write_json(STATE_FILE, snapshot)','        # The combined inventory wrapper persists a complete snapshot.')
    t=replace(t,'        return refreshed, None','        return refreshed, ("Partial provider inventory" if refreshed.get("partial") else None)')
    t=replace(t,'if routes.route_for(name, kind, settings) != "homebrew":', 'if routes.route_for(name, kind, settings) != "homebrew" and name not in routes.read_records(TOOLS_ROOT):')
    t=replace(t,'    settings = load_settings()\n    from update_engine import Engine\n    engine = Engine(sys.modules[__name__])', '''    if kind == "tool":
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
    engine = Engine(sys.modules[__name__])''')
    t=replace(t,'def send_notification(snapshot: dict[str, Any]) -> None:', 'def send_notification(snapshot: dict[str, Any], notifier_override=None) -> None:')
    t=replace(t,'        title = "Homebrew"','        title = "Mac Update Manager"')
    t=replace(t,'        title = "Homebrew Updates Available"','        title = "Mac Updates Available"')
    t=replace(t,'"-subtitle", "Homebrew Update Check"','"-subtitle", "Mac Update Check"')
    t=replace(t,'    helper = Path.home() / "Applications" / "Homebrew Update Notifications.app"', '''    if snapshot.get("notification_test"):
        title = "Mac Update Manager"
        message = "Notification test: the MacPorts migration was verified"
    helper = Path.home() / "Applications" / "Homebrew Update Notifications.app"''')
    t=replace(t,'    notifier = str(helper) if helper.is_file() else shutil.which("terminal-notifier")', '''    route = read_json(DATA_DIR / 'notifier-route.json') or {}
    selected = route.get('path') if route.get('provider') in {'macports', 'application_helper'} else None
    notifier = notifier_override or selected or (str(helper) if helper.is_file() else shutil.which("terminal-notifier"))''')
    t=replace(t,'    def do_GET(self):', '''    def _mac_operation(self):
        if self.path not in {"/api/migrations/plan", "/api/migrations/approve", "/api/macports/sync"}:
            return False
        body = self._read_json_body()
        def operation():
            reset_check_progress_for_run()
            set_check_progress(operation_name="migration", phase="preflight", running=True,
                               message="Checking MacPorts ready packages", eta_seconds=None)
            try:
                if self.path == "/api/migrations/plan":
                    return {"ok": True, "plan": mac_manager.migration_plan(sys.modules[__name__], body.get("name"))}
                if self.path == "/api/migrations/approve":
                    result = mac_manager.approve(sys.modules[__name__], body.get("id"))
                    return {"ok": True, "migration": result, "snapshot": compute_snapshot(False, enrich_details=False)}
                result = macports_provider.privileged(sys.modules[__name__], "sync")
                if result.code: raise RuntimeError(result.stderr)
                return {"ok": True}
            finally:
                with CHECK_PROGRESS_LOCK:
                    CHECK_PROGRESS["package_activity"] = None
                set_check_progress(running=False, phase="completed" if not sys.exc_info()[0] else "error",
                                   completed_at=now_iso(), eta_seconds=0)
        try:
            self._send_json(200, run_exclusive("migration", operation))
        except BusyError as exc:
            self._send_json(409, {"ok": False, "detail": str(exc)})
        except Exception as exc:
            self._send_json(400, {"ok": False, "detail": str(exc)})
        return True

    def do_GET(self):''')
    t=replace(t,'    def do_POST(self):','    def do_POST(self):\n        if self._mac_operation():\n            return')
    t=replace(t,'        body = self._read_json_body()\n        def operation():', '''        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or self.headers.get('Origin'):
            self._send_json(403, {'ok': False, 'detail': 'Desktop JSON requests only'})
            return True
        body = self._read_json_body()
        def operation():''')
    changes[p]=t
    p='backend/update_engine.py';t=source(p)
    t=replace(t,'import update_routes as routes','import update_routes as routes\nimport macports_provider')
    t=replace(t,'self.brew_env = {"HOMEBREW_NO_AUTO_UPDATE": "1",','self.brew_env = {"HOMEBREW_NO_AUTOREMOVE": "1", "HOMEBREW_NO_AUTO_UPDATE": "1",')
    t=replace(t,'routes.route_for(name, kind, self.settings)', 'routes.route_for(name, "formula" if kind == "tool" else kind, self.settings)')
    t=replace(t,'            if records.get(name, {}).get("provider") == "standalone_uv":', '''            if kind == "formula" and name in records:
                item["provider"] = "homebrew"
            if kind == "tool" and name in records:
                item["provider"] = records[name]["provider"]
            if kind != "formula" and records.get(name, {}).get("provider") == "standalone_uv":''')
    t=replace(t,'            name, kind = pkg["name"], pkg["kind"]','''            name, kind = pkg["name"], pkg["kind"]
            if pkg.get("inventory_stale"):
                plan.append({**pkg, "version": pkg.get("latest_version", "unknown"), "ready": False, "reason": "inventory_unavailable"})
                continue
            if kind == "port":
                plan.append(macports_provider.plan(self.s, pkg))
                continue''')
    t=replace(t,'    def install_tool(self, item):\n        name, provider = item["name"], item["provider"]','''    def install_tool(self, item):
        if item["provider"] == "macports":
            return macports_provider.upgrade(self.s, item)
        name, provider = item["name"], item["provider"]''')
    t=replace(t,'            result["started_at"], result["completed_at"] = started, s.now_iso()', '            result["kind"] = item["kind"]\n            result["started_at"], result["completed_at"] = started, s.now_iso()')
    changes[p]=t
    p='electron/main.js';t=source(p)
    # Deliberately retain old userData path, including Electron updater downloads.
    t=replace(t,"const DEV_APP_ROOT =", "app.setPath('userData', path.join(os.homedir(), 'Library', 'Application Support', 'Homebrew Update Manager'));\n\nconst DEV_APP_ROOT =")
    t=t.replace("title: 'Homebrew Update Manager'", "title: 'Mac Update Manager'")
    t=t.replace("dialog.showErrorBox('Homebrew Update Manager'", "dialog.showErrorBox('Mac Update Manager'")
    t=replace(t,"        'Homebrew Update Manager',", "        'Mac Update Manager',")
    t=replace(t,"payload.app === 'Homebrew Update Manager'", "payload.app === 'Mac Update Manager' && payload.protocol === 6")
    t += '''\nfor (const [channel, endpoint] of [['migration:plan','/api/migrations/plan'],
  ['migration:approve','/api/migrations/approve'], ['macports:sync','/api/macports/sync']]) {
  ipcMain.handle(channel, async (_event, payload) => fetchJson(serviceUrl(endpoint), {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload || {}),
  }));
}\n'''
    changes[p]=t
    # Health protocol prevents accidentally attaching to an older installed backend.
    changes['backend/brew-updates-service.py']=replace(changes['backend/brew-updates-service.py'],
        '                    "project_root": str(PROJECT_ROOT),','                    "project_root": str(PROJECT_ROOT),\n                    "protocol": 6,')
    p='electron/preload.js';t=source(p)
    t=replace(t,"  getState: () =>", "  migrationPlan: (name) => ipcRenderer.invoke('migration:plan', {name}),\n  migrationApprove: (id) => ipcRenderer.invoke('migration:approve', {id}),\n  macportsSync: () => ipcRenderer.invoke('macports:sync'),\n  getState: () =>")
    changes[p]=t
    p='renderer/app.js';t=source(p)
    t=replace(t,'function renderTable() {','function renderTable() {\n  renderMacManager();')
    t=replace(t,"pkg.kind === 'formula' || pkg.kind === 'cask'", "pkg.kind === 'formula' || pkg.kind === 'cask' || pkg.kind === 'port' || pkg.kind === 'tool'")
    t=replace(t,"  return !!String(settingsState?.settings?.brew_path || '').trim();", "  return !!String(settingsState?.settings?.brew_path || '').trim() || !!snapshot?.providers?.macports?.ok;")
    t=replace(t,"  const value = String(kind || 'formula');", "  if (kind === 'port') return 'MacPorts';\n  if (kind === 'tool') return i18n.lang === 'he' ? 'כלי מנוהל' : 'Managed tool';\n  const value = String(kind || 'formula');")
    t=replace(t,"if (pkg.provider && pkg.provider !== 'homebrew')", "if (pkg.provider)")
    changes[p]=t
    p='renderer/index.html';t=source(p)
    t=replace(t,'<script src="./app.js"></script>', '<script src="./mac-manager.js"></script>\n  <script src="./app.js"></script>')
    t=replace(t,'<select id="packageTypeFilter">', '<select id="packageTypeFilter">\n              <option value="port">MacPorts</option>\n              <option value="tool" data-i18n="table.filterTypeTool">Managed tools</option>')
    changes[p]=t.replace('Homebrew Update Manager','Mac Update Manager')
    p='renderer/i18n.js';t=source(p)
    t=replace(t,"        filterTypeAll: 'All types',", "        filterTypeTool: 'Managed tools',\n        filterTypeAll: 'All types',")
    t=replace(t,"        filterTypeAll: 'כל הסוגים',", "        filterTypeTool: 'כלים מנוהלים',\n        filterTypeAll: 'כל הסוגים',")
    for old,new in [('Homebrew Update Manager','Mac Update Manager'),('מנהל עדכוני Homebrew','מנהל עדכוני Mac'),
                    ('Modern desktop manager for Homebrew packages','Updates for Homebrew, MacPorts and your managed tools'),
                    ('אפליקציית דסקטופ מודרנית לניהול חבילות Homebrew','עדכוני Homebrew, MacPorts וכלים מנוהלים'),
                    ('These controls update Homebrew packages (formulae/casks), not the app version.','These controls update packages through their installed package manager.'),
                    ('הכפתורים כאן מעדכנים חבילות Homebrew (חבילות בנייה/אפליקציות mac), ולא את גרסת האפליקציה.','הכפתורים כאן מעדכנים חבילות באמצעות מנהל ההתקנה שלהן.')]:
        if old not in t: raise RuntimeError('Missing branding precondition: '+old)
        t=t.replace(old,new)
    changes[p]=t
    for p in ['package.json','package-lock.json']:
        t=source(p);t=t.replace('"version": "1.1.19"','"version": "1.2.3"')
        if p=='package.json':
            t=replace(t,'"productName": "Homebrew Update Manager"','"productName": "Mac Update Manager"')
            t=replace(t,'macOS desktop app for Homebrew update management','macOS desktop app for Homebrew, MacPorts and managed tool updates')
        changes[p]=t
    p='tests/progress-ui.cjs';t=source(p)
    t=replace(t,"      if (process.env.BREW_PROGRESS_PREVIEW_DIR) {", '''      const providers = await win.webContents.executeJavaScript(`(() => {
        snapshot = {counts:{}, providers:{homebrew:{ok:false},macports:{ok:true}},
          migrations:[{name:'terminal-notifier',ports:['terminal-notifier'],executable:true}],
          packages:[{name:'wget',kind:'formula',provider:'homebrew',outdated:true},
                    {name:'wget',kind:'port',provider:'macports',outdated:true}]};
        activeTab='installed'; renderTable();
        const panel=document.getElementById('macManagerPanel');
        const result={rows:document.querySelectorAll('#tbody tr').length,
          independent:packageSelectionKey('wget','port')!==packageSelectionKey('wget','formula'),
          proposal:panel.textContent.includes('terminal-notifier'),
          translated:!panel.textContent.includes('undefined'),
          enabled:isBrewPathConfigured(),located:!!document.querySelector('.wrap #macManagerPanel'),
          collapsible:panel.tagName==='DETAILS'};
        snapshot={counts:{},packages:[]};renderTable();return result;
      })()`);
      assert.equal(providers.rows,2);
      for (const key of ['independent','proposal','translated','enabled','located','collapsible']) assert.equal(providers[key],true,lang+': '+key);
      if (process.env.BREW_PROGRESS_PREVIEW_DIR) {''')
    changes[p]=t
    p='README.md';t=source(p)
    t=replace(t,'# Brew Update Manager (Desktop macOS App)','# Mac Update Manager (Desktop macOS App)')
    t=replace(t,'Electron desktop app for managing Homebrew updates locally on macOS.','Electron desktop app for Homebrew, MacPorts and managed tool updates on macOS.')
    t=t.replace('`Homebrew Update Manager-', '`Mac Update Manager-').replace('**Homebrew Update Manager**','**Mac Update Manager**')
    changes[p]=t+ '\n\n## Mac Update Manager 1.2\n\nSee [combined provider behavior and local rollout](docs/MAC_MANAGER.md).\n'
    return version_bundle(automatic_bundle(progress_bundle(changes)))

def progress_bundle(changes):
    """Bundle 002: check lifecycle owns its progress, including failure/cancellation."""
    p='backend/mac_manager.py';t=changes[p]
    t=replace(t,'import time\n','import time\nimport threading\n\nSCAN_CONTEXT = threading.local()\n')
    t=replace(t,'def combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):', '''def combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):
    if track_progress:
        s.reset_check_progress_for_run()
        s.set_check_progress(operation_name='check', phase='collecting_installed',
                             message='Checking Homebrew inventory', current_package='Homebrew',
                             done=0, total=3, eta_seconds=None)
    prior = getattr(SCAN_CONTEXT, 'active', False)
    SCAN_CONTEXT.active = prior or track_progress
    try:
        return _combined(s, brew_scan, refresh, track_progress, enrich_details)
    except Exception as error:
        if track_progress:
            canceled = isinstance(error, s.OperationCancelledError)
            s.set_check_progress(running=False, phase='canceled' if canceled else 'error',
                message='Check canceled' if canceled else 'Check failed: ' + str(error),
                canceled=canceled, error=None if canceled else str(error),
                current_package=None, eta_seconds=None, completed_at=s.now_iso())
        raise
    finally:
        SCAN_CONTEXT.active = prior


def _combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):''')
    t=replace(t,"        status['homebrew'] = {'ok': True, 'checked_at': s.now_iso()}","        status['homebrew'] = {'ok': not bool(state.get('errors')), 'checked_at': s.now_iso(),\n                              'error': '; '.join(state.get('errors', []))}")
    t=replace(t,'state = brew_scan(refresh, track_progress=False, enrich_details=enrich_details)',
              'state = brew_scan(refresh, track_progress=track_progress, enrich_details=enrich_details, complete_progress=False)')
    t=replace(t,'    except Exception as error:\n        errors.append(\'Homebrew:', '    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append(\'Homebrew:')
    t=replace(t,'    try:\n        ports = mp.inventory(s)', '''    if track_progress:
        s.raise_if_cancel_requested()
        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',
                             current_package='MacPorts', done=1, total=3, eta_seconds=None)
    try:
        ports = mp.inventory(s)''')
    t=replace(t,"    except Exception as error:\n        errors.append('MacPorts:","    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('MacPorts:")
    t=replace(t,'    offers = []\n', '''    if track_progress:
        s.raise_if_cancel_requested()
        s.set_check_progress(phase='preflight', message='Checking binary routes and migration proposals',
                             current_package=None, done=2, total=3, eta_seconds=None)
    offers = []
''')
    t=replace(t,"    except Exception as error:\n        errors.append('Migration recommendations:","    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('Migration recommendations:")
    t=replace(t,"partial=not all(p['ok'] for p in status.values()), migrations=offers,", "partial=bool(errors) or not all(p['ok'] for p in status.values()), migrations=offers,")
    t=replace(t,"    s.atomic_write_json(s.STATE_FILE, state)\n    if track_progress:","    if track_progress:\n        s.raise_if_cancel_requested()\n    s.atomic_write_json(s.STATE_FILE, state)\n    if track_progress:")
    t=replace(t,"        s.set_check_progress(running=False, phase='completed', message='Package inventories checked',\n                             completed_at=s.now_iso(), eta_seconds=0)",'''        failed = not any(provider['ok'] for provider in status.values())
        s.set_check_progress(running=False, phase='error' if failed else 'completed',
            message='All inventory checks failed' if failed else
                    ('Inventory checked with warnings' if state['partial'] else 'Package inventories checked'),
            done=3, total=3, current_package=None, error='; '.join(errors) if errors else None,
            completed_at=s.now_iso(), eta_seconds=None if failed else 0)''')
    changes[p]=t
    p='backend/brew-updates-service.py';t=changes[p]
    t=replace(t,'def compute_homebrew_snapshot(run_brew_update: bool, track_progress: bool = False, *, enrich_details: bool = True)',
              'def compute_homebrew_snapshot(run_brew_update: bool, track_progress: bool = False, *, enrich_details: bool = True, complete_progress: bool = True)')
    t=replace(t,'        if track_progress:\n            set_check_progress(\n                running=False,\n                phase="completed",\n                message="Check completed",',
              '        if track_progress and complete_progress:\n            set_check_progress(\n                running=False,\n                phase="completed",\n                message="Check completed",')
    t=replace(t,'        if track_progress:\n            set_check_progress(\n                running=False,\n                phase="error",\n                message=f"Check failed: {exc}",',
              '        if track_progress and complete_progress:\n            set_check_progress(\n                running=False,\n                phase="error",\n                message=f"Check failed: {exc}",')
    t=replace(t,'            eta = estimate_eta_seconds(progress_started_at, done, total)',
              '            raise_if_cancel_requested()\n            eta = estimate_eta_seconds(progress_started_at, done, total)',count=2)
    t=replace(t,'def run_cmd(args: list[str], timeout: int | None = 1800, *, cancellable: bool = False,', '''def run_cmd(args: list[str], timeout: int | None = 1800, *, cancellable: bool = False,
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


def _run_cmd_impl(args: list[str], timeout: int | None = 1800, *, cancellable: bool = False,''')
    t=replace(t,'    try:\n        return fn()\n    finally:\n        reset_cancel_state()', '''    prior_scan = getattr(mac_manager.SCAN_CONTEXT, 'active', False)
    if op_name in {'update_plan', 'migration'}:
        mac_manager.SCAN_CONTEXT.active = True
    try:
        return fn()
    except OperationCancelledError:
        set_check_progress(running=False, phase='canceled', message='Operation canceled',
                           canceled=True, current_package=None, eta_seconds=None, completed_at=now_iso())
        raise
    finally:
        mac_manager.SCAN_CONTEXT.active = prior_scan
        reset_cancel_state()''')
    t=replace(t,'                set_check_progress(running=False, phase="completed" if not sys.exc_info()[0] else "error",\n                                   completed_at=now_iso(), eta_seconds=0)', '''                canceled = isinstance(sys.exc_info()[1], OperationCancelledError)
                set_check_progress(running=False, phase='canceled' if canceled else ('error' if sys.exc_info()[0] else 'completed'),
                                   canceled=canceled, current_package=None,
                                   completed_at=now_iso(), eta_seconds=None if sys.exc_info()[0] else 0)''')
    t=replace(t,'                    set_check_progress(running=False, phase="completed", message="Update routes checked", completed_at=now_iso())', '''                    raise_if_cancel_requested()
                    set_check_progress(running=False, phase="completed", message="Update routes checked", current_package=None, completed_at=now_iso(), eta_seconds=0)''')
    t=replace(t,'                set_check_progress(running=False, phase="error", error=str(exc))', '''                canceled = isinstance(exc, OperationCancelledError)
                set_check_progress(running=False, phase='canceled' if canceled else 'error',
                                   canceled=canceled, error=None if canceled else str(exc),
                                   current_package=None, eta_seconds=None, completed_at=now_iso())''')
    t=replace(t,'            except Exception as exc:\n                self._send_json(500, {"ok": False, "error": "check_failed", "detail": str(exc)})', '''            except OperationCancelledError:
                self._send_json(200, {"ok": True, "canceled": True})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": "check_failed", "detail": str(exc)})''')
    changes[p]=t
    p='renderer/app.js';t=changes[p]
    t=replace(t,": (progress.running ? i18n.t('progress.noEta') : i18n.t('progress.doneNow'));", ": (progress.running ? i18n.t('progress.noEta') :\n        (progress.phase === 'completed' ? i18n.t('progress.doneNow') : '—'));")
    t=replace(t,"if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = current || i18n.t('progress.currentNone');", "if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = current || (progress.running ? i18n.t('progress.currentNone') : '—');")
    t=replace(t,'async function pollCheckProgress() {','let progressPollEpoch = 0;\n\nasync function pollCheckProgress() {\n  const epoch = progressPollEpoch;')
    t=replace(t,'function stopProgressPolling() {','function stopProgressPolling() {\n  progressPollEpoch += 1;')
    start=t.index('async function pollCheckProgress() {');end=t.index('function startProgressPolling()',start)
    block=t[start:end]
    block=replace(block,'    if (payload?.progress) {','    if (epoch !== progressPollEpoch) return;\n    if (payload?.progress) {')
    block=replace(block,'    progressPollInFlight = false;','    if (epoch === progressPollEpoch) progressPollInFlight = false;')
    t=t[:start]+block+t[end:]
    t=replace(t,'async function checkNow() {', '''function showCheckResult(state) {
  showMessage(i18n.t(state?.partial ? 'message.checkPartial' : 'message.checkSuccess'), !!state?.partial);
}

async function checkNow() {''')
    start=t.index('async function checkNow() {');end=t.index('async function updateOne(',start)
    block=t[start:end]
    block=replace(block,"showMessage(i18n.t('message.checkSuccess'));",'showCheckResult(snapshot);',count=2)
    block=replace(block,'    const res = await window.brewApp.runCheckNow();\n    snapshot =', '''    const res = await window.brewApp.runCheckNow();
    if (res?.canceled) {
      showMessage(i18n.t('message.checkCanceled'));
      return;
    }
    snapshot =''')
    block=replace(block,"    stopProgressPolling();\n    setLoading(false, 'message.loadingState');",'''    stopProgressPolling();
    try {
      const finalStatus = await window.brewApp.getProgress();
      if (finalStatus?.progress) renderCheckProgress(finalStatus.progress);
    } catch { /* Keep the last known status when the service is unavailable. */ }
    setLoading(false, 'message.loadingState');''')
    t=t[:start]+block+t[end:]
    changes[p]=t
    p='renderer/i18n.js';t=changes[p]
    t=replace(t,"          collecting_installed: 'Collecting installed packages',", "          collecting_installed: 'Collecting installed packages',\n          collecting_macports: 'Checking MacPorts inventory',")
    t=replace(t,"          collecting_installed: 'אוסף חבילות מותקנות',", "          collecting_installed: 'אוסף חבילות מותקנות',\n          collecting_macports: 'בודק חבילות MacPorts',")
    # Each language has its own message dictionary.
    en=t.index('      message: {');he=t.index('      message: {',en+1)
    t=t[:he]+t[he:].replace('      message: {',"      message: {\n        checkPartial: 'הבדיקה החזירה תוצאה חלקית. יש לבדוק את האזהרות של מנהלי החבילות.',\n        checkCanceled: 'הבדיקה בוטלה; המלאי הקודם נשמר.',",1)
    t=t[:en]+t[en:].replace('      message: {',"      message: {\n        checkPartial: 'The check returned incomplete results. Review the package manager warnings.',\n        checkCanceled: 'Check canceled; previous inventory retained.',",1)
    changes[p]=t
    for p in ['package.json','package-lock.json']:
        changes[p]=changes[p].replace('"version": "1.2.3"','"version": "1.2.4"')
    changes['docs/MAC_MANAGER.md'] += '\n\n## 1.2.4 check progress\n\nEach combined scan resets old update results and reports Homebrew, MacPorts, and route checking separately. Cancellation stops tracked commands and preserves the previous complete snapshot. Provider errors remain visible as partial results; failure of both providers is reported as an error. Finished operations show no current item.\n'
    changes['tests/test_check_lifecycle.py'] = CHECK_LIFECYCLE_TESTS
    p='tests/test_upgrade_activity.py';t=source(p)
    t=replace(t,'timeout=0.1, on_output=lambda *_: None,','timeout=1.0, on_output=lambda *_: None,')
    changes[p]=t
    p='tests/progress-ui.cjs';t=changes[p]
    t=replace(t,"      assert.equal(providers.rows,2);",'''      const terminal = await win.webContents.executeJavaScript(`(() => {
        const rows = [];
        for (const phase of ['completed','error','canceled']) {
          renderCheckProgress({running:false,operation_name:'check',phase,current_package:null});
          rows.push({phase,current:loaderCurrentValueEl.textContent,eta:loaderEtaValueEl.textContent});
        }
        return rows;
      })()`);
      for (const row of terminal) {
        assert.equal(row.current,'—',lang+': terminal current item');
        if (row.phase !== 'completed') assert.equal(row.eta,'—',lang+': terminal ETA');
      }
      assert.equal(providers.rows,2);''')
    changes[p]=t
    return changes

def automatic_bundle(changes):
    """Bundle 003: one scan refreshes catalogs and checks all mapped alternatives."""
    changes['backend/catalog_refresh.py'] = CATALOG_REFRESH_MODULE
    changes['backend/automatic_alternatives.py'] = AUTO_ALTERNATIVES_MODULE
    changes['tests/test_automatic_alternatives.py'] = AUTO_ALTERNATIVES_TESTS
    changes['tests/test_catalog_refresh.py'] = CATALOG_REFRESH_TESTS
    changes['renderer/mac-manager.js'] = AUTOMATIC_MANAGER_UI
    p='backend/mac_manager.py';t=changes[p]
    t=replace(t,'import update_routes as routes','import update_routes as routes\nimport catalog_refresh\nimport automatic_alternatives')
    t=replace(t,'                if blockers:\n                    offers.append', '''                if blockers:
                    if s.load_settings()['binary_only']:
                        p['update_block_reason'] = 'source_required'
                    offers.append''')
    t=replace(t,'    try:\n        ports = mp.inventory(s)', '''    interactive = track_progress and s.read_current_operation().get('name') == 'check_now'
    catalog_state = catalog_refresh.ensure(s, refresh=refresh, interactive=interactive)
    if track_progress:
        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',
                             current_package='MacPorts', eta_seconds=None)
    try:
        ports = mp.inventory(s)
        if not catalog_state['fresh']:
            ports = [dict(p, inventory_stale=True, update_block_reason='inventory_unavailable') for p in ports]''')
    t=replace(t,"status['macports'] = {'ok': True, 'checked_at': s.now_iso(),\n                             'catalog_note': 'Local catalog; refresh MacPorts catalog explicitly'}", "status['macports'] = {'ok': catalog_state['fresh'], 'checked_at': s.now_iso(),\n                             'catalog': catalog_state, 'catalog_note': catalog_state['status']}")
    t=replace(t,"        status['macports'] = {'ok': False, 'error': str(error),", "        status['macports'] = {'ok': False, 'error': str(error), 'catalog': catalog_state,")
    t=replace(t,"    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')", '''    if refresh:
        try:
            offers = automatic_alternatives.check(s, offers, packages, catalog_state['fresh'], report=track_progress)
        except s.OperationCancelledError:
            raise
        except Exception as error:
            errors.append('Alternative checks: ' + str(error))
            for offer in offers:
                offer.update(status='check_failed', detail=str(error), checked_at=s.now_iso())
    else:
        previous_offers = {o['name']: o for o in previous.get('migrations', [])}
        for offer in offers:
            pkg = next(p for p in packages if p['name']==offer['name'] and p['kind']=='formula')
            prior = previous_offers.get(offer['name'], {})
            offer['homebrew_target'] = pkg.get('latest_version')
            if (catalog_state['fresh'] and prior.get('homebrew_target') == pkg.get('latest_version')
                    and prior.get('status') not in {'requires_live_check','checking_pending'}
                    and catalog_refresh.recent(prior.get('checked_at'), 6*3600)):
                offer.update(prior)
            else:
                offer.update(status='checking_pending' if offer['ports'] else 'mapping_unverified')
            pkg.update(alternative_status=offer['status'], alternative_detail=offer.get('detail',''))
    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')''')
    changes[p]=t
    p='backend/brew-updates-service.py';t=changes[p]
    t=replace(t,'                if result.code: raise RuntimeError(result.stderr)\n                return {"ok": True}',
              '                if result.code: raise RuntimeError(result.stderr)\n                mac_manager.catalog_refresh.mark_success(sys.modules[__name__])\n                return {"ok": True}')
    t=replace(t,'                pkg.pop("update_block_reason", None)\n                if item', '''                pkg.pop("update_block_reason", None)
                if (pkg.get('kind') == 'formula' and load_settings()['binary_only'] and
                    any(o['name'] == pkg['name'] and o.get('source_blockers') for o in state.get('migrations', []))):
                    pkg['update_block_reason'] = 'source_required'
                if item''')
    changes[p]=t
    p='tests/progress-ui.cjs';t=changes[p]
    t=replace(t,'      assert.equal(providers.rows,2);', '''      const automatic = await win.webContents.executeJavaScript(`(() => {
        snapshot={packages:[{name:'llvm',kind:'formula',provider:'homebrew',outdated:true,update_block_reason:'source_required',alternative_status:'binary_available'}],
          providers:{macports:{ok:true,catalog:{status:'fresh'}}},
          migrations:[{name:'llvm',ports:['llvm-23'],status:'binary_available',executable:false},
                      {name:'missing',ports:[],status:'mapping_unverified'}]};
        activeTab='outdated';renderTable();
        const panel=document.getElementById('macManagerPanel');
        const labels=Array.from(panel.querySelectorAll('button')).map(b=>b.textContent);
        const result={automatic:panel.textContent.includes(macAlternativeLabel('binary_available')),
          unknown:panel.textContent.includes(macAlternativeLabel('mapping_unverified')),
          noManualChecks:labels.length===0,
          tableStatus:document.getElementById('tbody').textContent.includes(macAlternativeLabel('binary_available'))};
        snapshot={counts:{},packages:[]};renderTable();return result;
      })()`);
      for(const key of ['automatic','unknown','noManualChecks','tableStatus'])assert.equal(automatic[key],true,lang+': '+key);
      assert.equal(providers.rows,2);''')
    changes[p]=t
    p='backend/macports_provider.py';t=changes[p]
    t=replace(t,"s.set_check_progress(message='MacPorts is installing verified ready packages', eta_seconds=None)",
              "s.set_check_progress(message='Refreshing MacPorts catalog' if action=='sync' else 'MacPorts is installing verified ready packages', eta_seconds=None)")
    changes[p]=t
    p='renderer/app.js';t=changes[p]
    t=replace(t,'    const tdSelect = document.createElement(\'td\');', '''    if (pkg.alternative_status) {
      const alternative = document.createElement('div'); alternative.className='small muted';
      alternative.textContent = macAlternativeLabel(pkg.alternative_status);
      alternative.title = pkg.alternative_detail || '';
      tdStatus.appendChild(alternative);
    }
    const tdSelect = document.createElement('td');''')
    changes[p]=t
    for p in ['package.json','package-lock.json']:
        changes[p]=changes[p].replace('"version": "1.2.4"','"version": "1.3.0"')
    changes['docs/MAC_MANAGER.md'] += '\n\n## 1.3 automatic alternative checks\n\nStart Check refreshes the MacPorts catalog when its last verified sync or local index is older than 24 hours. Foreground scans request macOS authorization only when needed; scheduled checks report awaiting authorization and never open an unattended prompt. Failed or declined refresh keeps local inventory visible as unverified. All mapped Homebrew binary blockers are checked together, including full runtime closures. Shared exact archives are checked once; available results expire after six hours and missing observations after thirty minutes. Unknown network results are not reused. Availability is not installation/signature verification or migration approval. Only the supported terminal-notifier pilot can execute a reviewed migration; other available alternatives explicitly require consumer migration plans. Unknown mappings are displayed as unverified, not absent. Regular update actions never migrate packages.\n'
    return changes

def version_bundle(changes):
    """Bundle 004: compare alternative updates with the installed baseline."""
    p='backend/automatic_alternatives.py';t=changes[p]
    marker='def check(s, offers, packages, catalog_fresh, report=False):'
    t=replace(t,marker,'''VERSION_POLICY = 2

def compare_versions(probe, installed_version, homebrew_target, candidates):
    installed = probe.version(installed_version)
    parsed = [probe.version(value) for value in candidates.values()]
    if installed is None or not parsed or any(value is None for value in parsed):
        return dict(status='version_unverified', detail='Installed and candidate upstream versions could not be compared safely')
    if any(value < installed for value in parsed):
        return dict(status='older_than_installed', detail='A mapped MacPorts component is older than the installed version; downgrade blocked')
    if not any(value > installed for value in parsed):
        return dict(status='not_newer', detail='MacPorts provides the same upstream version; this is not a newer update')
    brew = probe.version(homebrew_target)
    below = brew is not None and any(value < brew for value in parsed)
    return dict(status='version_eligible', below_homebrew_target=below,
                detail='Newer than the installed version; below the Homebrew target' if below else 'Newer than the installed version')


def check(s, offers, packages, catalog_fresh, report=False):''')
    t=replace(t,"output = [dict(offer, checked_at=checked_at, archive_count=0,", "output = [dict(offer, checked_at=checked_at, archive_count=0, version_policy=VERSION_POLICY,")
    t=replace(t,"        offer['target'] = offer['homebrew_target']", "        offer['installed_version'] = package_by_name.get(name, {}).get('installed_version')\n        offer['target'] = None")
    t=replace(t,"                    target = probe.version(offer['target'])\n                    candidates = [probe.version(value) for value in versions.values()]\n                    if target is None or any(value is None for value in candidates):\n                        mark(offer, 'version_unverified', 'Upstream version comparison could not be verified')\n                    elif any(value < target for value in candidates):\n                        mark(offer, 'version_gap', 'The mapped MacPorts upstream version is older than the Homebrew target')\n                    else:\n                        eligible.append((offer, archives))", '''                    offer['target'] = versions[selected_roots[0]]
                    comparison = compare_versions(probe, offer['installed_version'], offer['homebrew_target'], versions)
                    offer['below_homebrew_target'] = comparison.get('below_homebrew_target', False)
                    offer['version_detail'] = comparison['detail']
                    if comparison['status'] != 'version_eligible':
                        mark(offer, comparison['status'], comparison['detail'])
                    else:
                        eligible.append((offer, archives))''')
    t=replace(t,"mark(offer, 'binary_available', 'All runtime archives and signatures answered HEAD; installation and integrity remain unverified')", "mark(offer, 'binary_available', offer['version_detail'] + '; all runtime archives and signatures are available; installation and integrity remain unverified')")
    t=replace(t,"row.update(alternative_status=offer['status'], alternative_detail=offer['detail'])", "row.update(alternative_status=offer['status'], alternative_detail=offer['detail'],\n                       alternative_target=offer.get('target'), alternative_below_homebrew=offer.get('below_homebrew_target', False))")
    changes[p]=t
    p='backend/mac_manager.py';t=changes[p]
    t=replace(t,"prior.get('homebrew_target') == pkg.get('latest_version')", "prior.get('homebrew_target') == pkg.get('latest_version')\n                    and prior.get('version_policy') == automatic_alternatives.VERSION_POLICY\n                    and prior.get('installed_version') == pkg.get('installed_version')")
    t=replace(t,"pkg.update(alternative_status=offer['status'], alternative_detail=offer.get('detail',''))", "pkg.update(alternative_status=offer['status'], alternative_detail=offer.get('detail',''),\n                       alternative_target=offer.get('target'), alternative_below_homebrew=offer.get('below_homebrew_target', False))")
    t=replace(t,"    need, have = probe.version(pkg['latest_version']), probe.version(target)\n    if need is None or have is None or have < need:\n        raise ValueError('MacPorts upstream version is older or cannot be compared safely')", '''    candidates = {root: next(n['version'] for n in graph['nodes'] if n['name'] == root) for root in roots}
    comparison = automatic_alternatives.compare_versions(probe, pkg['installed_version'], pkg['latest_version'], candidates)
    if comparison['status'] != 'version_eligible':
        raise ValueError(comparison['detail'])''')
    t=replace(t,"plan = dict(name=name, roots=roots, installed_version=pkg['installed_version'], target=target,", "plan = dict(name=name, roots=roots, installed_version=pkg['installed_version'], target=target,\n        version_policy=automatic_alternatives.VERSION_POLICY,\n        homebrew_target=pkg['latest_version'], below_homebrew_target=comparison['below_homebrew_target'],")
    t=replace(t,"for key in ('installed_version', 'target', 'consumers', 'remove_brew'):\n        if old[key] != fresh[key]:", "for key in ('installed_version', 'target', 'consumers', 'remove_brew', 'version_policy'):\n        if old.get(key) != fresh.get(key):")
    changes[p]=t
    p='renderer/mac-manager.js';t=changes[p]
    t=replace(t,"    version_gap:['גרסת MacPorts ישנה מהעדכון הנדרש','MacPorts version is older than the required update'],", "    older_than_installed:['החלופה ישנה מהגרסה המותקנת — ירידה בגרסה חסומה','Alternative is older than the installed version; downgrade blocked'],\n    not_newer:['אין גרסה חדשה יותר במסלול זה','No newer upstream version on this route'],")
    t=replace(t,"summary.textContent=`${offer.name} — ${macAlternativeLabel(offer.status)}`;row.appendChild(summary);", "summary.textContent=`${offer.name} — ${macAlternativeLabel(offer.status)}`;\n    if(offer.target&&offer.installed_version)summary.textContent+=` · ${offer.installed_version} → ${offer.target}`;\n    row.appendChild(summary);")
    t=replace(t,"      const availability=document.createElement('p');", '''      if(offer.below_homebrew_target) {
        const version=document.createElement('p');
        version.textContent=he?`זו גרסה חדשה מהמותקנת אצלך. יעד Homebrew הוא ${offer.homebrew_target}; אין בכך הוכחה שזו הגרסה האחרונה הנתמכת ב־Intel.`:`This is newer than your installed version. Homebrew targets ${offer.homebrew_target}; this does not prove it is the last Intel-supported release.`;
        row.appendChild(version);
      }
      const availability=document.createElement('p');''')
    changes[p]=t
    p='renderer/app.js';t=changes[p]
    t=replace(t,'alternative.textContent = macAlternativeLabel(pkg.alternative_status);', "alternative.textContent = macAlternativeLabel(pkg.alternative_status);\n      if(pkg.alternative_target)alternative.textContent+=` · ${pkg.installed_version} → ${pkg.alternative_target}`;")
    changes[p]=t
    p='renderer/i18n.js';t=changes[p]
    t=replace(t,"updateAll: 'Update packages using verified routes'", "updateAll: 'Update all'")
    t=replace(t,"updateAll: 'עדכן חבילות במסלולים שנבדקו'", "updateAll: 'עדכן הכול'")
    changes[p]=t
    p='tests/test_automatic_alternatives.py';t=changes[p]
    t=replace(t,"kind='formula', latest_version='1.2'", "kind='formula', installed_version='1.0', latest_version='1.2'")
    t=replace(t,"self.nodes[0]['version']='1.1'", "self.nodes[0]['version']='0.9'")
    t=replace(t,"self.assertEqual(self.run_check()[0][0]['status'],'version_gap')", "self.assertEqual(self.run_check()[0][0]['status'],'older_than_installed')")
    # This fixture tests variants, so provide a comparable installed baseline too.
    t=replace(t,"provider='homebrew',kind='formula',latest_version='1.2'", "provider='homebrew',kind='formula',installed_version='1.0',latest_version='1.2'")
    t=replace(t,'    def test_missing_transitive_is_distinct_from_network_unknown(self):', '''    def test_ready_candidate_may_be_below_homebrew_target(self):
        packages=[dict(name='first',provider='homebrew',kind='formula',installed_version='1.0',latest_version='3')]
        result=aa.check(self.s,[dict(name='first',ports=['first'])],packages,True)[0]
        self.assertEqual(result['status'],'binary_available')
        self.assertEqual(result['target'],'1.2')
        self.assertEqual(result['homebrew_target'],'3')
        self.assertTrue(result['below_homebrew_target'])
        self.assertEqual(packages[0]['alternative_target'],'1.2')

    def test_missing_transitive_is_distinct_from_network_unknown(self):''')
    changes[p]=t
    changes['tests/test_alternative_versions.py'] = ALTERNATIVE_VERSION_TESTS
    for p in ['package.json','package-lock.json']:
        changes[p]=changes[p].replace('"version": "1.3.0"','"version": "1.3.1"')
    changes['docs/MAC_MANAGER.md'] += '\n\n## 1.3.1 installed-version baseline\n\nAlternative updates compare the mapped MacPorts upstream component versions with the actual Homebrew installation, not with Homebrew latest. All mapped components must avoid a downgrade and at least one must be newer. A version below Homebrew latest can qualify after complete binary closure checks; it is never described as the last Intel-supported release without upstream evidence. Equal upstream versions and unknown comparisons remain distinct from updates. Cached recommendations are invalidated on policy or installed-version changes. Every approved migration repeats the same policy with a fresh inventory. The primary action is labeled Update all / עדכן הכול.\n'
    changes['docs/MAC_MANAGER.md'] += '''

## Agreed next work

- Generalize the supported automatic migration transaction to suitable standalone CLI tools: install binary dependencies, verify commands, activate the chosen provider, verify usage, and remove the previous copy only when managed dependencies permit it. Libraries and services retain their own migration requirements.
- Add Pixi/conda-forge as an automatic alternative provider alongside Homebrew and MacPorts. Reuse existing Pixi installations and records; evaluate compatible osx-64 binary closures and versions against the installed baseline, with no source-build fallback or unrequested duplicate installations.
- User-owned scripts referring to old Homebrew paths are explicitly not a migration blocker. The user accepts responsibility for adjusting such scripts when discovered. This does not waive checks for dependencies recorded by package managers, service configuration, or data handling.

These are pending tasks, not completed capabilities. Pixi already manages selected tools; its automatic alternative discovery and generalized migration execution still require implementation.
'''
    return changes

ALTERNATIVE_VERSION_TESTS = '''import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_update_policy import service as s
import automatic_alternatives as aa
import mac_manager as mm
import macports_provider as mp

class AlternativeVersions(unittest.TestCase):
    def setUp(self):
        self.probe=mp.load_probe()

    def test_candidate_between_installed_and_brew_is_eligible(self):
        result=aa.compare_versions(self.probe,'1.0_4','3.0_2',{'first':'2.0'})
        self.assertEqual(result['status'],'version_eligible')
        self.assertTrue(result['below_homebrew_target'])

    def test_downgrade_and_equal_are_not_updates(self):
        self.assertEqual(aa.compare_versions(self.probe,'2','4',{'first':'1'})['status'],'older_than_installed')
        self.assertEqual(aa.compare_versions(self.probe,'2_3','4',{'first':'2.0'})['status'],'not_newer')

    def test_all_mapped_components_must_avoid_downgrade(self):
        self.assertEqual(aa.compare_versions(self.probe,'2','4',{'first':'3','second':'1'})['status'],'older_than_installed')
        self.assertEqual(aa.compare_versions(self.probe,'2','4',{'first':'3','second':'2'})['status'],'version_eligible')

    def test_unknown_baseline_blocks_but_unknown_brew_target_does_not(self):
        self.assertEqual(aa.compare_versions(self.probe,'unknown','3',{'first':'2'})['status'],'version_unverified')
        self.assertEqual(aa.compare_versions(self.probe,'1','unknown',{'first':'2'})['status'],'version_eligible')
        self.assertEqual(aa.compare_versions(self.probe,'1','3',{'first':'2-rc1'})['status'],'version_unverified')

    def test_explicit_migration_uses_installed_baseline_and_never_downgrades(self):
        pkg=dict(name='terminal-notifier',kind='formula',installed_version='1',latest_version='3')
        node=dict(name='terminal-notifier',version='2',revision='0',variants='')
        from update_engine import Engine
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'DATA_DIR',Path(tmp)), \\
             patch.object(s,'is_excluded',return_value=False), \\
             patch.object(s,'fetch_outdated_index',return_value={}), \\
             patch.object(s,'fetch_installed_inventory',return_value=[pkg]), \\
             patch.object(Engine,'catalog',return_value={}),patch.object(Engine,'installed',return_value={}), \\
             patch.object(mm.routes,'bottle_blockers',return_value=[{'reason':'source_required'}]), \\
             patch.object(mp,'preflight',return_value={'nodes':[node]}),patch.object(mp,'checked',return_value=''), \\
             patch.object(s,'brew_bin',return_value='/fixture/brew'):
            plan=mm.migration_plan(s,'terminal-notifier')
            self.assertEqual(plan['target'],'2')
            self.assertTrue(plan['below_homebrew_target'])
            pkg['installed_version']='2.5'
            with self.assertRaisesRegex(ValueError,'downgrade'):mm.migration_plan(s,'terminal-notifier')

    def test_cached_offer_requires_same_policy_and_installed_baseline(self):
        from update_engine import Engine
        for policy,baseline,expected in [(1,'1','checking_pending'),(2,'0.5','checking_pending'),(2,'1','binary_available')]:
            pkg=dict(name='fixture',kind='formula',provider='homebrew',installed_version='1',latest_version='3',outdated=True)
            prior=dict(name='fixture',ports=['fixture'],homebrew_target='3',installed_version=baseline,
                       target='2',version_policy=policy,checked_at=s.now_iso(),status='binary_available')
            with tempfile.TemporaryDirectory() as tmp,patch.object(s,'DATA_DIR',Path(tmp)), \\
                 patch.object(s,'STATE_FILE',Path(tmp)/'state.json'),patch.object(s,'read_json',return_value={'migrations':[prior]}), \\
                 patch.object(mm.catalog_refresh,'ensure',return_value={'fresh':True,'status':'fresh'}), \\
                 patch.object(mp,'inventory',return_value=[]),patch.object(mm,'mapping',return_value={'fixture':['fixture']}), \\
                 patch.object(mm.routes,'read_records',return_value={}), \\
                 patch.object(Engine,'catalog',return_value={}),patch.object(Engine,'installed',return_value={}), \\
                 patch.object(mm.routes,'bottle_blockers',return_value=[{'reason':'source_required'}]):
                state=mm.combined(s,lambda *a,**kw:{'packages':[pkg]},False,False,False)
            self.assertEqual(state['migrations'][0]['status'],expected)
'''

CATALOG_REFRESH_MODULE = '''"""Automatic catalog freshness; Authorization Services remains foreground only."""
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
'''

CATALOG_REFRESH_TESTS = '''import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_update_policy import service as s
import catalog_refresh as catalog
import macports_provider as mp

class CatalogRefresh(unittest.TestCase):
    def test_fresh_index_needs_no_prompt(self):
        with patch.object(catalog,'fingerprint',return_value={'mtime_ns':int(time.time()*1e9),'size':1}), \\
             patch.object(s,'read_json',return_value={}),patch.object(mp,'privileged') as auth:
            self.assertTrue(catalog.ensure(s,True,True)['fresh'])
            auth.assert_not_called()

    def test_scheduled_stale_catalog_never_prompts(self):
        with patch.object(catalog,'inspect',return_value={'fresh':False,'status':'stale'}), \\
             patch.object(Path,'is_file',return_value=True),patch.object(mp,'privileged') as auth:
            self.assertEqual(catalog.ensure(s,True,False)['status'],'awaiting_authorization')
            auth.assert_not_called()

    def test_declined_authorization_is_not_fresh(self):
        with patch.object(catalog,'inspect',return_value={'fresh':False,'status':'stale'}), \\
             patch.object(Path,'is_file',return_value=True),patch.object(s,'raise_if_cancel_requested'), \\
             patch.object(s,'append_service_log'),patch.object(mp,'privileged',side_effect=RuntimeError('declined')):
            self.assertEqual(catalog.ensure(s,True,True)['status'],'refresh_failed')

    def test_successful_unchanged_sync_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'DATA_DIR',Path(tmp)), \\
             patch.object(catalog,'fingerprint',return_value={'mtime_ns':1,'size':1}):
            catalog.mark_success(s)
            self.assertTrue(catalog.inspect(s)['fresh'])
            with patch.object(catalog,'fingerprint',return_value={'mtime_ns':2,'size':1}):
                self.assertFalse(catalog.inspect(s)['fresh'])
'''

AUTOMATIC_MANAGER_UI = '''// Automatic availability results; review and approval remain separate actions.
function macAlternativeLabel(status) {
  status=status||'checking_pending';
  const he=i18n.lang==='he';
  const labels={
    binary_available:['נמצאה חבילה מוכנה ב־MacPorts','Ready package available in MacPorts'],
    mapping_unverified:['אין התאמה מאומתת ב־MacPorts','No verified MacPorts mapping'],
    catalog_unverified:['בדיקת החלופה ממתינה לרענון מידע','Alternative check awaits catalog refresh'],
    checking_pending:['החלופה תיבדק אוטומטית בבדיקה הבאה','Alternative will be checked automatically on the next scan'],
    version_gap:['גרסת MacPorts ישנה מהעדכון הנדרש','MacPorts version is older than the required update'],
    version_unverified:['לא ניתן לאמת התאמת גרסאות','Version compatibility could not be verified'],
    archive_missing:['חבילה מוכנה או תלות חסרה באתרים שנבדקו','Ready package or dependency is missing from checked sites'],
    archive_unknown:['לא ניתן לאמת זמינות בגלל שגיאת תקשורת','Availability could not be verified due to network errors'],
    resolution_failed:['לא ניתן להשלים בדיקת תלויות','Dependency resolution could not be completed'],
    architecture_unverified:['לא אומתה התאמה למחשב','Compatibility with this computer is unverified'],
    variants_unverified:['נדרשת בדיקת אפשרויות ההתקנה הקיימת','Installed variants need separate review'],
    excluded:['החבילה או תלות שלה מוחרגת','Package or dependency is excluded'],
    check_failed:['בדיקת החלופה נכשלה','Alternative check failed'],
  };
  return (labels[status]||[status,status])[he?0:1];
}
function renderMacManager() {
  let panel=document.getElementById('macManagerPanel');
  if (!panel) {
    panel=document.createElement('details');panel.id='macManagerPanel';panel.className='panel';
    panel.style.padding='16px';panel.style.marginBlock='16px';document.querySelector('.tabs').before(panel);
  }
  panel.replaceChildren();const he=i18n.lang==='he';
  const title=document.createElement('summary');
  title.textContent=(he?'עדכונים חסומים וחלופות':'Blocked updates and alternatives')+` (${snapshot?.migrations?.length||0})`;
  panel.appendChild(title);
  const hint=document.createElement('p');
  hint.textContent=he?'החלופות נבדקות אוטומטית עם ״התחל בדיקה״. מעבר למנהל אחר דורש בדיקת שימוש ואישור נפרד.':'Alternatives are checked automatically by Start Check. Changing managers requires a consumer review and separate approval.';
  panel.appendChild(hint);
  const labels={fresh:he?'עדכני':'current',refreshed:he?'רוענן אוטומטית':'refreshed automatically',stale:he?'ירוענן בבדיקה הבאה':'will refresh on next check',awaiting_authorization:he?'הרענון ממתין להזדהות; לחץ התחל בדיקה':'refresh awaits authorization; press Start Check',refresh_failed:he?'הרענון נכשל או לא אושר; התחל בדיקה ינסה שוב':'refresh failed or was declined; Start Check will retry',not_installed:he?'MacPorts אינו מותקן':'MacPorts is not installed'};
  for(const [name,provider] of Object.entries(snapshot?.providers||{})) {
    const status=document.createElement('p');
    status.textContent=`${name}: ${provider.ok?'✓':'⚠'} · ${provider.checked_at||'—'}`;
    if(provider.catalog)status.textContent+=' · '+(labels[provider.catalog.status]||provider.catalog.status);
    if(provider.error)status.title=provider.error;
    panel.appendChild(status);
  }
  if(['running','needs_attention'].includes(snapshot?.migration_journal?.status)) {
    const warning=document.createElement('p');warning.textContent=he?'מעבר קודם דורש בדיקה לפני המשך.':'A previous migration needs review before continuing.';panel.appendChild(warning);
  }
  for(const offer of snapshot?.migrations||[]) {
    const row=document.createElement('details');row.style.marginBlock='10px';
    const summary=document.createElement('summary');
    summary.textContent=`${offer.name} — ${macAlternativeLabel(offer.status)}`;row.appendChild(summary);
    const detail=document.createElement('p');
    detail.textContent=(offer.ports?.length?`${offer.ports.join(', ')} · `:'')+(offer.detail||'');
    if(offer.checked_at)detail.textContent+=' · '+(he?'נבדק: ':'Checked: ')+offer.checked_at;
    row.appendChild(detail);
    if(offer.status==='binary_available') {
      const availability=document.createElement('p');
      availability.textContent=he?'נבדקה זמינות קבצים לכל התלויות. חתימות ותפקוד יאומתו רק בהתקנה.':'Archive availability was checked for all dependencies. Signatures and functionality are verified during installation.';row.appendChild(availability);
      if(!offer.executable) {
        const blocked=document.createElement('p');blocked.textContent=he?'המעבר עדיין דורש תוכנית לצרכנים ולנתונים; אינו מבוצע בעדכון רגיל.':'Migration still requires a consumer and data plan; regular updates do not perform it.';row.appendChild(blocked);
      } else {
        const review=document.createElement('button');review.textContent=he?'סקירת תוכנית המעבר':'Review migration plan';
        review.disabled=!!busyLoading||!!lastKnownOperationRunning;
        review.onclick=async()=>{
          review.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();
          try {
            const {plan}=await window.brewApp.migrationPlan(offer.name);
            const contents=document.createElement('p');
            contents.textContent=`${plan.installed_version} → ${plan.target} · ${plan.graph.nodes.map(n=>n.name).join(', ')} · ${plan.paths.join(', ')} · ${he?'צרכנים':'Consumers'}: ${plan.consumers.join(', ')||'—'}`;row.appendChild(contents);
            const approve=document.createElement('button');approve.textContent=he?'אישור מעבר חד־פעמי':'Approve this migration once';
            approve.onclick=async()=>{
              if(!confirm(he?'לאשר התקנה, בדיקת שימוש והסרת העותק הקודם רק אם אין לו תלויים?':'Approve installation, verification, and removal of the old copy only if unused?'))return;
              approve.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();
              try {await window.brewApp.migrationApprove(plan.id);const response=await window.brewApp.getState();snapshot=response.snapshot||response;renderCards(snapshot);renderTable();}
              catch(error){showMessage(error.message,true);}
              finally{stopProgressPolling();setLoading(false,'message.loadingState');approve.disabled=false;}
            };row.appendChild(approve);
          }catch(error){showMessage(error.message,true);}
          finally{stopProgressPolling();setLoading(false,'message.loadingState');review.disabled=false;}
        };row.appendChild(review);
      }
    }
    panel.appendChild(row);
  }
}
'''

CHECK_LIFECYCLE_TESTS = '''import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from test_update_policy import service as s
import mac_manager as mm
import update_routes as routes
import macports_provider as mp


class CheckLifecycle(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        for name, value in [('STATE_FILE',Path(tmp)/'state.json'),('DATA_DIR',Path(tmp))]:
            self.stack.enter_context(patch.object(s,name,value))
        self.stack.enter_context(patch.object(s,'read_json',return_value={}))
        self.stack.enter_context(patch.object(routes,'read_records',return_value={}))
        self.stack.enter_context(patch.object(mp,'inventory',return_value=[]))
        from update_engine import Engine
        self.stack.enter_context(patch.object(Engine,'catalog',return_value={}))
        self.stack.enter_context(patch.object(Engine,'installed',return_value={}))
        s.reset_cancel_state()
        self.addCleanup(s.reset_cancel_state)

    def scan(self, brew=None):
        return mm.combined(s,brew or (lambda *a,**k: {'packages':[]}),False,True,False)

    def test_new_scan_clears_old_results_and_finishes_all_stages(self):
        s.set_check_progress(running=False,phase='completed',completed_packages=[{'name':'old'}])
        observed=[]
        def brew(*a,**k):
            observed.append(s.read_check_progress())
            return {'packages':[]}
        def ports(*a):
            observed.append(s.read_check_progress())
            return []
        with patch.object(mp,'inventory',side_effect=ports): self.scan(brew)
        self.assertTrue(all(p['running'] for p in observed))
        self.assertEqual(observed[0]['completed_packages'],[])
        self.assertEqual(observed[1]['phase'],'collecting_macports')
        end=s.read_check_progress()
        self.assertEqual(end['phase'],'completed')
        self.assertFalse(end['running'])
        self.assertEqual(end['percent'],100)
        self.assertIsNone(end['current_package'])

    def test_both_provider_failures_are_not_success(self):
        def fail(*a,**k): raise RuntimeError('offline')
        with patch.object(mp,'inventory',side_effect=fail): state=self.scan(fail)
        self.assertTrue(state['partial'])
        self.assertEqual(s.read_check_progress()['phase'],'error')

    def test_cancel_stops_before_next_provider_and_does_not_publish(self):
        def cancel(*a,**k):
            s.mark_cancel_requested()
            raise s.OperationCancelledError('cancel')
        with patch.object(mp,'inventory') as ports, patch.object(s,'atomic_write_json') as save:
            with self.assertRaises(s.OperationCancelledError): self.scan(cancel)
        ports.assert_not_called()
        save.assert_not_called()
        self.assertEqual(s.read_check_progress()['phase'],'canceled')
        self.assertFalse(getattr(mm.SCAN_CONTEXT,'active',False))

    def test_unexpected_save_failure_finishes_progress(self):
        with patch.object(s,'atomic_write_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.scan()
        self.assertEqual(s.read_check_progress()['phase'],'error')
        self.assertFalse(s.read_check_progress()['running'])

    def test_scan_commands_are_cancellable_and_stop_after_cancel(self):
        mm.SCAN_CONTEXT.active=True
        try:
            def command(*a,**kw):
                self.assertTrue(kw['cancellable'])
                s.mark_cancel_requested()
                return s.CommandResult(0,'','')
            with patch.object(s,'_run_cmd_impl',side_effect=command):
                with self.assertRaises(s.OperationCancelledError): s.run_cmd(['/fixture/brew'])
        finally: mm.SCAN_CONTEXT.active=False

    def test_untracked_background_scan_does_not_replace_update_progress(self):
        s.set_check_progress(running=True,phase='updating_packages',current_package='formula:live')
        mm.combined(s,lambda *a,**k: {'packages':[]},False,False,False)
        self.assertEqual(s.read_check_progress()['current_package'],'formula:live')

    def test_planning_commands_honor_cancel_and_release_operation(self):
        for operation in ['update_plan','migration']:
            def command(*a,**kw):
                self.assertTrue(kw['cancellable'])
                s.mark_cancel_requested()
                return s.CommandResult(0,'','')
            with patch.object(s,'_run_cmd_impl',side_effect=command):
                with self.assertRaises(s.OperationCancelledError):
                    s.run_exclusive(operation,lambda: s.run_cmd(['/fixture/tool']))
            self.assertEqual(s.read_check_progress()['phase'],'canceled')
            self.assertFalse(s.read_current_operation()['running'])
            self.assertFalse(getattr(mm.SCAN_CONTEXT,'active',False))

    def test_one_provider_failure_is_partial_not_total_failure(self):
        def fail(*a,**kw): raise RuntimeError('Brew unavailable')
        state=self.scan(fail)
        self.assertTrue(state['partial'])
        self.assertEqual(s.read_check_progress()['phase'],'completed')
        self.assertIn('warnings',s.read_check_progress()['message'])

    def test_real_brew_scanner_does_not_finish_combined_scan_early(self):
        observed=[]
        def ports(*a):
            observed.append(s.read_check_progress())
            return []
        with patch.object(s,'brew_bin',return_value='/fixture/brew'), \\
             patch.object(s,'fetch_outdated_index',return_value={}), \\
             patch.object(s,'fetch_installed_inventory',return_value=[]), \\
             patch.object(s,'append_check_log'),patch.object(mp,'inventory',side_effect=ports):
            self.scan(s.compute_homebrew_snapshot)
        self.assertTrue(observed[0]['running'])
        self.assertIsNone(observed[0]['completed_at'])
        self.assertEqual(observed[0]['phase'],'collecting_macports')

    def test_metadata_enrichment_checks_cancel_between_packages(self):
        def cancel(packages, progress_callback=None):
            s.mark_cancel_requested()
            progress_callback(1,1,{'name':'fixture','kind':'formula'})
        with patch.object(s,'brew_bin',return_value='/fixture/brew'), \\
             patch.object(s,'fetch_outdated_index',return_value={}), \\
             patch.object(s,'fetch_installed_inventory',return_value=[]), \\
             patch.object(s,'append_check_log'),patch.object(s,'enrich_release_dates',side_effect=cancel), \\
             patch.object(mp,'inventory') as ports:
            with self.assertRaises(s.OperationCancelledError):
                mm.combined(s,s.compute_homebrew_snapshot,False,True,True)
        ports.assert_not_called()
        self.assertEqual(s.read_check_progress()['phase'],'canceled')

    def test_normal_check_batches_all_alternatives_without_per_package_requests(self):
        packages=[dict(name=name,kind='formula',provider='homebrew',outdated=True,
                       installed_version='1',latest_version='2') for name in ['first','second']]
        with patch.object(mm.catalog_refresh,'ensure',return_value={'fresh':True,'status':'fresh'}) as catalog, \\
             patch.object(mm.routes,'bottle_blockers',return_value=[{'reason':'source_required'}]), \\
             patch.object(mm.automatic_alternatives,'check',side_effect=lambda s,offers,*args,**kw: offers) as batch:
            mm.combined(s,lambda *a,**kw:{'packages':packages},True,True,False)
        batch.assert_called_once()
        self.assertEqual({o['name'] for o in batch.call_args.args[1]},{'first','second'})
        self.assertTrue(catalog.call_args.kwargs['refresh'])
        self.assertTrue(all(p['update_block_reason']=='source_required' for p in packages))
'''

def manifest():
    file=RECOVERY/'manifest.json'
    return json.loads(file.read_text()) if file.exists() else None

def source(relative):
    old=manifest()
    if old and relative in old['files']:
        return (RECOVERY/'before'/relative).read_text()
    return (ROOT/relative).read_text()

def check_or_apply(apply=False):
    old=manifest()
    outputs=transforms()  # Evaluate every precondition before touching the checkout.
    if old:
        mismatched=[p for p,r in old['files'].items() if not (ROOT/p).is_file() or digest((ROOT/p).read_bytes())!=r['after']]
        if mismatched: raise RuntimeError('Files edited since implementation; update script with a new numbered bundle: '+', '.join(mismatched))
        desired={p:content.encode().replace(b'\n',b'\r\n') if b'\r\n' in (ROOT/p).read_bytes() else content.encode()
                 for p,content in outputs.items() if (ROOT/p).is_file()}
        if all(p in desired and digest(desired[p])==r['after'] for p,r in old['files'].items()) and set(outputs)==set(old['files']):
            print('All numbered bundles already applied; resulting hashes match.'); return True
    print(f'Bundle 001 preconditions passed: {len(outputs)} files.')
    if not apply: return False
    RECOVERY.mkdir(parents=True,exist_ok=True)
    record=old or {'bundle':0,'created_at':time.time(),'files':{}}
    record['bundle']+=1
    prepared=[]
    for relative, content in outputs.items():
        path=ROOT/relative; before=path.read_bytes() if path.exists() else None
        data=content.encode()
        if before and b'\r\n' in before: data=data.replace(b'\n',b'\r\n')
        saved=RECOVERY/'before'/relative
        if before is not None and relative not in record['files']:
            saved.parent.mkdir(parents=True,exist_ok=True); saved.write_bytes(before)
        prior=record['files'].get(relative)
        record['files'][relative]={'before':prior['before'] if prior else digest(before) if before is not None else None,'after':digest(data)}
        if before==data: continue
        prepared.append((path,data))
    # Persist recovery metadata before the first mutation. A partial application
    # can be inspected/restored without discarding pre-existing working changes.
    (RECOVERY/'manifest.json').write_text(json.dumps(record,indent=2))
    for path,data in prepared:
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.mac-manager-')
        with os.fdopen(fd,'wb') as stream: stream.write(data)
        os.chmod(tmp, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        os.replace(tmp,path)
    print('Applied bundle 001; original dirty files preserved at '+str(RECOVERY))
    return True

def run(args, env=None):
    RECOVERY.mkdir(parents=True,exist_ok=True)
    with (RECOVERY/'run.log').open('a') as log:
        offset=log.tell()
        log.write('\n$ '+repr(args)+'\n');log.flush()
        result=subprocess.run(args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=env)
    if result.returncode:
        with (RECOVERY/'run.log').open() as log:
            log.seek(offset); print(log.read()[-14000:])
        raise RuntimeError('Command failed; see '+str(RECOVERY/'run.log'))
    print('PASS: '+' '.join(args[:4]))

def verify():
    if not check_or_apply(): raise RuntimeError('Pending script changes: run apply before verification or rollout')
    run([sys.executable,str(Path(__file__).resolve()),'selftest'])
    run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_*.py'])
    for p in ['electron/main.js','electron/preload.js','renderer/app.js','renderer/mac-manager.js','renderer/i18n.js']:
        run(['node','--check',p])
    run(['node','tests/service-http.cjs'])
    run([str(ROOT/'node_modules/.bin/electron'),'tests/progress-ui.cjs'])

def selftest():
    # Replay the implementation against a copy of the exact pre-existing dirty
    # snapshot, not HEAD, then prove idempotency and lossless source restoration.
    global ROOT, RECOVERY
    actual_root,actual_recovery=ROOT,RECOVERY
    original=manifest()
    if not original: raise RuntimeError('Apply is required before snapshot replay')
    with tempfile.TemporaryDirectory(prefix='mac-manager-replay-') as tmp:
        checkout=Path(tmp)/'checkout';checkout.mkdir()
        for p,r in original['files'].items():
            if r['before'] is not None:
                dest=checkout/p;dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(actual_recovery/'before'/p,dest)
        for p in ['docs/macports-binary-coverage-2026-10-19/root-map.json',
                  'docs/macports-binary-coverage-2026-10-19/probe-research/resolve.tcl']:
            dest=checkout/p;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(actual_root/p,dest)
        ROOT,RECOVERY=checkout,Path(tmp)/'recovery'
        try:
            baseline={str(p.relative_to(checkout)):digest(p.read_bytes()) for p in checkout.rglob('*') if p.is_file()}
            target=checkout/'electron/preload.js';saved=target.read_bytes();target.write_text('unexpected content')
            try: check_or_apply(False)
            except RuntimeError: pass
            else: raise RuntimeError('Replay accepted a mismatched baseline')
            if (RECOVERY/'manifest.json').exists(): raise RuntimeError('Check modified the checkout')
            target.write_bytes(saved)
            check_or_apply(True);check_or_apply(True)
            saved=target.read_bytes();target.write_text('later developer edit')
            try: restore()
            except RuntimeError: pass
            else: raise RuntimeError('Restore overwrote later developer work')
            target.write_bytes(saved);restore()
            after={str(p.relative_to(checkout)):digest(p.read_bytes()) for p in checkout.rglob('*') if p.is_file()}
            if baseline!=after: raise RuntimeError('Snapshot restore was not lossless')
        finally: ROOT,RECOVERY=actual_root,actual_recovery
    print('Snapshot replay, preconditions, idempotency and protected restore: PASS')

def pack():
    verify()
    run([str(ROOT/'node_modules/.bin/electron-builder'),'--dir','--mac','--x64','--publish','never'])

def install():
    pack()
    target=Path('/Applications/Mac Update Manager.app')
    built=ROOT/'dist/mac/Mac Update Manager.app'
    if not built.is_dir(): raise RuntimeError('Expected Intel application bundle missing')
    # Refuse to interrupt package work. Closing the old idle UI also stops its owned service.
    import urllib.request
    interrupted_scans=[]
    for port in range(8765,8796):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=.2) as response: state=json.load(response)
        except Exception: continue
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/progress',timeout=2) as response:
            activity=json.load(response)
        if activity.get('parallel_check',{}).get('running'):
            raise RuntimeError('Active parallel check; installation deferred')
        if state.get('operation',{}).get('running'):
            if state['operation'].get('name')=='initial_snapshot' and state.get('app')=='Mac Update Manager':
                interrupted_scans.append((port,state['project_root']))
            else: raise RuntimeError('Active app operation; installation deferred')
    for app in [target,Path('/Applications/Homebrew Update Manager.app')]:
        if app.exists():
            run(['/usr/bin/osascript','-e',f'if application "{app}" is running then tell application "{app}" to quit'])
    # Only a read-only initial inventory may be stopped for replacement. Never
    # interrupt an update, migration, or privileged package command.
    import signal
    for port,project in interrupted_scans:
        expected=f'{project}/backend/brew-updates-service.py --serve --host 127.0.0.1 --port {port}'
        rows=subprocess.check_output(['/bin/ps','-axo','pid=,command='],text=True).splitlines()
        for row in rows:
            parts=row.strip().split(None,1)
            if len(parts)!=2: continue
            pid,command=parts
            if command.endswith(expected) and command[:-len(expected)].rstrip().endswith('/bin/python3.14'):
                os.kill(int(pid),signal.SIGINT)
                print('Stopped read-only initial inventory for replacement: '+pid)
    for app in [target,Path('/Applications/Homebrew Update Manager.app')]:
        if app.exists():
            dest=RECOVERY/(app.name+'.'+str(time.time_ns()))
            shutil.move(str(app),str(dest));print('Previous app preserved: '+str(dest))
    run(['/usr/bin/ditto',str(built),str(target)])
    refresh_inventory()
    run(['/usr/bin/open',str(target)])
    repair_scheduler()

def repair_scheduler(base=None):
    """Keep the existing schedule while replacing only the renamed app path."""
    import plistlib
    import urllib.request
    if base is None:
        deadline=time.monotonic()+30
        while base is None and time.monotonic()<deadline:
            for port in range(8765,8796):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=.2) as response: state=json.load(response)
                except Exception: continue
                if state.get('protocol')==6 and state.get('app')=='Mac Update Manager':
                    base=f'http://127.0.0.1:{port}';break
            if base is None: time.sleep(.5)
        if base is None: raise RuntimeError('The installed app did not become ready for schedule migration')
    with urllib.request.urlopen(base+'/api/settings',timeout=5) as response: settings=json.load(response)['settings']
    if not settings['scheduler']['enabled']:
        print('Scheduler remains disabled.');return
    path=Path.home()/'Library/LaunchAgents/com.local.brew-outdated-check.plist'
    expected='/Applications/Mac Update Manager.app/Contents/Resources/app.asar.unpacked/backend/brew-updates-service.py'
    before=plistlib.loads(path.read_bytes()) if path.exists() else {}
    if before.get('ProgramArguments',[None,None])[1]==expected:
        print('Scheduled check already points to Mac Update Manager.');return
    if path.exists():
        shutil.copyfile(path,RECOVERY/('launch-agent-before-'+str(time.time_ns())+'.plist'))
    request=urllib.request.Request(base+'/api/settings/scheduler',data=json.dumps({'scheduler':settings['scheduler']}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=30) as response: result=json.load(response)
    after=plistlib.loads(path.read_bytes())
    if not result.get('ok') or after['ProgramArguments'][1]!=expected:
        raise RuntimeError('Scheduled app path verification failed')
    for key in ['StartCalendarInterval','StartInterval','Label','RunAtLoad']:
        if key in before and before.get(key)!=after.get(key):
            raise RuntimeError('Schedule preference changed unexpectedly: '+key)
    with urllib.request.urlopen(base+'/api/settings',timeout=5) as response: actual=json.load(response)['settings']
    if settings!=actual: raise RuntimeError('User settings changed unexpectedly during schedule repair')
    print('Scheduled check path migrated; timing, label and user preferences preserved.')

def refresh_inventory():
    import importlib.util
    source=Path('/Applications/Mac Update Manager.app/Contents/Resources/app.asar.unpacked/backend/brew-updates-service.py')
    spec=importlib.util.spec_from_file_location('rollout_inventory',source)
    service=importlib.util.module_from_spec(spec);sys.modules[spec.name]=service;spec.loader.exec_module(service)
    state=service.compute_snapshot(False,enrich_details=False)
    if state.get('partial'): raise RuntimeError('Installed provider inventory is partial: '+repr(state['errors']))
    print('Installed app inventory verified: '+json.dumps(state['counts']),flush=True)

def pilot():
    verify()
    # Use the installed app's real API and exclusive operation lock, so a
    # separate developer process cannot race the live application's updates.
    import urllib.request
    import urllib.error
    base=None
    for port in range(8765,8796):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=.3) as response: state=json.load(response)
        except Exception: continue
        if state.get('protocol')==6 and state.get('app')=='Mac Update Manager':
            if state.get('operation',{}).get('running'): raise RuntimeError('The app is busy')
            base=f'http://127.0.0.1:{port}';break
    if not base: raise RuntimeError('Install and launch Mac Update Manager before the pilot')
    repair_scheduler(base)
    def post(endpoint,body):
        request=urllib.request.Request(base+endpoint,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=None) as response: return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode()) from error
    p=post('/api/migrations/plan',{'name':'terminal-notifier'})['plan']
    print('Authorized pilot: '+json.dumps({k:p[k] for k in ['name','target','consumers','remove_brew','id']},ensure_ascii=False),flush=True)
    result=post('/api/migrations/approve',{'id':p['id']})
    (RECOVERY/'pilot-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print('Pilot result: '+json.dumps(result['migration'],ensure_ascii=False,indent=2))

def restore():
    old=manifest()
    if not old: raise RuntimeError('No recovery manifest')
    for p,r in old['files'].items():
        path=ROOT/p
        if path.exists() and digest(path.read_bytes()) not in {r['after'],r['before']}:
            raise RuntimeError('Later edit preserved; refusing restore: '+p)
    for p,r in old['files'].items():
        path=ROOT/p
        if r['before'] is None:
            if path.exists(): path.unlink()
        else: shutil.copyfile(RECOVERY/'before'/p,path)
    shutil.move(str(RECOVERY/'manifest.json'),str(RECOVERY/('restored-'+str(time.time_ns())+'.json')))
    print('Original working files restored; app/package installation is not reversed automatically.')

def recover_stalled():
    """Recover only the observed empty-receipt launch failure, never a live install."""
    import signal
    import urllib.request
    data=Path.home()/'Library/Application Support/Homebrew Update Manager'
    journal_path=data/'migration-journal.json'
    journal=json.loads(journal_path.read_text())
    if journal.get('status')!='running' or journal.get('stage')!='install':
        raise RuntimeError('This is not the known stalled launch state')
    if journal.get('plan',{}).get('name')!='terminal-notifier':
        raise RuntimeError('Unexpected migration identity')
    rows=subprocess.check_output(['/bin/ps','-axo','pid=,command='],text=True).splitlines()
    if any('/usr/bin/osascript -e do shell script' in row or '/opt/local/bin/port -N' in row
           or 'worker /private/tmp/mac-manager.' in row for row in rows):
        raise RuntimeError('A privileged process is still live; recovery refused')
    receipts=list(Path('/private/tmp').glob('mac-manager.*'))
    if not receipts or any(list(p.iterdir()) for p in receipts):
        raise RuntimeError('Receipt state changed; inspect before recovery')
    if 'terminal-notifier' in subprocess.check_output(['/opt/local/bin/port','-q','installed','active'],text=True):
        raise RuntimeError('MacPorts installation exists; inspect before recovery')
    with urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3) as response: health=json.load(response)
    expected_root='/Applications/Mac Update Manager.app/Contents/Resources/app.asar.unpacked'
    if health.get('project_root')!=expected_root or health.get('operation',{}).get('name')!='migration':
        raise RuntimeError('Unexpected running service')
    expected=expected_root+'/backend/brew-updates-service.py --serve --host 127.0.0.1 --port 8765'
    matches=[]
    for row in rows:
        parts=row.strip().split(None,1)
        if len(parts)==2 and parts[1].endswith(expected): matches.append(int(parts[0]))
    if len(matches)!=1: raise RuntimeError('Service identity is ambiguous')
    RECOVERY.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(journal_path,RECOVERY/('stalled-migration-'+str(time.time_ns())+'.json'))
    os.kill(matches[0],signal.SIGINT)
    journal.update(status='needs_attention',stage='launch_failed',
                   error='Authorized worker did not start; no MacPorts package was installed. Recovered for retry.')
    journal_path.write_text(json.dumps(journal,ensure_ascii=False,indent=2))
    print('Recovered empty-receipt launch failure; original journal and package backup preserved.')

def restore_removed():
    """Restore only the seven observed autoremove victims, at their original versions."""
    import concurrent.futures
    import re
    import tarfile
    import urllib.request
    # Digests are the Intel Sonoma bottle entries in the official GHCR indexes,
    # checked against those indexes again before downloading. Never use a source
    # tarball or substitute a new formula version to repair this incident.
    targets={
      'c-ares':('1.34.8','38a79d86a4eeab5d3f1fdc017a6d4a1fc19c01d09c34f795f3bde6811eb5c1a5'),
      'cryptography':('48.0.0','ec66c3c7d05699b1d432927369f33945b4c8a2c9c5aafc2fb270d3aeceb6af69'),
      'merve':('1.2.2_1','2b74b54491f8c2f601a5bf649b9c6a94f0e55bce1b7f6e56451b1c7e8a262037'),
      'nbytes':('0.1.4','ee96a893bd236484dfb80e82f772830ce0ffd7f88d6943d6a8bb98f7a79ad65e'),
      'simdjson':('4.6.4','de7e44e78aa7e0272ecbdbdfe84afdff3beda6f68b24da2c62e5f0f2318b4856'),
      'tree':('2.3.2','0d8db41e5e26a0ffd2b7b0df8a8e0dd43a24f458ed34e2bf16352271066fcd74'),
      'uvwasi':('0.0.23','392d42e11570e542d8c92a15e9dee999a54123a4c6b5ce3a62bc8bb9c9f60a31')}
    directory=RECOVERY/'autoremove-repair';directory.mkdir(parents=True,exist_ok=True)
    baseline=json.loads((ROOT/'docs/macports-recovery-2026-10-19/app-state-after-node.json').read_text())
    old={p['name']:p['installed_version'] for p in baseline['packages']}
    if any(old.get(n)!=v for n,(v,_) in targets.items()): raise RuntimeError('Recovery baseline changed')
    present=set(subprocess.check_output(['/usr/local/bin/brew','list','--formula'],text=True).splitlines())
    def fetch(item):
        name,(version,expected)=item
        token=json.load(urllib.request.urlopen(f'https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/{name}:pull',timeout=30))['token']
        def request(path):
            return urllib.request.Request(f'https://ghcr.io/v2/homebrew/core/{name}/'+path,
                headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.oci.image.index.v1+json'})
        with urllib.request.urlopen(request('manifests/'+version),timeout=30) as response:
            raw=response.read()
        index=json.loads(raw)
        entries=[e for e in index['manifests'] if e.get('platform',{}).get('os')=='darwin'
                 and e.get('platform',{}).get('architecture')=='amd64'
                 and e.get('annotations',{}).get('org.opencontainers.image.ref.name')==version+'.sonoma']
        if len(entries)!=1 or entries[0]['annotations']['sh.brew.bottle.digest']!=expected:
            raise RuntimeError('Official archive identity changed: '+name)
        entry=entries[0]
        deps=json.loads(entry['annotations'].get('sh.brew.tab','{}')).get('runtime_dependencies',[])
        if any(d['full_name'] not in present|targets.keys() for d in deps):
            raise RuntimeError('Missing runtime dependency while restoring '+name)
        path=directory/f'{name}--{version}.sonoma.bottle.tar.gz'
        (directory/f'{name}-index.json').write_bytes(raw)
        if not path.exists() or digest(path.read_bytes())!=expected:
            with urllib.request.urlopen(request('blobs/sha256:'+expected),timeout=120) as response:
                with path.open('wb') as stream: shutil.copyfileobj(response,stream)
        if digest(path.read_bytes())!=expected: raise RuntimeError('Archive checksum mismatch: '+name)
        with tarfile.open(path) as archive:
            names=archive.getnames()
            if not any(n.endswith('/.brew/'+name+'.rb') for n in names):
                raise RuntimeError('Archive lacks its original formula: '+name)
            if any(n.startswith('/') or '..' in Path(n).parts or not n.startswith(name+'/') for n in names):
                raise RuntimeError('Archive layout is unexpected: '+name)
        return {'name':name,'version':version,'sha256':expected,'file':str(path),
                'tag':'sonoma','source':'official Homebrew GHCR','runtime_dependencies':deps}
    def fetch_with_retry(item):
        for attempt in range(3):
            try: return fetch(item)
            except (TimeoutError,OSError) as error:
                if attempt==2: raise RuntimeError(f'{item[0]}: {error}') from error
                print(f'Retrying official archive request for {item[0]} ({attempt+1}/2)',flush=True)
                time.sleep(1)
    cached=directory/'archives.json'
    archives=json.loads(cached.read_text()) if cached.exists() else []
    verified=(len(archives)==len(targets) and {r['name'] for r in archives}==set(targets)
        and all((r['version'],r['sha256'])==targets[r['name']]
                and r['file']==str(directory/f"{r['name']}--{r['version']}.sonoma.bottle.tar.gz")
                and Path(r['file']).exists() and digest(Path(r['file']).read_bytes())==r['sha256']
                and all(d['full_name'] in present|targets.keys() for d in r['runtime_dependencies']) for r in archives))
    if not verified:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            archives=list(executor.map(fetch_with_retry,targets.items()))
    (directory/'archives.json').write_text(json.dumps(archives,indent=2))
    print('All seven original Intel binary archives downloaded and SHA-256 verified.',flush=True)
    import urllib.error
    for port in range(8765,8796):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=.2) as response: state=json.load(response)
        except Exception: continue
        if state.get('operation',{}).get('running'): raise RuntimeError('The app is busy; recovery deferred')
    run(['/usr/bin/osascript','-e','if application "/Applications/Mac Update Manager.app" is running then tell application "/Applications/Mac Update Manager.app" to quit'])
    env={**os.environ,'HOMEBREW_NO_AUTOREMOVE':'1','HOMEBREW_NO_INSTALL_CLEANUP':'1',
         'HOMEBREW_NO_AUTO_UPDATE':'1','HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK':'1',
         'HOMEBREW_DEVELOPER':'1','HOMEBREW_NO_INSTALL_UPGRADE':'1'}
    for row in archives:
        installed=subprocess.check_output(['/usr/local/bin/brew','list','--versions',row['name']],text=True).strip() if row['name'] in present else ''
        if installed:
            if installed.split()[1]!=row['version']: raise RuntimeError('An existing installation changed: '+row['name'])
            continue
        run(['/usr/local/bin/brew','install','--formula','--force-bottle','--ignore-dependencies',row['file']],env=env)
        actual=subprocess.check_output(['/usr/local/bin/brew','list','--versions',row['name']],text=True).strip()
        if actual.split()[1]!=row['version']: raise RuntimeError('Restored version mismatch: '+row['name'])
    run(['/usr/local/bin/brew','linkage','--test',*[n for n in targets if n!='merve']],env=env)
    merve=subprocess.run(['/usr/local/bin/brew','linkage','--test','merve'],capture_output=True,text=True,env=env)
    known_issues={}
    if merve.returncode:
        prior=json.loads((ROOT/'docs/audit-2026-10-19/inventory/homebrew-receipts.json').read_text())
        old_merve=next(r for r in prior if r['name']=='merve')
        old_problem=(old_merve['version']=='1.2.2_1' and old_merve['poured_from_bottle']
            and any(d['full_name']=='simdutf' and d['version']=='9.0.0' for d in old_merve['runtime_dependencies'])
            and 'בעיות קישור ב־merve' in (ROOT/'docs/audit-2026-10-19/README.md').read_text()
            and '/usr/local/opt/simdutf/lib/libsimdutf.34.dylib' in merve.stdout)
        if not old_problem: raise RuntimeError('Unexpected linkage problem after restore: '+merve.stdout+merve.stderr)
        known_issues['merve']={'status':'pre-existing linkage problem preserved',
            'output':merve.stdout,'prior_evidence':'docs/audit-2026-10-19/README.md',
            'original_dependency':'simdutf 9.0.0, recorded in pre-migration receipts'}
    (directory/'completed.json').write_text(json.dumps({'restored':{n:v for n,(v,_) in targets.items()},
        'method':'official original-version Intel bottles, no source builds','known_issues':known_issues,
        'completed_at':time.time()},indent=2))
    print('Original seven formula versions restored. Six linkage checks passed; the previously documented merve linkage issue is recorded unchanged.')

TASKS_DOCUMENT = '''# מנהל עדכוני Mac — משימות והמשך עבודה

עודכן: 20 באוקטובר 2026. מצב הפרויקט שנבדק: 1.3.1.

זהו מסמך ההמשך לשיחות אחרות. תיבות לא מסומנות הן עבודה שטרם הושלמה;
מציאת קובץ התקנה אינה אישור שבוצעו התקנה, מעבר או בדיקת שימוש.

## המטרה

מערכת אחת שמזהה את ההתקנות במחשב ה־Mac Intel של המשתמש, בוחרת לכל התקנה
מסלול עדכון מתאים עם כל התלויות, ומנהלת עדכונים בלי בנייה מקוד מקור.
המועמדות צריכות להיבדק אוטומטית; המשתמש אינו צריך לבדוק חבילות אחת־אחת
או להחליט בעצמו מתי מידע מנהל החבילות דורש רענון.

## החלטות שאושרו

- Homebrew, MacPorts ו־Pixi ישמשו יחד כספקים לפי התאמה. ספקים רשמיים עצמאיים
  נשארים אפשרות נוספת; אין כוונה להחליף הכול במנהל יחיד.
- לבדוק חבילות מוכנות ואת כל תלויות ההרצה, בגרסאות ובארכיטקטורה המתאימות.
  אין מעבר אוטומטי לבנייה כשאין מסלול בינארי מלא.
- להשוות חלופה למה שמותקן בפועל. לדוגמה: מותקנת 1, חלופה מוכנה 2,
  ויעד Homebrew הוא 3 — חלופה 2 יכולה להתאים. אין להסיק מכך שהיא הגרסה
  האחרונה הנתמכת ב־Intel. ירידה בגרסה חסומה; גרסה זהה אינה עדכון חדש.
- מעבר בין מנהלים דורש אישור חד־פעמי לפעולה. לאחר מעבר מוצלח,
  הספק החדש מנהל את העדכונים הבאים לפי הקטלוג שלו.
- סקריפטים פרטיים שמפנים לנתיב Homebrew הישן **לא יחסמו מעבר**.
  המשתמש מקבל אחריות לתקן אותם כאשר יתגלו. אין צורך לבקש ממנו אישור נוסף
  רק בגלל אפשרות כזו. תלויות מתועדות, שירותים ונתוני משתמש עדיין מחייבים טיפול.
- לשמר התקנות עצמאיות ונתיבים שכבר נבחרו; להימנע מהתקנות כפולות לא נחוצות.
- `ngrok` נשאר מוחרג מסריקה לעדכון, מתכנון, ממעבר ומבקשות עדכון ישירות.
- כל שינויי הקוד והתיעוד ייכתבו דרך `tools/implement-mac-manager.py`;
  את הסקריפט עצמו עורכים ב־apply_patch. לשמר עבודה קיימת ולבדוק תנאי קדם.
- כל פקודות הטרמינל בסשן אינטראקטיבי. אם נעזרים ב־subagents, לתת משימה
  מוגדרת והקשר מצומצם, ולבחור מודל ומאמץ בהתאם לצורך.

## מה כבר קיים

- [x] אפליקציה בשם Mac Update Manager / מנהל עדכוני Mac, גרסה 1.3.1.
- [x] מלאי משולב של Homebrew, MacPorts וכלים עצמאיים, עם זהות ספק.
- [x] סריקה משותפת של חלופות MacPorts ממופות ובדיקת שרשרת התלויות.
- [x] רענון קטלוג MacPorts לפי מדיניות של 24 שעות. בהרצה ידנית נדרשת
  הזדהות macOS כשצריך; הרצה מתוזמנת אינה פותחת חלון הזדהות unattended.
- [x] השוואת חלופות לגרסה המותקנת, בדיקה חוזרת בתוכנית המעבר,
  וביטול שימוש בהמלצה שמורה אם הגרסה המותקנת או מדיניות ההשוואה השתנו.
- [x] פיילוט מעבר `terminal-notifier` הושלם. זה עדיין המעבר היחיד הנתמך
  לביצוע במנגנון המעבר של MacPorts; אין לסמן את האחרים כמועברים.
- [x] Pixi כבר מנהל את Ansible ואת Pandoc. Node ו־Deno מנוהלים במסלולים
  עצמאיים, ו־uv בהתקנה עצמאית קיימת. יש לאמת את הרישומים לפני שינויים.
- [x] הכפתור הראשי נקרא ״עדכן הכול״. הוא מבצע עדכונים במסלולים נתמכים;
  הוא עדיין אינו מבצע מעברים כלליים בין ספקים.
- [x] בבדיקת גרסה 1.3.1 עברו 61 בדיקות Python, בדיקות ממשק בעברית ובאנגלית
  וסריקה אמיתית. אלה תוצאות היסטוריות, לא תחליף לבדיקת השינוי הבא.

## משימות פתוחות לפי סדר עבודה

### T01 — אמינות המלאי והתצוגה

**מניע:** מסך שמציג טעינה אחרי סיום, או גרסה שאינה זו הפעילה, גורם למשתמש
להסיק מסקנות שגויות ועלול לגרום למתכנן לבחור מסלול על בסיס נתונים לא נכונים.

**תנאי השלמה:** התאמה בין מצב השירות לממשק אחרי הצלחה, כשל, ביטול וריצה
ללא עדכונים; פערי מלאי רלוונטיים מתוקנים או מסומנים כלא מאומתים וחוסמים
רק את הפעולה שתלויה בהם. לבדוק גם את הפעולה האמיתית שהפיקה את צילום המסך.

- [ ] לבדוק ולתקן חיווי ״ממתין״ שנשאר משמאל לכפתור ״רענון סטטוס״ אחרי
  ״התחל בדיקה״: בדיווח המשתמש מ־20.10.2026 נראה שהבדיקה הסתיימה, אך
  החיווי נשאר. לאמת את מצב השירות מול הממשק, לשחזר את התרחיש ולוודא
  שהחיווי מתעדכן בסיום, בכשל ובביטול. סיום הבדיקה בפועל טרם אומת.
- [ ] לתקן את מצב הטעינה שנשאר אחרי פעולת ״עדכן הכול״ שהסתיימה בלי
  התקנות: בצילום מ־19.10.2026 ב־17:06 הופיעו 0%, ״מכין נתונים״ ו״מחשב זמן״,
  אף שהשירות כבר דיווח completed, עם 0 עדכונים ו־26 שנדחו.
- [ ] להציג סיום ברור: כמה עודכנו, כמה דולגו ולמה; אין פעולה פעילה כשאין כזו.
- [ ] לאמת התאמה בין המלאי המדווח, receipt, הקובץ הפעיל וגרסתו בפועל,
  במיוחד לפני מעבר של LLVM. בדוחות הקודמים תועד פער שדורש אימות טרי.
- [ ] להבדיל בין עדכון תוכנה לבין שינוי revision/אריזה; גרסת upstream זהה
  אינה מוכיחה שאין תיקון אריזה חשוב, ואינה מוכיחה התאמה בין שני מנהלים.

### T02 — מיון אוטומטי של מועמדות למעבר

**מניע:** החסימה הכללית הנוכחית מערבבת כלים שאפשר להעביר בתהליך משותף
עם ספריות ושירותים שדורשים טיפול נוסף. רוצים לקדם את הראשונים בלי להמתין לאחרונים.

**תנאי השלמה:** לכל מועמדת יש סיווג, נימוק וראיות שניתנים לבדיקה, עם חסם
מדויק כשהמידע חסר. לוודא שסקריפט פרטי אפשרי אינו הופך לחסם, ושכלי שהוא גם
ספרייה או שירות אינו מסווג אוטומטית כעצמאי רק בגלל שיש לו פקודת CLI.

- [ ] לאסוף תלויות הפוכות של Homebrew, קבצים ופקודות שמותקנים, קישורי
  ספריות, וריאנטים ושירותים. להשתמש במידע זה למיון ראשוני.
- [ ] לסווג: כלי עצמאי מתאים לאוטומציה / ספרייה או סביבת פיתוח משותפת /
  שירות עם הגדרות ונתונים / חסר מידע. אין להציג את המיון כהוכחת שימוש מלאה.
- [ ] להתייחס להיעדר סקריפטים פרטיים כאל עניין שאינו נדרש להוכחה,
  בהתאם לאישור המשתמש; לא להפוך חיפוש כזה לחסם.

### T03 — מנגנון מעבר משותף לכלים עצמאיים

**מניע:** המערכת כבר מוצאת קבצים מוכנים אך אינה מסוגלת לבצע רוב המעברים;
המשתמש נשאר עם רשימת חלופות במקום לקבל עדכון עובד. קוד משותף ופרופילים
קטנים מאפשרים להוסיף כלים בלי להעתיק את הפיילוט כולו לכל חבילה.

**תנאי השלמה:** לפחות כלי נוסף שסווג כמתאים עובר מקצה לקצה דרך המנגנון
המשותף במחשב, ממשיך להיסרק ולהתעדכן אצל הספק החדש אחרי הפעלה מחדש,
ותוצאות כשל וביטול נבדקות. אין סימון הצלחה לפני בדיקת שימוש, ואין הסרת
עותק קודם כשהמעבר נכשל או כשיש תלויים מתועדים שלא טופלו.

- [ ] להוציא את הקוד המשותף מהפיילוט של terminal-notifier למנגנון כללי:
  תוכנית, גיבוי, התקנה בינארית, אימות, הפעלה, ניקוי ורישום ספק חדש.
- [ ] להגדיר לכל כלי פרופיל קטן: ספק ושם חבילה, פקודות ונתיבים, בדיקת גרסה,
  בדיקת שימוש, וקבצי הגדרות/נתונים אם קיימים. לא להסתפק ב־`--version` תמיד.
- [ ] להשלים הפעלה מסודרת דרך הנתיב שנבחר; לשמור ספק קודם פעיל עד אימות.
- [ ] להסיר עותק ישן רק לפי התוכנית ואחרי בדיקת תלויים מתועדים;
  למנוע autoremove או ניקוי גורף בלתי מתוכנן.
- [ ] לבדוק כשל, ביטול, דחיית הרשאה, מעבר חלקי, שחזור והמשך אחרי הפעלה מחדש.
- [ ] להציג תוכנית ואישור חד־פעמי מתוך הממשק. לאחר מעבר, סריקה ועדכון
  רגילים צריכים להמשיך גם בלי עותק Homebrew.

### T04 — Pixi כספק חלופות אוטומטי

**מניע:** תכולת המאגרים וזמינות קבצי Intel שונות בין הספקים. Pixi כבר קיים
במערכת, ובדיקת Poppler הראתה חלופה שהחיפוש המוגבל ל־MacPorts אינו מציע.

**תנאי השלמה:** סריקה רגילה מגלה חלופה דרך Pixi עם גרסת יעד ותלויות,
המשתמש רואה מדוע נבחרה, ופיילוט מתאים מותקן ונבדק דרך מסלול המעבר.
סריקה חוזרת אינה יוצרת התקנה כפולה, ועדכון הבא מסתמך על הספק החדש.
כשל ב־Pixi אינו משבית סריקת Homebrew או MacPorts.

- [ ] להרחיב את חיפוש החלופות ל־Pixi/conda-forge לצד MacPorts,
  מעבר למסלולים הפרטניים שכבר קיימים.
- [ ] למצוא גרסאות osx-64 חדשות מהמותקנת, גם אם אינן יעד Homebrew המדויק;
  לפתור ולנעול את כל התלויות ולבדוק זמינות בינארית לפני התקנה.
- [ ] לשלב בתוכנית העדכון, בהסברי בחירת המסלול ובמנגנון המעבר המשותף.
- [ ] לשמר סביבות ונתיבי משתמש קיימים, ולהציג התקנות מקבילות במקום להסתירן.
- [ ] לגלות גרסאות יעד מהספק שמנהל את הכלי, במקום להסתמך תמיד על Homebrew.

### T05 — מסלולים רשמיים עצמאיים

**מניע:** קובץ Intel רשמי עשוי להיות זמין גם כשאינו זמין במנהלי החבילות;
Nmap הוא מועמד קונקרטי שנמצא בבדיקה. אין צורך להמתין למאגר אם קיים מסלול
רשמי שניתן לנהל ולאמת, אך אין להניח מראש שלכל תוכנה יש עדכון עצמי.

**תנאי השלמה:** מסלול רשמי אחד לפחות עובר גילוי, אימות קובץ, התקנה,
בדיקת שימוש ורישום בעלות; אחרי הפעלה מחדש ניתן לגלות ולעדכן אותו שוב.
יש לבדוק נתיבים, תלויים מתועדים ותאימות למחשב, ולא להסתפק בקישור להורדה.

- [ ] לבדוק מסלול Nmap מהמפיץ: בבדיקה בשיחה נמצא מתקין רשמי
  ל־macOS x86-64 בגרסה 7.992. טרם אומתו התקנה ותפקוד במחשב במסלול זה.
- [ ] להגדיר אימות קובץ/חתימה או checksum, גרסה, הסרה ובעלות על ההתקנה.
- [ ] להשתמש בעדכון עצמי רק כשקיימת פקודה רשמית מתאימה להתקנה המסוימת.
  אין נתון שמבסס הנחה של־95% מהתוכנות יש פקודה כזו.

### T06 — טיפול במועמדות ובחסמים הקיימים

**מניע:** המשתמש צריך פתרונות לחבילות המותקנות אצלו, מעבר למנגנון כללי.
חסם משותף עשוי להסביר כמה חבילות, וחלק מהממצאים הקודמים עלולים להתיישן.

**תנאי השלמה:** לכל אחת מהמועמדות העדכניות יש החלטה עם ראיות ותאריך:
עודכנה או הועברה ונבדקה; אין עדכון חדש במסלול שנבדק; או חסומה מסיבה
מפורשת עם פעולה הבאה אפשרית. אין חובה מלאכותית לעדכן את כל ה־26 אם אין
חלופה מתאימה, ואין לסגור חסם באמצעות בנייה או ירידה בגרסה שלא אושרו.

- [ ] להתחיל מכלים שמתאימים למנגנון הכללי, לפי המיון בפועל, ולא להעביר
  ספריות או שירותים רק מפני שנמצא עבורם קובץ מוכן.
- [ ] לקדם בדיקת שימוש והתקנה מבודדת של Poppler דרך Pixi: בשיחה הצליח
  פתרון תלויות עבור 26.09.0 על osx-64, מול גרסה מותקנת שדווחה כ־26.04.0
  ויעד Homebrew 26.10.0. זו בדיקת lock בלבד; לא בוצעו התקנה ואימות תפקוד.
- [ ] לחקור את החסם המשותף `libunistring 1.5`: הוא זוהה בתוצאות הבדיקה
  עבור gnupg, gpgme, gpgmepp, php ו־tesseract. לבדוק זמינות מחדש או מסלול
  חלופי מלא; לא לערבב ספריות ממנהלים שונים באופן שרירותי.
- [ ] לבדוק מחדש קבצים חסרים של nmap ושל socat, מיפויים לא מאומתים
  ל־libtensorflow ול־merve, וכשל פתרון התלויות של whisper.cpp.
- [ ] להכין תוכניות נפרדות לספריות, לסביבות פיתוח ולשירותים, לרבות נתונים,
  הגדרות, הרחבות, ABI ותוכנות שמשתמשות בהם. ממצאי הקישוריות ההיסטוריים
  של merve ו־LLVM דורשים אימות לפני החלטה.

### T07 — ממשק אחיד ותנאי השלמה

**מניע:** המשתמש אינו אמור להבין קטלוגים, לבדוק כל חבילה בנפרד או לנחש
אם ״נמצאה חלופה״ פירושו שאפשר כבר להתקין אותה. הממשק צריך לשקף יכולת
ביצוע אמיתית ולהבהיר איזו פעולה נדרשת ממנו, אם בכלל.

**תנאי השלמה:** תרחיש רגיל בעברית ובאנגלית עובר מסריקה לתוצאות,
לסקירה ולאישור כשנדרש, לעדכון ולסיכום אמין. זמינות בינארית, מוכנות למעבר
והשלמת התקנה הן מצבים נפרדים. המונים, הכפתורים וההסברים תואמים לתוכנית
ולפעולה בפועל, גם במקרה של ספק כושל, היעדר עדכונים וביטול.

- [ ] ״התחל בדיקה״ בודק את הספקים הרלוונטיים ומציג ליד כל התקנה:
  ניתנת לעדכון / מעבר מוכן לסקירה ולאישור / חסומה עם סיבה מדויקת.
- [ ] לא להציג ״לא קיים״ כשיש רק מיפוי חסר, שגיאת תקשורת או בדיקה חלקית.
- [ ] להראות גרסה מותקנת, יעד הספק שנבחר ויעד Homebrew להשוואה כשנחוץ.
- [ ] ״עדכן הכול״ יעדכן חבילות במסלולים נתמכים ומעברים שכבר אושרו והושלמו;
  לא לבצע מעבר לא מאושר, לא לבנות מקוד מקור ולא לכלול ngrok.
- [ ] להשלים בדיקות רגרסיה, ממשק בשתי השפות ופיילוט אמיתי לכל מסלול חדש;
  לארוז ולהתקין רק לאחר שהבדיקות עברו, בלי לאבד הגדרות והיסטוריה.

## סדר ותלות בין המשימות

- T01 ו־T02 מספקות את בסיס האמינות והמיון. פער במלאי יחסום רק מועמדת
  שתלויה בנתון הבעייתי; אין להפוך אותו לעצירה גורפת של יתר העבודה.
- T03 נשענת על המיון ב־T02 ועל מלאי מאומת עבור כלי הפיילוט.
- אפשר לפתח גילוי חלופות ב־T04 וב־T05 במקביל; ביצוע מעבר בפועל ישתמש
  במנגנון T03. יש לבחור פיילוט ראשון לפי התאמה וראיות, ולא לפי שם מנהל מועדף.
- T06 מתחילה באימות הממצאים ויכולה להתקדם במקביל. העברות בפועל נשענות
  על המסלול שהושלם ב־T03–T05; ספריות ושירותים מקבלים תוכנית נפרדת.
- T07 מיושמת יחד עם כל שינוי התנהגות ומקבלת בדיקת קבלה משולבת בסוף.
- כל משימה יכולה להתפצל לצעדים קטנים. להשאיר תיבה פתוחה כל עוד תנאי
  ההשלמה שלה לא הוכחו; לעדכן ליד משימה שהושלמה תאריך וקישור לראיות ולבדיקות.

## תמונת מצב אחרונה בשיחה — אינה דו״ח חי

דווחו 236 התקנות ו־26 חבילות מיושנות. חלופות MacPorts סווגו כך:

| מצב | מספר |
| --- | ---: |
| זמינות בינארית נמצאה; מעבר טרם מומש עבורן | 8 |
| קובץ חבילה, תלות או חתימה חסר באתרים שנבדקו | 7 |
| אותה גרסת upstream כמו המותקנת | 5 |
| גרסה ישנה מהמותקנת | 3 |
| מיפוי לא מאומת | 2 |
| כשל פתרון תלויות | 1 |

שמונה המועמדות עם זמינות שנמצאה: include-what-you-use, nss, open-mpi,
p11-kit, pango, postgresql@17, py3cairo, unbound. יש לאמת מחדש זמינות
ולמיין שימושים; הרשימה אינה אישור שהן כלים עצמאיים או מוכנות למעבר.

מקורות לבדיקה חוזרת:

- [תיעוד המערכת](MAC_MANAGER.md)
- [דוחות האבחון והמיפוי](audit-2026-10-19/README.md)
- [בדיקת MacPorts הבינארית הקודמת](macports-binary-coverage-2026-10-19/README.md)
- [חבילות Poppler ב־conda-forge](https://anaconda.org/conda-forge/poppler/files)
- [הורדות Nmap הרשמיות](https://nmap.org/download)
- קובצי מצב, הגדרות ויומנים: `~/Library/Application Support/Homebrew Update Manager/`.
- רשומות ספקים: `~/.local/share/brew-update-manager/installed.json`.
- גיבויי המימוש ותוצאות האימות: `~/.local/share/brew-update-manager/recovery/implementation-v2/`.

## איך להמשיך בשיחה אחרת

1. לקרוא מסמך זה ואת MAC_MANAGER.md, הוראות AGENTS החלות ומצב Git העדכני.
2. לאמת גרסה מותקנת, פעולות פעילות, מלאי וממצאים רלוונטיים לפני שינוי.
3. לבחור משימה ממוספרת, לעדכן מצב והחלטות במסמך זה, ולשמור עבודה קיימת.
4. לפני הרצה רחבה של סקריפט המימוש, לבדוק תנאי קדם. בניסיון קודם הסקריפט
   עצר בגלל שינוי README.md מאז הגיבוי; אין לאפס Git או לדרוס את README
   כדי לעקוף זאת. יש להתאים חבילת שינוי לבסיס הנוכחי כשנדרשת עבודת קוד.
5. להבדיל בדיווח בין נמצא במאגר, פתרון תלויות הצליח, קובץ זמין,
   חתימה אומתה, הותקן, נבדק לשימוש, ומעבר הושלם.

אפשר לפתוח שיחה חדשה עם: ״קרא את docs/MAC_MANAGER_TASKS.md והמשך ממשימה T02,
תוך שמירה על ההחלטות וההחרגות המתועדות.״
'''

def document_tasks():
    """Documentation-only transaction; never replays source bundles or README."""
    path = ROOT / 'docs/MAC_MANAGER_TASKS.md'
    journal_path = RECOVERY / 'task-document.json'
    desired = TASKS_DOCUMENT.encode()
    before = path.read_bytes() if path.exists() else None
    if before == desired:
        print('Task document already current: '+str(path))
        return
    record = json.loads(journal_path.read_text()) if journal_path.exists() else {}
    if before is not None and digest(before) != record.get('after'):
        raise RuntimeError('Task document has independent edits; preserve and incorporate them before rewriting')
    RECOVERY.mkdir(parents=True, exist_ok=True)
    if before is not None:
        (RECOVERY / ('task-document.'+str(time.time_ns())+'.md')).write_bytes(before)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.task-document-')
    with os.fdopen(fd, 'wb') as stream: stream.write(desired)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    journal_path.write_text(json.dumps({'path':str(path),'after':digest(desired),'updated_at':time.time()},indent=2))
    if path.read_bytes() != desired: raise RuntimeError('Task document verification failed')
    print('Created and verified: '+str(path))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['check','apply','verify','pack','install','pilot','restore','selftest','recover-stalled','restore-removed','document-tasks'])
    mode=parser.parse_args().mode
    if mode=='check':check_or_apply()
    elif mode=='apply':check_or_apply(True);verify()
    elif mode=='verify':verify()
    elif mode=='pack':pack()
    elif mode=='install':install()
    elif mode=='pilot':pilot()
    elif mode=='restore':restore()
    elif mode=='selftest':selftest()
    elif mode=='recover-stalled':recover_stalled()
    elif mode=='restore-removed':restore_removed()
    elif mode=='document-tasks':document_tasks()

AUTO_ALTERNATIVES_MODULE = r'''"""Read-only, batched MacPorts archive observations for automatic recommendations.

Archive and .sig HEAD availability is not signature, install, or runtime verification.
Only terminal-notifier remains eligible for the separate approved migration route.
"""
from __future__ import annotations
import concurrent.futures as futures
import csv
import hashlib
import json
from pathlib import Path
import platform
import re
import tempfile
import time
import macports_provider as mp

ARCHIVE = re.compile(r'[A-Za-z0-9_.+-]+\Z')
CACHE_NAME = 'automatic-alternatives-cache.json'
TTL = {'available': 6 * 3600, 'missing_from_checked_sites': 30 * 60}

def _rows(directory, name):
    with (Path(directory) / name).open() as stream:
        return list(csv.DictReader(stream, delimiter='\t'))

def _key(row):
    identity = [platform.machine(), platform.release(), row['name'], row['archive'], row['variants']]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()

def check(s, offers, packages, catalog_fresh, report=False):
    """Return annotated offers; attach matching annotations to package inventory rows.

    Resolve all eligible roots once per scan, including scans served by HEAD cache.
    The current resolver graph, exclusions, versions, arch, and installed variants
    must still pass on every scan. No metadata result itself grants install approval.
    """
    checked_at = s.now_iso()
    output = [dict(offer, checked_at=checked_at, archive_count=0,
                   status='requires_live_check', detail=None,
                   executable=bool(offer.get('executable')) and offer.get('name') == 'terminal-notifier') for offer in offers]
    package_by_name = {p['name']: p for p in packages
                       if p.get('provider', 'homebrew') == 'homebrew' and p.get('kind') == 'formula'}
    installed = {p['name']: p for p in packages if p.get('provider') == 'macports'}
    tracked = False
    if report:
        import mac_manager
        tracked = getattr(mac_manager.SCAN_CONTEXT, 'active', False)

    def progress(name, done, total):
        if tracked:
            s.set_check_progress(phase='preflight', current_package=name, done=done,
                total=total, eta_seconds=None,
                message=f'Checking alternative archives ({done}/{total})')

    def mark(offer, status, detail):
        offer.update(status=status, detail=detail)

    pending = []
    for offer in output:
        name = offer.get('name', '')
        offer['homebrew_target'] = package_by_name.get(name, {}).get('latest_version',
            offer.get('homebrew_target', offer.get('target')))
        offer['target'] = offer['homebrew_target']
        roots = offer.get('ports', [])
        if not roots:
            mark(offer, 'mapping_unverified', 'No verified MacPorts package mapping')
        elif not isinstance(roots, list) or any(not isinstance(n, str) or not mp.NAME.fullmatch(n) for n in roots):
            mark(offer, 'mapping_unverified', 'Unsafe or invalid MacPorts package mapping')
        elif name == 'ngrok' or s.is_excluded(name, 'formula') or any(n == 'ngrok' or s.is_excluded(n, 'port') for n in roots):
            mark(offer, 'excluded', 'An excluded package cannot be offered as an alternative')
        elif not catalog_fresh:
            mark(offer, 'catalog_unverified', 'MacPorts catalog freshness could not be verified')
        else:
            pending.append(offer)
    s.raise_if_cancel_requested()
    if pending:
        roots = sorted({root for offer in pending for root in offer['ports']})
        progress('MacPorts metadata', 0, 0)
        try:
            with tempfile.TemporaryDirectory(prefix='mac-manager-auto-resolve-') as directory:
                s.raise_if_cancel_requested()
                mp.checked(s, [mp.TCL, str(mp.ROOT / 'tools/macports-resolve.tcl'), directory, *roots],
                           timeout=300, cancellable=True)
                s.raise_if_cancel_requested()
                node_rows = _rows(directory, 'nodes.tsv')
                result_rows = _rows(directory, 'root-results.tsv')
                closure_rows = _rows(directory, 'root-nodes.tsv')
            nodes = {}
            for row in node_rows:
                mp.valid(row['name'])
                if not ARCHIVE.fullmatch(row['archive']):
                    raise ValueError('Unsafe resolved archive identity')
                if row['archive'] in nodes and nodes[row['archive']] != row:
                    raise ValueError('Conflicting resolved archive identity')
                nodes[row['archive']] = row
            results = {}
            for row in result_rows:
                if row['root'] in results:
                    raise ValueError('Duplicate root resolution result')
                results[row['root']] = row
            closures = {}
            for row in closure_rows:
                if row['archive'] not in nodes or nodes[row['archive']]['name'] != row['name']:
                    raise ValueError('Incomplete resolved archive closure')
                closures.setdefault(row['root'], set()).add(row['archive'])
            probe = mp.load_probe()
            eligible = []
            for offer in pending:
                selected_roots = offer['ports']
                archives = set().union(*(closures.get(root, set()) for root in selected_roots))
                offer['archive_count'] = len(archives)
                rows = [nodes[a] for a in sorted(archives)]
                by_name = {}
                for row in rows:
                    by_name.setdefault(row['name'], []).append(row)
                if (any(results.get(root, {}).get('status') != '0' for root in selected_roots)
                    or not archives or any(len(by_name.get(root, [])) != 1 for root in selected_roots)
                    or any(len(matches) != 1 for matches in by_name.values())
                    or any(set(filter(None, row['requires'].split(','))) - by_name.keys() for row in rows)):
                    mark(offer, 'resolution_failed', 'Complete runtime dependency closure could not be resolved')
                    continue
                # Check every individual root closure as well as the union: a
                # malformed root must not borrow missing nodes from another root.
                complete = True
                for root in selected_roots:
                    local = [nodes[a] for a in closures.get(root, set())]
                    names = {row['name'] for row in local}
                    if root not in names or any(set(filter(None, row['requires'].split(','))) - names for row in local):
                        complete = False
                if not complete:
                    mark(offer, 'resolution_failed', 'A mapped root has an incomplete runtime closure')
                elif any(row['name'] == 'ngrok' or s.is_excluded(row['name'], 'port') for row in rows):
                    mark(offer, 'excluded', 'Runtime closure contains an excluded MacPorts dependency')
                elif any(not ({platform.machine(), 'noarch'} & set(row['arch'].split(','))) for row in rows):
                    mark(offer, 'architecture_unverified', 'Runtime archives do not match this machine architecture')
                elif any(row['name'] in installed and installed[row['name']].get('installed_variants', '') != row['variants'] for row in rows):
                    mark(offer, 'variants_unverified', 'Installed MacPorts variants differ from the resolved defaults')
                else:
                    versions = {root: by_name[root][0]['version'] for root in selected_roots}
                    offer['macports_versions'] = versions
                    target = probe.version(offer['target'])
                    candidates = [probe.version(value) for value in versions.values()]
                    if target is None or any(value is None for value in candidates):
                        mark(offer, 'version_unverified', 'Upstream version comparison could not be verified')
                    elif any(value < target for value in candidates):
                        mark(offer, 'version_gap', 'The mapped MacPorts upstream version is older than the Homebrew target')
                    else:
                        eligible.append((offer, archives))
            required = {a: nodes[a] for _, archives in eligible for a in archives}
            now = time.time()
            cache_path = s.DATA_DIR / CACHE_NAME
            saved = s.read_json(cache_path) or {}
            prior = saved.get('observations', {}) if isinstance(saved, dict) else {}
            if not isinstance(prior, dict):
                prior = {}
            observations, checked = {}, {}
            for archive, row in required.items():
                key = _key(row)
                record = prior.get(key, {})
                if not isinstance(record, dict):
                    continue
                status = record.get('archive_status')
                observed = record.get('observed_at')
                if (status in TTL and isinstance(observed, (float, int))
                    and 0 <= now - observed <= TTL[status]):
                    checked[archive] = status
                    observations[key] = record
            todo = iter(row for archive, row in required.items() if archive not in checked)
            progress(None, len(checked), len(required))
            pool = futures.ThreadPoolExecutor(max_workers=6)
            active = {}
            try:
                def submit():
                    s.raise_if_cancel_requested()
                    row = next(todo, None)
                    if row is not None:
                        active[pool.submit(probe.check_node, row)] = row
                for _ in range(min(6, len(required) - len(checked))):
                    submit()
                while active:
                    s.raise_if_cancel_requested()
                    done, _ = futures.wait(active, timeout=.25, return_when=futures.FIRST_COMPLETED)
                    for future in done:
                        s.raise_if_cancel_requested()
                        row = active.pop(future)
                        try:
                            status = future.result().get('archive_status', 'unknown')
                        except Exception:
                            status = 'unknown'
                        if status not in TTL:
                            status = 'unknown'
                        checked[row['archive']] = status
                        if status in TTL:
                            observations[_key(row)] = dict(archive_status=status, observed_at=time.time())
                        progress(row['name'] + ' / ' + row['archive'], len(checked), len(required))
                        submit()
            finally:
                # Cancellation does not wait for the active, individually bounded
                # HEAD requests; no additional work is queued after cancellation.
                pool.shutdown(wait=False, cancel_futures=True)
            s.raise_if_cancel_requested()
            s.atomic_write_json(cache_path, dict(version=1, observations=observations))
            for offer, archives in eligible:
                statuses = {checked[a] for a in archives}
                if 'missing_from_checked_sites' in statuses:
                    mark(offer, 'archive_missing', 'A runtime archive or signature is absent from the checked official mirrors')
                elif statuses != {'available'}:
                    mark(offer, 'archive_unknown', 'Network or mirror errors prevented verification of the complete runtime closure')
                else:
                    mark(offer, 'binary_available', 'All runtime archives and signatures answered HEAD; installation and integrity remain unverified')
        except s.OperationCancelledError:
            raise
        except Exception as error:
            for offer in pending:
                if offer['status'] == 'requires_live_check':
                    mark(offer, 'resolution_failed', 'Alternative check failed: ' + str(error))
    s.raise_if_cancel_requested()
    for offer in output:
        row = package_by_name.get(offer['name'])
        if row is not None:
            row.update(alternative_status=offer['status'], alternative_detail=offer['detail'])
    return output
'''

AUTO_ALTERNATIVES_TESTS = r'''import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import automatic_alternatives as aa

class Cancelled(Exception):
    pass

class AutomaticAlternativesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {}
        self.cancel = False
        self.calls = []
        self.s = SimpleNamespace(DATA_DIR=Path(self.tmp.name), OperationCancelledError=Cancelled,
            now_iso=lambda: '2026-10-19T00:00:00Z', is_excluded=lambda name, kind: False,
            raise_if_cancel_requested=self.check_cancel,
            read_json=lambda path: self.saved.get(str(path), {}), atomic_write_json=self.save)
        self.nodes = [self.node('first', '1.2', 'shared'), self.node('second', '1.2', 'shared'), self.node('shared')]
        self.closures = {'first': ['first', 'shared'], 'second': ['second', 'shared']}
        self.probe_calls = []
        self.statuses = {}
        self.probe = SimpleNamespace(version=lambda v: None if v is None or not str(v).replace('.', '').isdigit() else tuple(map(int, v.split('.'))),
                                     check_node=self.head)
        self.patches = [patch.object(aa.mp, 'checked', side_effect=self.resolve),
                        patch.object(aa.mp, 'load_probe', return_value=self.probe),
                        patch.object(aa.platform, 'machine', return_value='x86_64'),
                        patch.object(aa.platform, 'release', return_value='23.0')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def node(self, name, version='1.2', requires=''):
        return dict(name=name, version=version, revision='0', variants='', arch='x86_64',
                    archive=name+'-1.2_0.darwin_23.x86_64.tbz2', requires=requires, porturl='file:///metadata')

    def check_cancel(self):
        if self.cancel:
            raise Cancelled()

    def save(self, path, payload):
        self.saved[str(path)] = payload

    def head(self, row):
        self.probe_calls.append(row['name'])
        return dict(row, archive_status=self.statuses.get(row['name'], 'available'))

    def resolve(self, s, args, **kwargs):
        self.calls.append(args[3:])
        directory = Path(args[2])
        def write(name, fields, rows):
            with (directory/name).open('w') as stream:
                writer = csv.DictWriter(stream, fields, delimiter='\t')
                writer.writeheader()
                writer.writerows(rows)
        write('nodes.tsv', list(self.nodes[0]), self.nodes)
        write('root-results.tsv', ['root','status','error'],
              [dict(root=r,status='0',error='') for r in args[3:]])
        by_name = {r['name']:r for r in self.nodes}
        write('root-nodes.tsv', ['root','name','archive'],
              [dict(root=root,name=name,archive=by_name[name]['archive'])
               for root in args[3:] for name in self.closures[root]])

    def run_check(self, roots=('first','second'), fresh=True):
        packages = [dict(name=root, provider='homebrew', kind='formula', latest_version='1.2') for root in roots]
        offers = [dict(name=root,ports=[root]) for root in roots]
        return aa.check(self.s,offers,packages,fresh), packages

    def test_union_resolver_shared_archives_and_exact_cache(self):
        result, packages = self.run_check()
        self.assertEqual(self.calls, [['first','second']])
        self.assertCountEqual(self.probe_calls, ['first','second','shared'])
        self.assertTrue(all(r['status']=='binary_available' and r['archive_count']==2 for r in result))
        self.assertTrue(all(p['alternative_status']=='binary_available' for p in packages))
        self.run_check()
        self.assertEqual(len(self.calls), 2)  # metadata is resolved on every scan
        self.assertEqual(len(self.probe_calls), 3)
        self.nodes[-1]['variants']='+new'
        self.run_check()
        self.assertEqual(self.probe_calls.count('shared'), 2)

    def test_missing_transitive_is_distinct_from_network_unknown(self):
        self.statuses['shared']='missing_from_checked_sites'
        self.assertTrue(all(r['status']=='archive_missing' for r in self.run_check()[0]))
        self.saved.clear()
        self.statuses['shared']='unknown'
        self.assertTrue(all(r['status']=='archive_unknown' for r in self.run_check()[0]))
        self.run_check()
        self.assertEqual(self.probe_calls.count('shared'), 3)  # unknown never cached

    def test_mapping_catalog_and_old_version(self):
        result = aa.check(self.s,[dict(name='other',ports=[])],[],False)
        self.assertEqual(result[0]['status'],'mapping_unverified')
        self.assertEqual(self.run_check(fresh=False)[0][0]['status'],'catalog_unverified')
        self.assertFalse(self.calls)
        self.nodes[0]['version']='1.1'
        self.assertEqual(self.run_check()[0][0]['status'],'version_gap')

    def test_excluded_dependency_and_missing_closure_do_not_probe(self):
        self.s.is_excluded=lambda name,kind: name=='shared'
        self.assertTrue(all(r['status']=='excluded' for r in self.run_check()[0]))
        self.assertFalse(self.probe_calls)
        self.s.is_excluded=lambda *args: False
        self.closures['first']=['first']
        result,_=self.run_check()
        self.assertEqual(result[0]['status'],'resolution_failed')

    def test_cancel_before_resolver_and_on_completion(self):
        self.cancel=True
        with self.assertRaises(Cancelled): self.run_check()
        self.assertFalse(self.calls)
        self.cancel=False
        def cancel_head(row):
            self.cancel=True
            return dict(row,archive_status='available')
        self.probe.check_node=cancel_head
        with self.assertRaises(Cancelled): self.run_check()
        self.assertFalse(self.saved)

    def test_cache_age_host_and_variants_are_bounded(self):
        self.run_check()
        with patch.object(aa.time,'time',return_value=aa.time.time()+21601): self.run_check()
        self.assertEqual(len(self.probe_calls),6)
        with patch.object(aa.platform,'release',return_value='24.0'): self.run_check()
        self.assertEqual(len(self.probe_calls),9)

    def test_missing_observation_expires_after_thirty_minutes(self):
        self.statuses['shared']='missing_from_checked_sites'
        self.run_check()
        self.run_check()
        self.assertEqual(self.probe_calls.count('shared'),1)
        with patch.object(aa.time,'time',return_value=aa.time.time()+1801): self.run_check()
        self.assertEqual(self.probe_calls.count('shared'),2)
        self.assertEqual(self.probe_calls.count('first'),1)

    def test_architecture_and_installed_variant_mismatch(self):
        self.nodes[-1]['arch']='arm64'
        self.assertTrue(all(r['status']=='architecture_unverified' for r in self.run_check()[0]))
        self.assertFalse(self.probe_calls)
        self.nodes[-1]['arch']='noarch'
        packages=[dict(name='first',provider='homebrew',kind='formula',latest_version='1.2'),
                  dict(name='shared',provider='macports',installed_variants='+custom')]
        result=aa.check(self.s,[dict(name='first',ports=['first'])],packages,True)
        self.assertEqual(result[0]['status'],'variants_unverified')

if __name__ == '__main__':
    unittest.main()
'''

# Bundle 002 is deliberately based on the current checkout, including the
# independently edited README. It never replays bundle 001 over that work.
RELIABILITY_BASE = {
    'backend/brew-updates-service.py': '5bc1d525462fb8f4078be5bb95f703020273862d7b3afada22407377ce6bb2e5',
    'backend/mac_manager.py': '228cbe706b79435ebfeb43e9bcb397cbf870c977ce94232d45f75680a5e47026',
    'backend/update_engine.py': '087092557ecd3743162507de3a97f1290c0f0c03f12e037a3c76411f853d130a',
    'renderer/app.js': 'be9fac4823485b004cec05b6431f4c4cae15236f3e0d5b8fbad156a7ee60d638',
    'renderer/mac-manager.js': '1c1291259e55dc97880950f1850cbf623748b9b56dc8c56ebbf1bdac33f639f8',
    'renderer/i18n.js': '6a1db3b27e66c6832bce37b71566e07030c4dfdebef920510bd72719310e19ff',
    'tests/progress-ui.cjs': '00ad43eccbe0fa21d1d853764e6e14ac8a47e5fe241fbf3bf5ff0fb7928ba60a',
    'tests/test_update_policy.py': '34b6a87f5cc9f0aac3c3e90e35f251be0a74415345902d31ffe0149e3d29034d',
    'package.json': 'a8e9be76d9f3a89b86c6bbfa3b10d0d54266162f565e8fc38073aa73d0f0b3b6',
    'package-lock.json': '20226bb15946de020ad8756f8e7e4ccec3f8acd6b7bc2ac7eac0b0f7d5f4c7c2',
    'docs/MAC_MANAGER.md': 'e40655ba8f0106cf4fc9ed52642012a79dc613dd300524168062276757c63105',
    'docs/MAC_MANAGER_TASKS.md': '8e38366e9ff8aed7cfdb328eccbecc58d54b6c0629ede59b0900ba65122e17ca',
    'README.md': '9535c24ddc31d586ec29093aa4b4bb5d914c62597c4e9a8af7a9431faaf44ef5',
}
RELIABILITY_ACCEPTED = True

INSTALLATION_EVIDENCE = r'''"""Bounded local installation evidence; classification never authorizes migration."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time

CONTEXT = threading.local()
POLICY = 'local-evidence-v1'
# Only explicitly known, read-only version probes are executed.
VERSION_PROBES = {'llvm': ('clang', ['--version'], r'clang version (\d+\.\d+\.\d+)')}
DEVELOPMENT_MARKERS = {'node.h', 'Python.h', 'ruby.h', 'jvm.h', 'jni.h', 'rustc', 'llvm-config',
                       'pg_config', 'go', 'gofmt', 'javac', 'clang', 'clang++', 'gcc', 'g++', 'swiftc', 'ghc'}

def reset():
    CONTEXT.inventory = None

def command(s, args, timeout=30):
    s.raise_if_cancel_requested()
    result = s.run_cmd(args, timeout=timeout, cancellable=True,
                       env_override={'HOMEBREW_NO_AUTO_UPDATE':'1', 'HOMEBREW_NO_AUTOREMOVE':'1'})
    s.raise_if_cancel_requested()
    return result

def read_receipt(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None

def versions(version):
    text = str(version or 'unknown')
    match = re.fullmatch(r'(.+?)_(\d+)', text)
    return {'package_version':text, 'upstream_version':match[1] if match else text,
            'revision':int(match[2]) if match else 0}

def fingerprint(audit):
    return hashlib.sha256(json.dumps({k:v for k,v in audit.items() if k!='checked_at'},sort_keys=True).encode()).hexdigest()

def inspect(s, row, prefix, cellar):
    name = row['name']
    entries = row.get('installed') or []
    listed = [str(e['version']) for e in entries if isinstance(e, dict) and e.get('version')]
    base = cellar / name
    opt = prefix / 'opt' / name
    issues, warnings, receipts = [], [], []
    for version in sorted(set(listed) | ({p.name for p in base.iterdir() if p.is_dir()} if base.is_dir() else set())):
        s.raise_if_cancel_requested()
        path = base / version / 'INSTALL_RECEIPT.json'
        data = read_receipt(path)
        receipts.append({'version':version, 'path':str(path), 'present':path.is_file(),
                         'sha256':hashlib.sha256(path.read_bytes()).hexdigest() if data is not None else None,
                         'runtime_dependencies':(data or {}).get('runtime_dependencies'),
                         'used_options':(data or {}).get('used_options', []),
                         'bottle_rebuild':((data or {}).get('source') or {}).get('bottle', {}).get('rebuild')})
    active = None
    if opt.is_symlink() or opt.exists():
        resolved = opt.resolve()
        if resolved.is_dir() and resolved.parent == base.resolve():
            active = resolved
        else:
            issues.append('active_link_missing_or_outside_installation')
    linked = str(row.get('linked_keg') or '')
    if active is None and not issues:
        if linked and (base / linked).is_dir():
            active = base / linked
        elif len(listed) == 1 and (base / listed[0]).is_dir():
            active = base / listed[0]
        else:
            issues.append('active_keg_unverified')
    if active and linked and linked != active.name:
        issues.append('linked_keg_disagrees_with_opt')
    active_receipt = next((r for r in receipts if active and r['version']==active.name), None)
    if active and (not active_receipt or active_receipt['runtime_dependencies'] is None):
        issues.append('active_receipt_missing_or_incomplete')
    if active and active.name not in listed:
        issues.append('active_keg_missing_from_manager_inventory')
    for record in receipts:
        if not record['present'] and (not active or record['version'] != active.name):
            warnings.append('inactive_receipt_missing:'+record['version'])
    value = active.name if active else listed[0] if len(listed)==1 else 'unknown'
    result = dict(status='verified' if not issues else 'unverified', checked_at=s.now_iso(),
                  manager_versions=listed, manager_linked_keg=linked or None,
                  active_keg=str(active) if active else None, opt_path=str(opt),
                  receipts=receipts, issues=issues, warnings=warnings, **versions(value))
    if name in VERSION_PROBES and active:
        executable, args, pattern = VERSION_PROBES[name]
        path = active/'bin'/executable
        resolved = shutil.which(executable)
        result['command_resolution'] = {'name':executable, 'path':resolved,
            'resolved_path':str(Path(resolved).resolve()) if resolved else None,
            'installation_executable':str(path)}
        if path.is_file():
            probe = command(s, [str(path), *args])
            text = probe.stdout + probe.stderr
            match = re.search(pattern, text)
            result['version_probe'] = {'path':str(path), 'code':probe.code,
                                       'version':match[1] if match else None, 'output':text[-2000:]}
            if probe.code or not match:
                result['warnings'].append('version_probe_unavailable')
                result['functional_status']='failed' if probe.code else 'unverified'
            elif match[1] != result['upstream_version']:
                result['status']='conflict'
                result['issues'].append('executable_version_disagrees_with_receipt')
        else:
            result['warnings'].append('version_probe_executable_missing')
    return result

def collect(s, payload):
    prefix_result = command(s, [s.brew_bin(), '--prefix'])
    cellar_result = command(s, [s.brew_bin(), '--cellar'])
    if prefix_result.code or cellar_result.code:
        raise RuntimeError('Homebrew installation roots could not be verified')
    prefix, cellar = Path(prefix_result.stdout.strip()), Path(cellar_result.stdout.strip())
    if not prefix.is_absolute() or not cellar.is_absolute() or not cellar.is_dir():
        raise RuntimeError('Homebrew returned an invalid installation root')
    rows = {r['name']:r for r in payload.get('formulae', []) if isinstance(r,dict) and r.get('name')}
    omitted=[]
    # Reconcile only the Cellar's two installation levels, never user projects.
    for directory in sorted(cellar.iterdir()):
        s.raise_if_cancel_requested()
        if directory.is_dir() and directory.name not in rows:
            installed=[{'version':p.name} for p in directory.iterdir() if p.is_dir() and (p/'INSTALL_RECEIPT.json').is_file()]
            if installed:
                row=dict(name=directory.name,installed=installed,versions={},audit_metadata_missing=True)
                rows[directory.name]=row
                omitted.append(row)
    audits, reverse, gaps = {}, {}, []
    for name, row in rows.items():
        s.raise_if_cancel_requested()
        try:
            audits[name] = inspect(s,row,prefix,cellar)
            active=audits[name]['active_keg']
            for receipt in audits[name]['receipts']:
                if receipt['runtime_dependencies'] is None:
                    if active and receipt['version']==Path(active).name:
                        gaps.append(name+':runtime_dependencies_unavailable')
                for dep in receipt['runtime_dependencies'] or []:
                    key=str(dep.get('full_name') or dep.get('name') or '').split('/')[-1]
                    if key:
                        reverse.setdefault(key,[]).append({'name':name,'source':receipt['path'],'installed_version':receipt['version'],
                            'declared_directly':dep.get('declared_directly'), 'version':dep.get('version')})
            if audits[name]['issues']:
                gaps.extend(name+':'+x for x in audits[name]['issues'])
        except s.OperationCancelledError:
            raise
        except Exception as error:
            audits[name]=dict(status='unverified',package_version='unknown',issues=[str(error)],
                             warnings=[],receipts=[],active_keg=None,checked_at=s.now_iso())
            gaps.append(name+':inventory_unverified')
    for row in payload.get('casks',[]):
        for dependency in (row.get('depends_on') or {}).get('formula',[]):
            reverse.setdefault(dependency.split('/')[-1],[]).append(dict(name=row.get('token'),kind='cask',
                source='current_cask_dependency_metadata',installed_version=row.get('installed')))
    CONTEXT.inventory = dict(rows=rows,audits=audits,reverse=reverse,gaps=gaps,
                             prefix=str(prefix),cellar=str(cellar),omitted=omitted)
    return CONTEXT.inventory

def inventory_fields(s, row, context):
    audit = context['audits'].get(row['name']) if context else None
    if not audit:
        audit=dict(status='unverified',package_version='unknown',issues=['inventory_evidence_unavailable'],checked_at=s.now_iso())
    fields=dict(installed_version=audit['package_version'], inventory_verification=audit,
                manager_installed_versions=[str(x.get('version')) for x in row.get('installed',[]) if isinstance(x,dict)],
                installed_upstream_version=audit.get('upstream_version'),installed_revision=audit.get('revision'),
                latest_upstream_version=(row.get('versions') or {}).get('stable'),latest_revision=row.get('revision'))
    if audit['status'] != 'verified' or row.get('audit_metadata_missing'):
        fields.update(inventory_stale=True,update_block_reason='inventory_unavailable')
    if audit.get('functional_status')=='failed':
        fields.update(inventory_stale=True,update_block_reason='inventory_unavailable')
    return fields

def verified_dependencies(s, payload):
    context=getattr(CONTEXT,'inventory',None)
    if context is None:context=collect(s,payload)
    result={}
    for name,audit in context['audits'].items():
        valid=[r for r in audit.get('receipts',[]) if r['present'] and r['runtime_dependencies'] is not None]
        result[name]={'versions':[r['version'] for r in valid],
                      'options':[x for r in valid for x in r.get('used_options',[])]}
    return result

def file_evidence(s, root):
    evidence=dict(commands=[],libraries=[],headers=[],pkg_config=[],development=[],mach_o=[],
                  formula_source=None,service_definition=None,config_paths=[],data_paths=[],gaps=[])
    started=time.monotonic()
    scanned=0
    # Bounded installation tree only. Do not follow directory symlinks.
    for directory, dirs, files in os.walk(root, followlinks=False,
            onerror=lambda error:evidence['gaps'].append('installation_directory_unreadable:'+str(error))):
        s.raise_if_cancel_requested()
        if time.monotonic()-started > 20 or scanned > 20000:
            evidence['gaps'].append('installation_file_scan_limit')
            break
        relative=Path(directory).relative_to(root)
        for name in dirs:
            path=Path(directory)/name
            if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
                evidence['gaps'].append('external_directory_link:'+str(path.relative_to(root)))
        if relative.parts and relative.parts[0] in {'share','libexec'}:
            dirs[:]=[d for d in dirs if not d.startswith('.')]
        for name in files:
            scanned+=1
            if scanned % 64 == 0: s.raise_if_cancel_requested()
            path=Path(directory)/name
            rel=str(path.relative_to(root))
            if name in DEVELOPMENT_MARKERS and (name.endswith('.h') or os.access(path,os.X_OK)):
                evidence['development'].append(rel)
            if relative.parts and relative.parts[0] in {'bin','sbin'} and os.access(path,os.X_OK):
                evidence['commands'].append(str(path))
            if name.endswith(('.dylib','.a')) or re.search(r'\.so(?:\.\d+)*$',name):
                # Bundled private libraries do not imply an exported library API.
                if relative.parts and relative.parts[0]=='lib': evidence['libraries'].append(rel)
            if relative.parts and relative.parts[0]=='include': evidence['headers'].append(rel)
            if name.endswith('.pc'): evidence['pkg_config'].append(rel)
    formula_dir=root/'.brew'
    formula_files=sorted(formula_dir.glob('*.rb')) if formula_dir.is_dir() else []
    if len(formula_files)==1 and formula_files[0].resolve().is_relative_to(root.resolve()):
        path=formula_files[0]
        text=path.read_text(errors='replace')
        evidence['formula_source']=str(path)
        evidence['formula_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        evidence['service_definition']=bool(re.search(r'^\s*service\s+do\b',text,re.M))
        # Only extract literal, formula-owned locations; do not evaluate Ruby.
        for kind,pattern in [('config_paths',r'\betc\s*/\s*"([^"\n]+)"'),('data_paths',r'\bvar\s*/\s*"([^"\n]+)"')]:
            evidence[kind]=sorted(set(re.findall(pattern,text)))
    else:
        evidence['gaps'].append('installed_formula_service_metadata_unavailable')
    binaries=[]
    for raw in evidence['commands']:
        path=Path(raw)
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                evidence['gaps'].append('external_command_target:'+str(path))
                continue
            with path.open('rb') as stream: magic=stream.read(4)
            if magic in {b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca'}:
                binaries.append(path)
        except OSError as error:
            evidence['gaps'].append('executable_unreadable:'+str(path))
    for path in binaries[:24]:
        if time.monotonic()-started>30:
            evidence['gaps'].append('linkage_inspection_time_limit')
            break
        result=command(s,['/usr/bin/otool','-L',str(path)],timeout=10)
        evidence['mach_o'].append(dict(path=str(path),code=result.code,output=result.stdout[-12000:]))
        if result.code: evidence['gaps'].append('linkage_inspection_failed:'+str(path))
    if len(binaries)>24: evidence['gaps'].append('linkage_inspection_limit')
    return evidence

def classify(evidence):
    roles=[]
    files=evidence.get('files',{})
    if files.get('service_definition') or evidence.get('service'):
        roles.append('service')
    if evidence.get('consumers') or files.get('libraries') or files.get('headers') or files.get('pkg_config') or files.get('development'):
        roles.append('shared_environment')
    gaps=list(dict.fromkeys(evidence.get('gaps',[])+files.get('gaps',[])))
    audit=evidence.get('inventory',{})
    if audit.get('status')!='verified': gaps.append('installation_unverified')
    if audit.get('functional_status')=='failed': gaps.append('known_command_probe_failed')
    if not roles and not files.get('commands'): gaps.append('no_verified_command')
    # Positive roles survive incomplete evidence; automation always requires completeness.
    category=roles[0] if roles else 'insufficient_information' if gaps else 'standalone_candidate'
    if category=='standalone_candidate': roles.append('standalone')
    reasons=[]
    if 'service' in roles: reasons.append('service_definition_or_registration')
    if evidence.get('consumers'): reasons.append('documented_installed_consumers')
    if any(files.get(k) for k in ('libraries','headers','pkg_config')): reasons.append('exported_library_interface')
    if files.get('development'): reasons.append('development_environment_interface')
    if category=='standalone_candidate': reasons.append('complete_local_evidence_without_shared_roles')
    if gaps: reasons.append('required_evidence_incomplete')
    return dict(category=category,roles=roles,reasons=reasons,consumers=evidence.get('consumers',[]),
                gaps=sorted(set(gaps)),automation_candidate=category=='standalone_candidate',policy=POLICY)

def annotate(s, offers, packages, refresh, report=False):
    context=getattr(CONTEXT,'inventory',None)
    previous={o['name']:o for o in (s.read_json(s.STATE_FILE) or {}).get('migrations',[])}
    by_name={p['name']:p for p in packages if p.get('provider')=='homebrew' and p.get('kind')=='formula'}
    services=None
    if refresh and context and offers:
        try:
            result=command(s,[s.brew_bin(),'services','list','--json'],timeout=45)
            services=json.loads(result.stdout) if not result.code else None
            if not isinstance(services,list): services=None
        except s.OperationCancelledError: raise
        except Exception: services=None
    for offer in offers:
        s.raise_if_cancel_requested()
        name=offer['name']
        if name=='ngrok' or s.is_excluded(name,'formula'): continue
        if report:
            s.set_check_progress(current_package=name,message='Checking installation role: '+name,eta_seconds=None)
        pkg=by_name.get(name,{})
        audit=pkg.get('inventory_verification',{})
        if not refresh:
            prior=previous.get(name,{})
            if prior.get('classification',{}).get('inventory_fingerprint')==fingerprint(audit):
                offer['classification']=prior['classification']
            else:
                offer['classification']=dict(category='unclassified',roles=[],gaps=['fresh_classification_required'],reasons=[],consumers=[])
            continue
        evidence=dict(name=name,installation_id=pkg.get('installation_id'),checked_at=s.now_iso(),
                      inventory=audit,consumers=(context or {}).get('reverse',{}).get(name,[]),
                      gaps=[],service=None)
        try:
            if context is None:
                evidence['gaps'].append('installed_dependency_graph_unavailable')
            else:
                evidence['gaps'].extend(context['gaps'])
                root=audit.get('active_keg')
                if root: evidence['files']=file_evidence(s,Path(root))
                row=context['rows'].get(name,{})
                evidence['options']=[r.get('used_options',[]) for r in audit.get('receipts',[])]
                evidence['variants']=row.get('used_options',[])
                if services is None: evidence['gaps'].append('service_registry_unavailable')
                else: evidence['service']=next((r for r in services if r.get('name')==name),None)
                # A Homebrew service row may merely describe a stopped service.
                files=evidence.get('files',{})
                prefix=Path(context['prefix'])
                evidence['owned_paths']=[dict(path=str(prefix/base/value),exists=(prefix/base/value).exists())
                    for key,base in [('config_paths','etc'),('data_paths','var')] for value in files.get(key,[])
                    if not Path(value).is_absolute() and '..' not in Path(value).parts]
            classification=classify(evidence)
        except s.OperationCancelledError: raise
        except Exception as error:
            evidence['gaps'].append('collection_failed:'+str(error))
            classification=classify(evidence)
        inventory_fingerprint=fingerprint(audit)
        data=json.dumps(evidence,sort_keys=True,ensure_ascii=False).encode()
        key=hashlib.sha256(data).hexdigest()
        path=s.DATA_DIR/'migration-evidence'/(key+'.json')
        s.raise_if_cancel_requested()
        references=[]
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            s.atomic_write_json(path,evidence)
            references=[str(path)]
        except OSError as error:
            classification['gaps'].append('evidence_persistence_failed:'+str(error))
            classification['automation_candidate']=False
            if classification['category']=='standalone_candidate':classification['category']='insufficient_information'
        classification.update(checked_at=evidence['checked_at'],inventory_fingerprint=inventory_fingerprint,
                              evidence_refs=references)
        offer['classification']=classification
    return offers
'''

RUNTIME_UI = r'''
// Operation presentation is independent of the busy overlay.
let retainedOperationProgress = null;
let operationStatusUnverified = false;
let operationRequestGeneration = 0;
let operationStartedFloor = 0;
let runtimeRefreshGeneration = 0;

function runtimeText(he, en) { return i18n.lang === 'he' ? he : en; }

function presentOperationStatus() {
  const p = retainedOperationProgress;
  const terminal = p && !p.running && ['completed','error','canceled'].includes(p.phase);
  const reportStatus = terminal ? p.phase === 'error' ? 'failed' : p.phase === 'canceled' ? 'canceled'
    : p.partial || p.error || (p.deferred_packages || []).length ? 'warning' : 'completed' : 'idle';
  let status = p?.running || lastKnownParallelCheckRunning || (busyLoading && !terminal) ? 'running' : terminal ? reportStatus : 'idle';
  if (operationStatusUnverified) status = 'unverified';
  const labels = {
    idle:i18n.t('actions.statusIdle'),running:i18n.t('actions.statusRunning'),
    completed:i18n.t('actions.statusCompleted'),failed:i18n.t('actions.statusFailed'),
    warning:runtimeText('הושלם עם אזהרות','Completed with warnings'),
    canceled:runtimeText('בוטל','Canceled'),unverified:runtimeText('לא ניתן לאמת סטטוס','Status could not be verified'),
  };
  operationStatusBadgeEl.removeAttribute('data-i18n');
  operationStatusBadgeEl.className = `status-badge ${status === 'warning' || status === 'unverified' ? 'idle' : status === 'failed' ? 'error' : status === 'canceled' ? 'completed' : status}`;
  operationStatusBadgeEl.dataset.status = status;
  operationStatusBadgeEl.textContent = labels[status];
  // A completed report has its own persistent panel, never an active loader.
  if (loaderEl) loaderEl.classList.toggle('visible', !terminal && (busyLoading || !!p?.running) && !operationStatusUnverified);
  let panel = document.getElementById('operationResultSummary');
  if (!panel) {
    panel = document.createElement('section'); panel.id = 'operationResultSummary'; panel.className = 'panel';
    panel.style.padding = '16px'; panel.style.marginBlock = '12px'; panel.setAttribute('aria-live','polite');
    document.querySelector('.tabs').before(panel);
  }
  panel.hidden = !terminal;
  if (!terminal) return;
  panel.replaceChildren();
  const heading = document.createElement('strong'); heading.textContent = labels[reportStatus]; panel.appendChild(heading);
  if (String(p.operation_name || '').startsWith('update')) {
    const items = p.completed_packages || [];
    const skipped = items.filter(x => x.skipped);
    const updated = items.filter(x => x.ok && !x.skipped);
    const failed = items.filter(x => !x.ok && !x.skipped && !x.canceled);
    const canceled = items.filter(x => x.canceled);
    const deferred = p.deferred_packages || [];
    const remaining = p.remaining_packages || [];
    const text = document.createElement('p');
    text.textContent = runtimeText(
      `עודכנו: ${updated.length} · נכשלו: ${failed.length} · דולגו: ${skipped.length + deferred.length} · הופסקו: ${canceled.length} · לא בוצעו: ${remaining.length}`,
      `Updated: ${updated.length} · Failed: ${failed.length} · Skipped: ${skipped.length + deferred.length} · Canceled: ${canceled.length} · Not attempted: ${remaining.length}`);
    panel.appendChild(text);
    const list = document.createElement('ul');
    for (const item of [...deferred,...skipped,...failed,...canceled,...remaining]) {
      const line = document.createElement('li');
      const reason = item.reason || (item.canceled ? 'canceled' : !item.ok && !remaining.includes(item) ? 'failed' : 'not_attempted');
      line.textContent = `${item.name || '—'}: ${migrationReasonLabel(reason)}`;
      if (item.blockers?.length) line.textContent += ' · ' + item.blockers.map(b=>`${b.name}: ${migrationReasonLabel(b.reason)}`).join(', ');
      list.appendChild(line);
    }
    if (list.children.length) panel.appendChild(list);
  } else {
    const text = document.createElement('p');
    text.textContent = p.phase === 'completed' ? i18n.t(p.partial ? 'message.checkPartial' : 'message.checkSuccess')
      : p.phase === 'canceled' ? i18n.t('message.checkCanceled') : (p.error || i18n.t('actions.statusFailed'));
    panel.appendChild(text);
  }
}

function acceptOperationProgress(progress, authoritative = false) {
  if (!progress) return false;
  const stamp = Date.parse(progress.started_at || '') || 0;
  const previousStamp = Date.parse(retainedOperationProgress?.started_at || '') || 0;
  if (!authoritative && stamp && stamp < operationStartedFloor) return false;
  if (stamp && previousStamp && stamp < previousStamp) return false;
  if (!authoritative && retainedOperationProgress && !retainedOperationProgress.running
      && ['completed','error','canceled'].includes(retainedOperationProgress.phase)
      && progress.running && stamp && stamp === previousStamp) return false;
  if (!stamp && retainedOperationProgress?.started_at && progress.phase === 'idle') return false;
  retainedOperationProgress = progress;
  operationStatusUnverified = false;
  lastKnownOperationRunning = !!progress.running;
  lastKnownOperationName = String(progress.operation_name || '').toLowerCase();
  presentOperationStatus();
  applyActionAvailability();
  return true;
}

function beginOperationRequest() {
  operationRequestGeneration += 1;
  runtimeRefreshGeneration += 1;
  operationStartedFloor = Date.now();
  retainedOperationProgress = null;
  operationStatusUnverified = false;
  presentOperationStatus();
}

function statusUnavailable() {
  operationStatusUnverified = true;
  presentOperationStatus();
}

async function finishOperationRequest(response, generation) {
  if (generation !== operationRequestGeneration) return;
  // The response progress was captured while the backend still held its lock.
  if (response?.progress) {
    if (acceptOperationProgress(response.progress, true)) {
      renderCheckProgress(response.progress);
      if (!response.progress.running && !lastKnownParallelCheckRunning) stopProgressPolling();
      return;
    }
  }
  try {
    const payload = await window.brewApp.getProgress();
    if (generation !== operationRequestGeneration) return;
    if (!payload?.progress || !acceptOperationProgress(payload.progress)) throw new Error('Unverified operation status');
    renderCheckProgress(payload.progress);
    await syncParallelCheckState(payload.parallel_check, {showFeedback:false});
    if (!payload.progress.running && !payload.parallel_check?.running) stopProgressPolling();
  } catch {
    if (generation !== operationRequestGeneration) return;
    statusUnavailable();
    if (!progressPollTimer) startProgressPolling();
  }
}

function migrationReasonLabel(reason) {
  const labels = {
    source_required:['אין מסלול בינארי מלא','No complete binary route'],
    inventory_unavailable:['נדרש אימות ההתקנה','Installation verification required'],
    binary_route_unavailable:['המסלול הבינארי אינו מאומת','Binary route is unverified'],
    excluded:['מוחרג','Excluded'],not_outdated:['כבר מעודכן','Already current'],
    canceled:['בוטל','Canceled'],failed:['נכשל','Failed'],not_attempted:['לא בוצע','Not attempted'],
    service_definition_or_registration:['הוגדר שירות','Service definition or registration'],
    documented_installed_consumers:['נמצאו תלויים מותקנים','Installed consumers found'],
    exported_library_interface:['ממשק ספרייה משותף','Exported library interface'],
    development_environment_interface:['ממשק סביבת פיתוח','Development environment interface'],
    complete_local_evidence_without_shared_roles:['הראיות המקומיות הושלמו ללא תפקיד משותף','Local evidence complete without shared roles'],
    required_evidence_incomplete:['חסרות ראיות נדרשות','Required evidence is incomplete'],
  };
  return (labels[reason] || [reason,reason])[i18n.lang==='he'?0:1];
}
'''

def reliability_outputs(read):
    out = {'backend/installation_evidence.py': INSTALLATION_EVIDENCE}
    p='backend/brew-updates-service.py';t=read(p)
    t=replace(t,'import mac_manager','import mac_manager\nimport installation_evidence\nimport copy')
    t=replace(t,'def normalize_stale_progress_state(operation: dict[str, Any] | None = None) -> dict[str, Any]:', '''def runtime_status_payload():
    # Same lock order as operation startup: ownership first, then progress.
    # A stale caller snapshot must never terminate a newly started operation.
    with CURRENT_OPERATION_LOCK:
        operation = dict(CURRENT_OPERATION)
        progress = _normalize_stale_progress_locked(operation)
    return {'ok':True,'operation':operation,'progress':progress,'parallel_check':read_parallel_check_state()}


def normalize_stale_progress_state(operation: dict[str, Any] | None = None) -> dict[str, Any]:
    return runtime_status_payload()['progress']


def _normalize_stale_progress_locked(operation: dict[str, Any]) -> dict[str, Any]:''')
    t=replace(t,'        canceled=bool(progress.get("canceled")) or interrupted,',
        '        current_package=None,\n        canceled=bool(progress.get("canceled")),')
    t=replace(t,'''        if self.path == "/api/progress":
            operation = read_current_operation()
            progress = normalize_stale_progress_state(operation)
            self._send_json(
                200,
                {
                    "ok": True,
                    "progress": progress,
                    "operation": operation,
                    "parallel_check": read_parallel_check_state(),
                },
            )''', '''        if self.path == "/api/progress":
            self._send_json(200, runtime_status_payload())''')
    t=replace(t,'    payload = json.loads(res.stdout or "{}")\n    packages: list[dict[str, Any]] = []', '''    payload = json.loads(res.stdout or "{}")
    try:
        evidence_context = installation_evidence.collect(sys.modules[__name__], payload)
        payload.setdefault('formulae', []).extend(evidence_context['omitted'])
    except OperationCancelledError:
        raise
    except Exception as error:
        evidence_context = None
        installation_evidence.reset()
        append_service_log('Installation evidence unavailable: ' + str(error))
    packages: list[dict[str, Any]] = []''')
    t=replace(t,'        installed_version = installed_versions[-1] if installed_versions else str(row.get("linked_keg") or "unknown")',
        '''        installation_fields = installation_evidence.inventory_fields(sys.modules[__name__], row, evidence_context)
        installed_version = installation_fields['installed_version']''')
    # Casks keep their own inventory semantics; only change the formula block.
    start=t.index('def fetch_installed_inventory(');end=t.index('    for row in payload.get("casks", [])',start)
    block=t[start:end]
    block=replace(block,'            installed_version = outdated_meta.get("installed_version") or installed_version',
        "            installation_fields['manager_outdated_version'] = outdated_meta.get('installed_version')")
    block=replace(block,'                "kind": "formula",','                "kind": "formula",\n                **installation_fields,')
    t=t[:start]+block+t[end:]
    marker='def run_exclusive(op_name: str, fn):'
    wrapper='''def run_with_progress(op_name, fn, *, snapshot=False):
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
        return {'ok':True,'snapshot':value,'progress':progress} if snapshot else {**value,'progress':progress}
    return run_exclusive(op_name, operation)


'''
    t=replace(t,marker,wrapper+marker)
    t=replace(t,'''                snapshot = run_exclusive(
                    "check_now",
                    lambda: compute_snapshot(run_brew_update=True, track_progress=True),
                )
                self._send_json(200, {"ok": True, "snapshot": snapshot})''', '''                payload = run_with_progress(
                    "check_now",
                    lambda: compute_snapshot(run_brew_update=True, track_progress=True), snapshot=True,
                )
                self._send_json(200, payload)''')
    t=replace(t,'payload = run_exclusive(f"update_one:{kind}:{name}", _operation)', 'payload = run_with_progress(f"update_one:{kind}:{name}", _operation)')
    t=replace(t,'payload = run_exclusive("update_all", lambda: run_update_all(track_progress=True))', 'payload = run_with_progress("update_all", lambda: run_update_all(track_progress=True))')
    t=replace(t,'payload = run_exclusive(\n                    f"update_selected:', 'payload = run_with_progress(\n                    f"update_selected:')
    for error_name in ['check_failed','update_failed','update_all_failed','update_selected_failed']:
        old='self._send_json(500, {"ok": False, "error": "'+error_name+'", "detail": str(exc)})'
        new='self._send_json(500, {"ok": False, "error": "'+error_name+'", "detail": str(exc), "progress": getattr(exc, "operation_progress", None)})'
        t=replace(t,old,new)
    # Store the last terminal result for refresh/reload as well as HTTP response.
    t=replace(t,"        return {'ok':True,'snapshot':value,'progress':progress} if snapshot else {**value,'progress':progress}",
        "        with CHECK_PROGRESS_LOCK:\n            CHECK_PROGRESS['partial']=progress['partial']\n        return {'ok':True,'snapshot':value,'progress':progress} if snapshot else {**value,'progress':progress}")
    out[p]=t
    p='backend/mac_manager.py';t=read(p)
    t=replace(t,'import automatic_alternatives','import automatic_alternatives\nimport installation_evidence')
    t=replace(t,'    previous = s.read_json(s.STATE_FILE) or {}','    installation_evidence.reset()\n    previous = s.read_json(s.STATE_FILE) or {}')
    t=replace(t,"    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')",'''    offers = installation_evidence.annotate(s, offers, packages, refresh, report=track_progress)
    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')''')
    # Revalidate installation evidence when planning the already-supported pilot.
    t=replace(t,"    if not pkg or pkg.get('brew_installation_present') is False:","    if pkg and (pkg.get('inventory_stale') or pkg.get('inventory_verification', {}).get('status', 'verified') != 'verified'):\n        raise ValueError('Installation evidence is unverified; scan and resolve the reported inventory gap')\n    if not pkg or pkg.get('brew_installation_present') is False:")
    out[p]=t
    p='backend/update_engine.py';t=read(p)
    t=replace(t,'import macports_provider','import macports_provider\nimport installation_evidence')
    t=replace(t,'''        return {row["name"]: {"versions": [x["version"] for x in row.get("installed", [])],
                              "options": [option for x in row.get("installed", []) for option in x.get("used_options", [])]}
                for row in json.loads(result.stdout).get("formulae", [])}''',
        '''        return installation_evidence.verified_dependencies(self.s, json.loads(result.stdout))''')
    t=replace(t,'"deferred_count": len(deferred), "remaining_count": len(remaining), "results": results,','"deferred_count": len(deferred), "skipped_count": sum(bool(r.get("skipped")) for r in results) + len(deferred),\n                "remaining_count": len(remaining), "results": results,')
    out[p]=t
    p='renderer/app.js';t=read(p)
    t=replace(t,'function setLoading(on, messageKey',RUNTIME_UI+'\nfunction setLoading(on, messageKey')
    t=replace(t,"  loaderTextEl.textContent = i18n.t(messageKey, params);","  if (on) loaderTextEl.textContent = i18n.t(messageKey, params);")
    t=replace(t,"  if (!on && !lastKnownOperationRunning) {\n    resetLoaderProgress();\n    setOperationStatusBadge('idle');\n  }","  presentOperationStatus();")
    t=replace(t,"function renderCheckProgress(progress) {\n  if (!progress || !loaderEl) {", "function renderCheckProgress(progress) {\n  if (!progress || !acceptOperationProgress(progress) || !loaderEl) {")
    t=replace(t,"  if (progress.running || (String(progress.operation_name || '').startsWith('update') &&\n      ((progress.planned_packages || []).length || (progress.deferred_packages || []).length))) {", "  if (progress.running) {")
    t=replace(t,"  if (!loaderEl.classList.contains('visible') && !busyLoading) {\n    return;\n  }", "  if (!progress.running) {\n    renderUpdateLivePanel(progress);\n    if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = '—';\n    if (loaderEtaValueEl) loaderEtaValueEl.textContent = progress.phase === 'completed' ? i18n.t('progress.doneNow') : '—';\n    if (loaderCancelBtn) loaderCancelBtn.disabled = true;\n    presentOperationStatus();\n    return;\n  }")
    # Old status writers delegate to the same retained operation presentation.
    start=t.index('function setOperationStatusBadge(status) {');end=t.index('\nasync function refreshRuntimeStatus',start)
    t=t[:start]+'''function setOperationStatusBadge(status) {
  presentOperationStatus();
}
'''+t[end:]
    t=replace(t,"  const operationRunning = !!lastKnownOperationRunning;", "  const operationRunning = !!lastKnownOperationRunning || operationStatusUnverified;", count=2)
    # Runtime refresh and polling share generation protection with action responses.
    t=replace(t,'async function refreshRuntimeStatus({ showFeedback = true, reloadState = true } = {}) {',
        'async function refreshRuntimeStatus({ showFeedback = true, reloadState = true } = {}) {\n  const generation = ++runtimeRefreshGeneration;\n  const actionGeneration = operationRequestGeneration;')
    t=replace(t,'    const progressPayload = await window.brewApp.getProgress();',
        '    const progressPayload = await window.brewApp.getProgress();\n    if (generation !== runtimeRefreshGeneration || actionGeneration !== operationRequestGeneration) return;')
    t=replace(t,"  } catch (err) {\n    if (showFeedback) {\n      showMessage(i18n.t('message.refreshFailed'", "  } catch (err) {\n    if (generation !== runtimeRefreshGeneration || actionGeneration !== operationRequestGeneration) return;\n    if (showFeedback) {\n      showMessage(i18n.t('message.refreshFailed'")
    t=replace(t,"    if (showFeedback) {\n      showMessage(i18n.t('message.refreshFailed', { error: err.message }), true);", "    statusUnavailable();\n    if (!progressPollTimer) startProgressPolling();\n    if (showFeedback) {\n      showMessage(i18n.t('message.refreshFailed', { error: err.message }), true);")
    t=replace(t,'    // Ignore transient polling errors while check is in progress.','    if (epoch === progressPollEpoch) statusUnavailable();')
    t=replace(t,"    if (payload?.progress) {\n      renderCheckProgress(payload.progress);\n    }", "    if (payload?.progress) {\n      if (!acceptOperationProgress(payload.progress)) return;\n      renderCheckProgress(payload.progress);\n    }")
    t=replace(t,'    await syncParallelCheckState(payload?.parallel_check, { showFeedback: false });\n  } catch {',
        '''    await syncParallelCheckState(payload?.parallel_check, { showFeedback: false });
    if (!busyLoading && !payload?.progress?.running && !payload?.parallel_check?.running && payload?.progress) stopProgressPolling();
  } catch {''')
    # Action-local response is authoritative; finally fetch is only a compatibility fallback.
    for name,next_name in [('checkNow','updateOne'),('updateOne','updateSelected'),('updateSelected','updateAll'),('updateAll','loadSavedColumnWidths')]:
        start=t.index(('async function ' if name!='loadSavedColumnWidths' else 'function ')+name+'(')
        end=t.index('\n'+('function ' if next_name=='loadSavedColumnWidths' else 'async function ')+next_name+'(',start)
        block=t[start:end]
        marker='  startProgressPolling();'
        block=replace(block,marker,"  beginOperationRequest();\n  const actionGeneration = operationRequestGeneration;\n  let operationResponse = null;\n"+marker)
        block=block.replace('    const res = await window.brewApp.', '    const res = operationResponse = await window.brewApp.')
        if name=='checkNow':
            old='''    stopProgressPolling();
    try {
      const finalStatus = await window.brewApp.getProgress();
      if (finalStatus?.progress) renderCheckProgress(finalStatus.progress);
    } catch { /* Keep the last known status when the service is unavailable. */ }'''
            block=replace(block,old,'    await finishOperationRequest(operationResponse, actionGeneration);')
        else:
            block=replace(block,'    stopProgressPolling();','    await finishOperationRequest(operationResponse, actionGeneration);')
        t=t[:start]+block+t[end:]
    t=replace(t,'  const done = completed.length || Number(progress?.done || 0);','  const done = completed.filter(item => item.ok && !item.skipped).length;')
    t=replace(t,'  const remainingCount = remaining.length || Math.max(0, total - done);','  const remainingCount = remaining.length;')
    for variable,field in [('tdCurrentVersion','installed_version'),('tdLatestVersion','latest_version')]:
        t=replace(t,f"    {variable}.textContent = pkg.{field} || i18n.t('common.unknown');",f'''    const {variable}Value = document.createElement('bdi');
    {variable}Value.dir = pkg.{field} && pkg.{field} !== 'unknown' ? 'ltr' : 'auto';
    {variable}Value.textContent = pkg.{field} || i18n.t('common.unknown');
    {variable}.appendChild({variable}Value);''')
    # Switching languages must not reset a completed badge to the HTML default.
    t=replace(t,'    lastKnownParallelCheckRunning = true;','    lastKnownParallelCheckRunning = true;\n    presentOperationStatus();')
    t=replace(t,'    lastKnownParallelCheckRunning = false;\n    return;', '    lastKnownParallelCheckRunning = false;\n    presentOperationStatus();\n    return;')
    t=replace(t,'  lastKnownParallelCheckRunning = false;\n\n  await loadState', '  lastKnownParallelCheckRunning = false;\n  presentOperationStatus();\n\n  await loadState')
    out[p]=t
    p='renderer/mac-manager.js';t=read(p)
    anchor="    row.appendChild(detail);\n    if(offer.status==='binary_available') {"
    t=replace(t,anchor,'''    row.appendChild(detail);
    const classification = offer.classification || {category:'unclassified'};
    const installation=(snapshot?.packages||[]).find(p=>p.provider==='homebrew'&&p.kind==='formula'&&p.name===offer.name)?.inventory_verification;
    if(installation) {
      const evidence=document.createElement('details');const title=document.createElement('summary');
      title.textContent=he?'ראיות ההתקנה':'Installation evidence';
      const text=document.createElement('p');text.style.whiteSpace='pre-wrap';
      text.textContent=(he?'גרסה לפי ההתקנה הפעילה: ':'Active installation version: ')+installation.package_version+'\\n'+
        (he?'גרסאות שדווחו: ':'Manager versions: ')+(installation.manager_versions||[]).join(', ')+'\\n'+
        (installation.active_keg||'')+'\\n'+(installation.command_resolution?.resolved_path||'')+'\\n'+
        [...(installation.issues||[]),...(installation.warnings||[])].join(' · ');
      if(installation.version_probe?.code)text.textContent+='\\n'+installation.version_probe.output;
      evidence.append(title,text);row.appendChild(evidence);
    }
    const classificationLabels = {
      service:['שירות עם הגדרות ונתונים','Service with configuration and data'],
      shared_environment:['ספרייה או סביבת פיתוח משותפת','Shared library or development environment'],
      standalone_candidate:['כלי עצמאי מועמד לאוטומציה','Standalone automation candidate'],
      insufficient_information:['חסר מידע לסיווג','Insufficient classification evidence'],
      unclassified:['טרם סווג','Not yet classified'],
    };
    const classificationText=document.createElement('p');
    classificationText.className='migration-classification';
    classificationText.textContent=(he?'סיווג: ':'Classification: ')+(classificationLabels[classification.category]||classificationLabels.unclassified)[he?0:1];
    row.appendChild(classificationText);
    const reasons=document.createElement('p');
    reasons.textContent=(classification.reasons||[]).map(migrationReasonLabel).join(' · ');
    if(classification.checked_at) reasons.textContent+=' · '+classification.checked_at;
    row.appendChild(reasons);
    if(classification.consumers?.length) {
      const consumers=document.createElement('p');consumers.textContent=(he?'תלויים מתועדים: ':'Documented consumers: ')+classification.consumers.map(c=>c.name).join(', ');row.appendChild(consumers);
    }
    for(const [label,values] of [[he?'מידע חסר':'Missing evidence',classification.gaps],[he?'קובצי ראיות':'Evidence files',classification.evidence_refs]]) {
      if(values?.length) {const evidence=document.createElement('details');const title=document.createElement('summary');title.textContent=label;const text=document.createElement('p');text.textContent=values.join(' · ');evidence.append(title,text);row.appendChild(evidence);}
    }
    if(offer.status==='binary_available') {''')
    t=replace(t,"blocked.textContent=he?'המעבר עדיין דורש תוכנית לצרכנים ולנתונים; אינו מבוצע בעדכון רגיל.':'Migration still requires a consumer and data plan; regular updates do not perform it.';",
        "blocked.textContent=classification.category==='standalone_candidate'?(he?'מנגנון המעבר לכלי זה טרם מומש.':'The migration mechanism for this tool is not implemented yet.'):(he?'המעבר דורש טיפול בצרכנים, בנתונים או במידע החסר; אינו מבוצע בעדכון רגיל.':'Migration needs consumer, data or missing-evidence review; regular updates do not perform it.');")
    out[p]=t
    for p in ['package.json','package-lock.json']:
        data=json.loads(read(p));data['version']='1.3.2'
        if 'packages' in data and '' in data['packages']: data['packages']['']['version']='1.3.2'
        out[p]=json.dumps(data,ensure_ascii=False,indent=2)+'\n'
    out['docs/MAC_MANAGER.md']=read('docs/MAC_MANAGER.md')+'''\n\n## 1.3.2 operation results and installation evidence

Operation responses include a final progress copy captured before the operation lock is released. Completed, partial, canceled and failed reports remain visible independently of the busy overlay. Unreachable service status is unverified, never inferred success.

Homebrew inventory reconciles manager metadata with active opt links and local receipts, including retained Cellar installations omitted by brew info. Upstream version, package revision, inactive receipt gaps and PATH command resolution stay separate. Only the explicit LLVM clang probe is executed. Evidence conflicts block the affected installation.

Full checks collect local migration classifications independently of binary availability. Installed receipt reverse dependencies, exported interfaces, formula service definitions, registered services, options and bounded Mach-O linkage checks determine service/shared-environment/standalone-candidate/insufficient-information roles. Private projects are not searched. Classification evidence is retained under migration-evidence in the application data directory. A classification never enables migration: only the existing terminal-notifier pilot remains executable. Old classifications are unclassified until fresh evidence is available.
'''
    out['docs/MAC_MANAGER_TASKS.md']=read('docs/MAC_MANAGER_TASKS.md').replace('מצב הפרויקט שנבדק: 1.3.1.','מצב בסיס הפרויקט: 1.3.1; מימוש 1.3.2 בבדיקות קבלה.')+'''\n\n## התקדמות T01 ו־T02 — 20.10.2026

מימוש 1.3.2 כולל חיווי וסיכום פעולה אחידים, ראיות מלאי וסיווג מועמדות אוטומטי.
תיבות המשימות נשארות פתוחות עד בדיקות הקבלה באפליקציה. ראו דוח האימות
ב־docs/T01_T02_VALIDATION.md; אין במימוש זה הרחבת מעברים מעבר לפיילוט הקיים.
'''
    out['docs/T01_T02_VALIDATION.md']='''# T01 / T02 — local acceptance record

Target: 1.3.2, 2026-10-20. Implementation is not acceptance evidence.
Pending: bundle replay/restore, Python, transport, bilingual button-flow fixtures,
fresh machine inventory/classification, packaged-app Start Check and final status,
and a real zero-install update execution. No new package migrations are authorized.
'''
    out['tests/test_update_policy.py']=replace(read('tests/test_update_policy.py'),
        '            packages = service.fetch_installed_inventory({})',
        '''            with patch('installation_evidence.collect',return_value={'omitted':[]}):
                packages = service.fetch_installed_inventory({})''')
    if RELIABILITY_ACCEPTED:
        tasks=read('docs/MAC_MANAGER_TASKS.md')
        start=tasks.index('### T01');end=tasks.index('### T03')
        completed=tasks[start:end].replace('- [ ]','- [x]')
        completed=completed.replace('### T02','הושלם: 20.10.2026, גרסה 1.3.2. [ראיות ובדיקות](T01_T02_VALIDATION.md).\n\n### T02')
        completed+='הושלם: 20.10.2026, גרסה 1.3.2. [ראיות ובדיקות](T01_T02_VALIDATION.md).\n\n'
        tasks=tasks[:start]+completed+tasks[end:]
        tasks=tasks.replace('מצב הפרויקט שנבדק: 1.3.1.','מצב הפרויקט שנבדק והותקן: 1.3.2.')
        tasks+='''\n\n## מסירת T01 ו־T02 — 20.10.2026

שתי המשימות הושלמו ונבדקו בגרסה המקומית 1.3.2. ראו [דוח האימות](T01_T02_VALIDATION.md).
הסריקה המלאה שנבדקה מצאה 239 התקנות ו־28 מועמדות למעבר; לכולן נוסף סיווג
או פער מידע מפורש עם ראיות. אלה נתוני הבדיקה המתועדת, ולא מונים קבועים.
LLVM הפעיל מזוהה כ־22.1.5; גרסת 23.1.3_2 ללא receipt אינה נבחרת כפעילה.
פקודת clang הקיימת נכשלת בגלל ספריית Z3 חסרה; הפעולה התלויה בה חסומה.
תיקון החבילה עצמה נשאר ב־T06. לא נוספה הרשאת ביצוע למעברים חדשים.

לחבילה החדשה משתמשים ב־`tools/implement-mac-manager.py` עם המצבים
`t01-check`, `t01-apply`, `t01-verify`, `t01-install`, `t01-restore`.
`t01-restore` משחזר קובצי פרויקט בלבד; גיבויי האפליקציה וההגדרות נשמרים בנפרד.
המשימה הבאה לפי הסדר היא T03, על בסיס המיון שכבר נוסף ב־T02.
'''
        out['docs/MAC_MANAGER_TASKS.md']=tasks
        out['docs/T01_T02_VALIDATION.md']='''# T01 ו־T02 — אימות ומסירה של 1.3.2

תאריך: 20 באוקטובר 2026. גרסת Intel המקומית 1.3.2 נבנתה, הותקנה ונפתחה.

## תוצאות

- 80 בדיקות Python עברו, כולל מלאי, סיווג, ביטול, אפס התקנות ומרוץ בין התחלת פעולה לקריאת סטטוס ישנה.
- עברו בדיקות תחביר JavaScript, תעבורת HTTP ושני תרחישי Electron בעברית ובאנגלית. נבדקו לחיצות כפתור, IPC, תשובה סופית ו־finally; כשל, ביטול, נתונים חלקיים, דילוגים, תשובות מאוחרות, ניתוק וחזרה לתקשורת ובדיקה מקבילה.
- חבילת השינוי נבדקה על העתק של הקבצים המקוריים: בדיקה ללא כתיבה, החלה חוזרת זהה, שחזור מדויק וסירוב לדרוס עריכה מאוחרת. README הקיים נשמר.
- תאים של גרסאות מבודדים בכיוון LTR, כך ש־23.1.3_2 מוצג בסדר הנכון גם בעברית.
- בדיקת הקבלה השתמשה ב־renderer וב־preload מהאפליקציה הארוזה מול השירות המותקן. ״התחל בדיקה״ הסתיים עם 239 התקנות ו־28 מועמדות, לכולן סיווג. החיווי נשאר סופי והטעינה נעלמה, גם אחרי ״רענון סטטוס״ ובשתי השפות.
- ״עדכן הכול״ נבדק מול backend מהחבילה המותקנת בפרופיל נתונים מבודד: שאר ההתקנות הוחרגו בפרופיל הבדיקה בלבד, LLVM נשאר כמועמד חסום, והוצבה הגנה נוספת האוסרת כל התקנה. התוצאה: 0 ניסיונות התקנה, 0 עדכונים, דילוג אחד עם סיבה, ״הושלם עם אזהרות״ וללא טעינה. זהו מסלול הביצוע האמיתי עם סריקת המחשב, ולא ציור של מצב סופי בלבד.
- הגדרות המשתמש המקוריות הושוו לגיבוי ונשמרו. לא בוצעו מעבר ספק, הסרת חבילות או עדכון חבילות במסגרת המסירה.

## ראיות

קובצי JSON וצילומי מסך נשמרו בתיקיית השחזור המקומית:

- [תוצאות האימות האוטומטי](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t01-t02-1.3.2/verification.json)
- [בדיקת ״התחל בדיקה״ מהחבילה הארוזה](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t01-t02-1.3.2/packaged-check-accepted/ui-acceptance.json)
- [תשובת הסריקה וכל סיווגי המועמדות](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t01-t02-1.3.2/packaged-check-accepted/response.json)
- [בדיקת אפס התקנות](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t01-t02-1.3.2/packaged-zero-install-accepted/ui-acceptance.json)
- [תשובת אפס התקנות](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t01-t02-1.3.2/packaged-zero-install-accepted/response.json)
- [גיבויים, מניפסט וקובצי המקור הקודמים](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t01-t02-1.3.2)

כל בדיקת קבלה רושמת גרסה ו־SHA-256 של קוד הממשק, השירות ורכיב הראיות שבחבילה. ראיות הסיווג עצמן נשמרות ב־migration-evidence תחת תיקיית נתוני האפליקציה.

## ממצא שנשאר לטיפול

LLVM הפעיל הוא 22.1.5. תיקיית 23.1.3_2 ללא receipt מתועדת כעותק לא מאומת ואינה משמשת כגרסה הפעילה או כתלות בינארית מסופקת. clang הקיים נכשל בטעינת libz3.4.15.dylib; הגרסה הידועה נשמרת במלאי והפעולה התלויה באימות תפקודו נחסמת. תיקון LLVM או מעבר שלו שייכים ל־T06.

סיווג מועמדת אינו בדיקת שימוש מלאה או אישור מעבר. רק פיילוט terminal-notifier הקיים נשאר נתמך לביצוע; מנגנון המעבר הכללי הוא T03.
'''
    return out

RELIABILITY_RECOVERY = Path.home()/'.local/share/brew-update-manager/recovery/t01-t02-1.3.2'

def reliability_manifest():
    path=RELIABILITY_RECOVERY/'manifest.json'
    return json.loads(path.read_text()) if path.exists() else None

def reliability_source(relative):
    old=reliability_manifest()
    if old and relative in old['files']:
        before=RELIABILITY_RECOVERY/'before'/relative
        return before.read_text() if old['files'][relative]['before'] is not None else ''
    return (ROOT/relative).read_text()

def reliability_apply(apply=False):
    old=reliability_manifest()
    if old:
        for relative,record in old['files'].items():
            path=ROOT/relative
            if not path.is_file() or digest(path.read_bytes())!=record['after']:
                raise RuntimeError('Bundle 002 refuses later edits: '+relative)
        # README is protected even though bundle 002 does not modify it.
        if digest((ROOT/'README.md').read_bytes())!=RELIABILITY_BASE['README.md']:
            raise RuntimeError('Protected README changed; incorporate before proceeding')
    else:
        for relative,expected in RELIABILITY_BASE.items():
            path=ROOT/relative
            if not path.is_file() or digest(path.read_bytes())!=expected:
                raise RuntimeError('Bundle 002 baseline differs: '+relative)
    outputs=reliability_outputs(reliability_source)
    outputs.update(RELIABILITY_TESTS)
    data={p:text.encode() for p,text in outputs.items()}
    if old:
        if set(data)!=set(old['files']) or any(digest(value)!=old['files'][p]['after'] for p,value in data.items()):
            if not apply: raise RuntimeError('Bundle 002 contains pending script changes; run t01-apply')
        elif all((ROOT/p).read_bytes()==value for p,value in data.items()):
            print('Bundle 002 already applied; all hashes verified.');return
    if not apply:
        print('Bundle 002 preconditions passed; '+str(len(outputs))+' outputs prepared without mutation.');return
    RELIABILITY_RECOVERY.mkdir(parents=True,exist_ok=True)
    journal=old or dict(bundle='002',created_at=time.time(),files={})
    # Detect collisions before preserving or publishing any file.
    for relative in data:
        path=ROOT/relative
        if not old and relative not in RELIABILITY_BASE and path.exists():
            raise RuntimeError('Untracked output already exists; preserve it first: '+relative)
    for relative,value in data.items():
        path=ROOT/relative;before=path.read_bytes() if path.exists() else None
        if relative not in journal['files']:
            journal['files'][relative]={'before':digest(before) if before is not None else None}
            if before is not None:
                saved=RELIABILITY_RECOVERY/'before'/relative;saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(before)
        journal['files'][relative]['after']=digest(value)
    (RELIABILITY_RECOVERY/'manifest.json').write_text(json.dumps(journal,indent=2))
    for relative,value in data.items():
        path=ROOT/relative;path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.bundle002-')
        with os.fdopen(fd,'wb') as stream:stream.write(value)
        os.chmod(tmp,path.stat().st_mode&0o777 if path.exists() else 0o644)
        os.replace(tmp,path)
    print('Applied bundle 002; exact prior working files preserved at '+str(RELIABILITY_RECOVERY))

def reliability_restore():
    old=reliability_manifest()
    if not old: raise RuntimeError('Bundle 002 is not applied')
    for relative,record in old['files'].items():
        path=ROOT/relative
        if not path.is_file() or digest(path.read_bytes())!=record['after']:
            raise RuntimeError('Protected restore refuses later edits: '+relative)
    for relative,record in old['files'].items():
        path=ROOT/relative
        if record['before'] is None:path.unlink()
        else:shutil.copyfile(RELIABILITY_RECOVERY/'before'/relative,path)
    (RELIABILITY_RECOVERY/'manifest.json').rename(RELIABILITY_RECOVERY/('restored.'+str(time.time_ns())+'.json'))
    print('Bundle 002 restored; prior working files retained exactly.')

def reliability_replay():
    global ROOT,RELIABILITY_RECOVERY
    actual_root,actual_recovery=ROOT,RELIABILITY_RECOVERY
    old=reliability_manifest()
    if not old:raise RuntimeError('Apply bundle 002 before replay')
    with tempfile.TemporaryDirectory(prefix='mac-manager-bundle002-') as tmp:
        checkout=Path(tmp)/'checkout';checkout.mkdir()
        for relative in RELIABILITY_BASE:
            path=checkout/relative;path.parent.mkdir(parents=True,exist_ok=True)
            src=actual_recovery/'before'/relative
            shutil.copyfile(src if src.exists() else actual_root/relative,path)
        ROOT,RELIABILITY_RECOVERY=checkout,Path(tmp)/'recovery'
        try:
            baseline={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in ROOT.rglob('*') if p.is_file()}
            reliability_apply(False)
            assert not RELIABILITY_RECOVERY.exists(),'check wrote recovery metadata'
            reliability_apply(True);reliability_apply(True)
            target=ROOT/'renderer/app.js';data=target.read_bytes();target.write_text('later independent edit')
            try:reliability_restore()
            except RuntimeError:pass
            else:raise RuntimeError('restore overwrote later work')
            target.write_bytes(data);reliability_restore()
            after={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in ROOT.rglob('*') if p.is_file()}
            assert baseline==after,'restore was not byte-for-byte lossless'
        finally:ROOT,RELIABILITY_RECOVERY=actual_root,actual_recovery
    print('Bundle 002 check, replay, idempotency and protected restore: PASS')

def reliability_verify():
    reliability_apply(False)
    reliability_replay()
    run(['/usr/local/bin/python3.14','-m','unittest','discover','-s','tests','-p','test_*.py'])
    for path in ['electron/main.js','electron/preload.js','renderer/app.js','renderer/mac-manager.js','renderer/i18n.js']:
        run(['node','--check',path])
    run(['node','tests/service-http.cjs'])
    run([str(ROOT/'node_modules/.bin/electron'),'tests/progress-ui.cjs'])
    run([str(ROOT/'node_modules/.bin/electron'),'tests/operation-results-ui.cjs'])
    run(['git','diff','--check'])
    log=RECOVERY/'run.log'
    import re
    counts=re.findall(r'Ran (\d+) tests',log.read_text() if log.exists() else '')
    RELIABILITY_RECOVERY.mkdir(parents=True,exist_ok=True)
    (RELIABILITY_RECOVERY/'verification.json').write_text(json.dumps(dict(version='1.3.2',verified_at=time.time(),
        python_tests=int(counts[-1]) if counts else None,bundle_replay=True,syntax=True,http=True,
        bilingual_progress_fixture=True,bilingual_button_fixture=True,diff_check=True,
        outputs={p:r['after'] for p,r in reliability_manifest()['files'].items()}),indent=2))

def reliability_main(mode):
    global RECOVERY,verify
    if mode=='t01-check':reliability_apply(False)
    elif mode=='t01-apply':reliability_apply(True)
    elif mode=='t01-verify':reliability_verify()
    elif mode=='t01-restore':reliability_restore()
    elif mode=='t01-install':
        # Reuse the existing protected installer, with bundle 002 verification.
        preserved=RELIABILITY_RECOVERY/('app-data-before-'+str(time.time_ns()))
        source=Path.home()/'Library/Application Support/Homebrew Update Manager'
        for name in ['settings.json','state.json','update-history.json','last-update-run.json','migration-journal.json','notifier-route.json']:
            path=source/name
            if path.is_file():preserved.mkdir(parents=True,exist_ok=True);shutil.copy2(path,preserved/name)
        records=Path.home()/'.local/share/brew-update-manager/installed.json'
        if records.is_file():preserved.mkdir(parents=True,exist_ok=True);shutil.copy2(records,preserved/'installed-tools.json')
        RECOVERY=RELIABILITY_RECOVERY
        verify=reliability_verify
        install()
    else:raise RuntimeError('Unknown bundle 002 mode')

RELIABILITY_TESTS = {}
RELIABILITY_TESTS['tests/test_installation_evidence.py'] = r'''import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_update_policy import service as s
import installation_evidence as e
import mac_manager as mm


class InstallationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cellar=self.root/'Cellar';self.cellar.mkdir()
        self.data=self.root/'data'
        self.patches=[]
        for key,value in [('DATA_DIR',self.data),('STATE_FILE',self.data/'state.json'),('SERVICE_LOG_FILE',self.data/'service.log')]:
            p=patch.object(s,key,value);p.start();self.patches.append(p)
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        p=patch.object(s,'brew_bin',return_value='/fixture/brew');p.start();self.patches.append(p)
        e.reset();s.reset_cancel_state();self.addCleanup(s.reset_cancel_state)

    def keg(self,name,version='1.0',deps=None,receipt=True,active=True,formula='class Tool < Formula\nend\n'):
        root=self.cellar/name/version;root.mkdir(parents=True,exist_ok=True)
        if receipt:(root/'INSTALL_RECEIPT.json').write_text(json.dumps({'runtime_dependencies':deps or [],'used_options':[]}))
        (root/'.brew').mkdir(exist_ok=True);(root/'.brew'/f'{name}.rb').write_text(formula)
        if active:
            opt=self.root/'opt'/name;opt.parent.mkdir(exist_ok=True);opt.unlink(missing_ok=True);opt.symlink_to(root)
        return root

    def row(self,name,versions=('1.0',),linked=None):
        return dict(name=name,installed=[dict(version=v) for v in versions],linked_keg=linked)

    def command(self,args,**kw):
        self.assertTrue(kw.get('cancellable'))
        if args[-1]=='--prefix':return s.CommandResult(0,str(self.root),'')
        if args[-1]=='--cellar':return s.CommandResult(0,str(self.cellar),'')
        if 'services' in args:return s.CommandResult(0,'[]','')
        return s.CommandResult(0,'','')

    def test_opt_receipt_selects_active_not_last_manager_entry(self):
        self.keg('llvm','22.1.5');self.keg('llvm','23.1.3_2',receipt=False,active=False)
        audit=e.inspect(s,self.row('llvm',('23.1.3_2','22.1.5')),self.root,self.cellar)
        self.assertEqual(audit['package_version'],'22.1.5')
        self.assertEqual(audit['status'],'verified')
        self.assertIn('inactive_receipt_missing:23.1.3_2',audit['warnings'])

    def test_missing_active_receipt_and_broken_link_are_unverified(self):
        root=self.keg('tool',receipt=False)
        audit=e.inspect(s,self.row('tool'),self.root,self.cellar)
        self.assertEqual(audit['status'],'unverified')
        (self.root/'opt/tool').unlink();(self.root/'opt/tool').symlink_to(root/'missing')
        audit=e.inspect(s,self.row('tool'),self.root,self.cellar)
        self.assertIn('active_link_missing_or_outside_installation',audit['issues'])

    def test_revision_and_llvm_probe_and_foreign_path_are_separate(self):
        root=self.keg('llvm','22.1.5_2');(root/'bin').mkdir();(root/'bin/clang').write_text('fixture')
        with patch.object(e.shutil,'which',return_value='/usr/bin/clang'),patch.object(e,'command',return_value=s.CommandResult(0,'clang version 22.1.5','')):
            audit=e.inspect(s,self.row('llvm',('22.1.5_2',)),self.root,self.cellar)
        self.assertEqual(audit['upstream_version'],'22.1.5');self.assertEqual(audit['revision'],2)
        self.assertEqual(audit['status'],'verified');self.assertEqual(audit['command_resolution']['path'],'/usr/bin/clang')
        with patch.object(e,'command',return_value=s.CommandResult(0,'clang version 21.0.0','')):
            audit=e.inspect(s,self.row('llvm',('22.1.5_2',)),self.root,self.cellar)
        self.assertEqual(audit['status'],'conflict')

    def test_failed_version_probe_retains_installation_but_blocks_dependent_action(self):
        root=self.keg('llvm','22.1.5');(root/'bin').mkdir();(root/'bin/clang').write_text('fixture')
        with patch.object(e,'command',return_value=s.CommandResult(-6,'','dyld: library missing')):
            audit=e.inspect(s,self.row('llvm',('22.1.5',)),self.root,self.cellar)
        self.assertEqual(audit['package_version'],'22.1.5');self.assertEqual(audit['functional_status'],'failed')
        fields=e.inventory_fields(s,self.row('llvm',('22.1.5',)),dict(audits={'llvm':audit}))
        self.assertTrue(fields['inventory_stale']);self.assertEqual(fields['installed_version'],'22.1.5')

    def test_incomplete_keg_is_not_a_satisfied_binary_dependency(self):
        self.keg('llvm','22.1.5');self.keg('llvm','23.1.3_2',receipt=False,active=False)
        with patch.object(s,'run_cmd',side_effect=self.command):e.collect(s,dict(formulae=[self.row('llvm',('22.1.5','23.1.3_2'))]))
        result=e.verified_dependencies(s,{})
        self.assertEqual(result['llvm']['versions'],['22.1.5'])

    def test_receipts_reconcile_omitted_install_and_retained_consumer(self):
        self.keg('tool');self.keg('consumer','1',deps=[dict(full_name='tool',version='1.0')],active=False)
        self.keg('consumer','2',active=True)
        with patch.object(s,'run_cmd',side_effect=self.command):ctx=e.collect(s,dict(formulae=[self.row('tool')]))
        self.assertEqual(ctx['omitted'][0]['name'],'consumer')
        self.assertEqual(ctx['reverse']['tool'][0]['installed_version'],'1')

    def evidence(self,**kw):
        return dict(inventory={'status':'verified'},gaps=[],consumers=[],files=dict(commands=['tool'],gaps=[],**kw))

    def test_categories_and_role_precedence_and_no_private_script_requirement(self):
        self.assertEqual(e.classify(self.evidence())['category'],'standalone_candidate')
        self.assertEqual(e.classify(self.evidence(libraries=['lib/tool.dylib']))['category'],'shared_environment')
        self.assertEqual(e.classify(self.evidence(development=['bin/llvm-config']))['category'],'shared_environment')
        result=e.classify(self.evidence(service_definition=True,libraries=['lib/tool.dylib']))
        self.assertEqual(result['category'],'service');self.assertEqual(result['roles'],['service','shared_environment'])
        result=e.classify({**self.evidence(),'gaps':['otool_failed']})
        self.assertEqual(result['category'],'insufficient_information');self.assertFalse(result['automation_candidate'])
        result=e.classify({**self.evidence(service_definition=True),'gaps':['receipt_gap']})
        self.assertEqual(result['category'],'service');self.assertIn('receipt_gap',result['gaps'])

    def test_stopped_service_remains_service(self):
        result=e.classify({**self.evidence(),'service':dict(name='tool',status='none')})
        self.assertEqual(result['category'],'service')

    def test_compiler_cli_is_a_development_environment(self):
        root=self.keg('go');(root/'bin').mkdir();p=root/'bin/go';p.write_text('fixture');p.chmod(0o755)
        files=e.file_evidence(s,root)
        result=e.classify(dict(inventory={'status':'verified'},gaps=[],files=files))
        self.assertEqual(result['category'],'shared_environment')

    def test_external_command_target_prevents_standalone_classification(self):
        root=self.keg('tool');(root/'bin').mkdir();(root/'bin/tool').symlink_to('/usr/bin/true')
        files=e.file_evidence(s,root)
        self.assertTrue(any(x.startswith('external_command_target:') for x in files['gaps']))
        self.assertEqual(e.classify(dict(inventory={'status':'verified'},files=files,gaps=[]))['category'],'insufficient_information')

    def test_file_inspection_is_installation_scoped_and_cancellable(self):
        root=self.keg('tool',formula='class Tool < Formula\n service do\n run opt_bin/"tool"\n end\nend')
        (root/'bin').mkdir();p=root/'bin/tool';p.write_text('#!/bin/sh\n');p.chmod(0o755)
        external=self.root/'private';external.mkdir();(external/'secret.h').write_text('not inspected')
        (root/'include').symlink_to(external)
        files=e.file_evidence(s,root)
        self.assertTrue(files['service_definition']);self.assertEqual(files['headers'],[])
        s.mark_cancel_requested()
        with self.assertRaises(s.OperationCancelledError):e.file_evidence(s,root)

    def test_annotation_preserves_execution_policy_and_exclusion(self):
        root=self.keg('tool');(root/'bin').mkdir();p=root/'bin/tool';p.write_text('fixture');p.chmod(0o755)
        with patch.object(s,'run_cmd',side_effect=self.command):ctx=e.collect(s,dict(formulae=[self.row('tool')]))
        pkg=dict(name='tool',provider='homebrew',kind='formula',installation_id='homebrew:formula:tool',inventory_verification=ctx['audits']['tool'])
        offers=[dict(name='tool',executable=False,status='archive_unknown'),dict(name='ngrok',executable=False)]
        with patch.object(s,'run_cmd',side_effect=self.command):e.annotate(s,offers,[pkg],True)
        c=offers[0]['classification'];self.assertEqual(c['category'],'standalone_candidate')
        self.assertFalse(offers[0]['executable']);self.assertNotIn('classification',offers[1])
        self.assertTrue(Path(c['evidence_refs'][0]).is_file())
        s.atomic_write_json(s.STATE_FILE,dict(migrations=offers))
        pkg['inventory_verification']['checked_at']='new timestamp'
        result=e.annotate(s,[dict(name='tool')],[pkg],False)
        self.assertEqual(result[0]['classification']['category'],'standalone_candidate')
        pkg['inventory_verification']['package_version']='changed'
        self.assertEqual(e.annotate(s,[dict(name='tool')],[pkg],False)[0]['classification']['category'],'unclassified')

    def test_one_candidate_failure_does_not_erase_other_classification(self):
        offers=[dict(name='first',executable=False),dict(name='second',executable=False)]
        audits={n:dict(status='verified',active_keg=str(self.keg(n)),package_version='1.0') for n in ('first','second')}
        e.CONTEXT.inventory=dict(rows={},reverse={},gaps=[],prefix=str(self.root))
        packages=[dict(name=n,provider='homebrew',kind='formula',inventory_verification=a) for n,a in audits.items()]
        with patch.object(e,'file_evidence',side_effect=[OSError('unreadable'),dict(commands=['second'],gaps=[])]),patch.object(s,'run_cmd',side_effect=self.command):
            e.annotate(s,offers,packages,True)
        self.assertEqual(offers[0]['classification']['category'],'insufficient_information')
        self.assertEqual(offers[1]['classification']['category'],'standalone_candidate')

    def test_real_update_engine_zero_installs_keeps_deferred_reasons(self):
        from update_engine import Engine
        packages=[dict(name=n,kind='formula',provider='homebrew',outdated=True,inventory_stale=True,
                       installed_version='1',latest_version='2') for n in ['first','second']]
        state=dict(packages=packages)
        with patch.object(s,'RUN_JOURNAL_FILE',self.data/'run.json'),patch.object(s,'UPDATE_PLAN_FILE',self.data/'plan.json'), \
             patch.object(s,'compute_snapshot',return_value=state),patch.object(s,'safe_refresh_snapshot',return_value=(state,None)), \
             patch.object(s,'append_update_history_entries'),patch.object(s,'maybe_schedule_deferred_check'), \
             patch.object(Engine,'catalog',return_value={}),patch.object(Engine,'installed',return_value={}), \
             patch.object(s,'run_upgrade',side_effect=AssertionError('Unexpected installation')) as upgrade:
            result=s.run_with_progress('update_all',lambda:Engine(s).run(track_progress=True))
        upgrade.assert_not_called()
        self.assertEqual(result['updated_count'],0);self.assertEqual(result['deferred_count'],2)
        self.assertFalse(result['progress']['running']);self.assertEqual(result['progress']['phase'],'completed')
        self.assertTrue(all(x['reason']=='inventory_unavailable' for x in result['progress']['deferred_packages']))


class FinalProgressTests(unittest.TestCase):
    def setUp(self):
        s.reset_cancel_state();s.reset_check_progress_for_run()
        self.addCleanup(s.reset_cancel_state)

    def test_final_copy_captured_under_lock_and_not_mutated_by_next_run(self):
        def operation():
            self.assertTrue(s.OP_LOCK.locked())
            s.set_check_progress(running=False,phase='completed',completed_packages=[dict(name='first',ok=True)])
            return dict(partial=True,packages=[])
        with patch.object(s,'maybe_schedule_deferred_check'):
            payload=s.run_with_progress('check_now',operation,snapshot=True)
        s.reset_check_progress_for_run()
        self.assertEqual(payload['progress']['phase'],'completed');self.assertTrue(payload['progress']['partial'])
        self.assertEqual(payload['progress']['completed_packages'][0]['name'],'first')
        self.assertFalse(s.OP_LOCK.locked())

    def test_stale_caller_snapshot_cannot_finish_a_new_operation(self):
        with patch.dict(s.CURRENT_OPERATION,dict(running=True,name='check_now',started_at=s.now_iso())):
            s.set_check_progress(running=True,phase='preflight',current_package='new-operation')
            result=s.normalize_stale_progress_state({'running':False})
            self.assertTrue(result['running']);self.assertEqual(result['current_package'],'new-operation')
            payload=s.runtime_status_payload()
            self.assertTrue(payload['operation']['running']);self.assertTrue(payload['progress']['running'])

    def test_orphaned_progress_is_an_error_without_invented_user_cancellation(self):
        with patch.dict(s.CURRENT_OPERATION,dict(running=False,name=None)),patch.object(s,'append_service_log'):
            s.set_check_progress(running=True,phase='preflight',done=0,total=3,current_package='stale')
            result=s.normalize_stale_progress_state()
        self.assertEqual(result['phase'],'error');self.assertFalse(result['canceled']);self.assertIsNone(result['current_package'])

    def test_unexpected_update_failure_finishes_progress_before_unlock(self):
        def fail():raise OSError('fixture failure')
        with patch.object(s,'maybe_schedule_deferred_check'):
            with self.assertRaises(OSError) as caught:s.run_with_progress('update_all',fail)
        self.assertEqual(caught.exception.operation_progress['phase'],'error')
        self.assertFalse(s.read_check_progress()['running']);self.assertEqual(s.read_check_progress()['phase'],'error')
        self.assertFalse(s.OP_LOCK.locked())

    def test_cancellation_captures_terminal_state(self):
        def cancel():raise s.OperationCancelledError('fixture cancel')
        with patch.object(s,'maybe_schedule_deferred_check'):
            result=s.run_with_progress('check_now',cancel,snapshot=True)
        self.assertTrue(result['canceled']);self.assertEqual(result['progress']['phase'],'canceled')
'''

RELIABILITY_TESTS['tests/operation-results-ui.cjs'] = r'''// Button/IPC/finally regression fixture. Never invokes a package manager.
const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = process.env.BREW_PROGRESS_APP_ROOT || path.resolve(__dirname,'..');
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'mac-results-ui-')));
const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));
let ready; const initialized = new Promise(resolve=>{ready=resolve;});
let model={progress:{running:false,phase:'idle'}, response:null, error:null, offline:false, calls:0};
const state={updated_at:new Date().toISOString(),counts:{outdated_total:1},packages:[
  {name:'llvm',kind:'formula',provider:'homebrew',outdated:true,installed_version:'22.1.5',latest_version:'23.1.3_2',update_block_reason:'source_required'}]};
ipcMain.handle('state:get',()=>state);
ipcMain.handle('settings:get',()=>({settings:{brew_path:'/fixture/brew',binary_only:true},scheduler:{enabled:false}}));
ipcMain.handle('updates:history:get',()=>({items:[]}));
ipcMain.handle('app-update:check',()=>{ready();return {ok:true,updateAvailable:false};});
ipcMain.handle('progress:get',()=>{if(model.offline)throw Error('fixture offline');return {progress:model.progress,parallel_check:model.parallel||{running:false}};});
for(const channel of ['check:run','update:all','update:one','update:selected'])ipcMain.handle(channel,async()=>{
  model.calls++;
  await delay(30);
  model.progress=model.response?.progress||model.progress;
  if(model.error)throw Error(model.error);
  return model.response;
});
const watchdog=setTimeout(()=>{console.error('Operation fixture timed out');app.exit(1);},60000);
app.whenReady().then(async()=>{
  const win=new BrowserWindow({show:false,width:1100,height:1100,webPreferences:{preload:path.join(root,'electron/preload.js'),contextIsolation:true,backgroundThrottling:false}});
  const js=code=>win.webContents.executeJavaScript(code);
  try {
    await win.loadFile(path.join(root,'renderer/index.html'));await initialized;
    await js(`window.confirm=()=>true;window.alert=()=>{};void 0;`);
    for(const lang of ['he','en']) {
      await js(`(async()=>{i18n.setLang(${JSON.stringify(lang)});refreshStaticText();await loadState({showLoader:false});})()`);
      const versions=await js(`(()=>{const row=Array.from(tbodyEl.rows).find(r=>r.textContent.includes('llvm'));return Array.from(row.querySelectorAll('bdi')).map(e=>({dir:e.dir,value:e.textContent}));})()`);
      assert.deepEqual(versions,[{dir:'ltr',value:'22.1.5'},{dir:'ltr',value:'23.1.3_2'}],lang+': isolated package revisions');
      for(const scenario of ['check','partial','cancel','error','zero','deferred','mixed']) {
        const phase=scenario==='cancel'?'canceled':scenario==='error'?'error':'completed';
        const update=['zero','deferred','mixed'].includes(scenario);
        const stamp=new Date().toISOString();
        const completed=scenario==='mixed'?[{name:'done',ok:true},{name:'bad',ok:false},{name:'same',ok:true,skipped:true,reason:'not_outdated'}]:[];
        const progress={running:false,operation_name:update?'update_all':'check',phase,started_at:stamp,
          current_package:null,partial:scenario==='partial',error:scenario==='error'?'fixture failure':null,
          completed_packages:completed,failed_packages:completed.filter(x=>!x.ok),remaining_packages:[],
          deferred_packages:scenario==='deferred'?[{name:'llvm',reason:'source_required'}]:[],planned_packages:[],done:0,total:0};
        model={progress:{running:false,phase:'idle'},response:{ok:true,snapshot:state,progress,canceled:phase==='canceled',updated_count:scenario==='mixed'?1:0,failed_count:scenario==='mixed'?1:0,results:[]},calls:0};
        await js(`retainedOperationProgress=null;operationStartedFloor=0;operationStatusUnverified=false;lastKnownOperationRunning=false;setLoading(false);`);
        await js(`document.getElementById('${update?'updateAllBtn':'checkNowBtn'}').click();`);
        for(let attempt=0;attempt<100 && !model.calls;attempt++)await delay(10);
        await js(`(async()=>{const until=Date.now()+4000;while(busyLoading&&Date.now()<until)await new Promise(r=>setTimeout(r,10));if(busyLoading)throw Error('busy did not finish');})()`);
        assert.equal(model.calls,1,lang+': button IPC '+scenario);
        const actual=await js(`({status:operationStatusBadgeEl.dataset.status,loader:loaderEl.classList.contains('visible'),summary:document.getElementById('operationResultSummary').textContent,hidden:document.getElementById('operationResultSummary').hidden,disabled:checkNowBtn.disabled})`);
        const expected=scenario==='cancel'?'canceled':scenario==='error'?'failed':['partial','deferred'].includes(scenario)?'warning':'completed';
        assert.equal(actual.status,expected,lang+': '+scenario);assert.equal(actual.loader,false,lang+': loader '+scenario);
        assert.equal(actual.hidden,false);assert.equal(actual.disabled,false);
        if(scenario==='deferred')assert.ok(actual.summary.includes('llvm'));
        if(scenario==='mixed')assert.match(actual.summary,/1/);
        await js(`document.getElementById('refreshStatusBtn').click();`);await delay(70);
        assert.equal(await js(`operationStatusBadgeEl.dataset.status`),expected,lang+': refresh retained '+scenario);
        await js(`refreshStaticText();`);
        assert.equal(await js(`operationStatusBadgeEl.dataset.status`),expected,lang+': localization retained '+scenario);
      }
      // Polling can arrive late from a prior operation, or regress the same finished run.
      const stale=await js(`(()=>{const stamp=new Date().toISOString();operationStartedFloor=0;
        retainedOperationProgress=null;renderCheckProgress({running:false,phase:'completed',operation_name:'check',started_at:stamp});
        renderCheckProgress({running:true,phase:'preparing',operation_name:'check',started_at:stamp});
        renderCheckProgress({running:true,phase:'preparing',operation_name:'check',started_at:'2000-01-01T00:00:00Z'});
        return operationStatusBadgeEl.dataset.status;})()`);
      assert.equal(stale,'completed',lang+': late progress ignored');
      // A rejected request plus unreachable status remains unverified, then recovers.
      model={progress:{running:false,phase:'idle'},response:null,error:'fixture network failure',offline:false,calls:0};
      await js(`retainedOperationProgress=null;operationStartedFloor=0;operationStatusUnverified=false;lastKnownOperationRunning=false;setLoading(false);`);
      // Initial live-status read succeeds; switch off service when action is received.
      ipcMain.removeHandler('check:run');ipcMain.handle('check:run',async()=>{model.calls++;model.offline=true;throw Error('fixture network failure');});
      await js(`checkNowBtn.click();`);await delay(100);
      assert.equal(await js(`operationStatusBadgeEl.dataset.status`),'unverified',lang+': offline');
      model.offline=false;model.progress={running:false,phase:'completed',operation_name:'check',started_at:new Date().toISOString()};
      await js(`pollCheckProgress();`);await delay(50);
      assert.equal(await js(`operationStatusBadgeEl.dataset.status`),'completed',lang+': reconnect');
      ipcMain.removeHandler('check:run');ipcMain.handle('check:run',async()=>{model.calls++;await delay(30);model.progress=model.response.progress;return model.response;});
      // A background check does not overwrite the foreground update's progress.
      const parallel=await js(`(async()=>{retainedOperationProgress=null;operationStartedFloor=0;
        renderCheckProgress({running:true,phase:'updating_packages',operation_name:'update_all',started_at:new Date().toISOString()});
        await syncParallelCheckState({running:true,started_at:new Date().toISOString()});
        return {name:lastKnownOperationName,status:operationStatusBadgeEl.dataset.status};})()`);
      assert.equal(parallel.name,'update_all');assert.equal(parallel.status,'running');
      const classifications=await js(`(()=>{snapshot={counts:{},packages:[],migrations:[
        {name:'tool',status:'binary_available',executable:false,classification:{category:'standalone_candidate',reasons:['complete_local_evidence_without_shared_roles'],gaps:[],consumers:[]}},
        {name:'service',status:'archive_unknown',executable:false,classification:{category:'service',gaps:['fixture_gap'],consumers:[{name:'consumer'}]}},
        {name:'old',status:'archive_missing',executable:false}]};renderMacManager();const panel=document.getElementById('macManagerPanel');
        return {count:panel.querySelectorAll('.migration-classification').length,text:panel.textContent,buttons:panel.querySelectorAll('button').length};})()`);
      assert.equal(classifications.count,3);assert.equal(classifications.buttons,0);assert.ok(classifications.text.includes('consumer'));assert.ok(classifications.text.includes('fixture_gap'));
      await js(`stopProgressPolling();retainedOperationProgress=null;lastKnownOperationRunning=false;lastKnownParallelCheckRunning=false;setLoading(false);`);
      console.log(lang+': button completion, zero/deferred/mixed, refresh, cancel/error, stale, offline recovery, parallel and classification: PASS');
    }
    clearTimeout(watchdog);app.exit(0);
  } catch(error) {console.error(error);app.exit(1);}
});
'''

RELIABILITY_TESTS['tests/live-operation-ui.cjs'] = r'''// Explicit opt-in acceptance against an installed backend. No migration calls.
const {app,BrowserWindow,ipcMain}=require('electron');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const os=require('node:os');const http=require('node:http');
const root=process.env.BREW_PROGRESS_APP_ROOT;
const base=process.env.LIVE_SERVICE_URL;
const output=process.env.LIVE_ARTIFACT_DIR;
if(!root||!base||!output)throw Error('Set BREW_PROGRESS_APP_ROOT, LIVE_SERVICE_URL and LIVE_ARTIFACT_DIR explicitly');
if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(base))throw Error('Local backend required');
app.setPath('userData',fs.mkdtempSync(path.join(os.tmpdir(),'mac-live-ui-')));
fs.mkdirSync(output,{recursive:true});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function api(route,body){return new Promise((resolve,reject)=>{
 const data=body===undefined?null:JSON.stringify(body);
 const req=http.request(base+route,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','Content-Length':Buffer.byteLength(data)}:{},timeout:0},res=>{
  let text='';res.setEncoding('utf8');res.on('data',c=>text+=c);res.on('end',()=>{try{const result=JSON.parse(text);if(res.statusCode>=400)reject(Error(result.detail||result.error));else resolve(result);}catch(e){reject(e);}});
 });req.on('error',reject);if(data)req.write(data);req.end();
});}
let ready;const initialized=new Promise(resolve=>{ready=resolve;});
let requests=0,finished=0,lastResponse=null;
for(const [channel,route] of [['state:get','/api/state'],['settings:get','/api/settings'],['updates:history:get','/api/update-history'],['progress:get','/api/progress']])ipcMain.handle(channel,()=>api(route));
ipcMain.handle('app-update:check',()=>{ready();return {ok:true,updateAvailable:false};});
ipcMain.handle('check:run',async()=>{requests++;try{lastResponse=await api('/api/check',{});return lastResponse;}finally{finished++;}});
ipcMain.handle('update:all',async()=>{
 if(process.env.LIVE_ZERO_INSTALL!=='1')throw Error('Real update execution was not opted in');
 const plan=await api('/api/update-plan',{});
 if(plan.items.some(p=>p.ready))throw Error('Acceptance refuses updates: at least one installation would be attempted');
 requests++;try{lastResponse=await api('/api/update-all',{});assert.equal(lastResponse.updated_count,0);assert.equal(lastResponse.attempted_count,0);return lastResponse;}finally{finished++;}
});
const watchdog=setTimeout(()=>{console.error('Live acceptance timed out');app.exit(1);},15*60*1000);
app.whenReady().then(async()=>{
 const win=new BrowserWindow({show:false,width:1200,height:1100,webPreferences:{preload:path.join(root,'electron/preload.js'),contextIsolation:true,backgroundThrottling:false}});
 const js=code=>win.webContents.executeJavaScript(code);
 try{
  const health=await api('/health');assert.equal(health.operation.running,false);
  await win.loadFile(path.join(root,'renderer/index.html'));await initialized;
  await js(`window.confirm=()=>true;window.alert=()=>{};void 0;`);
  const action=process.env.LIVE_ZERO_INSTALL==='1'?'updateAllBtn':'checkNowBtn';
  console.log('Invoking packaged renderer button:',action,'against',base);
  await js(`document.getElementById('${action}').click();`);
  for(let attempt=0;attempt<1700&&!finished;attempt++){await delay(500);if(attempt%20===0){const p=await api('/api/progress');console.log('Live progress:',p.progress.phase,p.progress.current_package||'—');}}
  assert.equal(requests,1);assert.equal(finished,1);assert.ok(lastResponse?.progress,'Missing final captured progress');
  await js(`(async()=>{const until=Date.now()+10000;while(busyLoading&&Date.now()<until)await new Promise(r=>setTimeout(r,20));if(busyLoading)throw Error('Busy state did not finish');})()`);
  const results=[];
  for(const lang of ['he','en']){
   await js(`i18n.setLang('${lang}');refreshStaticText();`);
   const before=await js(`({status:operationStatusBadgeEl.dataset.status,loader:loaderEl.classList.contains('visible'),summary:document.getElementById('operationResultSummary').textContent,offers:snapshot.migrations?.length,classified:snapshot.migrations?.filter(o=>!!o.classification).length})`);
   assert.equal(before.loader,false);assert.ok(['completed','warning'].includes(before.status));
   if(action==='checkNowBtn')assert.equal(before.offers,before.classified);
   await js(`refreshStatusBtn.click();`);await delay(1000);
   assert.equal(await js(`operationStatusBadgeEl.dataset.status`),before.status);
   await js(`(()=>{document.getElementById('operationResultSummary').scrollIntoView({block:'start'});})()`);
   win.showInactive();
   await js(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);
   await delay(250);fs.writeFileSync(path.join(output,`result-${lang}.png`),(await win.webContents.capturePage()).toPNG());
   results.push({lang,...before});
  }
  fs.writeFileSync(path.join(output,'response.json'),JSON.stringify(lastResponse,null,2));
  const hash=file=>require('node:crypto').createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const unpacked=root.endsWith('app.asar')?root+'.unpacked':root;
  const code={version:JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8')).version,
    renderer:hash(path.join(root,'renderer/app.js')),backend:hash(path.join(unpacked,'backend/brew-updates-service.py')),
    evidence:hash(path.join(unpacked,'backend/installation_evidence.py'))};
  fs.writeFileSync(path.join(output,'ui-acceptance.json'),JSON.stringify({root,base,action,health,results,code},null,2));
  console.log('Packaged renderer + real backend acceptance: PASS',JSON.stringify(results));
  clearTimeout(watchdog);app.exit(0);
 }catch(e){console.error(e);app.exit(1);}
});
'''

T03_PROFILES = r'''"""Profiles for conservative Intel MacPorts migrations."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os, platform, re, tempfile, json, hashlib

@dataclass(frozen=True)
class Profile:
    name: str; ports: tuple[str,...]; commands: tuple[str,...]; version_args: tuple[str,...]

PROFILES={
 'terminal-notifier':Profile('terminal-notifier',('terminal-notifier',),('terminal-notifier',),('-version',)),
 'include-what-you-use':Profile('include-what-you-use',('include-what-you-use',),('include-what-you-use','iwyu_tool.py','fix_includes.py'),('--version',)),
}
def get(name): return PROFILES.get(name)
def intel_only():
    if platform.system()!='Darwin' or platform.machine()!='x86_64' or os.environ.get('SYSCTL_TRANSLATED')=='1':
        raise ValueError('Migration execution is supported only on native Intel macOS')
def command_paths(profile):
    return {name:Path('/opt/local/bin')/name for name in profile.commands}
def version_matches(text, target):
    return re.search(r'(?<![0-9])'+re.escape(str(target).split('_')[0])+r'(?![0-9])',text) is not None
def smoke(s, profile, target):
    paths=command_paths(profile); missing=[n for n,p in paths.items() if not p.is_file()]
    if missing: raise RuntimeError('MacPorts commands missing: '+', '.join(missing))
    version=s.run_cmd([str(paths[profile.commands[0]]),*profile.version_args],timeout=30,cancellable=True)
    if version.code or not version_matches(version.stdout+version.stderr,target): raise RuntimeError('Target version verification failed')
    result={'version':{'code':version.code,'output':(version.stdout+version.stderr)[-3000:]}}
    if profile.name=='include-what-you-use':
      sdk=s.run_cmd(['/usr/bin/xcrun','--show-sdk-path'],timeout=30,cancellable=True)
      if sdk.code or not Path(sdk.stdout.strip()).is_dir(): raise RuntimeError('Xcode SDK is unavailable')
      with tempfile.TemporaryDirectory(prefix='iwyu-smoke-') as d:
       root=Path(d); (root/'needed.h').write_text('struct Needed { int x; };\\n'); (root/'unused.h').write_text('struct Unused { int x; };\\n')
       source=root/'probe.cc'; source.write_text('#include "needed.h"\\n#include "unused.h"\\n#include <vector>\\nint main(){ Needed n{1}; std::vector<int> v{n.x}; return v[0]; }\\n')
       before=hashlib.sha256(source.read_bytes()).hexdigest()
       argv=[str(paths['include-what-you-use']),'-std=c++17','-isysroot',sdk.stdout.strip(),'-Xiwyu','--error=17',str(source)]
       run=s.run_cmd(argv,timeout=90,cancellable=True); output=run.stdout+run.stderr
       if run.code!=17 or 'unused.h' not in output: raise RuntimeError('IWYU analysis smoke test failed')
       cc=[{'directory':str(root),'command':'clang++ -std=c++17 -isysroot '+sdk.stdout.strip()+' '+str(source),'file':str(source)}]
       (root/'compile_commands.json').write_text(json.dumps(cc))
       env={'IWYU_BINARY':str(paths['include-what-you-use'])}
       tool=s.run_cmd(['/usr/local/bin/python3.14',str(paths['iwyu_tool.py']),'-p',str(root),str(source)],timeout=90,cancellable=True,env_override=env)
       if tool.code not in (0,17) or 'unused.h' not in tool.stdout+tool.stderr: raise RuntimeError('iwyu_tool.py smoke test failed')
       fix=s.run_cmd(['/usr/local/bin/python3.14',str(paths['fix_includes.py']),'--dry_run',str(source)],timeout=90,cancellable=True,env_override=env)
       if hashlib.sha256(source.read_bytes()).hexdigest()!=before: raise RuntimeError('fix_includes modified the smoke source')
       result.update(iwyu={'code':run.code,'output':output[-3000:]},tool={'code':tool.code,'output':(tool.stdout+tool.stderr)[-3000:]},fix={'code':fix.code,'output':(fix.stdout+fix.stderr)[-3000:]})
    return result
'''

T03_ENGINE = r'''"""Durable, profile-bound Homebrew to MacPorts transactions."""
from __future__ import annotations
from pathlib import Path
import hashlib,json,os,platform,shutil,time,uuid
import macports_provider as mp
import migration_profiles as profiles

SCHEMA=1
def _sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def _root(s): return s.DATA_DIR/'migrations'
def _write(s,p,v): s.atomic_write_json(p,v)
def _plan_path(s,pid): return _root(s)/'plans'/(pid+'.json')
def _transaction_path(s,tid): return _root(s)/'transactions'/tid
def _profile(name):
 p=profiles.get(name)
 if not p: raise ValueError('No supported migration profile for this package')
 return p
def _source(s,name):
 rows=s.fetch_installed_inventory(s.fetch_outdated_index()); row=next((x for x in rows if x.get('name')==name and x.get('kind')=='formula'),None)
 if not row or row.get('brew_installation_present') is False: raise ValueError('Homebrew source installation is absent')
 audit=row.get('inventory_verification',{})
 if audit.get('status')!='verified': raise ValueError('Installation evidence is unverified')
 active=Path(audit.get('active_keg') or '')
 receipt=active/'INSTALL_RECEIPT.json'
 if not active.is_dir() or not receipt.is_file(): raise ValueError('Active source keg or receipt is unavailable')
 versions=audit.get('manager_versions',[])
 if len(versions)!=1: raise ValueError('Multiple Homebrew kegs require manual review')
 return row,audit,active,receipt
def plan(s,name,action='migrate',transaction_id=None):
 profiles.intel_only()
 if action!='migrate': return recovery_plan(s,transaction_id,action)
 if name=='ngrok' or s.is_excluded(name,'formula'): raise ValueError('Package is excluded')
 profile=_profile(name); row,audit,active,receipt=_source(s,name)
 import mac_manager,automatic_alternatives,installation_evidence
 state=s.read_json(s.STATE_FILE) or {}; offer=next((o for o in state.get('migrations',[]) if o.get('name')==name),None)
 cls=(offer or {}).get('classification',{})
 if cls.get('category')!='standalone_candidate' or cls.get('gaps') or cls.get('consumers'): raise ValueError('Current classification does not permit an automatic migration')
 if not offer or offer.get('status')!='binary_available': raise ValueError('A current verified MacPorts binary route is required')
 graph=mp.preflight(s,list(profile.ports)); target=next(x['version'] for x in graph['nodes'] if x['name']==profile.ports[0])
 probe=mp.load_probe(); compared=automatic_alternatives.compare_versions(probe,row['installed_version'],row['latest_version'],{profile.ports[0]:target})
 if compared.get('status')!='version_eligible': raise ValueError(compared.get('detail','Target is not newer'))
 existing={x['name']:x for x in mp.inventory(s)}
 for node in graph['nodes']:
  old=existing.get(node['name'])
  if old and (old.get('installed_variants','')!=node['variants'] or old.get('installed_version')!=node['version']+'_'+node['revision']): raise ValueError('Existing MacPorts dependency differs: '+node['name'])
 consumers=mp.checked(s,[s.brew_bin(),'uses','--installed',name],timeout=90).splitlines()
 if consumers: raise ValueError('Documented Homebrew consumers block source removal: '+', '.join(consumers))
 pid=uuid.uuid4().hex; routes=Path(s.TOOLS_ROOT)/'migrations'/'routes.json'; paths=profiles.command_paths(profile)
 for command,path in paths.items():
  shim=Path(s.TOOLS_ROOT)/'bin'/command
  if shim.exists() and not shim.is_symlink(): raise ValueError('Unmanaged shim conflict: '+str(shim))
 data={'schema_version':SCHEMA,'id':pid,'action':'migrate','profile':profile.name,'profile_version':1,'created_at':time.time(),'expires_at':time.time()+900,'system':{'machine':platform.machine(),'macos':platform.mac_ver()[0]},'source':{'name':name,'version':row['installed_version'],'keg':str(active),'receipt_sha256':_sha(receipt)},'classification':cls,'target':{'ports':list(profile.ports),'version':target,'graph':graph},'commands':{k:str(v) for k,v in paths.items()},'routes_file':str(routes),'remove_homebrew':True,'consumers':consumers}
 data['fingerprint']=hashlib.sha256(json.dumps(data,sort_keys=True,default=str).encode()).hexdigest(); _plan_path(s,pid).parent.mkdir(parents=True,exist_ok=True);_write(s,_plan_path(s,pid),data);return data
def _load(s,pid):
 p=_plan_path(s,pid); data=s.read_json(p)
 if not data or data.get('schema_version')!=SCHEMA: raise ValueError('Migration plan is unavailable or obsolete')
 if data['expires_at']<time.time(): raise ValueError('Migration plan expired; request a new plan')
 return data
def _stage(s,journal,path,value): journal['stage']=value;journal['updated_at']=s.now_iso();_write(s,path/'journal.json',journal)
def approve(s,pid):
 old=_load(s,pid)
 if old.get('action')!='migrate': raise ValueError('Use recovery approval for recovery plans')
 fresh=plan(s,old['source']['name'])
 for key in ('source','target','classification','commands','remove_homebrew','consumers'):
  if old.get(key)!=fresh.get(key): raise ValueError('Migration changed; review a new plan')
 profile=_profile(old['profile']); tid=uuid.uuid4().hex; root=_transaction_path(s,tid); root.mkdir(parents=True); journal={'schema_version':SCHEMA,'transaction_id':tid,'plan_id':pid,'status':'running','stage':'preflight','started_at':s.now_iso(),'plan':old}; _write(s,root/'journal.json',journal)
 active=Path(old['source']['keg']); backup=root/'backup'; routes=Path(old['routes_file']); previous=routes.read_bytes() if routes.exists() else None
 try:
  _stage(s,journal,root,'backup'); shutil.copytree(active,backup/'keg',symlinks=True); (backup/'source-receipt.sha256').write_text(old['source']['receipt_sha256']);
  if _sha(backup/'keg'/'INSTALL_RECEIPT.json')!=old['source']['receipt_sha256']: raise RuntimeError('Backup receipt verification failed')
  _stage(s,journal,root,'install'); result=mp.privileged(s,'install',list(profile.ports));
  if result.code: raise RuntimeError(result.stderr or result.stdout)
  _stage(s,journal,root,'verify'); smoke=profiles.smoke(s,profile,old['target']['version']); journal['smoke']=smoke;_write(s,root/'journal.json',journal)
  _stage(s,journal,root,'activate'); routes.parent.mkdir(parents=True,exist_ok=True); saved=s.read_json(routes) or {}; saved[profile.name]={'provider':'macports','profile':profile.name,'commands':old['commands'],'verified_at':s.now_iso(),'transaction_id':tid};_write(s,routes,saved)
  _stage(s,journal,root,'cleanup'); consumers=mp.checked(s,[s.brew_bin(),'uses','--installed',profile.name],timeout=90).splitlines()
  if consumers: raise RuntimeError('Consumers appeared before cleanup: '+', '.join(consumers))
  out=mp.checked(s,[s.brew_bin(),'uninstall','--formula',profile.name],timeout=120,env_override={'HOMEBREW_NO_AUTO_UPDATE':'1','HOMEBREW_NO_AUTOREMOVE':'1','HOMEBREW_NO_INSTALL_CLEANUP':'1'});journal['brew_uninstall_output']=out[-3000:]
  _stage(s,journal,root,'postcheck');journal['postcheck']=profiles.smoke(s,profile,old['target']['version']);journal.update(status='completed',completed_at=s.now_iso());_stage(s,journal,root,'completed');return journal
 except Exception as e:
  journal.update(status='needs_attention',error=str(e),completed_at=s.now_iso());_write(s,root/'journal.json',journal);raise
def recovery_plan(s,tid,action):
 root=_transaction_path(s,tid);j=s.read_json(root/'journal.json')
 if not j or j.get('status')=='completed': raise ValueError('No recoverable transaction')
 if action not in ('resume','rollback'): raise ValueError('Unsupported recovery action')
 data={'schema_version':SCHEMA,'id':uuid.uuid4().hex,'action':action,'transaction_id':tid,'created_at':time.time(),'expires_at':time.time()+900,'journal':j}
 _plan_path(s,data['id']).parent.mkdir(parents=True,exist_ok=True);_write(s,_plan_path(s,data['id']),data);return data
'''

T03_VERIFY = r'''#!/usr/bin/env python3
"""Run isolated T03 checks or the user-approved real migration."""
from __future__ import annotations
import argparse,json,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; RECOVERY=Path.home()/'.local/share/brew-update-manager/recovery/t03-1.3.3'
def api(path,body=None):
 data=json.dumps(body).encode() if body is not None else None
 req=urllib.request.Request('http://127.0.0.1:8765'+path,data=data,headers={'Content-Type':'application/json'} if data else {})
 with urllib.request.urlopen(req,timeout=1800) as r:return json.load(r)
def main():
 p=argparse.ArgumentParser();p.add_argument('--checks',action='store_true');p.add_argument('--acceptance',action='store_true');p.add_argument('--pilot');a=p.parse_args()
 if a.checks:
  import subprocess;raise SystemExit(subprocess.call(['/usr/local/bin/python3.14',str(ROOT/'tools/implement-mac-manager.py'),'t03-verify']))
 if not(a.acceptance and a.pilot=='include-what-you-use'):raise SystemExit('Use --checks or --acceptance --pilot include-what-you-use')
 out=RECOVERY/('acceptance-'+str(time.time_ns()));out.mkdir(parents=True); report={'started_at':time.time(),'pilot':a.pilot}
 try:
  health=api('/health');report['health']=health
  if health.get('version')!='1.3.3' or 'migration-v1' not in health.get('capabilities',[]): raise RuntimeError('Installed app is not the verified 1.3.3 migration build')
  plan=api('/api/migrations/plan',{'name':a.pilot})['plan'];report['plan']=plan
  print(json.dumps({'source':plan['source'],'target':plan['target']['version'],'ports':plan['target']['ports'],'commands':plan['commands']},ensure_ascii=False,indent=2))
  if input('לאשר את המעבר המדויק המוצג? הקלד YES: ').strip()!='YES': report['result']='not_approved';code=2
  else:
   report['result']=api('/api/migrations/approve',{'id':plan['id']})
   if report['result'].get('migration',{}).get('status')!='completed': raise RuntimeError('Migration did not complete')
   # The backend verifies postcheck before returning; capture the final live state.
   report['after']=api('/api/state')
   # Restart only the idle update-manager app, then prove a new backend owns the route.
   import subprocess
   previous_pid=health.get('pid')
   subprocess.run(['/usr/bin/osascript','-e','tell application "Mac Update Manager" to quit'],check=True)
   subprocess.run(['/usr/bin/open','/Applications/Mac Update Manager.app'],check=True)
   deadline=time.time()+45
   while time.time()<deadline:
    try:
     restarted=api('/health')
     if restarted.get('pid')!=previous_pid and restarted.get('version')=='1.3.3': break
    except Exception: pass
    time.sleep(1)
   else: raise RuntimeError('App restart could not be verified')
   report['restarted_health']=restarted; code=0
 except Exception as e: report['error']=str(e);code=1
 report['finished_at']=time.time();(out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));(out/'summary.md').write_text('# T03 acceptance\\n\\n'+json.dumps({'result':report.get('result'),'error':report.get('error')},ensure_ascii=False,indent=2));print('דוח:',out);return code
if __name__=='__main__':raise SystemExit(main())
'''
T03_TESTS = r'''import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import migration_profiles as p
class Profiles(unittest.TestCase):
 def test_only_two_fixed_profiles(self):
  self.assertEqual(set(p.PROFILES),{'terminal-notifier','include-what-you-use'})
  self.assertEqual(p.get('include-what-you-use').commands,('include-what-you-use','iwyu_tool.py','fix_includes.py'))
 def test_intel_gate(self):
  with patch('migration_profiles.platform.machine',return_value='arm64'):
   with self.assertRaisesRegex(ValueError,'Intel'):p.intel_only()
 def test_paths_are_fixed(self):
  self.assertTrue(all(str(x).startswith('/opt/local/bin/') for x in p.command_paths(p.get('include-what-you-use')).values()))
if __name__=='__main__':unittest.main()
'''
T03_VALIDATION = '''# T03 — מנגנון מעבר לכלים עצמאיים\n\nגרסה 1.3.3 מוסיפה מסלול Mac Intel בלבד למעבר Homebrew ל־MacPorts עבור `include-what-you-use`. המימוש נבדק בבדיקות מבודדות; המעבר האמיתי לא בוצע בזמן הפיתוח.\n\nהפעלת בדיקות בלבד:\n\n```bash\n/usr/local/bin/python3.14 tools/verify-migration.py --checks\n```\n\nהפעלת בדיקת הקבלה והמעבר לאחר אישור מפורש:\n\n```bash\n/usr/local/bin/python3.14 tools/verify-migration.py --acceptance --pilot include-what-you-use\n```\n\nהפקודה יוצרת דוח תחת `~/.local/share/brew-update-manager/recovery/t03-1.3.3/`. T03 נסגרת רק אם הדוח מאשר את שלוש פקודות IWYU, הסרת עותק Homebrew, בעלות MacPorts והפעלה מחדש.\n'''

def t03_outputs(read):
    out={
      'backend/migration_profiles.py':T03_PROFILES,
      'backend/migration_engine.py':T03_ENGINE,
      'backend/brew-updates-service.py':t03_service(read('backend/brew-updates-service.py')),
      'backend/automatic_alternatives.py':read('backend/automatic_alternatives.py').replace("offer.get('name') == 'terminal-notifier'", "offer.get('name') in {'terminal-notifier','include-what-you-use'}"),
      'tests/test_migration_profiles.py':T03_TESTS,
      'docs/T03_VALIDATION.md':T03_VALIDATION,
      'tools/verify-migration.py':T03_VERIFY,
      'backend/mac_manager.py':read('backend/mac_manager.py').replace("'ok': catalog_state['fresh'], 'checked_at': s.now_iso(),", "'ok': True, 'checked_at': s.now_iso(),").replace("executable=p['name']=='terminal-notifier'", "executable=p['name'] in {'terminal-notifier','include-what-you-use'}")+"\n# Bundle 003 keeps the verified notifier pilot intact and delegates new profiles.\n_legacy_migration_plan=migration_plan\n_legacy_approve=approve\nimport migration_engine as _migration_engine\ndef migration_plan(s,name): return _legacy_migration_plan(s,name) if name=='terminal-notifier' else _migration_engine.plan(s,name)\ndef approve(s,plan_id):\n old=s.read_json(s.DATA_DIR/'migration-plan.json') or {}\n return _legacy_approve(s,plan_id) if old.get('name')=='terminal-notifier' and not old.get('schema_version') else _migration_engine.approve(s,plan_id)\ndef migration_recovery_plan(s, transaction_id, action): return _migration_engine.recovery_plan(s, transaction_id, action)\n",
      'package.json':read('package.json').replace('"version": "1.3.2"','"version": "1.3.3"'),
      'package-lock.json':read('package-lock.json').replace('"version": "1.3.2"','"version": "1.3.3"'),
      'docs/MAC_MANAGER_TASKS.md':read('docs/MAC_MANAGER_TASKS.md').replace('### T03 — מנגנון מעבר משותף לכלים עצמאיים','### T03 — מנגנון מעבר משותף לכלים עצמאיים\n\nסטטוס: המימוש מוכן לבדיקה מקומית; סגירה מחייבת את פקודת הקבלה של IWYU.'),
    }
    return out
T03_RECOVERY=Path.home()/'.local/share/brew-update-manager/recovery/t03-1.3.3'
def t03_service(source):
    source=source.replace('"/api/migrations/approve", "/api/macports/sync"}', '"/api/migrations/approve", "/api/migrations/recovery-plan", "/api/macports/sync"}')
    source=source.replace('if self.path == "/api/migrations/approve":\n                    result = mac_manager.approve(sys.modules[__name__], body.get("id"))', 'if self.path == "/api/migrations/recovery-plan":\n                    return {"ok": True, "plan": mac_manager.migration_recovery_plan(sys.modules[__name__], body.get("transaction_id"), body.get("action"))}\n                if self.path == "/api/migrations/approve":\n                    result = mac_manager.approve(sys.modules[__name__], body.get("id"))')
    source=source.replace('self._send_json(200, run_exclusive("migration", operation))', 'self._send_json(200, run_with_progress("migration", operation))')
    source=source.replace('except Exception as exc:\n            self._send_json(400, {"ok": False, "detail": str(exc)})\n        return True\n\n    def do_GET(self):', 'except Exception as exc:\n            self._send_json(400, {"ok": False, "detail": str(exc), "progress": getattr(exc, "operation_progress", None)})\n        return True\n\n    def do_GET(self):')
    source=source.replace('"protocol": 6,', '"protocol": 6,\n                    "version": "1.3.3",\n                    "pid": os.getpid(),\n                    "data_dir": str(DATA_DIR),\n                    "capabilities": ["migration-v1", "migration-recovery"],\n                    "backend_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),')
    source=source.replace('import argparse\n', 'import argparse\nimport hashlib\n',1)
    return source
def t03_manifest():
    p=T03_RECOVERY/'manifest.json';return json.loads(p.read_text()) if p.exists() else None
def t03_apply(apply=False):
    targets=t03_outputs(lambda x:(ROOT/x).read_text())
    old=t03_manifest()
    if old:
      for rel,r in old['files'].items():
       if not (ROOT/rel).is_file() or digest((ROOT/rel).read_bytes())!=r['after']: raise RuntimeError('Bundle 003 refuses later edit: '+rel)
      print('Bundle 003 already applied.');return
    if not apply:
      print('Bundle 003 can apply cleanly to '+str(len(targets))+' files.');return
    T03_RECOVERY.mkdir(parents=True,exist_ok=True); records={}
    for rel,content in targets.items():
      p=ROOT/rel;before=p.read_bytes() if p.exists() else None;after=content.encode();records[rel]={'before':digest(before) if before is not None else None,'after':digest(after)}
      if before!=after:
       if before is not None:(T03_RECOVERY/'before'/rel).parent.mkdir(parents=True,exist_ok=True);(T03_RECOVERY/'before'/rel).write_bytes(before)
       p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(after)
    (T03_RECOVERY/'manifest.json').write_text(json.dumps({'version':'1.3.3','files':records},indent=2));print('Applied bundle 003.')
def t03_restore():
 old=t03_manifest()
 if not old:raise RuntimeError('Bundle 003 is not applied')
 for rel,r in old['files'].items():
  p=ROOT/rel
  if not p.is_file() or digest(p.read_bytes())!=r['after']:raise RuntimeError('Protected restore refuses later edit: '+rel)
 for rel,r in old['files'].items():
  p=ROOT/rel
  if r['before'] is None:p.unlink()
  else:p.write_bytes((T03_RECOVERY/'before'/rel).read_bytes())
 (T03_RECOVERY/'manifest.json').rename(T03_RECOVERY/('restored-'+str(time.time_ns())+'.json'));print('Bundle 003 restored.')
def t03_verify():
 t03_apply(False)
 run(['/usr/local/bin/python3.14','-m','unittest','discover','-s','tests','-p','test_*.py'])
 for p in ['backend/migration_profiles.py','backend/migration_engine.py','electron/main.js','electron/preload.js','renderer/app.js','renderer/mac-manager.js']:run(['/usr/local/bin/python3.14','-m','py_compile',p] if p.endswith('.py') else ['node','--check',p])
 run(['node','tests/service-http.cjs']);run([str(ROOT/'node_modules/.bin/electron'),'tests/progress-ui.cjs']);run([str(ROOT/'node_modules/.bin/electron'),'tests/operation-results-ui.cjs']);run(['git','diff','--check'])
def t03_main(mode):
 if mode=='t03-check':t03_apply(False)
 elif mode=='t03-apply':t03_apply(True)
 elif mode=='t03-verify':t03_verify()
 elif mode=='t03-pack':t03_verify();run([str(ROOT/'node_modules/.bin/electron-builder'),'--dir','--mac','--x64','--publish','never'])
 elif mode=='t03-install':
  # Reuse the existing guarded installer, but bind it to bundle 003 verification.
  global verify
  verify=t03_verify
  install()
 elif mode=='t03-restore':t03_restore()
 else:raise RuntimeError('Unknown bundle 003 mode')

DIRECTION_DOCUMENTS = {
    'docs/PRODUCT_GOAL.md': '''# מטרת המוצר והחלטות העבודה

עודכן: 20.10.2026, בעקבות הבהרת המשתמש. זהו מקור ההחלטות למימוש הבא.
מסמך זה מתאר את היעד; מצב המימוש נמצא ב־[מסמך המשימות](MAC_MANAGER_TASKS.md).
אם תכנון קודם דורש אישור מעבר נפרד לכל חבילה או בדיקות מלאות בידי הסוכן,
ההחלטות במסמך זה מחליפות אותו.

## חוויית השימוש הנדרשת

1. המשתמש לוחץ ״התחל בדיקה״.
2. המערכת מזהה התקנות ועדכונים ובודקת את הספקים הרלוונטיים.
3. לכל חבילה היא בוחרת מסלול עם חבילה מוכנה וכל תלויותיה, המתאים לגרסת
   macOS ול־Mac Intel, עם הגרסה העדכנית ביותר מבין המסלולים הנתמכים שנבדקו.
4. התוצאות מציגות גרסה מותקנת, גרסת יעד, ספק נוכחי, ספק מבצע וסיבה לבחירה.
   מעבר ספק מתוכנן מוצג כחלק מהעדכון לפני הלחיצה.
5. המשתמש בוחר הכול או חלק ולוחץ ״עדכן הכול״ או ״עדכן נבחרים״.
   הלחיצה מאשרת את הפעולות המוצגות, כולל המעברים; אין אשף או אישור נוסף
   לכל חבילה. דרישת סיסמת מנהל מערכת של macOS עשויה להישאר כשנחוצה.
6. המערכת מתקינה, מאמתת ומסכמת מה עודכן, מה דולג ומה נכשל, עם הסיבה.
7. מעבר מוצלח נשמר: בסריקה הבאה ובפתיחה מחדש החבילה מזוהה אצל הספק החדש,
   והעדכון הבא נבדק מולו, גם כשעותק Homebrew כבר אינו קיים.

## מדיניות הבחירה

- היעד הנוכחי הוא Mac Intel אמיתי. אין להרחיב קבלה ל־Apple Silicon/Rosetta
  כחלק מהעבודה הזאת. המנגנון כללי למחשבי Intel, בלי רשימת התקנות של משתמש
  מסוים, גרסאות קבועות או נתיבי משתמש מקובעים.
- Homebrew, MacPorts ו־Pixi הם ספקים רצויים. קיימת תמיכה חלקית בלבד כיום;
  אין להציג יעד מוצר כאילו כל הספקים כבר תומכים במעבר כל חבילה.
- מסלול דורש התאמת זהות החבילה, ארכיטקטורה, מערכת, גרסה וכל תלויות ההרצה.
  זמינות קובץ בלבד אינה מספיקה. אין נפילה לבנייה מקוד מקור.
- מבין מסלולים שניתן לבצע ולבדוק, לבחור בגרסת upstream החדשה המתאימה
  ביותר; לא לדרוש התאמה ליעד Homebrew כשאינו מסופק בינארית. אין ירידה בגרסה.
  revisions נבדקים בתוך ספק; אין להשוות מספרי אריזה בין ספקים כגרסאות תוכנה.
- בשוויון גרסה והתאמה, לשמר את הספק שכבר מנהל את ההתקנה כדי למנוע מעברים
  מיותרים. כשאין לו מסלול מתאים או כשיש חלופה חדשה מתאימה, לבדוק חלופות מחדש.
- רישום ספק חדש מתבצע אחרי אימות הצלחה, ומייצג בעלות; הגרסה והעדכניות
  ממשיכות להגיע מהמלאי והקטלוג החיים של הספק, ולא מרשומה שמורה בלבד.
- כשל תקשורת או מידע חסר יוצגו כלא מאומתים, ולא כ״אין חבילה״.
- מסלול לא נתמך או תלויים/שירותים שלא טופלו יחסמו רק את החבילה המושפעת,
  עם סיבה ברורה. פישוט הממשק אינו הרשאה להסיר התקנה בלי אימות ושחזור.
- לשמר החרגות, ובפרט ngrok, הגדרות, תזמון, היסטוריה וזהות הרשאת ההתראות.
  אין לסרוק פרויקטים פרטיים או לחסום רק בשל אפשרות לסקריפט פרטי.

## משמעות הפיילוטים

`terminal-notifier` ו־`include-what-you-use` הם מקרי בדיקה טכניים למעבר.
הם אינם המטרה העסקית ואינם תפריט שהמשתמש צריך ללמוד. הצלחתם אינה הוכחה
למעבר ספריות, שירותים או כל חבילה אחרת. לא מחליפים פיילוט או מעבירים חבילה
אמיתית בזמן פיתוח תיעוד או קוד בלי ההרשאה המתאימה.

## חלוקת העבודה בבדיקות

- הסוכן מממש קוד, כותב בדיקות ומרכז אותן בסקריפט. במהלך הפיתוח מבצע רק
  אימות קצר וממוקד הנדרש לשינוי, כגון תחביר, תקינות קישורים או בדיקה נקודתית.
- המשתמש מריץ בפקודה אחת את חבילת הבדיקות המלאה: Python, HTTP, ממשק,
  רגרסיות, אריזה/קבלה לפי השינוי, ודוח שמור. אין להריץ את כולן מראש ואז
  למסור למשתמש סקריפט שחוזר על אותה עבודה, בלי בקשה מפורשת ממנו.
- בדיקות מבודדות אינן משנות חבילות או הגדרות אמיתיות. פעולה אמיתית בסקריפט
  דורשת הצגת הפעולות ואישור כולל אחד. ההפרדה היא שלב בטיחות בסקריפט הקבלה,
  ואינה דרישה לאישורי מעבר נוספים בממשק העדכון הרגיל.
- הדוח יבדיל בין בדיקות קוד, סימולציה ואימות במחשב, ויתעד גרסאות וחתימות,
  כשל, דילוג או מצב לא מאומת. דילוג אינו PASS; ניתוק אינו הוכחת הצלחה או כשל.
- קוראים את הדוח ומתקנים כשלים ממוקדים. אין להריץ מחדש הכול כשאותו קוד כבר
  נבדק, אלא אם שינוי או כשל מצדיקים זאת. עלות הטוקנים הקודמת לא נמדדה.
- מסירה ללא ריצת המשתמש תסומן ״מימוש נמסר; בדיקות מלאות טרם הורצו״.
  אין לטעון שהמטרה הושגה לפני דוח שמוכיח את תנאי הקבלה הרלוונטיים.

## גבול המצב הנוכחי

גרסת המקור היא 1.3.3. קיימים מלאי משולב, מסלולי עדכון, סיווג מועמדות
ותשתית מעבר חלקית. עדיין צריך לחבר בחירת ספק, מעבר ושמירת בעלות למסלול
״עדכן נבחרים/הכול״ ולחבר Pixi לגילוי ולבחירה הכלליים.
הסקריפט הקיים `tools/verify-migration.py` מוגבל לפיילוט T03; הוא אינו
בדיקת הקבלה הכוללת ליעד המתואר כאן. פירוט ב־[דוח T03](T03_VALIDATION.md).
''',
    'docs/MAC_MANAGER_TASKS.md': '''# מנהל עדכוני Mac — משימות והמשך עבודה

עודכן: 20.10.2026 לאחר הבהרת המטרה וחלוקת הבדיקות עם המשתמש.
גרסת המקור: 1.3.3 לפי package.json. בעדכון תיעוד זה לא נבדקה אפליקציה חיה
ולא הורצו בדיקות מוצר. תוצאות קודמות הן תוצאות היסטוריות בלבד.

## המטרה המחייבת

בדיקה אחת → בחירת ספק אוטומטית לפי חבילה מוכנה וגרסה מתאימה ל־Mac Intel →
בחירת חבילות → לחיצה על עדכון → ביצוע ושמירת הספק החדש לאחר מעבר מוצלח.
המעבר הוא חלק מהעדכון המוצג למשתמש, בלי אישור נוסף לכל חבילה.

[מטרת המוצר והחלטות העבודה](PRODUCT_GOAL.md) היא המסמך המחייב.
[תיעוד המימוש](MAC_MANAGER.md) מתאר את הקוד הקיים והפערים מול המטרה.
אין לפרש קיום קובץ או API כהשלמת מעבר אמיתי, שחזור או חוויית העדכון הרצויה.

## תמונת מצב

| משימה | מצב | מה הוכח ומה חסר |
| --- | --- | --- |
| T01 — אמינות מצב ומלאי | הושלמה לפי דוח 1.3.2 | חיווי סופי, טיפול בתשובות ישנות, receipts וקגים; תיקון LLVM עצמו פתוח ב־T06 |
| T02 — סיווג מועמדות | הושלמה לפי דוח 1.3.2 | כלי/ספרייה/שירות/חסר מידע עם ראיות; סיווג אינו היתר ביצוע |
| T03 — מעבר ובעלות מתמשכת | מומשה חלקית, פתוחה | נוספו מנוע ופרופילים ב־1.3.3; הפעלה, שחזור, רגרסיות ומעבר אמיתי אינם מוכחים במלואם |
| T04 — Pixi ובחירת ספקים | פתוחה | סביבות פרטניות קיימות; גילוי ובחירה כלליים עוד נדרשים |
| T05 — מסלולים רשמיים | המשך, עדיפות נמוכה | רק אחרי מסלול Homebrew/MacPorts/Pixi הרגיל; אין להרחיב עכשיו ללא צורך |
| T06 — חבילות חסומות | פתוחה | ממצאים היסטוריים לאימות; תיקון נפרד לכל חסם מבוסס |
| T07 — עדכון אחיד ובדיקות בפקודה | פתוחה, עדיפות מיידית עם T03/T04 | אין עדיין זרימה כללית מוכחת של בחירת ספק, עדכון ושמירת בעלות |

## החלטות עבודה

- Intel, חבילות בינאריות וכל תלויותיהן; בלי נפילה לבניית מקור או ירידה בגרסה.
- Homebrew, MacPorts, Pixi לפי התאמה. בשוויון לשמר ספק; לבחור גרסה חדשה
  מתאימה ממסלול נתמך ולבדוק מחדש חלופה כשהספק הקיים אינו נותן מענה.
- לחיצת עדכון מאשרת את הפעולות המוצגות, כולל מעבר ספק. דרישת הרשאת macOS
  כשנחוצה אינה אישור מעבר נוסף שהאפליקציה צריכה להמציא.
- לשמור ngrok מוחרג, הגדרות, היסטוריה, תזמון וזהות התראות. לא לחפש פרויקטים
  פרטיים; אפשרות לסקריפט פרטי אינה חסם. תלויים מתועדים ושירותים דורשים טיפול.
- כל שינוי מוצר, בדיקות ותיעוד מופק דרך `tools/implement-mac-manager.py`;
  את הסקריפט עורכים ב־apply_patch. כל פקודות הטרמינל בסשן אינטראקטיבי.
- לשמר את העבודה הלא מחויבת והגיבויים. אין להפעיל apply/document-tasks ישנים
  כדי לתקן תיעוד, ואין לאפס README או מניפסט קודם בגלל כשל תנאי קדם.
- הסוכן יבצע אימות קצר וממוקד בלבד; המשתמש יריץ את הבדיקות המלאות בסקריפט.
  הוראה זו מחליפה את החלוקה הישנה שבה הסוכן הריץ הכול והמשתמש רק פיילוט.

## T01 — אמינות מצב הפעולה והמלאי

**מניע:** למנוע ״ממתין״ או טעינה אחרי סיום, והחלטות על בסיס גרסה שאינה פעילה.
**מצב:** הושלמה לפי [ראיות 1.3.2](T01_T02_VALIDATION.md), 20.10.2026.

- [x] תוצאה סופית שאינה נמחקת ב־finally, ברענון או בשינוי שפה.
- [x] סיכום עדכונים, כשל, ביטול ודילוגים, לרבות אפס התקנות והכול נדחה.
- [x] התייחסות לתשובות ישנות, ניתוק ובדיקה במקביל לעדכון.
- [x] הפרדת גרסה מדווחת, קג פעיל, receipt, נתיב פקודה ו־revision.

**המשך:** לשמר ברגרסיות. אין להסיק מדוח 1.3.2 שקוד 1.3.3 כולו נבדק.
תיקון clang/LLVM שהתגלה אינו כלול בסגירת T01.

## T02 — סיווג מועמדות למעבר

**מניע:** לדעת מה אפשר להעביר ככלי עצמאי ומה דורש טיפול בתלויים/שירותים.
**מצב:** הושלמה לפי [ראיות 1.3.2](T01_T02_VALIDATION.md), 20.10.2026.

- [x] איסוף ראיות מקומי ותלויים, בלי חיפוש בפרויקטים פרטיים.
- [x] סיווג עצמאי / ספרייה או סביבה משותפת / שירות / חסר מידע.
- [x] נימוק, פער מידע וראיות, בנפרד מזמינות בינארית ומיכולת ביצוע.

**המשך:** להשתמש באותו איסוף לפני ביצוע; סיווג שמור אינו תחליף לאימות עדכני.

## T03 — השלמת המעבר ושמירת הספק

**מניע:** חלופה שמופיעה ברשימה צריכה להפוך לעדכון עובד, עם בעלות עקבית.
**מצב:** תשתית חלקית קיימת; [סקירת הפערים](T03_VALIDATION.md). פתוחה.

- [x] נוספו migration_engine.py ו־migration_profiles.py, תוכניות ויומני עסקה.
- [x] נוסף מסלול קוד ל־IWYU לצד פיילוט ההתראות הישן; אין בכך הוכחת קבלה.
- [ ] לאמת נתונים עדכניים מאיסוף T02 משותף, שינוי תוכנית, החרגות ונעילה בין תהליכים.
- [ ] להשלים גיבוי מאומת, ביטול, המשך ו־rollback אמיתיים בלי דריסת שינוי עצמאי.
- [ ] להשלים הפעלת פקודות, בעלות על shims ובדיקת PATH ושימוש לפני הסרת מקור.
- [ ] להשלים רישום ספק רק אחרי הצלחה, ללא רשומת tool כפולה מול מלאי MacPorts.
- [ ] לגלות עדכונים עתידיים דרך הספק החדש ולהריץ בדיקת שימוש אחרי עדכונו.
- [ ] לשמר helper וזהות התראות קיימים; אין להעביר LLVM כחלק מפיילוט IWYU.
- [ ] לחבר את המעבר לביצוע העדכונים הנבחרים ב־T07, ללא אשף אישור נוסף.

**תנאי השלמה:** דוח המשתמש מוכיח מעבר נתמך, אימות שימוש, גיבוי ושחזור מבוקר,
ומלאי ועדכון מהספק החדש אחרי פתיחה מחדש. בדיקות כשל מבודדות עוברות.
הפיילוטים הם ראיות הנדסיות בלבד; הם אינם הגדרת המוצר או משימה שהמשתמש צריך לנהל.

## T04 — גילוי Pixi ובחירת ספק אוטומטית

**מניע:** לבחור מסלול Intel מתאים מכל הספקים הנתמכים, מעבר למיפויי MacPorts.
**מצב:** פתוחה; Ansible/Pandoc במסלולים קיימים אינם הוכחה למנגנון הכללי.

- [ ] להגדיר מודל הצעה משותף: זהות תוכנה, בעלים נוכחי, ספק מבצע, גרסאות,
  התאמה למערכת, סגירת תלויות, זמינות בינארית, יכולת ביצוע ונימוק בחירה.
- [ ] לחבר Homebrew bottles, MacPorts archives ו־Pixi osx-64 למודל זה;
  לפתור תלויות ולאמת קבצים ומטא־נתונים, תוך שמירת כשל ספק כמידע חלקי.
- [ ] לבחור את הגרסה המתאימה החדשה ביותר מבין המסלולים שניתנים לביצוע;
  בשוויון לשמר בעלים, בלי downgrade ובלי השוואת revisions בין ספקים.
- [ ] לחבר ביצוע Pixi למנגנון המעבר ולשמור סביבות ונתיבים קיימים.
- [ ] לשמור בעלות ולבדוק עדכון עתידי גם כשאין עותק Homebrew.

**תנאי השלמה:** סריקה רגילה מציגה בחירה ונימוק; עדכון נבחר מתבצע בספק שנבחר,
ונשמר לאחר פתיחה מחדש. חבילה חסומה לא מפילה חבילות עם מסלול תקין.

## T05 — ספקים רשמיים עצמאיים

**מניע:** לתת מענה עתידי כשמנהלי החבילות אינם מספקים מסלול Intel מתאים.
**מצב:** פתוחה, אחרי זרימת העדכון המרכזית; אין הרחבת מימוש אוטומטית במסגרת T03.

- [ ] לבדוק מסלול רשמי רק לחבילה שנדרש עבורה פתרון; Nmap הוא ממצא היסטורי.
- [ ] להגדיר גילוי גרסה, אימות קובץ, התקנה, שימוש, בעלות ועדכון עתידי.

**תנאי השלמה:** מסלול נתמך שנבדק מקצה לקצה; קישור הורדה אינו מסלול שהושלם.

## T06 — טיפול בחסמים קיימים

**מניע:** לטפל במקרים שהמנגנון הכללי עדיין אינו יכול לבצע ולהציג סיבה מדויקת.
**מצב:** פתוחה. הממצאים הבאים היסטוריים ודורשים אימות לפני שינוי התקנות.

- [ ] LLVM: בדוח 1.3.2 הקג הפעיל 22.1.5, עותק 23.1.3_2 ללא receipt,
  ו־clang נכשל בגלל Z3 חסר. תיקון נפרד; לא להחליף ספריות ABI באופן שרירותי.
- [ ] Poppler/Pixi: תועד פתרון תלויות osx-64, לא התקנה או בדיקת שימוש.
- [ ] libunistring: לבחון מחדש חסם משותף לחבילות שנמצאו, בלי ערבוב ספקי ספריות.
- [ ] לאמת זמינות ומיפויים של nmap, socat, libtensorflow, merve ו־whisper.cpp.
- [ ] לספריות ושירותים להכין טיפול בתלויים, ABI, הגדרות ונתונים לפני ביצוע.

**תנאי השלמה:** לכל מקרה עדכני יש פעולה שהוכחה או חסם מתוארך ופעולה הבאה.
אין חובה מלאכותית למצוא עדכון לכל התקנה או להעביר ספרייה במסלול כלי עצמאי.

## T07 — עדכון נבחרים/הכול וסקריפט בדיקות אחד

**מניע:** המשתמש מפעיל בדיקה ועדכון; המערכת מטפלת בבחירת ספק ובמעבר.
**מצב:** פתוחה. כפתור ״עדכן הכול״ הקיים אינו מממש עדיין את הזרימה הכללית.

- [ ] להציג ברשימה אחידה מקור, יעד, ספק מבצע, סיבת בחירה ומעבר מתוכנן.
- [ ] להעביר את כל החבילות הנבחרות לאותו מתכנן וביצוע, גם כשנדרש מעבר ספק.
- [ ] הלחיצה על עדכון מאשרת את הפעולות המוצגות; לבטל דרישת אישור מעבר נפרד.
  שינוי מהותי מאז התצוגה יוחזר לסקירת הבחירה המעודכנת לפני שינוי התקנות.
- [ ] להציג חסם רק לחבילה המושפעת, ולשמר תוצאה סופית, ביטול ושתי שפות.
- [ ] להכין פקודה אחת לבדיקות המלאות שהמשתמש מריץ: בדיקות קוד מבודדות,
  HTTP, ממשק ורגרסיות; ואימות אמיתי לאחר הצגת הפעולות ואישור כולל אחד.
- [ ] לשמור דוח וחתימות, להפריד סימולציה ואימות מחשב, ולא לסמן דילוג כעובר.
- [ ] לקשור דוח לתוכן שנבדק; לבנות/להתקין רק בהתאם למצב בדיקות מוכח,
  לשמור אפליקציה והגדרות קודמות, ולא להחליף בזמן פעולה פעילה.

**תנאי השלמה:** דוח המשתמש מוכיח בדיקה → בחירה → עדכון במספר ספקים → סיכום →
פתיחה מחדש ועדכון אצל הספק הנכון. לשמר החרגות, היסטוריה וזהות התראות.
הסקריפט הנוכחי של IWYU אינו בדיקת קבלה כזו; אין למסור את פקודתו כהשלמת היעד.

## סדר העבודה הבא

1. להגדיר מודל הצעה ובחירה ובעלות משותף (T04/T03), תוך שימוש בקוד שכבר קיים.
2. להשלים הפעלה ושחזור במנוע לפני הסרת מקור אמיתית (T03).
3. לחבר את הבחירה והמעבר לכפתורי עדכן נבחרים/הכול (T07), ואז להשלים ספק Pixi (T04).
4. להכין את הבדיקות והסקריפט יחד עם המימוש; הסוכן מבצע אימות ממוקד בלבד.
5. למסור למשתמש פקודה אחת, לקרוא את הדוח ולטפל בכשלים; לסגור משימות לפי הראיות.
6. לקדם חסמים T06 לפי צורך. T05 בהמשך. לא להחליף מטרה בפיילוט נוסף.

## ראיות והמשך בשיחה אחרת

- [דוח T01/T02](T01_T02_VALIDATION.md): בדיקות 1.3.2 ותוצאות 239 התקנות
  ו־28 מועמדות מהסריקה שתועדה. אלה אינם נתוני מלאי חיים.
- [דוח T03](T03_VALIDATION.md): מה נוסף ב־1.3.3, מה טרם הוכח ומגבלות הסקריפט.
- [מלאי 19.10.2026](audit-2026-10-19/README.md) ודוחות binary-preflight,
  macports-binary-coverage: תמונות מצב היסטוריות, לא החלטות ביצוע נוכחיות.
- נתוני האפליקציה: `~/Library/Application Support/Homebrew Update Manager/`.
- גיבויים ודוחות: `~/.local/share/brew-update-manager/recovery/`.

בשיחה הבאה לקרוא PRODUCT_GOAL.md, מסמך זה ו־MAC_MANAGER.md, הוראות AGENTS
ומצב Git. לשמר שינויים קיימים; לא להפעיל חבילות ישנות מעל הקבצים הנוכחיים.
לקרוא את דוח המשתמש כשישנו ולהמשיך מהפער המוכח, ללא בדיקות מלאות חוזרות.
אין לבצע מעבר אמיתי בזמן פיתוח. מסירה מקומית בלבד, בלי push/release.

עדכון התיעוד הנוכחי מופק בפקודת `direction-docs-apply` של הגנרטור, עם גיבוי
ומניפסט נפרדים תחת `recovery/documentation-direction-2026-10-20/`.
הוא אינו משנה מניפסטי bundles 001/002/003. בדיקות ישנות שדורשות hash התיעוד
המקורי עשויות לעצור בצדק; יש להתאים חבילת מימוש חדשה, לא לעקוף את ההגנה.
''',
    'docs/MAC_MANAGER.md': '''# Mac Update Manager — implementation and product direction

Updated 2026-10-20. Source version: 1.3.3. This documentation-only review did not
query a live service or rerun product tests. The installed build and package state
must not be inferred from package.json alone.

Read [the product goal](PRODUCT_GOAL.md) for the user's current requirements and
[the task record](MAC_MANAGER_TASKS.md) for status and acceptance criteria. Those
decisions replace earlier designs requiring a separate approval for each migration
or full test runs by the coding agent before handing the user a test script.

## Required user flow — not yet complete

Start Check discovers updates and compares supported providers for native Intel
macOS. Select the newest suitable upstream version with a ready binary runtime
closure. Retain the existing owner on an otherwise equal route. Homebrew, MacPorts
and Pixi participate as their execution routes are implemented and verified.

Show installed/target versions, current/executing provider and the reason. A source
provider change is visible before the user clicks Update selected/all. That click
authorizes the displayed actions; there is no separate per-package migration
wizard. macOS may still require administrator credentials. Revalidate before any
change; a materially changed proposal returns for updated selection.

After successful migration, persist provider ownership and discover future versions
from that provider even without the Homebrew copy. Unknown metadata is unverified,
not absent. Unsupported migration blocks the affected package with a reason.
Never silently build from source, downgrade, or discard exclusions and user data.

## Existing inventory and update paths

Homebrew formulae/casks, active MacPorts ports and existing isolated tools share
an inventory. `kind=port` identifies MacPorts in the selection API; legacy
formula/cask requests remain Homebrew identities. Existing Pixi-managed tools
do not establish general Pixi alternative discovery or migration support.

MacPorts preflight resolves runtime archives for the current OS/architecture and
checks archive/signature availability. Installation uses MacPorts' binary mode
and signature verification. Availability alone does not prove a working migration.
Catalog refresh and cache handling exist; provider freshness and error behavior
still need regression coverage when integrated into the common selection policy.

Combined scans report provider stages and preserve partial errors. Version 1.3.2
introduced final progress in operation responses, retained terminal results,
protection against old responses and unverified status after communication loss.
Inventory evidence separates manager versions, active keg, receipt, PATH and
upstream/revision. Only explicitly configured commands, initially LLVM clang,
are probed. Evidence conflicts block affected decisions.

T02 classifies candidate roles using receipts, reverse dependencies, files,
interfaces, services and bounded linkage checks. Private projects are not scanned.
Evidence is stored in `migration-evidence` under the application data directory.
A standalone classification does not itself authorize execution.

## Existing migration APIs — internal/legacy path

- `POST /api/migrations/plan {name}` creates a proposal.
- `POST /api/migrations/approve {id}` executes a supported proposal.
- `POST /api/migrations/recovery-plan {transaction_id, action}` creates a recovery
  proposal; the current engine does not execute resume/rollback through approve.
- `POST /api/macports/sync` requests catalog refresh with macOS authorization.

The old terminal-notifier implementation remains separate. The 1.3.3 additions
delegate other names to `migration_engine.py`; `migration_profiles.py` defines
terminal-notifier and include-what-you-use profiles. This is partial groundwork,
not a completed common transaction engine. Current update-all does not perform
general cross-provider migrations. See [the T03 review](T03_VALIDATION.md).

The current engine writes plans and transactions under `DATA_DIR/migrations/`
and a route record under `TOOLS_ROOT/migrations/routes.json`. A written route
does not prove shims were activated or future provider upgrades are integrated.
Do not put MacPorts ownership into the legacy installed.json tool registry and
thereby manufacture a duplicate installed tool.

The existing notification helper, permission identity, app/data/profile identity,
launch agent, exclusions (including ngrok), settings and history must remain stable.

## Validation and delivery

The coding agent implements code/tests and performs only focused lightweight
checks appropriate to the edit. The user runs the full suite and acceptance
script; inspect its saved report and avoid rerunning unchanged suites needlessly.
Full unrun checks must be reported as pending. Isolated checks must not mutate
real packages; real acceptance shows the actions and asks for one overall approval.

`tools/verify-migration.py` currently has `--checks` and an IWYU-only acceptance
mode. It is not yet the complete script for the required product flow. Its
limitations and the distinction between historical runs and future validation
are recorded in T03_VALIDATION.md. Do not recommend a real migration until its
preflight, activation, rollback and acceptance gaps are resolved.

[The 1.3.2 validation report](T01_T02_VALIDATION.md) preserves historical evidence;
it does not certify later edits. Local delivery only; no push or release is implied.

## Generator and recovery

Product, tests and documentation are generated through
`tools/implement-mac-manager.py`; edit that script with apply_patch and run terminal
commands interactively. Preserve dirty work. Never replay legacy apply or
document-tasks over a newer bundle, reset README, or overwrite recovery manifests
to silence a precondition failure.

The 2026-10-20 direction update uses `direction-docs-check`, `direction-docs-apply`
and `direction-docs-restore`. These touch documentation only, with a separate
backup and manifest under `recovery/documentation-direction-2026-10-20/`.
Restore refuses subsequent independent edits. Existing bundles keep their old
manifests; their hash checks may correctly reject these newer documentation
files. Adapt the next implementation/verification bundle to the current baseline.
''',
    'docs/T03_VALIDATION.md': '''# T03 — מצב המימוש, ראיות ופערים

עודכן: 20.10.2026 בעקבות הבהרת המשתמש וקריאת הקוד. גרסת המקור: 1.3.3.
**סטטוס: מומש חלקית; פתוח. אין דוח קבלה למעבר IWYU ברשומת המסירה הקודמת.**
לא בוצע מעבר אמיתי במסגרת הפיתוח לפי הרשומה הקודמת. בעדכון התיעוד הזה
לא הורצו בדיקות מוצר ולא נבדק מצב חבילות חי.

## מה קיים

- נוספו `backend/migration_engine.py` ו־`backend/migration_profiles.py`.
- קיימים מסלול תוכנית ואישור, תוכניות עם schema ופקיעת תוקף, גיבוי קג,
  התקנת MacPorts, בדיקות שימוש בסיסיות, רישום ניתוב ויומן עסקה.
- מוגדרים שני פרופילים: terminal-notifier ו־include-what-you-use.
  בפועל terminal-notifier ממשיך למסלול הישן ב־mac_manager.py.
- נוסף endpoint לתוכנית recovery וסקריפט `tools/verify-migration.py`.
- במסירה הקודמת דווח שהורצו בדיקות מבודדות ונארזה/הותקנה 1.3.3.
  דיווח זה אינו הוכחת עמידה בכל סעיפי התוכנית או בדיקת קבלה אמיתית.

## פערים שנראים בקוד — לא תוצאות הרצת כשל

- `migration_engine.approve` דוחה פעולות recovery; יצירת recovery-plan
  אינה המשך או rollback. חריגה מסומנת needs_attention ללא פיצוי מלא.
- שלב activate כותב routes.json אך אינו מפרסם את פקודות הפרופיל דרך
  ניתוב/shims מנוהלים. postcheck חוזר לנתיבי היעד המפורשים, ולכן אינו
  מוכיח שהפקודות הרגילות נפתרות ליעד אחרי הסרת Homebrew.
- הגיבוי בודק hash של receipt בלבד; טרם הושלם אימות כל קובצי הקג,
  הקישורים, מצב ההפעלה ושחזור שאינו דורס שינוי מאוחר.
- התכנון קורא סיווג שמור מ־state. נדרש איסוף עדכני משותף ל־T02,
  השוואת שינויים מהותיים בלבד, מניעת עסקאות כפולות ונעילה בין תהליכים.
- בדיקות IWYU אינן מממשות את כל חוזה הקבלה: אין הזנת פלט הניתוח
  ל־fix_includes ב־stdin ואין דרישה מלאה ל־diff ולקוד היציאה הצפוי;
  נדרשת גם בדיקת התאמת Clang והפעלת שלוש הפקודות בנתיב הרגיל.
- טרם הושלם חיבור רישום הספק למלאי ולעדכון הבא עם בדיקת שימוש לאחר שדרוג.
- חיווי capabilities או גרסה ב־health אינו הוכחת recovery פעיל או זהות
  קוד שנלכדה בתחילת תהליך; בקוד הקיים hash השירות נקרא מהדיסק בבקשת health.
- אין עדיין זרימה כללית של בחירת ספק ועדכון נבחרים/הכול הכוללת מעבר.

הפערים מחייבים מימוש ואימות לפני הסרת מקור אמיתית. אין להסיק מכך שכל
התרחישים נכשלו בהרצה; זו סקירת קוד ממוקדת, לא בדיקת קבלה חדשה.

## הסקריפט שנמסר ומגבלותיו

`tools/verify-migration.py --checks` מפנה ל־`t03-verify` בגנרטור:
בדיקות Python, תחביר, HTTP, שני תרחישי Electron ו־git diff --check.
זהו המסלול המבודד הקיים; בעדכון התיעוד הזה הוא לא הורץ.

`--acceptance --pilot include-what-you-use` הוא מסלול קבלה ייעודי לפיילוט:
בדיקת health בסיסית, תוכנית דרך API, YES, ביצוע, לכידת state וניסיון
פתיחה מחדש. הוא אינו מריץ את כל הבדיקות או מממש את כל שלבי תוכנית הקבלה,
לרבות התאמת חתימות מלאה, גיבוי מצב בסיס, ממשק בשתי שפות ואימות שימוש מחדש.
לכן אין להציג את הפקודה כפתרון הקבלה המלא ואין להמליץ לבצע מעבר לפני השלמת הפערים.

הסקריפט שומר summary.json ו־summary.md תחת
`~/.local/share/brew-update-manager/recovery/t03-1.3.3/`.
לאחר עדכון תיעוד עצמאי, `t03-verify` עלול לעצור בשל hash ישן של מסמך
ב־bundle 003. זו הגנת שחזור צפויה; אין לאפס קבצים או לשנות מניפסט ישן.
הסקריפט והחבילה הבאים צריכים להכיר בבסיס הנוכחי.

## תנאי הסגירה המעודכנים

לפי [מטרת המוצר](PRODUCT_GOAL.md), העבודה שנותרה משלבת T03, T04 ו־T07:
בחירת ספק אוטומטית, מעבר כחלק מלחיצת העדכון ורישום בעלות מתמשך.
המשתמש יריץ את חבילת הבדיקות המלאה בפקודה אחת; הסוכן יבצע אימות ממוקד
בלבד ויטפל בדוח. אין צורך באישור מעבר נפרד לכל חבילה בממשק הרגיל.

סגירה דורשת הוכחת שימוש, מקור שהוסר במסלול מבוקר עם גיבוי, התנהגות כשל
ושחזור, מלאי ללא כפילות, ועדכון אצל הספק החדש אחרי פתיחה מחדש. שתי חבילות
הפיילוט הן מקרי בדיקה בלבד. אין לסמן את היעד הכללי כהושלם על בסיס פיילוט יחיד.
''',
}

DIRECTION_RECOVERY = Path.home() / '.local/share/brew-update-manager/recovery/documentation-direction-2026-10-20'

def direction_documentation(mode):
    """Apply only reviewed docs; keep old bundle manifests and later edits intact."""
    manifest_path = DIRECTION_RECOVERY / 'manifest.json'
    if not manifest_path.is_file():
        raise RuntimeError('Missing exact pre-edit documentation backup')
    manifest = json.loads(manifest_path.read_text())
    restoring = mode == 'direction-docs-restore'
    if mode not in ('direction-docs-check', 'direction-docs-apply', 'direction-docs-restore'):
        raise ValueError('Unknown documentation mode')
    changes = []
    for rel, content in DIRECTION_DOCUMENTS.items():
        record = manifest['files'][rel]
        path = ROOT / rel
        current = path.read_bytes() if path.exists() else None
        current_hash = digest(current) if current is not None else None
        after = content.encode()
        after_hash = digest(after)
        if record.get('after') not in (None, after_hash):
            raise RuntimeError('Documentation generator changed since application: ' + rel)
        if current_hash not in (record['before'], after_hash):
            raise RuntimeError('Documentation refuses independent edit: ' + rel)
        before_path = DIRECTION_RECOVERY / 'before' / rel
        original = before_path.read_bytes() if record['before'] is not None else None
        if original is not None and digest(original) != record['before']:
            raise RuntimeError('Documentation backup hash mismatch: ' + rel)
        desired = original if restoring else after
        if current != desired:
            changes.append((path, desired))
        record['after'] = after_hash
    if mode == 'direction-docs-check':
        print('Documentation check: ' + str(len(changes)) + ' files would change; no files written.')
        return
    manifest['generator_after_sha256'] = digest(Path(__file__).read_bytes())
    manifest['status'] = 'restored' if restoring else 'applied'
    # Save the intended hashes before writing any generated document.
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    for path, desired in changes:
        if desired is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.direction-docs-')
        with os.fdopen(fd, 'wb') as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    print('Documentation ' + manifest['status'] + ': ' + str(len(changes)) + ' files changed; backup: ' + str(DIRECTION_RECOVERY))

if __name__=='__main__':
    try:
        if len(sys.argv)==2 and sys.argv[1].startswith('direction-docs-'):direction_documentation(sys.argv[1])
        elif len(sys.argv)==2 and sys.argv[1].startswith('t01-'):reliability_main(sys.argv[1])
        elif len(sys.argv)==2 and sys.argv[1].startswith('t03-'):t03_main(sys.argv[1])
        else:main()
    except Exception as error:
        print('STOP: '+str(error),file=sys.stderr);sys.exit(1)
