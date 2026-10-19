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

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['check','apply','verify','pack','install','pilot','restore','selftest','recover-stalled','restore-removed'])
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

if __name__=='__main__':
    try: main()
    except Exception as error:
        print('STOP: '+str(error),file=sys.stderr);sys.exit(1)
