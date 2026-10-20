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
  import subprocess;raise SystemExit(subprocess.call([sys.executable,str(ROOT/'tools/verify-current-code.py')]))
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

CURRENT_CHECKS = r'''#!/usr/bin/env python3
"""Run existing isolated source checks and save reports; never migrate packages."""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
REPORTS = Path.home() / '.local/share/brew-update-manager/recovery/t03-1.3.3'

def syntax():
    files = sorted(p for folder in ('backend', 'tools', 'tests')
                   for p in (ROOT / folder).rglob('*.py') if '__pycache__' not in p.parts)
    for path in files:
        ast.parse(path.read_bytes(), filename=str(path))
    print(f'Python syntax: {len(files)} files OK')

def main():
    out = REPORTS / ('checks-' + str(time.time_ns()))
    out.mkdir(parents=True)
    sources = sorted(p for folder in ('backend', 'tools', 'tests', 'renderer', 'electron')
                     for p in (ROOT / folder).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix in {'.py', '.js', '.cjs', '.html', '.css'})
    sources += [ROOT / 'package.json', ROOT / 'package-lock.json']
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    report = {'mode': 'isolated_code_checks', 'real_migration': False,
              'started_at': time.time(), 'code_sha256': hashes, 'steps': [],
              'scope': 'Existing source suites only; no installed-app or real-migration acceptance'}
    (out / 'code-sha256.json').write_text(json.dumps(hashes, indent=2) + '\n')
    def save():
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        lines = ['# דוח בדיקות קוד מבודדות', '', 'תוצאה: ' + report.get('result', 'running'),
                 '', 'לא בוצעו מעבר, עדכון או הסרה של חבילות.',
                 'הדוח אינו בדיקת קבלה לאפליקציה המותקנת או למעבר ספק אמיתי.', '']
        lines += [f"- {s['name']}: {s['status']} ({s.get('log', '')})" for s in report['steps']]
        if report.get('error'): lines += ['', report['error']]
        (out / 'summary.md').write_text('\n'.join(lines) + '\n')
    save()
    print('דוח ולוגים:', out, flush=True)
    node = shutil.which('node')
    electron = ROOT / 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'
    if not node or not electron.is_file():
        report.update(result='prerequisite_missing', error='Node or local Electron is missing; no dependency installation was attempted')
        save()
        return 2
    js = sorted(p for folder in ('electron', 'renderer', 'tools', 'tests')
                for p in (ROOT / folder).rglob('*') if p.suffix in {'.js', '.cjs'})
    commands = [('python-syntax', [sys.executable, str(Path(__file__).resolve()), '--syntax-only'], 60),
                ('python-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'], 600)]
    commands += [('syntax-' + str(p.relative_to(ROOT)).replace('/', '-'), [node, '--check', str(p)], 30) for p in js]
    commands += [('http', [node, str(ROOT / 'tests/service-http.cjs')], 90),
                 ('progress-ui', [str(electron), str(ROOT / 'tests/progress-ui.cjs')], 120),
                 ('operation-results-ui', [str(electron), str(ROOT / 'tests/operation-results-ui.cjs')], 120),
                 ('whitespace', ['git', 'diff', '--check'], 30)]
    code = 0
    with tempfile.TemporaryDirectory(prefix='mac-manager-checks-') as temporary:
        env = {**os.environ, 'BREW_UPDATE_DATA_DIR': temporary, 'PYTHONDONTWRITEBYTECODE': '1'}
        for name, argv, timeout in commands:
            step = {'name': name, 'argv': argv, 'status': 'running', 'log': name + '.log'}
            report['steps'].append(step)
            save()
            print('בודק:', name, flush=True)
            process = None
            started = time.monotonic()
            try:
                with (out / step['log']).open('wb') as log:
                    process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                    while True:
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0: raise subprocess.TimeoutExpired(argv, timeout)
                        try:
                            result = process.wait(timeout=min(20, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            print(f'עדיין בודק: {name} ({int(time.monotonic() - started)} שניות)', flush=True)
                step.update(returncode=result, status='pass' if result == 0 else 'fail')
                if result != 0: code = 1
            except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
                if process is not None and process.poll() is None:
                    # Only the isolated subprocess group created above belongs to us.
                    os.killpg(process.pid, signal.SIGTERM)
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                step.update(status='unverified', error=str(error) or 'Interrupted by user')
                code = 2
            step['elapsed_seconds'] = round(time.monotonic() - started, 2)
            save()
            if code == 2: break
        completed = {s['name'] for s in report['steps']}
        for name, argv, _ in commands:
            if name not in completed:
                report['steps'].append({'name': name, 'argv': argv, 'status': 'not_run'})
    changed = [rel for rel, sha in hashes.items()
               if not (ROOT / rel).is_file() or hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() != sha]
    if changed:
        report['error'] = 'Source files changed during checks: ' + ', '.join(changed)
        code = 2
    report.update(finished_at=time.time(), exit_code=code,
                  result={0: 'pass', 1: 'fail', 2: 'unverified'}[code])
    save()
    print('הבדיקות הסתיימו:', report['result'], '\nדוח:', out, flush=True)
    return code

if __name__ == '__main__':
    if sys.argv[1:] == ['--syntax-only']: syntax()
    elif sys.argv[1:]: raise SystemExit('This runner accepts no arguments')
    else: raise SystemExit(main())
'''

def checks_runner_apply():
    recovery = Path.home() / '.local/share/brew-update-manager/recovery/checks-runner-2026-10-20'
    manifest_path = recovery / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    validation = (recovery / 'before/docs/T03_VALIDATION.md').read_text()
    start = validation.index('`tools/verify-migration.py --checks`')
    end = validation.index('`--acceptance --pilot', start)
    validation = (validation[:start] +
        '`tools/verify-migration.py --checks` מפעיל כעת את `verify-current-code.py` ישירות,\n'
        'בלי בדיקת מניפסט bundle ישן. הוא מריץ את בדיקות הקוד הקיימות: תחביר,\n'
        'Python, HTTP ושני תרחישי Electron מבודדים, ושומר דוח ולוג נפרד לכל שלב.\n'
        'לאחר העדכון הזה הבדיקות המלאות ממתינות להרצת המשתמש. זו אינה קבלה למעבר אמיתי.\n\n' + validation[end:])
    manager = (recovery / 'before/docs/MAC_MANAGER.md').read_text()
    manager = manager.replace('`tools/verify-migration.py` currently has `--checks` and an IWYU-only acceptance\nmode.',
        '`tools/verify-migration.py --checks` now calls `verify-current-code.py` directly,\n'
        'independent of old bundle manifests, and saves per-step logs and a summary.\n'
        'The full suite awaits the user run. The helper also has an IWYU-only acceptance mode.')
    outputs = {'tools/verify-current-code.py': CURRENT_CHECKS,
               'tools/verify-migration.py': T03_VERIFY,
               'docs/T03_VALIDATION.md': validation, 'docs/MAC_MANAGER.md': manager}
    for rel, text in outputs.items():
        record = manifest['files'][rel]
        current = (ROOT / rel).read_bytes() if (ROOT / rel).exists() else None
        sha = digest(current) if current is not None else None
        if sha not in (record['before'], digest(text.encode())):
            raise RuntimeError('Checks runner refuses later edit: ' + rel)
        record['after'] = digest(text.encode())
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    for rel, text in outputs.items():
        (ROOT / rel).write_text(text)
    print('Generated checks runner, wrapper and documentation; no test suites executed.')

INTEL_IMPLEMENTATION_PLAN = r'''# תוכנית מימוש — עדכון אחיד ובחירת ספק ל־Mac Intel

תאריך: 20.10.2026. מצב: תוכנית לביצוע; שינויי המוצר המפורטים כאן טרם מומשו.
בסיס הקוד: `6158c0a`, גרסה 1.3.3. יעד המסירה המקומית: 1.3.4.
המשימות בתוכנית: T03, T04, T07; שימור T01/T02. אין הרחבה ל־T05 או תיקוני T06.

מסמכי יסוד: [מטרת המוצר](PRODUCT_GOAL.md), [משימות וסטטוס](MAC_MANAGER_TASKS.md),
[מצב המימוש](MAC_MANAGER.md), [פערי T03](T03_VALIDATION.md).
החלטות המוצר האחרונות גוברות על תוכנית T03 הישנה ועל הודעות קומיט היסטוריות.

## 1. התוצאה שהמשתמש יקבל

1. ״התחל בדיקה״ מזהה התקנות ומחפש עדכונים בספקים הנתמכים.
2. ליד כל חבילה מופיעים הגרסה המותקנת, הגרסה שנבחרה, הספק המבצע והנימוק.
   אם מתוכנן מעבר, מוצג למשל ״Homebrew → MacPorts״ בתוך אותה שורת עדכון.
3. המשתמש מסמן חבילות או בוחר הכול ולוחץ עדכון. הלחיצה מאשרת את הפעולות
   המוצגות; אין confirm נוסף ואין אישור מעבר נפרד לכל חבילה.
4. המערכת מבצעת כל פעולה דרך הספק שנבחר, מאמתת, ומציגה תוצאה סופית.
5. לאחר מעבר מוצלח, הספק החדש נשמר. פתיחה מחדש וסריקות עתידיות ממשיכות
   לזהות ולעדכן את ההתקנה אצלו גם לאחר שהעותק הקודם הוסר.

הבחירה מבוססת על הגרסה המתאימה החדשה ביותר מבין מסלולים שניתנים לביצוע
עם חבילות מוכנות וכל התלויות. אין בנייה מקוד מקור, ירידה בגרסה או החלפת
ספק רק כדי לקבל את אותה גרסת תוכנה. הרשאת מנהל מערכת של macOS נשארת כשנחוצה.

## 2. נקודת הבסיס המוכחת וגבולותיה

המשתמש הריץ `tools/verify-migration.py --checks` וקיבל PASS ב־19 שלבים,
כולל 83 בדיקות Python, תחביר, HTTP ושני תרחישי Electron בעברית ובאנגלית.
הדוח נשמר ב־[תיקיית הריצה](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t03-1.3.3/checks-1792496033916688000/summary.json).
חתימות 42 קובצי הקוד התאימו בעת קריאת הדוח. זהו בסיס היסטורי מתאריך זה,
לא אישור לשינויי התוכנית החדשים. לא בוצע מעבר אמיתי בריצה זו.

פערים שנקראו בקוד הנוכחי:

| רכיב | מצב קיים | שינוי נדרש |
| --- | --- | --- |
| `mac_manager._combined` | מלאי וסיווג; רוב החלופות נבדקות אחרי חסם Homebrew | הצעות לכל התקנה רלוונטית, גם כשמסלול Homebrew תקין |
| `Engine.plan/run` | בוחר בעיקר לפי סוג, הגדרות ויעד Homebrew | שימוש בהחלטה שהוצגה בסריקה, עם ספק ויעד מדויקים |
| `update_routes` | מסלולי Pixi ממופים ו־installed.json; activate כותב shims | adapter ל־Pixi ובעלות מוגנת, בלי דריסת קבצים זרים |
| `migration_engine` | שלד תוכנית/התקנה/יומן | הפעלה, שחזור, חזרתיות, בעלות ואימות מלאים |
| `migration_profiles` | שני פרופילים ובדיקת IWYU חלקית | חוזה פרופיל משותף ובדיקות שימוש תקינות |
| `installation_evidence.annotate` | איסוף וסיווג בתוך לולאת סריקה | חילוץ איסוף של מועמדת יחידה לשימוש חוזר |
| `macports_provider.upgrade` | אימות גרסת מלאי לאחר port | גם בדיקת שימוש לפרופיל שהועבר |
| renderer | כפתור סקירת מעבר ו־confirm לעדכון נבחרים | רשימת עדכון אחת ולחיצה אחת |
| `/health` | hash השירות נקרא מהדיסק בבקשה | זהות קוד שנלכדת בתחילת התהליך |
| סקריפט הבדיקות | בדיקות מבודדות עובדות; קבלה ישנה מוגבלת לפיילוט | קבלה אחידה של גרסה ארוזה ושל זרימת העדכון |

אין למחוק את הקוד הקיים ולהתחיל מחדש. יש להשתמש בפונקציות הספקים,
במלאי, באיסוף הראיות ובמנגנון התוצאות של T01 שכבר קיימים.

## 3. היקף מדויק והחלטות שאינן נתונות לבחירה

- יעד קבלה: macOS על חומרת Intel; לזהות גם Rosetta ולדחות מסלול מעבר חדש על ARM.
- מסלולי ביצוע: עדכון אצל ספק נוכחי — Homebrew, MacPorts, Pixi; מעבר חדש
  של כלי עצמאי מ־Homebrew ל־MacPorts או Pixi. מעבר שמקורו MacPorts לספק
  אחר, ספריות, שירותים ומערכות פיתוח משותפות נשארים חסומים עם נימוק מפורש.
- מיפויים ופרופילים הם קבועים בקוד. אין להסיק זהות חבילה מדמיון בשם בלבד.
  אין לקבל פקודת shell או בדיקת שימוש מ־HTTP או מקובץ משתמש.
- המסלולים העצמאיים הקיימים ל־Node, Deno ו־uv ממשיכים לעבוד כפי שהם;
  אין להוסיף ספקי מתקינים רשמיים חדשים במסגרת התוכנית.
- לשמר Pixi קיים וסביבות שהמשתמש מנהל. אין להמיר סביבת משתמש לנכס של המנגנון.
- `terminal-notifier` הוא רגרסיה של מסלול קיים; helper וזהות הרשאת ההתראות נשמרים.
  `include-what-you-use` הוא מקרה הקבלה למעבר הנוסף ל־MacPorts, ללא טיפול ב־LLVM Homebrew.
- אין לבחור פיילוט חלופי אוטומטית אם IWYU חסום. תמיכת Pixi תיבדק מבודד;
  שינוי אמיתי נוסף דרך Pixi יוצג בנפרד לבחירת המשתמש במסגרת רשימת הפעולות הכוללת.
- ngrok נשאר מוחרג בכל ספק ובסגירת תלויות. לשמר כל יתר ההחרגות וההגדרות.
- אין לשנות shell startup files או prefix של Homebrew לצורך ניתוב חדש.
- אין לבצע מעבר, הסרה או עדכון חבילות אמיתי במהלך המימוש בידי הסוכן.
- אין להריץ חבילת בדיקות מלאה בידי הסוכן. אין להריץ אוטומטית pack/install
  שגוררים verify מלא; המשתמש יבצע זאת במסלול הקבלה המתואר בסעיף 14.

## 4. חלוקת אחריות וקבצים

| קובץ | אחריות לאחר השינוי |
| --- | --- |
| חדש: `backend/update_selection.py` | חוזי נתונים, השוואה, בחירה ו־fingerprint מהותי; ללא כתיבת התקנות |
| חדש: `backend/provider_ownership.py` | רישום בעלות, מיזוג למלאי וניתוב פקודות מוגן |
| חדש: `backend/pixi_provider.py` | גילוי גרסאות Pixi, lock, אימות graph, התקנה ומלאי של סביבות מנוהלות |
| חדש: `backend/operation_lock.py` | נעילת קובץ משותפת לפעולות משנות מצב |
| `backend/migration_engine.py` | עסקה, גיבוי, הפעלה, ניקוי, פיצוי ו־recovery |
| `backend/migration_profiles.py` | מיפויים נתמכים, פקודות חובה, version probes ו־smoke probes |
| `backend/installation_evidence.py` | אותו איסוף וסיווג לסריקה ולבדיקה טרם ביצוע |
| `backend/mac_manager.py` | מלאי משולב, discovery והצעות; ללא מימוש עסקה נוסף |
| `backend/update_engine.py` | ביצוע רציף של בחירת חבילות דרך החלטות אחידות |
| `backend/update_routes.py` | תאימות למסלולים הישנים; להעביר ל־adapter החדש בהדרגה |
| `backend/macports_provider.py` | לשמר preflight והתקנה בינארית; לאמת את היעד המדויק ובדיקת שימוש לאחר עדכון |
| `backend/brew-updates-service.py` | API, זהות שירות, נעילה וסיכום תוצאות |
| `electron/main.js`, `electron/preload.js` | העברת מזהי הצעות וסבבי בדיקה, בלי החלטת ספק בצד הלקוח |
| `renderer/app.js`, `mac-manager.js`, `index.html`, `i18n.js` | שורה אחידה, בחירה, ביצוע ותוצאה בשתי שפות |
| `tools/verify-current-code.py`, `tools/verify-migration.py` | בדיקות מלאות וקבלה בידי המשתמש; דוח אחד מפורט |

אין ליצור service נוסף, מסווג נוסף או engine נוסף. פונקציות קיימות עם אותה
אחריות יועברו או ייקראו דרך המתאם; אין להשאיר שתי דרכי ביצוע שונות לאותו עדכון.

## 5. שלב א — חבילת שינוי ושחזור קוד

להוסיף בגנרטור bundle 004 עבור 1.3.4. להתחיל מהקבצים הנוכחיים, כולל תוכנית
זו ותיקון סקריפט הבדיקות. אין לשנות manifest או גיבוי של bundles קודמים.

פקודות החבילה: `unified-check`, `unified-apply`, `unified-restore`,
`unified-pack`, `unified-install`. הבדיקות המלאות נשארות ב־`--checks`.

1. לפני עריכת הגנרטור לשמור עותק שלו, manifest של קובצי היעד ועותקים מדויקים
   תחת `~/.local/share/brew-update-manager/recovery/unified-intel-1.3.4/`.
2. לערוך רק את `tools/implement-mac-manager.py` באמצעות apply_patch ולהפיק ממנו
   קוד, בדיקות ותיעוד. `unified-check` בודק hashes ללא כתיבה.
3. לאמת את כל תנאי הקדם לפני כתיבה. החלה חוזרת זהה לא משנה דבר; עריכה עצמאית
   מאז ההחלה מחייבת עצירה ולא דריסה. שחזור חייב לסרב לעריכה מאוחרת.
4. `unified-pack/install` יקבלו דוח בדיקות תואם לחתימת הקוד והבדיקות, ולא יריצו
   בדיקות מחדש בעצמם. דוח ישן או קובץ חדש שלא נכלל בחתימה אינם תקפים.
5. לשמור README כפי שהוא, בלי reset או ניקוי עבודה קיימת. לציין פקודת שחזור
   והבדל בין שחזור קוד לבין rollback של עסקת חבילה.

**תוצר:** bundle שניתן להחיל ולשחזר במבודד, בלי לגעת בהתקנות אמיתיות.

## 6. שלב ב — חוזי הצעה, החלטה ובעלות

### 6.1 זהויות

`installation_id` מזהה התקנה אמיתית: `homebrew:formula:<name>`,
`homebrew:cask:<name>`, `macports:<port>` או `pixi:<managed-id>`.
`logical_id` הוא מזהה תוכנה מהמיפוי בקוד. שתי התקנות קיימות של אותה תוכנה
אינן הופכות אוטומטית להתקנה אחת ואינן מוסרות רק מפני ששמותיהן חופפים.

הפניה מפורשת של route חדש ל־installation_id של היעד מאפשרת לזהות שהמקור
הוחלף. אין ליצור רשומת tool נוספת עבור port אמיתי כדי לייצג את המעבר.

### 6.2 חוזה Candidate

כל adapter יחזיר JSON שניתן לשמירה, עם השדות הבאים:

```json
{
  "schema_version": 1,
  "logical_id": "<mapped-tool>",
  "source_installation_id": "homebrew:formula:<name>",
  "provider": "macports",
  "provider_names": ["<port>"],
  "upstream_version": "<version>",
  "package_version": "<provider-version>",
  "revision": "<provider-revision-or-null>",
  "action": "migrate",
  "availability": "verified",
  "execution_supported": true,
  "profile_id": "<profile>",
  "profile_version": 1,
  "system": {"os": "macos", "arch": "x86_64", "release": "<detected>"},
  "closure": [],
  "evidence_refs": [],
  "checked_at": "<timestamp>",
  "blockers": []
}
```

`action`: `update` / `migrate` / `none`. `availability`: `verified` /
`unavailable` / `unverified`. רשומת closure כוללת זהות, גרסה, build/revision,
variants, ארכיטקטורה, תלויות, URL או archive ID ו־checksum כשמפורסם.
שדות חסרים אינם מוחלפים בערך שנראה חיובי. חתימת MacPorts זמינה ואימות
חתימה שבוצע הם שני שדות שונים.

### 6.3 החלטה שמוצגת ומאושרת בלחיצה

להוסיף לכל התקנה `update_decision` עם:
`decision_id`, `plan_id`, `logical_id`, `source_installation_id`, `current_provider`,
`target_provider`, `installed_version`, `target_version`, `action`, `status`,
`selected_candidate`, `alternatives`, `reason_codes`, `blockers`, `created_at`,
`expires_at`, `material_fingerprint`.

`status`: `ready` / `current` / `blocked` / `unverified` / `needs_recovery`.
TTL של תוכנית: 15 דקות. הנתונים נשמרים תחת `DATA_DIR/update-plans/<id>.json`.
`GET /api/state` ו־`GET /api/update-plan` מציגים נתונים שמורים ואינם מתחילים
סריקת חלופות מלאה. כאשר פג תוקף, אפשר לקרוא את הנתונים אך אין לבצע אותם.

### 6.4 רישום בעלות

להשתמש ב־`TOOLS_ROOT/migrations/routes.json` עם schema מפורש ו־`routes`:
כל רשומה כוללת `logical_id`, מקור, ספק/זהות יעד, פרופיל וגרסתו, transaction ID,
generation של הניתוב, פקודות בבעלות המנגנון, נתיב evidence ותאריך אימות.
לשמור את שם המקור ל־provenance, לא כבסיס לגרסה עתידית.

לתמוך בקריאת הצורה הישנה ללא schema כנתון legacy; להמיר אטומית רק רשומה
שניתן לאמת את בעלותה במלאי. רשומה לא מאומתת נשמרת ומסומנת, לא נמחקת.
`installed.json` ימשיך לתאר כלים קיימים; אין להוסיף אליו port. סביבות Pixi
מנוהלות קיימות נקראות דרכו עד שהadapter אימת את המלאי האמיתי ואת השיוך.
כתיבות לשני קבצים יהיו מתועדות ביומן בר־התאוששות; אין להניח שהן אטומיות יחד.

## 7. שלב ג — גילוי ובחירת ספק

### 7.1 אלגוריתם מחייב

לממש ב־`update_selection.select(installed, candidates, ownership)`:

1. לסנן החרגות ושגיאות זהות. חבילה מוחרגת אינה מקבלת מועמדת לביצוע.
2. מועמדת חייבת להתאים ל־macOS/Intel, לספק גרסה שניתנת להשוואה, להיות
   בעלת closure בינארית מלאה ולהיות נתמכת לביצוע הפעולה המבוקשת.
3. מעבר מחייב סיווג עצמאי עדכני ללא shared/service roles, פערים או תלויים.
   עדכון רגיל באותו ספק אינו מקבל חסם מעבר רק משום שהחבילה היא ספרייה.
4. לפסול downgrade. מעבר לגרסת upstream זהה אינו עדכון; revision חדש אצל
   אותו ספק יכול להיות עדכון אם הספק מדווח עליו ככזה.
5. לבחור את הגרסה הגבוהה מבין המועמדות הכשירות. בשוויון לשמר את הספק
   הנוכחי; אם אינו מועמד כשיר, סדר שוויון קבוע: Homebrew, MacPorts, Pixi.
6. חלופה חדשה יותר שאינה נתמכת אינה מעלימה עדכון כשיר אצל הספק הנוכחי:
   מציגים אותה ב־alternatives עם חסם, והבחירה נשארת המסלול שניתן לבצע.
7. ספק שכשל אינו נחשב כמי שאין לו גרסה חדשה יותר. אם יש מסלול כשיר,
   מציגים אותו עם אזהרה שההשוואה חלקית; אין לטעון ״החדש ביותר״ באופן מוחלט.
   אם אין מסלול כשיר, התוצאה unverified כשחסר מידע, blocked כשיש חסם ידוע,
   ו־current רק כשכל הבדיקות הנדרשות הושלמו ואינן מציעות עדכון.

להפריד שלוש פעולות: `normalize_upstream`, `compare_upstream`,
`compare_same_provider_package`. אין להשתמש ב־version_key הנוכחית להשוואה
כללית, משום שהיא מאבדת משמעות של suffix ו־revision.
בשלב זה השוואה בין ספקים תומכת בגרסאות יציבות מספריות, עם v אופציונלי
ורכיבי נקודות. להשלים אפסים להשוואת 1.2 ו־1.2.0. suffix לא מוכר או prerelease
מחזירים `comparison_unknown`; אין להסיר אותם כדי להמציא סדר. הספקים ממשיכים
להחליט עדכניות אריזה בתוך הספק באמצעות המטא־נתונים שלהם.

### 7.2 איסוף בתוך הסריקה

- להרחיב `_combined` כך שחלופות נאספות לכל התקנה ממופה רלוונטית, גם כאשר
  Homebrew מספק bottle. אחרת לא ניתן למצוא חלופה בינארית חדשה יותר.
- לא לסנן מועמדות רק לפי `Homebrew outdated`. `outdated` בתוצאה המוצגת
  נגזר מ־update_decision מתאים, גם אם העדכון זוהה אצל ספק אחר.
- שגיאת Brew לא תעצור גילוי יעד עבור port או סביבת Pixi שכבר מנוהלים.
- לחלץ `collect_candidate(s, installation, context, force_refresh)` מתוך
  `installation_evidence.annotate`; הפונקציה תחזיר classification ו־refs.
  הסריקה וה־preflight משתמשים בה; אין מסווג נוסף במנוע.
- לצבור נתוני receipt ותלויים לכל הסריקה, לבדוק קבצים רק למועמדת הרלוונטית,
  ולחלוק תוצאות באותה ריצה. לפני שינוי התקנה לרענן נתונים מהותיים, לא להסתפק ב־cache.
- timeout לכל command/network probe, ביטול והתייחסות לכשל מקומי כתוצאה חלקית.
  לשמר מדיניות catalog_refresh הקיימת; לרענן בעת צורך, לא לשאול את המשתמש
  אם צריך רענון קטלוג. כשל הזדהות יוצג כחסם ממוקד.

### 7.3 Homebrew ו־MacPorts

Homebrew: להשתמש ב־catalog, verified_dependencies ו־bottle_blockers הקיימים;
לקשור את היעד ל־bottle tag/hash ולמטא־נתונים שנבדקו. קיומו של `--force-bottle`
אינו חוזה מלא האוסר בניית תלות: להשלים preflight לכל closure ולבדוק שהקטלוג
לא השתנה לפני הביצוע. אין להוסיף fallback של build-from-source.

MacPorts: להשתמש ב־preflight וב־privileged הקיימים, עם מצב `-b`. לשמר את
גרף הגרסאות, revisions ו־variants המדויק בתוכנית. לא לבצע מעבר שמחייב לשנות
גרסה/variants של port שכבר מותקן. שינוי קטלוג בין הבדיקות מחייב החלטה חדשה.
אחרי התקנה להשוות לגרף שאושר; יציאה 0 בלי היעד שנבחר אינה הצלחה.

### 7.4 Pixi — adapter נפרד

1. מקור המיפויים הראשוני הוא `PIXI_TOOLS` הקיים. מיפוי אינו פרופיל מעבר:
   רק פרופיל עם פקודות חובה ובדיקת שימוש יוכל לקבל execution_supported.
2. לגלות את Pixi runtime מהגדרות/runtime קיימים, לקרוא גרסה ויכולות; לא להתקין
   runtime ולא לשנות סביבות בזמן סריקה. runtime חסר הוא חסם Pixi בלבד.
3. לבדוק את הפלט של ה־CLI המותקן מול parser מוגדר, עם fixture מייצג.
   לפעול מתוך scratch בבעלות המנגנון; `--no-config` לבדו אינו מבטל config מקומי.
4. discovery: חיפוש שם ממופה מדויק ב־conda-forge עבור osx-64, בפלט JSON
   ובכל הגרסאות. לא לקחת רק חמש גרסאות ברירת מחדל ולא לרשת channels מפרויקט.
5. לנסות גרסאות יציבות בסדר יורד עד שמתקבל lock תקין למערכת המקומית.
   כשל solver עבור הגרסה הגבוהה אינו הוכחה שאין גרסה נמוכה יותר מתאימה.
   כל נסיונות ה־solve כפופים לביטול ותקציב זמן מצטבר של 180 שניות לכלי;
   timeout לפני השלמת ההשוואה מסומן במפורש כהשוואה חלקית.
6. manifest חדש מקבע את גרסת השורש שנבחרה. אין constraint מסוג >= שיכול
   להביא גרסה אחרת בביצוע. לא לכלול PyPI, editable/local dependencies או build sources.
7. לשמור lock, manifest וייצוא explicit בפורמט שה־CLI המקומי תומך בו.
   closure תכלול osx-64/noarch בלבד, ערוצי HTTPS מאושרים ו־hashes זמינים.
   noarch מותר כתלות, לא כהוכחה שכלי native מתאים ל־Intel.
8. לאמת דרישות מערכת/virtual packages דרך ה־solver ללא overrides שמעמידים
   פנים שגרסת macOS או הארכיטקטורה שונה. לאמת נגישות ארטיפקטים לפני ready.
9. התקנה בסביבת generation חדשה מתוך ה־lock המאושר במצב `--locked`,
   ללא solve מחדש. אם ה־CLI אינו תומך בחוזה זה, להחזיר חסם יכולת מפורש.
10. לקרוא את גרסת השורש והחבילות מ־conda-meta ולהשוות ל־lock; לאמת פקודות
    ובדיקת שימוש לפני החלפת ניתוב. לא לשנות או לנקות את סביבת הדור הקודם.
11. עדכון Pixi עתידי מגלה יעד ב־Pixi, יוצר lock ודור חדש ומחליף מצביע רק אחרי
    אימות. הוא אינו מבקש את latest_version של Homebrew כדי לבחור יעד.

## 8. שלב ד — תוכנית בלתי משתנה, נעילה וחזרתיות

### 8.1 Fingerprint מהותי

לחשב SHA-256 על JSON קנוני של: זהויות מקור ויעד; גרסאות ו־receipt hash;
קג פעיל; graph/lock/variants/artifact hashes; מערכת; פרופיל וגרסתו; החלטת סיווג,
תלויים ופערים; החרגות רלוונטיות; בעלות קבצי הפעלה ומצב ניתוב קודם.
לא לכלול זמני checked_at, נתיב קובץ ראיות חדש או מזהה תוכנית אקראי.

לפני שינוי ראשון לרענן ולהשוות. שינוי מהותי מחזיר 409 עם
`error=plan_changed`, הצעה חדשה וסיבה. אין לבצע אותה אוטומטית תחת הלחיצה
הקודמת. תצוגה מעודכנת מאפשרת לחיצה חדשה אחת; אין אשף אישורים לכל חבילה.

לפני תחילת batch יש לאמת את כל ההחלטות שנבחרו. חבילה שחסומה כבר בתצוגה
מדולגת עם נימוק; שינוי מהותי בהצעה מוכנה מחייב חזרה לרשימה לפני שינוי batch.
לאמת שוב כל item לפני ביצועו, מאחר שפעולה קודמת עשויה לשנות תלויות משותפות.

### 8.2 נעילה בין תהליכים

לשלב `fcntl.flock(LOCK_EX | LOCK_NB)` בקובץ תחת TOOLS_ROOT, לפני OP_LOCK
ובאותו סדר בכל הכניסות. הנעילה משותפת גם ל־DATA_DIR חלופי כדי למנוע שינוי
אותן התקנות משני backends. אין למחוק קובץ נעילה כדי להשתלט על תהליך חי.
לשמור בעלים/PID/operation ID לצורכי תצוגה, בלי לראות בקיום הקובץ נעילה פעילה.

לעטוף את כל מסלולי השינוי — update-one/selected/all, אישור migration,
recovery ו־sync. אין לקנן נעילה חדשה כאשר batch כבר מחזיק אותה: לקרוא
למתודה פנימית עם context בעלות מפורש. לא לעקוף נעילה במשתנה גלובלי.
בדיקה מקבילה קיימת בזמן עדכון רגיל נשמרת; מרגע התחלת batch עם migration
אין להתחיל בדיקה מקבילה חדשה, ובדיקה קיימת חייבת להסתיים לפני mutation.

### 8.3 זהות בקשה ועסקה

כל לחיצה יוצרת `request_id` פעם אחת בצד הממשק. backend שומר batch journal
והשיוך ל־plan לפני שינוי. בקשה חוזרת עם אותו ID ותוכן תחזיר מצב/תוצאה קיימים;
אותו ID עם תוכן אחר יחזיר 409. אין ליצור transaction חדש לאחר timeout.
עסקה לא פתורה חוסמת פעולה מתנגשת בלבד ודורשת recovery מפורש; אין auto-resume בהפעלה מחדש.

## 9. שלב ה — מנוע מעבר והפעלה בטוחים

### 9.1 מצבי עסקה

`preflight → backup → install → verify → activate → cleanup → postcheck → completed`.
כל שינוי שלב נכתב לפני ואחרי ב־journal.json עם סטטוס started/finished,
תוצאות ונתיבי גיבוי. כתיבה אטומית עם קובץ זמני ייחודי, flush, fsync לקובץ,
os.replace ו־fsync לתיקייה. להשתמש באותו helper לנתיבי תוכנית/בעלות רגישים;
לתקן את atomic_write_json הקיים שאינו יוצר parent שרירותי ואינו מבצע fsync.

מצבים סופיים: completed, canceled, rolled_back, needs_attention.
הפסקה בין שני רישומים אינה הוכחה שהשלב לא בוצע: recovery בודק מצב אמיתי.

### 9.2 גיבוי

- לאפשר במעבר החדש קג מקור יחיד בלבד, receipt תקין וקישור שניתן לשחזר.
- להעתיק קג עם הרשאות וקישורים, לתעד את כל הקבצים: relative path, סוג,
  mode, SHA-256 של קובץ רגיל או יעד קישור. לבדוק שלמות ההעתק מול manifest.
- לתעד opt/link state וקישורים ש־brew link מנהל, וכן מצביעי ניתוב ו־shims קיימים.
  אין לקרוא קישורי symlink כקובץ רגיל ולגבות בטעות עץ חיצוני.
- לבצע בדיקת שימוש מקור לפני מעבר ולשמור תוצאה. מקור פגום או מצב שלא ניתן
  לאמת חסומים; rollback אינו מבטיח לתקן בעיה שהייתה במקור קודם.
- גיבוי פגום/העתקה חלקית עוצרים לפני התקנת יעד והסרת מקור.

### 9.3 התקנה ואימות

ה־engine קורא ל־adapter של היעד, בלי ליצור מתקין חלופי.
הוא משתמש ברכיב MacPorts קיים רק אם הגרסה/variants תואמים בדיוק.
רכיבי MacPorts שנוספו מתועדים ונשארים אחרי כשל; אין הסרת תלויות אוטומטית.
Pixi משתמש בסביבה חדשה וב־lock שנבדק. בלי החלפת ניתוב עד שכל פקודות החובה,
גרסה ובדיקות שימוש הצליחו בנתיבי היעד המפורשים.

### 9.4 ניתוב פקודות

להשתמש ב־`TOOLS_ROOT/bin` ו־`TOOLS_ROOT/migrations/active/<logical-id>`.
כל דור הוא תיקייה שמכילה wrappers לכל פקודות הפרופיל; המצביע active מוחלף
אטומית. shim ב־bin מפנה ל־active, כך שהחלפת המצביע מעבירה יחד את כל הפקודות.

לפני כתיבה לבדוק lstat וגם קישור שבור. לדרוס רק קובץ שמניפסט בעלות קיים
מוכיח שהוא של המנגנון ושה־hash שלו לא השתנה. אין להסתפק ב״זה symlink״.
אם לא כל shims קיימים, להתקינם כש־active עדיין מפנה למקור שנבדק, ואז לבצע
את החלפת המצביע היחידה; במקרה כשל לשחזר רק קבצים שהמנגנון עצמו כתב.
הבדיקה הזו תשתמש גם ב־update_routes.activate הישן כדי למנוע מסלול עוקף בעלות.

לא לשנות PATH בקובצי shell. להשוות resolution של כל פקודה ב־login shell
רגיל לנתיב המנוהל, וגם בהרצה מפורשת. PATH שלא נותן קדימות ל־TOOLS_ROOT/bin
חוסם מעבר ומציג הוראה ממוקדת; לא להסיר מקור בתקווה שה־shell יבחר יעד אחר.
לא להסתיר את הבעיה באמצעות prepend זמני ב־postcheck.

ל־IWYU הפקודה הראשית מפנה למעטפת MacPorts, לא לבינארי Clang פנימי.
סקריפטי Python משתמשים ב־Python 3.14 שזוהה ונבדק; אין לקבע נתיב משתמש.
`iwyu_tool.py` מקבל IWYU_BINARY המצביע למעטפת המאומתת. argv מופרדים,
וכל קובץ shell שנוצר משתמש ב־shlex.quote, בלי להריץ טקסט HTTP.

### 9.5 cleanup ו־commit של הבעלות

לרענן תלויים, receipt, קג, החרגות ופקודות לפני הסרת מקור. שינוי חוסם הסרה;
לשחזר ניתוב אם ניתן, ולרשום needs_attention כאשר נדרשת החלטה חדשה.
הסרה רק לחבילת המקור המדויקת, בלי force, autoremove, cleanup כללי או תלויות,
עם HOMEBREW_NO_AUTO_UPDATE, HOMEBREW_NO_AUTOREMOVE,
HOMEBREW_NO_INSTALL_CLEANUP בערך 1.

לאחר הסרה לבדוק מלאי ו־smoke דרך הפקודות הרגילות. רק אחרי postcheck לכתוב
בעלות committed ולסיים completed. מידע tentative נשאר ביומן ולא הופך לבעלות
שהסריקה סומכת עליה. כשל כתיבת בעלות אחרי הסרה הוא מצב התאוששות, לא הצלחה.
כשל רענון תצוגה אחרי transaction שהושלם הוא אזהרה נפרדת, לא ״התקנה נכשלה״.

## 10. שלב ו — כשל, ביטול, המשך ושחזור

| נקודת כשל/ביטול | התנהגות נדרשת |
| --- | --- |
| לפני install | מקור וניתוב כפי שהיו; canceled/rolled_back לאחר אימות |
| install או verify | אין הפעלה; המקור נשמר; לתעד יעדים שנוספו |
| אחרי activate ולפני הסרה | להחזיר מצביע וקבצים למצב הקודם, בכפוף לבעלות |
| הסרה חלקית או אחרי הסרה | לבדוק מה נשאר; לשחזר מקור מהגיבוי רק כשאין התנגשות |
| יעד/קג/shim ששונו עצמאית | לא לדרוס; לשמר עותק תקין ולסיים needs_attention |
| תהליך שנפסק | לזהות יומן לא סופי, לבדוק כל postcondition ולהציג recovery |

לפצות ב־context נפרד שאינו נקטע בגלל דגל הביטול המקורי. אין לשחרר נעילה
ולהודיע canceled בזמן שעדיין רץ command משנה התקנות. פיצוי מקבל timeouts
משלה ולא מריץ command ללא גבול. timeout מותיר needs_attention אם אין הוכחה.

`recovery_plan(transaction_id, action)` ו־approve חייבים לממש resume/rollback,
לא רק לשמור תוכנית. תוכנית recovery מכילה מצב עדכני, פעולות ושינויים צפויים.
resume מדלג על שלב רק לאחר שהוכיח את postcondition שלו; אינו מסיר מקור שוב.
rollback מחזיר קג רק לנתיב המקורי, בודק היעדר בעלים אחר ו־hashes, ומשתמש
ב־brew link בלי overwrite/force כשצריך. אימות מול מצב הבסיס נשמר בדוח.
שחזור אינו מסיר רכיבי MacPorts חדשים או סביבות עצמאיות של המשתמש.

בממשק רגיל אין מסכי אישור מעבר. רק במקרה של תקלה לא פתורה מוצגים
״סקירת המשך״/״סקירת שחזור״ עם הפעולה העדכנית ואישור כולל להליך ההתאוששות.

## 11. שלב ז — פרופילים ובדיקות שימוש

להרחיב Profile עם: version, שמות ספקים, כל פקודות החובה, version argv/parser,
smoke function, נתיבים מוגדרים ושיטת הפעלה. מימושים נבחרים מקוד, לא מתוכן חיצוני.
פרופילים קטנים ו־adapters משותפים מאפשרים להוסיף תמיכה בלי להעתיק עסקה.
פרופיל חסר הוא execution_unsupported; אין להתקין חבילה רק לפי סיווג עצמאי.

להעביר את terminal-notifier לממשק המשותף דרך adapter של התנהגותו הקיימת,
בלי להעביר אותו מחדש או להחליף את notification helper. לשמר קריאת journal ישן
כראיה היסטורית; לא להריץ תוכנית legacy ללא fingerprint חדש.

להגדיר פרופילי Pixi למיפויים הקיימים רק כאשר יש פקודות חובה ו־smoke ידוע בקוד.
כלי שגם מייצא סביבת פיתוח/ספרייה ייחסם למעבר לפי T02 גם אם הוא בטבלת PIXI_TOOLS.
עדכוני כלי Pixi שכבר מנוהל ממשיכים בפרופיל הקיים, ללא הסרת סביבת מקור ישנה.

### IWYU — תיקון בדיקת הקבלה הקיימת

כל הקבצים בתיקייה זמנית בלבד. ליצור needed.h, unused.h, probe.cc עם vector
ו־compile_commands.json; להשתמש ב־arguments array, לא command string לא מצוטט.
לבחור נתיב עם רווחים גם ב־fixture כדי לבדוק quoting. לכתוב newlines אמיתיים.

1. SDK באמצעות xcrun --show-sdk-path; לבדוק תקינות ומערכת, לא להשתמש בערך ריק.
2. מעטפת IWYU עם std=c++17, isysroot מפורש ו־`-Xiwyu --error=17`.
   לדרוש exit 17 ופסקת המלצה שמסירה include של unused.h; לדחות שגיאות
   parsing/compile/dyld ונתיבים חסרים גם אם שם הקובץ מופיע בפלט.
3. iwyu_tool.py עם Python מאומת, IWYU_BINARY מפורש ופרמטרי IWYU שמחזירים
   אותו חוזה exit/ניתוח. לא לקבל exit 0 כתחליף ל־17 כשנתבקשה הפרת includes.
4. להזין את פלט הניתוח ב־stdin ל־fix_includes.py --dry_run עם סינון לקובץ הבדיקה.
   לדרוש exit 1 ו־diff שמסיר include מיותר; hash המקור נשאר ללא שינוי.
5. להתאים גרסת IWYU וגרסת Clang מתוך metadata וגרף MacPorts, בלי קיבוע גרסה.
6. לתעד stdout/stderr/returncode לכל command. timeout גרסה 30 שניות ושימוש
   90 שניות לכל בדיקה. לחזור על הבדיקות אחרי הפעלה ובקבלה לאחר restart.

אם run_cmd אינו תומך stdin, להוסיף input_data/bytes מפורש ולהעביר ל־Popen
בלי shell, תוך שימור ניקוז stdout/stderr וביטול. להוסיף בדיקה ממוקדת לכך.

## 12. שלב ח — מלאי, API וממשק

### 12.1 מלאי ועדכונים עתידיים

למזג בעלות רק על גבי התקנת יעד שנמצאת בפועל. port נשאר kind=port וסביבת
Pixi היא kind=tool עם provider=pixi. מקור שחסר אחרי מעבר committed אינו
מוחזר כ־formula מדומה. עותק Homebrew אחר שקיים באמת נשאר רשומה נפרדת.

תקלה בקריאת routes אינה מוחקת את המלאי; מסמנת אימות בעלות חסר וחוסמת
פעולה שתלויה בו. `macports_provider.upgrade` ו־Pixi upgrade קוראים smoke
של פרופיל הבעלות. כשל smoke מחזיר עדכון שנכשל גם אם המתקין החזיר 0;
אין לשמור גרסה מוצלחת מלאכותית. גרסה חיה נשמרת לצד כשל התפקוד.

### 12.2 API ותאימות

לשמר שמות endpoints ו־IPC קיימים ולהוסיף שדות אופציונליים:

```text
POST /api/check                         {}
GET  /api/state
GET  /api/update-plan
POST /api/update-selected               {request_id, plan_id, decisions:[{decision_id, material_fingerprint}]}
POST /api/update-all                    {request_id, plan_id, decisions:[...]}
POST /api/update-one                    {request_id, plan_id, decision_id, material_fingerprint}
POST /api/migrations/plan               {name}
POST /api/migrations/approve            {id, request_id}
POST /api/migrations/recovery-plan      {transaction_id, action:"resume"|"rollback"}
```

update-all שולח בדיוק את כל ההחלטות המוכנות שהוצגו, לא בחירה אחרת שהופיעה
בזמן ההמתנה. בקשה ישנה עם name/kind נשארת תקפה רק לעדכון באותו ספק;
היא לעולם אינה מאשרת מעבר שלא הוצג. לבקשה ללא plan שמחייבת מעבר להחזיר
409 `review_required`. פרמטרים זרים או זהות עמומה אינם נבחרים לפי השם הראשון.
כל endpoint עובר לאותו executor; אין לבצע לולאת HTTP של migrate ואז update.

400 לקלט לא תקין; 409 להתנגשות, תוכנית ישנה/משתנה או recovery נדרש;
500 לכשל בלתי צפוי. ביטול מחזיר תוצאה מפורשת ויומן. לשמר בדיקות Origin/JSON
ולהחילן על כל כניסה חדשה. אין לקבל נתיבי גיבוי, יעד executable או shell command מלקוח.
תגובות סופיות כוללות progress שנלכד לפני שחרור הנעילה.

לאסוף fingerprint של כל קובצי backend הרלוונטיים ב־startup פעם אחת ולשמור
בזיכרון. `/health` מחזיר version, PID, process_started_at, DATA_DIR, capabilities,
backend_code_sha256 ו־build ID מתוך החבילה. capabilities מציינות רק יכולת שבאמת
ממומשת. hash מהדיסק בזמן בקשה אינו משקף בהכרח את הקוד שטעון בתהליך.

### 12.3 ממשק

- לעדכן selectedPackagesFromSnapshot, pruneSelectedPackages וסינון שורות
  לפי installation_id/decision, תוך תאימות לרשומות היסטוריות name/kind.
- לעדכן preload/main להעברת payload של ההחלטות; backend הוא מקור האמת לבחירה.
- ״עדכן הכול״ בוחר את כל ready, ״עדכן נבחרים״ את המסומנים. חסומים אינם נבחרים
  אוטומטית. המונים יבדילו עדכון זמין, ניתן לביצוע וחסום.
- להסיר confirm/update dialog ואשף migration רגיל. להציג בפרטי השורה לפני
  הלחיצה ספק קודם/חדש, גרסאות, תלויות, גיבוי והסרה כשמתוכננים.
- להשתמש ב־beginOperationRequest/finishOperationRequest בכל פעולה, כולל recovery.
- 409 מחזיר לרשימה המעודכנת עם הסבר. ניתוק מציג ״לא ניתן לאמת סטטוס״,
  ממשיך מעקב לפי request ID ואינו שולח פעולה חדשה אוטומטית.
- לשמר סיכום אחרי סגירת חלונית, רענון ושינוי שפה. להציג מה עודכן, הועבר,
  דולג, נכשל או לא בוצע. אין ״ממתין״ אחרי תוצאה סופית.
- לתרגם את כל reason_codes ב־i18n.js בעברית ובאנגלית; בלי raw traceback
  בתצוגה הראשית. גרסאות/נתיבים ב־LTR; ראיות טכניות בפרטים הנפתחים.

## 13. שלב ט — בדיקות שהמממש יכתוב והמשתמש יריץ

אין להריץ את הטבלה במהלך המימוש. להוסיף את קובצי הבדיקה לסקריפט ולמסור
אותם למשתמש. אימות המממש מוגבל לתחביר הקבצים ששונו, קישורי התיעוד,
git diff --check ובדיקה נקודתית רק כאשר נחוצה לפתרון שגיאה מזוהה.

| קובץ/תחום | תרחישים מחייבים |
| --- | --- |
| חדש: test_update_selection.py | שלושה ספקים, חדש ביותר כשיר, שוויון שומר בעלים, revision באותו ספק, suffix לא מוכר, downgrade, ספק כושל ומסלול תקין, חלופה חדשה שאינה נתמכת |
| חדש: test_pixi_provider.py | JSON ומיפוי, כל הגרסאות, גרסה גבוהה שאינה נפתרת, osx-64/noarch, lock מדויק, URL/hash חסר, channel זר, metadata חסר, אין PyPI/local build, אין runtime, ביטול |
| חדש: test_provider_ownership.py | port יחיד ללא tool כפול, בלי Brew אחרי מעבר, Pixi מתוך conda-meta, legacy records, שגיאת routes, עדכון עתידי מספק חדש, כשל smoke אחרי update |
| חדש: test_migration_engine.py | הצלחה וכל שלבי הכשל, העתקה חלקית, hash/link/mode, התנגשות shim וקישור שבור, PATH, תלוי חדש, הסרה חלקית, ביטול ופיצוי |
| אותו engine | resume/rollback לפני/אחרי הסרה, תהליך שנקטע, שינוי עצמאי, double request, expiry, שינוי receipt/graph/variant/החרגות, timestamps בלבד אינם שינוי |
| חדש: test_operation_lock.py | שני תהליכים עם DATA_DIR שונים מול אותם TOOLS_ROOT, שחרור אחרי יציאת תהליך, אין נעילה מקוננת, חסימת parallel scan בזמן מעבר |
| הרחבת test_migration_profiles.py | native Intel/ARM/Rosetta, שלוש פקודות IWYU, version/Clang, stdin ודיפ אמיתי, quotes, source hash unchanged, timeouts ופקודה חסרה |
| HTTP | מקור/JSON, 400/409/500, legacy אינו מאשר מעבר, selected/all אותה תוכנית, final progress, hash startup אינו משתנה לאחר שינוי דיסק |
| חדש: unified-update-ui.cjs | כפתור נבחרים/הכול שולח בקשה אחת, ספק ונימוק, חסומים, שינוי תוכנית, תשובה מאוחרת, ניתוק/חזרה, recovery, עברית/אנגלית, תוצאה נשמרת |
| רגרסיה | כל 83 בדיקות הבסיס, תרחישי T01/T02, זהות התראות, ngrok, הגדרות, היסטוריה ושמות התקנות כפולים |
| bundle 004 | no-write check, replay ללא שינוי, restore מדויק וסירוב לעריכה מאוחרת; על עותק זמני בלבד |

בדיקות מנוע משתמשות ב־fake providers, תהליכי fixture ונתיבים זמניים, בלי
תקלות יזומות בהתקנות האמיתיות. HTTP אמיתי נגד backend מבודד עם provider
fixtures בלבד. אין לפתוח backend נוסף מול נתוני הייצור לשם בדיקה.
בדיקות update עתידי משתמשות בקטלוג מבוקר, לא ממציאות גרסה אמיתית שאינה זמינה.

להרחיב את signature collector: רשימה מלאה וממוינת של כל קובצי מוצר/בדיקות/
generator ו־package/lock רלוונטיים, לרבות קבצים חדשים ו־JSON mappings.
לחשב לפני ואחרי, ולהכשיל reuse אם הרשימה או תוכן קובץ השתנו. שינוי מסמך MD
בלבד אינו מחייב הרצה מחדש אם אינו חלק מקוד/fixture; אין להשתמש ב־commit SHA
כתחליף לחתימת התוכן. דוח 83 הבדיקות הוא baseline, לא דוח reusable לקוד החדש.

## 14. שלב י — סקריפט משתמש אחד ובדיקת קבלה

### 14.1 ממשק הפקודות לאחר המימוש בלבד

לשמר את הפקודה העובדת הנוכחית:

```text
tools/verify-migration.py --checks
```

ולהוסיף מסלול מלא אחד:

```text
tools/verify-migration.py --full
```

`--checks` מריץ בדיקות מבודדות ושומר דוח בלי שינוי התקנות אמיתיות.
`--full` מריץ אותן או משתמש בדוח תואם, אורז ומחליף את האפליקציה המקומית
לאחר גיבוי כשהיא פנויה, ואז מציג עדכונים ומבקש אישור כולל אחד לפני שינוי חבילות.
הפעלת --full היא הרשאה להחלפת האפליקציה המקומית; בלי YES לא משתנות חבילות,
אך האפליקציה עצמה כבר עשויה להיות מעודכנת. לציין זאת בתחילת הריצה ובדוח.

הדגל הישן `--acceptance --pilot ...` נשאר מזוהה אך מפנה למסלול הקבלה החדש
עם הגבלת אותה חבילה. אין להפעיל את המימוש החלקי הישן דרך fallback.
אין להוסיף פקודת התקנה עוקפת API בתוך הסקריפט.

### 14.2 סדר הריצה המלאה

1. לפתוח תיקיית דוח חדשה, לאמת Intel, Python, Node/Electron וכלי אריזה
   קיימים. לא להתקין תלויות משתמש אוטומטית. חסר יכולת מייצר exit 2 ודוח.
2. לאתר דוח בדיקות תואם ולוודא שכל שלב חובה עבר. אם אין — להריץ את
   הבדיקות המבודדות. כשל עוצר לפני pack או החלפת אפליקציה.
3. לארוז x64 בלבד בלי פרסום. להפיק build-manifest עם hashes של backend,
   renderer, preload ו־version. לחלץ מהחבילה ולאמת מול הקוד שנבדק.
4. לזהות את אפליקציית Mac Update Manager ואת backend שלה לפי נתיב, health,
   PID, process start ו־DATA_DIR. אם הזהות אינה ודאית, לעצור; אין pkill כללי.
5. לוודא שאין operation, parallel check או transaction פעיל; לגבות אפליקציה,
   הגדרות, היסטוריה וניתובים. לסגור רק Mac Update Manager ולהחליף מקומית.
6. לוודא שהתהליך הישן הסתיים, לפתוח את החדשה ולוודא version/build ID/
   fingerprint startup תואמים. לא להסתפק בכך שהדיסק מכיל קוד חדש.
7. לבצע ״התחל בדיקה״ דרך ה־API הרגיל ולשמור state/plan/מלאי ספקים לפני שינוי.
   להציג בעברית את הפעולות, הגרסאות, המעברים, התלויות והחסמים.
8. ברירת הקבלה למעבר היא IWYU אם הוא כשיר. עדכונים נוספים אינם נכללים
   אוטומטית; אפשרות בחירה של כל/חלק תוצג לפני האישור הכולל. אם IWYU חסום,
   לתעד את הסיבה ולא לבחור כלי חלופי כדי לסמן מעבר כעובר.
9. לבקש YES אחד הקשור ל־plan ID, fingerprints ולרשימת הפריטים. אם המשתמש
   לא מאשר — אין שינוי חבילות; לשמור מצב canceled/approval_required ו־exit 2.
10. לבצע דרך מסלול selected הרגיל. תרחיש הממשק יכול ללחוץ את הכפתור פעם אחת
    אחרי אישור הסקריפט, ולהשתמש ב־renderer/preload הארוזים מול השירות האמיתי.
    אסור לבצע פעם ב־API ואז שוב דרך כפתור. request_id קבוע לריצה.
11. לנטר progress ולשמור transaction IDs, תוצאות, journal וראיות. לוגים נשמרים
    בלי צורך בהעתקת פלט. timeout/ניתוק מחייב query לפי ID; אין retry של mutation.
12. לאמת מלאי אחרי, פקודות, smoke, בעלות, הסרת מקור מבוקרת וגיבוי. לבדוק
    תוצאה בשתי שפות; ביצוע אמיתי פעם אחת בלבד, והשפה השנייה בודקת אותה תוצאה.
    כפתורי שני המצבים נבדקים בשתי שפות ב־fixtures המבודדים.
13. כשהאפליקציה פנויה, לסגור ולפתוח שוב רק אותה; לבדוק PID חדש וחתימות,
    פקודות/שימוש מחדש, בעלות, מלאי ללא כפילות ותוכנית עדכון מהספק החדש.
14. להשוות baseline: שאר חבילות Homebrew, החרגות, binary_only, תזמון,
    הגדרות והיסטוריה. ההיסטוריה רשאית לקבל את הרשומות הצפויות בלבד.
    תלויות MacPorts שהוצגו ותיקיות/רשומות מנוהלות צפויות הן שינויים מותרים.
15. לשמור סיכום שמשווה מטרות לתוצאות. אין להסיק הצלחת מסלול Pixi אמיתי
    מסימולציה בלבד. אם אין עדכון עתידי, מספיק לתכנן אותו מתוך ספק היעד
    ולוודא מבודד התנהגות גרסה חדשה; לא להתקין אותה גרסה שוב כדי לדמות עדכון.

כל התקשרות לשירות צריכה deadline והדפסת עדכון קצר בזמן המתנה. סקריפט הקבלה
אינו הורג פעולת חבילות כדי לעמוד בזמן. אם התהליך פעיל או התוצאה אינה מאומתת,
לשמור מזהה עסקה ו־exit 2. במצב recovery אין להתקין מחדש; להציג תוכנית
recovery חדשה ולקבל אישור כולל אחד להליך שנבחר.

הרצה חוזרת אחרי completed מאמתת מצב קיים ואינה מתקינה/מסירה שוב.
אם הקוד השתנה מאז, בדיקות הקוד מתעדכנות, אבל transaction מוצלח אינו משוכפל.
אין לסגור Codex, להפעיל DevTools remote debugging או לשנות הגדרות MCP.

### 14.3 דוח וקודי יציאה

לשמור תחת `recovery/unified-intel-1.3.4/runs/<run-id>/`:
summary.json, summary.md בעברית, code/build manifests, לוג לכל שלב,
תוכניות, request/transaction IDs, journals, baseline/after inventories,
תוצאות smoke וצילומי ממשק. summary מפריד בין code_checks, packaged_app,
simulated_scenarios, live_migration ו־restart_verification.

לכל שלב status: pass / fail / blocked / not_run / unverified; לתעד גם scope,
ארגומנטים, זמני ריצה ונתיבי ראיות. אין ערך PASS לבדיקה שדולגה.

- 0: כל הבדיקות הנדרשות למצב המבוקש עברו.
- 1: כשל בדיקה או ביצוע שאומת.
- 2: תנאי קדם, אישור שלא ניתן, בדיקה שנחסמה או מצב שלא ניתן לאמת.

אם --checks עבר אבל קבלה אמיתית חסומה, לשמור code_checks=pass והקבלה
blocked, לא לשנות את אחת התוצאות כדי לקבל סימון כולל ירוק.

## 15. סדר קומיטים ומסירה

כל קבוצה כוללת שינויי גנרטור, קוד ובדיקות תואמים; הקומיט לא טוען שסט בדיקות
שלא הורץ עבר. אין ליצור קומיט שבו import חדש מצביע למודול שעוד לא קיים.

1. `feat(selection): add provider decisions and durable ownership` — חוזים,
   fingerprints, בעלות ותמיכת legacy, בדיקות הבחירה והמלאי.
2. `feat(pixi): discover and install exact Intel binary routes` — adapter,
   נעילה של גרסאות, discovery וסגירת תלויות, בדיקות מבוקרות.
3. `fix(migrations): complete activation rollback and recovery` — נעילות,
   גיבוי, ניתוב, פיצוי, פרופילים ובדיקות כשל; legacy notifier נשמר.
4. `feat(updates): execute displayed provider routes from update actions` —
   שילוב snapshot/engine/API/preload/UI, ללא אישור נוסף לכל מעבר.
5. `feat(validation): add one-command packaged Intel acceptance` — script,
   package 1.3.4 ו־lockfile, build identity, דוח ושימור הגדרות.
6. `docs: record delivered scope and pending user acceptance` — מצב אמיתי,
   פקודת משתמש וקישור לדוח, בלי לסגור משימות מוקדם.

שמות אלו הם כוונת חלוקה; מותר לפצל קבוצה כשיש תלות שמחייבת זאת, אך אין
לאחד הכול לקומיט עמום. push של קוד אינו release; אין tag או פרסום חבילה.
התקנה מקומית נעשית במסלול המשתמש לאחר בדיקות תואמות.

## 16. תנאי סיום — לסמן רק עם ראיות

- [ ] סריקה מציגה החלטות מספקים נתמכים, עם השוואה בינארית וגרסה מתאימה ל־Intel.
- [ ] הבחירה נכונה ב־fixtures של שלושת הספקים, שוויון, revision ומידע חסר.
- [ ] ״עדכן נבחרים״ ו״עדכן הכול״ משתמשים בהחלטות שהוצגו, בלי מעבר סמוי או confirm נוסף.
- [ ] מעבר נתמך בוצע ונבדק במחשב, כולל פקודות, מקור שהוסר וגיבוי; חסם סביבה נשאר חסם.
- [ ] ביטול, המשך ו־rollback הוכחו בנתיבים מבודדים, כולל שינוי עצמאי והסרה חלקית.
- [ ] אחרי restart הבעלות והמלאי מציגים ספק חדש ללא כפילות, ותוכנית העדכון מגיעה ממנו.
- [ ] smoke כושל אחרי עדכון ספק אינו מסומן הצלחה; רענון UI כושל אינו מוחק עסקה מוצלחת.
- [ ] בדיקות הקוד והממשק עברו בדוח המשתמש התואם לקוד שנמסר.
- [ ] ngrok, התקנות לא נבחרות, הגדרות וזהות ההתראות נשמרו לפי baseline.
- [ ] התיעוד מבדיל בין מימוש, בדיקות מבודדות וקבלה אמיתית לכל ספק.

לפני דוח המשתמש: ״המימוש נמסר; הבדיקות המלאות ובדיקת הקבלה ממתינות להרצת המשתמש״.
אחרי --checks בלבד: ״בדיקות הקוד עברו; קבלה אמיתית טרם הושלמה״.
אין לסגור T03/T04/T07 יחד רק בגלל שהסקריפט קיים או fixture עבר.

## 17. הוראות קצרות למודל המממש

1. לקרוא תוכנית זו ומסמכי היסוד; לבדוק Git ו־AGENTS בלי להפעיל חבילות ישנות.
2. להתחיל בשלב א ולהתקדם לפי הסדר: חוזים → adapters ובחירה → עסקה/שחזור →
   חיבור API/UI → סקריפט. לכתוב את הבדיקות עם הרכיב, לא להפעיל סט מלא.
3. לשמר שמות/ממשקים קיימים עד שהכניסה החדשה מחוברת; לא להשאיר bypass ישן
   שמאפשר הסרה, ניתוב או התקנה בלי אותו preflight ונעילה.
4. בחירה טכנית שלא נקבעה כאן תיעשה במימוש הפשוט שמשמר את החוזים. סתירה
   מהותית או אפשרות לאבד התקנה תתועד כחסם; אין לשנות יעד או להמציא הצלחה.
5. לעדכן את מסמך המשימות ודוח המימוש. לבצע אימות ממוקד בלבד ולתעד בדיוק מה נבדק.
6. למסור את הפקודה הבאה רק אחרי שהדגל --full נוצר ונבדק נקודתית. היא עדיין
   אינה זמינה בקוד בזמן כתיבת תוכנית זו; לא להורות למשתמש להריץ אותה עכשיו:

```bash
/usr/local/bin/python3.14 "/Users/tzoharlary/Documents/Projects/brew-update-manager/tools/verify-migration.py" --full
```

זו פקודת המסירה העתידית היחידה. המשתמש מריץ, מודיע שהסתיימה, והסוכן קורא
את הדוח המקומי ומטפל בכשלים ממוקדים בלי לדרוש העתקת פלט ארוך.

## 18. מקורות טכניים לבדיקת המימוש

הקוד המקומי הוא בסיס ההתאמה ל־CLI המותקן. המקורות הבאים נבדקו בתכנון,
אך אינם תחליף ל־capability check של הגרסה המקומית:

- [Pixi search](https://pixi.prefix.dev/latest/reference/cli/pixi/search/) — JSON,
  platform, channel ו־limit; --no-config עדיין מאפשר config מקומי, ולכן משתמשים ב־scratch מנוהל.
- [Pixi install](https://pixi.prefix.dev/latest/reference/cli/pixi/install/) —
  --locked בודק התאמת manifest ל־lock; --frozen אינו בודק אותה באותו אופן.
- [Pixi explicit export](https://pixi.prefix.dev/latest/reference/cli/pixi/workspace/export/conda-explicit-spec/) —
  יצוא רשימת הארטיפקטים; parser חייב להתאים לגרסה שהותקנה.
- [Homebrew manpage](https://docs.brew.sh/Manpage) — אפשרויות bottle והסרה;
  preflight של כל התלויות נדרש מעבר להוספת דגל בשורת הפקודה.
- [MacPorts guide](https://guide.macports.org/) — התקנה ומלאי, לצד ה־adapter
  הקיים שמפעיל port במצב בינארי ומכבד את אימות החתימות שלו.
'''

def intel_plan_documents(mode):
    recovery = Path.home() / '.local/share/brew-update-manager/recovery/unified-intel-plan-2026-10-20'
    manifest_path = recovery / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    tasks = (recovery / 'before/docs/MAC_MANAGER_TASKS.md').read_text()
    tasks += '''
## תוכנית המימוש הפעילה ודוח הבסיס — 20.10.2026

[תוכנית מימוש מפורטת ל־T03/T04/T07](INTEL_UPDATE_IMPLEMENTATION_PLAN.md)
מגדירה קבצים, חוזים, אלגוריתם בחירה, עסקה ושחזור, UI, בדיקות וסדר מסירה.
היא תוכנית לביצוע, לא דיווח על השלמת המוצר.

המשתמש הריץ את סקריפט הבדיקות: כל 19 השלבים עברו, כולל 83 בדיקות Python.
[דוח הריצה](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t03-1.3.3/checks-1792496033916688000/summary.json).
בקריאת הדוח חתימות 42 קובצי הקוד התאימו. לא בוצע מעבר חבילות אמיתי.
הסוכן לא הריץ את החבילה מחדש. דוח זה הוא בסיס לקוד 1.3.3, לא בדיקה לשינויי התוכנית.
T03/T04/T07 נשארות פתוחות. הפקודה --full במסמך היא יעד מימוש עתידי ואינה קיימת עדיין.
'''
    goal = (recovery / 'before/docs/PRODUCT_GOAL.md').read_text()
    goal += '\n## תוכנית ביצוע\n\n[תוכנית המימוש המפורטת](INTEL_UPDATE_IMPLEMENTATION_PLAN.md) מתרגמת את ההחלטות\nלחוזי קוד, שלבי עבודה ותנאי קבלה. סטטוס הביצוע נשאר במסמך המשימות.\n'
    outputs = {'docs/INTEL_UPDATE_IMPLEMENTATION_PLAN.md': INTEL_IMPLEMENTATION_PLAN,
               'docs/MAC_MANAGER_TASKS.md': tasks, 'docs/PRODUCT_GOAL.md': goal}
    if mode not in ('intel-plan-check', 'intel-plan-apply', 'intel-plan-restore'):
        raise ValueError('Unknown plan documentation operation')
    changes = []
    for rel, content in outputs.items():
        record = manifest['files'][rel]
        path = ROOT / rel
        current = path.read_bytes() if path.exists() else None
        current_sha = digest(current) if current is not None else None
        after = content.encode()
        if record.get('after') not in (None, digest(after)):
            raise RuntimeError('Plan generator changed after application: ' + rel)
        if current_sha not in (record['before'], digest(after)):
            raise RuntimeError('Plan documentation refuses independent edit: ' + rel)
        original = (recovery / 'before' / rel).read_bytes() if record['before'] else None
        if original is not None and digest(original) != record['before']:
            raise RuntimeError('Plan backup hash mismatch: ' + rel)
        desired = original if mode == 'intel-plan-restore' else after
        if desired != current:
            changes.append((path, desired))
        record['after'] = digest(after)
    if mode == 'intel-plan-check':
        print(f'Plan documentation: {len(changes)} files would change; no writes.')
        return
    manifest['status'] = 'restored' if mode.endswith('restore') else 'applied'
    manifest['generator_after_sha256'] = digest(Path(__file__).read_bytes())
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    for path, data in changes:
        if data is None:
            path.unlink()
        else:
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.intel-plan-')
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    print(f'Plan documentation {manifest["status"]}: {len(changes)} files changed.')


# Bundle 004 foundation: keep generated sources together with the generator.
UNIFIED_FOUNDATION_BACKEND_UPDATE_SELECTION_PY = r'''"""Conservative, side-effect-free Intel provider decision contracts.

A candidate is never executable merely because its version looks newer. The
adapter must attest to a verified binary closure and an executable profile.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

SCHEMA_VERSION = 1
TTL_SECONDS = 15 * 60
_PROVIDER_ORDER = {"homebrew": 0, "macports": 1, "pixi": 2}
_STABLE = re.compile(r"v?([0-9]+(?:\.[0-9]+)*)\Z")


def normalize_upstream(value):
    """Return a numeric tuple; ambiguous suffixes/prereleases are UNKNOWN."""
    if not isinstance(value, str):
        return None
    match = _STABLE.fullmatch(value.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def compare_upstream(left, right):
    """Compare upstream versions, or return None rather than guessing."""
    a, b = normalize_upstream(left), normalize_upstream(right)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def compare_same_provider_package(current_version, current_revision,
                                  target_version, target_revision,
                                  provider_reports_outdated=False):
    """Upstream order first; packaging revisions only within one provider."""
    result = compare_upstream(target_version, current_version)
    if result is None or result != 0:
        return result
    if current_revision is not None and target_revision is not None:
        try:
            a, b = int(target_revision), int(current_revision)
        except (TypeError, ValueError):
            return None
        return (a > b) - (a < b)
    return 1 if provider_reports_outdated is True else 0


def _installation_id(row):
    known = row.get("installation_id")
    if known:
        return known
    provider = row.get("provider", "homebrew")
    name = row.get("provider_name") or row.get("name")
    kind = row.get("kind", "formula")
    if not name or provider not in _PROVIDER_ORDER:
        return None
    if provider == "homebrew" and kind in ("formula", "cask"):
        return f"homebrew:{kind}:{name}"
    if provider == "macports" and kind == "port":
        return f"macports:{name}"
    if provider == "pixi" and kind == "tool":
        return f"pixi:{name}"
    return None


def _material(value):
    """Remove volatile observations from the fingerprint, recursively."""
    volatile = {"checked_at", "created_at", "expires_at", "plan_id",
                "decision_id", "evidence_path", "evidence_refs", "observed_at"}
    if isinstance(value, dict):
        return {k: _material(v) for k, v in sorted(value.items())
                if k not in volatile}
    if isinstance(value, list):
        return [_material(item) for item in value]
    return value


def material_fingerprint(source, candidate, ownership=None):
    data = {"source": _material(source), "candidate": _material(candidate),
            "ownership": _material(ownership or {})}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _migration_allowed(row):
    cls = row.get("classification") or {}
    if cls.get("category") != "standalone_candidate":
        return False
    if cls.get("gaps") or cls.get("consumers") or cls.get("shared_roles"):
        return False
    if cls.get("role") in ("service", "shared", "library"):
        return False
    return (row.get("provider") or "homebrew") == "homebrew" and row.get("kind") == "formula"


def _candidate_issues(row, candidate):
    issues = []
    source_id = _installation_id(row)
    provider = candidate.get("provider")
    action = candidate.get("action")
    if not source_id or candidate.get("source_installation_id") != source_id:
        issues.append("identity_mismatch")
    if candidate.get("logical_id") != (row.get("logical_id") or row.get("name")):
        issues.append("logical_identity_mismatch")
    if provider not in _PROVIDER_ORDER:
        issues.append("unknown_provider")
    if candidate.get("schema_version") != SCHEMA_VERSION:
        issues.append("unsupported_candidate_schema")
    if candidate.get("availability") != "verified":
        issues.append("provider_unverified")
    if candidate.get("execution_supported") is not True:
        issues.append("execution_unsupported")
    sysinfo = candidate.get("system") or {}
    if sysinfo.get("os") != "macos" or sysinfo.get("arch") != "x86_64":
        issues.append("unsupported_platform")
    if candidate.get("closure_verified") is not True:
        issues.append("binary_closure_unverified")
    closure = candidate.get("closure")
    if not isinstance(closure, list):
        issues.append("binary_closure_unverified")
    else:
        for node in closure:
            if not isinstance(node, dict) or node.get("arch") not in ("x86_64", "osx-64", "noarch"):
                issues.append("invalid_binary_closure")
                break
            if not (node.get("archive_id") or node.get("url")):
                issues.append("invalid_binary_closure")
                break
    if not isinstance(candidate.get("provider_names"), list) or not candidate["provider_names"]:
        issues.append("target_identity_missing")
    if action not in ("update", "migrate"):
        issues.append("unsupported_action")
    current_provider = row.get("provider") or "homebrew"
    if provider != current_provider and action != "migrate":
        issues.append("provider_switch_requires_migration")
    if provider == current_provider and action != "update":
        issues.append("same_provider_not_migration")
    if action == "migrate":
        if not _migration_allowed(row):
            issues.append("migration_classification_blocked")
        if not candidate.get("profile_id") or not isinstance(candidate.get("profile_version"), int):
            issues.append("migration_profile_missing")
    if candidate.get("blockers"):
        issues.extend(str(x) for x in candidate["blockers"])
    current_version = (row.get("installed_upstream_version") or row.get("upstream_version")
                       or row.get("installed_version"))
    cmp = (compare_same_provider_package(
        current_version, row.get("installed_revision", row.get("revision")),
        candidate.get("upstream_version"), candidate.get("revision"),
        candidate.get("provider_outdated", False))
        if provider == current_provider
        else compare_upstream(candidate.get("upstream_version"), current_version))
    if cmp is None:
        issues.append("comparison_unknown")
    elif cmp < 0:
        issues.append("downgrade_forbidden")
    elif cmp == 0:
        issues.append("not_newer")
    return list(dict.fromkeys(issues))


def select(installed, candidates, ownership=None, *, plan_id=None, created_at=None):
    """Return one JSON-compatible decision per physical installation."""
    ownership = ownership or {}
    plan_id = plan_id or uuid.uuid4().hex
    created = time.time() if created_at is None else created_at
    results = []
    for row in installed:
        installation_id = _installation_id(row)
        logical_id = row.get("logical_id") or row.get("name")
        provider = row.get("provider") or "homebrew"
        current = (row.get("installed_upstream_version") or row.get("upstream_version")
                   or row.get("installed_version"))
        relevant = [c for c in candidates
                    if c.get("source_installation_id") == installation_id]
        excluded = row.get("excluded") or logical_id == "ngrok" or row.get("name") == "ngrok"
        alternatives = []
        eligible = []
        incomplete = not row.get("discovery_complete", False)
        for candidate in relevant:
            issues = ["excluded_package"] if excluded else _candidate_issues(row, candidate)
            alternative = dict(candidate)
            alternative["reason_codes"] = issues
            alternatives.append(alternative)
            if "provider_unverified" in issues or "comparison_unknown" in issues:
                incomplete = True
            if not issues:
                eligible.append(candidate)
        def preference(c):
            version = normalize_upstream(c["upstream_version"])
            # Version tuple comparison must pad zeros for 1.2 vs 1.2.0.
            padded = version + (0,) * (20 - len(version)) if len(version) < 20 else version
            return (padded, int(c["provider"] == provider),
                    int(c.get("revision") or 0) if c["provider"] == provider else 0,
                    -_PROVIDER_ORDER[c["provider"]])
        selected = max(eligible, key=preference) if eligible else None
        if selected:
            status = "ready"
            action = selected["action"]
            reasons = ["partial_provider_comparison"] if incomplete else ["newest_verified_compatible"]
        elif excluded:
            status, action, reasons = "blocked", "none", ["excluded_package"]
        elif incomplete:
            status, action, reasons = "unverified", "none", ["discovery_incomplete"]
        elif any(a["reason_codes"] for a in alternatives):
            blocking = [a for a in alternatives if "not_newer" not in a["reason_codes"]]
            status, action, reasons = ("blocked", "none", ["no_executable_binary_route"]) if blocking else ("current", "none", ["already_current"])
        else:
            status, action, reasons = "current", "none", ["already_current"]
        source_ownership = (ownership.get(installation_id) or {}
                            if isinstance(ownership, dict) else {})
        result = {
            "decision_id": uuid.uuid4().hex, "plan_id": plan_id,
            "logical_id": logical_id, "source_installation_id": installation_id,
            "current_provider": provider,
            "target_provider": selected["provider"] if selected else provider,
            "installed_version": current,
            "target_version": selected.get("upstream_version") if selected else None,
            "action": action, "status": status, "selected_candidate": selected,
            "alternatives": alternatives, "reason_codes": reasons, "blockers": [],
            "created_at": created, "expires_at": created + TTL_SECONDS,
            "material_fingerprint": material_fingerprint(row, selected, source_ownership),
        }
        if row.get("needs_recovery"):
            result.update(status="needs_recovery", action="none",
                          selected_candidate=None, reason_codes=["recovery_required"])
        results.append(result)
    return results
'''

UNIFIED_FOUNDATION_BACKEND_PROVIDER_OWNERSHIP_PY = r'''"""Durable provider ownership: overlay verified physical inventory only.

Legacy routes are preserved as unverifiable until their exact target is checked.
No port installation is ever represented as a synthetic tool.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

SCHEMA_VERSION = 1


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       indent=2) + "\n").encode("utf-8")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def load_routes(root):
    path = Path(root) / "migrations" / "routes.json"
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": f"ownership_unreadable: {exc}"}
    if not isinstance(raw, dict):
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": "ownership_invalid"}
    if "schema_version" not in raw:
        return {"schema_version": SCHEMA_VERSION, "routes": [],
                "legacy": [{"logical_id": k, "record": v, "verified": False}
                           for k, v in raw.items()],
                "error": None}
    if raw.get("schema_version") != SCHEMA_VERSION or not isinstance(raw.get("routes"), list):
        return {"schema_version": SCHEMA_VERSION, "routes": [], "legacy": [],
                "error": "ownership_schema_unsupported"}
    return {"schema_version": SCHEMA_VERSION, "routes": raw["routes"],
            "legacy": raw.get("legacy", []), "error": None}


def _installation_id(row):
    if row.get("installation_id"):
        return row["installation_id"]
    if row.get("provider") == "macports" and row.get("kind") == "port":
        return "macports:" + row["name"]
    if row.get("provider") == "pixi" and row.get("kind") == "tool":
        return "pixi:" + row["name"]
    if row.get("provider") in (None, "homebrew") and row.get("kind") in ("formula", "cask"):
        return "homebrew:" + row["kind"] + ":" + row["name"]
    return None


def merge_inventory(installed, registry):
    """Never fabricate an installation; attach routes only to observed targets."""
    physical = {key: row for row in installed if (key := _installation_id(row))}
    results = [dict(row) for row in installed]
    by_id = {key: i for i, row in enumerate(results)
             if (key := _installation_id(row))}
    issues = []
    if registry.get("error"):
        issues.append(registry["error"])
    for route in registry.get("routes", []):
        if not isinstance(route, dict):
            issues.append("ownership_record_invalid")
            continue
        target_id = route.get("target_installation_id")
        if target_id not in physical:
            issues.append("ownership_target_missing:" + str(target_id))
            continue
        if route.get("status") != "committed":
            issues.append("ownership_not_committed:" + str(target_id))
            continue
        at = by_id[target_id]
        results[at]["ownership_route"] = route
        results[at]["logical_id"] = route.get("logical_id") or results[at].get("logical_id")
        # Keep physical provider and kind; never relabel a MacPorts port as tool.
    for legacy in registry.get("legacy", []):
        issues.append("legacy_ownership_unverified:" + str(legacy.get("logical_id")))
    return results, issues


def commit_route(root, route, journal_path, installed):
    """Journal changes to route registry; refuses unobserved or ambiguous targets."""
    root = Path(root)
    target = route.get("target_installation_id")
    observed = [row for row in installed if _installation_id(row) == target]
    if len(observed) != 1 or not route.get("transaction_id"):
        raise ValueError("Route ownership requires one verified physical target")
    if route.get("status") != "committed" or not route.get("logical_id"):
        raise ValueError("Uncommitted route cannot become durable ownership")
    journal_path = Path(journal_path)
    if not journal_path.is_file():
        raise ValueError("Recovery journal must exist before ownership is written")
    registry = load_routes(root)
    if registry["error"]:
        raise ValueError(registry["error"])
    entries = [x for x in registry["routes"]
               if x.get("source_installation_id") != route.get("source_installation_id")]
    previous = next((x for x in registry["routes"]
                     if x.get("source_installation_id") == route.get("source_installation_id")), None)
    route = dict(route, generation=int((previous or {}).get("generation") or 0) + 1,
                 verified_at=route.get("verified_at") or time.time())
    entries.append(route)
    target_state = dict(registry, routes=entries)
    digest = hashlib.sha256(json.dumps(target_state, sort_keys=True,
                                     default=str).encode()).hexdigest()
    journal = json.loads(journal_path.read_text())
    journal["ownership_pending_sha256"] = digest
    atomic_write_json(journal_path, journal)
    atomic_write_json(root / "migrations" / "routes.json", target_state)
    journal["ownership_committed_sha256"] = digest
    atomic_write_json(journal_path, journal)
    return route
'''

UNIFIED_FOUNDATION_BACKEND_OPERATION_LOCK_PY = r'''"""Cross-process lock for mutations sharing a TOOLS_ROOT, not a DATA_DIR."""
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
'''

UNIFIED_FOUNDATION_TESTS_TEST_UPDATE_SELECTION_PY = r'''import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import update_selection as selection


class ProviderSelection(unittest.TestCase):
    def source(self, **changes):
        base = {"name": "jq", "logical_id": "jq", "provider": "homebrew",
                "kind": "formula", "installed_version": "1.2",
                "discovery_complete": True,
                "classification": {"category": "standalone_candidate",
                                   "gaps": [], "consumers": []}}
        return dict(base, **changes)

    def candidate(self, provider="homebrew", upstream="1.3", action="update", **changes):
        base = {"schema_version": 1, "logical_id": "jq",
                "source_installation_id": "homebrew:formula:jq",
                "provider": provider, "provider_names": ["jq"],
                "upstream_version": upstream, "package_version": upstream,
                "revision": 0, "action": action,
                "availability": "verified", "execution_supported": True,
                "profile_id": "jq", "profile_version": 1,
                "system": {"os": "macos", "arch": "x86_64", "release": "15"},
                "closure": [], "closure_verified": True, "blockers": []}
        return dict(base, **changes)

    def test_numeric_normalization_and_unknown_suffix(self):
        self.assertEqual(selection.compare_upstream("v1.2", "1.2.0"), 0)
        self.assertIsNone(selection.compare_upstream("1.2rc1", "1.2"))
        self.assertIsNone(selection.compare_upstream("1.2_3", "1.2"))

    def test_selects_newest_verified_route_from_three_providers(self):
        routes = [self.candidate(),
                  self.candidate("macports", "1.4", "migrate"),
                  self.candidate("pixi", "1.5", "migrate")]
        decision, = selection.select([self.source()], routes)
        self.assertEqual((decision["target_provider"], decision["status"]),
                         ("pixi", "ready"))

    def test_tie_keeps_current_provider(self):
        routes = [self.candidate("macports", "1.3", "migrate"), self.candidate()]
        decision, = selection.select([self.source()], routes)
        self.assertEqual(decision["target_provider"], "homebrew")

    def test_newer_blocked_route_does_not_hide_current_update(self):
        routes = [self.candidate(), self.candidate("macports", "9", "migrate",
                                                   closure_verified=False)]
        decision, = selection.select([self.source()], routes)
        self.assertEqual(decision["target_provider"], "homebrew")
        self.assertEqual(len(decision["alternatives"]), 2)

    def test_revision_update_on_same_provider(self):
        source = self.source(installed_version="1.2", revision=0)
        decision, = selection.select([source], [self.candidate(upstream="1.2", revision=1)])
        self.assertEqual(decision["status"], "ready")

    def test_unknown_version_does_not_guess(self):
        decision, = selection.select([self.source()], [self.candidate(upstream="2.0-beta")])
        self.assertEqual(decision["status"], "unverified")

    def test_downgrade_is_not_selected(self):
        decision, = selection.select([self.source()], [self.candidate(upstream="1.1")])
        self.assertEqual(decision["status"], "blocked")

    def test_migration_requires_current_standalone_classification(self):
        decision, = selection.select([self.source(classification={"category": "shared"})],
                                     [self.candidate("macports", "2", "migrate")])
        self.assertEqual(decision["status"], "blocked")

    def test_excluded_package_cannot_execute(self):
        source = self.source(name="ngrok", logical_id="ngrok")
        decision, = selection.select([source], [])
        self.assertEqual(decision["status"], "blocked")

    def test_provider_failure_remains_partial(self):
        failed = self.candidate("pixi", "unknown", "migrate", availability="unverified")
        decision, = selection.select([self.source()], [failed, self.candidate()])
        self.assertEqual(decision["status"], "ready")
        self.assertIn("partial_provider_comparison", decision["reason_codes"])

    def test_fingerprint_ignores_observation_times_not_material_changes(self):
        a = self.candidate(checked_at="first", evidence_refs=["a"])
        b = self.candidate(checked_at="second", evidence_refs=["b"])
        first = selection.material_fingerprint(self.source(), a)
        self.assertEqual(first, selection.material_fingerprint(self.source(), b))
        b["package_version"] = "1.3_2"
        self.assertNotEqual(first, selection.material_fingerprint(self.source(), b))

    def test_cross_provider_revision_does_not_override_current_owner_on_upstream_tie(self):
        source = self.source(installed_upstream_version="1.2", installed_revision=7)
        port = self.candidate("macports", "1.3", "migrate", revision=999)
        home = self.candidate("homebrew", "1.3", "update", revision=0)
        chosen, = selection.select([source], [port, home])
        self.assertEqual(chosen["target_provider"], "homebrew")

    def test_installed_upstream_from_receipt_preserves_revision_update(self):
        source = self.source(installed_version="1.2_5", installed_upstream_version="1.2",
                             installed_revision=5)
        chosen, = selection.select([source], [self.candidate(upstream="1.2", revision=6)])
        self.assertEqual(chosen["status"], "ready")

    def test_cross_provider_lower_upstream_cannot_win_due_to_revision(self):
        source = self.source()
        home = self.candidate(upstream="1.4", revision=0)
        port = self.candidate("macports", "1.3", "migrate", revision=999)
        chosen, = selection.select([source], [home, port])
        self.assertEqual(chosen["target_provider"], "homebrew")

    def test_two_physical_installations_never_merge_by_logical_id(self):
        a = self.source()
        b = self.source(provider="macports", kind="port", installation_id="macports:jq")
        decisions = selection.select([a, b], [self.candidate()])
        self.assertEqual(len(decisions), 2)
        self.assertNotEqual(decisions[0]["source_installation_id"],
                            decisions[1]["source_installation_id"])


if __name__ == "__main__":
    unittest.main()
'''

UNIFIED_FOUNDATION_TESTS_TEST_PROVIDER_OWNERSHIP_PY = r'''import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import provider_ownership as owner


class Ownership(unittest.TestCase):
    def test_no_synthetic_port_or_duplicate_tool(self):
        rows = [{"name": "jq", "provider": "macports", "kind": "port",
                 "installation_id": "macports:jq"}]
        registry = {"routes": [{"logical_id": "jq", "source_installation_id": "homebrew:formula:jq",
                                "target_installation_id": "macports:jq", "status": "committed"}],
                    "legacy": [], "error": None}
        merged, issues = owner.merge_inventory(rows, registry)
        self.assertFalse(issues)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["kind"], "port")

    def test_old_route_is_preserved_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migrations" / "routes.json"
            owner.atomic_write_json(path, {"jq": {"provider": "macports"}})
            state = owner.load_routes(temp)
            self.assertEqual(len(state["legacy"]), 1)
            self.assertEqual(state["routes"], [])

    def test_broken_file_does_not_wipe_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migrations" / "routes.json"
            path.parent.mkdir(parents=True)
            path.write_text("{ invalid")
            state = owner.load_routes(temp)
            merged, issues = owner.merge_inventory([{"name": "jq", "kind": "formula"}], state)
            self.assertEqual(len(merged), 1)
            self.assertTrue(issues)

    def test_route_commit_requires_journal_and_physical_target(self):
        with tempfile.TemporaryDirectory() as temp:
            route = {"logical_id": "jq", "source_installation_id": "homebrew:formula:jq",
                     "target_installation_id": "macports:jq", "status": "committed",
                     "transaction_id": "transaction1"}
            journal = Path(temp) / "journal.json"
            journal.write_text("{}")
            with self.assertRaises(ValueError):
                owner.commit_route(temp, route, journal, [])
            row = {"name": "jq", "provider": "macports", "kind": "port"}
            saved = owner.commit_route(temp, route, journal, [row])
            self.assertEqual(saved["generation"], 1)
            self.assertEqual(len(owner.load_routes(temp)["routes"]), 1)


if __name__ == "__main__":
    unittest.main()
'''

UNIFIED_FOUNDATION_TESTS_TEST_OPERATION_LOCK_PY = r'''import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from operation_lock import OperationBusy, OperationLock


class LockTests(unittest.TestCase):
    def test_lock_is_shared_across_data_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            with OperationLock(temp, "request-1"):
                with self.assertRaises(OperationBusy):
                    with OperationLock(temp, "request-2"):
                        pass
            with OperationLock(temp, "request-3"):
                self.assertTrue((Path(temp) / "migrations" / "operation.lock").exists())


if __name__ == "__main__":
    unittest.main()
'''


UNIFIED_PIXI_PROVIDER = r'''"""Conservative Pixi osx-64 adapter; immutable lock verification before install.

This adapter never installs Pixi, never alters an existing user workspace, and
never uses a locally configured channel or PyPI/build dependencies.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from update_routes import PIXI_TOOLS
from update_selection import normalize_upstream, compare_upstream

TRUSTED_HOST = "conda.anaconda.org"
CHANNEL = "conda-forge"
PLATFORM = "osx-64"
MAX_SOLVE_SECONDS = 180
NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


class PixiUnavailable(RuntimeError):
    pass


def _check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name) or name == "ngrok":
        raise ValueError("Unmapped, excluded, or invalid Pixi tool")
    if name not in PIXI_TOOLS:
        raise ValueError("Pixi tool is not mapped")
    return name


def runtime(service):
    """Use only an already-installed, supported runtime."""
    path = Path(service.DATA_DIR) / "runtime" / "bin" / "pixi"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise PixiUnavailable("pixi_runtime_missing")
    result = service.run_cmd([str(path), "--version"], timeout=30, cancellable=True)
    if result.code or not re.search(r"\bpixi\s+\d+\.\d+", result.stdout + result.stderr):
        raise PixiUnavailable("pixi_runtime_unverified")
    return str(path)


def _env(root):
    root = Path(root)
    return {
        "HOME": str(root), "PIXI_HOME": str(root / "pixi-home"),
        "PIXI_CACHE_DIR": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "xdg"), "XDG_CACHE_HOME": str(root / "cache"),
        "PIXI_NO_INSTALL": "true", "PIXI_PLATFORM": PLATFORM,
    }


def parse_search(payload, mapped_name):
    """Fail closed on unknown Pixi CLI output instead of guessing package versions."""
    if isinstance(payload, dict) and isinstance(payload.get("packages"), list):
        records = payload["packages"]
    elif isinstance(payload, list):
        records = payload
    else:
        raise PixiUnavailable("pixi_search_schema_unsupported")
    versions = set()
    for record in records:
        if not isinstance(record, dict) or record.get("name") != mapped_name:
            continue
        if record.get("subdir", PLATFORM) not in (PLATFORM, "noarch"):
            continue
        version = record.get("version")
        if normalize_upstream(version) is not None:
            versions.add(version)
    return sorted(versions, key=lambda x: tuple(list(normalize_upstream(x)) + [0] * 20), reverse=True)


def manifest_text(name, version):
    _check_name(name)
    if normalize_upstream(version) is None:
        raise ValueError("Pixi version must be a stable numeric version")
    mapped = PIXI_TOOLS[name][0]
    return ('[workspace]\nname = "mac-manager-' + name.replace(".", "-") +
            '"\nchannels = ["conda-forge"]\nplatforms = ["osx-64"]\n\n' +
            '[dependencies]\n' + json.dumps(mapped) + " = " + json.dumps("==" + version) + "\n")


def _artifact(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != TRUSTED_HOST or parsed.username or parsed.password:
        raise PixiUnavailable("untrusted_binary_channel")
    if not parsed.path.startswith("/conda-forge/"):
        raise PixiUnavailable("untrusted_binary_channel")
    pieces = parsed.path.split("/")
    if len(pieces) < 4 or pieces[2] not in (PLATFORM, "noarch") or not pieces[-1].endswith((".conda", ".tar.bz2")):
        raise PixiUnavailable("wrong_platform_or_format")
    if not parsed.fragment or not re.fullmatch(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})", parsed.fragment):
        raise PixiUnavailable("binary_checksum_missing")
    return {"url": url.split("#")[0], "arch": pieces[2], "archive_id": pieces[-1],
            "checksum": parsed.fragment.lower(),
            "checksum_algorithm": "sha256" if len(parsed.fragment) == 64 else "md5"}


def parse_explicit(directory):
    paths = list(Path(directory).glob("*"))
    if not paths:
        raise PixiUnavailable("explicit_export_missing")
    nodes = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            text = line.strip()
            if text.startswith(("https://", "http://")):
                nodes.append(_artifact(text))
            elif text and not text.startswith(("#", "@EXPLICIT")):
                raise PixiUnavailable("unknown_explicit_spec_line")
    if not nodes or not any(node["arch"] == PLATFORM for node in nodes):
        raise PixiUnavailable("native_binary_closure_missing")
    return nodes


def _run(service, command, root, timeout):
    result = service.run_cmd(command, timeout=timeout, cancellable=True, env_override=_env(root))
    if result.code:
        raise PixiUnavailable("pixi_command_failed: " + (result.stderr or result.stdout)[-300:])
    return result


def discover(service, name, *, max_seconds=MAX_SOLVE_SECONDS):
    """Search all versions and find the newest solvable, verified Intel closure.

    A higher unsolved candidate is never silently replaced with a lower version
    when the time budget expires. No real Pixi environments are modified.
    """
    _check_name(name)
    if name in ("node", "deno", "uv"):
        raise PixiUnavailable("existing_independent_provider_preserved")
    pixi = runtime(service)
    with tempfile.TemporaryDirectory(prefix="mac-manager-pixi-") as temp:
        root = Path(temp)
        mapped = PIXI_TOOLS[name][0]
        found = _run(service, [pixi, "search", mapped, "--channel", CHANNEL,
                               "--platform", PLATFORM, "--json", "--limit", "10000",
                               "--no-config"], root, min(max_seconds, 60))
        try:
            versions = parse_search(json.loads(found.stdout), mapped)
        except (ValueError, TypeError) as error:
            raise PixiUnavailable("pixi_search_json_invalid") from error
        if not versions:
            raise PixiUnavailable("no_stable_osx_64_versions")
        deadline = time.monotonic() + max_seconds
        failures = []
        for version in versions:
            service.raise_if_cancel_requested()
            remaining = deadline - time.monotonic()
            if remaining < 10:
                raise PixiUnavailable("pixi_comparison_incomplete")
            manifest = root / "pixi.toml"
            manifest.write_text(manifest_text(name, version))
            lock = root / "pixi.lock"
            lock.unlink(missing_ok=True)
            try:
                _run(service, [pixi, "lock", "--no-install", "--no-config",
                               "--manifest-path", str(manifest)], root, min(remaining, 90))
                if not lock.is_file():
                    raise PixiUnavailable("pixi_lock_missing")
                export = root / "explicit"
                export.mkdir(exist_ok=True)
                _run(service, [pixi, "workspace", "export", "conda-explicit-spec",
                               str(export), "--manifest-path", str(manifest),
                               "--no-config"], root, min(deadline - time.monotonic(), 60))
                nodes = parse_explicit(export)
                return {"provider": "pixi", "upstream_version": version,
                        "package_version": version, "revision": None,
                        "provider_names": [mapped], "system": {
                            "os": "macos", "arch": "x86_64"},
                        "closure": nodes, "closure_verified": True,
                        "manifest": manifest.read_text(), "lock": lock.read_text(),
                        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
                        "availability": "verified", "execution_supported": True,
                        "unsolved_versions": failures}
            except PixiUnavailable as error:
                failures.append({"version": version, "reason": str(error)})
        raise PixiUnavailable("no_verified_binary_lock")


def install_locked(service, candidate, generation):
    """Install into a new empty managed generation only; never modify user data."""
    if candidate.get("provider") != "pixi" or not candidate.get("closure_verified"):
        raise ValueError("Cannot install unverified Pixi candidate")
    if candidate.get("system", {}).get("arch") != "x86_64":
        raise ValueError("Pixi candidate is not Intel")
    manifest = candidate.get("manifest")
    lock = candidate.get("lock")
    if not isinstance(manifest, str) or not isinstance(lock, str):
        raise ValueError("Exact Pixi manifest and lock are required")
    generation = Path(generation)
    if generation.exists():
        raise FileExistsError("Pixi generation must be new")
    if not candidate.get("lock_sha256") == hashlib.sha256(lock.encode()).hexdigest():
        raise ValueError("Pixi lock fingerprint changed")
    pixi = runtime(service)
    generation.mkdir(parents=True)
    (generation / "pixi.toml").write_text(manifest)
    (generation / "pixi.lock").write_text(lock)
    result = service.run_cmd([pixi, "install", "--locked", "--no-config",
                              "--manifest-path", str(generation / "pixi.toml")],
                             timeout=1800, cancellable=True, env_override=_env(generation))
    if result.code:
        raise PixiUnavailable("pixi_locked_install_failed: " + result.stderr[-200:])
    prefix = generation / ".pixi" / "envs" / "default"
    if not (prefix / "conda-meta").is_dir():
        raise PixiUnavailable("pixi_installed_inventory_missing")
    return prefix


def installed_record(prefix, mapped_name, required_version):
    """Read live conda-meta; a declared manifest alone is not installation evidence."""
    records = []
    for path in (Path(prefix) / "conda-meta").glob("*.json"):
        try:
            item = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if item.get("name") == mapped_name:
            records.append(item)
    if len(records) != 1 or compare_upstream(records[0].get("version"), required_version) != 0:
        raise PixiUnavailable("pixi_installed_version_mismatch")
    return records[0]


def verify_generation(prefix, closure):
    """Match every approved archive and published checksum to live conda-meta.

    An existing user-managed environment is never treated as evidence for a
    generated application-owned environment.
    """
    folder = Path(prefix) / "conda-meta"
    if not folder.is_dir() or not isinstance(closure, list) or not closure:
        raise PixiUnavailable("pixi_closure_inventory_missing")
    installed = {}
    for path in folder.glob("*.json"):
        try:
            metadata = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise PixiUnavailable("pixi_invalid_conda_metadata") from error
        name = metadata.get("fn")
        if not isinstance(name, str) or name in installed:
            raise PixiUnavailable("pixi_ambiguous_archive_inventory")
        installed[name] = metadata
    for node in closure:
        archive = node.get("archive_id")
        record = installed.get(archive)
        if record is None or node.get("arch") not in (PLATFORM, "noarch"):
            raise PixiUnavailable("pixi_expected_archive_missing")
        checksum = record.get(node.get("checksum_algorithm"))
        if not isinstance(checksum, str) or checksum.lower() != node.get("checksum"):
            raise PixiUnavailable("pixi_archive_checksum_mismatch")
        if record.get("subdir") != node["arch"]:
            raise PixiUnavailable("pixi_installed_architecture_mismatch")
    if len(installed) != len(closure):
        raise PixiUnavailable("pixi_unapproved_extra_archives")
    return {"verified_archives": len(closure)}
'''

UNIFIED_PIXI_TESTS = r'''import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import pixi_provider as p


class PixiContract(unittest.TestCase):
    def test_exact_manifest_and_no_inherited_channels(self):
        text = p.manifest_text("jq", "1.7")
        self.assertIn('channels = ["conda-forge"]', text)
        self.assertIn('platforms = ["osx-64"]', text)
        self.assertIn('"jq" = "==1.7"', text)
        self.assertNotIn(">=", text)

    def test_search_rejects_unknown_and_prerelease(self):
        with self.assertRaises(p.PixiUnavailable):
            p.parse_search({"unexpected": []}, "jq")
        found = p.parse_search([{"name":"jq","version":"1.2"},{"name":"jq","version":"2.0rc1"},
                                {"name":"git","version":"100"}], "jq")
        self.assertEqual(found, ["1.2"])

    def test_explicit_rejects_foreign_channel_and_missing_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "osx-64.txt"
            path.write_text("@EXPLICIT\nhttps://other.example/a.conda#abcdef\n")
            with self.assertRaises(p.PixiUnavailable):
                p.parse_explicit(temp)
            path.write_text("@EXPLICIT\nhttps://conda.anaconda.org/conda-forge/osx-64/a.conda\n")
            with self.assertRaises(p.PixiUnavailable):
                p.parse_explicit(temp)

    def test_explicit_accepts_verified_intel_and_noarch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "osx-64.txt"
            path.write_text("@EXPLICIT\nhttps://conda.anaconda.org/conda-forge/osx-64/jq-1.2.conda#" + "a"*64 + "\n"
                            "https://conda.anaconda.org/conda-forge/noarch/foo-1.0.conda#" + "b"*32 + "\n")
            nodes = p.parse_explicit(temp)
            self.assertEqual({x["arch"] for x in nodes}, {"osx-64", "noarch"})

    def test_no_existing_workspace_overwrite(self):
        class S:
            pass
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                p.install_locked(S(), {"provider":"pixi", "closure_verified":False}, temp)

    def test_ngrok_and_unmapped_blocked(self):
        for name in ("ngrok", "not-mapped", "../evil"):
            with self.assertRaises(ValueError):
                p.manifest_text(name, "1.2")

    def test_conda_meta_must_match_one_exact_version(self):
        with tempfile.TemporaryDirectory() as temp:
            meta = Path(temp) / "conda-meta"
            meta.mkdir()
            (meta / "jq-1.json").write_text('{"name":"jq","version":"1.2"}')
            self.assertEqual(p.installed_record(temp, "jq", "1.2.0")["version"], "1.2")
            with self.assertRaises(p.PixiUnavailable):
                p.installed_record(temp, "jq", "1.3")


    def test_pixi_full_closure_must_match_conda_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            meta = Path(root)/"conda-meta"
            meta.mkdir()
            (meta/"jq.json").write_text('{"fn":"jq-1.2.conda", "name":"jq",'
                                         '"version":"1.2", "subdir":"osx-64",'
                                         '"sha256":"' + "a"*64 + '"}')
            closure = [{"archive_id":"jq-1.2.conda","arch":"osx-64",
                        "checksum_algorithm":"sha256","checksum":"a"*64}]
            self.assertEqual(p.verify_generation(root,closure)["verified_archives"],1)
            closure[0]["checksum"] = "b"*64
            with self.assertRaises(p.PixiUnavailable):
                p.verify_generation(root,closure)


if __name__ == "__main__":
    unittest.main()
'''

UNIFIED_UPDATES = r'''"""Persisted, conservative unified Intel update decisions and batch execution.

The advertised plan is authoritative. Unsafe/missing migration routes are
visible as blocked alternatives; they can never be executed by this layer.
"""
from __future__ import annotations
import hashlib
import json
import platform
from pathlib import Path
import time
import uuid

import macports_provider
import provider_ownership
import update_routes
import update_selection
import migration_profiles


class PlanError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code


def _plan_dir(s):
    return Path(s.DATA_DIR) / "update-plans"


def _ids(row):
    name = row.get("provider_name") or row.get("name")
    provider = row.get("provider") or "homebrew"
    if provider == "macports" and row.get("kind") == "port":
        return "macports:" + name
    if provider == "pixi" and row.get("kind") == "tool":
        return "pixi:" + name
    if provider == "homebrew" and row.get("kind") in ("formula", "cask"):
        return "homebrew:" + row["kind"] + ":" + name
    return row.get("installation_id")


def _base_candidate(row, provider, action, *, version=None):
    version = version or row.get("latest_version")
    return dict(schema_version=1, logical_id=row.get("logical_id") or row["name"],
                source_installation_id=_ids(row), provider=provider,
                provider_names=[row.get("provider_name") or row["name"]],
                upstream_version=version, package_version=version,
                revision=None, action=action, availability="unverified",
                execution_supported=False, profile_id=None, profile_version=None,
                system={"os": "macos", "arch": "x86_64",
                        "release": platform.mac_ver()[0]},
                closure=[], closure_verified=False, evidence_refs=[],
                checked_at=time.time(), blockers=["binary_preflight_missing"])


def _formula_closure(name, catalog, installed, excluded):
    """Use the same dependency closure that guards the existing binary policy."""
    problems = update_routes.bottle_blockers(name, catalog, installed, excluded)
    if problems:
        return None, problems
    found, visited = [], set()
    def visit(current):
        if current in visited:
            return
        visited.add(current)
        entry = catalog.get(current)
        if not entry or not entry.get("bottle_checksum") or not entry.get("bottle_tag"):
            raise PlanError("binary_closure_unverified")
        found.append({"name": current, "arch": "x86_64", "version": entry.get("stable_version"),
                      "revision": entry.get("revision") or 0,
                      "archive_id": entry["bottle_tag"], "checksum": entry["bottle_checksum"],
                      "tag": entry["bottle_tag"]})
        for dep in entry.get("stable_dependencies", []):
            if isinstance(dep, str):
                visit(dep)
            elif isinstance(dep, dict):
                for dep_name, category in dep.items():
                    if category not in (":build", ":test", ":optional"):
                        visit(dep_name)
    visit(name)
    return found, []


def _homebrew(s, row, catalog, installed, settings):
    candidate = _base_candidate(row, "homebrew", "update")
    if row.get("kind") != "formula" or row.get("inventory_stale"):
        candidate["blockers"] = ["homebrew_candidate_unverified"]
        return candidate
    if not catalog or installed is None:
        candidate["blockers"] = ["homebrew_catalog_unavailable"]
        return candidate
    try:
        closure, issues = _formula_closure(row["name"], catalog, installed,
                                          settings["excluded_packages"])
        if issues:
            candidate["blockers"] = [x.get("reason", "binary_unavailable") for x in issues]
            return candidate
        entry = catalog[row["name"]]
        candidate.update(upstream_version=entry["stable_version"],
                         package_version=str(entry["stable_version"]) +
                         ("_" + str(entry.get("revision")) if entry.get("revision") else ""),
                         revision=int(entry.get("revision") or 0),
                         closure=closure, closure_verified=True, availability="verified",
                         execution_supported=True, provider_outdated=bool(row.get("outdated")),
                         blockers=[])
    except (KeyError, ValueError, PlanError) as error:
        candidate["blockers"] = [str(error)]
    return candidate


def _ports(s, row, roots, action):
    candidate = _base_candidate(row, "macports", action)
    candidate["provider_names"] = list(roots)
    if not roots:
        candidate["blockers"] = ["port_mapping_missing"]
        return candidate
    try:
        result = macports_provider.preflight(s, list(roots))
        nodes = result.get("nodes", [])
        node = next((n for n in nodes if n["name"] == roots[0]), None)
        if node is None:
            raise PlanError("port_root_unverified")
        closure = [dict(x, arch="x86_64", archive_id=x.get("archive"))
                   for x in nodes]
        candidate.update(upstream_version=node["version"], package_version=node["version"],
                         revision=node.get("revision"), closure=closure,
                         closure_verified=True, availability="verified",
                         execution_supported=(action == "update"),
                         blockers=[] if action == "update" else ["migration_execution_not_yet_integrated"],
                         graph=result)
    except Exception as exc:
        if isinstance(exc, getattr(s, "OperationCancelledError", ())):
            raise
        candidate["blockers"] = ["macports_preflight_failed: " + str(exc)[-200:]]
    return candidate



def _pixi(s, row):
    """Only app-owned Pixi tools can receive an in-place provider update."""
    candidate = _base_candidate(row, "pixi", "update", version=None)
    name = row.get("name")
    if name not in update_routes.PIXI_TOOLS:
        candidate["blockers"] = ["unmapped_pixi_tool"]
        return candidate
    managed = update_routes.read_records(s.TOOLS_ROOT).get(name, {})
    if managed.get("provider") != "pixi":
        candidate["blockers"] = ["pixi_ownership_unverified"]
        return candidate
    import pixi_provider
    prefix = Path(managed.get("prefix") or "/nonexistent")
    allowed = Path(s.TOOLS_ROOT) / "environments" / name
    if not prefix.resolve().is_relative_to(allowed.resolve()):
        candidate["blockers"] = ["user_environment_not_managed"]
        return candidate
    try:
        evidence = pixi_provider.installed_record(
            prefix, update_routes.PIXI_TOOLS[name][0], row["installed_version"])
        if evidence.get("version") != managed.get("version"):
            raise pixi_provider.PixiUnavailable("managed_pixi_version_mismatch")
        route = pixi_provider.discover(s, name)
        candidate.update(route, schema_version=1, logical_id=name,
                         source_installation_id=_ids(row), action="update",
                         profile_id=name, profile_version=1, blockers=[],
                         checked_at=time.time())
    except Exception as error:
        if isinstance(error, getattr(s, "OperationCancelledError", ())):
            raise
        candidate["blockers"] = ["pixi_discovery_unverified:" + str(error)[-200:]]
    return candidate


def _pixi_update(s, source, candidate):
    """Install an immutable managed Pixi generation and check its live metadata."""
    import pixi_provider
    name = source["name"]
    fresh = _pixi(s, source)
    for key in ("upstream_version", "lock_sha256", "closure", "manifest", "lock"):
        if not fresh.get("execution_supported") or fresh.get(key) != candidate.get(key):
            raise PlanError("plan_changed")
    directory = (Path(s.TOOLS_ROOT) / "environments" / name /
                 ("generation-" + uuid.uuid4().hex))
    prefix = pixi_provider.install_locked(s, candidate, directory)
    mapped, commands, probe = update_routes.PIXI_TOOLS[name]
    pixi_provider.installed_record(prefix, mapped, candidate["upstream_version"])
    pixi_provider.verify_generation(prefix, candidate["closure"])
    binaries = {command: prefix / "bin" / command for command in commands}
    if not all(p.is_file() for p in binaries.values()):
        raise PlanError("pixi_missing_mandatory_commands")
    environment = {"CONDA_PREFIX": str(prefix), "PATH": str(prefix / "bin") + ":/usr/bin:/bin"}
    check = s.run_cmd([str(prefix / "bin" / probe[0]), *probe[1:]],
                      timeout=90, cancellable=True, env_override=environment)
    if check.code:
        raise PlanError("pixi_smoke_failed", check.stderr or check.stdout)
    record = update_routes.activate(
        s.TOOLS_ROOT, name, "pixi", directory, prefix,
        candidate["upstream_version"], commands, check.stdout + check.stderr)
    return {"ok": True, "name": name, "kind": "tool",
            "code": 0, "provider": "pixi", "action": "update",
            "installed_version": record["version"],
            "stdout": "Exact locked Pixi generation installed and verified",
            "stderr": ""}

def _load_homebrew_catalog(s):
    from update_engine import Engine
    engine = Engine(s)
    return engine.catalog(), engine.installed()


def _unavailable_bridge(row, provider, target_names, reason):
    """A mapped package is not necessarily a verified executable route."""
    action = "update" if provider == (row.get("provider") or "homebrew") else "migrate"
    candidate = _base_candidate(row, provider, action)
    candidate["provider_names"] = list(target_names)
    candidate["blockers"] = [reason]
    return candidate


def _pixi_bridge(s, row, *, timeout=60):
    """Discover an exact Intel route, without inventing a missing migration."""
    candidate = _unavailable_bridge(
        row, "pixi", [update_routes.PIXI_TOOLS[row["name"]][0]],
        "pixi_migration_transaction_unavailable")
    import pixi_provider
    try:
        verified = pixi_provider.discover(s, row["name"], max_seconds=timeout)
        candidate.update(verified, schema_version=1, logical_id=row["name"],
                         source_installation_id=_ids(row), action="migrate",
                         execution_supported=False, profile_id=None, profile_version=None,
                         blockers=["pixi_migration_transaction_unavailable"],
                         checked_at=time.time())
    except Exception as error:
        if isinstance(error, getattr(s, "OperationCancelledError", ())):
            raise
        candidate["blockers"].append("pixi_discovery_unverified:" + str(error)[-180:])
    return candidate


def collect_candidates(s, snapshot):
    """Collect all *mapped* provider alternatives, regardless of brew.outdated.

    Classifications originate in the common T02 inventory scan. A missing
    migration profile remains a visible blocker, not a runnable alternative.
    """
    rows = snapshot.get("packages", [])
    mapping = json.loads((Path(__file__).with_name("macports-map.json")).read_text())
    candidates = []
    settings = s.load_settings()
    try:
        catalog, installed = _load_homebrew_catalog(s)
    except Exception:
        catalog, installed = None, None
    for row in rows:
        provider = row.get("provider") or "homebrew"
        name = row.get("name")
        if (not name or row.get("excluded") or name == "ngrok"
                or row.get("inventory_stale")):
            row["discovery_complete"] = False
            continue
        row["discovery_complete"] = True
        if provider == "homebrew" and row.get("kind") == "formula":
            if catalog is None or installed is None:
                candidates.append(_unavailable_bridge(
                    row, "homebrew", [name], "homebrew_catalog_unavailable"))
                row["discovery_complete"] = False
            else:
                entry = catalog.get(name)
                current = row.get("installed_upstream_version") or row.get("installed_version")
                version = entry.get("stable_version") if isinstance(entry, dict) else None
                comparison = (update_selection.compare_upstream(str(version), str(current))
                              if version is not None else None)
                # No closure inspection is needed if a known stable upstream
                # cannot be an update; other mapped providers are still probed.
                if row.get("outdated") or comparison is None or comparison > 0:
                    candidate = _homebrew(s, row, catalog, installed, settings)
                    candidates.append(candidate)
                    if candidate.get("availability") != "verified":
                        row["discovery_complete"] = False
            roots = mapping.get(name)
            if roots:
                profile = migration_profiles.get(name)
                classification = row.get("classification") or {}
                if not profile or profile.provider != "macports":
                    candidates.append(_unavailable_bridge(
                        row, "macports", roots, "migration_profile_missing"))
                elif classification.get("category") != "standalone_candidate":
                    candidates.append(_unavailable_bridge(
                        row, "macports", roots, "fresh_classification_required"))
                else:
                    offer = _ports(s, row, roots, "migrate")
                    offer["profile_id"] = profile.name
                    offer["profile_version"] = profile.version
                    if offer["availability"] == "verified":
                        try:
                            import migration_engine
                            plan = migration_engine.plan(s, name, classification=classification)
                            offer.update(migration_plan_id=plan["id"],
                                         migration_fingerprint=plan["fingerprint"],
                                         execution_supported=True, blockers=[])
                        except Exception as error:
                            if isinstance(error, getattr(s, "OperationCancelledError", ())):
                                raise
                            offer["blockers"] = ["migration_preflight_failed:" + str(error)[-180:]]
                    else:
                        row["discovery_complete"] = False
                    candidates.append(offer)
            if name in update_routes.PIXI_TOOLS:
                classification = row.get("classification") or {}
                if classification.get("category") != "standalone_candidate":
                    candidates.append(_unavailable_bridge(
                        row, "pixi", [update_routes.PIXI_TOOLS[name][0]],
                        "fresh_classification_required"))
                else:
                    candidate = _pixi_bridge(s, row)
                    candidates.append(candidate)
                    if candidate.get("availability") != "verified":
                        row["discovery_complete"] = False
        elif provider == "homebrew" and row.get("kind") == "cask":
            candidates.append(_unavailable_bridge(
                row, "homebrew", [name], "cask_binary_adapter_not_verified"))
        elif provider == "macports" and row.get("kind") == "port":
            if row.get("outdated"):
                candidate = _ports(s, row, [name], "update")
                candidates.append(candidate)
                if candidate.get("availability") != "verified":
                    row["discovery_complete"] = False
        elif provider == "pixi" and row.get("kind") == "tool":
            candidate = _pixi(s, row)
            candidates.append(candidate)
            if candidate.get("availability") != "verified":
                row["discovery_complete"] = False
    return candidates

def _safe_snapshot(s, snapshot):
    rows = snapshot.get("packages", [])
    normalized = []
    for item in rows:
        row = dict(item)
        row["installation_id"] = _ids(row)
        normalized.append(row)
    registry = provider_ownership.load_routes(s.TOOLS_ROOT)
    merged, issues = provider_ownership.merge_inventory(normalized, registry)
    return merged, registry, issues


def generate_plan(s, snapshot):
    rows, registry, issues = _safe_snapshot(s, snapshot)
    candidates = collect_candidates(s, dict(snapshot, packages=rows))
    pid = uuid.uuid4().hex
    decisions = update_selection.select(rows, candidates, plan_id=pid)
    if registry.get("error"):
        for decision in decisions:
            decision.update(status="unverified", action="none", selected_candidate=None,
                            reason_codes=["ownership_unreadable"], blockers=[registry["error"]])
    by_id = {d["source_installation_id"]: d for d in decisions}
    for row in rows:
        decision = by_id.get(row["installation_id"])
        row["update_decision"] = decision
        if decision and decision["status"] == "ready":
            row["outdated"] = True
    created = time.time()
    plan = {"schema_version": 1, "id": pid, "created_at": created,
            "expires_at": created + update_selection.TTL_SECONDS,
            "decisions": decisions, "ownership_issues": issues}
    provider_ownership.atomic_write_json(_plan_dir(s) / (pid + ".json"), plan)
    provider_ownership.atomic_write_json(_plan_dir(s) / "latest.json", {"plan_id": pid})
    snapshot["packages"] = rows
    snapshot["update_decisions"] = decisions
    snapshot["unified_plan_id"] = pid
    snapshot["unified_plan_expires_at"] = plan["expires_at"]
    snapshot["unified_plan_stale"] = False
    return plan


def read_plan(s, plan_id=None):
    if plan_id is None:
        pointer = s.read_json(_plan_dir(s) / "latest.json") or {}
        plan_id = pointer.get("plan_id")
    if not isinstance(plan_id, str) or not re_full_id(plan_id):
        return None
    plan = s.read_json(_plan_dir(s) / (plan_id + ".json"))
    return plan if isinstance(plan, dict) and plan.get("id") == plan_id else None


def re_full_id(value):
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def attach_cached(s, snapshot):
    """After expiry, present the plan as stale without offering executable routes."""
    plan = read_plan(s)
    if not plan:
        if snapshot.get("unified_plan_id"):
            snapshot["unified_plan_stale"] = True
            snapshot["unified_plan_expires_at"] = 0
            snapshot["update_decisions"] = []
            for row in snapshot.get("packages", []):
                row.pop("update_decision", None)
        return snapshot
    expiry = plan.get("expires_at")
    fresh = isinstance(expiry, (int, float)) and expiry > time.time()
    stored = plan.get("decisions") or []
    decisions = stored if fresh else [
        dict(d, status="unverified", action="none",
             selected_candidate=None, reason_codes=["plan_expired"],
             blockers=["plan_expired"]) for d in stored
    ]
    by_id = {d["source_installation_id"]: d for d in decisions}
    for row in snapshot.get("packages", []):
        row["update_decision"] = by_id.get(_ids(row))
        if fresh and row["update_decision"] and row["update_decision"]["status"] == "ready":
            row["outdated"] = True
    snapshot["update_decisions"] = decisions
    snapshot["unified_plan_id"] = plan["id"]
    snapshot["unified_plan_expires_at"] = expiry
    snapshot["unified_plan_stale"] = not fresh
    return snapshot

def validate_request(s, body, *, all_ready=False):
    if not isinstance(body, dict):
        raise PlanError("invalid_request")
    request_id, plan_id = body.get("request_id"), body.get("plan_id")
    if not isinstance(request_id, str) or not re_full_id(request_id):
        raise PlanError("invalid_request_id")
    plan = read_plan(s, plan_id)
    if not plan:
        raise PlanError("plan_not_found")
    if plan.get("expires_at", 0) <= time.time():
        raise PlanError("plan_expired")
    submitted = body.get("decisions")
    if not isinstance(submitted, list):
        if body.get("decision_id") and body.get("material_fingerprint"):
            submitted = [{"decision_id": body["decision_id"],
                          "material_fingerprint": body["material_fingerprint"]}]
        else:
            raise PlanError("review_required")
    identifiers = [x.get("decision_id") for x in submitted if isinstance(x, dict)]
    if len(identifiers) != len(submitted) or len(identifiers) != len(set(identifiers)):
        raise PlanError("ambiguous_decisions")
    known = {x["decision_id"]: x for x in plan["decisions"]}
    items = []
    for raw in submitted:
        decision = known.get(raw["decision_id"])
        if decision is None or raw.get("material_fingerprint") != decision["material_fingerprint"]:
            raise PlanError("plan_changed")
        if decision["status"] != "ready":
            raise PlanError("decision_not_ready")
        if decision["action"] == "migrate":
            candidate = decision["selected_candidate"] or {}
            if not candidate.get("migration_plan_id") or not candidate.get("migration_fingerprint"):
                raise PlanError("migration_route_unverified")
        items.append(decision)
    if all_ready and set(identifiers) != {
        d["decision_id"] for d in plan["decisions"] if d["status"] == "ready"}:
        raise PlanError("all_ready_mismatch")
    return plan, items


def _request_path(s, request_id):
    return Path(s.DATA_DIR) / "update-requests" / (request_id + ".json")


def _canonical_request(plan_id, items):
    return hashlib.sha256(json.dumps(
        {"plan_id": plan_id, "decisions": sorted(
            (x["decision_id"], x["material_fingerprint"]) for x in items)},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def execute(s, body, *, all_ready=False):
    """Idempotent, fail-closed execution of approved same-provider routes only.

    Caller must hold the cross-process operation lock. A crash leaves a durable
    'needs_recovery' request rather than silently repeating a package mutation.
    """
    request_id = body.get("request_id") if isinstance(body, dict) else None
    path = _request_path(s, request_id) if isinstance(request_id, str) and re_full_id(request_id) else None
    journal = s.read_json(path) if path else None
    if journal and journal.get("status") == "completed":
        supplied = body.get("decisions")
        if supplied is None and body.get("decision_id"):
            supplied = [{"decision_id": body.get("decision_id"),
                         "material_fingerprint": body.get("material_fingerprint")}]
        fingerprint = hashlib.sha256(json.dumps({
            "plan_id": body.get("plan_id"), "decisions": sorted(
                (x.get("decision_id"), x.get("material_fingerprint")) for x in (supplied or []))},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fingerprint != journal.get("request_hash"):
            raise PlanError("request_conflict")
        return journal["result"]
    plan, decisions = validate_request(s, body, all_ready=all_ready)
    signature = _canonical_request(plan["id"], decisions)
    if journal:
        if journal.get("request_hash") != signature:
            raise PlanError("request_conflict")
        if journal.get("status") == "completed":
            return journal["result"]
        raise PlanError("needs_recovery")
    # Before the first mutation, refresh all changed installation metadata.
    current = s.compute_snapshot(False, enrich_details=False)
    rows = {_ids(p): p for p in current.get("packages", [])}
    for decision in decisions:
        item = rows.get(decision["source_installation_id"])
        if not item or item.get("installed_version") != decision["installed_version"]:
            raise PlanError("plan_changed")
        if (item.get("provider") or "homebrew") != decision["current_provider"]:
            raise PlanError("plan_changed")
        if decision["action"] == "migrate":
            import migration_engine
            _, fingerprint = migration_engine._material(s, decision["logical_id"])
            if fingerprint != decision["selected_candidate"]["migration_fingerprint"]:
                raise PlanError("plan_changed")
    journal = {"schema_version": 1, "request_id": request_id, "plan_id": plan["id"],
               "request_hash": signature, "status": "running", "results": [],
               "started_at": s.now_iso()}
    provider_ownership.atomic_write_json(path, journal)
    try:
        for decision in decisions:
            s.raise_if_cancel_requested()
            source = rows[decision["source_installation_id"]]
            provider = decision["target_provider"]
            candidate = decision["selected_candidate"]
            if decision["action"] == "migrate":
                import migration_engine
                _, fingerprint = migration_engine._material(s, decision["logical_id"])
                if fingerprint != candidate["migration_fingerprint"]:
                    raise PlanError("plan_changed")
                transaction = migration_engine.approve(
                    s, candidate["migration_plan_id"], request_id=request_id)
                outcome = {
                    "ok": transaction.get("status") == "completed",
                    "name": source["name"], "kind": source["kind"],
                    "provider": provider, "action": "migrate",
                    "transaction_id": transaction["transaction_id"],
                    "code": 0 if transaction.get("status") == "completed" else 1,
                    "stdout": "Source removed and new provider verified",
                    "stderr": "",
                }
            elif provider != decision["current_provider"]:
                raise PlanError("review_required")
            # Fail closed on changed binary closure; no silent source fallback.
            elif provider == "homebrew":
                catalog, installed = None, None
                from update_engine import Engine
                engine = Engine(s)
                catalog, installed = engine.catalog(), engine.installed()
                renewed = _homebrew(s, source, catalog, installed, s.load_settings())
                if not renewed.get("closure_verified") or renewed.get("closure") != candidate.get("closure"):
                    raise PlanError("plan_changed")
                # Delegate to the same source-fallback detector and binary-only
                # preflight used by legacy updates; do not start a second
                # Homebrew installer without those safeguards.
                outcome = s.run_upgrade(source["name"], source["kind"])
                outcome["provider"] = "homebrew"
            elif provider == "macports":
                latest = _ports(s, source, candidate["provider_names"], "update")
                if (not latest["execution_supported"] or
                    latest["closure"] != candidate["closure"]):
                    raise PlanError("plan_changed")
                outcome = macports_provider.upgrade(s, source)
            elif provider == "pixi":
                outcome = _pixi_update(s, source, candidate)
            else:
                raise PlanError("provider_route_unavailable")
            journal["results"].append(outcome)
            provider_ownership.atomic_write_json(path, journal)
            if not outcome.get("ok"):
                journal["status"] = "needs_recovery"
                provider_ownership.atomic_write_json(path, journal)
                raise PlanError("needs_recovery")
        journal["status"] = "completed"
        journal["completed_at"] = s.now_iso()
        refreshed, refresh_error = s.safe_refresh_snapshot(current)
        refreshed.pop("update_decisions", None)
        refreshed.pop("unified_plan_id", None)
        provider_ownership.atomic_write_json(_plan_dir(s) / "latest.json", {"plan_id": None})
        journal["result"] = {
            "ok": refresh_error is None, "results": journal["results"],
            "updated_count": sum(bool(r.get("ok")) for r in journal["results"]),
            "migrated_count": sum(bool(r.get("ok")) and r.get("action") == "migrate"
                                  for r in journal["results"]),
            "failed_count": sum(not bool(r.get("ok")) for r in journal["results"]),
            "attempted_count": len(journal["results"]),
            "deferred_count": 0, "skipped_count": 0, "remaining_count": 0,
            "snapshot": refreshed, "inventory_refresh_error": refresh_error,
            "request_id": request_id,
        }
        journal["status"] = "completed"
        provider_ownership.atomic_write_json(path, journal)
        s.set_check_progress(running=False, phase="completed", completed_at=s.now_iso(),
                             message="Verified update batch completed", eta_seconds=0)
        return journal["result"]
    except BaseException:
        journal["status"] = "needs_recovery"
        provider_ownership.atomic_write_json(path, journal)
        raise
'''

UNIFIED_UPDATES_TESTS = r'''import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import unified_updates as u
import update_selection as sel


class FakeService:
    def __init__(self, root):
        self.DATA_DIR = Path(root)
        self.TOOLS_ROOT = Path(root)
    def read_json(self, path):
        import json
        return json.loads(Path(path).read_text()) if Path(path).is_file() else None


class Plans(unittest.TestCase):
    def test_stored_plan_and_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            plan = {"id": "a"*32, "expires_at": time.time()-1, "decisions": []}
            from provider_ownership import atomic_write_json
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"), plan)
            self.assertEqual(u.read_plan(s, "a"*32)["id"], "a"*32)
            with self.assertRaisesRegex(u.PlanError, "plan_expired"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": "a"*32,
                                       "decisions": []})

    def test_request_rejects_changed_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            d = {"decision_id": "c"*32, "material_fingerprint": "hash",
                 "status": "ready", "action": "update"}
            from provider_ownership import atomic_write_json
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"),
                              {"id": "a"*32, "expires_at": time.time()+600,
                               "decisions": [d]})
            with self.assertRaisesRegex(u.PlanError, "plan_changed"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": "a"*32,
                                       "decisions": [{"decision_id": "c"*32,
                                                      "material_fingerprint": "wrong"}]})

    def test_legacy_never_authorizes_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            from provider_ownership import atomic_write_json
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"),
                              {"id": "a"*32, "expires_at": time.time()+600,
                               "decisions": []})
            with self.assertRaisesRegex(u.PlanError, "review_required"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": "a"*32,
                                       "name": "include-what-you-use", "kind": "formula"})

    def test_installation_ids_remain_provider_specific(self):
        self.assertEqual(u._ids({"name": "jq", "kind": "port", "provider": "macports"}),
                         "macports:jq")
        self.assertEqual(u._ids({"name": "jq", "kind": "formula", "provider": "homebrew"}),
                         "homebrew:formula:jq")


    def test_cross_provider_discovery_when_brew_is_current(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "include-what-you-use", "kind": "formula",
                   "provider": "homebrew", "installed_version": "1.0",
                   "outdated": False, "classification": {"category": "standalone_candidate"}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"include-what-you-use": {"stable_version": "1.0"}}, {})), \
                 patch.object(u, "_ports", return_value=dict(
                     u._base_candidate(row, "macports", "migrate"),
                     availability="verified", upstream_version="2.0",
                     closure_verified=True, execution_supported=False, blockers=[])), \
                 patch("migration_engine.plan", return_value={"id": "a"*32, "fingerprint": "f"}):
                candidates = u.collect_candidates(s, {"packages": [row]})
            self.assertTrue(row["discovery_complete"])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["provider"], "macports")
            self.assertTrue(candidates[0]["execution_supported"])
            self.assertEqual(candidates[0]["migration_plan_id"], "a"*32)

    def test_unsupported_mapping_never_calls_port_preflight(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "libtensorflow", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": False,
                   "classification": {"category": "standalone_candidate"}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"libtensorflow": {"stable_version": "1.0"}}, {})), \
                 patch.object(u, "_ports", side_effect=AssertionError("unsupported probe")):
                candidates = u.collect_candidates(s, {"packages": [row]})
            self.assertEqual(len(candidates), 1)
            self.assertFalse(candidates[0]["execution_supported"])
            self.assertIn("migration_profile_missing", candidates[0]["blockers"])

    def test_mapped_pixi_candidate_is_discovered_but_not_executable(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": False,
                   "classification": {"category": "standalone_candidate"}}
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"jq": {"stable_version": "1.0"}}, {})), \
                 patch("pixi_provider.discover", return_value={
                     "availability": "verified", "upstream_version": "2.0",
                     "system": {"os": "macos", "arch": "x86_64"},
                     "closure": [{"arch": "osx-64", "archive_id": "verified-fixture"}],
                     "closure_verified": True}):
                candidates = u.collect_candidates(s, {"packages": [row]})
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["provider"], "pixi")
            self.assertFalse(candidates[0]["execution_supported"])
            decision, = sel.select([row], candidates)
            self.assertNotEqual(decision["status"], "ready")
            self.assertIn("pixi_migration_transaction_unavailable",
                          decision["alternatives"][0]["reason_codes"])

    def test_blocked_pixi_does_not_hide_working_homebrew(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            s = FakeService(temp)
            s.load_settings = lambda: {"excluded_packages": []}
            row = {"name": "jq", "kind": "formula", "provider": "homebrew",
                   "installed_version": "1.0", "outdated": True,
                   "classification": {"category": "standalone_candidate"}}
            home = dict(u._base_candidate(row, "homebrew", "update"),
                        availability="verified", upstream_version="1.2",
                        execution_supported=True, closure_verified=True,
                        blockers=[])
            with patch.object(u, "_load_homebrew_catalog", return_value=(
                {"jq": {"stable_version": "1.2"}}, {})), \
                 patch.object(u, "_homebrew", return_value=home), \
                 patch("pixi_provider.discover", return_value={
                     "availability": "verified", "upstream_version": "2.0",
                     "system": {"os": "macos", "arch": "x86_64"},
                     "closure_verified": True, "closure": []}):
                candidates = u.collect_candidates(s, {"packages": [row]})
            selected, = sel.select([row], candidates)
            self.assertEqual(selected["target_provider"], "homebrew")

    def test_expired_cached_plan_cannot_be_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            from provider_ownership import atomic_write_json
            s = FakeService(temp)
            ident = "a" * 32
            decision = {"decision_id": "c"*32, "source_installation_id": "homebrew:formula:jq",
                        "status": "ready", "action": "update", "material_fingerprint": "hash",
                        "selected_candidate": {"provider": "homebrew"}}
            atomic_write_json(Path(temp)/"update-plans"/(ident+".json"),
                              {"id": ident, "expires_at": time.time()-5,
                               "decisions": [decision]})
            atomic_write_json(Path(temp)/"update-plans"/"latest.json", {"plan_id": ident})
            snapshot = {"packages": [{"name": "jq", "kind": "formula", "outdated": True}]}
            u.attach_cached(s, snapshot)
            self.assertTrue(snapshot["unified_plan_stale"])
            self.assertEqual(snapshot["unified_plan_id"], ident)
            self.assertEqual(snapshot["update_decisions"][0]["status"], "unverified")
            self.assertEqual(snapshot["update_decisions"][0]["reason_codes"], ["plan_expired"])
            self.assertIsNone(snapshot["packages"][0]["update_decision"]["selected_candidate"])
            self.assertEqual(u.read_plan(s, ident)["decisions"][0], decision)
            with self.assertRaisesRegex(u.PlanError, "plan_expired"):
                u.validate_request(s, {"request_id": "b"*32, "plan_id": ident,
                                       "decisions": [{"decision_id": "c"*32,
                                                      "material_fingerprint": "hash"}]})

    def test_fresh_cached_plan_remains_ready_with_expiry_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            from provider_ownership import atomic_write_json
            s = FakeService(temp)
            expiry = time.time()+500
            plan = {"id": "a"*32, "expires_at": expiry,
                    "decisions": [{"source_installation_id": "homebrew:formula:jq",
                                   "status": "ready", "action": "update"}]}
            atomic_write_json(Path(temp)/"update-plans"/("a"*32+".json"), plan)
            atomic_write_json(Path(temp)/"update-plans"/"latest.json", {"plan_id": "a"*32})
            snapshot = {"packages": [{"name": "jq", "kind": "formula", "outdated": False}]}
            u.attach_cached(s, snapshot)
            self.assertFalse(snapshot["unified_plan_stale"])
            self.assertEqual(snapshot["unified_plan_expires_at"], expiry)
            self.assertTrue(snapshot["packages"][0]["outdated"])
            self.assertEqual(snapshot["packages"][0]["update_decision"]["status"], "ready")

    def test_missing_cached_plan_clears_historical_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = {"unified_plan_id": "a"*32,
                        "update_decisions": [{"status": "ready"}],
                        "packages": [{"name": "jq", "kind": "formula",
                                      "update_decision": {"status": "ready"}}]}
            u.attach_cached(FakeService(temp), snapshot)
            self.assertTrue(snapshot["unified_plan_stale"])
            self.assertEqual(snapshot["update_decisions"], [])
            self.assertNotIn("update_decision", snapshot["packages"][0])

    def test_initial_state_without_plan_is_unmodified(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = {"packages": [{"name": "jq", "kind": "formula"}]}
            self.assertIs(u.attach_cached(FakeService(temp), snapshot), snapshot)
            self.assertNotIn("unified_plan_stale", snapshot)

    def test_pixi_user_workspace_cannot_be_upgraded(self):
        with tempfile.TemporaryDirectory() as temp:
            from unittest.mock import patch
            service = FakeService(temp)
            service.TOOLS_ROOT = Path(temp)
            service.DATA_DIR = Path(temp)
            row = {"name": "jq", "provider": "pixi", "kind": "tool",
                   "installed_version": "1.2", "installation_id": "pixi:jq"}
            with patch.object(u.update_routes, "read_records",
                              return_value={"jq": {"provider": "pixi",
                                                    "prefix": "/tmp/unmanaged-user-pixi"}}):
                candidate = u._pixi(service, row)
            self.assertFalse(candidate["execution_supported"])
            self.assertIn("user_environment_not_managed", candidate["blockers"])

    def test_pixi_missing_owner_blocks_update(self):
        with tempfile.TemporaryDirectory() as temp:
            from unittest.mock import patch
            service = FakeService(temp)
            row = {"name": "jq", "provider": "pixi", "kind": "tool",
                   "installed_version": "1.2", "installation_id": "pixi:jq"}
            with patch.object(u.update_routes, "read_records", return_value={}):
                candidate = u._pixi(service, row)
            self.assertIn("pixi_ownership_unverified", candidate["blockers"])


if __name__ == "__main__":
    unittest.main()
'''


UNIFIED_PATCH_DATA = {"backend/brew-updates-service.py":{"origin":"fa29ff482a58a7b96231db8d176bc4dc438191cb","target":"2b2cc8daacd444f0564e2a14663cbbb28ba49ddc","hunks":[[2,1,["\n"]],[10,1,["\n"]],[12,1,["\n"]],[35,1,["\n"]],[41,0,["import unified_updates\n","import operation_lock\n","import provider_ownership\n"]],[42,1,["\n"]],[47,1,["\n"]],[60,1,["\n"]],[62,0,["BACKEND_STARTUP_SHA = hashlib.sha256(b''.join(\n","    p.name.encode() + b'\\0' + hashlib.sha256(p.read_bytes()).digest()\n","    for p in sorted((PROJECT_ROOT / 'backend').glob('*.py')))).hexdigest()\n","PROCESS_STARTED_AT = now_start = time.time()\n"]],[69,1,["\n"]],[83,1,["\n"]],[88,1,["\n"]],[92,1,["    \"running\": False,\n"]],[94,3,["    \"started_at\": None,\n","}\n","\n"]],[99,4,["    \"requested\": False,\n","    \"requested_at\": None,\n","}\n","\n"]],[107,2,["}\n","\n"]],[110,1,["\n"]],[113,5,["    \"requested\": False,\n","    \"requested_at\": None,\n","    \"reason\": None,\n","}\n","\n"]],[120,2,["    \"running\": False,\n","    \"started_at\": None,\n"]],[124,4,["    \"reason\": None,\n","}\n","\n","\n"]],[129,1,["    return {\n"]],[150,3,["    }\n","\n","\n"]],[155,1,["\n"]],[157,1,["\n"]],[161,2,["}\n","\n"]],[164,1,["\n"]],[179,2,["}\n","\n"]],[183,2,["\n","\n"]],[190,2,["\n","\n"]],[194,2,["\n","\n"]],[198,2,["\n","\n"]],[202,2,["\n","\n"]],[207,2,["        return None\n","    try:\n"]],[210,2,["    except ValueError:\n","        return None\n"]],[215,2,["\n","\n"]],[218,1,["    with CHECK_PROGRESS_LOCK:\n"]],[220,2,["\n","\n"]],[223,1,["    with CURRENT_OPERATION_LOCK:\n"]],[225,2,["\n","\n"]],[228,1,["    with PARALLEL_CHECK_LOCK:\n"]],[230,2,["\n","\n"]],[235,1,["    with CURRENT_OPERATION_LOCK:\n"]],[239,2,["\n","\n"]],[243,2,["\n","\n"]],[248,1,["\n"]],[250,2,["        return progress\n","\n"]],[253,2,["        return progress\n","\n"]],[258,2,["\n","    set_check_progress(\n"]],[264,1,["            if interrupted\n"]],[266,1,["        ),\n"]],[274,1,["            if interrupted\n"]],[276,2,["        ),\n","    )\n"]],[280,2,["\n","\n"]],[283,1,["    with CANCEL_STATE_LOCK:\n"]],[285,2,["\n","\n"]],[288,1,["    with CANCEL_STATE_LOCK:\n"]],[291,2,["\n","\n"]],[294,1,["    with CANCEL_STATE_LOCK:\n"]],[298,2,["\n","\n"]],[301,1,["    with DEFERRED_CHECK_LOCK:\n"]],[307,4,["            return True\n","        return False\n","\n","\n"]],[312,1,["    with DEFERRED_CHECK_LOCK:\n"]],[318,1,["        }\n"]],[322,3,["        return payload\n","\n","\n"]],[329,1,["\n"]],[331,1,["        try:\n"]],[335,1,["            )\n"]],[338,2,["            return\n","        except BusyError as exc:\n"]],[342,1,["        except Exception as exc:\n"]],[345,2,["            return\n","\n"]],[349,2,["\n","\n"]],[354,2,["        return\n","\n"]],[358,3,["        return\n","\n","    worker = threading.Thread(\n"]],[363,5,["        daemon=True,\n","    )\n","    worker.start()\n","\n","\n"]],[370,1,["\n"]],[373,2,["\n","    try:\n"]],[376,1,["    except Exception as exc:\n"]],[380,1,["    finally:\n"]],[385,1,["\n"]],[389,2,["\n","\n"]],[392,1,["    with PARALLEL_CHECK_LOCK:\n"]],[395,1,["\n"]],[401,1,["\n"]],[403,2,["\n","    worker = threading.Thread(\n"]],[410,3,["        daemon=True,\n","    )\n","    worker.start()\n"]],[414,2,["\n","\n"]],[417,1,["    *,\n"]],[435,2,[") -> None:\n","    with CHECK_PROGRESS_LOCK:\n"]],[456,1,["            else:\n"]],[474,1,["\n"]],[479,1,["        else:\n"]],[483,2,["\n","\n"]],[487,1,["    with CHECK_PROGRESS_LOCK:\n"]],[495,2,["\n","\n"]],[501,1,["\n"]],[504,2,["        return None\n","\n"]],[508,2,["        return None\n","\n"]],[512,2,["\n","\n"]],[521,1,["        return 0\n"]],[523,2,["        return None\n","\n"]],[530,1,["            continue\n"]],[534,1,["\n"]],[536,2,["        return None\n","\n"]],[540,2,["\n","\n"]],[545,1,["        return None\n"]],[548,1,["        return 0\n"]],[550,2,["\n","\n"]],[555,1,["        raise RuntimeError(\n"]],[558,3,["        )\n","\n","\n"]],[565,2,["\n","\n"]],[570,2,["\n","\n"]],[575,1,["\n"]],[580,2,["\n","    try:\n"]],[588,1,["\n"]],[597,2,["\n","    return {\n"]],[605,3,["    }\n","\n","\n"]],[610,3,["        return None\n","\n","    try:\n"]],[614,3,["    except Exception:\n","        return None\n","\n"]],[619,1,["\n"]],[625,1,["\n"]],[628,1,["        try:\n"]],[632,2,["            pass\n","\n"]],[636,1,["            try:\n"]],[641,1,["        else:\n"]],[643,1,["\n"]],[645,2,["\n","\n"]],[648,1,["    ensure_dirs()\n"]],[651,1,["\n"]],[655,1,["        try:\n"]],[660,1,["\n"]],[666,1,["\n"]],[668,2,["\n","\n"]],[671,1,["    ensure_dirs()\n"]],[677,2,["\n","\n"]],[695,2,["\n","\n"]],[699,2,["\n","\n"]],[702,3,["    settings = load_settings()\n","    records = routes.read_records(TOOLS_ROOT)\n","    for pkg in packages:\n"]],[723,3,["    return packages\n","\n","\n"]],[727,1,["    with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):\n"]],[729,2,["\n","\n"]],[733,1,["    if raw is None:\n"]],[736,2,["        return []\n","\n"]],[740,2,["        return []\n","\n"]],[744,2,["        if not isinstance(item, dict):\n","            continue\n"]],[747,1,["            {\n"]],[751,1,["                \"ok\": bool(item.get(\"ok\", False)),\n"]],[756,2,["            }\n","        )\n"]],[759,2,["\n","\n"]],[762,1,["    ensure_dirs()\n"]],[767,1,["        return\n"]],[769,2,["\n","\n"]],[773,2,["        return\n","    with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):\n"]],[778,2,["\n","\n"]],[782,1,["    ensure_dirs()\n"]],[786,3,["            yield\n","            return\n","\n"]],[791,1,["            try:\n"]],[798,3,["\n","        try:\n","            yield\n"]],[803,2,["\n","\n"]],[806,3,["    if not path.exists():\n","        return\n","\n"]],[811,2,["\n","    try:\n"]],[814,1,["        append_service_log(\n"]],[816,3,["        )\n","    except Exception as exc:\n","        append_service_log(\n"]],[820,3,["        )\n","\n","\n"]],[829,2,["    ]\n","\n"]],[836,1,["\n"]],[841,1,["\n"]],[846,1,["    ]\n"]],[848,1,["\n"]],[858,1,["        ]\n"]],[864,1,["        ]\n"]],[867,1,["\n"]],[869,1,["\n"]],[871,1,["    seen: set[str] = set()\n"]],[874,3,["        if not raw:\n","            continue\n","        try:\n"]],[878,1,["        except Exception:\n"]],[881,2,["            continue\n","        seen.add(normalized)\n"]],[884,1,["\n"]],[889,1,["            continue\n"]],[895,2,["            break\n","\n"]],[900,1,["    }\n"]],[903,2,["\n","\n"]],[908,2,["        return \"\"\n","\n"]],[913,1,["\n"]],[917,1,["\n"]],[919,2,["\n","\n"]],[929,1,["        ],\n"]],[935,2,["    }\n","\n"]],[940,2,["        return payload\n","\n"]],[945,1,["    }\n"]],[948,1,["\n"]],[950,3,["    return payload\n","\n","\n"]],[956,2,["\n","\n"]],[960,1,["\n"]],[963,1,["\n"]],[965,1,["        try:\n"]],[968,1,["        except Exception as exc:\n"]],[970,2,["\n","        return {\n"]],[975,3,["        }\n","\n","    try:\n"]],[982,1,["    except Exception as exc:\n"]],[984,1,["\n"]],[991,2,["\n","    return {\n"]],[994,5,["        \"active\": is_launch_agent_loaded(),\n","        \"launch_agent_path\": str(LAUNCH_AGENT_FILE),\n","    }\n","\n","\n"]],[1002,1,["    return {\n"]],[1004,5,["        \"active\": is_launch_agent_loaded(),\n","        \"launch_agent_path\": str(LAUNCH_AGENT_FILE),\n","    }\n","\n","\n"]],[1010,3,["    if not path.exists():\n","        return None\n","    try:\n"]],[1014,4,["    except Exception:\n","        return None\n","\n","\n"]],[1019,6,["    provider_ownership.atomic_write_json(path, payload)\n","\n","\n"]],[1026,1,["    ensure_dirs()\n"]],[1030,2,["\n","\n"]],[1034,2,["\n","\n"]],[1038,2,["\n","\n"]],[1046,1,["    }\n"]],[1055,2,["\n","\n"]],[1059,3,["        return False\n","\n","    try:\n"]],[1063,6,["            return False\n","\n","        used_process_group = False\n","        if hasattr(os, \"killpg\"):\n","            try:\n","                pgid = os.getpgid(proc.pid)\n"]],[1070,5,["                used_process_group = True\n","            except Exception:\n","                used_process_group = False\n","\n","        if not used_process_group:\n"]],[1076,1,["\n"]],[1078,6,["        return True\n","    except subprocess.TimeoutExpired:\n","        used_process_group = False\n","        if hasattr(os, \"killpg\"):\n","            try:\n","                pgid = os.getpgid(proc.pid)\n"]],[1085,5,["                used_process_group = True\n","            except Exception:\n","                used_process_group = False\n","\n","        if not used_process_group:\n"]],[1091,1,["        try:\n"]],[1093,7,["        except Exception:\n","            pass\n","        return True\n","    except Exception:\n","        return False\n","\n","\n"]],[1101,2,["            on_output: Callable[[str, str], None] | None = None,\n","            env_override: dict[str, str] | None = None,\n","            input_data: str | None = None) -> CommandResult:\n"]],[1104,2,["    if tracked_scan:\n","        raise_if_cancel_requested()\n"]],[1107,3,["                           on_output=on_output, env_override=env_override, input_data=input_data)\n","    if tracked_scan:\n","        raise_if_cancel_requested()\n"]],[1111,2,["\n","\n"]],[1114,2,["            on_output: Callable[[str, str], None] | None = None,\n","            env_override: dict[str, str] | None = None,\n","            input_data: str | None = None) -> CommandResult:\n","    if input_data is not None and on_output is not None:\n","        raise ValueError(\"Streaming progress and explicit stdin cannot be combined\")\n"]],[1118,0,["        stdin=subprocess.PIPE if input_data is not None else None,\n"]],[1123,2,["    )\n","\n"]],[1129,2,["\n","    try:\n"]],[1134,1,["\n"]],[1136,1,["                try:\n"]],[1145,1,["\n"]],[1148,1,["            for reader in readers:\n"]],[1151,1,["            try:\n"]],[1156,1,["            for reader in readers:\n"]],[1163,1,["        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)\n"]],[1165,1,["    except subprocess.TimeoutExpired:\n"]],[1172,1,["        else:\n"]],[1175,1,["    finally:\n"]],[1177,1,["            with ACTIVE_COMMAND_LOCK:\n"]],[1181,2,["\n","\n"]],[1184,1,["    settings = load_settings()\n"]],[1187,1,["        raise RuntimeError(\n"]],[1189,3,["        )\n","\n","    try:\n"]],[1194,1,["        raise RuntimeError(\n"]],[1197,2,["\n","\n"]],[1202,2,["\n","\n"]],[1209,1,["\n"]],[1212,1,["                \"ok\": True,\n"]],[1218,3,["            }\n","\n","        return {\n"]],[1226,2,["        }\n","\n"]],[1229,1,["\n"]],[1233,1,["\n"]],[1235,1,["\n"]],[1241,1,["    else:\n"]],[1244,2,["\n","    set_check_progress(\n"]],[1248,3,["    )\n","\n","    return {\n"]],[1256,3,["    }\n","\n","\n"]],[1262,1,["        try:\n"]],[1266,1,["                               current_package=None, eta_seconds=None, completed_at=now_iso())\n"]],[1271,1,["                               current_package=None, eta_seconds=None, completed_at=now_iso())\n"]],[1276,1,["        with CHECK_PROGRESS_LOCK:\n"]],[1280,2,["\n","\n"]],[1287,2,["        )\n","\n"]],[1290,1,["    with CURRENT_OPERATION_LOCK:\n"]],[1294,1,["\n"]],[1298,1,["    try:\n","        if op_name.startswith((\"update\", \"migration\", \"macports\")) and op_name != \"update_plan\":\n","            with operation_lock.OperationLock(TOOLS_ROOT, op_name):\n","                return fn()\n"]],[1300,1,["    except OperationCancelledError:\n"]],[1303,2,["        raise\n","    finally:\n"]],[1313,2,["\n","\n"]],[1316,2,["    if raw is None:\n","        return []\n"]],[1330,3,["    return []\n","\n","\n"]],[1334,1,["    b = brew_bin()\n"]],[1336,1,["    if res.code != 0:\n"]],[1339,2,["\n","    try:\n"]],[1345,1,["\n"]],[1347,7,["\n","    for row in payload.get(\"formulae\", []) or []:\n","        if not isinstance(row, dict):\n","            continue\n","        name = str(row.get(\"name\") or \"\").strip()\n","        if not name:\n","            continue\n"]],[1356,7,["            \"installed_version\": installed_versions[-1] if installed_versions else \"unknown\",\n","            \"latest_version\": str(row.get(\"current_version\") or row.get(\"version\") or \"unknown\"),\n","        }\n","\n","    for row in payload.get(\"casks\", []) or []:\n","        if not isinstance(row, dict):\n","            continue\n"]],[1364,2,["        if not token:\n","            continue\n"]],[1368,4,["            \"installed_version\": installed_versions[-1] if installed_versions else \"unknown\",\n","            \"latest_version\": str(row.get(\"current_version\") or row.get(\"version\") or \"unknown\"),\n","        }\n","\n"]],[1373,2,["\n","\n"]],[1376,1,["    b = brew_bin()\n"]],[1378,1,["    if res.code != 0:\n"]],[1380,1,["\n"]],[1382,1,["    try:\n"]],[1385,2,["    except OperationCancelledError:\n","        raise\n"]],[1392,9,["\n","    for row in payload.get(\"formulae\", []) or []:\n","        if not isinstance(row, dict):\n","            continue\n","\n","        name = str(row.get(\"name\") or \"\").strip()\n","        if not name:\n","            continue\n","\n"]],[1406,1,["\n"]],[1408,2,["        outdated = bool(outdated_meta) or bool(row.get(\"outdated\", False))\n","        if outdated_meta:\n"]],[1411,5,["            latest_version = outdated_meta.get(\"latest_version\") or latest_version\n","\n","        packages.append(\n","            {\n","                \"name\": name,\n"]],[1420,14,["                \"description_he\": \"\",\n","                \"homepage\": str(row.get(\"homepage\") or \"\"),\n","                \"installed_version\": installed_version,\n","                \"latest_version\": latest_version,\n","                \"installed_versions\": installed_versions,\n","                \"outdated\": outdated,\n","                \"current_release_date\": None,\n","                \"current_release_date_source\": None,\n","                \"latest_release_date\": None,\n","                \"latest_release_date_source\": None,\n","                \"outdated_since\": None,\n","                \"days_since_outdated\": None,\n","                \"release_date\": None,\n","                \"release_date_source\": None,\n"]],[1435,8,["                \"ruby_source_path\": str(row.get(\"ruby_source_path\") or \"\"),\n","            }\n","        )\n","\n","    for row in payload.get(\"casks\", []) or []:\n","        if not isinstance(row, dict):\n","            continue\n","\n"]],[1444,3,["        if not token:\n","            continue\n","\n"]],[1450,1,["\n"]],[1452,2,["        outdated = bool(outdated_meta) or bool(row.get(\"outdated\", False))\n","        if outdated_meta:\n"]],[1455,2,["            latest_version = outdated_meta.get(\"latest_version\") or latest_version\n","\n"]],[1460,1,["        else:\n"]],[1462,3,["\n","        packages.append(\n","            {\n"]],[1469,14,["                \"description_he\": \"\",\n","                \"homepage\": str(row.get(\"homepage\") or \"\"),\n","                \"installed_version\": installed_version,\n","                \"latest_version\": latest_version,\n","                \"installed_versions\": installed_versions,\n","                \"outdated\": outdated,\n","                \"current_release_date\": None,\n","                \"current_release_date_source\": None,\n","                \"latest_release_date\": None,\n","                \"latest_release_date_source\": None,\n","                \"outdated_since\": None,\n","                \"days_since_outdated\": None,\n","                \"release_date\": None,\n","                \"release_date_source\": None,\n"]],[1484,4,["                \"ruby_source_path\": str(row.get(\"ruby_source_path\") or \"\"),\n","            }\n","        )\n","\n"]],[1491,5,["            continue\n","        packages.append(\n","            {\n","                \"name\": name,\n","                \"kind\": kind,\n"]],[1498,1,["                \"description_he\": \"\",\n"]],[1504,8,["                \"current_release_date\": None,\n","                \"current_release_date_source\": None,\n","                \"latest_release_date\": None,\n","                \"latest_release_date_source\": None,\n","                \"outdated_since\": None,\n","                \"days_since_outdated\": None,\n","                \"release_date\": None,\n","                \"release_date_source\": None,\n"]],[1514,3,["            }\n","        )\n","\n"]],[1519,1,["    records = routes.read_records(TOOLS_ROOT)\n"]],[1523,1,["        for name in missing:\n"]],[1529,1,["        for name in missing:\n"]],[1545,3,["    return packages\n","\n","\n"]],[1553,2,["\n","\n"]],[1558,1,["        return None\n"]],[1561,2,["\n","    b = brew_bin()\n"]],[1564,3,["    if res.code != 0 or not res.stdout:\n","        return None\n","\n"]],[1568,3,["    if not path.exists():\n","        return None\n","\n"]],[1573,2,["\n","\n"]],[1576,2,["    ruby_source_path = str(pkg.get(\"ruby_source_path\") or \"\").strip()\n","    if ruby_source_path:\n"]],[1582,4,["        if candidate.exists():\n","            return candidate\n","\n","    name = str(pkg.get(\"name\") or \"\").strip()\n"]],[1587,2,["        return None\n","\n"]],[1594,1,["        ]\n"]],[1596,3,["            if candidate.exists():\n","                return candidate\n","\n"]],[1600,4,["            if candidate.exists():\n","                return candidate\n","        return None\n","\n"]],[1607,1,["    ]\n"]],[1609,3,["        if candidate.exists():\n","            return candidate\n","\n"]],[1613,6,["        if candidate.exists():\n","            return candidate\n","\n","    return None\n","\n","\n"]],[1621,2,["        return None\n","\n"]],[1624,5,["    if res.code != 0 or not res.stdout:\n","        return None\n","    return res.stdout.splitlines()[0].strip()\n","\n","\n"]],[1630,1,["    try:\n"]],[1632,4,["    except Exception:\n","        return None\n","\n","\n"]],[1638,2,["    if not source:\n","        return False\n"]],[1642,2,["\n","\n"]],[1646,2,["        return False\n","    res = run_cmd(\n"]],[1650,3,["    )\n","    if res.code != 0:\n","        return False\n"]],[1654,2,["\n","\n"]],[1658,3,["        return []\n","    res = run_cmd(\n","        [\n"]],[1670,1,["        ],\n"]],[1672,4,["    )\n","    if res.code != 0 or not res.stdout:\n","        return []\n","\n"]],[1679,1,["            continue\n"]],[1685,1,["\n"]],[1689,2,["\n","\n"]],[1694,2,["        return None\n","\n"]],[1701,1,["\n"]],[1703,1,["    res = run_cmd(\n"]],[1706,6,["    )\n","    if res.code != 0 or not res.stdout:\n","        return None\n","    return res.stdout.splitlines()[0].strip()\n","\n","\n"]],[1717,1,["    )\n"]],[1723,3,["    return []\n","\n","\n"]],[1727,1,["    pkg: dict[str, Any],\n"]],[1731,1,["    name = str(pkg.get(\"name\") or \"\").strip()\n"]],[1733,1,["\n"]],[1735,3,["\n","    ruby_source_path = str(pkg.get(\"ruby_source_path\") or \"\").strip()\n","    if ruby_source_path:\n"]],[1739,1,["\n"]],[1744,1,["\n"]],[1747,2,["            candidates.extend(\n","                [\n"]],[1751,2,["                ]\n","            )\n"]],[1754,2,["            candidates.extend(\n","                [\n"]],[1758,3,["                ]\n","            )\n","\n"]],[1762,1,["    seen: set[str] = set()\n"]],[1766,2,["            continue\n","        seen.add(normalized)\n"]],[1769,1,["\n"]],[1771,2,["\n","\n"]],[1778,1,[") -> tuple[str | None, str, str | None, str]:\n"]],[1781,2,["        return None, \"unavailable\", None, \"unavailable\"\n","\n"]],[1785,1,["\n"]],[1787,1,["        try:\n"]],[1789,1,["        except Exception as exc:\n"]],[1791,2,["            continue\n","\n"]],[1796,2,["            break\n","\n"]],[1799,2,["        return None, \"unavailable\", None, \"unavailable\"\n","\n"]],[1804,1,["\n"]],[1807,1,["\n"]],[1810,1,["\n"]],[1822,2,["                    )\n","\n"]],[1825,2,["        return (\n","            latest_date,\n"]],[1828,1,["            latest_date,\n"]],[1830,2,["        )\n","\n"]],[1833,2,["\n","\n"]],[1837,2,["\n","\n"]],[1841,2,["\n","\n"]],[1845,1,["        [\n"]],[1848,2,["            str(pkg.get(\"installed_version\") or \"\"),\n","            str(pkg.get(\"latest_version\") or \"\"),\n"]],[1851,4,["        ]\n","    )\n","\n","\n"]],[1857,2,["        return False\n","\n"]],[1861,3,["        return False\n","\n","    try:\n"]],[1865,3,["    except ValueError:\n","        return False\n","\n"]],[1870,1,["\n"]],[1873,2,["\n","\n"]],[1876,1,["    pkg: dict[str, Any],\n"]],[1879,1,[") -> tuple[str | None, str, str | None, str]:\n"]],[1883,1,["        return (\n"]],[1888,2,["        )\n","\n"]],[1892,1,["\n"]],[1898,1,["\n"]],[1905,1,["\n"]],[1910,1,["\n"]],[1914,1,["\n"]],[1920,3,["            str(pkg.get(\"installed_version\") or \"\"),\n","            str(pkg.get(\"latest_version\") or \"\"),\n","        )\n"]],[1929,1,["\n"]],[1931,1,["        current_release_date = latest_release_date\n"]],[1934,1,["\n"]],[1936,1,["        current_release_date = latest_release_date\n"]],[1938,1,["\n"]],[1945,2,["    }\n","\n"]],[1948,2,["\n","\n"]],[1951,3,["    packages: list[dict[str, Any]],\n","    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,\n",") -> None:\n"]],[1956,1,["\n"]],[1958,2,["    for pkg in packages:\n","        if pkg.get(\"outdated\"):\n"]],[1961,1,["\n"]],[1963,4,["\n","    total = len(packages)\n","\n","    for index, pkg in enumerate(packages, start=1):\n"]],[1969,1,["\n"]],[1974,1,["        )\n"]],[1979,2,["\n","        if pkg.get(\"outdated\"):\n"]],[1983,1,["        else:\n"]],[1986,1,["\n"]],[1991,4,["\n","        if progress_callback:\n","            progress_callback(index, total, pkg)\n","\n"]],[1997,2,["\n","\n"]],[2001,4,["    if not source:\n","        return \"\"\n","\n","    try:\n"]],[2008,1,["        )\n"]],[2012,1,["\n"]],[2019,1,["\n"]],[2022,1,["    except Exception as exc:\n"]],[2024,3,["        return None\n","\n","\n"]],[2028,3,["    packages: list[dict[str, Any]],\n","    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,\n",") -> None:\n"]],[2034,1,["\n"]],[2037,4,["\n","    total = len(packages)\n","\n","    for index, pkg in enumerate(packages, start=1):\n"]],[2044,4,["            if progress_callback:\n","                progress_callback(index, total, pkg)\n","            continue\n","\n"]],[2051,4,["            if progress_callback:\n","                progress_callback(index, total, pkg)\n","            continue\n","\n"]],[2056,5,["            pkg[\"description_he\"] = desc_en\n","            if progress_callback:\n","                progress_callback(index, total, pkg)\n","            continue\n","\n"]],[2066,3,["        else:\n","            pkg[\"description_he\"] = desc_en\n","\n"]],[2070,4,["\n","        if progress_callback:\n","            progress_callback(index, total, pkg)\n","\n"]],[2076,2,["\n","\n"]],[2081,2,["\n","\n"]],[2087,1,["    return {\n"]],[2098,3,["    }\n","\n","\n"]],[2104,1,["    for pkg in packages:\n"]],[2110,2,["                if key in old:\n","                    pkg[key] = old[key]\n"]],[2114,2,["                if key in old:\n","                    pkg[key] = old[key]\n"]],[2118,2,["                if key in old:\n","                    pkg[key] = old[key]\n"]],[2125,2,["\n","\n"]],[2130,1,["\n"]],[2134,1,["\n"]],[2136,4,["\n","    b = brew_bin()\n","\n","    try:\n"]],[2141,1,["            if track_progress:\n"]],[2149,2,["                )\n","\n"]],[2157,1,["            else:\n"]],[2159,2,["\n","            if track_progress:\n"]],[2162,3,["\n","        if track_progress:\n","            set_check_progress(\n"]],[2167,3,["                done=0,\n","                total=1,\n","                eta_seconds=None,\n"]],[2171,2,["            )\n","\n"]],[2174,4,["\n","        if track_progress:\n","            set_check_progress(done=1, total=1, eta_seconds=0)\n","            set_check_progress(\n"]],[2180,3,["                done=0,\n","                total=1,\n","                eta_seconds=None,\n"]],[2184,2,["            )\n","\n"]],[2187,4,["\n","        if track_progress:\n","            set_check_progress(done=1, total=1, eta_seconds=0)\n","\n"]],[2192,1,["\n"]],[2194,7,["            if not track_progress:\n","                return\n","            raise_if_cancel_requested()\n","            eta = estimate_eta_seconds(progress_started_at, done, total)\n","            current = f\"{pkg.get('kind')}:{pkg.get('name')}\"\n","            set_check_progress(\n","                phase=\"resolving_dates\",\n"]],[2202,9,["                done=done,\n","                total=total,\n","                eta_seconds=eta,\n","                current_package=current,\n","            )\n","\n","        if track_progress:\n","            set_check_progress(\n","                phase=\"resolving_dates\",\n"]],[2212,7,["                done=0,\n","                total=total_packages,\n","                eta_seconds=None,\n","                current_package=\"\",\n","            )\n","\n","        if enrich_details:\n"]],[2220,1,["        else:\n"]],[2222,1,["\n"]],[2224,7,["            if not track_progress:\n","                return\n","            raise_if_cancel_requested()\n","            eta = estimate_eta_seconds(progress_started_at, done, total)\n","            current = f\"{pkg.get('kind')}:{pkg.get('name')}\"\n","            set_check_progress(\n","                phase=\"translating_descriptions\",\n"]],[2232,9,["                done=done,\n","                total=total,\n","                eta_seconds=eta,\n","                current_package=current,\n","            )\n","\n","        if track_progress:\n","            set_check_progress(\n","                phase=\"translating_descriptions\",\n"]],[2242,7,["                done=0,\n","                total=total_packages,\n","                eta_seconds=None,\n","                current_package=\"\",\n","            )\n","\n","        if enrich_details:\n"]],[2250,1,["\n"]],[2253,1,["\n"]],[2260,2,["        }\n","\n"]],[2265,5,["        )\n","\n","        if track_progress and complete_progress:\n","            set_check_progress(\n","                running=False,\n"]],[2273,3,["                total=total_packages,\n","                eta_seconds=0,\n","                current_package=\"\",\n"]],[2277,3,["                completed_at=now_iso(),\n","            )\n","\n"]],[2281,4,["    except Exception as exc:\n","        if track_progress and complete_progress:\n","            set_check_progress(\n","                running=False,\n"]],[2287,1,["                eta_seconds=0,\n"]],[2289,5,["                completed_at=now_iso(),\n","            )\n","        raise\n","\n","\n"]],[2297,1,["\n"]],[2301,1,["\n"]],[2304,1,["\n"]],[2306,1,["        if not isinstance(item, dict):\n"]],[2308,4,["\n","        name = str(item.get(\"name\") or \"\").strip()\n","        kind = str(item.get(\"kind\") or \"\").strip()\n","\n"]],[2315,1,["\n"]],[2318,2,["            continue\n","\n"]],[2322,1,["\n"]],[2325,1,["\n"]],[2327,2,["\n","\n"]],[2332,1,["    return {\n"]],[2336,3,["    }\n","\n","\n"]],[2342,1,["        return \"\"\n"]],[2346,2,["\n","\n"]],[2352,2,["        \"name\": name,\n","        \"kind\": kind,\n"]],[2363,1,["    }\n"]],[2365,1,["\n"]],[2368,1,["\n"]],[2377,1,["        }\n"]],[2379,1,["\n"]],[2389,1,["                    continue\n"]],[2393,1,["                    continue\n"]],[2395,1,["                try:\n"]],[2406,1,["                            self.state[\"stage\"] = \"building\"\n"]],[2409,1,["                            self.state[\"stage\"] = \"building\"\n"]],[2412,1,["                    continue\n"]],[2415,1,["\n"]],[2420,1,["\n"]],[2422,1,["        with CHECK_PROGRESS_LOCK:\n"]],[2425,2,["            }\n","\n"]],[2430,1,["            return\n"]],[2440,1,["                stage = \"downloading\"\n"]],[2442,1,["                stage = \"finishing\"\n"]],[2446,1,["                stage = \"downloading\"\n"]],[2456,1,["                stage = \"finishing\"\n"]],[2462,2,["\n","\n"]],[2473,2,["        pkg = package_from_snapshot(state, name, kind)\n","        if not pkg:\n"]],[2476,1,["        item = engine.plan([pkg])[0]\n"]],[2482,1,["        if not pkg:\n"]],[2485,2,["    settings = load_settings()\n","    from update_engine import Engine\n"]],[2490,2,["        pkg = package_from_snapshot(state, name, kind)\n","        if not pkg:\n"]],[2493,1,["        item = engine.plan([pkg])[0]\n"]],[2498,1,["    if kind == \"formula\":\n"]],[2503,4,["    b = brew_bin()\n","\n","    if kind == \"formula\":\n","        cmd = [b, \"upgrade\", \"--force-bottle\", name]\n"]],[2509,1,["    else:\n"]],[2511,1,["\n"]],[2517,1,["\n"]],[2521,1,["        if activity.state[\"stage\"] == \"building\":\n"]],[2523,1,["            with ACTIVE_COMMAND_LOCK:\n"]],[2526,2,["\n","    try:\n"]],[2529,2,["                      on_output=observe, env_override={\n","                          **engine.brew_env,\n","                          \"HOMEBREW_NO_AUTO_UPDATE\": \"1\",\n","                          \"HOMEBREW_NO_AUTOREMOVE\": \"1\",\n","                          \"HOMEBREW_NO_INSTALL_CLEANUP\": \"1\",\n","                      })\n","    finally:\n"]],[2532,1,["        with CHECK_PROGRESS_LOCK:\n"]],[2540,1,["\n"]],[2545,1,["    else:\n"]],[2547,2,["\n","    return {\n"]],[2551,2,["        \"name\": name,\n","        \"kind\": kind,\n"]],[2557,3,["    }\n","\n","\n"]],[2564,3,["    return None\n","\n","\n"]],[2570,1,["    *,\n"]],[2576,2,["        name = str(item.get(\"name\") or \"\").strip()\n","        kind = str(item.get(\"kind\") or \"\").strip()\n"]],[2579,2,["            continue\n","\n"]],[2586,2,["        )\n","\n"]],[2591,1,["\n"]],[2593,1,["            {\n"]],[2595,3,["                \"name\": name,\n","                \"kind\": kind,\n","                \"ok\": bool(item.get(\"ok\", False)),\n"]],[2599,2,["                \"installed_version\": installed_version,\n","                \"latest_version\": latest_version,\n"]],[2602,3,["            }\n","        )\n","\n"]],[2606,2,["\n","\n"]],[2610,1,["\n"]],[2615,1,["\n"]],[2620,1,["\n"]],[2625,1,["\n"]],[2628,3,["    return payload\n","\n","\n"]],[2632,1,["    try:\n"]],[2635,1,["    except Exception as exc:\n"]],[2640,2,["\n","\n"]],[2643,1,["    from update_engine import Engine\n"]],[2645,2,["\n","\n"]],[2648,1,["    from update_engine import Engine\n"]],[2650,2,["\n","\n"]],[2654,2,["\n","\n"]],[2659,1,["        title = \"Mac Update Manager\"\n"]],[2661,1,["    else:\n"]],[2667,1,["\n"]],[2669,1,["        title = \"Mac Update Manager\"\n"]],[2677,1,["\n"]],[2684,1,["\n"]],[2699,3,["\n","\n","\n"]],[2704,1,["\n"]],[2707,1,["\n"]],[2715,1,["\n"]],[2719,1,["            return {}\n"]],[2721,3,["        if not raw:\n","            return {}\n","        try:\n"]],[2726,2,["            return {}\n","\n"]],[2730,1,["            return False\n"]],[2733,1,["            return True\n"]],[2739,1,["            try:\n"]],[2745,2,["                    identifier = body.get(\"id\")\n","                    approved = mac_manager._migration_engine._load_plan(sys.modules[__name__], identifier)\n","                    if approved.get(\"action\") not in (\"rollback\", \"resume\"):\n","                        raise unified_updates.PlanError(\"review_required\", \"Use the unified update list to approve new migrations\")\n","                    result = mac_manager._migration_engine.approve(sys.modules[__name__], identifier,\n","                                                                    request_id=body.get(\"request_id\"))\n","                    return {\"ok\": True, \"migration\": result,\n","                            \"snapshot\": compute_snapshot(False, enrich_details=False)}\n"]],[2758,1,["        try:\n"]],[2760,1,["        except BusyError as exc:\n"]],[2762,1,["        except unified_updates.PlanError as exc:\n","            self._send_json(409, {\"ok\": False, \"error\": exc.code, \"detail\": str(exc)})\n","        except Exception as exc:\n"]],[2764,2,["        return True\n","\n","    def _apply_unified_request(self, body, *, all_ready=False):\n","        try:\n","            def operation():\n","                reset_check_progress_for_run()\n","                set_check_progress(operation_name=\"unified_update\", phase=\"preflight\",\n","                                   message=\"Validating displayed provider decisions\",\n","                                   running=True, eta_seconds=None)\n","                return unified_updates.execute(sys.modules[__name__], body, all_ready=all_ready)\n","            response = run_with_progress(\"update_unified\", operation)\n","            self._send_json(200, response)\n","        except unified_updates.PlanError as exc:\n","            code = 400 if exc.code in {\"invalid_request\", \"invalid_request_id\", \"ambiguous_decisions\"} else 409\n","            self._send_json(code, {\"ok\": False, \"error\": exc.code, \"detail\": str(exc),\n","                                   \"plan\": unified_updates.read_plan(sys.modules[__name__])})\n","        except (BusyError, operation_lock.OperationBusy) as exc:\n","            self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc)})\n","        except Exception as exc:\n","            self._send_json(500, {\"ok\": False, \"error\": \"update_failed\", \"detail\": str(exc)})\n","        return True\n","\n"]],[2768,1,["            operation = read_current_operation()\n"]],[2777,1,["                    \"version\": \"1.3.4\",\n"]],[2780,2,["                    \"capabilities\": [\"provider-decisions-v1\", \"migration-recovery-v2\", \"batch-idempotency-v1\"],\n","                    \"backend_sha256\": BACKEND_STARTUP_SHA,\n","                    \"backend_code_sha256\": BACKEND_STARTUP_SHA,\n","                    \"process_started_at\": PROCESS_STARTED_AT,\n","                    \"build_id\": \"unified-intel-1.3.4\",\n"]],[2783,3,["            )\n","            return\n","\n"]],[2787,6,["            try:\n","                settings = load_settings()\n","                self._send_json(\n","                    200,\n","                    {\n","                        \"ok\": True,\n"]],[2797,3,["                    },\n","                )\n","            except Exception as exc:\n"]],[2801,2,["            return\n","\n"]],[2804,2,["            try:\n","                settings = load_settings()\n"]],[2807,3,["                self._send_json(\n","                    200,\n","                    {\n"]],[2816,3,["                    },\n","                )\n","            except Exception as exc:\n"]],[2820,2,["            return\n","\n"]],[2825,1,["                try:\n"]],[2829,1,["                    return\n"]],[2832,1,["                    return\n"]],[2841,1,["                    continue\n"]],[2849,0,["            unified_updates.attach_cached(sys.modules[__name__], state)\n","            transactions = DATA_DIR / \"migrations\" / \"transactions\"\n","            state[\"pending_recovery\"] = [\n","                {\"transaction_id\": row.get(\"transaction_id\"),\n","                 \"status\": row.get(\"status\"), \"stage\": row.get(\"stage\"),\n","                 \"logical_id\": (row.get(\"plan\") or {}).get(\"profile\")}\n","                for path in sorted(transactions.glob(\"*/journal.json\"))\n","                if (row := read_json(path)) is not None\n","                and row.get(\"status\") not in (\"completed\", \"rolled_back\", \"canceled\")\n","            ] if transactions.is_dir() else []\n"]],[2850,2,["            return\n","\n","        if self.path == \"/api/update-plan\":\n","            self._send_json(200, {\"ok\": True, \"plan\": unified_updates.read_plan(sys.modules[__name__])})\n","            return\n","\n","        if self.path.startswith(\"/api/update-requests/\"):\n","            request_id = self.path.removeprefix(\"/api/update-requests/\")\n","            if not unified_updates.re_full_id(request_id):\n","                self._send_json(400, {\"ok\": False, \"error\": \"invalid_request_id\"})\n","                return\n","            path = DATA_DIR / \"update-requests\" / (request_id + \".json\")\n","            journal = read_json(path)\n","            if journal:\n","                transactions = []\n","                for txn_path in (DATA_DIR / \"migrations\" / \"transactions\").glob(\"*/journal.json\"):\n","                    txn = read_json(txn_path) or {}\n","                    if txn.get(\"request_id\") == request_id:\n","                        transactions.append({\"transaction_id\": txn.get(\"transaction_id\"),\n","                                             \"status\": txn.get(\"status\"),\n","                                             \"stage\": txn.get(\"stage\")})\n","                self._send_json(200, {\"ok\": True, \"request\": journal,\n","                                      \"transactions\": transactions})\n","            else:\n","                self._send_json(404, {\"ok\": False, \"error\": \"request_not_found\"})\n","            return\n","\n"]],[2854,2,["            return\n","\n"]],[2857,5,["            try:\n","                self._send_json(\n","                    200,\n","                    {\n","                        \"ok\": True,\n"]],[2863,3,["                    },\n","                )\n","            except Exception as exc:\n"]],[2867,4,["            return\n","\n","        self._send_json(404, {\"ok\": False, \"error\": \"not_found\"})\n","\n"]],[2873,1,["            return\n"]],[2875,1,["            try:\n"]],[2879,1,["                settings = load_settings()\n"]],[2881,1,["                save_settings(settings)\n"]],[2885,5,["            except BusyError as exc:\n","                self._send_json(409, {\"ok\": False, \"detail\": str(exc)})\n","            return\n","        if self.path == \"/api/update-plan\":\n","            try:\n"]],[2892,1,["                    reset_check_progress_for_run()\n"]],[2900,3,["            except BusyError as exc:\n","                self._send_json(409, {\"ok\": False, \"detail\": str(exc)})\n","            except Exception as exc:\n"]],[2908,1,["            return\n"]],[2912,2,["                self._send_json(200, payload)\n","            else:\n"]],[2915,2,["            return\n","\n"]],[2918,1,["            body = self._read_json_body()\n"]],[2920,2,["\n","            try:\n"]],[2923,1,["                settings = load_settings()\n"]],[2925,1,["                save_settings(settings)\n"]],[2927,5,["\n","                self._send_json(\n","                    200,\n","                    {\n","                        \"ok\": True,\n"]],[2933,3,["                    },\n","                )\n","            except Exception as exc:\n"]],[2937,2,["            return\n","\n"]],[2940,1,["            body = self._read_json_body()\n"]],[2942,2,["\n","            try:\n"]],[2945,1,["                settings = load_settings()\n"]],[2947,2,["                save_settings(settings)\n","\n"]],[2950,4,["                self._send_json(\n","                    200,\n","                    {\n","                        \"ok\": True,\n"]],[2956,3,["                    },\n","                )\n","            except Exception as exc:\n"]],[2960,2,["            return\n","\n"]],[2963,1,["            operation = read_current_operation()\n"]],[2965,1,["\n"]],[2970,1,["                )\n"]],[2974,5,["                self._send_json(\n","                    202,\n","                    {\n","                        \"ok\": True,\n","                        \"parallel\": True,\n"]],[2981,1,["                        \"operation\": operation,\n"]],[2983,4,["                    },\n","                )\n","                return\n","\n"]],[2989,5,["                self._send_json(\n","                    202,\n","                    {\n","                        \"ok\": True,\n","                        \"parallel\": True,\n"]],[2996,1,["                        \"operation\": operation,\n"]],[2998,6,["                    },\n","                )\n","                return\n","\n","            try:\n","                payload = run_with_progress(\n"]],[3006,4,["                )\n","                self._send_json(200, payload)\n","            except BusyError as exc:\n","                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n"]],[3012,1,["            except Exception as exc:\n"]],[3014,2,["            return\n","\n"]],[3017,1,["            body = self._read_json_body()\n","            if body.get(\"plan_id\"):\n","                return self._apply_unified_request(body)\n"]],[3020,2,["\n","            try:\n"]],[3023,1,["\n"]],[3028,2,["                    reset_check_progress_for_run()\n","                    set_check_progress(\n"]],[3034,1,["                        total=1,\n"]],[3036,4,["                        current_package=f\"{kind}:{name}\",\n","                        planned_packages=planned,\n","                        completed_packages=completed,\n","                        failed_packages=failed,\n"]],[3044,2,["                    )\n","\n"]],[3052,1,["\n"]],[3054,2,["\n","                    set_check_progress(\n"]],[3057,1,["                        total=1,\n"]],[3059,9,["                        current_package=f\"{kind}:{name}\",\n","                        completed_packages=completed,\n","                        failed_packages=failed,\n","                        remaining_packages=[],\n","                        cancel_requested=is_cancel_requested(),\n","                        canceled=cancelled,\n","                    )\n","\n","                    set_check_progress(\n"]],[3071,8,["                        planned_packages=planned,\n","                        completed_packages=completed,\n","                        failed_packages=failed,\n","                        remaining_packages=[],\n","                        cancel_requested=is_cancel_requested(),\n","                        canceled=cancelled,\n","                    )\n","\n"]],[3080,1,["\n"]],[3086,1,["                    )\n"]],[3088,1,["\n"]],[3093,2,["                        set_check_progress(\n","                            running=False,\n"]],[3097,13,["                            done=1,\n","                            total=1,\n","                            eta_seconds=0,\n","                            current_package=\"\",\n","                            planned_packages=planned,\n","                            completed_packages=completed,\n","                            failed_packages=failed,\n","                            remaining_packages=[],\n","                            cancel_requested=is_cancel_requested(),\n","                            canceled=False,\n","                            error=None,\n","                            completed_at=now_iso(),\n","                        )\n"]],[3111,2,["                        set_check_progress(\n","                            running=False,\n"]],[3115,9,["                            done=1,\n","                            total=1,\n","                            eta_seconds=0,\n","                            current_package=\"\",\n","                            planned_packages=planned,\n","                            completed_packages=completed,\n","                            failed_packages=failed,\n","                            remaining_packages=[],\n","                            cancel_requested=is_cancel_requested(),\n"]],[3125,3,["                            error=None,\n","                            completed_at=now_iso(),\n","                        )\n"]],[3129,2,["                        set_check_progress(\n","                            running=False,\n"]],[3133,10,["                            done=1,\n","                            total=1,\n","                            eta_seconds=0,\n","                            current_package=\"\",\n","                            planned_packages=planned,\n","                            completed_packages=completed,\n","                            failed_packages=failed,\n","                            remaining_packages=[],\n","                            cancel_requested=is_cancel_requested(),\n","                            canceled=False,\n"]],[3144,3,["                            completed_at=now_iso(),\n","                        )\n","\n"]],[3152,1,["\n"]],[3155,3,["            except BusyError as exc:\n","                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n","            except Exception as exc:\n"]],[3159,2,["            return\n","\n"]],[3162,1,["            body = self._read_json_body()\n","            if body.get(\"plan_id\"):\n","                return self._apply_unified_request(body, all_ready=True)\n","            try:\n"]],[3164,4,["                self._send_json(200, payload)\n","            except BusyError as exc:\n","                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n","            except Exception as exc:\n"]],[3169,2,["            return\n","\n"]],[3172,1,["            body = self._read_json_body()\n","            if body.get(\"plan_id\"):\n","                return self._apply_unified_request(body)\n"]],[3174,2,["\n","            try:\n"]],[3177,1,["                payload = run_with_progress(\n"]],[3180,2,["                )\n","                self._send_json(200, payload)\n"]],[3184,3,["            except BusyError as exc:\n","                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n","            except Exception as exc:\n"]],[3188,5,["            return\n","\n","        self._send_json(404, {\"ok\": False, \"error\": \"not_found\"})\n","\n","\n"]],[3194,1,["    ensure_dirs()\n"]],[3210,1,["        with CHECK_PROGRESS_LOCK:\n"]],[3215,1,["\n"]],[3218,1,["            return\n"]],[3221,1,["\n"]],[3227,1,["\n"]],[3229,1,["\n"]],[3231,1,["    try:\n"]],[3233,1,["    finally:\n"]],[3237,2,["\n","\n"]],[3240,1,["    ensure_dirs()\n"]],[3244,1,["\n"]],[3248,2,["            {\n","                \"ok\": True,\n"]],[3255,5,["        )\n","    )\n","    return 0\n","\n","\n"]],[3268,2,["\n","\n"]],[3272,2,["    ensure_dirs()\n","\n"]],[3275,1,["\n"]],[3278,1,["\n"]],[3281,1,["\n"]],[3283,3,["    return 0\n","\n","\n"]]]},"renderer/app.js":{"origin":"5f0952f4d9a5de44bd7a55111a46f6ac7c87e2b2","target":"a4c5a2a0e9f46205450228cc7628eddc3d13ef6c","hunks":[[1,1,["\n"]],[18,1,["\n"]],[95,1,["\n"]],[125,1,["\n"]],[141,1,["\n"]],[151,1,["  }\n"]],[154,2,["}\n","\n"]],[168,1,["  };\n"]],[170,2,["}\n","\n"]],[179,2,["}\n","\n"]],[185,3,["    return;\n","  }\n","\n"]],[192,1,["\n"]],[196,1,["\n"]],[202,2,["  }\n","\n"]],[207,2,["  }\n","\n"]],[214,2,["  }\n","\n"]],[221,2,["  }\n","\n"]],[225,3,["  }\n","}\n","\n"]],[231,2,["}\n","\n"]],[237,1,["\n"]],[241,6,["    }));\n","  }\n","\n","  renderAppUpdateProgress();\n","}\n","\n"]],[250,2,["}\n","\n"]],[255,6,["    installAppUpdateBtn.disabled = true;\n","    renderSettingsSummary();\n","    renderAppUpdateProgress();\n","    return;\n","  }\n","\n"]],[269,1,["\n"]],[272,1,["      } else {\n"]],[274,2,["      }\n","\n"]],[278,1,["    } else {\n"]],[281,2,["    }\n","  } else {\n"]],[285,8,["    }));\n","    installAppUpdateBtn.disabled = true;\n","  }\n","\n","  renderSettingsSummary();\n","  renderAppUpdateProgress();\n","}\n","\n"]],[294,1,["  try {\n"]],[296,1,["    if (!raw) {\n"]],[298,2,["    }\n","    const parsed = JSON.parse(raw);\n"]],[303,2,["    };\n","  } catch {\n"]],[306,3,["  }\n","}\n","\n"]],[311,2,["}\n","\n"]],[317,1,["  };\n"]],[319,2,["}\n","\n"]],[322,1,["  checkAppUpdateBtn.disabled = true;\n"]],[324,6,["  if (!silent) {\n","    clearAppUpdateProgress();\n","  }\n","\n","  if (!silent) {\n","    installAppUpdateBtn.disabled = true;\n"]],[331,3,["  }\n","\n","  try {\n"]],[335,1,["    appUpdateState = payload;\n"]],[338,1,["  } catch (err) {\n"]],[343,9,["    }\n","    renderSettingsSummary();\n","  } finally {\n","    appUpdateProgressEnabled = false;\n","    clearAppUpdateProgress();\n","    checkAppUpdateBtn.disabled = false;\n","  }\n","}\n","\n"]],[355,1,["  }\n"]],[359,2,["}\n","\n"]],[365,2,["}\n","\n"]],[369,3,["    return;\n","  }\n","\n"]],[374,1,["  checkAppUpdateBtn.disabled = true;\n"]],[378,2,["\n","  try {\n"]],[381,2,["    appUpdateState = payload;\n","\n"]],[386,3,["      return;\n","    }\n","\n"]],[391,1,["    setInlineStatus(\n"]],[394,4,["    );\n","    installAppUpdateBtn.disabled = !shouldAllowInstall(appUpdateState);\n","    appUpdateProgressEnabled = false;\n","  } catch (err) {\n"]],[399,3,["    installAppUpdateBtn.disabled = !shouldAllowInstall(appUpdateState);\n","    appUpdateProgressEnabled = false;\n","  } finally {\n"]],[403,5,["    checkAppUpdateBtn.disabled = false;\n","    renderAppUpdateProgress();\n","  }\n","}\n","\n"]],[410,2,["}\n","\n"]],[414,3,["  const selectedCount = selectedPackagesFromSnapshot().length;\n","  const visibleOutdatedCount = getFilteredPackages().filter((pkg) => pkg?.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg))).length;\n","  const operationRunning = !!lastKnownOperationRunning || operationStatusUnverified;\n"]],[418,1,["\n"]],[420,3,["  updateAllBtn.disabled = busyLoading || operationRunning || !allowed || (!!snapshot?.unified_plan_id && !snapshot?.update_decisions?.some(d => d.status === 'ready'));\n","\n","  if (updateSelectedBtn) {\n"]],[424,3,["  }\n","\n","  if (selectVisibleOutdatedBtn) {\n"]],[428,6,["  }\n","\n","  if (clearSelectionBtn) {\n","    clearSelectionBtn.disabled = busyLoading || operationRunning || selectedCount === 0;\n","  }\n","\n"]],[436,2,["  });\n","\n"]],[440,3,["  });\n","}\n","\n"]],[445,1,["  showMessage(i18n.t('message.brewPathRequired'), true);\n"]],[447,3,["}\n","\n","\n"]],[456,1,["\n"]],[458,1,["\n"]],[466,1,["  const labels = {\n"]],[471,1,["  };\n"]],[483,1,["  }\n"]],[496,1,["    const text = document.createElement('p');\n"]],[500,1,["    panel.appendChild(text);\n"]],[508,1,["    }\n"]],[510,2,["  } else {\n","    const text = document.createElement('p');\n"]],[514,4,["    panel.appendChild(text);\n","  }\n","}\n","\n"]],[529,2,["  operationStatusUnverified = false;\n","  lastKnownOperationRunning = !!progress.running;\n"]],[532,5,["  presentOperationStatus();\n","  applyActionAvailability();\n","  return true;\n","}\n","\n"]],[542,4,["  operationStatusUnverified = false;\n","  presentOperationStatus();\n","}\n","\n"]],[548,3,["  presentOperationStatus();\n","}\n","\n"]],[558,6,["      return;\n","    }\n","  }\n","  try {\n","    const payload = await window.brewApp.getProgress();\n","    if (generation !== operationRequestGeneration) return;\n"]],[568,7,["  } catch {\n","    if (generation !== operationRequestGeneration) return;\n","    statusUnavailable();\n","    if (!progressPollTimer) startProgressPolling();\n","  }\n","}\n","\n"]],[576,1,["  const labels = {\n"]],[588,1,["  };\n"]],[590,2,["}\n","\n"]],[599,4,["    }\n","    setOperationStatusBadge('running');\n","  }\n","\n"]],[605,7,["  }\n","\n","  applyActionAvailability();\n","\n","  presentOperationStatus();\n","}\n","\n"]],[616,2,["}\n","\n"]],[622,2,["}\n","\n"]],[628,2,["}\n","\n"]],[633,1,["    if (!key) return;\n"]],[635,2,["  });\n","\n"]],[639,3,["  }\n","}\n","\n"]],[645,2,["  }\n","\n"]],[649,2,["  }\n","\n"]],[653,3,["  }\n","}\n","\n"]],[659,1,["\n"]],[663,2,["}\n","\n"]],[667,2,["}\n","\n"]],[675,2,["}\n","\n"]],[679,2,["}\n","\n"]],[682,3,["  presentOperationStatus();\n","}\n","\n"]],[687,1,["  const actionGeneration = operationRequestGeneration;\n"]],[690,3,["  }\n","\n","  try {\n"]],[694,1,["    if (generation !== runtimeRefreshGeneration || actionGeneration !== operationRequestGeneration) return;\n"]],[697,1,["\n"]],[702,1,["        if (showFeedback) {\n"]],[706,2,["        }\n","      } else {\n"]],[711,1,["        }\n"]],[716,1,["        } else {\n"]],[718,2,["        }\n","        if (showFeedback) {\n"]],[721,4,["        }\n","      }\n","    }\n","\n"]],[726,1,["\n"]],[728,2,["    await loadUpdateHistory();\n","\n"]],[732,6,["    }\n","  } catch (err) {\n","    if (generation !== runtimeRefreshGeneration || actionGeneration !== operationRequestGeneration) return;\n","    statusUnavailable();\n","    if (!progressPollTimer) startProgressPolling();\n","    if (showFeedback) {\n"]],[739,2,["    }\n","  } finally {\n"]],[743,4,["    }\n","  }\n","}\n","\n"]],[752,1,["\n"]],[755,2,["    presentOperationStatus();\n","    if (showFeedback) {\n"]],[758,4,["    }\n","    return;\n","  }\n","\n"]],[765,4,["    presentOperationStatus();\n","    return;\n","  }\n","\n"]],[772,2,["  presentOperationStatus();\n","\n"]],[775,1,["\n"]],[778,3,["    return;\n","  }\n","\n"]],[783,3,["  }\n","}\n","\n"]],[791,3,["  }\n","\n","  return {\n"]],[796,3,["  };\n","}\n","\n"]],[804,2,["  }\n","\n"]],[809,3,["  });\n","}\n","\n"]],[815,1,["  }\n"]],[822,2,["}\n","\n"]],[828,3,["  }\n","}\n","\n"]],[836,2,["}\n","\n"]],[843,2,["}\n","\n"]],[853,2,["}\n","\n"]],[856,1,["  if (item?.canceled) {\n"]],[858,2,["  }\n","\n"]],[863,2,["  }\n","\n"]],[866,5,["    return i18n.t('message.skippedReasonNotOutdated');\n","  }\n","\n","  const code = Number(item?.code || 0);\n","  if (code === 124) {\n"]],[872,2,["  }\n","  if (Number.isFinite(code) && code !== 0) {\n"]],[875,2,["  }\n","\n"]],[878,2,["}\n","\n"]],[881,1,["  if (item?.canceled) {\n"]],[883,2,["  }\n","\n"]],[886,2,["  const code = Number(item?.code || 0);\n","\n"]],[890,1,["  }\n"]],[892,2,["    return i18n.t('progress.failureExplainCommandTimeout');\n","  }\n"]],[896,1,["  }\n"]],[899,1,["  }\n"]],[902,6,["  }\n","\n","  if (code === 124) {\n","    return i18n.t('progress.failureExplainCommandTimeout');\n","  }\n","  if (Number.isFinite(code) && code !== 0) {\n"]],[909,2,["  }\n","\n"]],[912,2,["}\n","\n"]],[919,1,["  }\n"]],[921,2,["}\n","\n"]],[924,3,["  if (!container) return;\n","  clearNode(container);\n","\n"]],[928,6,["  if (!list.length) {\n","    container.appendChild(makeProgressEmptyNode());\n","    return;\n","  }\n","\n","  list.forEach((item) => {\n"]],[936,1,["\n"]],[940,2,["\n","    if (mode === 'completed') {\n"]],[943,1,["    } else {\n"]],[945,2,["    }\n","\n"]],[948,2,["\n","    if (mode === 'completed') {\n"]],[952,2,["        const note = document.createElement('div');\n","        note.className = 'update-live-item-note';\n"]],[955,1,["        row.appendChild(note);\n"]],[957,2,["        const note = document.createElement('div');\n","        note.className = 'update-live-item-note';\n"]],[960,4,["        row.appendChild(note);\n","      }\n","    }\n","\n"]],[965,3,["  });\n","}\n","\n"]],[969,2,["  if (!container) return;\n","\n"]],[975,5,["    }\n","  });\n","\n","  clearNode(container);\n","\n"]],[981,1,["  if (!list.length) {\n"]],[983,4,["    container.appendChild(makeProgressEmptyNode());\n","    return;\n","  }\n","\n"]],[988,2,["\n","  list.forEach((item) => {\n"]],[992,1,["\n"]],[998,2,["    }\n","\n"]],[1002,1,["\n"]],[1006,1,["\n"]],[1009,1,["\n"]],[1013,1,["      } else {\n"]],[1015,3,["      }\n","    });\n","\n"]],[1019,2,["  });\n","\n"]],[1022,1,["    if (!validKeys.has(key)) {\n"]],[1024,4,["    }\n","  });\n","}\n","\n"]],[1045,1,["  );\n"]],[1049,1,["  };\n"]],[1053,1,["  });\n"]],[1057,1,["  });\n"]],[1068,2,["}\n","\n"]],[1073,1,["\n"]],[1084,1,["\n"]],[1086,4,["    updateLivePanelEl.classList.remove('visible');\n","    return;\n","  }\n","\n"]],[1091,1,["\n"]],[1096,1,["\n"]],[1101,1,["\n"]],[1105,2,["}\n","\n"]],[1112,2,["}\n","\n"]],[1119,1,["\n"]],[1122,1,["        clearTimeout(timer);\n"]],[1124,1,["      })\n"]],[1126,1,["        clearTimeout(timer);\n"]],[1128,4,["      });\n","  });\n","}\n","\n"]],[1133,1,["  const raw = String(value || '').trim();\n"]],[1139,2,["  }\n","  return {\n"]],[1143,3,["  };\n","}\n","\n"]],[1148,1,["  return {\n"]],[1155,3,["  };\n","}\n","\n"]],[1160,1,["\n"]],[1167,1,["\n"]],[1175,2,["  }\n","\n"]],[1180,1,["\n"]],[1182,2,["}\n","\n"]],[1185,2,["  const scheduler = settingsState?.scheduler;\n","  if (!scheduler) {\n"]],[1188,4,["    settingsSummaryEl.textContent = i18n.t('settings.loading');\n","    return;\n","  }\n","\n"]],[1194,2,["}\n","\n"]],[1199,1,["\n"]],[1204,1,["\n"]],[1208,2,["}\n","\n"]],[1214,1,["  const scheduler = settingsState?.scheduler;\n"]],[1216,3,["\n","  if (!scheduler) {\n","    settingsSummaryEl.textContent = i18n.t('settings.loading');\n"]],[1221,3,["    return;\n","  }\n","\n"]],[1229,1,["\n"]],[1231,1,["  renderSettingsSummary();\n"]],[1233,3,["  applyActionAvailability();\n","}\n","\n"]],[1237,1,["  try {\n"]],[1240,2,["    renderSettings();\n","  } catch (err) {\n"]],[1244,3,["  }\n","}\n","\n"]],[1250,2,["\n","  try {\n"]],[1253,2,["    settingsState = {\n","      ...(settingsState || {}),\n"]],[1256,5,["      settings: {\n","        ...(settingsState?.settings || {}),\n","      },\n","    };\n","    renderSettings();\n"]],[1262,1,["  } catch (err) {\n"]],[1264,3,["  }\n","}\n","\n"]],[1270,2,["\n","  try {\n"]],[1273,4,["    settingsState = {\n","      ...(settingsState || {}),\n","      settings: {\n","        ...(settingsState?.settings || {}),\n"]],[1278,1,["      },\n"]],[1280,1,["    };\n"]],[1283,8,["    renderSettingsSummary();\n","    applyActionAvailability();\n","    await loadState();\n","  } catch (err) {\n","    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaveFailed', { error: err.message }), true);\n","  }\n","}\n","\n"]],[1296,1,["  selectedPackageKeys.clear();\n"]],[1298,1,["  renderTable();\n"]],[1300,4,["  showMessage(i18n.t('message.brewPathRequired'), true);\n","  applyActionAvailability();\n","}\n","\n"]],[1306,2,["\n","  try {\n"]],[1312,1,["\n"]],[1318,5,["      );\n","      return;\n","    }\n","\n","    setInlineStatus(\n"]],[1325,1,["    );\n"]],[1329,6,["    }\n","  } catch (err) {\n","    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaveFailed', { error: err.message }), true);\n","  }\n","}\n","\n"]],[1343,8,["  loaderStepEls.forEach((stepEl) => {\n","    stepEl.classList.remove('active', 'done');\n","  });\n","\n","  if (loaderCancelBtn) {\n","    loaderCancelBtn.disabled = true;\n","  }\n","\n"]],[1352,2,["    updateLivePanelEl.classList.remove('visible');\n","  }\n"]],[1361,2,["}\n","\n"]],[1367,1,["  }\n"]],[1370,1,["  }\n"]],[1377,1,["  }\n"]],[1379,2,["}\n","\n"]],[1385,2,["}\n","\n"]],[1389,2,["  loaderStepEls.forEach((stepEl) => {\n","    stepEl.classList.remove('active', 'done');\n"]],[1394,1,["\n"]],[1397,3,["      return;\n","    }\n","\n"]],[1402,4,["    }\n","  });\n","}\n","\n"]],[1408,4,["    return;\n","  }\n","\n","  lastKnownOperationRunning = !!progress.running;\n"]],[1413,1,["  if (progress.running) {\n"]],[1415,2,["  }\n","\n"]],[1422,4,["    presentOperationStatus();\n","    return;\n","  }\n","\n"]],[1427,1,["\n"]],[1433,1,["\n"]],[1447,1,["\n"]],[1453,2,["\n","  if (loaderCancelBtn) {\n"]],[1459,2,["  }\n","\n"]],[1463,1,["\n"]],[1465,3,["\n","  if (progress.running) {\n","    setOperationStatusBadge('running');\n"]],[1472,1,["  } else {\n"]],[1474,3,["  }\n","}\n","\n"]],[1478,1,["\n"]],[1483,2,["  try {\n","    const payload = await window.brewApp.getProgress();\n"]],[1489,1,["    }\n"]],[1492,1,["  } catch {\n"]],[1494,1,["  } finally {\n"]],[1496,3,["  }\n","}\n","\n"]],[1503,2,["}\n","\n"]],[1510,1,["  }\n"]],[1512,2,["}\n","\n"]],[1517,6,["\n","  if (loaderCancelBtn) {\n","    loaderCancelBtn.disabled = true;\n","  }\n","\n","  try {\n"]],[1526,1,["    } else {\n"]],[1528,2,["    }\n","  } catch (err) {\n"]],[1531,1,["  } finally {\n"]],[1534,3,["  }\n","}\n","\n"]],[1539,2,["}\n","\n"]],[1545,2,["  }\n","\n"]],[1549,2,["  }\n","\n"]],[1553,2,["  }\n","\n"]],[1557,2,["  }\n","\n"]],[1562,3,["  }\n","}\n","\n"]],[1569,2,["  }\n","\n"]],[1573,2,["  }\n","\n"]],[1577,2,["  }\n","\n"]],[1581,2,["  }\n","\n"]],[1589,3,["  }\n","}\n","\n"]],[1594,3,["  updateHistoryFilterControls();\n","}\n","\n"]],[1598,2,["  if (!normalizedTerm) return true;\n","  const text = [\n"]],[1606,6,["  ]\n","    .map((value) => String(value || '').toLowerCase())\n","    .join(' ');\n","  return text.includes(normalizedTerm);\n","}\n","\n"]],[1614,3,["}\n","\n","\n"]],[1622,2,["}\n","\n"]],[1625,3,["  return String(pkg?.update_decision?.source_installation_id || pkg?.installation_id || packageSelectionKey(pkg?.name, pkg?.kind));\n","}\n","function unifiedReady(pkg) {\n","  return pkg?.update_decision?.status === 'ready';\n","}\n","function unifiedPayload(packages) {\n","  if (!snapshot?.unified_plan_id) return null;\n","  if (packages.some(p => !unifiedReady(p))) throw new Error(\n","    i18n.lang === 'he' ? 'החבילה חסומה. יש לבצע בדיקה חדשה.' : 'Package is blocked. Run another check.');\n","  if (!globalThis.crypto?.randomUUID) throw new Error('Secure request identifiers are unavailable');\n","  return {\n","    request_id: globalThis.crypto.randomUUID().replaceAll('-', ''),\n","    plan_id: snapshot.unified_plan_id,\n","    decisions: packages.map(p => ({\n","      decision_id: p.update_decision.decision_id,\n","      material_fingerprint: p.update_decision.material_fingerprint,\n","    })),\n","  };\n","}\n","\n"]],[1629,1,["  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n"]],[1631,1,["    .filter((pkg) => !pkg?.excluded && !pkg?.update_block_reason && pkg?.outdated && (!snapshot?.unified_plan_id || unifiedReady(pkg)) && selectedPackageKeys.has(packageSelectionKeyFromPkg(pkg)))\n"]],[1635,0,["      installation_id: pkg?.installation_id,\n","      update_decision: pkg?.update_decision,\n"]],[1637,2,["}\n","\n"]],[1640,1,["  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n"]],[1643,1,["      .filter((pkg) => pkg?.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg)))\n"]],[1646,2,["  );\n","\n"]],[1650,1,["    if (!validKeys.has(key)) {\n"]],[1653,3,["    }\n","  });\n","\n"]],[1657,2,["}\n","\n"]],[1660,1,["  const selectedCount = selectedPackagesFromSnapshot().length;\n"]],[1662,1,["\n"]],[1665,3,["  }\n","\n","  if (updateSelectedBtn) {\n"]],[1669,2,["  }\n","\n"]],[1673,3,["  const operationRunning = !!lastKnownOperationRunning || operationStatusUnverified;\n","\n","  if (selectVisibleOutdatedBtn) {\n"]],[1677,7,["  }\n","\n","  if (clearSelectionBtn) {\n","    clearSelectionBtn.disabled = busyLoading || operationRunning || selectedCount === 0;\n","  }\n","}\n","\n"]],[1689,2,["    return;\n","  }\n"]],[1692,2,["}\n","\n"]],[1700,4,["  });\n","  renderTable();\n","}\n","\n"]],[1706,4,["  selectedPackageKeys.clear();\n","  renderTable();\n","}\n","\n"]],[1713,2,["}\n","\n"]],[1719,3,["  return true;\n","}\n","\n"]],[1724,2,["}\n","\n"]],[1728,2,["}\n","\n"]],[1731,1,["  const raw = String(value || '').trim();\n"]],[1735,2,["}\n","\n"]],[1743,4,["  });\n","  return direction === 'desc' ? -cmp : cmp;\n","}\n","\n"]],[1750,1,["\n"]],[1754,1,["\n"]],[1756,3,["  return direction === 'desc' ? -cmp : cmp;\n","}\n","\n"]],[1761,1,["\n"]],[1765,1,["      break;\n"]],[1768,1,["      break;\n"]],[1771,1,["      break;\n"]],[1774,1,["      break;\n"]],[1777,1,["      break;\n"]],[1779,1,["      cmp = compareNumberValues(\n"]],[1782,3,["        direction,\n","      );\n","      break;\n"]],[1788,1,["      cmp = compareNumberValues(\n"]],[1791,4,["        direction,\n","      );\n","      break;\n","    }\n"]],[1797,2,["      break;\n","    default:\n"]],[1800,3,["      break;\n","  }\n","\n"]],[1805,2,["}\n","\n"]],[1810,1,["\n"]],[1813,2,["  }\n","\n"]],[1816,2,["}\n","\n"]],[1819,1,["  try {\n"]],[1821,1,["    if (!raw) {\n"]],[1823,3,["    }\n","\n","    const parsed = JSON.parse(raw);\n"]],[1828,2,["\n","    if (!isSortableColumnKey(key)) {\n"]],[1831,2,["    }\n","\n"]],[1834,1,["  } catch {\n"]],[1836,3,["  }\n","}\n","\n"]],[1844,2,["}\n","\n"]],[1848,3,["    return;\n","  }\n","\n"]],[1853,2,["    tableSortState = {\n","      key: normalizedKey,\n"]],[1856,4,["    };\n","  } else {\n","    tableSortState = {\n","      key: normalizedKey,\n"]],[1861,3,["    };\n","  }\n","\n"]],[1865,2,["}\n","\n"]],[1868,4,["  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n","\n","  headers.forEach((th) => {\n","    const key = String(th.dataset.colKey || '').trim();\n"]],[1874,1,["\n"]],[1876,1,["\n"]],[1881,4,["      }\n","      return;\n","    }\n","\n"]],[1888,1,["\n"]],[1892,1,["    } else {\n"]],[1894,2,["    }\n","\n"]],[1900,4,["    }\n","  });\n","}\n","\n"]],[1905,8,["  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n","\n","  headers.forEach((th) => {\n","    const key = String(th.dataset.colKey || '').trim();\n","    if (!isSortableColumnKey(key)) {\n","      return;\n","    }\n","\n"]],[1914,1,["\n"]],[1918,2,["    };\n","\n"]],[1922,5,["        return;\n","      }\n","      onSort();\n","    });\n","\n"]],[1929,10,["        return;\n","      }\n","      event.preventDefault();\n","      onSort();\n","    });\n","  });\n","\n","  renderTableSortIndicators();\n","}\n","\n"]],[1942,2,["}\n","\n"]],[1947,1,["  }\n"]],[1950,1,["  }\n"]],[1952,2,["}\n","\n"]],[1958,2,["}\n","\n"]],[1961,2,["  if (!normalizedTerm) return true;\n","  const text = [\n"]],[1968,6,["  ]\n","    .map((value) => String(value || '').toLowerCase())\n","    .join(' ');\n","  return text.includes(normalizedTerm);\n","}\n","\n"]],[1977,2,["}\n","\n"]],[1982,2,["}\n","\n"]],[1987,2,["}\n","\n"]],[1999,1,["    default:\n"]],[2001,3,["  }\n","}\n","\n"]],[2011,2,["  });\n","\n"]],[2014,2,["}\n","\n"]],[2018,1,["\n"]],[2025,3,["    return;\n","  }\n","\n"]],[2030,1,["\n"]],[2033,1,["\n"]],[2036,1,["\n"]],[2040,1,["\n"]],[2044,1,["\n"]],[2047,1,["\n"]],[2050,1,["\n"]],[2056,1,["\n"]],[2060,1,["\n"]],[2063,1,["\n"]],[2067,3,["  });\n","}\n","\n"]],[2075,1,["\n"]],[2078,1,["\n"]],[2083,1,["  } else {\n"]],[2085,5,["  }\n","\n","  updateHistoryFilterControls();\n","}\n","\n"]],[2093,3,["  renderHistoryPanel();\n","\n","  try {\n"]],[2100,1,["    );\n"]],[2102,1,["  } catch (err) {\n"]],[2104,1,["  } finally {\n"]],[2106,4,["    renderHistoryPanel();\n","  }\n","}\n","\n"]],[2113,1,["  }\n"]],[2116,1,["  }\n"]],[2118,2,["}\n","\n"]],[2126,1,["  }\n"]],[2128,2,["}\n","\n"]],[2135,1,["  }\n"]],[2137,2,["}\n","\n"]],[2142,2,["    return i18n.t('message.skippedReasonNotOutdated');\n","  }\n"]],[2145,2,["}\n","\n"]],[2157,1,["\n"]],[2164,3,["  });\n","}\n","\n"]],[2168,1,["  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n"]],[2172,1,["\n"]],[2175,1,["\n"]],[2179,1,["\n"]],[2181,2,["}\n","\n"]],[2188,2,["  });\n","  renderTableSortIndicators();\n"]],[2191,1,["\n"]],[2197,1,["\n"]],[2199,1,["    tableTitleEl.textContent = activeTab === 'outdated'\n"]],[2201,3,["        count: i18n.formatNumber(list.length),\n","        query: searchRaw,\n","      })\n"]],[2205,3,["        count: i18n.formatNumber(list.length),\n","        query: searchRaw,\n","      });\n"]],[2209,1,["    tableTitleEl.textContent = activeTab === 'outdated'\n"]],[2212,2,["  } else {\n","    tableTitleEl.textContent = activeTab === 'outdated'\n"]],[2218,3,["      });\n","  }\n","\n"]],[2222,2,["  if (!list.length) {\n","    const tr = document.createElement('tr');\n"]],[2231,1,["    } else {\n"]],[2233,1,["    }\n"]],[2235,1,["    tbodyEl.appendChild(tr);\n"]],[2237,4,["    applyActionAvailability();\n","    return;\n","  }\n","\n"]],[2242,2,["    const tr = document.createElement('tr');\n","\n"]],[2249,1,["\n"]],[2257,2,["    }\n","\n"]],[2265,1,["\n"]],[2272,1,["\n"]],[2276,1,["\n"]],[2280,1,["\n"]],[2284,1,["\n"]],[2291,1,["\n","    if (pkg.update_decision) {\n","      const d = pkg.update_decision;\n","      const info = document.createElement('div');\n","      info.className = 'small muted'; info.dir = 'auto';\n","      info.textContent = (d.current_provider === d.target_provider ? d.target_provider\n","        : d.current_provider + ' → ' + d.target_provider) + ' · ' + d.status\n","        + ' · ' + (d.reason_codes || []).join(', ');\n","      tdStatus.appendChild(info);\n","    }\n"]],[2298,1,["    }\n"]],[2300,1,["    if (pkg.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg))) {\n"]],[2303,1,["\n"]],[2313,2,["      });\n","\n"]],[2317,1,["    } else {\n"]],[2319,2,["    }\n","\n"]],[2322,1,["    if (pkg.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg))) {\n"]],[2326,1,["      btn.addEventListener('click', () => updateOne(pkg.name, pkg.kind, btn, pkg));\n"]],[2328,1,["    } else {\n"]],[2330,2,["    }\n","\n"]],[2344,4,["\n","    tbodyEl.appendChild(tr);\n","  });\n","\n"]],[2349,3,["  applyActionAvailability();\n","}\n","\n"]],[2358,2,["    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n","  }\n"]],[2361,1,["  renderTable();\n"]],[2363,2,["  renderHistoryPanel();\n","  renderAppUpdateStatus();\n"]],[2366,2,["  applyActionAvailability();\n","\n"]],[2376,2,["}\n","\n"]],[2379,2,["  clearMessage();\n","\n"]],[2384,2,["    renderCards(snapshot);\n","    renderTable();\n"]],[2388,4,["    applyActionAvailability();\n","    return;\n","  }\n","\n"]],[2394,2,["  }\n","  try {\n"]],[2397,4,["    renderCards(snapshot);\n","    renderTable();\n","    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n","  } catch (err) {\n"]],[2402,1,["  } finally {\n"]],[2405,4,["    }\n","  }\n","}\n","\n"]],[2411,2,["}\n","\n"]],[2414,3,["  if (!ensureBrewConfiguredForActions()) return;\n","  clearMessage();\n","\n"]],[2418,2,["  try {\n","    const payload = await window.brewApp.getProgress();\n"]],[2421,1,["  } catch {\n"]],[2423,2,["  }\n","\n"]],[2429,3,["    applyActionAvailability();\n","  }\n","\n"]],[2434,1,["\n"]],[2441,1,["        } else {\n"]],[2443,1,["        }\n"]],[2451,1,["      } else {\n"]],[2453,1,["      }\n"]],[2457,4,["    }\n","    return;\n","  }\n","\n"]],[2462,5,["  beginOperationRequest();\n","  const actionGeneration = operationRequestGeneration;\n","  let operationResponse = null;\n","  startProgressPolling();\n","  try {\n"]],[2470,2,["      return;\n","    }\n"]],[2473,3,["    renderCards(snapshot);\n","    renderTable();\n","    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n"]],[2477,1,["  } catch (err) {\n"]],[2479,13,["    if (/fetch failed/i.test(String(err?.message || ''))) {\n","      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n","    }\n","  } finally {\n","    await finishOperationRequest(operationResponse, actionGeneration);\n","    setLoading(false, 'message.loadingState');\n","  }\n","}\n","\n","async function updateOne(name, kind, sourceBtn = null, row = null) {\n","  if (!ensureBrewConfiguredForActions()) return;\n","  const request = row && snapshot?.unified_plan_id ? unifiedPayload([row]) : null;\n","  if (!request && !confirm(i18n.t('confirm.updateOne', { name, kind: kindLabel(kind) }))) return;\n","\n"]],[2494,3,["  }\n","\n","  clearMessage();\n"]],[2499,13,["  beginOperationRequest();\n","  const actionGeneration = operationRequestGeneration;\n","  let operationResponse = null;\n","  startProgressPolling();\n","\n","  try {\n","    const res = operationResponse = await window.brewApp.updateOne(name, kind, request);\n","    snapshot = res.snapshot;\n","    renderCards(snapshot);\n","    renderTable();\n","    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n","\n","    if (res?.result?.ok || (request && res?.ok)) {\n"]],[2517,1,["      }\n"]],[2519,1,["\n"]],[2525,2,["      }\n","    } else {\n"]],[2529,4,["    }\n","\n","    await loadUpdateHistory();\n","  } catch (err) {\n"]],[2534,6,["    if (/fetch failed/i.test(String(err?.message || ''))) {\n","      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n","    }\n","  } finally {\n","    await finishOperationRequest(operationResponse, actionGeneration);\n","    setLoading(false, 'message.loadingState');\n"]],[2542,4,["    }\n","  }\n","}\n","\n"]],[2547,2,["  if (!ensureBrewConfiguredForActions()) return;\n","\n"]],[2552,6,["    return;\n","  }\n","\n","  const selectedRows = (snapshot?.packages || []).filter(pkg =>\n","    selected.some(item => item.installation_id === pkg.installation_id));\n","  const request = snapshot?.unified_plan_id ? unifiedPayload(selectedRows) : null;\n","  if (!request && !confirm(i18n.t('confirm.updateSelected', { count: i18n.formatNumber(selected.length) }))) return;\n","\n","  if (updateSelectedBtn) {\n"]],[2559,3,["  }\n","\n","  clearMessage();\n"]],[2564,8,["  beginOperationRequest();\n","  const actionGeneration = operationRequestGeneration;\n","  let operationResponse = null;\n","  startProgressPolling();\n","\n","  try {\n","    const res = operationResponse = await window.brewApp.updateSelected(request || selected);\n","    snapshot = res.snapshot;\n"]],[2573,4,["    renderCards(snapshot);\n","    renderTable();\n","    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n","\n"]],[2578,2,["      success: i18n.formatNumber(res.updated_count || 0),\n","      failed: i18n.formatNumber(res.failed_count || 0),\n"]],[2581,3,["    });\n","\n","    const results = Array.isArray(res?.results) ? res.results : [];\n"]],[2589,1,["\n"]],[2593,2,["      }\n","\n"]],[2597,4,["      })}`;\n","    }\n","\n","    if (res?.inventory_refresh_error) {\n"]],[2603,4,["        error: res.inventory_refresh_error,\n","      })}`;\n","    }\n","\n"]],[2608,2,["\n","    const verifiedLatestCount = results.reduce((acc, result) => {\n"]],[2611,4,["      const pkg = findPackageInSnapshot(result?.name, result?.kind);\n","      return pkg && !pkg.outdated ? acc + 1 : acc;\n","    }, 0);\n","    if (verifiedLatestCount > 0) {\n"]],[2616,4,["    }\n","\n","    await loadUpdateHistory();\n","  } catch (err) {\n"]],[2621,6,["    if (/fetch failed/i.test(String(err?.message || ''))) {\n","      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n","    }\n","  } finally {\n","    await finishOperationRequest(operationResponse, actionGeneration);\n","    setLoading(false, 'message.loadingState');\n"]],[2628,3,["  }\n","}\n","\n"]],[2632,3,["  if (!ensureBrewConfiguredForActions()) return;\n","  const request = snapshot?.unified_plan_id ? unifiedPayload(\n","    (snapshot?.packages || []).filter(unifiedReady)) : null;\n","  if (!request && !confirm(i18n.t('confirm.updateAll'))) return;\n","\n"]],[2636,2,["\n","  clearMessage();\n"]],[2640,10,["  beginOperationRequest();\n","  const actionGeneration = operationRequestGeneration;\n","  let operationResponse = null;\n","  startProgressPolling();\n","  try {\n","    const res = operationResponse = await window.brewApp.updateAll(request);\n","    snapshot = res.snapshot;\n","    renderCards(snapshot);\n","    renderTable();\n","    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n"]],[2651,5,["      success: i18n.formatNumber(res.updated_count || 0),\n","      failed: i18n.formatNumber(res.failed_count || 0),\n","    }));\n","\n","    if (res?.inventory_refresh_error) {\n"]],[2657,1,["        error: res.inventory_refresh_error,\n"]],[2659,4,["    }\n","\n","    const results = Array.isArray(res?.results) ? res.results : [];\n","    const verifiedLatestCount = results.reduce((acc, result) => {\n"]],[2664,4,["      const pkg = findPackageInSnapshot(result?.name, result?.kind);\n","      return pkg && !pkg.outdated ? acc + 1 : acc;\n","    }, 0);\n","    if (verifiedLatestCount > 0) {\n"]],[2669,4,["    }\n","\n","    await loadUpdateHistory();\n","  } catch (err) {\n"]],[2674,6,["    if (/fetch failed/i.test(String(err?.message || ''))) {\n","      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n","    }\n","  } finally {\n","    await finishOperationRequest(operationResponse, actionGeneration);\n","    setLoading(false, 'message.loadingState');\n"]],[2681,3,["  }\n","}\n","\n"]],[2685,1,["  try {\n"]],[2688,1,["    const parsed = JSON.parse(raw);\n"]],[2690,1,["  } catch {\n"]],[2692,3,["  }\n","}\n","\n"]],[2696,1,["  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n"]],[2698,3,["  headers.forEach((th) => {\n","    const key = String(th.dataset.colKey || '').trim();\n","    if (!key) return;\n"]],[2702,1,["  });\n"]],[2704,2,["}\n","\n"]],[2707,1,["  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n"]],[2709,4,["\n","  headers.forEach((th) => {\n","    const key = String(th.dataset.colKey || '').trim();\n","    if (!key) return;\n"]],[2716,2,["    }\n","\n"]],[2720,1,["\n"]],[2722,2,["      event.preventDefault();\n","\n"]],[2729,1,["\n"]],[2733,1,["\n"]],[2738,2,["      };\n","\n"]],[2747,2,["      };\n","\n"]],[2751,4,["    });\n","  });\n","}\n","\n"]],[2760,4,["    renderTable();\n","  });\n","});\n","\n"]],[2767,2,["});\n","\n"]],[2772,5,["    updateSearchControls();\n","    renderTable();\n","  });\n","}\n","\n"]],[2781,3,["    packageSearchTerm = '';\n","    updateSearchControls();\n","    renderTable();\n"]],[2785,3,["  });\n","}\n","\n"]],[2791,5,["    updateSearchControls();\n","    renderTable();\n","  });\n","}\n","\n"]],[2799,5,["    updateSearchControls();\n","    renderTable();\n","  });\n","}\n","\n"]],[2806,1,["    packageSearchTerm = '';\n"]],[2812,6,["    }\n","    updateSearchControls();\n","    renderTable();\n","  });\n","}\n","\n"]],[2821,4,["    renderHistoryPanel();\n","  });\n","}\n","\n"]],[2828,4,["    renderHistoryPanel();\n","  });\n","}\n","\n"]],[2835,4,["    renderHistoryPanel();\n","  });\n","}\n","\n"]],[2842,4,["    renderHistoryPanel();\n","  });\n","}\n","\n"]],[2855,5,["    }\n","    renderHistoryPanel();\n","  });\n","}\n","\n"]],[2881,2,["  }\n","});\n"]],[2888,3,["  }\n","});\n","\n"]],[2899,1,["\n"]],[2903,1,["  applyActionAvailability();\n"]],[2906,1,["  renderAppUpdateStatus();\n"]],[2909,2,["}\n","\n"]],[2913,1,["  try {\n"]],[2917,1,["    });\n"]],[2920,1,["    await loadState();\n"]],[2922,2,["});\n","\n"]]]}}

UNIFIED_MODIFIED_FILES = {
    ".github/workflows/ci.yml": "name: CI\n\non:\n  pull_request:\n  push:\n    branches: [ main, master ]\n\njobs:\n  validate:\n    runs-on: macos-latest\n\n    steps:\n      - name: Checkout\n        uses: actions/checkout@v4\n\n      - name: Setup Node\n        uses: actions/setup-node@v4\n        with:\n          node-version: 22\n          cache: npm\n\n      - name: Setup Python\n        uses: actions/setup-python@v5\n        with:\n          python-version: '3.14'\n\n      - name: Install dependencies\n        run: npm ci\n\n      - name: JavaScript syntax check\n        run: |\n          node --check renderer/app.js\n          node --check renderer/i18n.js\n          node --check electron/main.js\n          node --check electron/preload.js\n\n      - name: Python syntax check\n        run: |\n          python3 -m compileall -q backend tools tests\n          python3 -m py_compile tools/implement-mac-manager.py tools/verify-migration.py\n\n      - name: Focused cached-plan regression checks\n        run: |\n          python3 -m unittest discover -s tests -p 'test_unified_updates.py'\n          node tests/stale-plan-ui.cjs\n          python3 tools/implement-mac-manager.py unified-check\n\n      - name: Packaging smoke check\n        run: npm run pack\n",
    "docs/MAC_MANAGER_TASKS.md": "# מנהל עדכוני Mac — משימות והמשך עבודה\n\nעודכן: 20.10.2026 לאחר הבהרת המטרה וחלוקת הבדיקות עם המשתמש.\nגרסת המקור: 1.3.3 לפי package.json. בעדכון תיעוד זה לא נבדקה אפליקציה חיה\nולא הורצו בדיקות מוצר. תוצאות קודמות הן תוצאות היסטוריות בלבד.\n\n## המטרה המחייבת\n\nבדיקה אחת → בחירת ספק אוטומטית לפי חבילה מוכנה וגרסה מתאימה ל־Mac Intel →\nבחירת חבילות → לחיצה על עדכון → ביצוע ושמירת הספק החדש לאחר מעבר מוצלח.\nהמעבר הוא חלק מהעדכון המוצג למשתמש, בלי אישור נוסף לכל חבילה.\n\n[מטרת המוצר והחלטות העבודה](PRODUCT_GOAL.md) היא המסמך המחייב.\n[תיעוד המימוש](MAC_MANAGER.md) מתאר את הקוד הקיים והפערים מול המטרה.\nאין לפרש קיום קובץ או API כהשלמת מעבר אמיתי, שחזור או חוויית העדכון הרצויה.\n\n## תמונת מצב\n\n| משימה | מצב | מה הוכח ומה חסר |\n| --- | --- | --- |\n| T01 — אמינות מצב ומלאי | הושלמה לפי דוח 1.3.2 | חיווי סופי, טיפול בתשובות ישנות, receipts וקגים; תיקון LLVM עצמו פתוח ב־T06 |\n| T02 — סיווג מועמדות | הושלמה לפי דוח 1.3.2 | כלי/ספרייה/שירות/חסר מידע עם ראיות; סיווג אינו היתר ביצוע |\n| T03 — מעבר ובעלות מתמשכת | מומשה חלקית, פתוחה | נוספו מנוע ופרופילים ב־1.3.3; הפעלה, שחזור, רגרסיות ומעבר אמיתי אינם מוכחים במלואם |\n| T04 — Pixi ובחירת ספקים | פתוחה | סביבות פרטניות קיימות; גילוי ובחירה כלליים עוד נדרשים |\n| T05 — מסלולים רשמיים | המשך, עדיפות נמוכה | רק אחרי מסלול Homebrew/MacPorts/Pixi הרגיל; אין להרחיב עכשיו ללא צורך |\n| T06 — חבילות חסומות | פתוחה | ממצאים היסטוריים לאימות; תיקון נפרד לכל חסם מבוסס |\n| T07 — עדכון אחיד ובדיקות בפקודה | פתוחה, עדיפות מיידית עם T03/T04 | אין עדיין זרימה כללית מוכחת של בחירת ספק, עדכון ושמירת בעלות |\n\n## החלטות עבודה\n\n- Intel, חבילות בינאריות וכל תלויותיהן; בלי נפילה לבניית מקור או ירידה בגרסה.\n- Homebrew, MacPorts, Pixi לפי התאמה. בשוויון לשמר ספק; לבחור גרסה חדשה\n  מתאימה ממסלול נתמך ולבדוק מחדש חלופה כשהספק הקיים אינו נותן מענה.\n- לחיצת עדכון מאשרת את הפעולות המוצגות, כולל מעבר ספק. דרישת הרשאת macOS\n  כשנחוצה אינה אישור מעבר נוסף שהאפליקציה צריכה להמציא.\n- לשמור ngrok מוחרג, הגדרות, היסטוריה, תזמון וזהות התראות. לא לחפש פרויקטים\n  פרטיים; אפשרות לסקריפט פרטי אינה חסם. תלויים מתועדים ושירותים דורשים טיפול.\n- כל שינוי מוצר, בדיקות ותיעוד מופק דרך `tools/implement-mac-manager.py`;\n  את הסקריפט עורכים ב־apply_patch. כל פקודות הטרמינל בסשן אינטראקטיבי.\n- לשמר את העבודה הלא מחויבת והגיבויים. אין להפעיל apply/document-tasks ישנים\n  כדי לתקן תיעוד, ואין לאפס README או מניפסט קודם בגלל כשל תנאי קדם.\n- הסוכן יבצע אימות קצר וממוקד בלבד; המשתמש יריץ את הבדיקות המלאות בסקריפט.\n  הוראה זו מחליפה את החלוקה הישנה שבה הסוכן הריץ הכול והמשתמש רק פיילוט.\n\n## T01 — אמינות מצב הפעולה והמלאי\n\n**מניע:** למנוע ״ממתין״ או טעינה אחרי סיום, והחלטות על בסיס גרסה שאינה פעילה.\n**מצב:** הושלמה לפי [ראיות 1.3.2](T01_T02_VALIDATION.md), 20.10.2026.\n\n- [x] תוצאה סופית שאינה נמחקת ב־finally, ברענון או בשינוי שפה.\n- [x] סיכום עדכונים, כשל, ביטול ודילוגים, לרבות אפס התקנות והכול נדחה.\n- [x] התייחסות לתשובות ישנות, ניתוק ובדיקה במקביל לעדכון.\n- [x] הפרדת גרסה מדווחת, קג פעיל, receipt, נתיב פקודה ו־revision.\n\n**המשך:** לשמר ברגרסיות. אין להסיק מדוח 1.3.2 שקוד 1.3.3 כולו נבדק.\nתיקון clang/LLVM שהתגלה אינו כלול בסגירת T01.\n\n## T02 — סיווג מועמדות למעבר\n\n**מניע:** לדעת מה אפשר להעביר ככלי עצמאי ומה דורש טיפול בתלויים/שירותים.\n**מצב:** הושלמה לפי [ראיות 1.3.2](T01_T02_VALIDATION.md), 20.10.2026.\n\n- [x] איסוף ראיות מקומי ותלויים, בלי חיפוש בפרויקטים פרטיים.\n- [x] סיווג עצמאי / ספרייה או סביבה משותפת / שירות / חסר מידע.\n- [x] נימוק, פער מידע וראיות, בנפרד מזמינות בינארית ומיכולת ביצוע.\n\n**המשך:** להשתמש באותו איסוף לפני ביצוע; סיווג שמור אינו תחליף לאימות עדכני.\n\n## T03 — השלמת המעבר ושמירת הספק\n\n**מניע:** חלופה שמופיעה ברשימה צריכה להפוך לעדכון עובד, עם בעלות עקבית.\n**מצב:** תשתית חלקית קיימת; [סקירת הפערים](T03_VALIDATION.md). פתוחה.\n\n- [x] נוספו migration_engine.py ו־migration_profiles.py, תוכניות ויומני עסקה.\n- [x] נוסף מסלול קוד ל־IWYU לצד פיילוט ההתראות הישן; אין בכך הוכחת קבלה.\n- [ ] לאמת נתונים עדכניים מאיסוף T02 משותף, שינוי תוכנית, החרגות ונעילה בין תהליכים.\n- [ ] להשלים גיבוי מאומת, ביטול, המשך ו־rollback אמיתיים בלי דריסת שינוי עצמאי.\n- [ ] להשלים הפעלת פקודות, בעלות על shims ובדיקת PATH ושימוש לפני הסרת מקור.\n- [ ] להשלים רישום ספק רק אחרי הצלחה, ללא רשומת tool כפולה מול מלאי MacPorts.\n- [ ] לגלות עדכונים עתידיים דרך הספק החדש ולהריץ בדיקת שימוש אחרי עדכונו.\n- [ ] לשמר helper וזהות התראות קיימים; אין להעביר LLVM כחלק מפיילוט IWYU.\n- [ ] לחבר את המעבר לביצוע העדכונים הנבחרים ב־T07, ללא אשף אישור נוסף.\n\n**תנאי השלמה:** דוח המשתמש מוכיח מעבר נתמך, אימות שימוש, גיבוי ושחזור מבוקר,\nומלאי ועדכון מהספק החדש אחרי פתיחה מחדש. בדיקות כשל מבודדות עוברות.\nהפיילוטים הם ראיות הנדסיות בלבד; הם אינם הגדרת המוצר או משימה שהמשתמש צריך לנהל.\n\n## T04 — גילוי Pixi ובחירת ספק אוטומטית\n\n**מניע:** לבחור מסלול Intel מתאים מכל הספקים הנתמכים, מעבר למיפויי MacPorts.\n**מצב:** פתוחה; Ansible/Pandoc במסלולים קיימים אינם הוכחה למנגנון הכללי.\n\n- [ ] להגדיר מודל הצעה משותף: זהות תוכנה, בעלים נוכחי, ספק מבצע, גרסאות,\n  התאמה למערכת, סגירת תלויות, זמינות בינארית, יכולת ביצוע ונימוק בחירה.\n- [ ] לחבר Homebrew bottles, MacPorts archives ו־Pixi osx-64 למודל זה;\n  לפתור תלויות ולאמת קבצים ומטא־נתונים, תוך שמירת כשל ספק כמידע חלקי.\n- [ ] לבחור את הגרסה המתאימה החדשה ביותר מבין המסלולים שניתנים לביצוע;\n  בשוויון לשמר בעלים, בלי downgrade ובלי השוואת revisions בין ספקים.\n- [ ] לחבר ביצוע Pixi למנגנון המעבר ולשמור סביבות ונתיבים קיימים.\n- [ ] לשמור בעלות ולבדוק עדכון עתידי גם כשאין עותק Homebrew.\n\n**תנאי השלמה:** סריקה רגילה מציגה בחירה ונימוק; עדכון נבחר מתבצע בספק שנבחר,\nונשמר לאחר פתיחה מחדש. חבילה חסומה לא מפילה חבילות עם מסלול תקין.\n\n## T05 — ספקים רשמיים עצמאיים\n\n**מניע:** לתת מענה עתידי כשמנהלי החבילות אינם מספקים מסלול Intel מתאים.\n**מצב:** פתוחה, אחרי זרימת העדכון המרכזית; אין הרחבת מימוש אוטומטית במסגרת T03.\n\n- [ ] לבדוק מסלול רשמי רק לחבילה שנדרש עבורה פתרון; Nmap הוא ממצא היסטורי.\n- [ ] להגדיר גילוי גרסה, אימות קובץ, התקנה, שימוש, בעלות ועדכון עתידי.\n\n**תנאי השלמה:** מסלול נתמך שנבדק מקצה לקצה; קישור הורדה אינו מסלול שהושלם.\n\n## T06 — טיפול בחסמים קיימים\n\n**מניע:** לטפל במקרים שהמנגנון הכללי עדיין אינו יכול לבצע ולהציג סיבה מדויקת.\n**מצב:** פתוחה. הממצאים הבאים היסטוריים ודורשים אימות לפני שינוי התקנות.\n\n- [ ] LLVM: בדוח 1.3.2 הקג הפעיל 22.1.5, עותק 23.1.3_2 ללא receipt,\n  ו־clang נכשל בגלל Z3 חסר. תיקון נפרד; לא להחליף ספריות ABI באופן שרירותי.\n- [ ] Poppler/Pixi: תועד פתרון תלויות osx-64, לא התקנה או בדיקת שימוש.\n- [ ] libunistring: לבחון מחדש חסם משותף לחבילות שנמצאו, בלי ערבוב ספקי ספריות.\n- [ ] לאמת זמינות ומיפויים של nmap, socat, libtensorflow, merve ו־whisper.cpp.\n- [ ] לספריות ושירותים להכין טיפול בתלויים, ABI, הגדרות ונתונים לפני ביצוע.\n\n**תנאי השלמה:** לכל מקרה עדכני יש פעולה שהוכחה או חסם מתוארך ופעולה הבאה.\nאין חובה מלאכותית למצוא עדכון לכל התקנה או להעביר ספרייה במסלול כלי עצמאי.\n\n## T07 — עדכון נבחרים/הכול וסקריפט בדיקות אחד\n\n**מניע:** המשתמש מפעיל בדיקה ועדכון; המערכת מטפלת בבחירת ספק ובמעבר.\n**מצב:** פתוחה. כפתור ״עדכן הכול״ הקיים אינו מממש עדיין את הזרימה הכללית.\n\n- [ ] להציג ברשימה אחידה מקור, יעד, ספק מבצע, סיבת בחירה ומעבר מתוכנן.\n- [ ] להעביר את כל החבילות הנבחרות לאותו מתכנן וביצוע, גם כשנדרש מעבר ספק.\n- [ ] הלחיצה על עדכון מאשרת את הפעולות המוצגות; לבטל דרישת אישור מעבר נפרד.\n  שינוי מהותי מאז התצוגה יוחזר לסקירת הבחירה המעודכנת לפני שינוי התקנות.\n- [ ] להציג חסם רק לחבילה המושפעת, ולשמר תוצאה סופית, ביטול ושתי שפות.\n- [ ] להכין פקודה אחת לבדיקות המלאות שהמשתמש מריץ: בדיקות קוד מבודדות,\n  HTTP, ממשק ורגרסיות; ואימות אמיתי לאחר הצגת הפעולות ואישור כולל אחד.\n- [ ] לשמור דוח וחתימות, להפריד סימולציה ואימות מחשב, ולא לסמן דילוג כעובר.\n- [ ] לקשור דוח לתוכן שנבדק; לבנות/להתקין רק בהתאם למצב בדיקות מוכח,\n  לשמור אפליקציה והגדרות קודמות, ולא להחליף בזמן פעולה פעילה.\n\n**תנאי השלמה:** דוח המשתמש מוכיח בדיקה → בחירה → עדכון במספר ספקים → סיכום →\nפתיחה מחדש ועדכון אצל הספק הנכון. לשמר החרגות, היסטוריה וזהות התראות.\nהסקריפט הנוכחי של IWYU אינו בדיקת קבלה כזו; אין למסור את פקודתו כהשלמת היעד.\n\n## סדר העבודה הבא\n\n1. להגדיר מודל הצעה ובחירה ובעלות משותף (T04/T03), תוך שימוש בקוד שכבר קיים.\n2. להשלים הפעלה ושחזור במנוע לפני הסרת מקור אמיתית (T03).\n3. לחבר את הבחירה והמעבר לכפתורי עדכן נבחרים/הכול (T07), ואז להשלים ספק Pixi (T04).\n4. להכין את הבדיקות והסקריפט יחד עם המימוש; הסוכן מבצע אימות ממוקד בלבד.\n5. למסור למשתמש פקודה אחת, לקרוא את הדוח ולטפל בכשלים; לסגור משימות לפי הראיות.\n6. לקדם חסמים T06 לפי צורך. T05 בהמשך. לא להחליף מטרה בפיילוט נוסף.\n\n## ראיות והמשך בשיחה אחרת\n\n- [דוח T01/T02](T01_T02_VALIDATION.md): בדיקות 1.3.2 ותוצאות 239 התקנות\n  ו־28 מועמדות מהסריקה שתועדה. אלה אינם נתוני מלאי חיים.\n- [דוח T03](T03_VALIDATION.md): מה נוסף ב־1.3.3, מה טרם הוכח ומגבלות הסקריפט.\n- [מלאי 19.10.2026](audit-2026-10-19/README.md) ודוחות binary-preflight,\n  macports-binary-coverage: תמונות מצב היסטוריות, לא החלטות ביצוע נוכחיות.\n- נתוני האפליקציה: `~/Library/Application Support/Homebrew Update Manager/`.\n- גיבויים ודוחות: `~/.local/share/brew-update-manager/recovery/`.\n\nבשיחה הבאה לקרוא PRODUCT_GOAL.md, מסמך זה ו־MAC_MANAGER.md, הוראות AGENTS\nומצב Git. לשמר שינויים קיימים; לא להפעיל חבילות ישנות מעל הקבצים הנוכחיים.\nלקרוא את דוח המשתמש כשישנו ולהמשיך מהפער המוכח, ללא בדיקות מלאות חוזרות.\nאין לבצע מעבר אמיתי בזמן פיתוח. מסירה מקומית בלבד, בלי push/release.\n\nעדכון התיעוד הנוכחי מופק בפקודת `direction-docs-apply` של הגנרטור, עם גיבוי\nומניפסט נפרדים תחת `recovery/documentation-direction-2026-10-20/`.\nהוא אינו משנה מניפסטי bundles 001/002/003. בדיקות ישנות שדורשות hash התיעוד\nהמקורי עשויות לעצור בצדק; יש להתאים חבילת מימוש חדשה, לא לעקוף את ההגנה.\n\n## תוכנית המימוש הפעילה ודוח הבסיס — 20.10.2026\n\n[תוכנית מימוש מפורטת ל־T03/T04/T07](INTEL_UPDATE_IMPLEMENTATION_PLAN.md)\nמגדירה קבצים, חוזים, אלגוריתם בחירה, עסקה ושחזור, UI, בדיקות וסדר מסירה.\nהיא תוכנית לביצוע, לא דיווח על השלמת המוצר.\n\nהמשתמש הריץ את סקריפט הבדיקות: כל 19 השלבים עברו, כולל 83 בדיקות Python.\n[דוח הריצה](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t03-1.3.3/checks-1792496033916688000/summary.json).\nבקריאת הדוח חתימות 42 קובצי הקוד התאימו. לא בוצע מעבר חבילות אמיתי.\nהסוכן לא הריץ את החבילה מחדש. דוח זה הוא בסיס לקוד 1.3.3, לא בדיקה לשינויי התוכנית.\nT03/T04/T07 נשארות פתוחות. הפקודה --full במסמך היא יעד מימוש עתידי ואינה קיימת עדיין.\n\n\n## עדכון מימוש בענף 1.3.4 (20.10.2026)\n\nקוד עבודה בענף \\`feat/unified-intel-134-foundations\\`, ב־PR טיוטה מס' 1.\nזו **אינה גרסה מותקנת או מוכנה להפצה**. הנתונים של משתמש ה־Mac אינם שונו\nבידי הסוכן. ה־CI מבצע בדיקות תחביר ו־packaging smoke בלבד, לא את בדיקות\nה־Python/Electron המלאות או מעבר הספק האמיתי.\n\nקיים בקוד:\n- בחירת גרסה וספק עם החלטות שמורות, סיבות, 15 דקות תוקף ו־fingerprint.\n- כיסוי עדכונים בינאריים של Homebrew formula, MacPorts וכלים מנוהלים ב־Pixi.\n- ניהול בעלות על התקנות, נעילת פעולות בין תהליכים, ורישום request IDs למניעת\n  הפעלה כפולה אחרי ניתוק.\n- תשתית מעבר Homebrew ל־MacPorts לפרופילים מוכרים, עם גיבוי קג, יומן,\n  בדיקת שימוש, ניתוב פקודות שמגן על קבצים זרים וסקירת resume/rollback.\n- UI אחיד, תוכניות API, כפתור פעולה אחד, ותוצאות עם מזהי בקשה.\n- סקריפט \\`tools/verify-migration.py --checks/--full\\` לגרסה 1.3.4.\n  \\`--full\\` יכול להחליף את האפליקציה המקומית ודורש YES לשינוי חבילות.\n\nעדיין לא הוכח / נשאר לבדיקה או להשלמה:\n- קבלת Mac אמיתית של IWYU אחרי החלפה, ניקוי, שחזור והפעלה מחדש.\n- פיילוט Pixi אמיתי; מעבר חדש מ־Homebrew ל־Pixi אינו מופעל עדיין\n  כעסקה נתמכת (עדכון של סביבת Pixi מנוהלת קיים בנפרד).\n- קבלה מעמיקה של Cask ובדיקת closure/metadata לכל שאר החבילות.\n- רגרסיות HTTP/renderer, תרחישי כשל מלאים ו־restore של bundle 004.\n- חתימות קוד/חבילה, ניתוב ב־login shell, הרשאת מנהל ו־recovery נבדקים\n  בקבלה המקומית; קיומן של הפונקציות אינו הוכחת התנהגותן ב־Mac.\n\n**אין לסגור T03/T04/T07** לפני דוח \\`--checks\\` מלא ודוח \\`--full\\`\nשמפריד הצלחה מדמה מקבלה אמיתית. אין לאשר מעבר בלתי מזוהה או לפרסם release.\n\n\n## סטטוס עדכני בענף 1.3.4 (20.10.2026)\n\nהטבלה והמשימות למעלה משמרות **תמונת בסיס 1.3.3**; יש להבחין בין מה שכבר קיים בקוד בענף לבין מה שנבדק על ה־Mac. [דוח המימוש העדכני](UNIFIED_IMPLEMENTATION_STATUS.md) קובע את המצב.\n\n- T03: מנוע גיבוי/ניתוב/שחזור קיים לכלים עם פרופיל; חסרות בדיקות תקלה וקבלה אמיתית.\n- T04: adapters תלת־ספקיים קיימים, אך הסריקה אינה אוספת כל חלופה לכל מקור; Homebrew→Pixi עדיין אינו מסלול מעבר מבצע.\n- T07: UI/API מאוחדים קיימים אך legacy נותר, ויש להשלים רגרסיות, תרגומי שגיאות ו־409.\n- תיקוני תוקף תוכנית `0f2f999`/`855a995` אושרו ב־[CI ממוקד](https://github.com/TzoharLary/brew-update-manager/actions/runs/39449649595): 10 בדיקות Python, בדיקת JS, בדיקת מחולל ואריזה בסיסית. זו אינה בדיקת קבלה.\n- פקודת `--full` קיימת ב־1.3.4 אך אין לפרסם או להריץ כקבלה מאושרת לפני השלמת קוד ובדיקות.\n",
    "docs/MAC_MANAGER.md": "# Mac Update Manager — implementation and product direction\n\nUpdated 2026-10-20. Source version: 1.3.3. This documentation-only review did not\nquery a live service or rerun product tests. The installed build and package state\nmust not be inferred from package.json alone.\n\nRead [the product goal](PRODUCT_GOAL.md) for the user's current requirements and\n[the task record](MAC_MANAGER_TASKS.md) for status and acceptance criteria. Those\ndecisions replace earlier designs requiring a separate approval for each migration\nor full test runs by the coding agent before handing the user a test script.\n\n## Required user flow — not yet complete\n\nStart Check discovers updates and compares supported providers for native Intel\nmacOS. Select the newest suitable upstream version with a ready binary runtime\nclosure. Retain the existing owner on an otherwise equal route. Homebrew, MacPorts\nand Pixi participate as their execution routes are implemented and verified.\n\nShow installed/target versions, current/executing provider and the reason. A source\nprovider change is visible before the user clicks Update selected/all. That click\nauthorizes the displayed actions; there is no separate per-package migration\nwizard. macOS may still require administrator credentials. Revalidate before any\nchange; a materially changed proposal returns for updated selection.\n\nAfter successful migration, persist provider ownership and discover future versions\nfrom that provider even without the Homebrew copy. Unknown metadata is unverified,\nnot absent. Unsupported migration blocks the affected package with a reason.\nNever silently build from source, downgrade, or discard exclusions and user data.\n\n## Existing inventory and update paths\n\nHomebrew formulae/casks, active MacPorts ports and existing isolated tools share\nan inventory. `kind=port` identifies MacPorts in the selection API; legacy\nformula/cask requests remain Homebrew identities. Existing Pixi-managed tools\ndo not establish general Pixi alternative discovery or migration support.\n\nMacPorts preflight resolves runtime archives for the current OS/architecture and\nchecks archive/signature availability. Installation uses MacPorts' binary mode\nand signature verification. Availability alone does not prove a working migration.\nCatalog refresh and cache handling exist; provider freshness and error behavior\nstill need regression coverage when integrated into the common selection policy.\n\nCombined scans report provider stages and preserve partial errors. Version 1.3.2\nintroduced final progress in operation responses, retained terminal results,\nprotection against old responses and unverified status after communication loss.\nInventory evidence separates manager versions, active keg, receipt, PATH and\nupstream/revision. Only explicitly configured commands, initially LLVM clang,\nare probed. Evidence conflicts block affected decisions.\n\nT02 classifies candidate roles using receipts, reverse dependencies, files,\ninterfaces, services and bounded linkage checks. Private projects are not scanned.\nEvidence is stored in `migration-evidence` under the application data directory.\nA standalone classification does not itself authorize execution.\n\n## Existing migration APIs — internal/legacy path\n\n- `POST /api/migrations/plan {name}` creates a proposal.\n- `POST /api/migrations/approve {id}` executes a supported proposal.\n- `POST /api/migrations/recovery-plan {transaction_id, action}` creates a recovery\n  proposal; the current engine does not execute resume/rollback through approve.\n- `POST /api/macports/sync` requests catalog refresh with macOS authorization.\n\nThe old terminal-notifier implementation remains separate. The 1.3.3 additions\ndelegate other names to `migration_engine.py`; `migration_profiles.py` defines\nterminal-notifier and include-what-you-use profiles. This is partial groundwork,\nnot a completed common transaction engine. Current update-all does not perform\ngeneral cross-provider migrations. See [the T03 review](T03_VALIDATION.md).\n\nThe current engine writes plans and transactions under `DATA_DIR/migrations/`\nand a route record under `TOOLS_ROOT/migrations/routes.json`. A written route\ndoes not prove shims were activated or future provider upgrades are integrated.\nDo not put MacPorts ownership into the legacy installed.json tool registry and\nthereby manufacture a duplicate installed tool.\n\nThe existing notification helper, permission identity, app/data/profile identity,\nlaunch agent, exclusions (including ngrok), settings and history must remain stable.\n\n## Validation and delivery\n\nThe coding agent implements code/tests and performs only focused lightweight\nchecks appropriate to the edit. The user runs the full suite and acceptance\nscript; inspect its saved report and avoid rerunning unchanged suites needlessly.\nFull unrun checks must be reported as pending. Isolated checks must not mutate\nreal packages; real acceptance shows the actions and asks for one overall approval.\n\n`tools/verify-migration.py --checks` now calls `verify-current-code.py` directly,\nindependent of old bundle manifests, and saves per-step logs and a summary.\nThe full suite awaits the user run. The helper also has an IWYU-only acceptance mode. It is not yet the complete script for the required product flow. Its\nlimitations and the distinction between historical runs and future validation\nare recorded in T03_VALIDATION.md. Do not recommend a real migration until its\npreflight, activation, rollback and acceptance gaps are resolved.\n\n[The 1.3.2 validation report](T01_T02_VALIDATION.md) preserves historical evidence;\nit does not certify later edits. Local delivery only; no push or release is implied.\n\n## Generator and recovery\n\nProduct, tests and documentation are generated through\n`tools/implement-mac-manager.py`; edit that script with apply_patch and run terminal\ncommands interactively. Preserve dirty work. Never replay legacy apply or\ndocument-tasks over a newer bundle, reset README, or overwrite recovery manifests\nto silence a precondition failure.\n\nThe 2026-10-20 direction update uses `direction-docs-check`, `direction-docs-apply`\nand `direction-docs-restore`. These touch documentation only, with a separate\nbackup and manifest under `recovery/documentation-direction-2026-10-20/`.\nRestore refuses subsequent independent edits. Existing bundles keep their old\nmanifests; their hash checks may correctly reject these newer documentation\nfiles. Adapt the next implementation/verification bundle to the current baseline.\n\n\n## Branch 1.3.4 — implementation in progress\n\nSee [unified implementation status](UNIFIED_IMPLEMENTATION_STATUS.md). The installed 1.3.3 application is unchanged; source and fixtures on the feature branch have not passed user acceptance.\n\n\n## Current 1.3.4 development branch (2026-10-20)\n\nHistorical sections above refer to 1.3.3. The [current implementation report](UNIFIED_IMPLEMENTATION_STATUS.md) takes precedence: 1.3.4 already has guarded provider decisions, managed Pixi updates, limited MacPorts migrations, and a `--full` runner **written but not accepted**. Cached-plan expiry is fail-closed. [Focused CI #39449649595](https://github.com/TzoharLary/brew-update-manager/actions/runs/39449649595) passed 10 Python tests, JS helper, bundle no-write check, syntax and packaging smoke only. Full provider discovery, removal of legacy bypass paths, failure suites and native Mac acceptance remain open. No release or local install.\n",
    "backend/update_routes.py": "\"\"\"Binary tool routes. Never replace libraries inside Homebrew's prefix.\"\"\"\nfrom __future__ import annotations\n\nimport json\nimport os\nfrom pathlib import Path\nimport re\nimport shlex\nimport time\n\n\n# Deliberately limited to command line tools; databases, compilers and shared\n# libraries need a project/data migration rather than an executable replacement.\nPIXI_TOOLS = {\n    \"ansible\": (\"ansible\", [\"ansible\", \"ansible-playbook\", \"ansible-galaxy\", \"ansible-vault\"], [\"ansible\", \"--version\"]),\n    \"cmake\": (\"cmake\", [\"cmake\", \"ctest\", \"cpack\"], [\"cmake\", \"--version\"]),\n    \"deno\": (\"deno\", [\"deno\"], [\"deno\", \"--version\"]),\n    \"doxygen\": (\"doxygen\", [\"doxygen\"], [\"doxygen\", \"--version\"]),\n    \"ffmpeg\": (\"ffmpeg\", [\"ffmpeg\", \"ffprobe\"], [\"ffmpeg\", \"-version\"]),\n    \"gh\": (\"gh\", [\"gh\"], [\"gh\", \"--version\"]),\n    \"git\": (\"git\", [\"git\", \"git-upload-pack\", \"git-receive-pack\"], [\"git\", \"--version\"]),\n    \"jq\": (\"jq\", [\"jq\"], [\"jq\", \"--version\"]),\n    \"node\": (\"nodejs\", [\"node\", \"npm\", \"npx\"], [\"node\", \"--version\"]),\n    \"pandoc\": (\"pandoc\", [\"pandoc\"], [\"pandoc\", \"--version\"]),\n    \"poppler\": (\"poppler\", [\"pdftotext\", \"pdfinfo\", \"pdftoppm\", \"pdftocairo\", \"pdfseparate\", \"pdfunite\", \"pdfimages\", \"pdffonts\"], [\"pdfinfo\", \"-v\"]),\n    \"tesseract\": (\"tesseract\", [\"tesseract\"], [\"tesseract\", \"--version\"]),\n    \"uv\": (\"uv\", [\"uv\", \"uvx\"], [\"uv\", \"--version\"]),\n    \"whisper.cpp\": (\"whisper.cpp\", [\"whisper-cli\"], [\"whisper-cli\", \"--help\"]),\n}\n\n\ndef version_key(value):\n    return tuple(int(x) for x in re.findall(r\"\\d+\", str(value).split(\"_\")[0]))\n\n\ndef package_version(value):\n    version = str(value).split(\"_\")[0]\n    if not re.fullmatch(r\"[0-9][0-9A-Za-z.+-]*\", version):\n        raise ValueError(f\"Unsupported version constraint: {value}\")\n    return version\n\n\ndef read_records(root: Path):\n    try:\n        return json.loads((root / \"installed.json\").read_text())\n    except FileNotFoundError:\n        return {}\n\n\ndef route_for(name, kind, settings):\n    if kind != \"formula\":\n        return \"homebrew\"\n    enabled = settings.get(\"binary_tools\", [])\n    if name not in enabled:\n        return \"homebrew\"\n    if name == \"azure-cli\":\n        return \"uv\"\n    if name in {\"node\", \"deno\"}:\n        return \"native\"\n    return \"pixi\" if name in PIXI_TOOLS else \"homebrew\"\n\n\ndef prepare_workspace(root: Path, name, version, platform):\n    package_version(version)\n    directory = root / \"environments\" / name / str(time.time_ns())\n    directory.mkdir(parents=True)\n    conda_name = PIXI_TOOLS[name][0]\n    manifest = directory / \"pixi.toml\"\n    manifest.write_text(\n        '[workspace]\\nname = \"managed-tool\"\\nchannels = [\"conda-forge\"]\\n'\n        f'platforms = [\"{platform}\"]\\n\\n[dependencies]\\n'\n        f'{json.dumps(conda_name)} = {json.dumps(\">=\" + package_version(version))}\\n'\n        + ('python = \"3.14.*\"\\n' if name == \"ansible\" else '')\n    )\n    return directory, manifest\n\n\ndef validate_lock(manifest: Path, python: str, run, env):\n    # Pixi can export a machine-readable, explicit set without a YAML dependency.\n    export_dir = manifest.parent / \"explicit\"\n    result = run([env[\"PIXI_BIN\"], \"workspace\", \"export\", \"conda-explicit-spec\", str(export_dir),\n                  \"--manifest-path\", str(manifest), \"--no-config\"], timeout=120, env_override=env)\n    if result.code:\n        raise RuntimeError(result.stderr or result.stdout)\n    urls = [line.strip() for path in export_dir.glob(\"*\") if path.is_file()\n            for line in path.read_text().splitlines() if line.startswith(\"https://\")]\n    if not urls or any(not url.startswith(\"https://conda.anaconda.org/conda-forge/\")\n                       or not any(f\"/{subdir}/\" in url for subdir in (env[\"PIXI_PLATFORM\"], \"noarch\"))\n                       for url in urls):\n        raise RuntimeError(\"Lock does not consist exclusively of trusted platform binaries\")\n    return urls\n\n\ndef activate(root: Path, name, provider, directory: Path, prefix: Path, version, executables, probe_output):\n    \"\"\"Publish a complete tool update only after ownership of every shim is proved.\n\n    The installed.json record and the exact generated wrapper bytes form the\n    legacy ownership manifest; arbitrary files and broken links are blocked.\n    \"\"\"\n    import hashlib\n    from provider_ownership import atomic_write_json\n    root = Path(root)\n    bin_dir = root / \"bin\"\n    active_dir = root / \"active\"\n    link = active_dir / name\n    prefix = Path(prefix)\n    available = list(dict.fromkeys(executables))\n    if not available or not all((prefix / \"bin\" / command).is_file() for command in available):\n        raise RuntimeError(\"Provider did not supply all mandatory tool commands\")\n    if name not in PIXI_TOOLS and provider == \"pixi\":\n        raise RuntimeError(\"Unmapped Pixi package\")\n    if name == \"ngrok\":\n        raise RuntimeError(\"Excluded package\")\n    records = read_records(root)\n    previous = records.get(name)\n    if os.path.lexists(link):\n        if not link.is_symlink() or not previous or os.readlink(link) != previous.get(\"prefix\"):\n            raise RuntimeError(\"Active tool routing is not owned by the manager\")\n    elif previous and previous.get(\"prefix\"):\n        raise RuntimeError(\"Prior managed active pointer is missing\")\n    if previous and set(previous.get(\"executables\", [])) != set(available):\n        raise RuntimeError(\"Existing command set differs from required profile\")\n    def wrapper(command):\n        return ('#!/bin/sh\\n' +\n                f'export CONDA_PREFIX={shlex.quote(str(link))}\\n' +\n                f'export PATH={shlex.quote(str(link / \"bin\"))}:{shlex.quote(str(bin_dir))}:\"$PATH\"\\n' +\n                f'exec {shlex.quote(str(link / \"bin\" / command))} \"$@\"\\n')\n    import hashlib\n    for command in available:\n        destination = bin_dir / command\n        if not os.path.lexists(destination):\n            continue\n        if not previous or destination.is_symlink() or not destination.is_file():\n            raise RuntimeError(\"Unmanaged tool shim conflict: \" + str(destination))\n        raw = destination.read_bytes()\n        actual = hashlib.sha256(raw).hexdigest()\n        old_hash = (previous.get(\"owned_shims\") or {}).get(command)\n        legacy = hashlib.sha256(wrapper(command).encode()).hexdigest()\n        if actual != old_hash and not (old_hash is None and actual == legacy):\n            raise RuntimeError(\"Independently changed managed shim: \" + command)\n    bin_dir.mkdir(parents=True, exist_ok=True)\n    active_dir.mkdir(parents=True, exist_ok=True)\n    staged_wrappers = []\n    try:\n        for command in available:\n            destination = bin_dir / command\n            staged = destination.parent / (\".\" + command + \".\" + str(time.time_ns()) + \".next\")\n            staged.write_text(wrapper(command))\n            staged.chmod(0o755)\n            staged_wrappers.append((staged, destination))\n        staged_active = active_dir / (\".\" + name + \".\" + str(time.time_ns()) + \".next\")\n        staged_active.symlink_to(prefix)\n        # Existing shims execute through link, so switch only after all\n        # destinations and preconditions are rechecked.\n        for command in available:\n            destination = bin_dir / command\n            if os.path.lexists(destination):\n                prior_hash = (previous or {}).get(\"owned_shims\", {}).get(command)\n                actual_hash = hashlib.sha256(destination.read_bytes()).hexdigest()\n                if destination.is_symlink() or (prior_hash and actual_hash != prior_hash) or (\n                        not prior_hash and actual_hash != hashlib.sha256(wrapper(command).encode()).hexdigest()):\n                    raise RuntimeError(\"Shim changed during installation\")\n        for staged, destination in staged_wrappers:\n            os.replace(staged, destination)\n        os.replace(staged_active, link)\n        record = {\"provider\": provider, \"version\": version, \"prefix\": str(prefix),\n                  \"workspace\": str(directory), \"executables\": available,\n                  \"owned_shims\": {c: hashlib.sha256((bin_dir / c).read_bytes()).hexdigest()\n                                  for c in available},\n                  \"verified_at\": time.time(), \"probe_output\": probe_output[-3000:]}\n        records[name] = record\n        atomic_write_json(root / \"installed.json\", records)\n        return record\n    finally:\n        for staged, _ in staged_wrappers:\n            if os.path.lexists(staged):\n                staged.unlink()\n\n\ndef bottle_blockers(name, formulae, installed, excluded):\n    \"\"\"Conservative runtime closure from Homebrew's platform-selected API.\n\n    Missing metadata, non-default options, and unavailable runtime bottles fail\n    closed. Build-only dependencies are unnecessary when pouring a bottle.\n    \"\"\"\n    blockers = []\n    visited = set()\n\n    def visit(current, root=False):\n        if current in visited:\n            return\n        visited.add(current)\n        if f\"formula:{current}\" in excluded:\n            blockers.append({\"name\": current, \"reason\": \"excluded_dependency\"})\n            return\n        row = formulae.get(current)\n        if not row:\n            blockers.append({\"name\": current, \"reason\": \"metadata_unavailable\"})\n            return\n        old = installed.get(current, {})\n        latest = str(row.get(\"stable_version\", \"\"))\n        revision = int(row.get(\"revision\") or 0)\n        if revision:\n            latest += f\"_{revision}\"\n        if not root and latest in old.get(\"versions\", []):\n            return\n        if old.get(\"options\"):\n            blockers.append({\"name\": current, \"reason\": \"custom_build_options\"})\n        if not row.get(\"bottle_checksum\") or not row.get(\"bottle_tag\"):\n            blockers.append({\"name\": current, \"reason\": \"source_required\"})\n        for dependency in row.get(\"stable_dependencies\", []):\n            if isinstance(dependency, str):\n                visit(dependency)\n            elif isinstance(dependency, dict):\n                for dep, category in dependency.items():\n                    if category not in (\":build\", \":test\", \":optional\"):\n                        visit(dep)\n\n    visit(name, root=True)\n    return blockers\n",
    "tools/verify-current-code.py": "#!/usr/bin/env python3\n\"\"\"Run existing isolated source checks and save reports; never migrate packages.\"\"\"\nfrom __future__ import annotations\nimport ast\nimport hashlib\nimport json\nimport os\nfrom pathlib import Path\nimport shutil\nimport signal\nimport subprocess\nimport sys\nimport tempfile\nimport time\n\nROOT = Path(__file__).resolve().parents[1]\nREPORTS = Path.home() / '.local/share/brew-update-manager/recovery/unified-intel-1.3.4/runs'\n\ndef syntax():\n    files = sorted(p for folder in ('backend', 'tools', 'tests')\n                   for p in (ROOT / folder).rglob('*.py') if '__pycache__' not in p.parts)\n    for path in files:\n        ast.parse(path.read_bytes(), filename=str(path))\n    print(f'Python syntax: {len(files)} files OK')\n\ndef main():\n    out = REPORTS / ('checks-' + str(time.time_ns()))\n    out.mkdir(parents=True)\n    sources = sorted(p for folder in ('backend', 'tools', 'tests', 'renderer', 'electron')\n                     for p in (ROOT / folder).rglob('*')\n                     if p.is_file() and '__pycache__' not in p.parts\n                     and p.suffix in {'.py', '.js', '.cjs', '.html', '.css', '.json', '.tcl'})\n    sources += [ROOT / 'package.json', ROOT / 'package-lock.json']\n    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()\n              for p in sorted(set(sources))}\n    report = {'mode': 'isolated_code_checks', 'real_migration': False,\n              'started_at': time.time(), 'code_sha256': hashes, 'steps': [],\n              'scope': 'Existing source suites only; no installed-app or real-migration acceptance'}\n    (out / 'code-sha256.json').write_text(json.dumps(hashes, indent=2) + '\\n')\n    def save():\n        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\\n')\n        lines = ['# דוח בדיקות קוד מבודדות', '', 'תוצאה: ' + report.get('result', 'running'),\n                 '', 'לא בוצעו מעבר, עדכון או הסרה של חבילות.',\n                 'הדוח אינו בדיקת קבלה לאפליקציה המותקנת או למעבר ספק אמיתי.', '']\n        lines += [f\"- {s['name']}: {s['status']} ({s.get('log', '')})\" for s in report['steps']]\n        if report.get('error'): lines += ['', report['error']]\n        (out / 'summary.md').write_text('\\n'.join(lines) + '\\n')\n    save()\n    print('דוח ולוגים:', out, flush=True)\n    node = shutil.which('node')\n    electron = ROOT / 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'\n    if not node or not electron.is_file():\n        report.update(result='prerequisite_missing', error='Node or local Electron is missing; no dependency installation was attempted')\n        save()\n        return 2\n    js = sorted(p for folder in ('electron', 'renderer', 'tools', 'tests')\n                for p in (ROOT / folder).rglob('*') if p.suffix in {'.js', '.cjs'})\n    commands = [('python-syntax', [sys.executable, str(Path(__file__).resolve()), '--syntax-only'], 60),\n                ('python-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'], 600)]\n    commands += [('syntax-' + str(p.relative_to(ROOT)).replace('/', '-'), [node, '--check', str(p)], 30) for p in js]\n    commands += [('http', [node, str(ROOT / 'tests/service-http.cjs')], 90),\n                 ('progress-ui', [str(electron), str(ROOT / 'tests/progress-ui.cjs')], 120),\n                 ('operation-results-ui', [str(electron), str(ROOT / 'tests/operation-results-ui.cjs')], 120),\n                 ('whitespace', ['git', 'diff', '--check'], 30)]\n    code = 0\n    with tempfile.TemporaryDirectory(prefix='mac-manager-checks-') as temporary:\n        env = {**os.environ, 'BREW_UPDATE_DATA_DIR': temporary, 'PYTHONDONTWRITEBYTECODE': '1'}\n        for name, argv, timeout in commands:\n            step = {'name': name, 'argv': argv, 'status': 'running', 'log': name + '.log'}\n            report['steps'].append(step)\n            save()\n            print('בודק:', name, flush=True)\n            process = None\n            started = time.monotonic()\n            try:\n                with (out / step['log']).open('wb') as log:\n                    process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log,\n                                               stderr=subprocess.STDOUT, start_new_session=True)\n                    while True:\n                        remaining = timeout - (time.monotonic() - started)\n                        if remaining <= 0: raise subprocess.TimeoutExpired(argv, timeout)\n                        try:\n                            result = process.wait(timeout=min(20, remaining))\n                            break\n                        except subprocess.TimeoutExpired:\n                            print(f'עדיין בודק: {name} ({int(time.monotonic() - started)} שניות)', flush=True)\n                step.update(returncode=result, status='pass' if result == 0 else 'fail')\n                if result != 0: code = 1\n            except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:\n                if process is not None and process.poll() is None:\n                    # Only the isolated subprocess group created above belongs to us.\n                    os.killpg(process.pid, signal.SIGTERM)\n                    try: process.wait(timeout=5)\n                    except subprocess.TimeoutExpired:\n                        os.killpg(process.pid, signal.SIGKILL)\n                        process.wait()\n                step.update(status='unverified', error=str(error) or 'Interrupted by user')\n                code = 2\n            step['elapsed_seconds'] = round(time.monotonic() - started, 2)\n            save()\n            if code == 2: break\n        completed = {s['name'] for s in report['steps']}\n        for name, argv, _ in commands:\n            if name not in completed:\n                report['steps'].append({'name': name, 'argv': argv, 'status': 'not_run'})\n    changed = [rel for rel, sha in hashes.items()\n               if not (ROOT / rel).is_file() or hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() != sha]\n    if changed:\n        report['error'] = 'Source files changed during checks: ' + ', '.join(changed)\n        code = 2\n    report.update(finished_at=time.time(), exit_code=code,\n                  result={0: 'pass', 1: 'fail', 2: 'unverified'}[code])\n    save()\n    print('הבדיקות הסתיימו:', report['result'], '\\nדוח:', out, flush=True)\n    return code\n\nif __name__ == '__main__':\n    if sys.argv[1:] == ['--syntax-only']: syntax()\n    elif sys.argv[1:]: raise SystemExit('This runner accepts no arguments')\n    else: raise SystemExit(main())\n",
    "tools/verify-migration.py": "#!/usr/bin/env python3\n\"\"\"User-operated 1.3.4 Intel code checks, packaged-app and update acceptance.\n\nThe agent does not run this command. --full is authorization to replace only\nthe local Mac Update Manager app; package mutations still need one explicit\nYES tied to the displayed immutable plan.\n\"\"\"\nfrom __future__ import annotations\n\nimport argparse\nimport hashlib\nimport json\nimport os\nfrom pathlib import Path\nimport platform\nimport plistlib\nimport shutil\nimport subprocess\nimport sys\nimport threading\nimport time\nimport urllib.error\nimport urllib.request\nimport uuid\n\nROOT = Path(__file__).resolve().parents[1]\nRECOVERY = Path.home() / \".local/share/brew-update-manager/recovery/unified-intel-1.3.4\"\nAPPLICATION = Path(\"/Applications/Mac Update Manager.app\")\nBUILT = ROOT / \"dist/mac/Mac Update Manager.app\"\nAPP_ID = \"com.tzoharlary.brewupdatemanager\"\nVERSION = \"1.3.4\"\n\n\nclass Blocked(RuntimeError):\n    pass\n\n\ndef sha(path):\n    return hashlib.sha256(Path(path).read_bytes()).hexdigest()\n\n\ndef backend_digest(directory):\n    return hashlib.sha256(b\"\".join(\n        path.name.encode() + b\"\\0\" + hashlib.sha256(path.read_bytes()).digest()\n        for path in sorted(Path(directory).glob(\"*.py\")))).hexdigest()\n\n\ndef signature():\n    files = sorted(\n        path for folder in (\"backend\", \"electron\", \"renderer\", \"tests\", \"tools\")\n        for path in (ROOT / folder).rglob(\"*\")\n        if path.is_file() and \"__pycache__\" not in path.parts\n        and path.suffix in (\".py\", \".js\", \".cjs\", \".json\", \".html\", \".css\", \".tcl\"))\n    files += [ROOT / \"package.json\", ROOT / \"package-lock.json\"]\n    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(set(files))}\n\n\ndef unique_report():\n    path = RECOVERY / \"runs\" / (\"run-\" + str(time.time_ns()))\n    path.mkdir(parents=True)\n    return path\n\n\nclass Report:\n    def __init__(self, mode):\n        self.dir = unique_report()\n        self.data = {\n            \"schema_version\": 1, \"mode\": mode,\n            \"started_at\": time.time(), \"version\": VERSION,\n            \"code_checks\": \"not_run\", \"packaged_app\": \"not_run\",\n            \"simulated_scenarios\": \"not_run\", \"live_migration\": \"not_run\",\n            \"restart_verification\": \"not_run\", \"steps\": [],\n        }\n        self.save()\n\n    def save(self):\n        (self.dir / \"summary.json\").write_text(\n            json.dumps(self.data, ensure_ascii=False, indent=2, default=str) + \"\\n\")\n        lines = [\"# דוח בדיקת מנהל עדכוני Mac Intel\", \"\",\n                 \"גרסה: \" + VERSION, \"תוצאה: \" + self.data.get(\"result\", \"running\"), \"\"]\n        for key in (\"code_checks\", \"packaged_app\", \"simulated_scenarios\",\n                    \"live_migration\", \"restart_verification\"):\n            lines.append(\"- \" + key + \": \" + str(self.data[key]))\n        lines += [\"\", \"## שלבים\", \"\"]\n        lines += [\"- \" + x[\"name\"] + \": \" + x[\"status\"] +\n                  (\" — \" + x[\"detail\"] if x.get(\"detail\") else \"\")\n                  for x in self.data[\"steps\"]]\n        if self.data.get(\"error\"):\n            lines += [\"\", \"שגיאה: \" + self.data[\"error\"]]\n        (self.dir / \"summary.md\").write_text(\"\\n\".join(lines) + \"\\n\")\n\n    def step(self, name, status, detail=None, **extra):\n        self.data[\"steps\"].append(\n            {\"name\": name, \"status\": status, \"at\": time.time(),\n             **({\"detail\": detail} if detail else {}), **extra})\n        self.save()\n        print(name + \": \" + status + ((\" — \" + detail) if detail else \"\"), flush=True)\n\n\ndef command(args, report, name, timeout=1800):\n    destination = report.dir / (name + \".log\")\n    report.step(name, \"running\", argv=args)\n    with destination.open(\"wb\") as output:\n        try:\n            result = subprocess.run(args, cwd=ROOT, stdout=output,\n                                    stderr=subprocess.STDOUT, timeout=timeout)\n        except subprocess.TimeoutExpired as error:\n            report.step(name, \"unverified\", \"Timed out without assuming rollback\",\n                        log=str(destination))\n            raise Blocked(\"Timed out: \" + name) from error\n    if result.returncode:\n        report.step(name, \"fail\", \"See \" + str(destination),\n                    returncode=result.returncode)\n        raise RuntimeError(name + \" failed\")\n    report.step(name, \"pass\", log=str(destination))\n\n\ndef api(port, path, body=None, timeout=4):\n    url = \"http://127.0.0.1:\" + str(port) + path\n    data = json.dumps(body).encode() if body is not None else None\n    request = urllib.request.Request(\n        url, data=data,\n        headers={\"Content-Type\": \"application/json\"} if data is not None else {})\n    with urllib.request.urlopen(request, timeout=timeout) as response:\n        return json.load(response)\n\n\ndef find_service():\n    found = []\n    for port in range(8765, 8796):\n        try:\n            health = api(port, \"/health\", timeout=0.3)\n            if health.get(\"app\") == \"Mac Update Manager\" and health.get(\"pid\"):\n                found.append((port, health))\n        except (OSError, ValueError, urllib.error.URLError):\n            continue\n    if len(found) > 1:\n        raise Blocked(\"Multiple Mac Update Manager backends; cannot select one\")\n    return found[0] if found else None\n\n\ndef prerequisites(report):\n    if platform.system() != \"Darwin\" or platform.machine() != \"x86_64\":\n        raise Blocked(\"Native macOS Intel is required\")\n    if os.environ.get(\"SYSCTL_TRANSLATED\") == \"1\":\n        raise Blocked(\"Rosetta is not an Intel migration target\")\n    if sys.version_info[:2] != (3, 14):\n        raise Blocked(\"Run under verified Python 3.14\")\n    for program in (\"node\", \"git\", \"/usr/bin/ditto\", \"/usr/bin/open\", \"/usr/bin/osascript\"):\n        if not (shutil.which(program) if not program.startswith(\"/\") else Path(program).is_file()):\n            raise Blocked(\"Required existing executable missing: \" + program)\n    if not (ROOT / \"node_modules/.bin/electron-builder\").is_file():\n        raise Blocked(\"electron-builder is not installed; no dependencies were installed\")\n    report.step(\"prerequisites\", \"pass\", \"macOS Intel and existing local tooling\")\n\n\ndef matching_code_report(wanted):\n    directory = RECOVERY / \"runs\"\n    for path in sorted(directory.glob(\"*/summary.json\"), reverse=True):\n        try:\n            report = json.loads(path.read_text())\n            if (report.get(\"mode\") == \"isolated_code_checks\" and\n                    report.get(\"result\") == \"pass\" and\n                    report.get(\"code_sha256\") == wanted and\n                    all(step.get(\"status\") == \"pass\" for step in report.get(\"steps\", []))):\n                return path\n        except (OSError, ValueError):\n            continue\n    return None\n\n\ndef isolated_checks(report, *, reuse=True):\n    wanted = signature()\n    target = report.dir / \"code-manifest.json\"\n    target.write_text(json.dumps(wanted, indent=2) + \"\\n\")\n    matched = matching_code_report(wanted) if reuse else None\n    if matched is None:\n        command([sys.executable, str(ROOT / \"tools/verify-current-code.py\")],\n                report, \"isolated-code-suite\", timeout=1800)\n        matched = matching_code_report(wanted)\n        if matched is None:\n            raise Blocked(\"Code check results are missing or signed for different source files\")\n    report.data[\"code_checks\"] = \"pass\"\n    report.data[\"checks_report\"] = str(matched)\n    report.save()\n    return wanted\n\n\ndef metadata(app):\n    path = Path(app) / \"Contents/Info.plist\"\n    if not path.is_file():\n        raise Blocked(\"Application Info.plist is missing\")\n    with path.open(\"rb\") as stream:\n        info = plistlib.load(stream)\n    if info.get(\"CFBundleIdentifier\") != APP_ID:\n        raise Blocked(\"Unexpected application identifier\")\n    return info\n\n\ndef package_application(report, hashes):\n    command([str(ROOT / \"node_modules/.bin/electron-builder\"),\n             \"--dir\", \"--mac\", \"--x64\", \"--publish\", \"never\"],\n            report, \"intel-package\", timeout=1800)\n    info = metadata(BUILT)\n    if str(info.get(\"CFBundleShortVersionString\")) != VERSION:\n        raise Blocked(\"Packaged application version is not \" + VERSION)\n    backend = BUILT / \"Contents/Resources/app.asar.unpacked/backend\"\n    if not backend.is_dir():\n        raise Blocked(\"Packaged backend files are unavailable\")\n    for relative, expected in hashes.items():\n        if relative.startswith(\"backend/\"):\n            candidate = backend / relative.removeprefix(\"backend/\")\n            if not candidate.is_file() or sha(candidate) != expected:\n                raise Blocked(\"Packaged code differs: \" + relative)\n    product = {\"version\": VERSION,\n               \"backend_code_sha256\": backend_digest(backend),\n               \"packaged_path\": str(BUILT), \"application_id\": APP_ID,\n               \"build_id\": \"unified-intel-1.3.4\"}\n    (report.dir / \"build-manifest.json\").write_text(json.dumps(product, indent=2) + \"\\n\")\n    report.data[\"packaged_app\"] = \"pass\"\n    report.save()\n    return product\n\n\ndef check_idle(service):\n    if service is None:\n        return\n    port, health = service\n    if health.get(\"operation\", {}).get(\"running\"):\n        raise Blocked(\"App service has an active operation; replacement is deferred\")\n    progress = api(port, \"/api/progress\")\n    if progress.get(\"parallel_check\", {}).get(\"running\"):\n        raise Blocked(\"A parallel scan is active; replacement is deferred\")\n    if progress.get(\"progress\", {}).get(\"running\"):\n        raise Blocked(\"An update is still running\")\n    if health.get(\"capabilities\") is None:\n        raise Blocked(\"Running service identity could not be verified\")\n\n\ndef backup_user_state(report, service):\n    if service:\n        data = Path(service[1].get(\"data_dir\") or \"/nonexistent\")\n    else:\n        data = ROOT / \"data\" if (ROOT / \"data\").is_dir() else Path.home() / \"Library/Application Support/Homebrew Update Manager\"\n    paths = [data / name for name in (\"settings.json\", \"update-history.json\",\n                                      \"last-update-run.json\", \"state.json\")]\n    paths += [Path.home() / \".local/share/brew-update-manager/migrations/routes.json\",\n              Path.home() / \".local/share/brew-update-manager/installed.json\"]\n    snapshots = {}\n    for original in paths:\n        if original.is_file():\n            relative = original.relative_to(Path.home()) if original.is_relative_to(Path.home()) else Path(\"external\") / original.name\n            saved = report.dir / \"backup\" / relative\n            saved.parent.mkdir(parents=True, exist_ok=True)\n            shutil.copy2(original, saved)\n            snapshots[str(original)] = {\"path\": str(saved), \"sha256\": sha(saved)}\n    (report.dir / \"backed-up-state.json\").write_text(\n        json.dumps(snapshots, ensure_ascii=False, indent=2) + \"\\n\")\n    report.step(\"state-backup\", \"pass\", str(len(snapshots)) + \" existing files\")\n\n\ndef is_alive(pid):\n    try:\n        os.kill(int(pid), 0)\n        return True\n    except (ProcessLookupError, ValueError):\n        return False\n    except PermissionError:\n        return True\n\n\ndef install_packaged(report, service):\n    check_idle(service)\n    if APPLICATION.exists() and service is None:\n        check = subprocess.run([\"/usr/bin/osascript\", \"-e\",\n                                'application id \"' + APP_ID + '\" is running'],\n                               capture_output=True, text=True, timeout=15)\n        if check.returncode or check.stdout.strip().lower() != \"false\":\n            raise Blocked(\"Existing application status is unknown or it is still running\")\n    backup_user_state(report, service)\n    if APPLICATION.exists():\n        metadata(APPLICATION)\n        command([\"/usr/bin/ditto\", str(APPLICATION),\n                 str(report.dir / \"backup\" / \"Mac Update Manager.app\")],\n                report, \"application-backup\", timeout=180)\n    if service:\n        old_pid = service[1][\"pid\"]\n        command([\"/usr/bin/osascript\", \"-e\",\n                 'tell application id \"' + APP_ID + '\" to quit'],\n                report, \"close-owned-application\", timeout=45)\n        deadline = time.time() + 45\n        while time.time() < deadline and is_alive(old_pid):\n            time.sleep(1)\n        if is_alive(old_pid):\n            raise Blocked(\"Old backend is still running; no app swap\")\n    staging = APPLICATION.parent / (\".mac-manager-next-\" + uuid.uuid4().hex + \".app\")\n    retired = APPLICATION.parent / (\".mac-manager-previous-\" + uuid.uuid4().hex + \".app\")\n    command([\"/usr/bin/ditto\", str(BUILT), str(staging)], report,\n            \"stage-application\", timeout=180)\n    metadata(staging)\n    try:\n        if APPLICATION.exists():\n            os.replace(APPLICATION, retired)\n        os.replace(staging, APPLICATION)\n    except BaseException:\n        if not APPLICATION.exists() and retired.exists():\n            os.replace(retired, APPLICATION)\n        raise\n    report.data[\"old_app_retained_at\"] = str(retired) if retired.exists() else None\n    report.step(\"application-swap\", \"pass\", \"Local app replaced; package installations unchanged\")\n\n\ndef wait_for_health(report, product, previous_pid=None):\n    deadline = time.time() + 90\n    last = None\n    while time.time() < deadline:\n        try:\n            service = find_service()\n            if service is not None:\n                port, health = service\n                last = health\n                if (health.get(\"version\") == VERSION and\n                    health.get(\"backend_code_sha256\") == product[\"backend_code_sha256\"] and\n                    health.get(\"build_id\") == product[\"build_id\"] and\n                    health.get(\"pid\") != previous_pid):\n                    report.step(\"service-identity\", \"pass\", \"Live process matches packaged source\")\n                    return service\n        except Exception:\n            pass\n        time.sleep(1)\n    raise Blocked(\"Unable to identify a running packaged backend: \" + str(last))\n\n\ndef run_update_request(report, port, payload):\n    response = {}\n    error = []\n    done = threading.Event()\n    def worker():\n        try:\n            response.update(api(port, \"/api/update-selected\", payload, timeout=7200))\n        except BaseException as exc:\n            error.append(str(exc))\n        finally:\n            done.set()\n    thread = threading.Thread(target=worker, daemon=True)\n    thread.start()\n    while not done.wait(timeout=8):\n        try:\n            current = api(port, \"/api/progress\", timeout=5)\n            progress = current.get(\"progress\") or {}\n            print(\"עדכון:\", progress.get(\"phase\") or \"running\",\n                  progress.get(\"current_package\") or \"\", flush=True)\n        except Exception:\n            print(\"אין כרגע חיווי מהשירות; הבקשה לא נשלחת שוב.\", flush=True)\n    if error:\n        try:\n            journal = api(port, \"/api/update-requests/\" + payload[\"request_id\"])\n            report.data[\"request_state_after_disconnect\"] = journal\n            report.save()\n        except Exception:\n            pass\n        raise Blocked(\"Request status cannot be confirmed; do not retry mutation: \" + error[0])\n    return response\n\n\ndef chosen_actions(report, plan, pilot):\n    decisions = [d for d in plan.get(\"decisions\", [])\n                 if d.get(\"status\") == \"ready\"]\n    if pilot:\n        decisions = [d for d in decisions if d[\"logical_id\"] == pilot]\n    for idx, d in enumerate(decisions, 1):\n        print(f\"{idx}. {d['logical_id']}  {d['installed_version']} → \"\n              f\"{d['target_version']}  {d['current_provider']} → \"\n              f\"{d['target_provider']} ({d['action']})\")\n    iw = next((d for d in decisions\n               if d[\"logical_id\"] == \"include-what-you-use\" and\n               d[\"action\"] == \"migrate\"), None)\n    if not iw:\n        raise Blocked(\"IWYU migration is not ready; never pick a substitute pilot automatically\")\n    if pilot:\n        return [iw]\n    raw = input(\"מספרי הפעולות הרצויות, מופרדים בפסיק (ברירת מחדל: IWYU בלבד): \").strip()\n    if not raw:\n        return [iw]\n    try:\n        indices = {int(x.strip()) for x in raw.split(\",\")}\n    except ValueError as error:\n        raise Blocked(\"Invalid selection\") from error\n    if not indices or any(x < 1 or x > len(decisions) for x in indices):\n        raise Blocked(\"Invalid selection indices\")\n    return [d for index, d in enumerate(decisions, 1) if index in indices]\n\n\ndef verify_migration(report, service, decisions):\n    state = api(service[0], \"/api/state\")\n    report.data[\"after_inventory\"] = state\n    for decision in decisions:\n        if decision[\"action\"] != \"migrate\":\n            continue\n        source_id = decision[\"source_installation_id\"]\n        target_provider = decision[\"target_provider\"]\n        target_name = decision[\"selected_candidate\"][\"provider_names\"][0]\n        rows = state.get(\"packages\", [])\n        if any(x.get(\"installation_id\") == source_id and\n               not x.get(\"inventory_stale\") for x in rows):\n            raise Blocked(\"Source installation still appears in verified inventory\")\n        expected_id = \"macports:\" + target_name if target_provider == \"macports\" else \"pixi:\" + target_name\n        if not any(x.get(\"installation_id\") == expected_id and\n                   not x.get(\"inventory_stale\") for x in rows):\n            raise Blocked(\"Migrated provider installation is not verified\")\n    report.step(\"live-migration-state\", \"pass\", \"No synthetic old installation observed\")\n    return state\n\n\ndef perform_full(report, pilot):\n    prerequisites(report)\n    matched = isolated_checks(report)\n    product = package_application(report, matched)\n    previous = find_service()\n    check_idle(previous)\n    old_pid = previous[1][\"pid\"] if previous else None\n    install_packaged(report, previous)\n    command([\"/usr/bin/open\", str(APPLICATION)], report, \"open-new-application\", timeout=30)\n    live = wait_for_health(report, product, previous_pid=old_pid)\n    report.data[\"installed_service\"] = live[1]\n    report.save()\n    scanned = api(live[0], \"/api/check\", {}, timeout=1800)\n    report.data[\"before_inventory\"] = scanned.get(\"snapshot\", scanned)\n    plan_result = api(live[0], \"/api/update-plan\")\n    plan = plan_result.get(\"plan\")\n    if not isinstance(plan, dict):\n        raise Blocked(\"No immutable plan was produced by the installed application\")\n    report.data[\"displayed_plan\"] = plan\n    report.save()\n    selected = chosen_actions(report, plan, pilot)\n    approved = [{\"decision_id\": d[\"decision_id\"],\n                 \"material_fingerprint\": d[\"material_fingerprint\"]} for d in selected]\n    request = {\"request_id\": uuid.uuid4().hex, \"plan_id\": plan[\"id\"],\n               \"decisions\": approved}\n    report.data[\"request\"] = request\n    report.step(\"approval-required\", \"blocked\", \"No package will be changed before YES\")\n    chosen_names = \", \".join(d[\"logical_id\"] for d in selected)\n    if input(\"לאשר את הפעולות \" + chosen_names + \" עבור plan \" + plan[\"id\"] + \" ? הקלד YES: \").strip() != \"YES\":\n        raise Blocked(\"User did not approve the displayed operations\")\n    response = run_update_request(report, live[0], request)\n    report.data[\"mutation_response\"] = response\n    report.save()\n    if not response.get(\"ok\") or not all(x.get(\"ok\") for x in response.get(\"results\", [])):\n        raise RuntimeError(\"An approved update failed; examine its transaction journal\")\n    report.data[\"live_migration\"] = \"pass\"\n    report.save()\n    verify_migration(report, live, selected)\n    check_idle(find_service())\n    command([\"/usr/bin/osascript\", \"-e\",\n             'tell application id \"' + APP_ID + '\" to quit'], report,\n            \"restart-close-owned-app\", timeout=45)\n    deadline = time.time() + 45\n    while is_alive(live[1][\"pid\"]) and time.time() < deadline:\n        time.sleep(1)\n    if is_alive(live[1][\"pid\"]):\n        raise Blocked(\"Old app PID remains active; restart deferred\")\n    command([\"/usr/bin/open\", str(APPLICATION)], report, \"restart-open-app\", timeout=30)\n    restarted = wait_for_health(report, product, previous_pid=live[1][\"pid\"])\n    report.data[\"restarted_service\"] = restarted[1]\n    verify_migration(report, restarted, selected)\n    report.data[\"restart_verification\"] = \"pass\"\n    report.save()\n\n\ndef main():\n    parser = argparse.ArgumentParser()\n    parser.add_argument(\"--checks\", action=\"store_true\")\n    parser.add_argument(\"--full\", action=\"store_true\")\n    parser.add_argument(\"--acceptance\", action=\"store_true\")\n    parser.add_argument(\"--pilot\", default=None)\n    args = parser.parse_args()\n    if not args.checks and not (args.full or args.acceptance):\n        parser.error(\"Use --checks or --full\")\n    if args.pilot and args.pilot != \"include-what-you-use\":\n        parser.error(\"Only the IWYU migration pilot is accepted automatically\")\n    report = Report(\"isolated_code_checks\" if args.checks else \"full_local_acceptance\")\n    code = 2\n    try:\n        if args.checks:\n            prerequisites(report)\n            isolated_checks(report, reuse=False)\n        else:\n            print(\"הפקודה עשויה להחליף את אפליקציית Mac Update Manager המקומית לפני אישור חבילות.\", flush=True)\n            perform_full(report, args.pilot)\n        code = 0\n        report.data[\"result\"] = \"pass\"\n    except Blocked as exc:\n        report.data.update(result=\"blocked\", error=str(exc))\n        code = 2\n    except (KeyboardInterrupt, Exception) as exc:\n        report.data.update(result=\"fail\", error=str(exc))\n        code = 1\n    finally:\n        report.data[\"finished_at\"] = time.time()\n        report.data[\"exit_code\"] = code\n        report.save()\n        print(\"דוח:\", report.dir, flush=True)\n    return code\n\n\nif __name__ == \"__main__\":\n    raise SystemExit(main())\n",
    "package.json": "{\n  \"name\": \"brew-update-manager\",\n  \"version\": \"1.3.4\",\n  \"private\": true,\n  \"author\": \"Tzoharlary\",\n  \"license\": \"MIT\",\n  \"description\": \"macOS desktop app for Homebrew, MacPorts and managed tool updates\",\n  \"main\": \"electron/main.js\",\n  \"scripts\": {\n    \"start-app\": \"electron .\",\n    \"generate-icon\": \"/usr/local/bin/python3 tools/generate_app_icon.py\",\n    \"pack\": \"electron-builder --dir --publish never\",\n    \"build\": \"node tools/run-quick-build.js\",\n    \"build:release\": \"node tools/run-release-build.js\"\n  },\n  \"devDependencies\": {\n    \"electron\": \"35.1.5\",\n    \"electron-builder\": \"26.0.12\"\n  },\n  \"build\": {\n    \"appId\": \"com.tzoharlary.brewupdatemanager\",\n    \"productName\": \"Mac Update Manager\",\n    \"directories\": {\n      \"output\": \"dist\",\n      \"buildResources\": \"build\"\n    },\n    \"files\": [\n      \"electron/**/*\",\n      \"renderer/**/*\",\n      \"backend/**/*\",\n      \"tools/**/*\",\n      \"package.json\",\n      \"README.md\",\n      \"LICENSE\"\n    ],\n    \"asarUnpack\": [\n      \"backend/**\",\n      \"tools/**\"\n    ],\n    \"dmg\": {\n      \"writeUpdateInfo\": false\n    },\n    \"mac\": {\n      \"icon\": \"build/icon.icns\",\n      \"category\": \"public.app-category.utilities\",\n      \"target\": [\n        {\n          \"target\": \"dmg\",\n          \"arch\": [\n            \"x64\",\n            \"arm64\"\n          ]\n        }\n      ],\n      \"artifactName\": \"${productName}-${version}-${arch}.${ext}\",\n      \"hardenedRuntime\": false,\n      \"entitlements\": \"build/entitlements.mac.plist\",\n      \"entitlementsInherit\": \"build/entitlements.mac.plist\"\n    }\n  }\n}\n",
    "package-lock.json": "{\n  \"name\": \"brew-update-manager\",\n  \"version\": \"1.3.4\",\n  \"lockfileVersion\": 3,\n  \"requires\": true,\n  \"packages\": {\n    \"\": {\n      \"name\": \"brew-update-manager\",\n      \"version\": \"1.3.4\",\n      \"devDependencies\": {\n        \"electron\": \"35.1.5\",\n        \"electron-builder\": \"26.0.12\"\n      }\n    },\n    \"node_modules/@develar/schema-utils\": {\n      \"version\": \"2.6.5\",\n      \"resolved\": \"https://registry.npmjs.org/@develar/schema-utils/-/schema-utils-2.6.5.tgz\",\n      \"integrity\": \"sha512-0cp4PsWQ/9avqTVMCtZ+GirikIA36ikvjtHweU4/j8yLtgObI0+JUPhYFScgwlteveGB1rt3Cm8UhN04XayDig==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ajv\": \"^6.12.0\",\n        \"ajv-keywords\": \"^3.4.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 8.9.0\"\n      },\n      \"funding\": {\n        \"type\": \"opencollective\",\n        \"url\": \"https://opencollective.com/webpack\"\n      }\n    },\n    \"node_modules/@electron/asar\": {\n      \"version\": \"3.2.18\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/asar/-/asar-3.2.18.tgz\",\n      \"integrity\": \"sha512-2XyvMe3N3Nrs8cV39IKELRHTYUWFKrmqqSY1U+GMlc0jvqjIVnoxhNd2H4JolWQncbJi1DCvb5TNxZuI2fEjWg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"commander\": \"^5.0.0\",\n        \"glob\": \"^7.1.6\",\n        \"minimatch\": \"^3.0.4\"\n      },\n      \"bin\": {\n        \"asar\": \"bin/asar.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10.12.0\"\n      }\n    },\n    \"node_modules/@electron/asar/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/@electron/asar/node_modules/brace-expansion\": {\n      \"version\": \"1.1.13\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-1.1.13.tgz\",\n      \"integrity\": \"sha512-9ZLprWS6EENmhEOpjCYW2c8VkmOvckIJZfkr7rBW6dObmfgJ/L1GpSYW5Hpo9lDz4D1+n0Ckz8rU7FwHDQiG/w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\",\n        \"concat-map\": \"0.0.1\"\n      }\n    },\n    \"node_modules/@electron/asar/node_modules/minimatch\": {\n      \"version\": \"3.1.5\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-3.1.5.tgz\",\n      \"integrity\": \"sha512-VgjWUsnnT6n+NUk6eZq77zeFdpW2LWDzP6zFGrCbHXiYNul5Dzqk2HHQ5uFH2DNW5Xbp8+jVzaeNt94ssEEl4w==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^1.1.7\"\n      },\n      \"engines\": {\n        \"node\": \"*\"\n      }\n    },\n    \"node_modules/@electron/fuses\": {\n      \"version\": \"1.8.0\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/fuses/-/fuses-1.8.0.tgz\",\n      \"integrity\": \"sha512-zx0EIq78WlY/lBb1uXlziZmDZI4ubcCXIMJ4uGjXzZW0nS19TjSPeXPAjzzTmKQlJUZm0SbmZhPKP7tuQ1SsEw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"chalk\": \"^4.1.1\",\n        \"fs-extra\": \"^9.0.1\",\n        \"minimist\": \"^1.2.5\"\n      },\n      \"bin\": {\n        \"electron-fuses\": \"dist/bin.js\"\n      }\n    },\n    \"node_modules/@electron/fuses/node_modules/fs-extra\": {\n      \"version\": \"9.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-9.1.0.tgz\",\n      \"integrity\": \"sha512-hcg3ZmepS30/7BSFqRvoo3DOMQu7IjqxO5nCDt+zM9XWjb33Wg7ziNT+Qvqbuc3+gWpzO02JubVyk2G4Zvo1OQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"at-least-node\": \"^1.0.0\",\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@electron/fuses/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/@electron/fuses/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@electron/get\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/get/-/get-2.0.3.tgz\",\n      \"integrity\": \"sha512-Qkzpg2s9GnVV2I2BjRksUi43U5e6+zaQMcjoJy0C+C5oxaKl+fmckGDQFtRpZpZV0NQekuZZ+tGz7EA9TVnQtQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"debug\": \"^4.1.1\",\n        \"env-paths\": \"^2.2.0\",\n        \"fs-extra\": \"^8.1.0\",\n        \"got\": \"^11.8.5\",\n        \"progress\": \"^2.0.3\",\n        \"semver\": \"^6.2.0\",\n        \"sumchecker\": \"^3.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"optionalDependencies\": {\n        \"global-agent\": \"^3.0.0\"\n      }\n    },\n    \"node_modules/@electron/node-gyp\": {\n      \"version\": \"10.2.0-electron.1\",\n      \"resolved\": \"git+ssh://git@github.com/electron/node-gyp.git#06b29aafb7708acef8b3669835c8a7857ebc92d2\",\n      \"integrity\": \"sha512-lBSgDMQqt7QWMuIjS8zNAq5FI5o5RVBAcJUGWGI6GgoQITJt3msAkUrHp8YHj3RTVE+h70ndqMGqURjp3IfRyQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"env-paths\": \"^2.2.0\",\n        \"exponential-backoff\": \"^3.1.1\",\n        \"glob\": \"^8.1.0\",\n        \"graceful-fs\": \"^4.2.6\",\n        \"make-fetch-happen\": \"^10.2.1\",\n        \"nopt\": \"^6.0.0\",\n        \"proc-log\": \"^2.0.1\",\n        \"semver\": \"^7.3.5\",\n        \"tar\": \"^6.2.1\",\n        \"which\": \"^2.0.2\"\n      },\n      \"bin\": {\n        \"node-gyp\": \"bin/node-gyp.js\"\n      },\n      \"engines\": {\n        \"node\": \">=12.13.0\"\n      }\n    },\n    \"node_modules/@electron/node-gyp/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/@electron/node-gyp/node_modules/brace-expansion\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-2.0.3.tgz\",\n      \"integrity\": \"sha512-MCV/fYJEbqx68aE58kv2cA/kiky1G8vux3OR6/jbS+jIMe/6fJWa0DTzJU7dqijOWYwHi1t29FlfYI9uytqlpA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\"\n      }\n    },\n    \"node_modules/@electron/node-gyp/node_modules/glob\": {\n      \"version\": \"8.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/glob/-/glob-8.1.0.tgz\",\n      \"integrity\": \"sha512-r8hpEjiQEYlF2QU0df3dS+nxxSIreXQS1qRhMJM0Q5NDdR386C7jb7Hwwod8Fgiuex+k0GFjgft18yvxm5XoCQ==\",\n      \"deprecated\": \"Old versions of glob are not supported, and contain widely publicized security vulnerabilities, which have been fixed in the current version. Please update. Support for old versions may be purchased (at exorbitant rates) by contacting i@izs.me\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"fs.realpath\": \"^1.0.0\",\n        \"inflight\": \"^1.0.4\",\n        \"inherits\": \"2\",\n        \"minimatch\": \"^5.0.1\",\n        \"once\": \"^1.3.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/@electron/node-gyp/node_modules/minimatch\": {\n      \"version\": \"5.1.9\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-5.1.9.tgz\",\n      \"integrity\": \"sha512-7o1wEA2RyMP7Iu7GNba9vc0RWWGACJOCZBJX2GJWip0ikV+wcOsgVuY9uE8CPiyQhkGFSlhuSkZPavN7u1c2Fw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^2.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@electron/node-gyp/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@electron/notarize\": {\n      \"version\": \"2.5.0\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/notarize/-/notarize-2.5.0.tgz\",\n      \"integrity\": \"sha512-jNT8nwH1f9X5GEITXaQ8IF/KdskvIkOFfB2CvwumsveVidzpSc+mvhhTMdAGSYF3O+Nq49lJ7y+ssODRXu06+A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"debug\": \"^4.1.1\",\n        \"fs-extra\": \"^9.0.1\",\n        \"promise-retry\": \"^2.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@electron/notarize/node_modules/fs-extra\": {\n      \"version\": \"9.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-9.1.0.tgz\",\n      \"integrity\": \"sha512-hcg3ZmepS30/7BSFqRvoo3DOMQu7IjqxO5nCDt+zM9XWjb33Wg7ziNT+Qvqbuc3+gWpzO02JubVyk2G4Zvo1OQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"at-least-node\": \"^1.0.0\",\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@electron/notarize/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/@electron/notarize/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@electron/osx-sign\": {\n      \"version\": \"1.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/osx-sign/-/osx-sign-1.3.1.tgz\",\n      \"integrity\": \"sha512-BAfviURMHpmb1Yb50YbCxnOY0wfwaLXH5KJ4+80zS0gUkzDX3ec23naTlEqKsN+PwYn+a1cCzM7BJ4Wcd3sGzw==\",\n      \"dev\": true,\n      \"license\": \"BSD-2-Clause\",\n      \"dependencies\": {\n        \"compare-version\": \"^0.1.2\",\n        \"debug\": \"^4.3.4\",\n        \"fs-extra\": \"^10.0.0\",\n        \"isbinaryfile\": \"^4.0.8\",\n        \"minimist\": \"^1.2.6\",\n        \"plist\": \"^3.0.5\"\n      },\n      \"bin\": {\n        \"electron-osx-flat\": \"bin/electron-osx-flat.js\",\n        \"electron-osx-sign\": \"bin/electron-osx-sign.js\"\n      },\n      \"engines\": {\n        \"node\": \">=12.0.0\"\n      }\n    },\n    \"node_modules/@electron/osx-sign/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/@electron/osx-sign/node_modules/isbinaryfile\": {\n      \"version\": \"4.0.10\",\n      \"resolved\": \"https://registry.npmjs.org/isbinaryfile/-/isbinaryfile-4.0.10.tgz\",\n      \"integrity\": \"sha512-iHrqe5shvBUcFbmZq9zOQHBoeOhZJu6RQGrDpBgenUm/Am+F3JM2MgQj+rK3Z601fzrL5gLZWtAPH2OBaSVcyw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 8.0.0\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/gjtorikian/\"\n      }\n    },\n    \"node_modules/@electron/osx-sign/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/@electron/osx-sign/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@electron/rebuild\": {\n      \"version\": \"3.7.0\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/rebuild/-/rebuild-3.7.0.tgz\",\n      \"integrity\": \"sha512-VW++CNSlZwMYP7MyXEbrKjpzEwhB5kDNbzGtiPEjwYysqyTCF+YbNJ210Dj3AjWsGSV4iEEwNkmJN9yGZmVvmw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@electron/node-gyp\": \"git+https://github.com/electron/node-gyp.git#06b29aafb7708acef8b3669835c8a7857ebc92d2\",\n        \"@malept/cross-spawn-promise\": \"^2.0.0\",\n        \"chalk\": \"^4.0.0\",\n        \"debug\": \"^4.1.1\",\n        \"detect-libc\": \"^2.0.1\",\n        \"fs-extra\": \"^10.0.0\",\n        \"got\": \"^11.7.0\",\n        \"node-abi\": \"^3.45.0\",\n        \"node-api-version\": \"^0.2.0\",\n        \"ora\": \"^5.1.0\",\n        \"read-binary-file-arch\": \"^1.0.6\",\n        \"semver\": \"^7.3.5\",\n        \"tar\": \"^6.0.5\",\n        \"yargs\": \"^17.0.1\"\n      },\n      \"bin\": {\n        \"electron-rebuild\": \"lib/cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">=12.13.0\"\n      }\n    },\n    \"node_modules/@electron/rebuild/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/@electron/rebuild/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/@electron/rebuild/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@electron/rebuild/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@electron/universal\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/universal/-/universal-2.0.1.tgz\",\n      \"integrity\": \"sha512-fKpv9kg4SPmt+hY7SVBnIYULE9QJl8L3sCfcBsnqbJwwBwAeTLokJ9TRt9y7bK0JAzIW2y78TVVjvnQEms/yyA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@electron/asar\": \"^3.2.7\",\n        \"@malept/cross-spawn-promise\": \"^2.0.0\",\n        \"debug\": \"^4.3.1\",\n        \"dir-compare\": \"^4.2.0\",\n        \"fs-extra\": \"^11.1.1\",\n        \"minimatch\": \"^9.0.3\",\n        \"plist\": \"^3.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=16.4\"\n      }\n    },\n    \"node_modules/@electron/universal/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/@electron/universal/node_modules/brace-expansion\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-2.0.3.tgz\",\n      \"integrity\": \"sha512-MCV/fYJEbqx68aE58kv2cA/kiky1G8vux3OR6/jbS+jIMe/6fJWa0DTzJU7dqijOWYwHi1t29FlfYI9uytqlpA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\"\n      }\n    },\n    \"node_modules/@electron/universal/node_modules/fs-extra\": {\n      \"version\": \"11.3.4\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-11.3.4.tgz\",\n      \"integrity\": \"sha512-CTXd6rk/M3/ULNQj8FBqBWHYBVYybQ3VPBw0xGKFe3tuH7ytT6ACnvzpIQ3UZtB8yvUKC2cXn1a+x+5EVQLovA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=14.14\"\n      }\n    },\n    \"node_modules/@electron/universal/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/@electron/universal/node_modules/minimatch\": {\n      \"version\": \"9.0.9\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-9.0.9.tgz\",\n      \"integrity\": \"sha512-OBwBN9AL4dqmETlpS2zasx+vTeWclWzkblfZk7KTA5j3jeOONz/tRCnZomUyvNg83wL5Zv9Ss6HMJXAgL8R2Yg==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^2.0.2\"\n      },\n      \"engines\": {\n        \"node\": \">=16 || 14 >=14.17\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/@electron/universal/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@electron/windows-sign\": {\n      \"version\": \"1.2.2\",\n      \"resolved\": \"https://registry.npmjs.org/@electron/windows-sign/-/windows-sign-1.2.2.tgz\",\n      \"integrity\": \"sha512-dfZeox66AvdPtb2lD8OsIIQh12Tp0GNCRUDfBHIKGpbmopZto2/A8nSpYYLoedPIHpqkeblZ/k8OV0Gy7PYuyQ==\",\n      \"dev\": true,\n      \"license\": \"BSD-2-Clause\",\n      \"optional\": true,\n      \"peer\": true,\n      \"dependencies\": {\n        \"cross-dirname\": \"^0.1.0\",\n        \"debug\": \"^4.3.4\",\n        \"fs-extra\": \"^11.1.1\",\n        \"minimist\": \"^1.2.8\",\n        \"postject\": \"^1.0.0-alpha.6\"\n      },\n      \"bin\": {\n        \"electron-windows-sign\": \"bin/electron-windows-sign.js\"\n      },\n      \"engines\": {\n        \"node\": \">=14.14\"\n      }\n    },\n    \"node_modules/@electron/windows-sign/node_modules/fs-extra\": {\n      \"version\": \"11.3.4\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-11.3.4.tgz\",\n      \"integrity\": \"sha512-CTXd6rk/M3/ULNQj8FBqBWHYBVYybQ3VPBw0xGKFe3tuH7ytT6ACnvzpIQ3UZtB8yvUKC2cXn1a+x+5EVQLovA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"peer\": true,\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=14.14\"\n      }\n    },\n    \"node_modules/@electron/windows-sign/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"peer\": true,\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/@electron/windows-sign/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"peer\": true,\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@gar/promisify\": {\n      \"version\": \"1.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/@gar/promisify/-/promisify-1.1.3.tgz\",\n      \"integrity\": \"sha512-k2Ty1JcVojjJFwrg/ThKi2ujJ7XNLYaFGNB/bWT9wGR+oSMJHMa5w+CUq6p/pVrKeNNgA7pCqEcjSnHVoqJQFw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/@isaacs/cliui\": {\n      \"version\": \"8.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/@isaacs/cliui/-/cliui-8.0.2.tgz\",\n      \"integrity\": \"sha512-O8jcjabXaleOG9DQ0+ARXWZBTfnP4WNAqzuiJK7ll44AmxGKv/J2M4TPjxjY3znBCfvBXFzucm1twdyFybFqEA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"string-width\": \"^5.1.2\",\n        \"string-width-cjs\": \"npm:string-width@^4.2.0\",\n        \"strip-ansi\": \"^7.0.1\",\n        \"strip-ansi-cjs\": \"npm:strip-ansi@^6.0.1\",\n        \"wrap-ansi\": \"^8.1.0\",\n        \"wrap-ansi-cjs\": \"npm:wrap-ansi@^7.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/@isaacs/cliui/node_modules/ansi-regex\": {\n      \"version\": \"6.2.2\",\n      \"resolved\": \"https://registry.npmjs.org/ansi-regex/-/ansi-regex-6.2.2.tgz\",\n      \"integrity\": \"sha512-Bq3SmSpyFHaWjPk8If9yc6svM8c56dB5BAtW4Qbw5jHTwwXXcTLoRMkpDJp6VL0XzlWaCHTXrkFURMYmD0sLqg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/ansi-regex?sponsor=1\"\n      }\n    },\n    \"node_modules/@isaacs/cliui/node_modules/ansi-styles\": {\n      \"version\": \"6.2.3\",\n      \"resolved\": \"https://registry.npmjs.org/ansi-styles/-/ansi-styles-6.2.3.tgz\",\n      \"integrity\": \"sha512-4Dj6M28JB+oAH8kFkTLUo+a2jwOFkuqb3yucU0CANcRRUbxS0cP0nZYCGjcc3BNXwRIsUVmDGgzawme7zvJHvg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/ansi-styles?sponsor=1\"\n      }\n    },\n    \"node_modules/@isaacs/cliui/node_modules/emoji-regex\": {\n      \"version\": \"9.2.2\",\n      \"resolved\": \"https://registry.npmjs.org/emoji-regex/-/emoji-regex-9.2.2.tgz\",\n      \"integrity\": \"sha512-L18DaJsXSUk2+42pv8mLs5jJT2hqFkFE4j21wOmgbUqsZ2hL72NsUU785g9RXgo3s0ZNgVl42TiHp3ZtOv/Vyg==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/@isaacs/cliui/node_modules/string-width\": {\n      \"version\": \"5.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/string-width/-/string-width-5.1.2.tgz\",\n      \"integrity\": \"sha512-HnLOCR3vjcY8beoNLtcjZ5/nxn2afmME6lhrDrebokqMap+XbeW8n9TXpPDOqdGK5qcI3oT0GKTW6wC7EMiVqA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"eastasianwidth\": \"^0.2.0\",\n        \"emoji-regex\": \"^9.2.2\",\n        \"strip-ansi\": \"^7.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/@isaacs/cliui/node_modules/strip-ansi\": {\n      \"version\": \"7.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/strip-ansi/-/strip-ansi-7.2.0.tgz\",\n      \"integrity\": \"sha512-yDPMNjp4WyfYBkHnjIRLfca1i6KMyGCtsVgoKe/z1+6vukgaENdgGBZt+ZmKPc4gavvEZ5OgHfHdrazhgNyG7w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ansi-regex\": \"^6.2.2\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/strip-ansi?sponsor=1\"\n      }\n    },\n    \"node_modules/@isaacs/cliui/node_modules/wrap-ansi\": {\n      \"version\": \"8.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/wrap-ansi/-/wrap-ansi-8.1.0.tgz\",\n      \"integrity\": \"sha512-si7QWI6zUMq56bESFvagtmzMdGOtoxfR+Sez11Mobfc7tm+VkUckk9bW2UeffTGVUbOksxmSw0AA2gs8g71NCQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ansi-styles\": \"^6.1.0\",\n        \"string-width\": \"^5.0.1\",\n        \"strip-ansi\": \"^7.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/wrap-ansi?sponsor=1\"\n      }\n    },\n    \"node_modules/@malept/cross-spawn-promise\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/@malept/cross-spawn-promise/-/cross-spawn-promise-2.0.0.tgz\",\n      \"integrity\": \"sha512-1DpKU0Z5ThltBwjNySMC14g0CkbyhCaz9FkhxqNsZI6uAPJXFS8cMXlBKo26FJ8ZuW6S9GCMcR9IO5k2X5/9Fg==\",\n      \"dev\": true,\n      \"funding\": [\n        {\n          \"type\": \"individual\",\n          \"url\": \"https://github.com/sponsors/malept\"\n        },\n        {\n          \"type\": \"tidelift\",\n          \"url\": \"https://tidelift.com/subscription/pkg/npm-.malept-cross-spawn-promise?utm_medium=referral&utm_source=npm_fund\"\n        }\n      ],\n      \"license\": \"Apache-2.0\",\n      \"dependencies\": {\n        \"cross-spawn\": \"^7.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 12.13.0\"\n      }\n    },\n    \"node_modules/@malept/flatpak-bundler\": {\n      \"version\": \"0.4.0\",\n      \"resolved\": \"https://registry.npmjs.org/@malept/flatpak-bundler/-/flatpak-bundler-0.4.0.tgz\",\n      \"integrity\": \"sha512-9QOtNffcOF/c1seMCDnjckb3R9WHcG34tky+FHpNKKCW0wc/scYLwMtO+ptyGUfMW0/b/n4qRiALlaFHc9Oj7Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"debug\": \"^4.1.1\",\n        \"fs-extra\": \"^9.0.0\",\n        \"lodash\": \"^4.17.15\",\n        \"tmp-promise\": \"^3.0.2\"\n      },\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@malept/flatpak-bundler/node_modules/fs-extra\": {\n      \"version\": \"9.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-9.1.0.tgz\",\n      \"integrity\": \"sha512-hcg3ZmepS30/7BSFqRvoo3DOMQu7IjqxO5nCDt+zM9XWjb33Wg7ziNT+Qvqbuc3+gWpzO02JubVyk2G4Zvo1OQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"at-least-node\": \"^1.0.0\",\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@malept/flatpak-bundler/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/@malept/flatpak-bundler/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/@npmcli/fs\": {\n      \"version\": \"2.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/@npmcli/fs/-/fs-2.1.2.tgz\",\n      \"integrity\": \"sha512-yOJKRvohFOaLqipNtwYB9WugyZKhC/DZC4VYPmpaCzDBrA8YpK3qHZ8/HGscMnE4GqbkLNuVcCnxkeQEdGt6LQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"@gar/promisify\": \"^1.1.3\",\n        \"semver\": \"^7.3.5\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/@npmcli/fs/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@npmcli/move-file\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/@npmcli/move-file/-/move-file-2.0.1.tgz\",\n      \"integrity\": \"sha512-mJd2Z5TjYWq/ttPLLGqArdtnC74J6bOzg4rMDnN+p1xTacZ2yPRCk2y0oSWQtygLR9YVQXgOcONrwtnk3JupxQ==\",\n      \"deprecated\": \"This functionality has been moved to @npmcli/fs\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"mkdirp\": \"^1.0.4\",\n        \"rimraf\": \"^3.0.2\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/@pkgjs/parseargs\": {\n      \"version\": \"0.11.0\",\n      \"resolved\": \"https://registry.npmjs.org/@pkgjs/parseargs/-/parseargs-0.11.0.tgz\",\n      \"integrity\": \"sha512-+1VkjdD0QBLPodGrJUeqarH8VAIvQODIbwh9XpP5Syisf7YoQgsJKPNFoqqLQlu+VQ/tVSshMR6loPMn8U+dPg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"engines\": {\n        \"node\": \">=14\"\n      }\n    },\n    \"node_modules/@sindresorhus/is\": {\n      \"version\": \"4.6.0\",\n      \"resolved\": \"https://registry.npmjs.org/@sindresorhus/is/-/is-4.6.0.tgz\",\n      \"integrity\": \"sha512-t09vSN3MdfsyCHoFcTRCH/iUtG7OJ0CsjzB8cjAmKc/va/kIgeDI/TxsigdncE/4be734m0cvIYwNaV4i2XqAw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sindresorhus/is?sponsor=1\"\n      }\n    },\n    \"node_modules/@szmarczak/http-timer\": {\n      \"version\": \"4.0.6\",\n      \"resolved\": \"https://registry.npmjs.org/@szmarczak/http-timer/-/http-timer-4.0.6.tgz\",\n      \"integrity\": \"sha512-4BAffykYOgO+5nzBWYwE3W90sBgLJoUPRWWcL8wlyiM8IB8ipJz3UMJ9KXQd1RKQXpKp8Tutn80HZtWsu2u76w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"defer-to-connect\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/@tootallnate/once\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/@tootallnate/once/-/once-2.0.0.tgz\",\n      \"integrity\": \"sha512-XCuKFP5PS55gnMVu3dty8KPatLqUoy/ZYzDzAGCQ8JNFCkLXzmI7vNHCR+XpbZaMWQK/vQubr7PkYq8g470J/A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10\"\n      }\n    },\n    \"node_modules/@types/cacheable-request\": {\n      \"version\": \"6.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/@types/cacheable-request/-/cacheable-request-6.0.3.tgz\",\n      \"integrity\": \"sha512-IQ3EbTzGxIigb1I3qPZc1rWJnH0BmSKv5QYTalEwweFvyBDLSAe24zP0le/hyi7ecGfZVlIVAg4BZqb8WBwKqw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@types/http-cache-semantics\": \"*\",\n        \"@types/keyv\": \"^3.1.4\",\n        \"@types/node\": \"*\",\n        \"@types/responselike\": \"^1.0.0\"\n      }\n    },\n    \"node_modules/@types/debug\": {\n      \"version\": \"4.1.13\",\n      \"resolved\": \"https://registry.npmjs.org/@types/debug/-/debug-4.1.13.tgz\",\n      \"integrity\": \"sha512-KSVgmQmzMwPlmtljOomayoR89W4FynCAi3E8PPs7vmDVPe84hT+vGPKkJfThkmXs0x0jAaa9U8uW8bbfyS2fWw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@types/ms\": \"*\"\n      }\n    },\n    \"node_modules/@types/fs-extra\": {\n      \"version\": \"9.0.13\",\n      \"resolved\": \"https://registry.npmjs.org/@types/fs-extra/-/fs-extra-9.0.13.tgz\",\n      \"integrity\": \"sha512-nEnwB++1u5lVDM2UI4c1+5R+FYaKfaAzS4OococimjVm3nQw3TuzH5UNsocrcTBbhnerblyHj4A49qXbIiZdpA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@types/node\": \"*\"\n      }\n    },\n    \"node_modules/@types/http-cache-semantics\": {\n      \"version\": \"4.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/@types/http-cache-semantics/-/http-cache-semantics-4.2.0.tgz\",\n      \"integrity\": \"sha512-L3LgimLHXtGkWikKnsPg0/VFx9OGZaC+eN1u4r+OB1XRqH3meBIAVC2zr1WdMH+RHmnRkqliQAOHNJ/E0j/e0Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/@types/keyv\": {\n      \"version\": \"3.1.4\",\n      \"resolved\": \"https://registry.npmjs.org/@types/keyv/-/keyv-3.1.4.tgz\",\n      \"integrity\": \"sha512-BQ5aZNSCpj7D6K2ksrRCTmKRLEpnPvWDiLPfoGyhZ++8YtiK9d/3DBKPJgry359X/P1PfruyYwvnvwFjuEiEIg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@types/node\": \"*\"\n      }\n    },\n    \"node_modules/@types/ms\": {\n      \"version\": \"2.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/@types/ms/-/ms-2.1.0.tgz\",\n      \"integrity\": \"sha512-GsCCIZDE/p3i96vtEqx+7dBUGXrc7zeSK3wwPHIaRThS+9OhWIXRqzs4d6k1SVU8g91DrNRWxWUGhp5KXQb2VA==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/@types/node\": {\n      \"version\": \"22.19.15\",\n      \"resolved\": \"https://registry.npmjs.org/@types/node/-/node-22.19.15.tgz\",\n      \"integrity\": \"sha512-F0R/h2+dsy5wJAUe3tAU6oqa2qbWY5TpNfL/RGmo1y38hiyO1w3x2jPtt76wmuaJI4DQnOBu21cNXQ2STIUUWg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"undici-types\": \"~6.21.0\"\n      }\n    },\n    \"node_modules/@types/plist\": {\n      \"version\": \"3.0.5\",\n      \"resolved\": \"https://registry.npmjs.org/@types/plist/-/plist-3.0.5.tgz\",\n      \"integrity\": \"sha512-E6OCaRmAe4WDmWNsL/9RMqdkkzDCY1etutkflWk4c+AcjDU07Pcz1fQwTX0TQz+Pxqn9i4L1TU3UFpjnrcDgxA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"@types/node\": \"*\",\n        \"xmlbuilder\": \">=11.0.1\"\n      }\n    },\n    \"node_modules/@types/responselike\": {\n      \"version\": \"1.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/@types/responselike/-/responselike-1.0.3.tgz\",\n      \"integrity\": \"sha512-H/+L+UkTV33uf49PH5pCAUBVPNj2nDBXTN+qS1dOwyyg24l3CcicicCA7ca+HMvJBZcFgl5r8e+RR6elsb4Lyw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@types/node\": \"*\"\n      }\n    },\n    \"node_modules/@types/verror\": {\n      \"version\": \"1.10.11\",\n      \"resolved\": \"https://registry.npmjs.org/@types/verror/-/verror-1.10.11.tgz\",\n      \"integrity\": \"sha512-RlDm9K7+o5stv0Co8i8ZRGxDbrTxhJtgjqjFyVh/tXQyl/rYtTKlnTvZ88oSTeYREWurwx20Js4kTuKCsFkUtg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/@types/yauzl\": {\n      \"version\": \"2.10.3\",\n      \"resolved\": \"https://registry.npmjs.org/@types/yauzl/-/yauzl-2.10.3.tgz\",\n      \"integrity\": \"sha512-oJoftv0LSuaDZE3Le4DbKX+KS9G36NzOeSap90UIK0yMA/NhKJhqlSGtNDORNRaIbQfzjXDrQa0ytJ6mNRGz/Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"@types/node\": \"*\"\n      }\n    },\n    \"node_modules/@xmldom/xmldom\": {\n      \"version\": \"0.8.12\",\n      \"resolved\": \"https://registry.npmjs.org/@xmldom/xmldom/-/xmldom-0.8.12.tgz\",\n      \"integrity\": \"sha512-9k/gHF6n/pAi/9tqr3m3aqkuiNosYTurLLUtc7xQ9sxB/wm7WPygCv8GYa6mS0fLJEHhqMC1ATYhz++U/lRHqg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10.0.0\"\n      }\n    },\n    \"node_modules/7zip-bin\": {\n      \"version\": \"5.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/7zip-bin/-/7zip-bin-5.2.0.tgz\",\n      \"integrity\": \"sha512-ukTPVhqG4jNzMro2qA9HSCSSVJN3aN7tlb+hfqYCt3ER0yWroeA2VR38MNrOHLQ/cVj+DaIMad0kFCtWWowh/A==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/abbrev\": {\n      \"version\": \"1.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/abbrev/-/abbrev-1.1.1.tgz\",\n      \"integrity\": \"sha512-nne9/IiQ/hzIhY6pdDnbBtz7DjPTKrY00P/zvPSm5pOFkl6xuGrGnXn/VtTNNfNtAfZ9/1RtehkszU9qcTii0Q==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/agent-base\": {\n      \"version\": \"7.1.4\",\n      \"resolved\": \"https://registry.npmjs.org/agent-base/-/agent-base-7.1.4.tgz\",\n      \"integrity\": \"sha512-MnA+YT8fwfJPgBx3m60MNqakm30XOkyIoH1y6huTQvC0PwZG7ki8NacLBcrPbNoo8vEZy7Jpuk7+jMO+CUovTQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 14\"\n      }\n    },\n    \"node_modules/agentkeepalive\": {\n      \"version\": \"4.6.0\",\n      \"resolved\": \"https://registry.npmjs.org/agentkeepalive/-/agentkeepalive-4.6.0.tgz\",\n      \"integrity\": \"sha512-kja8j7PjmncONqaTsB8fQ+wE2mSU2DJ9D4XKoJ5PFWIdRMa6SLSN1ff4mOr4jCbfRSsxR4keIiySJU0N9T5hIQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"humanize-ms\": \"^1.2.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 8.0.0\"\n      }\n    },\n    \"node_modules/aggregate-error\": {\n      \"version\": \"3.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/aggregate-error/-/aggregate-error-3.1.0.tgz\",\n      \"integrity\": \"sha512-4I7Td01quW/RpocfNayFdFVk1qSuoh0E7JrbRJ16nH01HhKFQ88INq9Sd+nd72zqRySlr9BmDA8xlEJ6vJMrYA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"clean-stack\": \"^2.0.0\",\n        \"indent-string\": \"^4.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/ajv\": {\n      \"version\": \"6.14.0\",\n      \"resolved\": \"https://registry.npmjs.org/ajv/-/ajv-6.14.0.tgz\",\n      \"integrity\": \"sha512-IWrosm/yrn43eiKqkfkHis7QioDleaXQHdDVPKg0FSwwd/DuvyX79TZnFOnYpB7dcsFAMmtFztZuXPDvSePkFw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"fast-deep-equal\": \"^3.1.1\",\n        \"fast-json-stable-stringify\": \"^2.0.0\",\n        \"json-schema-traverse\": \"^0.4.1\",\n        \"uri-js\": \"^4.2.2\"\n      },\n      \"funding\": {\n        \"type\": \"github\",\n        \"url\": \"https://github.com/sponsors/epoberezkin\"\n      }\n    },\n    \"node_modules/ajv-keywords\": {\n      \"version\": \"3.5.2\",\n      \"resolved\": \"https://registry.npmjs.org/ajv-keywords/-/ajv-keywords-3.5.2.tgz\",\n      \"integrity\": \"sha512-5p6WTN0DdTGVQk6VjcEju19IgaHudalcfabD7yhDGeA6bcQnmL+CpveLJq/3hvfwd1aof6L386Ougkx6RfyMIQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"peerDependencies\": {\n        \"ajv\": \"^6.9.1\"\n      }\n    },\n    \"node_modules/ansi-regex\": {\n      \"version\": \"5.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/ansi-regex/-/ansi-regex-5.0.1.tgz\",\n      \"integrity\": \"sha512-quJQXlTSUGL2LH9SUXo8VwsY4soanhgo6LNSm84E1LBcE8s3O0wpdiRzyR9z/ZZJMlMWv37qOOb9pdJlMUEKFQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/ansi-styles\": {\n      \"version\": \"4.3.0\",\n      \"resolved\": \"https://registry.npmjs.org/ansi-styles/-/ansi-styles-4.3.0.tgz\",\n      \"integrity\": \"sha512-zbB9rCJAT1rbjiVDb2hqKFHNYLxgtk8NURxZ3IZwD3F6NtxbXZQCnnSi1Lkx+IDohdPlFp222wVALIheZJQSEg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"color-convert\": \"^2.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/ansi-styles?sponsor=1\"\n      }\n    },\n    \"node_modules/app-builder-bin\": {\n      \"version\": \"5.0.0-alpha.12\",\n      \"resolved\": \"https://registry.npmjs.org/app-builder-bin/-/app-builder-bin-5.0.0-alpha.12.tgz\",\n      \"integrity\": \"sha512-j87o0j6LqPL3QRr8yid6c+Tt5gC7xNfYo6uQIQkorAC6MpeayVMZrEDzKmJJ/Hlv7EnOQpaRm53k6ktDYZyB6w==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/app-builder-lib\": {\n      \"version\": \"26.0.12\",\n      \"resolved\": \"https://registry.npmjs.org/app-builder-lib/-/app-builder-lib-26.0.12.tgz\",\n      \"integrity\": \"sha512-+/CEPH1fVKf6HowBUs6LcAIoRcjeqgvAeoSE+cl7Y7LndyQ9ViGPYibNk7wmhMHzNgHIuIbw4nWADPO+4mjgWw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@develar/schema-utils\": \"~2.6.5\",\n        \"@electron/asar\": \"3.2.18\",\n        \"@electron/fuses\": \"^1.8.0\",\n        \"@electron/notarize\": \"2.5.0\",\n        \"@electron/osx-sign\": \"1.3.1\",\n        \"@electron/rebuild\": \"3.7.0\",\n        \"@electron/universal\": \"2.0.1\",\n        \"@malept/flatpak-bundler\": \"^0.4.0\",\n        \"@types/fs-extra\": \"9.0.13\",\n        \"async-exit-hook\": \"^2.0.1\",\n        \"builder-util\": \"26.0.11\",\n        \"builder-util-runtime\": \"9.3.1\",\n        \"chromium-pickle-js\": \"^0.2.0\",\n        \"config-file-ts\": \"0.2.8-rc1\",\n        \"debug\": \"^4.3.4\",\n        \"dotenv\": \"^16.4.5\",\n        \"dotenv-expand\": \"^11.0.6\",\n        \"ejs\": \"^3.1.8\",\n        \"electron-publish\": \"26.0.11\",\n        \"fs-extra\": \"^10.1.0\",\n        \"hosted-git-info\": \"^4.1.0\",\n        \"is-ci\": \"^3.0.0\",\n        \"isbinaryfile\": \"^5.0.0\",\n        \"js-yaml\": \"^4.1.0\",\n        \"json5\": \"^2.2.3\",\n        \"lazy-val\": \"^1.0.5\",\n        \"minimatch\": \"^10.0.0\",\n        \"plist\": \"3.1.0\",\n        \"resedit\": \"^1.7.0\",\n        \"semver\": \"^7.3.8\",\n        \"tar\": \"^6.1.12\",\n        \"temp-file\": \"^3.4.0\",\n        \"tiny-async-pool\": \"1.3.0\"\n      },\n      \"engines\": {\n        \"node\": \">=14.0.0\"\n      },\n      \"peerDependencies\": {\n        \"dmg-builder\": \"26.0.12\",\n        \"electron-builder-squirrel-windows\": \"26.0.12\"\n      }\n    },\n    \"node_modules/app-builder-lib/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/app-builder-lib/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/app-builder-lib/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/app-builder-lib/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/argparse\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/argparse/-/argparse-2.0.1.tgz\",\n      \"integrity\": \"sha512-8+9WqebbFzpX9OR+Wa6O29asIogeRMzcGtAINdpMHHyAg10f05aSFVBbcEqGf/PXw1EjAZ+q2/bEBg3DvurK3Q==\",\n      \"dev\": true,\n      \"license\": \"Python-2.0\"\n    },\n    \"node_modules/assert-plus\": {\n      \"version\": \"1.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/assert-plus/-/assert-plus-1.0.0.tgz\",\n      \"integrity\": \"sha512-NfJ4UzBCcQGLDlQq7nHxH+tv3kyZ0hHQqF5BO6J7tNJeP5do1llPr8dZ8zHonfhAu0PHAdMkSo+8o0wxg9lZWw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"engines\": {\n        \"node\": \">=0.8\"\n      }\n    },\n    \"node_modules/astral-regex\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/astral-regex/-/astral-regex-2.0.0.tgz\",\n      \"integrity\": \"sha512-Z7tMw1ytTXt5jqMcOP+OQteU1VuNK9Y02uuJtKQ1Sv69jXQKKg5cibLwGJow8yzZP+eAc18EmLGPal0bp36rvQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/async\": {\n      \"version\": \"3.2.6\",\n      \"resolved\": \"https://registry.npmjs.org/async/-/async-3.2.6.tgz\",\n      \"integrity\": \"sha512-htCUDlxyyCLMgaM3xXg0C0LW2xqfuQ6p05pCEIsXuyQ+a1koYKTuBMzRNwmybfLgvJDMd0r1LTn4+E0Ti6C2AA==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/async-exit-hook\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/async-exit-hook/-/async-exit-hook-2.0.1.tgz\",\n      \"integrity\": \"sha512-NW2cX8m1Q7KPA7a5M2ULQeZ2wR5qI5PAbw5L0UOMxdioVk9PMZ0h1TmyZEkPYrCvYjDlFICusOu1dlEKAAeXBw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.12.0\"\n      }\n    },\n    \"node_modules/asynckit\": {\n      \"version\": \"0.4.0\",\n      \"resolved\": \"https://registry.npmjs.org/asynckit/-/asynckit-0.4.0.tgz\",\n      \"integrity\": \"sha512-Oei9OH4tRh0YqU3GxhX79dM/mwVgvbZJaSNaRk+bshkj0S5cfHcgYakreBjrHwatXKbz+IoIdYLxrKim2MjW0Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/at-least-node\": {\n      \"version\": \"1.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/at-least-node/-/at-least-node-1.0.0.tgz\",\n      \"integrity\": \"sha512-+q/t7Ekv1EDY2l6Gda6LLiX14rU9TV20Wa3ofeQmwPFZbOMo9DXrLbOjFaaclkXKWidIaopwAObQDqwWtGUjqg==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">= 4.0.0\"\n      }\n    },\n    \"node_modules/balanced-match\": {\n      \"version\": \"4.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-4.0.4.tgz\",\n      \"integrity\": \"sha512-BLrgEcRTwX2o6gGxGOCNyMvGSp35YofuYzw9h1IMTRmKqttAZZVU67bdb9Pr2vUHA8+j3i2tJfjO6C6+4myGTA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \"18 || 20 || >=22\"\n      }\n    },\n    \"node_modules/base64-js\": {\n      \"version\": \"1.5.1\",\n      \"resolved\": \"https://registry.npmjs.org/base64-js/-/base64-js-1.5.1.tgz\",\n      \"integrity\": \"sha512-AKpaYlHn8t4SVbOHCy+b5+KKgvR4vrsD8vbvrbiQJps7fKDTkjkDry6ji0rUJjC0kzbNePLwzxq8iypo41qeWA==\",\n      \"dev\": true,\n      \"funding\": [\n        {\n          \"type\": \"github\",\n          \"url\": \"https://github.com/sponsors/feross\"\n        },\n        {\n          \"type\": \"patreon\",\n          \"url\": \"https://www.patreon.com/feross\"\n        },\n        {\n          \"type\": \"consulting\",\n          \"url\": \"https://feross.org/support\"\n        }\n      ],\n      \"license\": \"MIT\"\n    },\n    \"node_modules/bl\": {\n      \"version\": \"4.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/bl/-/bl-4.1.0.tgz\",\n      \"integrity\": \"sha512-1W07cM9gS6DcLperZfFSj+bWLtaPGSOHWhPiGzXmvVJbRLdG82sH/Kn8EtW1VqWVA54AKf2h5k5BbnIbwF3h6w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"buffer\": \"^5.5.0\",\n        \"inherits\": \"^2.0.4\",\n        \"readable-stream\": \"^3.4.0\"\n      }\n    },\n    \"node_modules/boolean\": {\n      \"version\": \"3.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/boolean/-/boolean-3.2.0.tgz\",\n      \"integrity\": \"sha512-d0II/GO9uf9lfUHH2BQsjxzRJZBdsjgsBiW4BvhWk/3qoKwQFjIDVN19PfX8F2D/r9PCMTtLWjYVCFrpeYUzsw==\",\n      \"deprecated\": \"Package no longer supported. Contact Support at https://www.npmjs.com/support for more info.\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/brace-expansion\": {\n      \"version\": \"5.0.5\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-5.0.5.tgz\",\n      \"integrity\": \"sha512-VZznLgtwhn+Mact9tfiwx64fA9erHH/MCXEUfB/0bX/6Fz6ny5EGTXYltMocqg4xFAQZtnO3DHWWXi8RiuN7cQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^4.0.2\"\n      },\n      \"engines\": {\n        \"node\": \"18 || 20 || >=22\"\n      }\n    },\n    \"node_modules/buffer\": {\n      \"version\": \"5.7.1\",\n      \"resolved\": \"https://registry.npmjs.org/buffer/-/buffer-5.7.1.tgz\",\n      \"integrity\": \"sha512-EHcyIPBQ4BSGlvjB16k5KgAJ27CIsHY/2JBmCRReo48y9rQ3MaUzWX3KVlBa4U7MyX02HdVj0K7C3WaB3ju7FQ==\",\n      \"dev\": true,\n      \"funding\": [\n        {\n          \"type\": \"github\",\n          \"url\": \"https://github.com/sponsors/feross\"\n        },\n        {\n          \"type\": \"patreon\",\n          \"url\": \"https://www.patreon.com/feross\"\n        },\n        {\n          \"type\": \"consulting\",\n          \"url\": \"https://feross.org/support\"\n        }\n      ],\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"base64-js\": \"^1.3.1\",\n        \"ieee754\": \"^1.1.13\"\n      }\n    },\n    \"node_modules/buffer-crc32\": {\n      \"version\": \"0.2.13\",\n      \"resolved\": \"https://registry.npmjs.org/buffer-crc32/-/buffer-crc32-0.2.13.tgz\",\n      \"integrity\": \"sha512-VO9Ht/+p3SN7SKWqcrgEzjGbRSJYTx+Q1pTQC0wrWqHx0vpJraQ6GtHx8tvcg1rlK1byhU5gccxgOgj7B0TDkQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \"*\"\n      }\n    },\n    \"node_modules/buffer-from\": {\n      \"version\": \"1.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/buffer-from/-/buffer-from-1.1.2.tgz\",\n      \"integrity\": \"sha512-E+XQCRwSbaaiChtv6k6Dwgc+bx+Bs6vuKJHHl5kox/BaKbhiXzqQOwK4cO22yElGp2OCmjwVhT3HmxgyPGnJfQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/builder-util\": {\n      \"version\": \"26.0.11\",\n      \"resolved\": \"https://registry.npmjs.org/builder-util/-/builder-util-26.0.11.tgz\",\n      \"integrity\": \"sha512-xNjXfsldUEe153h1DraD0XvDOpqGR0L5eKFkdReB7eFW5HqysDZFfly4rckda6y9dF39N3pkPlOblcfHKGw+uA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@types/debug\": \"^4.1.6\",\n        \"7zip-bin\": \"~5.2.0\",\n        \"app-builder-bin\": \"5.0.0-alpha.12\",\n        \"builder-util-runtime\": \"9.3.1\",\n        \"chalk\": \"^4.1.2\",\n        \"cross-spawn\": \"^7.0.6\",\n        \"debug\": \"^4.3.4\",\n        \"fs-extra\": \"^10.1.0\",\n        \"http-proxy-agent\": \"^7.0.0\",\n        \"https-proxy-agent\": \"^7.0.0\",\n        \"is-ci\": \"^3.0.0\",\n        \"js-yaml\": \"^4.1.0\",\n        \"sanitize-filename\": \"^1.6.3\",\n        \"source-map-support\": \"^0.5.19\",\n        \"stat-mode\": \"^1.0.0\",\n        \"temp-file\": \"^3.4.0\",\n        \"tiny-async-pool\": \"1.3.0\"\n      }\n    },\n    \"node_modules/builder-util-runtime\": {\n      \"version\": \"9.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/builder-util-runtime/-/builder-util-runtime-9.3.1.tgz\",\n      \"integrity\": \"sha512-2/egrNDDnRaxVwK3A+cJq6UOlqOdedGA7JPqCeJjN2Zjk1/QB/6QUi3b714ScIGS7HafFXTyzJEOr5b44I3kvQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"debug\": \"^4.3.4\",\n        \"sax\": \"^1.2.4\"\n      },\n      \"engines\": {\n        \"node\": \">=12.0.0\"\n      }\n    },\n    \"node_modules/builder-util/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/builder-util/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/builder-util/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/cacache\": {\n      \"version\": \"16.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/cacache/-/cacache-16.1.3.tgz\",\n      \"integrity\": \"sha512-/+Emcj9DAXxX4cwlLmRI9c166RuL3w30zp4R7Joiv2cQTtTtA+jeuCAjH3ZlGnYS3tKENSrKhAzVVP9GVyzeYQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"@npmcli/fs\": \"^2.1.0\",\n        \"@npmcli/move-file\": \"^2.0.0\",\n        \"chownr\": \"^2.0.0\",\n        \"fs-minipass\": \"^2.1.0\",\n        \"glob\": \"^8.0.1\",\n        \"infer-owner\": \"^1.0.4\",\n        \"lru-cache\": \"^7.7.1\",\n        \"minipass\": \"^3.1.6\",\n        \"minipass-collect\": \"^1.0.2\",\n        \"minipass-flush\": \"^1.0.5\",\n        \"minipass-pipeline\": \"^1.2.4\",\n        \"mkdirp\": \"^1.0.4\",\n        \"p-map\": \"^4.0.0\",\n        \"promise-inflight\": \"^1.0.1\",\n        \"rimraf\": \"^3.0.2\",\n        \"ssri\": \"^9.0.0\",\n        \"tar\": \"^6.1.11\",\n        \"unique-filename\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/cacache/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/cacache/node_modules/brace-expansion\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-2.0.3.tgz\",\n      \"integrity\": \"sha512-MCV/fYJEbqx68aE58kv2cA/kiky1G8vux3OR6/jbS+jIMe/6fJWa0DTzJU7dqijOWYwHi1t29FlfYI9uytqlpA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\"\n      }\n    },\n    \"node_modules/cacache/node_modules/glob\": {\n      \"version\": \"8.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/glob/-/glob-8.1.0.tgz\",\n      \"integrity\": \"sha512-r8hpEjiQEYlF2QU0df3dS+nxxSIreXQS1qRhMJM0Q5NDdR386C7jb7Hwwod8Fgiuex+k0GFjgft18yvxm5XoCQ==\",\n      \"deprecated\": \"Old versions of glob are not supported, and contain widely publicized security vulnerabilities, which have been fixed in the current version. Please update. Support for old versions may be purchased (at exorbitant rates) by contacting i@izs.me\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"fs.realpath\": \"^1.0.0\",\n        \"inflight\": \"^1.0.4\",\n        \"inherits\": \"2\",\n        \"minimatch\": \"^5.0.1\",\n        \"once\": \"^1.3.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/cacache/node_modules/lru-cache\": {\n      \"version\": \"7.18.3\",\n      \"resolved\": \"https://registry.npmjs.org/lru-cache/-/lru-cache-7.18.3.tgz\",\n      \"integrity\": \"sha512-jumlc0BIUrS3qJGgIkWZsyfAM7NCWiBcCDhnd+3NNM5KbBmLTgHVfWBcg6W+rLUsIpzpERPsvwUP7CckAQSOoA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/cacache/node_modules/minimatch\": {\n      \"version\": \"5.1.9\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-5.1.9.tgz\",\n      \"integrity\": \"sha512-7o1wEA2RyMP7Iu7GNba9vc0RWWGACJOCZBJX2GJWip0ikV+wcOsgVuY9uE8CPiyQhkGFSlhuSkZPavN7u1c2Fw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^2.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/cacheable-lookup\": {\n      \"version\": \"5.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/cacheable-lookup/-/cacheable-lookup-5.0.4.tgz\",\n      \"integrity\": \"sha512-2/kNscPhpcxrOigMZzbiWF7dz8ilhb/nIHU3EyZiXWXpeq/au8qJ8VhdftMkty3n7Gj6HIGalQG8oiBNB3AJgA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10.6.0\"\n      }\n    },\n    \"node_modules/cacheable-request\": {\n      \"version\": \"7.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/cacheable-request/-/cacheable-request-7.0.4.tgz\",\n      \"integrity\": \"sha512-v+p6ongsrp0yTGbJXjgxPow2+DL93DASP4kXCDKb8/bwRtt9OEF3whggkkDkGNzgcWy2XaF4a8nZglC7uElscg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"clone-response\": \"^1.0.2\",\n        \"get-stream\": \"^5.1.0\",\n        \"http-cache-semantics\": \"^4.0.0\",\n        \"keyv\": \"^4.0.0\",\n        \"lowercase-keys\": \"^2.0.0\",\n        \"normalize-url\": \"^6.0.1\",\n        \"responselike\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/call-bind-apply-helpers\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/call-bind-apply-helpers/-/call-bind-apply-helpers-1.0.2.tgz\",\n      \"integrity\": \"sha512-Sp1ablJ0ivDkSzjcaJdxEunN5/XvksFJ2sMBFfq6x0ryhQV/2b/KwFe21cMpmHtPOSij8K99/wSfoEuTObmuMQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"es-errors\": \"^1.3.0\",\n        \"function-bind\": \"^1.1.2\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/chalk\": {\n      \"version\": \"4.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/chalk/-/chalk-4.1.2.tgz\",\n      \"integrity\": \"sha512-oKnbhFyRIXpUuez8iBMmyEa4nbj4IOQyuhc/wy9kY7/WVPcwIO9VA668Pu8RkO7+0G76SLROeyw9CpQ061i4mA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ansi-styles\": \"^4.1.0\",\n        \"supports-color\": \"^7.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/chalk?sponsor=1\"\n      }\n    },\n    \"node_modules/chownr\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/chownr/-/chownr-2.0.0.tgz\",\n      \"integrity\": \"sha512-bIomtDF5KGpdogkLd9VspvFzk9KfpyyGlS8YFVZl7TGPBHL5snIOnxeshwVgPteQ9b4Eydl+pVbIyE1DcvCWgQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/chromium-pickle-js\": {\n      \"version\": \"0.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/chromium-pickle-js/-/chromium-pickle-js-0.2.0.tgz\",\n      \"integrity\": \"sha512-1R5Fho+jBq0DDydt+/vHWj5KJNJCKdARKOCwZUen84I5BreWoLqRLANH1U87eJy1tiASPtMnGqJJq0ZsLoRPOw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/ci-info\": {\n      \"version\": \"3.9.0\",\n      \"resolved\": \"https://registry.npmjs.org/ci-info/-/ci-info-3.9.0.tgz\",\n      \"integrity\": \"sha512-NIxF55hv4nSqQswkAeiOi1r83xy8JldOFDTWiug55KBu9Jnblncd2U6ViHmYgHf01TPZS77NJBhBMKdWj9HQMQ==\",\n      \"dev\": true,\n      \"funding\": [\n        {\n          \"type\": \"github\",\n          \"url\": \"https://github.com/sponsors/sibiraj-s\"\n        }\n      ],\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/clean-stack\": {\n      \"version\": \"2.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/clean-stack/-/clean-stack-2.2.0.tgz\",\n      \"integrity\": \"sha512-4diC9HaTE+KRAMWhDhrGOECgWZxoevMc5TlkObMqNSsVU62PYzXZ/SMTjzyGAFF1YusgxGcSWTEXBhp0CPwQ1A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=6\"\n      }\n    },\n    \"node_modules/cli-cursor\": {\n      \"version\": \"3.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/cli-cursor/-/cli-cursor-3.1.0.tgz\",\n      \"integrity\": \"sha512-I/zHAwsKf9FqGoXM4WWRACob9+SNukZTd94DWF57E4toouRulbCxcUh6RKUEOQlYTHJnzkPMySvPNaaSLNfLZw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"restore-cursor\": \"^3.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/cli-spinners\": {\n      \"version\": \"2.9.2\",\n      \"resolved\": \"https://registry.npmjs.org/cli-spinners/-/cli-spinners-2.9.2.tgz\",\n      \"integrity\": \"sha512-ywqV+5MmyL4E7ybXgKys4DugZbX0FC6LnwrhjuykIjnK9k8OQacQ7axGKnjDXWNhns0xot3bZI5h55H8yo9cJg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=6\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/cli-truncate\": {\n      \"version\": \"2.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/cli-truncate/-/cli-truncate-2.1.0.tgz\",\n      \"integrity\": \"sha512-n8fOixwDD6b/ObinzTrp1ZKFzbgvKZvuz/TvejnLn1aQfC6r52XEx85FmuC+3HI+JM7coBRXUvNqEU2PHVrHpg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"slice-ansi\": \"^3.0.0\",\n        \"string-width\": \"^4.2.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/cliui\": {\n      \"version\": \"8.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/cliui/-/cliui-8.0.1.tgz\",\n      \"integrity\": \"sha512-BSeNnyus75C4//NQ9gQt1/csTXyo/8Sb+afLAkzAptFuMsod9HFokGNudZpi/oQV73hnVK+sR+5PVRMd+Dr7YQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"string-width\": \"^4.2.0\",\n        \"strip-ansi\": \"^6.0.1\",\n        \"wrap-ansi\": \"^7.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/clone\": {\n      \"version\": \"1.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/clone/-/clone-1.0.4.tgz\",\n      \"integrity\": \"sha512-JQHZ2QMW6l3aH/j6xCqQThY/9OH4D/9ls34cgkUBiEeocRTU04tHfKPBsUK1PqZCUQM7GiA0IIXJSuXHI64Kbg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.8\"\n      }\n    },\n    \"node_modules/clone-response\": {\n      \"version\": \"1.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/clone-response/-/clone-response-1.0.3.tgz\",\n      \"integrity\": \"sha512-ROoL94jJH2dUVML2Y/5PEDNaSHgeOdSDicUyS7izcF63G6sTc/FTjLub4b8Il9S8S0beOfYt0TaA5qvFK+w0wA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"mimic-response\": \"^1.0.0\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/color-convert\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/color-convert/-/color-convert-2.0.1.tgz\",\n      \"integrity\": \"sha512-RRECPsj7iu/xb5oKYcsFHSppFNnsj/52OVTRKb4zP5onXwVF3zVmmToNcOfGC+CRDpfK/U584fMg38ZHCaElKQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"color-name\": \"~1.1.4\"\n      },\n      \"engines\": {\n        \"node\": \">=7.0.0\"\n      }\n    },\n    \"node_modules/color-name\": {\n      \"version\": \"1.1.4\",\n      \"resolved\": \"https://registry.npmjs.org/color-name/-/color-name-1.1.4.tgz\",\n      \"integrity\": \"sha512-dOy+3AuW3a2wNbZHIuMZpTcgjGuLU/uBL/ubcZF9OXbDo8ff4O8yVp5Bf0efS8uEoYo5q4Fx7dY9OgQGXgAsQA==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/combined-stream\": {\n      \"version\": \"1.0.8\",\n      \"resolved\": \"https://registry.npmjs.org/combined-stream/-/combined-stream-1.0.8.tgz\",\n      \"integrity\": \"sha512-FQN4MRfuJeHf7cBbBMJFXhKSDq+2kAArBlmRBvcvFE5BB1HZKXtSFASDhdlz9zOYwxh8lDdnvmMOe/+5cdoEdg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"delayed-stream\": \"~1.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.8\"\n      }\n    },\n    \"node_modules/commander\": {\n      \"version\": \"5.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/commander/-/commander-5.1.0.tgz\",\n      \"integrity\": \"sha512-P0CysNDQ7rtVw4QIQtm+MRxV66vKFSvlsQvGYXZWR3qFU0jlMKHZZZgw8e+8DSah4UDKMqnknRDQz+xuQXQ/Zg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 6\"\n      }\n    },\n    \"node_modules/compare-version\": {\n      \"version\": \"0.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/compare-version/-/compare-version-0.1.2.tgz\",\n      \"integrity\": \"sha512-pJDh5/4wrEnXX/VWRZvruAGHkzKdr46z11OlTPN+VrATlWWhSKewNCJ1futCO5C7eJB3nPMFZA1LeYtcFboZ2A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.10.0\"\n      }\n    },\n    \"node_modules/concat-map\": {\n      \"version\": \"0.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/concat-map/-/concat-map-0.0.1.tgz\",\n      \"integrity\": \"sha512-/Srv4dswyQNBfohGpz9o6Yb3Gz3SrUDqBH5rTuhGR7ahtlbYKnVxw2bCFMRljaA7EXHaXZ8wsHdodFvbkhKmqg==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/config-file-ts\": {\n      \"version\": \"0.2.8-rc1\",\n      \"resolved\": \"https://registry.npmjs.org/config-file-ts/-/config-file-ts-0.2.8-rc1.tgz\",\n      \"integrity\": \"sha512-GtNECbVI82bT4RiDIzBSVuTKoSHufnU7Ce7/42bkWZJZFLjmDF2WBpVsvRkhKCfKBnTBb3qZrBwPpFBU/Myvhg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"glob\": \"^10.3.12\",\n        \"typescript\": \"^5.4.3\"\n      }\n    },\n    \"node_modules/config-file-ts/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/config-file-ts/node_modules/brace-expansion\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-2.0.3.tgz\",\n      \"integrity\": \"sha512-MCV/fYJEbqx68aE58kv2cA/kiky1G8vux3OR6/jbS+jIMe/6fJWa0DTzJU7dqijOWYwHi1t29FlfYI9uytqlpA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\"\n      }\n    },\n    \"node_modules/config-file-ts/node_modules/glob\": {\n      \"version\": \"10.5.0\",\n      \"resolved\": \"https://registry.npmjs.org/glob/-/glob-10.5.0.tgz\",\n      \"integrity\": \"sha512-DfXN8DfhJ7NH3Oe7cFmu3NCu1wKbkReJ8TorzSAFbSKrlNaQSKfIzqYqVY8zlbs2NLBbWpRiU52GX2PbaBVNkg==\",\n      \"deprecated\": \"Old versions of glob are not supported, and contain widely publicized security vulnerabilities, which have been fixed in the current version. Please update. Support for old versions may be purchased (at exorbitant rates) by contacting i@izs.me\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"foreground-child\": \"^3.1.0\",\n        \"jackspeak\": \"^3.1.2\",\n        \"minimatch\": \"^9.0.4\",\n        \"minipass\": \"^7.1.2\",\n        \"package-json-from-dist\": \"^1.0.0\",\n        \"path-scurry\": \"^1.11.1\"\n      },\n      \"bin\": {\n        \"glob\": \"dist/esm/bin.mjs\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/config-file-ts/node_modules/minimatch\": {\n      \"version\": \"9.0.9\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-9.0.9.tgz\",\n      \"integrity\": \"sha512-OBwBN9AL4dqmETlpS2zasx+vTeWclWzkblfZk7KTA5j3jeOONz/tRCnZomUyvNg83wL5Zv9Ss6HMJXAgL8R2Yg==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^2.0.2\"\n      },\n      \"engines\": {\n        \"node\": \">=16 || 14 >=14.17\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/config-file-ts/node_modules/minipass\": {\n      \"version\": \"7.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/minipass/-/minipass-7.1.3.tgz\",\n      \"integrity\": \"sha512-tEBHqDnIoM/1rXME1zgka9g6Q2lcoCkxHLuc7ODJ5BxbP5d4c2Z5cGgtXAku59200Cx7diuHTOYfSBD8n6mm8A==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\",\n      \"engines\": {\n        \"node\": \">=16 || 14 >=14.17\"\n      }\n    },\n    \"node_modules/core-util-is\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/core-util-is/-/core-util-is-1.0.2.tgz\",\n      \"integrity\": \"sha512-3lqz5YjWTYnW6dlDa5TLaTCcShfar1e40rmcJVwCBJC6mWlFuj0eCHIElmG1g5kyuJ/GD+8Wn4FFCcz4gJPfaQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/crc\": {\n      \"version\": \"3.8.0\",\n      \"resolved\": \"https://registry.npmjs.org/crc/-/crc-3.8.0.tgz\",\n      \"integrity\": \"sha512-iX3mfgcTMIq3ZKLIsVFAbv7+Mc10kxabAGQb8HvjA1o3T1PIYprbakQ65d3I+2HGHt6nSKkM9PYjgoJO2KcFBQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"buffer\": \"^5.1.0\"\n      }\n    },\n    \"node_modules/cross-dirname\": {\n      \"version\": \"0.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/cross-dirname/-/cross-dirname-0.1.0.tgz\",\n      \"integrity\": \"sha512-+R08/oI0nl3vfPcqftZRpytksBXDzOUveBq/NBVx0sUp1axwzPQrKinNx5yd5sxPu8j1wIy8AfnVQ+5eFdha6Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"peer\": true\n    },\n    \"node_modules/cross-spawn\": {\n      \"version\": \"7.0.6\",\n      \"resolved\": \"https://registry.npmjs.org/cross-spawn/-/cross-spawn-7.0.6.tgz\",\n      \"integrity\": \"sha512-uV2QOWP2nWzsy2aMp8aRibhi9dlzF5Hgh5SHaB9OiTGEyDTiJJyx0uy51QXdyWbtAHNua4XJzUKca3OzKUd3vA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"path-key\": \"^3.1.0\",\n        \"shebang-command\": \"^2.0.0\",\n        \"which\": \"^2.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 8\"\n      }\n    },\n    \"node_modules/debug\": {\n      \"version\": \"4.4.3\",\n      \"resolved\": \"https://registry.npmjs.org/debug/-/debug-4.4.3.tgz\",\n      \"integrity\": \"sha512-RGwwWnwQvkVfavKVt22FGLw+xYSdzARwm0ru6DhTVA3umU5hZc28V3kO4stgYryrTlLpuvgI9GiijltAjNbcqA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ms\": \"^2.1.3\"\n      },\n      \"engines\": {\n        \"node\": \">=6.0\"\n      },\n      \"peerDependenciesMeta\": {\n        \"supports-color\": {\n          \"optional\": true\n        }\n      }\n    },\n    \"node_modules/decompress-response\": {\n      \"version\": \"6.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/decompress-response/-/decompress-response-6.0.0.tgz\",\n      \"integrity\": \"sha512-aW35yZM6Bb/4oJlZncMH2LCoZtJXTRxES17vE3hoRiowU2kWHaJKFkSBDnDR+cm9J+9QhXmREyIfv0pji9ejCQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"mimic-response\": \"^3.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/decompress-response/node_modules/mimic-response\": {\n      \"version\": \"3.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/mimic-response/-/mimic-response-3.1.0.tgz\",\n      \"integrity\": \"sha512-z0yWI+4FDrrweS8Zmt4Ej5HdJmky15+L2e6Wgn3+iK5fWzb6T3fhNFq2+MeTRb064c6Wr4N/wv0DzQTjNzHNGQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/defaults\": {\n      \"version\": \"1.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/defaults/-/defaults-1.0.4.tgz\",\n      \"integrity\": \"sha512-eFuaLoy/Rxalv2kr+lqMlUnrDWV+3j4pljOIJgLIhI058IQfWJ7vXhyEIHu+HtC738klGALYxOKDO0bQP3tg8A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"clone\": \"^1.0.2\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/defer-to-connect\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/defer-to-connect/-/defer-to-connect-2.0.1.tgz\",\n      \"integrity\": \"sha512-4tvttepXG1VaYGrRibk5EwJd1t4udunSOVMdLSAL6mId1ix438oPwPZMALY41FCijukO1L0twNcGsdzS7dHgDg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/define-data-property\": {\n      \"version\": \"1.1.4\",\n      \"resolved\": \"https://registry.npmjs.org/define-data-property/-/define-data-property-1.1.4.tgz\",\n      \"integrity\": \"sha512-rBMvIzlpA8v6E+SJZoo++HAYqsLrkg7MSfIinMPFhmkorw7X+dOXVJQs+QT69zGkzMyfDnIMN2Wid1+NbL3T+A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"es-define-property\": \"^1.0.0\",\n        \"es-errors\": \"^1.3.0\",\n        \"gopd\": \"^1.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/define-properties\": {\n      \"version\": \"1.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/define-properties/-/define-properties-1.2.1.tgz\",\n      \"integrity\": \"sha512-8QmQKqEASLd5nx0U1B1okLElbUuuttJ/AnYmRXbbbGDWh6uS208EjD4Xqq/I9wK7u0v6O08XhTWnt5XtEbR6Dg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"define-data-property\": \"^1.0.1\",\n        \"has-property-descriptors\": \"^1.0.0\",\n        \"object-keys\": \"^1.1.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/delayed-stream\": {\n      \"version\": \"1.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/delayed-stream/-/delayed-stream-1.0.0.tgz\",\n      \"integrity\": \"sha512-ZySD7Nf91aLB0RxL4KGrKHBXl7Eds1DAmEdcoVawXnLD7SDhpNgtuII2aAkg7a7QS41jxPSZ17p4VdGnMHk3MQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.4.0\"\n      }\n    },\n    \"node_modules/detect-libc\": {\n      \"version\": \"2.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/detect-libc/-/detect-libc-2.1.2.tgz\",\n      \"integrity\": \"sha512-Btj2BOOO83o3WyH59e8MgXsxEQVcarkUOpEYrubB0urwnN10yQ364rsiByU11nZlqWYZm05i/of7io4mzihBtQ==\",\n      \"dev\": true,\n      \"license\": \"Apache-2.0\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/detect-node\": {\n      \"version\": \"2.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/detect-node/-/detect-node-2.1.0.tgz\",\n      \"integrity\": \"sha512-T0NIuQpnTvFDATNuHN5roPwSBG83rFsuO+MXXH9/3N1eFbn4wcPjttvjMLEPWJ0RGUYgQE7cGgS3tNxbqCGM7g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/dir-compare\": {\n      \"version\": \"4.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/dir-compare/-/dir-compare-4.2.0.tgz\",\n      \"integrity\": \"sha512-2xMCmOoMrdQIPHdsTawECdNPwlVFB9zGcz3kuhmBO6U3oU+UQjsue0i8ayLKpgBcm+hcXPMVSGUN9d+pvJ6+VQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"minimatch\": \"^3.0.5\",\n        \"p-limit\": \"^3.1.0 \"\n      }\n    },\n    \"node_modules/dir-compare/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/dir-compare/node_modules/brace-expansion\": {\n      \"version\": \"1.1.13\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-1.1.13.tgz\",\n      \"integrity\": \"sha512-9ZLprWS6EENmhEOpjCYW2c8VkmOvckIJZfkr7rBW6dObmfgJ/L1GpSYW5Hpo9lDz4D1+n0Ckz8rU7FwHDQiG/w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\",\n        \"concat-map\": \"0.0.1\"\n      }\n    },\n    \"node_modules/dir-compare/node_modules/minimatch\": {\n      \"version\": \"3.1.5\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-3.1.5.tgz\",\n      \"integrity\": \"sha512-VgjWUsnnT6n+NUk6eZq77zeFdpW2LWDzP6zFGrCbHXiYNul5Dzqk2HHQ5uFH2DNW5Xbp8+jVzaeNt94ssEEl4w==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^1.1.7\"\n      },\n      \"engines\": {\n        \"node\": \"*\"\n      }\n    },\n    \"node_modules/dmg-builder\": {\n      \"version\": \"26.0.12\",\n      \"resolved\": \"https://registry.npmjs.org/dmg-builder/-/dmg-builder-26.0.12.tgz\",\n      \"integrity\": \"sha512-59CAAjAhTaIMCN8y9kD573vDkxbs1uhDcrFLHSgutYdPcGOU35Rf95725snvzEOy4BFB7+eLJ8djCNPmGwG67w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"app-builder-lib\": \"26.0.12\",\n        \"builder-util\": \"26.0.11\",\n        \"builder-util-runtime\": \"9.3.1\",\n        \"fs-extra\": \"^10.1.0\",\n        \"iconv-lite\": \"^0.6.2\",\n        \"js-yaml\": \"^4.1.0\"\n      },\n      \"optionalDependencies\": {\n        \"dmg-license\": \"^1.0.11\"\n      }\n    },\n    \"node_modules/dmg-builder/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/dmg-builder/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/dmg-builder/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/dmg-license\": {\n      \"version\": \"1.0.11\",\n      \"resolved\": \"https://registry.npmjs.org/dmg-license/-/dmg-license-1.0.11.tgz\",\n      \"integrity\": \"sha512-ZdzmqwKmECOWJpqefloC5OJy1+WZBBse5+MR88z9g9Zn4VY+WYUkAyojmhzJckH5YbbZGcYIuGAkY5/Ys5OM2Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"os\": [\n        \"darwin\"\n      ],\n      \"dependencies\": {\n        \"@types/plist\": \"^3.0.1\",\n        \"@types/verror\": \"^1.10.3\",\n        \"ajv\": \"^6.10.0\",\n        \"crc\": \"^3.8.0\",\n        \"iconv-corefoundation\": \"^1.1.7\",\n        \"plist\": \"^3.0.4\",\n        \"smart-buffer\": \"^4.0.2\",\n        \"verror\": \"^1.10.0\"\n      },\n      \"bin\": {\n        \"dmg-license\": \"bin/dmg-license.js\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/dotenv\": {\n      \"version\": \"16.6.1\",\n      \"resolved\": \"https://registry.npmjs.org/dotenv/-/dotenv-16.6.1.tgz\",\n      \"integrity\": \"sha512-uBq4egWHTcTt33a72vpSG0z3HnPuIl6NqYcTrKEg2azoEyl2hpW0zqlxysq2pK9HlDIHyHyakeYaYnSAwd8bow==\",\n      \"dev\": true,\n      \"license\": \"BSD-2-Clause\",\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://dotenvx.com\"\n      }\n    },\n    \"node_modules/dotenv-expand\": {\n      \"version\": \"11.0.7\",\n      \"resolved\": \"https://registry.npmjs.org/dotenv-expand/-/dotenv-expand-11.0.7.tgz\",\n      \"integrity\": \"sha512-zIHwmZPRshsCdpMDyVsqGmgyP0yT8GAgXUnkdAoJisxvf33k7yO6OuoKmcTGuXPWSsm8Oh88nZicRLA9Y0rUeA==\",\n      \"dev\": true,\n      \"license\": \"BSD-2-Clause\",\n      \"dependencies\": {\n        \"dotenv\": \"^16.4.5\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      },\n      \"funding\": {\n        \"url\": \"https://dotenvx.com\"\n      }\n    },\n    \"node_modules/dunder-proto\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/dunder-proto/-/dunder-proto-1.0.1.tgz\",\n      \"integrity\": \"sha512-KIN/nDJBQRcXw0MLVhZE9iQHmG68qAVIBg9CqmUYjmQIhgij9U5MFvrqkUL5FbtyyzZuOeOt0zdeRe4UY7ct+A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"call-bind-apply-helpers\": \"^1.0.1\",\n        \"es-errors\": \"^1.3.0\",\n        \"gopd\": \"^1.2.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/eastasianwidth\": {\n      \"version\": \"0.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/eastasianwidth/-/eastasianwidth-0.2.0.tgz\",\n      \"integrity\": \"sha512-I88TYZWc9XiYHRQ4/3c5rjjfgkjhLyW2luGIheGERbNQ6OY7yTybanSpDXZa8y7VUP9YmDcYa+eyq4ca7iLqWA==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/ejs\": {\n      \"version\": \"3.1.10\",\n      \"resolved\": \"https://registry.npmjs.org/ejs/-/ejs-3.1.10.tgz\",\n      \"integrity\": \"sha512-UeJmFfOrAQS8OJWPZ4qtgHyWExa088/MtK5UEyoJGFH67cDEXkZSviOiKRCZ4Xij0zxI3JECgYs3oKx+AizQBA==\",\n      \"dev\": true,\n      \"license\": \"Apache-2.0\",\n      \"dependencies\": {\n        \"jake\": \"^10.8.5\"\n      },\n      \"bin\": {\n        \"ejs\": \"bin/cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">=0.10.0\"\n      }\n    },\n    \"node_modules/electron\": {\n      \"version\": \"35.1.5\",\n      \"resolved\": \"https://registry.npmjs.org/electron/-/electron-35.1.5.tgz\",\n      \"integrity\": \"sha512-LolvbKKQUSCGvEwbEQNt1cxD1t+YYClDNwBIjn4d28KM8FSqUn9zJuf6AbqNA7tVs9OFl/EQpmg/m4lZV1hH8g==\",\n      \"dev\": true,\n      \"hasInstallScript\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@electron/get\": \"^2.0.0\",\n        \"@types/node\": \"^22.7.7\",\n        \"extract-zip\": \"^2.0.1\"\n      },\n      \"bin\": {\n        \"electron\": \"cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">= 12.20.55\"\n      }\n    },\n    \"node_modules/electron-builder\": {\n      \"version\": \"26.0.12\",\n      \"resolved\": \"https://registry.npmjs.org/electron-builder/-/electron-builder-26.0.12.tgz\",\n      \"integrity\": \"sha512-cD1kz5g2sgPTMFHjLxfMjUK5JABq3//J4jPswi93tOPFz6btzXYtK5NrDt717NRbukCUDOrrvmYVOWERlqoiXA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"app-builder-lib\": \"26.0.12\",\n        \"builder-util\": \"26.0.11\",\n        \"builder-util-runtime\": \"9.3.1\",\n        \"chalk\": \"^4.1.2\",\n        \"dmg-builder\": \"26.0.12\",\n        \"fs-extra\": \"^10.1.0\",\n        \"is-ci\": \"^3.0.0\",\n        \"lazy-val\": \"^1.0.5\",\n        \"simple-update-notifier\": \"2.0.0\",\n        \"yargs\": \"^17.6.2\"\n      },\n      \"bin\": {\n        \"electron-builder\": \"cli.js\",\n        \"install-app-deps\": \"install-app-deps.js\"\n      },\n      \"engines\": {\n        \"node\": \">=14.0.0\"\n      }\n    },\n    \"node_modules/electron-builder-squirrel-windows\": {\n      \"version\": \"26.0.12\",\n      \"resolved\": \"https://registry.npmjs.org/electron-builder-squirrel-windows/-/electron-builder-squirrel-windows-26.0.12.tgz\",\n      \"integrity\": \"sha512-kpwXM7c/ayRUbYVErQbsZ0nQZX4aLHQrPEG9C4h9vuJCXylwFH8a7Jgi2VpKIObzCXO7LKHiCw4KdioFLFOgqA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"peer\": true,\n      \"dependencies\": {\n        \"app-builder-lib\": \"26.0.12\",\n        \"builder-util\": \"26.0.11\",\n        \"electron-winstaller\": \"5.4.0\"\n      }\n    },\n    \"node_modules/electron-builder/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/electron-builder/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/electron-builder/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/electron-publish\": {\n      \"version\": \"26.0.11\",\n      \"resolved\": \"https://registry.npmjs.org/electron-publish/-/electron-publish-26.0.11.tgz\",\n      \"integrity\": \"sha512-a8QRH0rAPIWH9WyyS5LbNvW9Ark6qe63/LqDB7vu2JXYpi0Gma5Q60Dh4tmTqhOBQt0xsrzD8qE7C+D7j+B24A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@types/fs-extra\": \"^9.0.11\",\n        \"builder-util\": \"26.0.11\",\n        \"builder-util-runtime\": \"9.3.1\",\n        \"chalk\": \"^4.1.2\",\n        \"form-data\": \"^4.0.0\",\n        \"fs-extra\": \"^10.1.0\",\n        \"lazy-val\": \"^1.0.5\",\n        \"mime\": \"^2.5.2\"\n      }\n    },\n    \"node_modules/electron-publish/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/electron-publish/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/electron-publish/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/electron-winstaller\": {\n      \"version\": \"5.4.0\",\n      \"resolved\": \"https://registry.npmjs.org/electron-winstaller/-/electron-winstaller-5.4.0.tgz\",\n      \"integrity\": \"sha512-bO3y10YikuUwUuDUQRM4KfwNkKhnpVO7IPdbsrejwN9/AABJzzTQ4GeHwyzNSrVO+tEH3/Np255a3sVZpZDjvg==\",\n      \"dev\": true,\n      \"hasInstallScript\": true,\n      \"license\": \"MIT\",\n      \"peer\": true,\n      \"dependencies\": {\n        \"@electron/asar\": \"^3.2.1\",\n        \"debug\": \"^4.1.1\",\n        \"fs-extra\": \"^7.0.1\",\n        \"lodash\": \"^4.17.21\",\n        \"temp\": \"^0.9.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"@electron/windows-sign\": \"^1.1.2\"\n      }\n    },\n    \"node_modules/electron-winstaller/node_modules/fs-extra\": {\n      \"version\": \"7.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-7.0.1.tgz\",\n      \"integrity\": \"sha512-YJDaCJZEnBmcbw13fvdAM9AwNOJwOzrE4pqMqBq5nFiEqXUqHwlK4B+3pUw6JNvfSPtX05xFHtYy/1ni01eGCw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"peer\": true,\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.1.2\",\n        \"jsonfile\": \"^4.0.0\",\n        \"universalify\": \"^0.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=6 <7 || >=8\"\n      }\n    },\n    \"node_modules/emoji-regex\": {\n      \"version\": \"8.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/emoji-regex/-/emoji-regex-8.0.0.tgz\",\n      \"integrity\": \"sha512-MSjYzcWNOA0ewAHpz0MxpYFvwg6yjy1NG3xteoqz644VCo/RPgnr1/GGt+ic3iJTzQ8Eu3TdM14SawnVUmGE6A==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/encoding\": {\n      \"version\": \"0.1.13\",\n      \"resolved\": \"https://registry.npmjs.org/encoding/-/encoding-0.1.13.tgz\",\n      \"integrity\": \"sha512-ETBauow1T35Y/WZMkio9jiM0Z5xjHHmJ4XmjZOq1l/dXz3lr2sRn87nJy20RupqSh1F2m3HHPSp8ShIPQJrJ3A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"iconv-lite\": \"^0.6.2\"\n      }\n    },\n    \"node_modules/end-of-stream\": {\n      \"version\": \"1.4.5\",\n      \"resolved\": \"https://registry.npmjs.org/end-of-stream/-/end-of-stream-1.4.5.tgz\",\n      \"integrity\": \"sha512-ooEGc6HP26xXq/N+GCGOT0JKCLDGrq2bQUZrQ7gyrJiZANJ/8YDTxTpQBXGMn+WbIQXNVpyWymm7KYVICQnyOg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"once\": \"^1.4.0\"\n      }\n    },\n    \"node_modules/env-paths\": {\n      \"version\": \"2.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/env-paths/-/env-paths-2.2.1.tgz\",\n      \"integrity\": \"sha512-+h1lkLKhZMTYjog1VEpJNG7NZJWcuc2DDk/qsqSTRRCOXiLjeQ1d1/udrUGhqMxUgAlwKNZ0cf2uqan5GLuS2A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=6\"\n      }\n    },\n    \"node_modules/err-code\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/err-code/-/err-code-2.0.3.tgz\",\n      \"integrity\": \"sha512-2bmlRpNKBxT/CRmPOlyISQpNj+qSeYvcym/uT0Jx2bMOlKLtSy1ZmLuVxSEKKyor/N5yhvp/ZiG1oE3DEYMSFA==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/es-define-property\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/es-define-property/-/es-define-property-1.0.1.tgz\",\n      \"integrity\": \"sha512-e3nRfgfUZ4rNGL232gUgX06QNyyez04KdjFrF+LTRoOXmrOgFKDg4BCdsjW8EnT69eqdYGmRpJwiPVYNrCaW3g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/es-errors\": {\n      \"version\": \"1.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/es-errors/-/es-errors-1.3.0.tgz\",\n      \"integrity\": \"sha512-Zf5H2Kxt2xjTvbJvP2ZWLEICxA6j+hAmMzIlypy4xcBg1vKVnx89Wy0GbS+kf5cwCVFFzdCFh2XSCFNULS6csw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/es-object-atoms\": {\n      \"version\": \"1.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/es-object-atoms/-/es-object-atoms-1.1.1.tgz\",\n      \"integrity\": \"sha512-FGgH2h8zKNim9ljj7dankFPcICIK9Cp5bm+c2gQSYePhpaG5+esrLODihIorn+Pe6FGJzWhXQotPv73jTaldXA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"es-errors\": \"^1.3.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/es-set-tostringtag\": {\n      \"version\": \"2.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/es-set-tostringtag/-/es-set-tostringtag-2.1.0.tgz\",\n      \"integrity\": \"sha512-j6vWzfrGVfyXxge+O0x5sh6cvxAog0a/4Rdd2K36zCMV5eJ+/+tOAngRO8cODMNWbVRdVlmGZQL2YS3yR8bIUA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"es-errors\": \"^1.3.0\",\n        \"get-intrinsic\": \"^1.2.6\",\n        \"has-tostringtag\": \"^1.0.2\",\n        \"hasown\": \"^2.0.2\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/es6-error\": {\n      \"version\": \"4.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/es6-error/-/es6-error-4.1.1.tgz\",\n      \"integrity\": \"sha512-Um/+FxMr9CISWh0bi5Zv0iOD+4cFh5qLeks1qhAopKVAJw3drgKbKySikp7wGhDL0HPeaja0P5ULZrxLkniUVg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/escalade\": {\n      \"version\": \"3.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/escalade/-/escalade-3.2.0.tgz\",\n      \"integrity\": \"sha512-WUj2qlxaQtO4g6Pq5c29GTcWGDyd8itL8zTlipgECz3JesAiiOKotd8JU6otB3PACgG6xkJUyVhboMS+bje/jA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=6\"\n      }\n    },\n    \"node_modules/escape-string-regexp\": {\n      \"version\": \"4.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/escape-string-regexp/-/escape-string-regexp-4.0.0.tgz\",\n      \"integrity\": \"sha512-TtpcNJ3XAzx3Gq8sWRzJaVajRs0uVxA2YAkdb1jm2YkPz4G6egUFAyA3n5vtEIZefPk5Wa4UXbKuS5fKkJWdgA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/exponential-backoff\": {\n      \"version\": \"3.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/exponential-backoff/-/exponential-backoff-3.1.3.tgz\",\n      \"integrity\": \"sha512-ZgEeZXj30q+I0EN+CbSSpIyPaJ5HVQD18Z1m+u1FXbAeT94mr1zw50q4q6jiiC447Nl/YTcIYSAftiGqetwXCA==\",\n      \"dev\": true,\n      \"license\": \"Apache-2.0\"\n    },\n    \"node_modules/extract-zip\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/extract-zip/-/extract-zip-2.0.1.tgz\",\n      \"integrity\": \"sha512-GDhU9ntwuKyGXdZBUgTIe+vXnWj0fppUEtMDL0+idd5Sta8TGpHssn/eusA9mrPr9qNDym6SxAYZjNvCn/9RBg==\",\n      \"dev\": true,\n      \"license\": \"BSD-2-Clause\",\n      \"dependencies\": {\n        \"debug\": \"^4.1.1\",\n        \"get-stream\": \"^5.1.0\",\n        \"yauzl\": \"^2.10.0\"\n      },\n      \"bin\": {\n        \"extract-zip\": \"cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">= 10.17.0\"\n      },\n      \"optionalDependencies\": {\n        \"@types/yauzl\": \"^2.9.1\"\n      }\n    },\n    \"node_modules/extsprintf\": {\n      \"version\": \"1.4.1\",\n      \"resolved\": \"https://registry.npmjs.org/extsprintf/-/extsprintf-1.4.1.tgz\",\n      \"integrity\": \"sha512-Wrk35e8ydCKDj/ArClo1VrPVmN8zph5V4AtHwIuHhvMXsKf73UT3BOD+azBIW+3wOJ4FhEH7zyaJCFvChjYvMA==\",\n      \"dev\": true,\n      \"engines\": [\n        \"node >=0.6.0\"\n      ],\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/fast-deep-equal\": {\n      \"version\": \"3.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/fast-deep-equal/-/fast-deep-equal-3.1.3.tgz\",\n      \"integrity\": \"sha512-f3qQ9oQy9j2AhBe/H9VC91wLmKBCCU/gDOnKNAYG5hswO7BLKj09Hc5HYNz9cGI++xlpDCIgDaitVs03ATR84Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/fast-json-stable-stringify\": {\n      \"version\": \"2.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fast-json-stable-stringify/-/fast-json-stable-stringify-2.1.0.tgz\",\n      \"integrity\": \"sha512-lhd/wF+Lk98HZoTCtlVraHtfh5XYijIjalXck7saUtuanSDyLMxnHhSXEDJqHxD7msR8D0uCmqlkwjCV8xvwHw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/fd-slicer\": {\n      \"version\": \"1.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fd-slicer/-/fd-slicer-1.1.0.tgz\",\n      \"integrity\": \"sha512-cE1qsB/VwyQozZ+q1dGxR8LBYNZeofhEdUNGSMbQD3Gw2lAzX9Zb3uIU6Ebc/Fmyjo9AWWfnn0AUCHqtevs/8g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"pend\": \"~1.2.0\"\n      }\n    },\n    \"node_modules/filelist\": {\n      \"version\": \"1.0.6\",\n      \"resolved\": \"https://registry.npmjs.org/filelist/-/filelist-1.0.6.tgz\",\n      \"integrity\": \"sha512-5giy2PkLYY1cP39p17Ech+2xlpTRL9HLspOfEgm0L6CwBXBTgsK5ou0JtzYuepxkaQ/tvhCFIJ5uXo0OrM2DxA==\",\n      \"dev\": true,\n      \"license\": \"Apache-2.0\",\n      \"dependencies\": {\n        \"minimatch\": \"^5.0.1\"\n      }\n    },\n    \"node_modules/filelist/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/filelist/node_modules/brace-expansion\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-2.0.3.tgz\",\n      \"integrity\": \"sha512-MCV/fYJEbqx68aE58kv2cA/kiky1G8vux3OR6/jbS+jIMe/6fJWa0DTzJU7dqijOWYwHi1t29FlfYI9uytqlpA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\"\n      }\n    },\n    \"node_modules/filelist/node_modules/minimatch\": {\n      \"version\": \"5.1.9\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-5.1.9.tgz\",\n      \"integrity\": \"sha512-7o1wEA2RyMP7Iu7GNba9vc0RWWGACJOCZBJX2GJWip0ikV+wcOsgVuY9uE8CPiyQhkGFSlhuSkZPavN7u1c2Fw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^2.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/foreground-child\": {\n      \"version\": \"3.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/foreground-child/-/foreground-child-3.3.1.tgz\",\n      \"integrity\": \"sha512-gIXjKqtFuWEgzFRJA9WCQeSJLZDjgJUOMCMzxtvFq/37KojM1BFGufqsCy0r4qSQmYLsZYMeyRqzIWOMup03sw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"cross-spawn\": \"^7.0.6\",\n        \"signal-exit\": \"^4.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=14\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/foreground-child/node_modules/signal-exit\": {\n      \"version\": \"4.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/signal-exit/-/signal-exit-4.1.0.tgz\",\n      \"integrity\": \"sha512-bzyZ1e88w9O1iNJbKnOlvYTrWPDl46O1bG0D3XInv+9tkPrxrN8jUUTiFlDkkmKWgn1M6CfIA13SuGqOa9Korw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">=14\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/form-data\": {\n      \"version\": \"4.0.5\",\n      \"resolved\": \"https://registry.npmjs.org/form-data/-/form-data-4.0.5.tgz\",\n      \"integrity\": \"sha512-8RipRLol37bNs2bhoV67fiTEvdTrbMUYcFTiy3+wuuOnUog2QBHCZWXDRijWQfAkhBj2Uf5UnVaiWwA5vdd82w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"asynckit\": \"^0.4.0\",\n        \"combined-stream\": \"^1.0.8\",\n        \"es-set-tostringtag\": \"^2.1.0\",\n        \"hasown\": \"^2.0.2\",\n        \"mime-types\": \"^2.1.12\"\n      },\n      \"engines\": {\n        \"node\": \">= 6\"\n      }\n    },\n    \"node_modules/fs-extra\": {\n      \"version\": \"8.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-8.1.0.tgz\",\n      \"integrity\": \"sha512-yhlQgA6mnOJUKOsRUFsgJdQCvkKhcz8tlZG5HBQfReYZy46OwLcY+Zia0mtdHsOo9y/hP+CxMN0TU9QxoOtG4g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^4.0.0\",\n        \"universalify\": \"^0.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=6 <7 || >=8\"\n      }\n    },\n    \"node_modules/fs-minipass\": {\n      \"version\": \"2.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-minipass/-/fs-minipass-2.1.0.tgz\",\n      \"integrity\": \"sha512-V/JgOLFCS+R6Vcq0slCuaeWEdNC3ouDlJMNIsacH2VtALiu9mV4LPrHc5cDl8k5aw6J8jwgWWpiTo5RYhmIzvg==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"minipass\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 8\"\n      }\n    },\n    \"node_modules/fs.realpath\": {\n      \"version\": \"1.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs.realpath/-/fs.realpath-1.0.0.tgz\",\n      \"integrity\": \"sha512-OO0pH2lK6a0hZnAdau5ItzHPI6pUlvI7jMVnxUQRtw4owF2wk8lOSabtGDCTP4Ggrg2MbGnWO9X8K1t4+fGMDw==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/function-bind\": {\n      \"version\": \"1.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/function-bind/-/function-bind-1.1.2.tgz\",\n      \"integrity\": \"sha512-7XHNxH7qX9xG5mIwxkhumTox/MIRNcOgDrxWsMt2pAr23WHp6MrRlN7FBSFpCpr+oVO0F744iUgR82nJMfG2SA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/get-caller-file\": {\n      \"version\": \"2.0.5\",\n      \"resolved\": \"https://registry.npmjs.org/get-caller-file/-/get-caller-file-2.0.5.tgz\",\n      \"integrity\": \"sha512-DyFP3BM/3YHTQOCUL/w0OZHR0lpKeGrxotcHWcqNEdnltqFwXVfhEBQ94eIo34AfQpo0rGki4cyIiftY06h2Fg==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \"6.* || 8.* || >= 10.*\"\n      }\n    },\n    \"node_modules/get-intrinsic\": {\n      \"version\": \"1.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/get-intrinsic/-/get-intrinsic-1.3.0.tgz\",\n      \"integrity\": \"sha512-9fSjSaos/fRIVIp+xSJlE6lfwhES7LNtKaCBIamHsjr2na1BiABJPo0mOjjz8GJDURarmCPGqaiVg5mfjb98CQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"call-bind-apply-helpers\": \"^1.0.2\",\n        \"es-define-property\": \"^1.0.1\",\n        \"es-errors\": \"^1.3.0\",\n        \"es-object-atoms\": \"^1.1.1\",\n        \"function-bind\": \"^1.1.2\",\n        \"get-proto\": \"^1.0.1\",\n        \"gopd\": \"^1.2.0\",\n        \"has-symbols\": \"^1.1.0\",\n        \"hasown\": \"^2.0.2\",\n        \"math-intrinsics\": \"^1.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/get-proto\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/get-proto/-/get-proto-1.0.1.tgz\",\n      \"integrity\": \"sha512-sTSfBjoXBp89JvIKIefqw7U2CCebsc74kiY6awiGogKtoSGbgjYE/G/+l9sF3MWFPNc9IcoOC4ODfKHfxFmp0g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"dunder-proto\": \"^1.0.1\",\n        \"es-object-atoms\": \"^1.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/get-stream\": {\n      \"version\": \"5.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/get-stream/-/get-stream-5.2.0.tgz\",\n      \"integrity\": \"sha512-nBF+F1rAZVCu/p7rjzgA+Yb4lfYXrpl7a6VmJrU8wF9I1CKvP/QwPNZHnOlwbTkY6dvtFIzFMSyQXbLoTQPRpA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"pump\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/glob\": {\n      \"version\": \"7.2.3\",\n      \"resolved\": \"https://registry.npmjs.org/glob/-/glob-7.2.3.tgz\",\n      \"integrity\": \"sha512-nFR0zLpU2YCaRxwoCJvL6UvCH2JFyFVIvwTLsIf21AuHlMskA1hhTdk+LlYJtOlYt9v6dvszD2BGRqBL+iQK9Q==\",\n      \"deprecated\": \"Old versions of glob are not supported, and contain widely publicized security vulnerabilities, which have been fixed in the current version. Please update. Support for old versions may be purchased (at exorbitant rates) by contacting i@izs.me\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"fs.realpath\": \"^1.0.0\",\n        \"inflight\": \"^1.0.4\",\n        \"inherits\": \"2\",\n        \"minimatch\": \"^3.1.1\",\n        \"once\": \"^1.3.0\",\n        \"path-is-absolute\": \"^1.0.0\"\n      },\n      \"engines\": {\n        \"node\": \"*\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/glob/node_modules/balanced-match\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/balanced-match/-/balanced-match-1.0.2.tgz\",\n      \"integrity\": \"sha512-3oSeUO0TMV67hN1AmbXsK4yaqU7tjiHlbxRDZOpH0KW9+CeX4bRAaX0Anxt0tx2MrpRpWwQaPwIlISEJhYU5Pw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/glob/node_modules/brace-expansion\": {\n      \"version\": \"1.1.13\",\n      \"resolved\": \"https://registry.npmjs.org/brace-expansion/-/brace-expansion-1.1.13.tgz\",\n      \"integrity\": \"sha512-9ZLprWS6EENmhEOpjCYW2c8VkmOvckIJZfkr7rBW6dObmfgJ/L1GpSYW5Hpo9lDz4D1+n0Ckz8rU7FwHDQiG/w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"balanced-match\": \"^1.0.0\",\n        \"concat-map\": \"0.0.1\"\n      }\n    },\n    \"node_modules/glob/node_modules/minimatch\": {\n      \"version\": \"3.1.5\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-3.1.5.tgz\",\n      \"integrity\": \"sha512-VgjWUsnnT6n+NUk6eZq77zeFdpW2LWDzP6zFGrCbHXiYNul5Dzqk2HHQ5uFH2DNW5Xbp8+jVzaeNt94ssEEl4w==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^1.1.7\"\n      },\n      \"engines\": {\n        \"node\": \"*\"\n      }\n    },\n    \"node_modules/global-agent\": {\n      \"version\": \"3.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/global-agent/-/global-agent-3.0.0.tgz\",\n      \"integrity\": \"sha512-PT6XReJ+D07JvGoxQMkT6qji/jVNfX/h364XHZOWeRzy64sSFr+xJ5OX7LI3b4MPQzdL4H8Y8M0xzPpsVMwA8Q==\",\n      \"dev\": true,\n      \"license\": \"BSD-3-Clause\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"boolean\": \"^3.0.1\",\n        \"es6-error\": \"^4.1.1\",\n        \"matcher\": \"^3.0.0\",\n        \"roarr\": \"^2.15.3\",\n        \"semver\": \"^7.3.2\",\n        \"serialize-error\": \"^7.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=10.0\"\n      }\n    },\n    \"node_modules/global-agent/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"optional\": true,\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/globalthis\": {\n      \"version\": \"1.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/globalthis/-/globalthis-1.0.4.tgz\",\n      \"integrity\": \"sha512-DpLKbNU4WylpxJykQujfCcwYWiV/Jhm50Goo0wrVILAv5jOr9d+H+UR3PhSCD2rCCEIg0uc+G+muBTwD54JhDQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"define-properties\": \"^1.2.1\",\n        \"gopd\": \"^1.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/gopd\": {\n      \"version\": \"1.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/gopd/-/gopd-1.2.0.tgz\",\n      \"integrity\": \"sha512-ZUKRh6/kUFoAiTAtTYPZJ3hw9wNxx+BIBOijnlG9PnrJsCcSjs1wyyD6vJpaYtgnzDrKYRSqf3OO6Rfa93xsRg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/got\": {\n      \"version\": \"11.8.6\",\n      \"resolved\": \"https://registry.npmjs.org/got/-/got-11.8.6.tgz\",\n      \"integrity\": \"sha512-6tfZ91bOr7bOXnK7PRDCGBLa1H4U080YHNaAQ2KsMGlLEzRbk44nsZF2E1IeRc3vtJHPVbKCYgdFbaGO2ljd8g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@sindresorhus/is\": \"^4.0.0\",\n        \"@szmarczak/http-timer\": \"^4.0.5\",\n        \"@types/cacheable-request\": \"^6.0.1\",\n        \"@types/responselike\": \"^1.0.0\",\n        \"cacheable-lookup\": \"^5.0.3\",\n        \"cacheable-request\": \"^7.0.2\",\n        \"decompress-response\": \"^6.0.0\",\n        \"http2-wrapper\": \"^1.0.0-beta.5.2\",\n        \"lowercase-keys\": \"^2.0.0\",\n        \"p-cancelable\": \"^2.0.0\",\n        \"responselike\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10.19.0\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sindresorhus/got?sponsor=1\"\n      }\n    },\n    \"node_modules/graceful-fs\": {\n      \"version\": \"4.2.11\",\n      \"resolved\": \"https://registry.npmjs.org/graceful-fs/-/graceful-fs-4.2.11.tgz\",\n      \"integrity\": \"sha512-RbJ5/jmFcNNCcDV5o9eTnBLJ/HszWV0P73bc+Ff4nS/rJj+YaS6IGyiOL0VoBYX+l1Wrl3k63h/KrH+nhJ0XvQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/has-flag\": {\n      \"version\": \"4.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/has-flag/-/has-flag-4.0.0.tgz\",\n      \"integrity\": \"sha512-EykJT/Q1KjTWctppgIAgfSO0tKVuZUjhgMr17kqTumMl6Afv3EISleU7qZUzoXDFTAHTDC4NOoG/ZxU3EvlMPQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/has-property-descriptors\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/has-property-descriptors/-/has-property-descriptors-1.0.2.tgz\",\n      \"integrity\": \"sha512-55JNKuIW+vq4Ke1BjOTjM2YctQIvCT7GFzHwmfZPGo5wnrgkid0YQtnAleFSqumZm4az3n2BS+erby5ipJdgrg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"es-define-property\": \"^1.0.0\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/has-symbols\": {\n      \"version\": \"1.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/has-symbols/-/has-symbols-1.1.0.tgz\",\n      \"integrity\": \"sha512-1cDNdwJ2Jaohmb3sg4OmKaMBwuC48sYni5HUw2DvsC8LjGTLK9h+eb1X6RyuOHe4hT0ULCW68iomhjUoKUqlPQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/has-tostringtag\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/has-tostringtag/-/has-tostringtag-1.0.2.tgz\",\n      \"integrity\": \"sha512-NqADB8VjPFLM2V0VvHUewwwsw0ZWBaIdgo+ieHtK3hasLz4qeCRjYcqfB6AQrBggRKppKF8L52/VqdVsO47Dlw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"has-symbols\": \"^1.0.3\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/hasown\": {\n      \"version\": \"2.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/hasown/-/hasown-2.0.2.tgz\",\n      \"integrity\": \"sha512-0hJU9SCPvmMzIBdZFqNPXWa6dqh7WdH0cII9y+CyS8rG3nL48Bclra9HmKhVVUHyPWNH5Y7xDwAB7bfgSjkUMQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"function-bind\": \"^1.1.2\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/hosted-git-info\": {\n      \"version\": \"4.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/hosted-git-info/-/hosted-git-info-4.1.0.tgz\",\n      \"integrity\": \"sha512-kyCuEOWjJqZuDbRHzL8V93NzQhwIB71oFWSyzVo+KPZI+pnQPPxucdkrOZvkLRnrf5URsQM+IJ09Dw29cRALIA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"lru-cache\": \"^6.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/http-cache-semantics\": {\n      \"version\": \"4.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/http-cache-semantics/-/http-cache-semantics-4.2.0.tgz\",\n      \"integrity\": \"sha512-dTxcvPXqPvXBQpq5dUr6mEMJX4oIEFv6bwom3FDwKRDsuIjjJGANqhBuoAn9c1RQJIdAKav33ED65E2ys+87QQ==\",\n      \"dev\": true,\n      \"license\": \"BSD-2-Clause\"\n    },\n    \"node_modules/http-proxy-agent\": {\n      \"version\": \"7.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/http-proxy-agent/-/http-proxy-agent-7.0.2.tgz\",\n      \"integrity\": \"sha512-T1gkAiYYDWYx3V5Bmyu7HcfcvL7mUrTWiM6yOfa3PIphViJ/gFPbvidQ+veqSOHci/PxBcDabeUNCzpOODJZig==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"agent-base\": \"^7.1.0\",\n        \"debug\": \"^4.3.4\"\n      },\n      \"engines\": {\n        \"node\": \">= 14\"\n      }\n    },\n    \"node_modules/http2-wrapper\": {\n      \"version\": \"1.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/http2-wrapper/-/http2-wrapper-1.0.3.tgz\",\n      \"integrity\": \"sha512-V+23sDMr12Wnz7iTcDeJr3O6AIxlnvT/bmaAAAP/Xda35C90p9599p0F1eHR/N1KILWSoWVAiOMFjBBXaXSMxg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"quick-lru\": \"^5.1.1\",\n        \"resolve-alpn\": \"^1.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10.19.0\"\n      }\n    },\n    \"node_modules/https-proxy-agent\": {\n      \"version\": \"7.0.6\",\n      \"resolved\": \"https://registry.npmjs.org/https-proxy-agent/-/https-proxy-agent-7.0.6.tgz\",\n      \"integrity\": \"sha512-vK9P5/iUfdl95AI+JVyUuIcVtd4ofvtrOr3HNtM2yxC9bnMbEdp3x01OhQNnjb8IJYi38VlTE3mBXwcfvywuSw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"agent-base\": \"^7.1.2\",\n        \"debug\": \"4\"\n      },\n      \"engines\": {\n        \"node\": \">= 14\"\n      }\n    },\n    \"node_modules/humanize-ms\": {\n      \"version\": \"1.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/humanize-ms/-/humanize-ms-1.2.1.tgz\",\n      \"integrity\": \"sha512-Fl70vYtsAFb/C06PTS9dZBo7ihau+Tu/DNCk/OyHhea07S+aeMWpFFkUaXRa8fI+ScZbEI8dfSxwY7gxZ9SAVQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ms\": \"^2.0.0\"\n      }\n    },\n    \"node_modules/iconv-corefoundation\": {\n      \"version\": \"1.1.7\",\n      \"resolved\": \"https://registry.npmjs.org/iconv-corefoundation/-/iconv-corefoundation-1.1.7.tgz\",\n      \"integrity\": \"sha512-T10qvkw0zz4wnm560lOEg0PovVqUXuOFhhHAkixw8/sycy7TJt7v/RrkEKEQnAw2viPSJu6iAkErxnzR0g8PpQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"os\": [\n        \"darwin\"\n      ],\n      \"dependencies\": {\n        \"cli-truncate\": \"^2.1.0\",\n        \"node-addon-api\": \"^1.6.3\"\n      },\n      \"engines\": {\n        \"node\": \"^8.11.2 || >=10\"\n      }\n    },\n    \"node_modules/iconv-lite\": {\n      \"version\": \"0.6.3\",\n      \"resolved\": \"https://registry.npmjs.org/iconv-lite/-/iconv-lite-0.6.3.tgz\",\n      \"integrity\": \"sha512-4fCk79wshMdzMp2rH06qWrJE4iolqLhCUH+OiuIgU++RB0+94NlDL81atO7GX55uUKueo0txHNtvEyI6D7WdMw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"safer-buffer\": \">= 2.1.2 < 3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=0.10.0\"\n      }\n    },\n    \"node_modules/ieee754\": {\n      \"version\": \"1.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/ieee754/-/ieee754-1.2.1.tgz\",\n      \"integrity\": \"sha512-dcyqhDvX1C46lXZcVqCpK+FtMRQVdIMN6/Df5js2zouUsqG7I6sFxitIC+7KYK29KdXOLHdu9zL4sFnoVQnqaA==\",\n      \"dev\": true,\n      \"funding\": [\n        {\n          \"type\": \"github\",\n          \"url\": \"https://github.com/sponsors/feross\"\n        },\n        {\n          \"type\": \"patreon\",\n          \"url\": \"https://www.patreon.com/feross\"\n        },\n        {\n          \"type\": \"consulting\",\n          \"url\": \"https://feross.org/support\"\n        }\n      ],\n      \"license\": \"BSD-3-Clause\"\n    },\n    \"node_modules/imurmurhash\": {\n      \"version\": \"0.1.4\",\n      \"resolved\": \"https://registry.npmjs.org/imurmurhash/-/imurmurhash-0.1.4.tgz\",\n      \"integrity\": \"sha512-JmXMZ6wuvDmLiHEml9ykzqO6lwFbof0GG4IkcGaENdCRDDmMVnny7s5HsIgHCbaq0w2MyPhDqkhTUgS2LU2PHA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.8.19\"\n      }\n    },\n    \"node_modules/indent-string\": {\n      \"version\": \"4.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/indent-string/-/indent-string-4.0.0.tgz\",\n      \"integrity\": \"sha512-EdDDZu4A2OyIK7Lr/2zG+w5jmbuk1DVBnEwREQvBzspBJkCEbRa8GxU1lghYcaGJCnRWibjDXlq779X1/y5xwg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/infer-owner\": {\n      \"version\": \"1.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/infer-owner/-/infer-owner-1.0.4.tgz\",\n      \"integrity\": \"sha512-IClj+Xz94+d7irH5qRyfJonOdfTzuDaifE6ZPWfx0N0+/ATZCbuTPq2prFl526urkQd90WyUKIh1DfBQ2hMz9A==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/inflight\": {\n      \"version\": \"1.0.6\",\n      \"resolved\": \"https://registry.npmjs.org/inflight/-/inflight-1.0.6.tgz\",\n      \"integrity\": \"sha512-k92I/b08q4wvFscXCLvqfsHCrjrF7yiXsQuIVvVE7N82W3+aqpzuUdBbfhWcy/FZR3/4IgflMgKLOsvPDrGCJA==\",\n      \"deprecated\": \"This module is not supported, and leaks memory. Do not use it. Check out lru-cache if you want a good and tested way to coalesce async requests by a key value, which is much more comprehensive and powerful.\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"once\": \"^1.3.0\",\n        \"wrappy\": \"1\"\n      }\n    },\n    \"node_modules/inherits\": {\n      \"version\": \"2.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/inherits/-/inherits-2.0.4.tgz\",\n      \"integrity\": \"sha512-k/vGaX4/Yla3WzyMCvTQOXYeIHvqOKtnqBduzTHpzpQZzAskKMhZ2K+EnBiSM9zGSoIFeMpXKxa4dYeZIQqewQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/ip-address\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/ip-address/-/ip-address-10.1.0.tgz\",\n      \"integrity\": \"sha512-XXADHxXmvT9+CRxhXg56LJovE+bmWnEWB78LB83VZTprKTmaC5QfruXocxzTZ2Kl0DNwKuBdlIhjL8LeY8Sf8Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 12\"\n      }\n    },\n    \"node_modules/is-ci\": {\n      \"version\": \"3.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/is-ci/-/is-ci-3.0.1.tgz\",\n      \"integrity\": \"sha512-ZYvCgrefwqoQ6yTyYUbQu64HsITZ3NfKX1lzaEYdkTDcfKzzCI/wthRRYKkdjHKFVgNiXKAKm65Zo1pk2as/QQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ci-info\": \"^3.2.0\"\n      },\n      \"bin\": {\n        \"is-ci\": \"bin.js\"\n      }\n    },\n    \"node_modules/is-fullwidth-code-point\": {\n      \"version\": \"3.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/is-fullwidth-code-point/-/is-fullwidth-code-point-3.0.0.tgz\",\n      \"integrity\": \"sha512-zymm5+u+sCsSWyD9qNaejV3DFvhCKclKdizYaJUuHA83RLjb7nSuGnddCHGv0hk+KY7BMAlsWeK4Ueg6EV6XQg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/is-interactive\": {\n      \"version\": \"1.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/is-interactive/-/is-interactive-1.0.0.tgz\",\n      \"integrity\": \"sha512-2HvIEKRoqS62guEC+qBjpvRubdX910WCMuJTZ+I9yvqKU2/12eSL549HMwtabb4oupdj2sMP50k+XJfB/8JE6w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/is-lambda\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/is-lambda/-/is-lambda-1.0.1.tgz\",\n      \"integrity\": \"sha512-z7CMFGNrENq5iFB9Bqo64Xk6Y9sg+epq1myIcdHaGnbMTYOxvzsEtdYqQUylB7LxfkvgrrjP32T6Ywciio9UIQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/is-unicode-supported\": {\n      \"version\": \"0.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/is-unicode-supported/-/is-unicode-supported-0.1.0.tgz\",\n      \"integrity\": \"sha512-knxG2q4UC3u8stRGyAVJCOdxFmv5DZiRcdlIaAQXAbSfJya+OhopNotLQrstBhququ4ZpuKbDc/8S6mgXgPFPw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/isbinaryfile\": {\n      \"version\": \"5.0.7\",\n      \"resolved\": \"https://registry.npmjs.org/isbinaryfile/-/isbinaryfile-5.0.7.tgz\",\n      \"integrity\": \"sha512-gnWD14Jh3FzS3CPhF0AxNOJ8CxqeblPTADzI38r0wt8ZyQl5edpy75myt08EG2oKvpyiqSqsx+Wkz9vtkbTqYQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 18.0.0\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/gjtorikian/\"\n      }\n    },\n    \"node_modules/isexe\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/isexe/-/isexe-2.0.0.tgz\",\n      \"integrity\": \"sha512-RHxMLp9lnKHGHRng9QFhRCMbYAcVpn69smSGcq3f36xjgVVWThj4qqLbTLlq7Ssj8B+fIQ1EuCEGI2lKsyQeIw==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/jackspeak\": {\n      \"version\": \"3.4.3\",\n      \"resolved\": \"https://registry.npmjs.org/jackspeak/-/jackspeak-3.4.3.tgz\",\n      \"integrity\": \"sha512-OGlZQpz2yfahA/Rd1Y8Cd9SIEsqvXkLVoSw/cgwhnhFMDbsQFeZYoJJ7bIZBS9BcamUW96asq/npPWugM+RQBw==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\",\n      \"dependencies\": {\n        \"@isaacs/cliui\": \"^8.0.2\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      },\n      \"optionalDependencies\": {\n        \"@pkgjs/parseargs\": \"^0.11.0\"\n      }\n    },\n    \"node_modules/jake\": {\n      \"version\": \"10.9.4\",\n      \"resolved\": \"https://registry.npmjs.org/jake/-/jake-10.9.4.tgz\",\n      \"integrity\": \"sha512-wpHYzhxiVQL+IV05BLE2Xn34zW1S223hvjtqk0+gsPrwd/8JNLXJgZZM/iPFsYc1xyphF+6M6EvdE5E9MBGkDA==\",\n      \"dev\": true,\n      \"license\": \"Apache-2.0\",\n      \"dependencies\": {\n        \"async\": \"^3.2.6\",\n        \"filelist\": \"^1.0.4\",\n        \"picocolors\": \"^1.1.1\"\n      },\n      \"bin\": {\n        \"jake\": \"bin/cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/js-yaml\": {\n      \"version\": \"4.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/js-yaml/-/js-yaml-4.1.1.tgz\",\n      \"integrity\": \"sha512-qQKT4zQxXl8lLwBtHMWwaTcGfFOZviOJet3Oy/xmGk2gZH677CJM9EvtfdSkgWcATZhj/55JZ0rmy3myCT5lsA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"argparse\": \"^2.0.1\"\n      },\n      \"bin\": {\n        \"js-yaml\": \"bin/js-yaml.js\"\n      }\n    },\n    \"node_modules/json-buffer\": {\n      \"version\": \"3.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/json-buffer/-/json-buffer-3.0.1.tgz\",\n      \"integrity\": \"sha512-4bV5BfR2mqfQTJm+V5tPPdf+ZpuhiIvTuAB5g8kcrXOZpTT/QwwVRWBywX1ozr6lEuPdbHxwaJlm9G6mI2sfSQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/json-schema-traverse\": {\n      \"version\": \"0.4.1\",\n      \"resolved\": \"https://registry.npmjs.org/json-schema-traverse/-/json-schema-traverse-0.4.1.tgz\",\n      \"integrity\": \"sha512-xbbCH5dCYU5T8LcEhhuh7HJ88HXuW3qsI3Y0zOZFKfZEHcpWiHU/Jxzk629Brsab/mMiHQti9wMP+845RPe3Vg==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/json-stringify-safe\": {\n      \"version\": \"5.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/json-stringify-safe/-/json-stringify-safe-5.0.1.tgz\",\n      \"integrity\": \"sha512-ZClg6AaYvamvYEE82d3Iyd3vSSIjQ+odgjaTzRuO3s7toCdFKczob2i0zCh7JE8kWn17yvAWhUVxvqGwUalsRA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"optional\": true\n    },\n    \"node_modules/json5\": {\n      \"version\": \"2.2.3\",\n      \"resolved\": \"https://registry.npmjs.org/json5/-/json5-2.2.3.tgz\",\n      \"integrity\": \"sha512-XmOWe7eyHYH14cLdVPoyg+GOH3rYX++KpzrylJwSW98t3Nk+U8XOl8FWKOgwtzdb8lXGf6zYwDUzeHMWfxasyg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"bin\": {\n        \"json5\": \"lib/cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">=6\"\n      }\n    },\n    \"node_modules/jsonfile\": {\n      \"version\": \"4.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-4.0.0.tgz\",\n      \"integrity\": \"sha512-m6F1R3z8jjlf2imQHS2Qez5sjKWQzbuuhuJ/FKYFRZvPE3PuHcSMVZzfsLhGVOkfd20obL5SWEBew5ShlquNxg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/keyv\": {\n      \"version\": \"4.5.4\",\n      \"resolved\": \"https://registry.npmjs.org/keyv/-/keyv-4.5.4.tgz\",\n      \"integrity\": \"sha512-oxVHkHR/EJf2CNXnWxRLW6mg7JyCCUcG0DtEGmL2ctUo1PNTin1PUil+r/+4r5MpVgC/fn1kjsx7mjSujKqIpw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"json-buffer\": \"3.0.1\"\n      }\n    },\n    \"node_modules/lazy-val\": {\n      \"version\": \"1.0.5\",\n      \"resolved\": \"https://registry.npmjs.org/lazy-val/-/lazy-val-1.0.5.tgz\",\n      \"integrity\": \"sha512-0/BnGCCfyUMkBpeDgWihanIAF9JmZhHBgUhEqzvf+adhNGLoP6TaiI5oF8oyb3I45P+PcnrqihSf01M0l0G5+Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/lodash\": {\n      \"version\": \"4.18.1\",\n      \"resolved\": \"https://registry.npmjs.org/lodash/-/lodash-4.18.1.tgz\",\n      \"integrity\": \"sha512-dMInicTPVE8d1e5otfwmmjlxkZoUpiVLwyeTdUsi/Caj/gfzzblBcCE5sRHV/AsjuCmxWrte2TNGSYuCeCq+0Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/log-symbols\": {\n      \"version\": \"4.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/log-symbols/-/log-symbols-4.1.0.tgz\",\n      \"integrity\": \"sha512-8XPvpAA8uyhfteu8pIvQxpJZ7SYYdpUivZpGy6sFsBuKRY/7rQGavedeB8aK+Zkyq6upMFVL/9AW6vOYzfRyLg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"chalk\": \"^4.1.0\",\n        \"is-unicode-supported\": \"^0.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/lowercase-keys\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/lowercase-keys/-/lowercase-keys-2.0.0.tgz\",\n      \"integrity\": \"sha512-tqNXrS78oMOE73NMxK4EMLQsQowWf8jKooH9g7xPavRT706R6bkQJ6DY2Te7QukaZsulxa30wQ7bk0pm4XiHmA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/lru-cache\": {\n      \"version\": \"6.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/lru-cache/-/lru-cache-6.0.0.tgz\",\n      \"integrity\": \"sha512-Jo6dJ04CmSjuznwJSS3pUeWmd/H0ffTlkXXgwZi+eq1UCmqQwCh+eLsYOYCwY991i2Fah4h1BEMCx4qThGbsiA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"yallist\": \"^4.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/make-fetch-happen\": {\n      \"version\": \"10.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/make-fetch-happen/-/make-fetch-happen-10.2.1.tgz\",\n      \"integrity\": \"sha512-NgOPbRiaQM10DYXvN3/hhGVI2M5MtITFryzBGxHM5p4wnFxsVCbxkrBrDsk+EZ5OB4jEOT7AjDxtdF+KVEFT7w==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"agentkeepalive\": \"^4.2.1\",\n        \"cacache\": \"^16.1.0\",\n        \"http-cache-semantics\": \"^4.1.0\",\n        \"http-proxy-agent\": \"^5.0.0\",\n        \"https-proxy-agent\": \"^5.0.0\",\n        \"is-lambda\": \"^1.0.1\",\n        \"lru-cache\": \"^7.7.1\",\n        \"minipass\": \"^3.1.6\",\n        \"minipass-collect\": \"^1.0.2\",\n        \"minipass-fetch\": \"^2.0.3\",\n        \"minipass-flush\": \"^1.0.5\",\n        \"minipass-pipeline\": \"^1.2.4\",\n        \"negotiator\": \"^0.6.3\",\n        \"promise-retry\": \"^2.0.1\",\n        \"socks-proxy-agent\": \"^7.0.0\",\n        \"ssri\": \"^9.0.0\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/make-fetch-happen/node_modules/agent-base\": {\n      \"version\": \"6.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/agent-base/-/agent-base-6.0.2.tgz\",\n      \"integrity\": \"sha512-RZNwNclF7+MS/8bDg70amg32dyeZGZxiDuQmZxKLAlQjr3jGyLx+4Kkk58UO7D2QdgFIQCovuSuZESne6RG6XQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"debug\": \"4\"\n      },\n      \"engines\": {\n        \"node\": \">= 6.0.0\"\n      }\n    },\n    \"node_modules/make-fetch-happen/node_modules/http-proxy-agent\": {\n      \"version\": \"5.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/http-proxy-agent/-/http-proxy-agent-5.0.0.tgz\",\n      \"integrity\": \"sha512-n2hY8YdoRE1i7r6M0w9DIw5GgZN0G25P8zLCRQ8rjXtTU3vsNFBI/vWK/UIeE6g5MUUz6avwAPXmL6Fy9D/90w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@tootallnate/once\": \"2\",\n        \"agent-base\": \"6\",\n        \"debug\": \"4\"\n      },\n      \"engines\": {\n        \"node\": \">= 6\"\n      }\n    },\n    \"node_modules/make-fetch-happen/node_modules/https-proxy-agent\": {\n      \"version\": \"5.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/https-proxy-agent/-/https-proxy-agent-5.0.1.tgz\",\n      \"integrity\": \"sha512-dFcAjpTQFgoLMzC2VwU+C/CbS7uRL0lWmxDITmqm7C+7F0Odmj6s9l6alZc6AELXhrnggM2CeWSXHGOdX2YtwA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"agent-base\": \"6\",\n        \"debug\": \"4\"\n      },\n      \"engines\": {\n        \"node\": \">= 6\"\n      }\n    },\n    \"node_modules/make-fetch-happen/node_modules/lru-cache\": {\n      \"version\": \"7.18.3\",\n      \"resolved\": \"https://registry.npmjs.org/lru-cache/-/lru-cache-7.18.3.tgz\",\n      \"integrity\": \"sha512-jumlc0BIUrS3qJGgIkWZsyfAM7NCWiBcCDhnd+3NNM5KbBmLTgHVfWBcg6W+rLUsIpzpERPsvwUP7CckAQSOoA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/matcher\": {\n      \"version\": \"3.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/matcher/-/matcher-3.0.0.tgz\",\n      \"integrity\": \"sha512-OkeDaAZ/bQCxeFAozM55PKcKU0yJMPGifLwV4Qgjitu+5MoAfSQN4lsLJeXZ1b8w0x+/Emda6MZgXS1jvsapng==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"escape-string-regexp\": \"^4.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/math-intrinsics\": {\n      \"version\": \"1.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/math-intrinsics/-/math-intrinsics-1.1.0.tgz\",\n      \"integrity\": \"sha512-/IXtbwEk5HTPyEwyKX6hGkYXxM9nbj64B+ilVJnC/R6B0pH5G4V3b0pVbL7DBj4tkhBAppbQUlf6F6Xl9LHu1g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/mime\": {\n      \"version\": \"2.6.0\",\n      \"resolved\": \"https://registry.npmjs.org/mime/-/mime-2.6.0.tgz\",\n      \"integrity\": \"sha512-USPkMeET31rOMiarsBNIHZKLGgvKc/LrjofAnBlOttf5ajRvqiRA8QsenbcooctK6d6Ts6aqZXBA+XbkKthiQg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"bin\": {\n        \"mime\": \"cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">=4.0.0\"\n      }\n    },\n    \"node_modules/mime-db\": {\n      \"version\": \"1.52.0\",\n      \"resolved\": \"https://registry.npmjs.org/mime-db/-/mime-db-1.52.0.tgz\",\n      \"integrity\": \"sha512-sPU4uV7dYlvtWJxwwxHD0PuihVNiE7TyAbQ5SWxDCB9mUYvOgroQOwYQQOKPJ8CIbE+1ETVlOoK1UC2nU3gYvg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 0.6\"\n      }\n    },\n    \"node_modules/mime-types\": {\n      \"version\": \"2.1.35\",\n      \"resolved\": \"https://registry.npmjs.org/mime-types/-/mime-types-2.1.35.tgz\",\n      \"integrity\": \"sha512-ZDY+bPm5zTTF+YpCrAU9nK0UgICYPT0QtT1NZWFv4s++TNkcgVaT0g6+4R2uI4MjQjzysHB1zxuWL50hzaeXiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"mime-db\": \"1.52.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 0.6\"\n      }\n    },\n    \"node_modules/mimic-fn\": {\n      \"version\": \"2.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/mimic-fn/-/mimic-fn-2.1.0.tgz\",\n      \"integrity\": \"sha512-OqbOk5oEQeAZ8WXWydlu9HJjz9WVdEIvamMCcXmuqUYjTknH/sqsWvhQ3vgwKFRR1HpjvNBKQ37nbJgYzGqGcg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=6\"\n      }\n    },\n    \"node_modules/mimic-response\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/mimic-response/-/mimic-response-1.0.1.tgz\",\n      \"integrity\": \"sha512-j5EctnkH7amfV/q5Hgmoal1g2QHFJRraOtmx0JpIqkxhBhI/lJSl1nMpQ45hVarwNETOoWEimndZ4QK0RHxuxQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=4\"\n      }\n    },\n    \"node_modules/minimatch\": {\n      \"version\": \"10.2.5\",\n      \"resolved\": \"https://registry.npmjs.org/minimatch/-/minimatch-10.2.5.tgz\",\n      \"integrity\": \"sha512-MULkVLfKGYDFYejP07QOurDLLQpcjk7Fw+7jXS2R2czRQzR56yHRveU5NDJEOviH+hETZKSkIk5c+T23GjFUMg==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\",\n      \"dependencies\": {\n        \"brace-expansion\": \"^5.0.5\"\n      },\n      \"engines\": {\n        \"node\": \"18 || 20 || >=22\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/minimist\": {\n      \"version\": \"1.2.8\",\n      \"resolved\": \"https://registry.npmjs.org/minimist/-/minimist-1.2.8.tgz\",\n      \"integrity\": \"sha512-2yyAR8qBkN3YuheJanUpWC5U3bb5osDywNB8RzDVlDwDHbocAJveqqj1u8+SVD7jkWT4yvsHCpWqqWqAxb0zCA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/ljharb\"\n      }\n    },\n    \"node_modules/minipass\": {\n      \"version\": \"3.3.6\",\n      \"resolved\": \"https://registry.npmjs.org/minipass/-/minipass-3.3.6.tgz\",\n      \"integrity\": \"sha512-DxiNidxSEK+tHG6zOIklvNOwm3hvCrbUrdtzY74U6HKTJxvIDfOUL5W5P2Ghd3DTkhhKPYGqeNUIh5qcM4YBfw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"yallist\": \"^4.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/minipass-collect\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/minipass-collect/-/minipass-collect-1.0.2.tgz\",\n      \"integrity\": \"sha512-6T6lH0H8OG9kITm/Jm6tdooIbogG9e0tLgpY6mphXSm/A9u8Nq1ryBG+Qspiub9LjWlBPsPS3tWQ/Botq4FdxA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"minipass\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 8\"\n      }\n    },\n    \"node_modules/minipass-fetch\": {\n      \"version\": \"2.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/minipass-fetch/-/minipass-fetch-2.1.2.tgz\",\n      \"integrity\": \"sha512-LT49Zi2/WMROHYoqGgdlQIZh8mLPZmOrN2NdJjMXxYe4nkN6FUyuPuOAOedNJDrx0IRGg9+4guZewtp8hE6TxA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"minipass\": \"^3.1.6\",\n        \"minipass-sized\": \"^1.0.3\",\n        \"minizlib\": \"^2.1.2\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"encoding\": \"^0.1.13\"\n      }\n    },\n    \"node_modules/minipass-flush\": {\n      \"version\": \"1.0.7\",\n      \"resolved\": \"https://registry.npmjs.org/minipass-flush/-/minipass-flush-1.0.7.tgz\",\n      \"integrity\": \"sha512-TbqTz9cUwWyHS2Dy89P3ocAGUGxKjjLuR9z8w4WUTGAVgEj17/4nhgo2Du56i0Fm3Pm30g4iA8Lcqctc76jCzA==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\",\n      \"dependencies\": {\n        \"minipass\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 8\"\n      }\n    },\n    \"node_modules/minipass-pipeline\": {\n      \"version\": \"1.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/minipass-pipeline/-/minipass-pipeline-1.2.4.tgz\",\n      \"integrity\": \"sha512-xuIq7cIOt09RPRJ19gdi4b+RiNvDFYe5JH+ggNvBqGqpQXcru3PcRmOZuHBKWK1Txf9+cQ+HMVN4d6z46LZP7A==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"minipass\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/minipass-sized\": {\n      \"version\": \"1.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/minipass-sized/-/minipass-sized-1.0.3.tgz\",\n      \"integrity\": \"sha512-MbkQQ2CTiBMlA2Dm/5cY+9SWFEN8pzzOXi6rlM5Xxq0Yqbda5ZQy9sU75a673FE9ZK0Zsbr6Y5iP6u9nktfg2g==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"minipass\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/minizlib\": {\n      \"version\": \"2.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/minizlib/-/minizlib-2.1.2.tgz\",\n      \"integrity\": \"sha512-bAxsR8BVfj60DWXHE3u30oHzfl4G7khkSuPW+qvpd7jFRHm7dLxOjUk1EHACJ/hxLY8phGJ0YhYHZo7jil7Qdg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"minipass\": \"^3.0.0\",\n        \"yallist\": \"^4.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 8\"\n      }\n    },\n    \"node_modules/mkdirp\": {\n      \"version\": \"1.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/mkdirp/-/mkdirp-1.0.4.tgz\",\n      \"integrity\": \"sha512-vVqVZQyf3WLx2Shd0qJ9xuvqgAyKPLAiqITEtqW0oIUjzo3PePDd6fW9iFz30ef7Ysp/oiWqbhszeGWW2T6Gzw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"bin\": {\n        \"mkdirp\": \"bin/cmd.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/ms\": {\n      \"version\": \"2.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/ms/-/ms-2.1.3.tgz\",\n      \"integrity\": \"sha512-6FlzubTLZG3J2a/NVCAleEhjzq5oxgHyaCU9yYXvcLsvoVaHJq/s5xXI6/XXP6tz7R9xAOtHnSO/tXtF3WRTlA==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/negotiator\": {\n      \"version\": \"0.6.4\",\n      \"resolved\": \"https://registry.npmjs.org/negotiator/-/negotiator-0.6.4.tgz\",\n      \"integrity\": \"sha512-myRT3DiWPHqho5PrJaIRyaMv2kgYf0mUVgBNOYMuCH5Ki1yEiQaf/ZJuQ62nvpc44wL5WDbTX7yGJi1Neevw8w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 0.6\"\n      }\n    },\n    \"node_modules/node-abi\": {\n      \"version\": \"3.89.0\",\n      \"resolved\": \"https://registry.npmjs.org/node-abi/-/node-abi-3.89.0.tgz\",\n      \"integrity\": \"sha512-6u9UwL0HlAl21+agMN3YAMXcKByMqwGx+pq+P76vii5f7hTPtKDp08/H9py6DY+cfDw7kQNTGEj/rly3IgbNQA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"semver\": \"^7.3.5\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/node-abi/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/node-addon-api\": {\n      \"version\": \"1.7.2\",\n      \"resolved\": \"https://registry.npmjs.org/node-addon-api/-/node-addon-api-1.7.2.tgz\",\n      \"integrity\": \"sha512-ibPK3iA+vaY1eEjESkQkM0BbCqFOaZMiXRTtdB0u7b4djtY6JnsjvPdUHVMg6xQt3B8fpTTWHI9A+ADjM9frzg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/node-api-version\": {\n      \"version\": \"0.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/node-api-version/-/node-api-version-0.2.1.tgz\",\n      \"integrity\": \"sha512-2xP/IGGMmmSQpI1+O/k72jF/ykvZ89JeuKX3TLJAYPDVLUalrshrLHkeVcCCZqG/eEa635cr8IBYzgnDvM2O8Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"semver\": \"^7.3.5\"\n      }\n    },\n    \"node_modules/node-api-version/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/nopt\": {\n      \"version\": \"6.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/nopt/-/nopt-6.0.0.tgz\",\n      \"integrity\": \"sha512-ZwLpbTgdhuZUnZzjd7nb1ZV+4DoiC6/sfiVKok72ym/4Tlf+DFdlHYmT2JPmcNNWV6Pi3SDf1kT+A4r9RTuT9g==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"abbrev\": \"^1.0.0\"\n      },\n      \"bin\": {\n        \"nopt\": \"bin/nopt.js\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/normalize-url\": {\n      \"version\": \"6.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/normalize-url/-/normalize-url-6.1.0.tgz\",\n      \"integrity\": \"sha512-DlL+XwOy3NxAQ8xuC0okPgK46iuVNAK01YN7RueYBqqFeGsBjV9XmCAzAdgt+667bCl5kPh9EqKKDwnaPG1I7A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/object-keys\": {\n      \"version\": \"1.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/object-keys/-/object-keys-1.1.1.tgz\",\n      \"integrity\": \"sha512-NuAESUOUMrlIXOfHKzD6bpPu3tYt3xvjNdRIQ+FeT0lNb4K8WR70CaDxhuNguS2XG+GjkyMwOzsN5ZktImfhLA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"engines\": {\n        \"node\": \">= 0.4\"\n      }\n    },\n    \"node_modules/once\": {\n      \"version\": \"1.4.0\",\n      \"resolved\": \"https://registry.npmjs.org/once/-/once-1.4.0.tgz\",\n      \"integrity\": \"sha512-lNaJgI+2Q5URQBkccEKHTQOPaXdUxnZZElQTZY0MFUAuaEqe1E+Nyvgdz/aIyNi6Z9MzO5dv1H8n58/GELp3+w==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"wrappy\": \"1\"\n      }\n    },\n    \"node_modules/onetime\": {\n      \"version\": \"5.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/onetime/-/onetime-5.1.2.tgz\",\n      \"integrity\": \"sha512-kbpaSSGJTWdAY5KPVeMOKXSrPtr8C8C7wodJbcsd51jRnmD+GZu8Y0VoU6Dm5Z4vWr0Ig/1NKuWRKf7j5aaYSg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"mimic-fn\": \"^2.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=6\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/ora\": {\n      \"version\": \"5.4.1\",\n      \"resolved\": \"https://registry.npmjs.org/ora/-/ora-5.4.1.tgz\",\n      \"integrity\": \"sha512-5b6Y85tPxZZ7QytO+BQzysW31HJku27cRIlkbAXaNx+BdcVi+LlRFmVXzeF6a7JCwJpyw5c4b+YSVImQIrBpuQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"bl\": \"^4.1.0\",\n        \"chalk\": \"^4.1.0\",\n        \"cli-cursor\": \"^3.1.0\",\n        \"cli-spinners\": \"^2.5.0\",\n        \"is-interactive\": \"^1.0.0\",\n        \"is-unicode-supported\": \"^0.1.0\",\n        \"log-symbols\": \"^4.1.0\",\n        \"strip-ansi\": \"^6.0.0\",\n        \"wcwidth\": \"^1.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/p-cancelable\": {\n      \"version\": \"2.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/p-cancelable/-/p-cancelable-2.1.1.tgz\",\n      \"integrity\": \"sha512-BZOr3nRQHOntUjTrH8+Lh54smKHoHyur8We1V8DSMVrl5A2malOOwuJRnKRDjSnkoeBh4at6BwEnb5I7Jl31wg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/p-limit\": {\n      \"version\": \"3.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/p-limit/-/p-limit-3.1.0.tgz\",\n      \"integrity\": \"sha512-TYOanM3wGwNGsZN2cVTYPArw454xnXj5qmWF1bEoAc4+cU/ol7GVh7odevjp1FNHduHc3KZMcFduxU5Xc6uJRQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"yocto-queue\": \"^0.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/p-map\": {\n      \"version\": \"4.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/p-map/-/p-map-4.0.0.tgz\",\n      \"integrity\": \"sha512-/bjOqmgETBYB5BoEeGVea8dmvHb2m9GLy1E9W43yeyfP6QQCZGFNa+XRceJEuDB6zqr+gKpIAmlLebMpykw/MQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"aggregate-error\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/package-json-from-dist\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/package-json-from-dist/-/package-json-from-dist-1.0.1.tgz\",\n      \"integrity\": \"sha512-UEZIS3/by4OC8vL3P2dTXRETpebLI2NiI5vIrjaD/5UtrkFX/tNbwjTSRAGC/+7CAo2pIcBaRgWmcBBHcsaCIw==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\"\n    },\n    \"node_modules/path-is-absolute\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/path-is-absolute/-/path-is-absolute-1.0.1.tgz\",\n      \"integrity\": \"sha512-AVbw3UJ2e9bq64vSaS9Am0fje1Pa8pbGqTTsmXfaIiMpnr5DlDhfJOuLj9Sf95ZPVDAUerDfEk88MPmPe7UCQg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.10.0\"\n      }\n    },\n    \"node_modules/path-key\": {\n      \"version\": \"3.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/path-key/-/path-key-3.1.1.tgz\",\n      \"integrity\": \"sha512-ojmeN0qd+y0jszEtoY48r0Peq5dwMEkIlCOu6Q5f41lfkswXuKtYrhgoTpLnyIcHm24Uhqx+5Tqm2InSwLhE6Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/path-scurry\": {\n      \"version\": \"1.11.1\",\n      \"resolved\": \"https://registry.npmjs.org/path-scurry/-/path-scurry-1.11.1.tgz\",\n      \"integrity\": \"sha512-Xa4Nw17FS9ApQFJ9umLiJS4orGjm7ZzwUrwamcGQuHSzDyth9boKDaycYdDcZDuqYATXw4HFXgaqWTctW/v1HA==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\",\n      \"dependencies\": {\n        \"lru-cache\": \"^10.2.0\",\n        \"minipass\": \"^5.0.0 || ^6.0.2 || ^7.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=16 || 14 >=14.18\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/path-scurry/node_modules/lru-cache\": {\n      \"version\": \"10.4.3\",\n      \"resolved\": \"https://registry.npmjs.org/lru-cache/-/lru-cache-10.4.3.tgz\",\n      \"integrity\": \"sha512-JNAzZcXrCt42VGLuYz0zfAzDfAvJWW6AfYlDBQyDV5DClI2m5sAmK+OIO7s59XfsRsWHp02jAJrRadPRGTt6SQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/path-scurry/node_modules/minipass\": {\n      \"version\": \"7.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/minipass/-/minipass-7.1.3.tgz\",\n      \"integrity\": \"sha512-tEBHqDnIoM/1rXME1zgka9g6Q2lcoCkxHLuc7ODJ5BxbP5d4c2Z5cGgtXAku59200Cx7diuHTOYfSBD8n6mm8A==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\",\n      \"engines\": {\n        \"node\": \">=16 || 14 >=14.17\"\n      }\n    },\n    \"node_modules/pe-library\": {\n      \"version\": \"0.4.1\",\n      \"resolved\": \"https://registry.npmjs.org/pe-library/-/pe-library-0.4.1.tgz\",\n      \"integrity\": \"sha512-eRWB5LBz7PpDu4PUlwT0PhnQfTQJlDDdPa35urV4Osrm0t0AqQFGn+UIkU3klZvwJ8KPO3VbBFsXquA6p6kqZw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=12\",\n        \"npm\": \">=6\"\n      },\n      \"funding\": {\n        \"type\": \"github\",\n        \"url\": \"https://github.com/sponsors/jet2jet\"\n      }\n    },\n    \"node_modules/pend\": {\n      \"version\": \"1.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/pend/-/pend-1.2.0.tgz\",\n      \"integrity\": \"sha512-F3asv42UuXchdzt+xXqfW1OGlVBe+mxa2mqI0pg5yAHZPvFmY3Y6drSf/GQ1A86WgWEN9Kzh/WrgKa6iGcHXLg==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/picocolors\": {\n      \"version\": \"1.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/picocolors/-/picocolors-1.1.1.tgz\",\n      \"integrity\": \"sha512-xceH2snhtb5M9liqDsmEw56le376mTZkEX/jEb/RxNFyegNul7eNslCXP9FDj/Lcu0X8KEyMceP2ntpaHrDEVA==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/plist\": {\n      \"version\": \"3.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/plist/-/plist-3.1.0.tgz\",\n      \"integrity\": \"sha512-uysumyrvkUX0rX/dEVqt8gC3sTBzd4zoWfLeS29nb53imdaXVvLINYXTI2GNqzaMuvacNx4uJQ8+b3zXR0pkgQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"@xmldom/xmldom\": \"^0.8.8\",\n        \"base64-js\": \"^1.5.1\",\n        \"xmlbuilder\": \"^15.1.1\"\n      },\n      \"engines\": {\n        \"node\": \">=10.4.0\"\n      }\n    },\n    \"node_modules/postject\": {\n      \"version\": \"1.0.0-alpha.6\",\n      \"resolved\": \"https://registry.npmjs.org/postject/-/postject-1.0.0-alpha.6.tgz\",\n      \"integrity\": \"sha512-b9Eb8h2eVqNE8edvKdwqkrY6O7kAwmI8kcnBv1NScolYJbo59XUF0noFq+lxbC1yN20bmC0WBEbDC5H/7ASb0A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"peer\": true,\n      \"dependencies\": {\n        \"commander\": \"^9.4.0\"\n      },\n      \"bin\": {\n        \"postject\": \"dist/cli.js\"\n      },\n      \"engines\": {\n        \"node\": \">=14.0.0\"\n      }\n    },\n    \"node_modules/postject/node_modules/commander\": {\n      \"version\": \"9.5.0\",\n      \"resolved\": \"https://registry.npmjs.org/commander/-/commander-9.5.0.tgz\",\n      \"integrity\": \"sha512-KRs7WVDKg86PWiuAqhDrAQnTXZKraVcCc6vFdL14qrZ/DcWwuRo7VoiYXalXO7S5GKpqYiVEwCbgFDfxNHKJBQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"peer\": true,\n      \"engines\": {\n        \"node\": \"^12.20.0 || >=14\"\n      }\n    },\n    \"node_modules/proc-log\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/proc-log/-/proc-log-2.0.1.tgz\",\n      \"integrity\": \"sha512-Kcmo2FhfDTXdcbfDH76N7uBYHINxc/8GW7UAVuVP9I+Va3uHSerrnKV6dLooga/gh7GlgzuCCr/eoldnL1muGw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/progress\": {\n      \"version\": \"2.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/progress/-/progress-2.0.3.tgz\",\n      \"integrity\": \"sha512-7PiHtLll5LdnKIMw100I+8xJXR5gW2QwWYkT6iJva0bXitZKa/XMrSbdmg3r2Xnaidz9Qumd0VPaMrZlF9V9sA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.4.0\"\n      }\n    },\n    \"node_modules/promise-inflight\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/promise-inflight/-/promise-inflight-1.0.1.tgz\",\n      \"integrity\": \"sha512-6zWPyEOFaQBJYcGMHBKTKJ3u6TBsnMFOIZSa6ce1e/ZrrsOlnHRHbabMjLiBYKp+n44X9eUI6VUPaukCXHuG4g==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/promise-retry\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/promise-retry/-/promise-retry-2.0.1.tgz\",\n      \"integrity\": \"sha512-y+WKFlBR8BGXnsNlIHFGPZmyDf3DFMoLhaflAnyZgV6rG6xu+JwesTo2Q9R6XwYmtmwAFCkAk3e35jEdoeh/3g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"err-code\": \"^2.0.2\",\n        \"retry\": \"^0.12.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/pump\": {\n      \"version\": \"3.0.4\",\n      \"resolved\": \"https://registry.npmjs.org/pump/-/pump-3.0.4.tgz\",\n      \"integrity\": \"sha512-VS7sjc6KR7e1ukRFhQSY5LM2uBWAUPiOPa/A3mkKmiMwSmRFUITt0xuj+/lesgnCv+dPIEYlkzrcyXgquIHMcA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"end-of-stream\": \"^1.1.0\",\n        \"once\": \"^1.3.1\"\n      }\n    },\n    \"node_modules/punycode\": {\n      \"version\": \"2.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/punycode/-/punycode-2.3.1.tgz\",\n      \"integrity\": \"sha512-vYt7UD1U9Wg6138shLtLOvdAu+8DsC/ilFtEVHcH+wydcSpNE20AfSOduf6MkRFahL5FY7X1oU7nKVZFtfq8Fg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=6\"\n      }\n    },\n    \"node_modules/quick-lru\": {\n      \"version\": \"5.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/quick-lru/-/quick-lru-5.1.1.tgz\",\n      \"integrity\": \"sha512-WuyALRjWPDGtt/wzJiadO5AXY+8hZ80hVpe6MyivgraREW751X3SbhRvG3eLKOYN+8VEvqLcf3wdnt44Z4S4SA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/read-binary-file-arch\": {\n      \"version\": \"1.0.6\",\n      \"resolved\": \"https://registry.npmjs.org/read-binary-file-arch/-/read-binary-file-arch-1.0.6.tgz\",\n      \"integrity\": \"sha512-BNg9EN3DD3GsDXX7Aa8O4p92sryjkmzYYgmgTAc6CA4uGLEDzFfxOxugu21akOxpcXHiEgsYkC6nPsQvLLLmEg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"debug\": \"^4.3.4\"\n      },\n      \"bin\": {\n        \"read-binary-file-arch\": \"cli.js\"\n      }\n    },\n    \"node_modules/readable-stream\": {\n      \"version\": \"3.6.2\",\n      \"resolved\": \"https://registry.npmjs.org/readable-stream/-/readable-stream-3.6.2.tgz\",\n      \"integrity\": \"sha512-9u/sniCrY3D5WdsERHzHE4G2YCXqoG5FTHUiCC4SIbr6XcLZBY05ya9EKjYek9O5xOAwjGq+1JdGBAS7Q9ScoA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"inherits\": \"^2.0.3\",\n        \"string_decoder\": \"^1.1.1\",\n        \"util-deprecate\": \"^1.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">= 6\"\n      }\n    },\n    \"node_modules/require-directory\": {\n      \"version\": \"2.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/require-directory/-/require-directory-2.1.1.tgz\",\n      \"integrity\": \"sha512-fGxEI7+wsG9xrvdjsrlmL22OMTTiHRwAMroiEeMgq8gzoLC/PQr7RsRDSTLUg/bZAZtF+TVIkHc6/4RIKrui+Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=0.10.0\"\n      }\n    },\n    \"node_modules/resedit\": {\n      \"version\": \"1.7.2\",\n      \"resolved\": \"https://registry.npmjs.org/resedit/-/resedit-1.7.2.tgz\",\n      \"integrity\": \"sha512-vHjcY2MlAITJhC0eRD/Vv8Vlgmu9Sd3LX9zZvtGzU5ZImdTN3+d6e/4mnTyV8vEbyf1sgNIrWxhWlrys52OkEA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"pe-library\": \"^0.4.1\"\n      },\n      \"engines\": {\n        \"node\": \">=12\",\n        \"npm\": \">=6\"\n      },\n      \"funding\": {\n        \"type\": \"github\",\n        \"url\": \"https://github.com/sponsors/jet2jet\"\n      }\n    },\n    \"node_modules/resolve-alpn\": {\n      \"version\": \"1.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/resolve-alpn/-/resolve-alpn-1.2.1.tgz\",\n      \"integrity\": \"sha512-0a1F4l73/ZFZOakJnQ3FvkJ2+gSTQWz/r2KE5OdDY0TxPm5h4GkqkWWfM47T7HsbnOtcJVEF4epCVy6u7Q3K+g==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/responselike\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/responselike/-/responselike-2.0.1.tgz\",\n      \"integrity\": \"sha512-4gl03wn3hj1HP3yzgdI7d3lCkF95F21Pz4BPGvKHinyQzALR5CapwC8yIi0Rh58DEMQ/SguC03wFj2k0M/mHhw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"lowercase-keys\": \"^2.0.0\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/restore-cursor\": {\n      \"version\": \"3.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/restore-cursor/-/restore-cursor-3.1.0.tgz\",\n      \"integrity\": \"sha512-l+sSefzHpj5qimhFSE5a8nufZYAM3sBSVMAPtYkmC+4EH2anSGaEMXSD0izRQbu9nfyQ9y5JrVmp7E8oZrUjvA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"onetime\": \"^5.1.0\",\n        \"signal-exit\": \"^3.0.2\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/retry\": {\n      \"version\": \"0.12.0\",\n      \"resolved\": \"https://registry.npmjs.org/retry/-/retry-0.12.0.tgz\",\n      \"integrity\": \"sha512-9LkiTwjUh6rT555DtE9rTX+BKByPfrMzEAtnlEtdEwr3Nkffwiihqe2bWADg+OQRjt9gl6ICdmB/ZFDCGAtSow==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 4\"\n      }\n    },\n    \"node_modules/rimraf\": {\n      \"version\": \"3.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/rimraf/-/rimraf-3.0.2.tgz\",\n      \"integrity\": \"sha512-JZkJMZkAGFFPP2YqXZXPbMlMBgsxzE8ILs4lMIX/2o0L9UBw9O/Y3o6wFw/i9YLapcUJWwqbi3kdxIPdC62TIA==\",\n      \"deprecated\": \"Rimraf versions prior to v4 are no longer supported\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"glob\": \"^7.1.3\"\n      },\n      \"bin\": {\n        \"rimraf\": \"bin.js\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/isaacs\"\n      }\n    },\n    \"node_modules/roarr\": {\n      \"version\": \"2.15.4\",\n      \"resolved\": \"https://registry.npmjs.org/roarr/-/roarr-2.15.4.tgz\",\n      \"integrity\": \"sha512-CHhPh+UNHD2GTXNYhPWLnU8ONHdI+5DI+4EYIAOaiD63rHeYlZvyh8P+in5999TTSFgUYuKUAjzRI4mdh/p+2A==\",\n      \"dev\": true,\n      \"license\": \"BSD-3-Clause\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"boolean\": \"^3.0.1\",\n        \"detect-node\": \"^2.0.4\",\n        \"globalthis\": \"^1.0.1\",\n        \"json-stringify-safe\": \"^5.0.1\",\n        \"semver-compare\": \"^1.0.0\",\n        \"sprintf-js\": \"^1.1.2\"\n      },\n      \"engines\": {\n        \"node\": \">=8.0\"\n      }\n    },\n    \"node_modules/safe-buffer\": {\n      \"version\": \"5.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/safe-buffer/-/safe-buffer-5.2.1.tgz\",\n      \"integrity\": \"sha512-rp3So07KcdmmKbGvgaNxQSJr7bGVSVk5S9Eq1F+ppbRo70+YeaDxkw5Dd8NPN+GD6bjnYm2VuPuCXmpuYvmCXQ==\",\n      \"dev\": true,\n      \"funding\": [\n        {\n          \"type\": \"github\",\n          \"url\": \"https://github.com/sponsors/feross\"\n        },\n        {\n          \"type\": \"patreon\",\n          \"url\": \"https://www.patreon.com/feross\"\n        },\n        {\n          \"type\": \"consulting\",\n          \"url\": \"https://feross.org/support\"\n        }\n      ],\n      \"license\": \"MIT\"\n    },\n    \"node_modules/safer-buffer\": {\n      \"version\": \"2.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/safer-buffer/-/safer-buffer-2.1.2.tgz\",\n      \"integrity\": \"sha512-YZo3K82SD7Riyi0E1EQPojLz7kpepnSQI9IyPbHHg1XXXevb5dJI7tpyN2ADxGcQbHG7vcyRHk0cbwqcQriUtg==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/sanitize-filename\": {\n      \"version\": \"1.6.4\",\n      \"resolved\": \"https://registry.npmjs.org/sanitize-filename/-/sanitize-filename-1.6.4.tgz\",\n      \"integrity\": \"sha512-9ZyI08PsvdQl2r/bBIGubpVdR3RR9sY6RDiWFPreA21C/EFlQhmgo20UZlNjZMMZNubusLhAQozkA0Od5J21Eg==\",\n      \"dev\": true,\n      \"license\": \"WTFPL OR ISC\",\n      \"dependencies\": {\n        \"truncate-utf8-bytes\": \"^1.0.0\"\n      }\n    },\n    \"node_modules/sax\": {\n      \"version\": \"1.6.0\",\n      \"resolved\": \"https://registry.npmjs.org/sax/-/sax-1.6.0.tgz\",\n      \"integrity\": \"sha512-6R3J5M4AcbtLUdZmRv2SygeVaM7IhrLXu9BmnOGmmACak8fiUtOsYNWUS4uK7upbmHIBbLBeFeI//477BKLBzA==\",\n      \"dev\": true,\n      \"license\": \"BlueOak-1.0.0\",\n      \"engines\": {\n        \"node\": \">=11.0.0\"\n      }\n    },\n    \"node_modules/semver\": {\n      \"version\": \"6.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-6.3.1.tgz\",\n      \"integrity\": \"sha512-BR7VvDCVHO+q2xBEWskxS6DJE1qRnb7DxzUrogb71CWoSficBxYsiAGd+Kl0mmq/MprG9yArRkyrQxTO6XjMzA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      }\n    },\n    \"node_modules/semver-compare\": {\n      \"version\": \"1.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/semver-compare/-/semver-compare-1.0.0.tgz\",\n      \"integrity\": \"sha512-YM3/ITh2MJ5MtzaM429anh+x2jiLVjqILF4m4oyQB18W7Ggea7BfqdH/wGMK7dDiMghv/6WG7znWMwUDzJiXow==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true\n    },\n    \"node_modules/serialize-error\": {\n      \"version\": \"7.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/serialize-error/-/serialize-error-7.0.1.tgz\",\n      \"integrity\": \"sha512-8I8TjW5KMOKsZQTvoxjuSIa7foAwPWGOts+6o7sgjz41/qMD9VQHEDxi6PBvK2l0MXUmqZyNpUK+T2tQaaElvw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"type-fest\": \"^0.13.1\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/shebang-command\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/shebang-command/-/shebang-command-2.0.0.tgz\",\n      \"integrity\": \"sha512-kHxr2zZpYtdmrN1qDjrrX/Z1rR1kG8Dx+gkpK1G4eXmvXswmcE1hTWBWYUzlraYw1/yZp6YuDY77YtvbN0dmDA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"shebang-regex\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/shebang-regex\": {\n      \"version\": \"3.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/shebang-regex/-/shebang-regex-3.0.0.tgz\",\n      \"integrity\": \"sha512-7++dFhtcx3353uBaq8DDR4NuxBetBzC7ZQOhmTQInHEd6bSrXdiEyzCvG07Z44UYdLShWUyXt5M/yhz8ekcb1A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/signal-exit\": {\n      \"version\": \"3.0.7\",\n      \"resolved\": \"https://registry.npmjs.org/signal-exit/-/signal-exit-3.0.7.tgz\",\n      \"integrity\": \"sha512-wnD2ZE+l+SPC/uoS0vXeE9L1+0wuaMqKlfz9AMUo38JsyLSBWSFcHR1Rri62LZc12vLr1gb3jl7iwQhgwpAbGQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/simple-update-notifier\": {\n      \"version\": \"2.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/simple-update-notifier/-/simple-update-notifier-2.0.0.tgz\",\n      \"integrity\": \"sha512-a2B9Y0KlNXl9u/vsW6sTIu9vGEpfKu2wRV6l1H3XEas/0gUIzGzBoP/IouTcUQbm9JWZLH3COxyn03TYlFax6w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"semver\": \"^7.5.3\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/simple-update-notifier/node_modules/semver\": {\n      \"version\": \"7.7.4\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-7.7.4.tgz\",\n      \"integrity\": \"sha512-vFKC2IEtQnVhpT78h1Yp8wzwrf8CM+MzKMHGJZfBtzhZNycRFnXsHk6E5TxIkkMsgNS7mdX3AGB7x2QM2di4lA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver.js\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/slice-ansi\": {\n      \"version\": \"3.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/slice-ansi/-/slice-ansi-3.0.0.tgz\",\n      \"integrity\": \"sha512-pSyv7bSTC7ig9Dcgbw9AuRNUb5k5V6oDudjZoMBSr13qpLBG7tB+zgCkARjq7xIUgdz5P1Qe8u+rSGdouOOIyQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"ansi-styles\": \"^4.0.0\",\n        \"astral-regex\": \"^2.0.0\",\n        \"is-fullwidth-code-point\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/smart-buffer\": {\n      \"version\": \"4.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/smart-buffer/-/smart-buffer-4.2.0.tgz\",\n      \"integrity\": \"sha512-94hK0Hh8rPqQl2xXc3HsaBoOXKV20MToPkcXvwbISWLEs+64sBq5kFgn2kJDHb1Pry9yrP0dxrCI9RRci7RXKg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 6.0.0\",\n        \"npm\": \">= 3.0.0\"\n      }\n    },\n    \"node_modules/socks\": {\n      \"version\": \"2.8.7\",\n      \"resolved\": \"https://registry.npmjs.org/socks/-/socks-2.8.7.tgz\",\n      \"integrity\": \"sha512-HLpt+uLy/pxB+bum/9DzAgiKS8CX1EvbWxI4zlmgGCExImLdiad2iCwXT5Z4c9c3Eq8rP2318mPW2c+QbtjK8A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ip-address\": \"^10.0.1\",\n        \"smart-buffer\": \"^4.2.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 10.0.0\",\n        \"npm\": \">= 3.0.0\"\n      }\n    },\n    \"node_modules/socks-proxy-agent\": {\n      \"version\": \"7.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/socks-proxy-agent/-/socks-proxy-agent-7.0.0.tgz\",\n      \"integrity\": \"sha512-Fgl0YPZ902wEsAyiQ+idGd1A7rSFx/ayC1CQVMw5P+EQx2V0SgpGtf6OKFhVjPflPUl9YMmEOnmfjCdMUsygww==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"agent-base\": \"^6.0.2\",\n        \"debug\": \"^4.3.3\",\n        \"socks\": \"^2.6.2\"\n      },\n      \"engines\": {\n        \"node\": \">= 10\"\n      }\n    },\n    \"node_modules/socks-proxy-agent/node_modules/agent-base\": {\n      \"version\": \"6.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/agent-base/-/agent-base-6.0.2.tgz\",\n      \"integrity\": \"sha512-RZNwNclF7+MS/8bDg70amg32dyeZGZxiDuQmZxKLAlQjr3jGyLx+4Kkk58UO7D2QdgFIQCovuSuZESne6RG6XQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"debug\": \"4\"\n      },\n      \"engines\": {\n        \"node\": \">= 6.0.0\"\n      }\n    },\n    \"node_modules/source-map\": {\n      \"version\": \"0.6.1\",\n      \"resolved\": \"https://registry.npmjs.org/source-map/-/source-map-0.6.1.tgz\",\n      \"integrity\": \"sha512-UjgapumWlbMhkBgzT7Ykc5YXUT46F0iKu8SGXq0bcwP5dz/h0Plj6enJqjz1Zbq2l5WaqYnrVbwWOWMyF3F47g==\",\n      \"dev\": true,\n      \"license\": \"BSD-3-Clause\",\n      \"engines\": {\n        \"node\": \">=0.10.0\"\n      }\n    },\n    \"node_modules/source-map-support\": {\n      \"version\": \"0.5.21\",\n      \"resolved\": \"https://registry.npmjs.org/source-map-support/-/source-map-support-0.5.21.tgz\",\n      \"integrity\": \"sha512-uBHU3L3czsIyYXKX88fdrGovxdSCoTGDRZ6SYXtSRxLZUzHg5P/66Ht6uoUlHu9EZod+inXhKo3qQgwXUT/y1w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"buffer-from\": \"^1.0.0\",\n        \"source-map\": \"^0.6.0\"\n      }\n    },\n    \"node_modules/sprintf-js\": {\n      \"version\": \"1.1.3\",\n      \"resolved\": \"https://registry.npmjs.org/sprintf-js/-/sprintf-js-1.1.3.tgz\",\n      \"integrity\": \"sha512-Oo+0REFV59/rz3gfJNKQiBlwfHaSESl1pcGyABQsnnIfWOFt6JNj5gCog2U6MLZ//IGYD+nA8nI+mTShREReaA==\",\n      \"dev\": true,\n      \"license\": \"BSD-3-Clause\",\n      \"optional\": true\n    },\n    \"node_modules/ssri\": {\n      \"version\": \"9.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/ssri/-/ssri-9.0.1.tgz\",\n      \"integrity\": \"sha512-o57Wcn66jMQvfHG1FlYbWeZWW/dHZhJXjpIcTfXldXEk5nz5lStPo3mK0OJQfGR3RbZUlbISexbljkJzuEj/8Q==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"minipass\": \"^3.1.1\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/stat-mode\": {\n      \"version\": \"1.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/stat-mode/-/stat-mode-1.0.0.tgz\",\n      \"integrity\": \"sha512-jH9EhtKIjuXZ2cWxmXS8ZP80XyC3iasQxMDV8jzhNJpfDb7VbQLVW4Wvsxz9QZvzV+G4YoSfBUVKDOyxLzi/sg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 6\"\n      }\n    },\n    \"node_modules/string_decoder\": {\n      \"version\": \"1.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/string_decoder/-/string_decoder-1.3.0.tgz\",\n      \"integrity\": \"sha512-hkRX8U1WjJFd8LsDJ2yQ/wWWxaopEsABU1XfkM8A+j0+85JAGppt16cr1Whg6KIbb4okU6Mql6BOj+uup/wKeA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"safe-buffer\": \"~5.2.0\"\n      }\n    },\n    \"node_modules/string-width\": {\n      \"version\": \"4.2.3\",\n      \"resolved\": \"https://registry.npmjs.org/string-width/-/string-width-4.2.3.tgz\",\n      \"integrity\": \"sha512-wKyQRQpjJ0sIp62ErSZdGsjMJWsap5oRNihHhu6G7JVO/9jIB6UyevL+tXuOqrng8j/cxKTWyWUwvSTriiZz/g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"emoji-regex\": \"^8.0.0\",\n        \"is-fullwidth-code-point\": \"^3.0.0\",\n        \"strip-ansi\": \"^6.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/string-width-cjs\": {\n      \"name\": \"string-width\",\n      \"version\": \"4.2.3\",\n      \"resolved\": \"https://registry.npmjs.org/string-width/-/string-width-4.2.3.tgz\",\n      \"integrity\": \"sha512-wKyQRQpjJ0sIp62ErSZdGsjMJWsap5oRNihHhu6G7JVO/9jIB6UyevL+tXuOqrng8j/cxKTWyWUwvSTriiZz/g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"emoji-regex\": \"^8.0.0\",\n        \"is-fullwidth-code-point\": \"^3.0.0\",\n        \"strip-ansi\": \"^6.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/strip-ansi\": {\n      \"version\": \"6.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/strip-ansi/-/strip-ansi-6.0.1.tgz\",\n      \"integrity\": \"sha512-Y38VPSHcqkFrCpFnQ9vuSXmquuv5oXOKpGeT6aGrr3o3Gc9AlVa6JBfUSOCnbxGGZF+/0ooI7KrPuUSztUdU5A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ansi-regex\": \"^5.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/strip-ansi-cjs\": {\n      \"name\": \"strip-ansi\",\n      \"version\": \"6.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/strip-ansi/-/strip-ansi-6.0.1.tgz\",\n      \"integrity\": \"sha512-Y38VPSHcqkFrCpFnQ9vuSXmquuv5oXOKpGeT6aGrr3o3Gc9AlVa6JBfUSOCnbxGGZF+/0ooI7KrPuUSztUdU5A==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ansi-regex\": \"^5.0.1\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/sumchecker\": {\n      \"version\": \"3.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/sumchecker/-/sumchecker-3.0.1.tgz\",\n      \"integrity\": \"sha512-MvjXzkz/BOfyVDkG0oFOtBxHX2u3gKbMHIF/dXblZsgD3BWOFLmHovIpZY7BykJdAjcqRCBi1WYBNdEC9yI7vg==\",\n      \"dev\": true,\n      \"license\": \"Apache-2.0\",\n      \"dependencies\": {\n        \"debug\": \"^4.1.0\"\n      },\n      \"engines\": {\n        \"node\": \">= 8.0\"\n      }\n    },\n    \"node_modules/supports-color\": {\n      \"version\": \"7.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/supports-color/-/supports-color-7.2.0.tgz\",\n      \"integrity\": \"sha512-qpCAvRl9stuOHveKsn7HncJRvv501qIacKzQlO/+Lwxc9+0q2wLyv4Dfvt80/DPn2pqOBsJdDiogXGR9+OvwRw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"has-flag\": \"^4.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/tar\": {\n      \"version\": \"6.2.1\",\n      \"resolved\": \"https://registry.npmjs.org/tar/-/tar-6.2.1.tgz\",\n      \"integrity\": \"sha512-DZ4yORTwrbTj/7MZYq2w+/ZFdI6OZ/f9SFHR+71gIVUZhOQPHzVCLpvRnPgyaMpfWxxk/4ONva3GQSyNIKRv6A==\",\n      \"deprecated\": \"Old versions of tar are not supported, and contain widely publicized security vulnerabilities, which have been fixed in the current version. Please update. Support for old versions may be purchased (at exorbitant rates) by contacting i@izs.me\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"chownr\": \"^2.0.0\",\n        \"fs-minipass\": \"^2.0.0\",\n        \"minipass\": \"^5.0.0\",\n        \"minizlib\": \"^2.1.1\",\n        \"mkdirp\": \"^1.0.3\",\n        \"yallist\": \"^4.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/tar/node_modules/minipass\": {\n      \"version\": \"5.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/minipass/-/minipass-5.0.0.tgz\",\n      \"integrity\": \"sha512-3FnjYuehv9k6ovOEbyOswadCDPX1piCfhV8ncmYtHOjuPwylVWsghTLo7rabjC3Rx5xD4HDx8Wm1xnMF7S5qFQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">=8\"\n      }\n    },\n    \"node_modules/temp\": {\n      \"version\": \"0.9.4\",\n      \"resolved\": \"https://registry.npmjs.org/temp/-/temp-0.9.4.tgz\",\n      \"integrity\": \"sha512-yYrrsWnrXMcdsnu/7YMYAofM1ktpL5By7vZhf15CrXijWWrEYZks5AXBudalfSWJLlnen/QUJUB5aoB0kqZUGA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"peer\": true,\n      \"dependencies\": {\n        \"mkdirp\": \"^0.5.1\",\n        \"rimraf\": \"~2.6.2\"\n      },\n      \"engines\": {\n        \"node\": \">=6.0.0\"\n      }\n    },\n    \"node_modules/temp-file\": {\n      \"version\": \"3.4.0\",\n      \"resolved\": \"https://registry.npmjs.org/temp-file/-/temp-file-3.4.0.tgz\",\n      \"integrity\": \"sha512-C5tjlC/HCtVUOi3KWVokd4vHVViOmGjtLwIh4MuzPo/nMYTV/p1urt3RnMz2IWXDdKEGJH3k5+KPxtqRsUYGtg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"async-exit-hook\": \"^2.0.1\",\n        \"fs-extra\": \"^10.0.0\"\n      }\n    },\n    \"node_modules/temp-file/node_modules/fs-extra\": {\n      \"version\": \"10.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/fs-extra/-/fs-extra-10.1.0.tgz\",\n      \"integrity\": \"sha512-oRXApq54ETRj4eMiFzGnHWGy+zo5raudjuxN0b8H7s/RU2oW0Wvsx9O0ACRN/kRq9E8Vu/ReskGB5o3ji+FzHQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"graceful-fs\": \"^4.2.0\",\n        \"jsonfile\": \"^6.0.1\",\n        \"universalify\": \"^2.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/temp-file/node_modules/jsonfile\": {\n      \"version\": \"6.2.0\",\n      \"resolved\": \"https://registry.npmjs.org/jsonfile/-/jsonfile-6.2.0.tgz\",\n      \"integrity\": \"sha512-FGuPw30AdOIUTRMC2OMRtQV+jkVj2cfPqSeWXv1NEAJ1qZ5zb1X6z1mFhbfOB/iy3ssJCD+3KuZ8r8C3uVFlAg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"universalify\": \"^2.0.0\"\n      },\n      \"optionalDependencies\": {\n        \"graceful-fs\": \"^4.1.6\"\n      }\n    },\n    \"node_modules/temp-file/node_modules/universalify\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-2.0.1.tgz\",\n      \"integrity\": \"sha512-gptHNQghINnc/vTGIk0SOFGFNXw7JVrlRUtConJRlvaw6DuX0wO5Jeko9sWrMBhh+PsYAZ7oXAiOnf/UKogyiw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 10.0.0\"\n      }\n    },\n    \"node_modules/temp/node_modules/mkdirp\": {\n      \"version\": \"0.5.6\",\n      \"resolved\": \"https://registry.npmjs.org/mkdirp/-/mkdirp-0.5.6.tgz\",\n      \"integrity\": \"sha512-FP+p8RB8OWpF3YZBCrP5gtADmtXApB5AMLn+vdyA+PyxCjrCs00mjyUozssO33cwDeT3wNGdLxJ5M//YqtHAJw==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"peer\": true,\n      \"dependencies\": {\n        \"minimist\": \"^1.2.6\"\n      },\n      \"bin\": {\n        \"mkdirp\": \"bin/cmd.js\"\n      }\n    },\n    \"node_modules/temp/node_modules/rimraf\": {\n      \"version\": \"2.6.3\",\n      \"resolved\": \"https://registry.npmjs.org/rimraf/-/rimraf-2.6.3.tgz\",\n      \"integrity\": \"sha512-mwqeW5XsA2qAejG46gYdENaxXjx9onRNCfn7L0duuP4hCuTIi/QO7PDK07KJfp1d+izWPrzEJDcSqBa0OZQriA==\",\n      \"deprecated\": \"Rimraf versions prior to v4 are no longer supported\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"peer\": true,\n      \"dependencies\": {\n        \"glob\": \"^7.1.3\"\n      },\n      \"bin\": {\n        \"rimraf\": \"bin.js\"\n      }\n    },\n    \"node_modules/tiny-async-pool\": {\n      \"version\": \"1.3.1\",\n      \"resolved\": \"https://registry.npmjs.org/tiny-async-pool/-/tiny-async-pool-1.3.0.tgz\",\n      \"integrity\": \"sha512-01EAw5EDrcVrdgyCLgoSPvqznC0sVxDSVeiOz09FUpjh71G79VCqneOr+xvt7T1r76CF6ZZfPjHorN2+d+3mqA==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"semver\": \"^5.5.0\"\n      }\n    },\n    \"node_modules/tiny-async-pool/node_modules/semver\": {\n      \"version\": \"5.7.2\",\n      \"resolved\": \"https://registry.npmjs.org/semver/-/semver-5.7.2.tgz\",\n      \"integrity\": \"sha512-cBznnQ9KjJqU67B52RMC65CMarK2600WFnbkcaiwWq3xy/5haFJlshgnpjovMVJ+Hff49d8GEn0b87C5pDQ10g==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"bin\": {\n        \"semver\": \"bin/semver\"\n      }\n    },\n    \"node_modules/tmp\": {\n      \"version\": \"0.2.5\",\n      \"resolved\": \"https://registry.npmjs.org/tmp/-/tmp-0.2.5.tgz\",\n      \"integrity\": \"sha512-voyz6MApa1rQGUxT3E+BK7/ROe8itEx7vD8/HEvt4xwXucvQ5G5oeEiHkmHZJuBO21RpOf+YYm9MOivj709jow==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=14.14\"\n      }\n    },\n    \"node_modules/tmp-promise\": {\n      \"version\": \"3.0.3\",\n      \"resolved\": \"https://registry.npmjs.org/tmp-promise/-/tmp-promise-3.0.3.tgz\",\n      \"integrity\": \"sha512-RwM7MoPojPxsOBYnyd2hy0bxtIlVrihNs9pj5SUvY8Zz1sQcQG2tG1hSr8PDxfgEB8RNKDhqbIlroIarSNDNsQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"tmp\": \"^0.2.0\"\n      }\n    },\n    \"node_modules/truncate-utf8-bytes\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/truncate-utf8-bytes/-/truncate-utf8-bytes-1.0.2.tgz\",\n      \"integrity\": \"sha512-95Pu1QXQvruGEhv62XCMO3Mm90GscOCClvrIUwCM0PYOXK3kaF3l3sIHxx71ThJfcbM2O5Au6SO3AWCSEfW4mQ==\",\n      \"dev\": true,\n      \"license\": \"WTFPL\",\n      \"dependencies\": {\n        \"utf8-byte-length\": \"^1.0.1\"\n      }\n    },\n    \"node_modules/type-fest\": {\n      \"version\": \"0.13.1\",\n      \"resolved\": \"https://registry.npmjs.org/type-fest/-/type-fest-0.13.1.tgz\",\n      \"integrity\": \"sha512-34R7HTnG0XIJcBSn5XhDd7nNFPRcXYRZrBB2O2jdKqYODldSzBAqzsWoZYYvduky73toYS/ESqxPvkDf/F0XMg==\",\n      \"dev\": true,\n      \"license\": \"(MIT OR CC0-1.0)\",\n      \"optional\": true,\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    },\n    \"node_modules/typescript\": {\n      \"version\": \"5.9.3\",\n      \"resolved\": \"https://registry.npmjs.org/typescript/-/typescript-5.9.3.tgz\",\n      \"integrity\": \"sha512-jl1vZzPDinLr9eUt3J/t7V6FgNEw9QjvBPdysz9KfQDD41fQrC2Y4vKQdiaUpFT4bXlb1RHhLpp8wtm6M5TgSw==\",\n      \"dev\": true,\n      \"license\": \"Apache-2.0\",\n      \"bin\": {\n        \"tsc\": \"bin/tsc\",\n        \"tsserver\": \"bin/tsserver\"\n      },\n      \"engines\": {\n        \"node\": \">=14.17\"\n      }\n    },\n    \"node_modules/undici-types\": {\n      \"version\": \"6.21.0\",\n      \"resolved\": \"https://registry.npmjs.org/undici-types/-/undici-types-6.21.0.tgz\",\n      \"integrity\": \"sha512-iwDZqg0QAGrg9Rav5H4n0M64c3mkR59cJ6wQp+7C4nI0gsmExaedaYLNO44eT4AtBBwjbTiGPMlt2Md0T9H9JQ==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/unique-filename\": {\n      \"version\": \"2.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/unique-filename/-/unique-filename-2.0.1.tgz\",\n      \"integrity\": \"sha512-ODWHtkkdx3IAR+veKxFV+VBkUMcN+FaqzUUd7IZzt+0zhDZFPFxhlqwPF3YQvMHx1TD0tdgYl+kuPnJ8E6ql7A==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"unique-slug\": \"^3.0.0\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/unique-slug\": {\n      \"version\": \"3.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/unique-slug/-/unique-slug-3.0.0.tgz\",\n      \"integrity\": \"sha512-8EyMynh679x/0gqE9fT9oilG+qEt+ibFyqjuVTsZn1+CMxH+XLlpvr2UZx4nVcCwTpx81nICr2JQFkM+HPLq4w==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"imurmurhash\": \"^0.1.4\"\n      },\n      \"engines\": {\n        \"node\": \"^12.13.0 || ^14.15.0 || >=16.0.0\"\n      }\n    },\n    \"node_modules/universalify\": {\n      \"version\": \"0.1.2\",\n      \"resolved\": \"https://registry.npmjs.org/universalify/-/universalify-0.1.2.tgz\",\n      \"integrity\": \"sha512-rBJeI5CXAlmy1pV+617WB9J63U6XcazHHF2f2dbJix4XzpUF0RS3Zbj0FGIOCAva5P/d/GBOYaACQ1w+0azUkg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">= 4.0.0\"\n      }\n    },\n    \"node_modules/uri-js\": {\n      \"version\": \"4.4.1\",\n      \"resolved\": \"https://registry.npmjs.org/uri-js/-/uri-js-4.4.1.tgz\",\n      \"integrity\": \"sha512-7rKUyy33Q1yc98pQ1DAmLtwX109F7TIfWlW1Ydo8Wl1ii1SeHieeh0HHfPeL2fMXK6z0s8ecKs9frCuLJvndBg==\",\n      \"dev\": true,\n      \"license\": \"BSD-2-Clause\",\n      \"dependencies\": {\n        \"punycode\": \"^2.1.0\"\n      }\n    },\n    \"node_modules/utf8-byte-length\": {\n      \"version\": \"1.0.5\",\n      \"resolved\": \"https://registry.npmjs.org/utf8-byte-length/-/utf8-byte-length-1.0.5.tgz\",\n      \"integrity\": \"sha512-Xn0w3MtiQ6zoz2vFyUVruaCL53O/DwUvkEeOvj+uulMm0BkUGYWmBYVyElqZaSLhY6ZD0ulfU3aBra2aVT4xfA==\",\n      \"dev\": true,\n      \"license\": \"(WTFPL OR MIT)\"\n    },\n    \"node_modules/util-deprecate\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/util-deprecate/-/util-deprecate-1.0.2.tgz\",\n      \"integrity\": \"sha512-EPD5q1uXyFxJpCrLnCc1nHnq3gOa6DZBocAIiI2TaSCA7VCJ1UJDMagCzIkXNsUYfD1daK//LTEQ8xiIbrHtcw==\",\n      \"dev\": true,\n      \"license\": \"MIT\"\n    },\n    \"node_modules/verror\": {\n      \"version\": \"1.10.1\",\n      \"resolved\": \"https://registry.npmjs.org/verror/-/verror-1.10.1.tgz\",\n      \"integrity\": \"sha512-veufcmxri4e3XSrT0xwfUR7kguIkaxBeosDg00yDWhk49wdwkSUrvvsm7nc75e1PUyvIeZj6nS8VQRYz2/S4Xg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"optional\": true,\n      \"dependencies\": {\n        \"assert-plus\": \"^1.0.0\",\n        \"core-util-is\": \"1.0.2\",\n        \"extsprintf\": \"^1.2.0\"\n      },\n      \"engines\": {\n        \"node\": \">=0.6.0\"\n      }\n    },\n    \"node_modules/wcwidth\": {\n      \"version\": \"1.0.1\",\n      \"resolved\": \"https://registry.npmjs.org/wcwidth/-/wcwidth-1.0.1.tgz\",\n      \"integrity\": \"sha512-XHPEwS0q6TaxcvG85+8EYkbiCux2XtWG2mkc47Ng2A77BQu9+DqIOJldST4HgPkuea7dvKSj5VgX3P1d4rW8Tg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"defaults\": \"^1.0.3\"\n      }\n    },\n    \"node_modules/which\": {\n      \"version\": \"2.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/which/-/which-2.0.2.tgz\",\n      \"integrity\": \"sha512-BLI3Tl1TW3Pvl70l3yq3Y64i+awpwXqsGBYWkkqMtnbXgrMD+yj7rhW0kuEDxzJaYXGjEW5ogapKNMEKNMjibA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"dependencies\": {\n        \"isexe\": \"^2.0.0\"\n      },\n      \"bin\": {\n        \"node-which\": \"bin/node-which\"\n      },\n      \"engines\": {\n        \"node\": \">= 8\"\n      }\n    },\n    \"node_modules/wrap-ansi\": {\n      \"version\": \"7.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/wrap-ansi/-/wrap-ansi-7.0.0.tgz\",\n      \"integrity\": \"sha512-YVGIj2kamLSTxw6NsZjoBxfSwsn0ycdesmc4p+Q21c5zPuZ1pl+NfxVdxPtdHvmNVOQ6XSYG4AUtyt/Fi7D16Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ansi-styles\": \"^4.0.0\",\n        \"string-width\": \"^4.1.0\",\n        \"strip-ansi\": \"^6.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/wrap-ansi?sponsor=1\"\n      }\n    },\n    \"node_modules/wrap-ansi-cjs\": {\n      \"name\": \"wrap-ansi\",\n      \"version\": \"7.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/wrap-ansi/-/wrap-ansi-7.0.0.tgz\",\n      \"integrity\": \"sha512-YVGIj2kamLSTxw6NsZjoBxfSwsn0ycdesmc4p+Q21c5zPuZ1pl+NfxVdxPtdHvmNVOQ6XSYG4AUtyt/Fi7D16Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"ansi-styles\": \"^4.0.0\",\n        \"string-width\": \"^4.1.0\",\n        \"strip-ansi\": \"^6.0.0\"\n      },\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/chalk/wrap-ansi?sponsor=1\"\n      }\n    },\n    \"node_modules/wrappy\": {\n      \"version\": \"1.0.2\",\n      \"resolved\": \"https://registry.npmjs.org/wrappy/-/wrappy-1.0.2.tgz\",\n      \"integrity\": \"sha512-l4Sp/DRseor9wL6EvV2+TuQn63dMkPjZ/sp9XkghTEbV9KlPS1xUsZ3u7/IQO4wxtcFB4bgpQPRcR3QCvezPcQ==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/xmlbuilder\": {\n      \"version\": \"15.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/xmlbuilder/-/xmlbuilder-15.1.1.tgz\",\n      \"integrity\": \"sha512-yMqGBqtXyeN1e3TGYvgNgDVZ3j84W4cwkOXQswghol6APgZWaff9lnbvN7MHYJOiXsvGPXtjTYJEiC9J2wv9Eg==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=8.0\"\n      }\n    },\n    \"node_modules/y18n\": {\n      \"version\": \"5.0.8\",\n      \"resolved\": \"https://registry.npmjs.org/y18n/-/y18n-5.0.8.tgz\",\n      \"integrity\": \"sha512-0pfFzegeDWJHJIAmTLRP2DwHjdF5s7jo9tuztdQxAhINCdvS+3nGINqPd00AphqJR/0LhANUS6/+7SCb98YOfA==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">=10\"\n      }\n    },\n    \"node_modules/yallist\": {\n      \"version\": \"4.0.0\",\n      \"resolved\": \"https://registry.npmjs.org/yallist/-/yallist-4.0.0.tgz\",\n      \"integrity\": \"sha512-3wdGidZyq5PB084XLES5TpOSRA3wjXAlIWMhum2kRcv/41Sn2emQ0dycQW4uZXLejwKvg6EsvbdlVL+FYEct7A==\",\n      \"dev\": true,\n      \"license\": \"ISC\"\n    },\n    \"node_modules/yargs\": {\n      \"version\": \"17.7.2\",\n      \"resolved\": \"https://registry.npmjs.org/yargs/-/yargs-17.7.2.tgz\",\n      \"integrity\": \"sha512-7dSzzRQ++CKnNI/krKnYRV7JKKPUXMEh61soaHKg9mrWEhzFWhFnxPxGl+69cD1Ou63C13NUPCnmIcrvqCuM6w==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"cliui\": \"^8.0.1\",\n        \"escalade\": \"^3.1.1\",\n        \"get-caller-file\": \"^2.0.5\",\n        \"require-directory\": \"^2.1.1\",\n        \"string-width\": \"^4.2.3\",\n        \"y18n\": \"^5.0.5\",\n        \"yargs-parser\": \"^21.1.1\"\n      },\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/yargs-parser\": {\n      \"version\": \"21.1.1\",\n      \"resolved\": \"https://registry.npmjs.org/yargs-parser/-/yargs-parser-21.1.1.tgz\",\n      \"integrity\": \"sha512-tVpsJW7DdjecAiFpbIB1e3qxIQsE6NoPc5/eTdrbbIC4h0LVsWhnoa3g+m2HclBIujHzsxZ4VJVA+GUuc2/LBw==\",\n      \"dev\": true,\n      \"license\": \"ISC\",\n      \"engines\": {\n        \"node\": \">=12\"\n      }\n    },\n    \"node_modules/yauzl\": {\n      \"version\": \"2.10.0\",\n      \"resolved\": \"https://registry.npmjs.org/yauzl/-/yauzl-2.10.0.tgz\",\n      \"integrity\": \"sha512-p4a9I6X6nu6IhoGmBqAcbJy1mlC4j27vEPZX9F4L4/vZT3Lyq1VkFHw/V/PUcB9Buo+DG3iHkT0x3Qya58zc3g==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"dependencies\": {\n        \"buffer-crc32\": \"~0.2.3\",\n        \"fd-slicer\": \"~1.1.0\"\n      }\n    },\n    \"node_modules/yocto-queue\": {\n      \"version\": \"0.1.0\",\n      \"resolved\": \"https://registry.npmjs.org/yocto-queue/-/yocto-queue-0.1.0.tgz\",\n      \"integrity\": \"sha512-rVksvsnNCdJ/ohGc6xgPwyN8eheCxsiLM8mxuE/t/mOVqJewPuO1miLpTHQiRgTKCLexL4MeAFVagts7HmNZ2Q==\",\n      \"dev\": true,\n      \"license\": \"MIT\",\n      \"engines\": {\n        \"node\": \">=10\"\n      },\n      \"funding\": {\n        \"url\": \"https://github.com/sponsors/sindresorhus\"\n      }\n    }\n  }\n}\n",
    "backend/macports_provider.py": "\"\"\"MacPorts provider: exact archive preflight; no implicit cross-provider migration.\"\"\"\nfrom __future__ import annotations\nimport concurrent.futures\nimport csv\nimport hashlib\nimport importlib.util\nimport json\nimport os\nfrom pathlib import Path\nimport re\nimport shlex\nimport tempfile\nimport time\n\nPORT = '/opt/local/bin/port'\nTCL = '/opt/local/libexec/macports/bin/tclsh8.6'\nNAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]*\\Z')\nROOT = Path(__file__).resolve().parents[1]\n\ndef valid(name):\n    if not isinstance(name, str) or not NAME.fullmatch(name):\n        raise ValueError('Invalid MacPorts package identity')\n    return name\n\ndef checked(s, args, **kwargs):\n    result = s.run_cmd(args, **kwargs)\n    if result.code:\n        raise RuntimeError(result.stderr or result.stdout or f'Command failed: {args[0]}')\n    return result.stdout\n\ndef inventory(s):\n    if not Path(PORT).is_file():\n        raise RuntimeError('MacPorts is not installed at /opt/local/bin/port')\n    raw = checked(s, [PORT, '-q', 'installed', 'active'], timeout=90)\n    outdated = checked(s, [PORT, '-q', 'outdated'], timeout=90)\n    if raw.strip() and not re.search(r'@\\S+\\s+\\(active\\)',raw):\n        raise RuntimeError('Unrecognized MacPorts installed inventory')\n    if outdated.strip() and not re.search(r'\\s<\\s',outdated):\n        raise RuntimeError('Unrecognized MacPorts outdated inventory')\n    old = {m[0]: m[1] for m in re.findall(r'^\\s*(\\S+)\\s+\\S+\\s+<\\s+(\\S+)', outdated, re.M)}\n    packages = []\n    for name, version, variants in re.findall(r'^\\s*(\\S+)\\s+@([^\\s+]+)((?:\\+[^\\s]+)?)\\s+\\(active\\)', raw, re.M):\n        valid(name)\n        packages.append(dict(name=name, kind='port', provider='macports', provider_name=name,\n            installation_id=f'macports:{name}', display_name=name, description='MacPorts',\n            description_he='', installed_version=version, latest_version=old.get(name, version),\n            installed_variants=variants, outdated=name in old, excluded=False,\n            route_reason='installed_owner', homepage='', current_release_date=None,\n            latest_release_date=None, days_since_outdated=None))\n    return packages\n\ndef load_probe():\n    spec = importlib.util.spec_from_file_location('mac_archive_probe', ROOT / 'tools/check-macports-archives.py')\n    mod = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(mod)\n    return mod\n\ndef preflight(s, names, installed=None):\n    names = [valid(n) for n in names]\n    if not names:\n        raise ValueError('No verified MacPorts mapping')\n    if any(n == 'ngrok' for n in names):\n        raise ValueError('ngrok is excluded')\n    probe = load_probe()\n    with tempfile.TemporaryDirectory(prefix='mac-manager-resolve-') as directory:\n        checked(s, [TCL, str(ROOT / 'tools/macports-resolve.tcl'), directory, *names],\n                timeout=300, cancellable=True)\n        def rows(file):\n            with (Path(directory) / file).open() as stream:\n                return list(csv.DictReader(stream, delimiter='\\t'))\n        results = rows('root-results.tsv')\n        if len(results) != len(names) or any(r['status'] != '0' for r in results):\n            raise RuntimeError('MacPorts dependency resolution failed: ' + json.dumps(results))\n        nodes = rows('nodes.tsv')\n        if not nodes or not set(names).issubset({r['name'] for r in nodes}):\n            raise RuntimeError('Incomplete MacPorts dependency graph')\n        by_name = {r['name']: r for r in nodes}\n        for row in nodes:\n            valid(row['name'])\n            if row['name'] == 'ngrok' or s.is_excluded(row['name'], 'port'):\n                raise RuntimeError('Excluded MacPorts dependency: ' + row['name'])\n            if not re.fullmatch(r'[A-Za-z0-9_.+-]+', row['archive']):\n                raise RuntimeError('Invalid archive identity')\n            missing = set(filter(None, row['requires'].split(','))) - by_name.keys()\n            if missing:\n                raise RuntimeError('Incomplete runtime closure: ' + str(missing))\n        # The metadata resolver evaluates default variants. Never install a different\n        # installed variant set on the strength of those observations.\n        for pkg in installed or inventory(s):\n            if pkg['name'] in by_name and pkg.get('installed_variants', '') != by_name[pkg['name']]['variants']:\n                raise RuntimeError('Installed variants require a separate migration: ' + pkg['name'])\n        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:\n            checked_nodes = list(pool.map(probe.check_node, nodes))\n        s.raise_if_cancel_requested()\n        missing = [r['archive'] for r in checked_nodes if r['archive_status'] != 'available']\n        if missing:\n            raise RuntimeError('Ready archives unavailable: ' + ', '.join(missing))\n        return dict(names=names, nodes=nodes, checked_at=s.now_iso(),\n                    signatures_verified=False, archive_count=len(nodes))\n\ndef applescript(s, command):\n    # Fixed executable and validated argv only. No user-provided shell programs.\n    source = 'do shell script ' + json.dumps(command) + ' with administrator privileges'\n    return checked(s, ['/usr/bin/osascript', '-e', source], timeout=None)\n\ndef privileged(s, action, names=(), activity=None):\n    if action not in {'install', 'upgrade', 'sync'}:\n        raise ValueError('Unsupported privileged operation')\n    names = [valid(n) for n in names]\n    if 'ngrok' in names or (action != 'sync' and not names):\n        raise ValueError('Excluded or empty privileged operation')\n    if s.is_cancel_requested():\n        raise s.OperationCancelledError('Canceled before authorization')\n    s.set_check_progress(phase='updating_packages', message='Waiting for macOS administrator authorization', eta_seconds=None)\n    argv = [PORT, '-N', '-b', action, *names]\n    # mktemp is executed AS ROOT. All redirected files live in the resulting\n    # root-owned directory; never redirect privileged output into user files.\n    worker = ('umask 022; ' + shlex.join(argv) + ' > \"$1/output\" 2>&1 & child=$!; '\n              'echo \"$child\" > \"$1/pid\"; wait \"$child\"; code=$?; '\n              'echo \"$code\" > \"$1/status\"')\n    # Keep the authorized shell alive until port finishes. Detached processes\n    # launched by Authorization Services may disappear when that shell exits.\n    directory = Path(applescript(s, 'd=$(/usr/bin/mktemp -d /private/tmp/mac-manager.XXXXXXXX); '\n                                '/bin/chmod 755 \"$d\"; echo \"$d\"').strip())\n    if not re.fullmatch(r'/private/tmp/mac-manager\\.[A-Za-z0-9]{8}', str(directory)):\n        raise RuntimeError('Invalid privileged operation receipt')\n    s.append_service_log(f'MacPorts authorized receipt: {directory}')\n    s.raise_if_cancel_requested()\n    foreground = '/bin/sh -c ' + shlex.quote(worker) + ' worker ' + shlex.quote(str(directory))\n    offset, canceled, started = 0, False, False\n    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:\n      command = executor.submit(applescript, s, foreground)\n      while not command.done():\n        if (directory / 'pid').exists() and not started:\n            started = True\n            s.set_check_progress(message='Refreshing MacPorts catalog' if action=='sync' else 'MacPorts is installing verified ready packages', eta_seconds=None)\n        if s.is_cancel_requested() and not canceled and (directory / 'pid').exists() and not (directory / 'status').exists():\n            # The receipt directory and PID were created by root, not supplied\n            # by the renderer. Kill descendants before the root port process.\n            cancel = ('[ -f ' + shlex.quote(str(directory / 'status')) + ' ] && exit 0; '\n                      'p=$(/bin/cat ' + shlex.quote(str(directory / 'pid')) + '); '\n                      'case \"$(/bin/ps -p \"$p\" -o command=)\" in *\"/opt/local/bin/port\"*) ;; *) exit 0;; esac; '\n                      'tree=\"$p\"; frontier=\"$p\"; '\n                      'while [ -n \"$frontier\" ]; do next=\"\"; for parent in $frontier; do '\n                      'children=$(/bin/ps -axo pid=,ppid= | /usr/bin/awk -v p=\"$parent\" \\'$2==p {print $1}\\'); '\n                      'next=\"$next $children\"; done; [ -z \"$(echo \"$next\" | /usr/bin/tr -d \" \")\" ] && break; '\n                      'tree=\"$next $tree\"; frontier=\"$next\"; done; /bin/kill -TERM $tree 2>/dev/null || true')\n            applescript(s, cancel)\n            canceled = True\n        output = (directory / 'output').read_text(errors='replace') if (directory / 'output').exists() else ''\n        if activity and len(output) > offset:\n            latest=output[offset:]\n            for marker,stage in [('Fetching archive','downloading'),('Fetching distfiles','downloading'),\n                                  ('Verifying','verifying'),('Installing','installing'),\n                                  ('Activating','installing'),('Cleaning','finishing')]:\n                if marker in latest: activity.state['stage']=stage\n            activity.observe(output[offset:], 'macports')\n        offset = len(output)\n        time.sleep(.25)\n      command.result()  # Authorization/launch errors must never become a polling loop.\n    if not (directory / 'status').is_file():\n        raise RuntimeError(f'MacPorts worker ended without a completion receipt: {directory}')\n    output = (directory / 'output').read_text(errors='replace')\n    code = int((directory / 'status').read_text().strip())\n    s.append_service_log(f'MacPorts {action}: receipt={directory} exit={code}')\n    if canceled:\n        raise s.OperationCancelledError('MacPorts operation canceled')\n    return s.CommandResult(code, output, '' if code == 0 else output)\n\ndef plan(s, pkg):\n    item = {**pkg, 'provider': 'macports', 'version': pkg['latest_version'], 'ready': False}\n    if pkg.get('inventory_stale') or s.is_excluded(pkg['name'], 'port') or pkg['name'] == 'ngrok':\n        return {**item, 'reason': 'excluded_or_unverified'}\n    try:\n        current = inventory(s)\n        row = next((p for p in current if p['name'] == pkg['name']), None)\n        if not row:\n            raise RuntimeError('Package is not installed in MacPorts; use migration approval')\n        item.update(version=row['latest_version'], preflight=preflight(s, [pkg['name']], current), ready=True)\n    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        item.update(reason='binary_route_unavailable', detail=str(error))\n    return item\n\ndef upgrade(s, item):\n    activity = s.UpgradeActivity(item['name'])\n    activity.state.update(provider='macports', stage='downloading')\n    activity.observe('Checking ready archives before MacPorts update', 'macports')\n    fresh = plan(s, item)\n    if not fresh['ready']:\n        return dict(name=item['name'], kind='port', provider='macports', ok=False, code=78,\n                    stdout='', stderr=fresh.get('detail', fresh.get('reason', 'Blocked')))\n    result = privileged(s, 'upgrade', [item['name']], activity)\n    after = next((p for p in inventory(s) if p['name'] == item['name']), None)\n    graph = fresh['preflight'].get('nodes', [])\n    root = next((n for n in graph if n.get('name') == item['name']), None)\n    expected = None if root is None else str(root['version']) + (\n        '_' + str(root['revision']) if root.get('revision') not in ('', '0', None) else '')\n    exact = (after is not None and root is not None\n             and after.get('installed_version') == expected\n             and after.get('installed_variants', '') == root.get('variants', ''))\n    ok = result.code == 0 and exact\n    smoke_error = None\n    if ok:\n        import provider_ownership\n        import migration_profiles\n        registry = provider_ownership.load_routes(s.TOOLS_ROOT)\n        if registry.get('error'):\n            ok = False\n            smoke_error = 'Ownership registry could not be verified'\n        else:\n            owner = next((r for r in registry.get('routes', [])\n                          if r.get('target_installation_id') == f\"macports:{item['name']}\"\n                          and r.get('status') == 'committed'), None)\n            if owner:\n                profile = migration_profiles.get(owner.get('profile_id'))\n                if not profile or owner.get('profile_version') != profile.version:\n                    ok = False\n                    smoke_error = 'Managed profile missing or changed'\n                else:\n                    try:\n                        migration_profiles.smoke(s, profile, root['version'])\n                    except Exception as error:\n                        ok = False\n                        smoke_error = 'Updated package failed usage verification: ' + str(error)\n    return dict(name=item['name'], kind='port', provider='macports', ok=ok,\n                installed_version=after.get('installed_version') if after else None,\n                code=result.code if result.code else (0 if ok else 1), stdout=result.stdout,\n                stderr=smoke_error or (result.stderr if result.code else\n                       '' if ok else 'Exact binary version or variants verification failed'))\n",
    "backend/mac_manager.py": "\"\"\"Combined inventory and explicit one-time migration transactions.\"\"\"\nfrom __future__ import annotations\nimport hashlib\nimport json\nimport re\nfrom pathlib import Path\nimport shutil\nimport tempfile\nimport time\nimport threading\n\nSCAN_CONTEXT = threading.local()\nimport macports_provider as mp\nimport update_routes as routes\nimport catalog_refresh\nimport automatic_alternatives\nimport installation_evidence\nimport unified_updates\n\ndef mapping():\n    return json.loads((Path(__file__).with_name('macports-map.json')).read_text())\n\ndef combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):\n    if track_progress:\n        s.reset_check_progress_for_run()\n        s.set_check_progress(operation_name='check', phase='collecting_installed',\n                             message='Checking Homebrew inventory', current_package='Homebrew',\n                             done=0, total=3, eta_seconds=None)\n    prior = getattr(SCAN_CONTEXT, 'active', False)\n    SCAN_CONTEXT.active = prior or track_progress\n    try:\n        return _combined(s, brew_scan, refresh, track_progress, enrich_details)\n    except Exception as error:\n        if track_progress:\n            canceled = isinstance(error, s.OperationCancelledError)\n            s.set_check_progress(running=False, phase='canceled' if canceled else 'error',\n                message='Check canceled' if canceled else 'Check failed: ' + str(error),\n                canceled=canceled, error=None if canceled else str(error),\n                current_package=None, eta_seconds=None, completed_at=s.now_iso())\n        raise\n    finally:\n        SCAN_CONTEXT.active = prior\n\n\ndef _combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):\n    installation_evidence.reset()\n    previous = s.read_json(s.STATE_FILE) or {}\n    status, errors = {}, []\n    try:\n        state = brew_scan(refresh, track_progress=track_progress, enrich_details=enrich_details, complete_progress=False)\n        packages = state['packages']\n        errors.extend(state.get('errors', []))\n        status['homebrew'] = {'ok': not bool(state.get('errors')), 'checked_at': s.now_iso(),\n                              'error': '; '.join(state.get('errors', []))}\n    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('Homebrew: ' + str(error))\n        packages = [dict(p, inventory_stale=True, update_block_reason='inventory_unavailable')\n                    for p in previous.get('packages', []) if p.get('provider') != 'macports']\n        status['homebrew'] = {'ok': False, 'error': str(error),\n            'checked_at': previous.get('providers', {}).get('homebrew', {}).get('checked_at')}\n    # Existing independently managed tools and their Brew copies are separate\n    # installations. Keep legacy formula requests tied to the Brew copy.\n    extras=[]\n    for pkg in packages:\n        if pkg.get('provider','homebrew') not in {'homebrew','macports'}:\n            if pkg['kind'] != 'tool' and pkg.get('brew_installation_present') is not False and pkg.get('brew_installed_version'):\n                copy=dict(pkg,provider='homebrew',installed_version=pkg['brew_installed_version'],\n                          outdated=routes.version_key(pkg['brew_installed_version']) < routes.version_key(pkg['latest_version']))\n                extras.append(copy)\n            pkg['kind']='tool'\n    packages.extend(extras)\n    # Even without Brew, retain registered tools, but block updates until their\n    # target metadata can be verified. Never call an unknown target current.\n    present={p['name'] for p in packages if p.get('kind')=='tool'}\n    for name,record in routes.read_records(s.TOOLS_ROOT).items():\n        if name not in present:\n            packages.append(dict(name=name,kind='tool',provider=record['provider'],\n                installed_version=record['version'],latest_version='unknown',outdated=False,\n                inventory_stale=True,update_block_reason='inventory_unavailable',description='Managed tool'))\n    if track_progress:\n        s.raise_if_cancel_requested()\n        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',\n                             current_package='MacPorts', done=1, total=3, eta_seconds=None)\n    interactive = track_progress and s.read_current_operation().get('name') == 'check_now'\n    catalog_state = catalog_refresh.ensure(s, refresh=refresh, interactive=interactive)\n    if track_progress:\n        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',\n                             current_package='MacPorts', eta_seconds=None)\n    try:\n        ports = mp.inventory(s)\n        if not catalog_state['fresh']:\n            ports = [dict(p, inventory_stale=True, update_block_reason='inventory_unavailable') for p in ports]\n        packages.extend(ports)\n        status['macports'] = {'ok': True, 'checked_at': s.now_iso(),\n                             'catalog': catalog_state, 'catalog_note': catalog_state['status']}\n    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('MacPorts: ' + str(error))\n        packages.extend(dict(p, inventory_stale=True, update_block_reason='inventory_unavailable')\n                        for p in previous.get('packages', []) if p.get('provider') == 'macports')\n        status['macports'] = {'ok': False, 'error': str(error), 'catalog': catalog_state,\n            'checked_at': previous.get('providers', {}).get('macports', {}).get('checked_at')}\n    for pkg in packages:\n        provider = pkg.get('provider', 'homebrew')\n        pkg.update(provider=provider, provider_name=pkg['name'], installation_id=(\n            f\"homebrew:{pkg['kind']}:{pkg['name']}\" if provider=='homebrew' else\n            f\"macports:{pkg['name']}\" if provider=='macports' else\n            f\"pixi:{pkg['name']}\" if provider=='pixi' else\n            f\"{provider}:{pkg['kind']}:{pkg['name']}\"))\n        if provider == 'macports':\n            pkg['excluded'] = pkg['name'] == 'ngrok' or s.is_excluded(pkg['name'], 'port')\n            if pkg['excluded']: pkg['outdated'] = False\n    links = mapping()\n    for pkg in packages:\n        if pkg['provider'] != 'macports':\n            pkg['parallel_installations'] = [p['installation_id'] for p in packages\n                if p['provider'] == 'macports' and p['name'] in links.get(pkg['name'], [])]\n    if track_progress:\n        s.raise_if_cancel_requested()\n        s.set_check_progress(phase='preflight', message='Checking binary routes and migration proposals',\n                             current_package=None, done=2, total=3, eta_seconds=None)\n    offers = []\n    try:\n        from update_engine import Engine\n        engine = Engine(s)\n        catalog, installed = engine.catalog(), engine.installed()\n        for p in packages:\n            if p['provider'] == 'homebrew' and p['kind'] == 'formula' and p['name'] in links and not p.get('excluded'):\n                blockers = routes.bottle_blockers(p['name'], catalog, installed, s.load_settings()['excluded_packages'])\n                if blockers:\n                    if s.load_settings()['binary_only']:\n                        p['update_block_reason'] = 'source_required'\n                    offers.append(dict(name=p['name'], ports=links.get(p['name'], []),\n                        status='requires_live_check' if links.get(p['name']) else 'mapping_unverified',\n                        executable=p['name'] in {'terminal-notifier','include-what-you-use'}, source_blockers=blockers))\n    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('Migration recommendations: ' + str(error))\n    if refresh:\n        try:\n            offers = automatic_alternatives.check(s, offers, packages, catalog_state['fresh'], report=track_progress)\n        except s.OperationCancelledError:\n            raise\n        except Exception as error:\n            errors.append('Alternative checks: ' + str(error))\n            for offer in offers:\n                offer.update(status='check_failed', detail=str(error), checked_at=s.now_iso())\n    else:\n        previous_offers = {o['name']: o for o in previous.get('migrations', [])}\n        for offer in offers:\n            pkg = next(p for p in packages if p['name']==offer['name'] and p['kind']=='formula')\n            prior = previous_offers.get(offer['name'], {})\n            offer['homebrew_target'] = pkg.get('latest_version')\n            if (catalog_state['fresh'] and prior.get('homebrew_target') == pkg.get('latest_version')\n                    and prior.get('version_policy') == automatic_alternatives.VERSION_POLICY\n                    and prior.get('installed_version') == pkg.get('installed_version')\n                    and prior.get('status') not in {'requires_live_check','checking_pending'}\n                    and catalog_refresh.recent(prior.get('checked_at'), 6*3600)):\n                offer.update(prior)\n            else:\n                offer.update(status='checking_pending' if offer['ports'] else 'mapping_unverified')\n            pkg.update(alternative_status=offer['status'], alternative_detail=offer.get('detail',''),\n                       alternative_target=offer.get('target'), alternative_below_homebrew=offer.get('below_homebrew_target', False))\n    # T02 classifies mapped Homebrew CLI tools even when Brew has a bottle.\n    # Supplemental records are not advertised as MacPorts migration offers.\n    mapped = set(links) | set(routes.PIXI_TOOLS)\n    existing = {offer['name'] for offer in offers}\n    supplemental = [dict(name=p['name'], status='classification_only',\n                         ports=[], executable=False)\n                    for p in packages if p.get('provider') == 'homebrew'\n                    and p.get('kind') == 'formula' and p.get('name') in mapped\n                    and p.get('name') not in existing\n                    and not p.get('excluded') and not p.get('inventory_stale')]\n    augmented = installation_evidence.annotate(\n        s, offers + supplemental, packages, refresh, report=track_progress)\n    classifications = {offer['name']: offer['classification'] for offer in augmented\n                       if offer.get('classification')}\n    for package in packages:\n        if package.get('provider') == 'homebrew' and package.get('kind') == 'formula':\n            if package.get('name') in classifications:\n                package['classification'] = classifications[package['name']]\n    offers = augmented[:len(offers)]\n    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')\n    state = dict(app=s.APP_NAME, updated_at=s.now_iso(), packages=packages,\n                 counts=s.summarize_counts(packages), errors=errors, providers=status,\n                 partial=bool(errors) or not all(p['ok'] for p in status.values()), migrations=offers,\n                 migration_journal=journal)\n    if track_progress:\n        s.raise_if_cancel_requested()\n    if refresh:\n        try:\n            unified_updates.generate_plan(s, state)\n        except s.OperationCancelledError:\n            raise\n        except Exception as error:\n            errors.append('Unified plan: ' + str(error))\n            state['partial'] = True\n    else:\n        unified_updates.attach_cached(s, state)\n    s.atomic_write_json(s.STATE_FILE, state)\n    if track_progress:\n        failed = not any(provider['ok'] for provider in status.values())\n        s.set_check_progress(running=False, phase='error' if failed else 'completed',\n            message='All inventory checks failed' if failed else\n                    ('Inventory checked with warnings' if state['partial'] else 'Package inventories checked'),\n            done=3, total=3, current_package=None, error='; '.join(errors) if errors else None,\n            completed_at=s.now_iso(), eta_seconds=None if failed else 0)\n    return state\n\ndef migration_plan(s, name):\n    if name == 'ngrok' or s.is_excluded(name, 'formula'):\n        raise ValueError('Package is excluded')\n    roots = mapping().get(name, [])\n    if not roots:\n        raise ValueError('No verified package mapping')\n    current = s.fetch_installed_inventory(s.fetch_outdated_index())\n    pkg = next((p for p in current if p['name'] == name and p['kind'] == 'formula'), None)\n    if pkg and (pkg.get('inventory_stale') or pkg.get('inventory_verification', {}).get('status', 'verified') != 'verified'):\n        raise ValueError('Installation evidence is unverified; scan and resolve the reported inventory gap')\n    if not pkg or pkg.get('brew_installation_present') is False:\n        raise ValueError('The Homebrew installation is not present')\n    from update_engine import Engine\n    engine = Engine(s)\n    blockers = routes.bottle_blockers(name, engine.catalog(), engine.installed(), s.load_settings()['excluded_packages'])\n    if not blockers:\n        raise ValueError('Homebrew has a binary route; migration is unnecessary')\n    graph = mp.preflight(s, roots)\n    target = next(n['version'] for n in graph['nodes'] if n['name'] == roots[0])\n    probe = mp.load_probe()\n    # Cross-manager comparison is strictly upstream only, never revisions.\n    candidates = {root: next(n['version'] for n in graph['nodes'] if n['name'] == root) for root in roots}\n    comparison = automatic_alternatives.compare_versions(probe, pkg['installed_version'], pkg['latest_version'], candidates)\n    if comparison['status'] != 'version_eligible':\n        raise ValueError(comparison['detail'])\n    consumers = mp.checked(s, [s.brew_bin(), 'uses', '--installed', name], timeout=90).splitlines()\n    plan = dict(name=name, roots=roots, installed_version=pkg['installed_version'], target=target,\n        version_policy=automatic_alternatives.VERSION_POLICY,\n        homebrew_target=pkg['latest_version'], below_homebrew_target=comparison['below_homebrew_target'],\n        graph=graph, consumers=consumers, executable=name=='terminal-notifier',\n        paths=['/opt/local', '/usr/local'], remove_brew=not consumers,\n        requires='Notification integration test' if name=='terminal-notifier' else 'Consumer/project/data migration plan',\n        created_at=time.time())\n    plan['id'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()\n    s.atomic_write_json(s.DATA_DIR / 'migration-plan.json', plan)\n    return plan\n\ndef approve(s, plan_id):\n    old = s.read_json(s.DATA_DIR / 'migration-plan.json') or {}\n    if old.get('id') != plan_id or time.time()-old.get('created_at', 0)>900:\n        raise ValueError('Migration plan expired; request a fresh plan')\n    if not old.get('executable') or old.get('name') != 'terminal-notifier':\n        raise ValueError('This package requires its own consumer migration plan')\n    s.raise_if_cancel_requested()\n    fresh = migration_plan(s, old['name'])\n    for key in ('installed_version', 'target', 'consumers', 'remove_brew', 'version_policy'):\n        if old.get(key) != fresh.get(key):\n            raise ValueError('Migration changed; review a new plan')\n    if old['graph']['nodes'] != fresh['graph']['nodes']:\n        raise ValueError('Dependency graph changed; review a new plan')\n    journal = dict(plan=fresh, status='running', stage='backup', started_at=s.now_iso())\n    dest = s.DATA_DIR / 'migration-journal.json'\n    def stage(value):\n        journal['stage']=value\n        s.atomic_write_json(dest, journal)\n    stage('backup')\n    try:\n        # Preserve the whole original keg, its symlinks and metadata before uninstall.\n        prefix = Path(mp.checked(s, [s.brew_bin(), '--prefix', 'terminal-notifier'], timeout=30).strip()).resolve()\n        cellar = Path(mp.checked(s, [s.brew_bin(), '--cellar'], timeout=30).strip()).resolve()\n        if not prefix.is_relative_to(cellar / 'terminal-notifier') or not prefix.is_dir():\n            raise RuntimeError('Unexpected terminal-notifier keg path')\n        backup = s.TOOLS_ROOT / 'recovery' / ('notifier-' + str(time.time_ns()))\n        backup.mkdir(parents=True)\n        shutil.copytree(prefix, backup / prefix.name, symlinks=True)\n        for path in prefix.rglob('*'):\n            relative = path.relative_to(prefix)\n            copy = backup / prefix.name / relative\n            if path.is_symlink():\n                if path.readlink() != copy.readlink(): raise RuntimeError('Backup link mismatch')\n            elif path.is_file() and hashlib.sha256(path.read_bytes()).digest()!=hashlib.sha256(copy.read_bytes()).digest():\n                raise RuntimeError('Backup content mismatch')\n        journal['backup']=str(backup)\n        stage('install')\n        activity = s.UpgradeActivity('terminal-notifier')\n        activity.state['provider']='macports'\n        current = next((p for p in mp.inventory(s) if p['name']=='terminal-notifier'), None)\n        root = next(n for n in fresh['graph']['nodes'] if n['name']=='terminal-notifier')\n        expected = root['version'] + '_' + root['revision']\n        if not current or current['installed_version'] != expected or current['outdated']:\n            result = mp.privileged(s, 'install', ['terminal-notifier'], activity)\n            if result.code: raise RuntimeError(result.stderr)\n        else:\n            journal['installation_reused'] = True\n        s.raise_if_cancel_requested()\n        stage('verify')\n        installed = next((p for p in mp.inventory(s) if p['name']=='terminal-notifier'), None)\n        if not installed or installed['outdated']:\n            raise RuntimeError('MacPorts installation verification failed')\n        binary = notifier_path()\n        if not binary: raise RuntimeError('MacPorts notification executable is absent')\n        version = mp.checked(s, [str(binary), '-version'], timeout=30)\n        if not re.search(r'(?<![0-9])'+re.escape(fresh['target'])+r'(?![0-9])',version):\n            raise RuntimeError('Notifier version mismatch')\n        # The CLI package and the application's notification delivery identity\n        # are separate. Preserve the existing app helper and its user permission.\n        # Running MacPorts' default bundle ID would change that identity.\n        helper = app_notification_path()\n        s.send_notification({'packages': [], 'notification_test': True}, notifier_override=str(helper))\n        stage('activate')\n        s.atomic_write_json(s.DATA_DIR / 'notifier-route.json', {'provider':'application_helper',\n            'path':str(helper), 'cli_provider':'macports', 'cli_path':str(binary)})\n        s.raise_if_cancel_requested()\n        stage('cleanup')\n        consumers = mp.checked(s, [s.brew_bin(), 'uses', '--installed', 'terminal-notifier'], timeout=60).splitlines()\n        if not consumers:\n            journal['brew_uninstall_output'] = mp.checked(s, [s.brew_bin(), 'uninstall', '--formula', 'terminal-notifier'],\n                timeout=120, env_override={'HOMEBREW_NO_AUTOREMOVE':'1',\n                    'HOMEBREW_NO_INSTALL_CLEANUP':'1', 'HOMEBREW_NO_AUTO_UPDATE':'1'})\n        journal.update(status='completed', completed_at=s.now_iso(), preserved_for_consumers=consumers)\n        stage('completed')\n        return journal\n    except Exception as error:\n        journal.update(status='needs_attention', error=str(error), completed_at=s.now_iso())\n        s.atomic_write_json(dest, journal)\n        raise\n\ndef app_notification_path():\n    helper = Path.home() / 'Applications/Homebrew Update Notifications.app/Contents/MacOS/terminal-notifier'\n    if not helper.is_file():\n        raise RuntimeError('The application notification helper is absent; configure its notification identity before migrating')\n    return helper\n\ndef notifier_path():\n    candidates = [Path('/opt/local/bin/terminal-notifier'),\n        Path('/opt/local/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier'),\n        Path('/Applications/MacPorts/terminal-notifier.app/Contents/MacOS/terminal-notifier')]\n    return next((p for p in candidates if p.is_file()), None)\n\n# Bundle 003 keeps the verified notifier pilot intact and delegates new profiles.\n_legacy_migration_plan=migration_plan\n_legacy_approve=approve\nimport migration_engine as _migration_engine\ndef migration_plan(s,name): return _legacy_migration_plan(s,name) if name=='terminal-notifier' else _migration_engine.plan(s,name)\ndef approve(s,plan_id):\n old=s.read_json(s.DATA_DIR/'migration-plan.json') or {}\n return _legacy_approve(s,plan_id) if old.get('name')=='terminal-notifier' and not old.get('schema_version') else _migration_engine.approve(s,plan_id)\ndef migration_recovery_plan(s, transaction_id, action): return _migration_engine.recovery_plan(s, transaction_id, action)\n",
    "electron/main.js": "const { app, BrowserWindow, ipcMain, dialog } = require('electron');\nconst fs = require('node:fs');\nconst os = require('node:os');\nconst net = require('node:net');\nconst http = require('node:http');\nconst path = require('node:path');\nconst { spawn, spawnSync } = require('node:child_process');\nconst { createAppUpdater } = require('./app-updater');\n\napp.setPath('userData', path.join(os.homedir(), 'Library', 'Application Support', 'Homebrew Update Manager'));\n\nconst DEV_APP_ROOT = path.join(__dirname, '..');\nconst PACKAGED_ASAR_ROOT = path.join(process.resourcesPath, 'app.asar');\nconst PACKAGED_UNPACKED_ROOT = path.join(process.resourcesPath, 'app.asar.unpacked');\n\nfunction appRoot({ unpacked = false } = {}) {\n  if (!app.isPackaged) {\n    return DEV_APP_ROOT;\n  }\n\n  return unpacked ? PACKAGED_UNPACKED_ROOT : PACKAGED_ASAR_ROOT;\n}\n\nfunction resolveServiceScriptCandidates() {\n  return [\n    path.join(appRoot({ unpacked: true }), 'backend', 'brew-updates-service.py'),\n    path.join(appRoot({ unpacked: false }), 'backend', 'brew-updates-service.py'),\n    path.join(process.resourcesPath, 'backend', 'brew-updates-service.py'),\n  ];\n}\n\nfunction resolveServiceScriptPath() {\n  const candidates = resolveServiceScriptCandidates();\n  const resolved = candidates.find((candidate) => fs.existsSync(candidate)) || candidates[0];\n  return {\n    resolved,\n    candidates,\n  };\n}\n\nconst SERVICE_SCRIPT_INFO = resolveServiceScriptPath();\nconst SERVICE_SCRIPT = SERVICE_SCRIPT_INFO.resolved;\nconst SERVICE_HOST = '127.0.0.1';\nconst SERVICE_PORT_MIN = 8765;\nconst SERVICE_PORT_MAX = 8795;\nlet activeServicePort = SERVICE_PORT_MIN;\nconst REQUIRED_PYTHON = '3.14';\nconst FIXED_UPDATE_REPO = 'tzoharlary/brew-update-manager';\n\nlet resolvedPythonBin = null;\n\nlet mainWindow = null;\nlet appUpdater = null;\nlet ownedServiceChild = null;\n\nfunction broadcastAppUpdateProgress(payload) {\n  for (const win of BrowserWindow.getAllWindows()) {\n    if (!win.isDestroyed()) {\n      win.webContents.send('app-update:progress', payload);\n    }\n  }\n}\n\nfunction executableExists(filePath) {\n  if (!filePath) return false;\n  try {\n    fs.accessSync(filePath, fs.constants.X_OK);\n    return true;\n  } catch {\n    return false;\n  }\n}\n\nfunction isPython314(pythonBin) {\n  const probe = spawnSync(\n    pythonBin,\n    ['-c', 'import sys; raise SystemExit(0 if (sys.version_info.major, sys.version_info.minor)==(3,14) else 1)'],\n    { stdio: 'ignore' },\n  );\n  return probe.status === 0;\n}\n\nfunction resolvePythonBin() {\n  const fromEnv = process.env.PYTHON_BIN ? [process.env.PYTHON_BIN] : [];\n  const candidates = [\n    ...fromEnv,\n    path.join(os.homedir(), 'Library', 'Application Support', 'Homebrew Update Manager',\n      'runtime', 'python', 'current', 'bin', 'python3.14'),\n    '/opt/homebrew/bin/python3.14',\n    '/opt/homebrew/bin/python3',\n    '/usr/local/bin/python3.14',\n    '/usr/local/bin/python3',\n    'python3.14',\n    'python3',\n  ];\n\n  for (const candidate of candidates) {\n    if (candidate.includes('/') && !executableExists(candidate)) {\n      continue;\n    }\n    if (isPython314(candidate)) {\n      return candidate;\n    }\n  }\n\n  throw new Error(\n    `Python ${REQUIRED_PYTHON} not found. Please install Python ${REQUIRED_PYTHON} and make sure it is available in PATH.`,\n  );\n}\n\nfunction getPythonBin() {\n  if (!resolvedPythonBin) {\n    resolvedPythonBin = resolvePythonBin();\n  }\n  return resolvedPythonBin;\n}\n\nfunction delay(ms) {\n  return new Promise((resolve) => setTimeout(resolve, ms));\n}\n\nfunction serviceBase() {\n  return `http://${SERVICE_HOST}:${activeServicePort}`;\n}\n\nfunction serviceUrl(pathname) {\n  return `${serviceBase()}${pathname}`;\n}\n\nasync function fetchJson(url, options = {}) {\n  // Upgrade responses may legitimately take hours. Keep their connection open\n  // without fetch's response-header deadline; progress uses separate requests.\n  return new Promise((resolve, reject) => {\n    const headers = { ...(options.headers || {}) };\n    if (options.body) headers['Content-Length'] = Buffer.byteLength(options.body);\n    const request = http.request(url, { method: options.method || 'GET', headers, timeout: 0 }, (response) => {\n      let body = '';\n      response.setEncoding('utf8');\n      response.on('data', chunk => { body += chunk; });\n      response.on('error', reject);\n      response.on('end', () => {\n        let payload;\n        try { payload = JSON.parse(body); } catch { reject(new Error('Invalid backend response')); return; }\n        if (response.statusCode < 200 || response.statusCode >= 300) {\n          reject(new Error(payload.detail || payload.error || `HTTP ${response.statusCode}`));\n        } else resolve(payload);\n      });\n    });\n    request.on('error', reject);\n    if (options.body) request.write(options.body);\n    request.end();\n  });\n}\n\nasync function serviceHealthyOnPort(port) {\n  try {\n    const res = await fetch(`http://${SERVICE_HOST}:${port}/health`, { method: 'GET' });\n    if (!res.ok) return false;\n\n    const payload = await res.json().catch(() => null);\n    if (!payload || typeof payload !== 'object') return false;\n    return payload.ok === true && payload.app === 'Mac Update Manager' && payload.protocol === 6;\n  } catch {\n    return false;\n  }\n}\n\nfunction isPortAvailable(port) {\n  return new Promise((resolve) => {\n    const server = net.createServer();\n\n    server.once('error', () => {\n      resolve(false);\n    });\n\n    server.once('listening', () => {\n      server.close(() => resolve(true));\n    });\n\n    server.listen(port, SERVICE_HOST);\n  });\n}\n\nasync function selectServicePort() {\n  for (let port = SERVICE_PORT_MIN; port <= SERVICE_PORT_MAX; port += 1) {\n    // eslint-disable-next-line no-await-in-loop\n    if (await serviceHealthyOnPort(port)) {\n      return { port, alreadyRunning: true };\n    }\n  }\n\n  for (let port = SERVICE_PORT_MIN; port <= SERVICE_PORT_MAX; port += 1) {\n    // eslint-disable-next-line no-await-in-loop\n    if (await isPortAvailable(port)) {\n      return { port, alreadyRunning: false };\n    }\n  }\n\n  throw new Error(`No available backend port found in range ${SERVICE_PORT_MIN}-${SERVICE_PORT_MAX}`);\n}\n\nasync function ensureServiceRunning() {\n  const selection = await selectServicePort();\n  activeServicePort = selection.port;\n\n  if (selection.alreadyRunning) return;\n\n  if (!fs.existsSync(SERVICE_SCRIPT)) {\n    const tried = SERVICE_SCRIPT_INFO.candidates\n      .map((candidate) => `- ${candidate}`)\n      .join('\\n');\n    throw new Error(`Backend service script missing: ${SERVICE_SCRIPT}\\nTried:\\n${tried}`);\n  }\n\n  const pythonBin = getPythonBin();\n\n  const child = spawn(pythonBin, [\n    SERVICE_SCRIPT,\n    '--serve',\n    '--host',\n    SERVICE_HOST,\n    '--port',\n    String(activeServicePort),\n  ], {\n    detached: false,\n    stdio: 'ignore',\n  });\n  ownedServiceChild = child;\n  child.once('exit', () => {\n    if (ownedServiceChild === child) ownedServiceChild = null;\n  });\n\n  for (let i = 0; i < 60; i += 1) {\n    // eslint-disable-next-line no-await-in-loop\n    if (await serviceHealthyOnPort(activeServicePort)) return;\n    // eslint-disable-next-line no-await-in-loop\n    await delay(200);\n  }\n\n  throw new Error(`Service did not become healthy in time on port ${activeServicePort}`);\n}\n\nfunction createWindow() {\n  mainWindow = new BrowserWindow({\n    width: 1340,\n    height: 900,\n    minWidth: 1100,\n    minHeight: 700,\n    title: 'Mac Update Manager',\n    webPreferences: {\n      preload: path.join(__dirname, 'preload.js'),\n      contextIsolation: true,\n      nodeIntegration: false,\n    },\n  });\n\n  mainWindow.loadFile(path.join(appRoot(), 'renderer', 'index.html'));\n\n  mainWindow.on('closed', () => {\n    mainWindow = null;\n  });\n}\n\nfunction focusMainWindow() {\n  if (!mainWindow) {\n    createWindow();\n    return;\n  }\n\n  if (mainWindow.isMinimized()) {\n    mainWindow.restore();\n  }\n  mainWindow.focus();\n}\n\nasync function bootstrap() {\n  await ensureServiceRunning();\n  createWindow();\n}\n\n// Single-instance behavior: additional launches focus existing window.\nconst gotLock = app.requestSingleInstanceLock();\nif (!gotLock) {\n  app.quit();\n} else {\n  app.on('second-instance', () => {\n    focusMainWindow();\n  });\n}\n\napp.whenReady().then(async () => {\n  try {\n    appUpdater = createAppUpdater({\n      app,\n      repo: FIXED_UPDATE_REPO,\n    });\n\n    const marker = appUpdater.readAndConsumeUpdateMarker();\n    if (marker?.status === 'rolled_back') {\n      dialog.showErrorBox(\n        'Mac Update Manager',\n        'The previous app update failed to start correctly and was rolled back to the last working version.',\n      );\n    }\n\n    await bootstrap();\n  } catch (error) {\n    dialog.showErrorBox('Mac Update Manager', `Failed to start backend service:\\n${error.message}`);\n    app.quit();\n    return;\n  }\n\n  app.on('activate', () => {\n    if (BrowserWindow.getAllWindows().length === 0) {\n      createWindow();\n    } else {\n      focusMainWindow();\n    }\n  });\n});\n\napp.on('window-all-closed', () => {\n  app.quit();\n});\n\napp.on('before-quit', () => {\n  if (ownedServiceChild && ownedServiceChild.exitCode === null) {\n    // The backend drains any active check/update before stopping its server.\n    ownedServiceChild.kill('SIGTERM');\n    ownedServiceChild = null;\n  }\n});\n\nipcMain.handle('state:get', async () => fetchJson(serviceUrl('/api/state')));\nipcMain.handle('update:plan:get', async () => fetchJson(serviceUrl('/api/update-plan')));\nipcMain.handle('progress:get', async () => fetchJson(serviceUrl('/api/progress')));\nipcMain.handle('updates:history:get', async () => fetchJson(serviceUrl('/api/update-history')));\nipcMain.handle('settings:get', async () => fetchJson(serviceUrl('/api/settings')));\nipcMain.handle('settings:update-policy', async (_event, payload) => fetchJson(serviceUrl('/api/settings/update-policy'), {\n  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('settings:scheduler:update', async (_event, payload) => fetchJson(serviceUrl('/api/settings/scheduler'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('settings:brew-path:update', async (_event, payload) => fetchJson(serviceUrl('/api/settings/brew-path'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('settings:brew-path:auto-detect', async () => fetchJson(serviceUrl('/api/brew/auto-detect')));\nipcMain.handle('app-update:check', async () => {\n  if (!appUpdater) {\n    throw new Error('App updater is not initialized');\n  }\n\n  return appUpdater.checkForUpdate({\n    onProgress: broadcastAppUpdateProgress,\n  });\n});\nipcMain.handle('app-update:download-install', async () => {\n  if (!appUpdater) {\n    throw new Error('App updater is not initialized');\n  }\n\n  return appUpdater.downloadAndInstallUpdate({\n    onProgress: broadcastAppUpdateProgress,\n  });\n});\nipcMain.handle('check:run', async () => fetchJson(serviceUrl('/api/check'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify({}),\n}));\nipcMain.handle('update:one', async (_event, payload) => fetchJson(serviceUrl('/api/update-one'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('update:all', async (_event, payload) => fetchJson(serviceUrl('/api/update-all'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('update:selected', async (_event, payload) => fetchJson(serviceUrl('/api/update-selected'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('operation:cancel', async () => fetchJson(serviceUrl('/api/cancel-operation'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify({}),\n}));\n\nfor (const [channel, endpoint] of [['migration:plan','/api/migrations/plan'],\n  ['migration:approve','/api/migrations/approve'], ['migration:recovery-plan','/api/migrations/recovery-plan'], ['macports:sync','/api/macports/sync']]) {\n  ipcMain.handle(channel, async (_event, payload) => fetchJson(serviceUrl(endpoint), {\n    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload || {}),\n  }));\n}\n",
    "electron/preload.js": "const { contextBridge, ipcRenderer } = require('electron');\n\ncontextBridge.exposeInMainWorld('brewApp', {\n  migrationPlan: (name) => ipcRenderer.invoke('migration:plan', {name}),\n  migrationApprove: (id) => ipcRenderer.invoke('migration:approve', {id}),\n  migrationRecoveryPlan: (transaction_id, action) => ipcRenderer.invoke('migration:recovery-plan', {transaction_id, action}),\n  macportsSync: () => ipcRenderer.invoke('macports:sync'),\n  getState: () => ipcRenderer.invoke('state:get'),\n  getUpdatePlan: () => ipcRenderer.invoke('update:plan:get'),\n  getProgress: () => ipcRenderer.invoke('progress:get'),\n  getUpdateHistory: () => ipcRenderer.invoke('updates:history:get'),\n  getSettings: () => ipcRenderer.invoke('settings:get'),\n  updateScheduler: (scheduler) => ipcRenderer.invoke('settings:scheduler:update', { scheduler }),\n  updateBrewPath: (brewPath) => ipcRenderer.invoke('settings:brew-path:update', { brew_path: brewPath }),\n  updatePolicy: (policy) => ipcRenderer.invoke('settings:update-policy', policy),\n  autoDetectBrewPath: () => ipcRenderer.invoke('settings:brew-path:auto-detect'),\n  checkAppUpdate: () => ipcRenderer.invoke('app-update:check'),\n  downloadAndInstallAppUpdate: () => ipcRenderer.invoke('app-update:download-install'),\n  onAppUpdateProgress: (handler) => {\n    if (typeof handler !== 'function') {\n      return () => {};\n    }\n    const listener = (_event, payload) => handler(payload);\n    ipcRenderer.on('app-update:progress', listener);\n    return () => ipcRenderer.removeListener('app-update:progress', listener);\n  },\n  runCheckNow: () => ipcRenderer.invoke('check:run'),\n  updateOne: (name, kind, decision) => ipcRenderer.invoke('update:one', decision || { name, kind }),\n  updateAll: (payload) => ipcRenderer.invoke('update:all', payload || {}),\n  updateSelected: (packages) => ipcRenderer.invoke('update:selected', Array.isArray(packages) ? { packages } : packages),\n  cancelOperation: () => ipcRenderer.invoke('operation:cancel'),\n});\n",
    "renderer/mac-manager.js": "// Automatic availability results; review and approval remain separate actions.\nfunction macAlternativeLabel(status) {\n  status=status||'checking_pending';\n  const he=i18n.lang==='he';\n  const labels={\n    binary_available:['נמצאה חבילה מוכנה ב־MacPorts','Ready package available in MacPorts'],\n    mapping_unverified:['אין התאמה מאומתת ב־MacPorts','No verified MacPorts mapping'],\n    catalog_unverified:['בדיקת החלופה ממתינה לרענון מידע','Alternative check awaits catalog refresh'],\n    checking_pending:['החלופה תיבדק אוטומטית בבדיקה הבאה','Alternative will be checked automatically on the next scan'],\n    older_than_installed:['החלופה ישנה מהגרסה המותקנת — ירידה בגרסה חסומה','Alternative is older than the installed version; downgrade blocked'],\n    not_newer:['אין גרסה חדשה יותר במסלול זה','No newer upstream version on this route'],\n    version_unverified:['לא ניתן לאמת התאמת גרסאות','Version compatibility could not be verified'],\n    archive_missing:['חבילה מוכנה או תלות חסרה באתרים שנבדקו','Ready package or dependency is missing from checked sites'],\n    archive_unknown:['לא ניתן לאמת זמינות בגלל שגיאת תקשורת','Availability could not be verified due to network errors'],\n    resolution_failed:['לא ניתן להשלים בדיקת תלויות','Dependency resolution could not be completed'],\n    architecture_unverified:['לא אומתה התאמה למחשב','Compatibility with this computer is unverified'],\n    variants_unverified:['נדרשת בדיקת אפשרויות ההתקנה הקיימת','Installed variants need separate review'],\n    excluded:['החבילה או תלות שלה מוחרגת','Package or dependency is excluded'],\n    check_failed:['בדיקת החלופה נכשלה','Alternative check failed'],\n  };\n  return (labels[status]||[status,status])[he?0:1];\n}\nfunction unifiedPlanExpired(state, nowMillis = Date.now()) {\n  if (!state?.unified_plan_id) return false;\n  if (state.unified_plan_stale === true) return true;\n  const expiry = Number(state.unified_plan_expires_at);\n  return !Number.isFinite(expiry) || expiry <= 0 || nowMillis >= expiry * 1000;\n}\nfunction unifiedReasonLabel(code) {\n  const he=i18n.lang==='he';\n  const known={\n    newest_verified_compatible:['העדכון הבינארי החדש ביותר שאומת','Latest verified compatible binary'],\n    partial_provider_comparison:['השוואת הספקים חלקית','Provider comparison is incomplete'],\n    discovery_incomplete:['מידע על חלק מהספקים חסר','Some providers could not be verified'],\n    excluded_package:['חבילה מוחרגת','Excluded package'],\n    no_executable_binary_route:['אין מסלול בינארי בטוח לביצוע','No executable binary route'],\n    recovery_required:['נדרש שחזור עסקה','Transaction recovery required'],\n    plan_expired:['תוכנית העדכון פגה — נדרשת בדיקה חדשה','Update plan expired — run a new check'],\n    ownership_unreadable:['אי אפשר לאמת בעלות','Ownership registry cannot be verified'],\n    already_current:['אין עדכון מאומת','No verified update'],\n  };\n  return (known[code]||[he?'נימוק טכני בפרטים':'Technical reason in details',he?'נימוק טכני בפרטים':'Technical reason in details'])[he?0:1];\n}\nfunction renderMacManager() {\n  let panel=document.getElementById('macManagerPanel');\n  if (!panel) {\n    panel=document.createElement('details');panel.id='macManagerPanel';panel.className='panel';\n    panel.style.padding='16px';panel.style.marginBlock='16px';document.querySelector('.tabs').before(panel);\n  }\n  panel.replaceChildren();const he=i18n.lang==='he';\n  const title=document.createElement('summary');\n  title.textContent=(he?'עדכונים חסומים וחלופות':'Blocked updates and alternatives')+` (${snapshot?.migrations?.length||0})`;\n  panel.appendChild(title);\n  if (unifiedPlanExpired(snapshot)) {\n    const warning=document.createElement('p');\n    warning.textContent=he?'תוכנית העדכון אינה בתוקף. כדי לעדכן חבילות יש לבצע בדיקה חדשה.':'The update plan is no longer valid. Run a new check before updating packages.';\n    warning.setAttribute('role','status');\n    panel.appendChild(warning);\n    const rescan=document.createElement('button');\n    rescan.textContent=he?'בדוק עדכונים מחדש':'Check for updates again';\n    rescan.disabled=!!busyLoading||!!lastKnownOperationRunning;\n    rescan.onclick=()=>checkNow();\n    panel.appendChild(rescan);\n  }\n  const hint=document.createElement('p');\n  hint.textContent=he?'עדכון חבילה יוצג רק עם נתיב בינארי מאומת. פעולות מעבר שאינן מוכנות חסומות.':'An update is executable only with a verified binary route. Unfinished migration paths remain blocked.';\n  for (const decision of snapshot?.update_decisions||[]) {\n    if (decision.status==='current') continue;\n    const line=document.createElement('p');\n    const route=decision.current_provider===decision.target_provider?decision.current_provider:`${decision.current_provider} → ${decision.target_provider}`;\n    line.textContent=`${decision.logical_id}: ${decision.installed_version||'—'} → ${decision.target_version||'—'} · ${route} · ${decision.status} · ${(decision.reason_codes||[]).map(unifiedReasonLabel).join(' · ')}`;\n    panel.appendChild(line);\n  }\n  panel.appendChild(hint);\n  const labels={fresh:he?'עדכני':'current',refreshed:he?'רוענן אוטומטית':'refreshed automatically',stale:he?'ירוענן בבדיקה הבאה':'will refresh on next check',awaiting_authorization:he?'הרענון ממתין להזדהות; לחץ התחל בדיקה':'refresh awaits authorization; press Start Check',refresh_failed:he?'הרענון נכשל או לא אושר; התחל בדיקה ינסה שוב':'refresh failed or was declined; Start Check will retry',not_installed:he?'MacPorts אינו מותקן':'MacPorts is not installed'};\n  for(const [name,provider] of Object.entries(snapshot?.providers||{})) {\n    const status=document.createElement('p');\n    status.textContent=`${name}: ${provider.ok?'✓':'⚠'} · ${provider.checked_at||'—'}`;\n    if(provider.catalog)status.textContent+=' · '+(labels[provider.catalog.status]||provider.catalog.status);\n    if(provider.error)status.title=provider.error;\n    panel.appendChild(status);\n  }\n  if(['running','needs_attention'].includes(snapshot?.migration_journal?.status)) {\n    const warning=document.createElement('p');warning.textContent=he?'מעבר קודם דורש בדיקה לפני המשך.':'A previous migration needs review before continuing.';panel.appendChild(warning);\n  }\n  for (const pending of snapshot?.pending_recovery || []) {\n    const item=document.createElement('div');\n    item.style.marginBlock='12px';\n    const description=document.createElement('p');\n    description.textContent=(he?'נדרש טיפול בעסקה: ':'Recovery required: ')+\n      (pending.logical_id||'—')+' · '+(pending.stage||'—');\n    item.appendChild(description);\n    for (const action of ['resume','rollback']) {\n      const button=document.createElement('button');\n      button.textContent=action==='resume'?(he?'סקירת המשך':'Review resume'):\n        (he?'סקירת שחזור':'Review rollback');\n      button.disabled=!!busyLoading||!!lastKnownOperationRunning;\n      button.onclick=async()=>{\n        button.disabled=true;\n        try {\n          const response=await window.brewApp.migrationRecoveryPlan(pending.transaction_id,action);\n          const plan=response.plan;\n          if (!plan || !plan.id) throw new Error('Recovery plan unavailable');\n          const review=document.createElement('p');\n          review.textContent=JSON.stringify(plan.preview||{},null,2);\n          review.style.whiteSpace='pre-wrap';\n          item.appendChild(review);\n          const approve=document.createElement('button');\n          approve.textContent=he?'אשר את פעולת ההתאוששות':'Approve this recovery';\n          approve.onclick=async()=>{\n            approve.disabled=true;\n            setLoading(true,'message.loadingState');\n            beginOperationRequest();\n            const generation=operationRequestGeneration;\n            let result=null;\n            startProgressPolling();\n            try {\n              result=await window.brewApp.migrationApprove(plan.id);\n              await loadState({showLoader:false});\n            } catch(error) {\n              showMessage(error.message,true);\n            } finally {\n              await finishOperationRequest(result,generation);\n              setLoading(false,'message.loadingState');\n            }\n          };\n          item.appendChild(approve);\n        } catch(error) {\n          showMessage(error.message,true);\n        } finally {\n          button.disabled=false;\n        }\n      };\n      item.appendChild(button);\n    }\n    panel.appendChild(item);\n  }\n  for(const offer of snapshot?.migrations||[]) {\n    const row=document.createElement('details');row.style.marginBlock='10px';\n    const summary=document.createElement('summary');\n    summary.textContent=`${offer.name} — ${macAlternativeLabel(offer.status)}`;\n    if(offer.target&&offer.installed_version)summary.textContent+=` · ${offer.installed_version} → ${offer.target}`;\n    row.appendChild(summary);\n    const detail=document.createElement('p');\n    detail.textContent=(offer.ports?.length?`${offer.ports.join(', ')} · `:'')+(offer.detail||'');\n    if(offer.checked_at)detail.textContent+=' · '+(he?'נבדק: ':'Checked: ')+offer.checked_at;\n    row.appendChild(detail);\n    const classification = offer.classification || {category:'unclassified'};\n    const installation=(snapshot?.packages||[]).find(p=>p.provider==='homebrew'&&p.kind==='formula'&&p.name===offer.name)?.inventory_verification;\n    if(installation) {\n      const evidence=document.createElement('details');const title=document.createElement('summary');\n      title.textContent=he?'ראיות ההתקנה':'Installation evidence';\n      const text=document.createElement('p');text.style.whiteSpace='pre-wrap';\n      text.textContent=(he?'גרסה לפי ההתקנה הפעילה: ':'Active installation version: ')+installation.package_version+'\\n'+\n        (he?'גרסאות שדווחו: ':'Manager versions: ')+(installation.manager_versions||[]).join(', ')+'\\n'+\n        (installation.active_keg||'')+'\\n'+(installation.command_resolution?.resolved_path||'')+'\\n'+\n        [...(installation.issues||[]),...(installation.warnings||[])].join(' · ');\n      if(installation.version_probe?.code)text.textContent+='\\n'+installation.version_probe.output;\n      evidence.append(title,text);row.appendChild(evidence);\n    }\n    const classificationLabels = {\n      service:['שירות עם הגדרות ונתונים','Service with configuration and data'],\n      shared_environment:['ספרייה או סביבת פיתוח משותפת','Shared library or development environment'],\n      standalone_candidate:['כלי עצמאי מועמד לאוטומציה','Standalone automation candidate'],\n      insufficient_information:['חסר מידע לסיווג','Insufficient classification evidence'],\n      unclassified:['טרם סווג','Not yet classified'],\n    };\n    const classificationText=document.createElement('p');\n    classificationText.className='migration-classification';\n    classificationText.textContent=(he?'סיווג: ':'Classification: ')+(classificationLabels[classification.category]||classificationLabels.unclassified)[he?0:1];\n    row.appendChild(classificationText);\n    const reasons=document.createElement('p');\n    reasons.textContent=(classification.reasons||[]).map(migrationReasonLabel).join(' · ');\n    if(classification.checked_at) reasons.textContent+=' · '+classification.checked_at;\n    row.appendChild(reasons);\n    if(classification.consumers?.length) {\n      const consumers=document.createElement('p');consumers.textContent=(he?'תלויים מתועדים: ':'Documented consumers: ')+classification.consumers.map(c=>c.name).join(', ');row.appendChild(consumers);\n    }\n    for(const [label,values] of [[he?'מידע חסר':'Missing evidence',classification.gaps],[he?'קובצי ראיות':'Evidence files',classification.evidence_refs]]) {\n      if(values?.length) {const evidence=document.createElement('details');const title=document.createElement('summary');title.textContent=label;const text=document.createElement('p');text.textContent=values.join(' · ');evidence.append(title,text);row.appendChild(evidence);}\n    }\n    if(!snapshot?.unified_plan_id && offer.status==='binary_available') {\n      if(offer.below_homebrew_target) {\n        const version=document.createElement('p');\n        version.textContent=he?`זו גרסה חדשה מהמותקנת אצלך. יעד Homebrew הוא ${offer.homebrew_target}; אין בכך הוכחה שזו הגרסה האחרונה הנתמכת ב־Intel.`:`This is newer than your installed version. Homebrew targets ${offer.homebrew_target}; this does not prove it is the last Intel-supported release.`;\n        row.appendChild(version);\n      }\n      const availability=document.createElement('p');\n      availability.textContent=he?'נבדקה זמינות קבצים לכל התלויות. חתימות ותפקוד יאומתו רק בהתקנה.':'Archive availability was checked for all dependencies. Signatures and functionality are verified during installation.';row.appendChild(availability);\n      if(!offer.executable) {\n        const blocked=document.createElement('p');blocked.textContent=classification.category==='standalone_candidate'?(he?'מנגנון המעבר לכלי זה טרם מומש.':'The migration mechanism for this tool is not implemented yet.'):(he?'המעבר דורש טיפול בצרכנים, בנתונים או במידע החסר; אינו מבוצע בעדכון רגיל.':'Migration needs consumer, data or missing-evidence review; regular updates do not perform it.');row.appendChild(blocked);\n      } else {\n        const review=document.createElement('button');review.textContent=he?'סקירת תוכנית המעבר':'Review migration plan';\n        review.disabled=!!busyLoading||!!lastKnownOperationRunning;\n        review.onclick=async()=>{\n          review.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();\n          try {\n            const {plan}=await window.brewApp.migrationPlan(offer.name);\n            const contents=document.createElement('p');\n            contents.textContent=`${plan.installed_version} → ${plan.target} · ${plan.graph.nodes.map(n=>n.name).join(', ')} · ${plan.paths.join(', ')} · ${he?'צרכנים':'Consumers'}: ${plan.consumers.join(', ')||'—'}`;row.appendChild(contents);\n            const approve=document.createElement('button');approve.textContent=he?'אישור מעבר חד־פעמי':'Approve this migration once';\n            approve.onclick=async()=>{\n              if(!confirm(he?'לאשר התקנה, בדיקת שימוש והסרת העותק הקודם רק אם אין לו תלויים?':'Approve installation, verification, and removal of the old copy only if unused?'))return;\n              approve.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();\n              try {await window.brewApp.migrationApprove(plan.id);const response=await window.brewApp.getState();snapshot=response.snapshot||response;renderCards(snapshot);renderTable();}\n              catch(error){showMessage(error.message,true);}\n              finally{stopProgressPolling();setLoading(false,'message.loadingState');approve.disabled=false;}\n            };row.appendChild(approve);\n          }catch(error){showMessage(error.message,true);}\n          finally{stopProgressPolling();setLoading(false,'message.loadingState');review.disabled=false;}\n        };row.appendChild(review);\n      }\n    }\n    panel.appendChild(row);\n  }\n}\n",
}

UNIFIED_EXISTING_BLOBS = {
    "docs/PRODUCT_GOAL.md": "a898c850eb8e84793940b6cdd3b3549845758b77",
    "docs/INTEL_UPDATE_IMPLEMENTATION_PLAN.md": "cacc92986ca8816400464cc9968a3dc7d44cc3c0",
    ".github/workflows/ci.yml": "e17b64678c84613fc87bf8c9c189655c620a6ba9",
    "docs/MAC_MANAGER_TASKS.md": "6dc9563527293ab7c7910d4b8057778171f104c6",
    "docs/MAC_MANAGER.md": "2c425e910834fc08f283214442a71896317a2ea0",
    "backend/update_routes.py": "7b017fffa6627c6321187396cea81e57e7c245ca",
    "tools/verify-current-code.py": "766265930f4342bcf54b434053510d78d51d3e45",
    "tools/verify-migration.py": "e9df93e719efe755d8acaf03087e3a413f02145c",
    "package.json": "53ff834648c07b47a509bd727e0396aa94598d24",
    "package-lock.json": "9e05af589baffcb614f7b25e05b27f41b72f8e5a",
    "backend/macports_provider.py": "836d1850b33c5f0820cd720db7e9cfd204f6b8e3",
    "backend/migration_engine.py": "22aa1d285bc3486e66d13dd6df24bf774cef441f",
    "backend/migration_profiles.py": "7e70f06c7d553d099c5847d41789836a2c357abf",
    "tests/test_migration_profiles.py": "4e1ea80ee21ba60c344c35ecdc7285e28fac9439",
    "renderer/app.js": "5f0952f4d9a5de44bd7a55111a46f6ac7c87e2b2",
    "backend/brew-updates-service.py": "fa29ff482a58a7b96231db8d176bc4dc438191cb",
    "backend/mac_manager.py": "6a34b0e3ddebd771d3d2199a26594274efb5620e",
    "electron/main.js": "2836438fee8de455747831085cd449275baa5d07",
    "electron/preload.js": "52b1b04ab13f9a87729c0e34490eac3365f4a604",
    "renderer/mac-manager.js": "8c6e2be51e01b1348cea813b2e33e9b3a0638606",
}

UNIFIED_ROUTING = r'''"""Protected all-or-nothing command routing for native Intel migrations.

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
            _atomic_bytes(shim, content)
            written[name] = {"prior": prior.hex() if prior is not None else None,
                             "after_sha256": hashlib.sha256(content).hexdigest()}
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
        _revert(root, active, before_pointer, written)
        raise


def _revert(root, active, previous_pointer, written):
    # If an independent actor changed a generated shim, never overwrite it.
    for name, item in written.items():
        path = Path(root) / "bin" / name
        if not path.is_file() or digest(path) != item["after_sha256"]:
            raise RoutingConflict("Cannot restore changed shim: " + name)
    if previous_pointer is None:
        if active.is_symlink():
            active.unlink()
    else:
        _atomic_link(active, previous_pointer)
    for name, item in written.items():
        path = Path(root) / "bin" / name
        if item["prior"] is None:
            path.unlink()
        else:
            _atomic_bytes(path, bytes.fromhex(item["prior"]))


def restore(root, activation):
    active = Path(activation["active"])
    if not active.is_symlink():
        raise RoutingConflict("Missing active pointer")
    current = os.readlink(active)
    if Path(current).resolve() != Path(activation["target"]).resolve():
        raise RoutingConflict("Active route was changed independently")
    _revert(root, active, activation["previous_pointer"], activation["shims"])
    return {"restored": True, "source": activation["source"]}


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
'''

UNIFIED_ROUTING_TESTS = r'''import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import routing_activation as route


class RoutingTests(unittest.TestCase):
    def setup_dirs(self, root):
        binpath = Path(root) / "bin"
        binpath.mkdir()
        a = binpath / "tool"
        a.write_text("#!/bin/sh\nexit 0\n")
        a.chmod(0o755)
        b = binpath / "replacement"
        b.write_text("#!/bin/sh\nexit 0\n")
        b.chmod(0o755)
        return a, b

    def test_unmanaged_file_and_broken_link_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b = self.setup_dirs(temp)
            with self.assertRaises(route.RoutingConflict):
                route.preflight(temp, "jq", {"tool": str(a)})
            a.unlink()
            a.symlink_to("/no/such/path")
            with self.assertRaises(route.RoutingConflict):
                route.preflight(temp, "jq", {"tool": str(b)})

    def test_all_commands_switch_atomically_and_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b = self.setup_dirs(temp)
            shim = Path(temp)/"bin"/"newshim"
            out = route.activate(temp, "jq", "tx1", {"newshim":str(a)},
                                 {"newshim":str(b)})
            self.assertTrue(shim.is_file())
            self.assertEqual(os.readlink(out["active"]), out["target"])
            route.restore(temp,out)
            self.assertFalse(os.path.lexists(shim))
            self.assertFalse(os.path.lexists(out["active"]))
            self.assertTrue(a.is_file())

    def test_independent_edit_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b = self.setup_dirs(temp)
            out = route.activate(temp, "jq", "tx2", {"newshim":str(a)},
                                 {"newshim":str(b)})
            shim = Path(temp)/"bin"/"newshim"
            shim.write_text("independent edit")
            with self.assertRaises(route.RoutingConflict):
                route.restore(temp,out)
            self.assertEqual(shim.read_text(), "independent edit")


if __name__ == "__main__":
    unittest.main()
'''

UNIFIED_PROFILE_CODE = r'''"""Fixed, profile-bound probes for standalone Intel migration targets."""
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
'''

UNIFIED_PROFILE_TESTS = r'''import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import migration_profiles as p

class Profiles(unittest.TestCase):
    def test_two_fixed_profiles_and_commands(self):
        self.assertEqual(set(p.PROFILES), {"terminal-notifier", "include-what-you-use"})
        self.assertEqual(p.get("include-what-you-use").commands,
                         ("include-what-you-use", "iwyu_tool.py", "fix_includes.py"))
    def test_arm_or_rosetta_rejected(self):
        with patch("migration_profiles.platform.system", return_value="Darwin"),\
             patch("migration_profiles.platform.machine", return_value="arm64"):
            with self.assertRaises(ValueError): p.intel_only()
        with patch("migration_profiles.platform.system", return_value="Darwin"),\
             patch("migration_profiles.platform.machine", return_value="x86_64"),\
             patch.dict("migration_profiles.os.environ", {"SYSCTL_TRANSLATED":"1"}):
            with self.assertRaises(ValueError): p.intel_only()
    def test_version_parser_not_fooled_by_minor_digits(self):
        self.assertTrue(p.version_matches("IWYU 0.24", "0.24"))
        self.assertFalse(p.version_matches("IWYU 0.241", "0.24"))
    def test_paths_use_real_port_location(self):
        self.assertTrue(all(str(v).startswith("/opt/local/bin/")
                            for v in p.command_paths(p.get("include-what-you-use")).values()))

if __name__=="__main__": unittest.main()
'''

UNIFIED_ENGINE = r'''"""Journalled Intel Homebrew->MacPorts migrations with guarded recovery.

No source removal is attempted until the source is backed up, binary graph and
smoke probes have passed, and owned command routing has been verified.
Unresolved transactions never automatically resume on process startup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

import installation_evidence
import macports_provider as mp
import migration_profiles as profiles
import provider_ownership as ownership
import routing_activation as routing
from update_selection import compare_upstream, material_fingerprint

SCHEMA = 2
FINISHED = {"completed", "rolled_back", "canceled"}


class MigrationConflict(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code


def _root(s):
    return Path(s.DATA_DIR) / "migrations"


def _plan_path(s, plan_id):
    return _root(s) / "plans" / (plan_id + ".json")


def _transaction_path(s, transaction_id):
    return _root(s) / "transactions" / transaction_id


def _write(path, data):
    ownership.atomic_write_json(path, data)


def _read(s, path):
    return s.read_json(path)


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _check_id(value):
    if not isinstance(value, str) or len(value) != 32 or any(
            char not in "0123456789abcdef" for char in value):
        raise MigrationConflict("invalid_identifier")
    return value


def _source(s, name):
    inventory = s.fetch_installed_inventory(s.fetch_outdated_index())
    rows = [r for r in inventory if r.get("name") == name
            and r.get("kind") == "formula"
            and r.get("brew_installation_present") is not False]
    if len(rows) != 1:
        raise MigrationConflict("source_installation_ambiguous")
    row = rows[0]
    audit = row.get("inventory_verification") or {}
    keg = Path(audit.get("active_keg") or "/nonexistent")
    receipt = keg / "INSTALL_RECEIPT.json"
    if (audit.get("status") != "verified" or not keg.is_dir() or
            keg.is_symlink() or not receipt.is_file() or
            len(audit.get("manager_versions") or []) != 1):
        raise MigrationConflict("source_evidence_unverified")
    return row, audit, keg, receipt


def _source_commands(keg, profile):
    commands = {}
    for command in profile.commands:
        path = keg / "bin" / command
        if not path.is_file():
            raise MigrationConflict("source_command_missing:" + command)
        commands[command] = str(path)
    return commands


def _fresh_classification(s, name, row, *, provided=None):
    """Validate the same T02 classification against a fresh source audit."""
    if provided is None:
        state = s.read_json(s.STATE_FILE) or {}
        entries = [x for x in state.get("packages", [])
                   if x.get("provider") == "homebrew"
                   and x.get("kind") == "formula" and x.get("name") == name]
        provided = entries[0].get("classification") if len(entries) == 1 else None
        if provided is None:
            offer = next((x for x in state.get("migrations", [])
                          if x.get("name") == name), None)
            provided = (offer or {}).get("classification")
    cls = provided if isinstance(provided, dict) else {}
    audit = row.get("inventory_verification") or {}
    if (cls.get("category") != "standalone_candidate" or cls.get("gaps")
            or cls.get("consumers") or cls.get("shared_roles")
            or cls.get("inventory_fingerprint") != installation_evidence.fingerprint(audit)):
        raise MigrationConflict("standalone_classification_blocked")
    checked = cls.get("checked_at")
    if not isinstance(checked, str):
        raise MigrationConflict("classification_timestamp_missing")
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(checked.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timezone is required")
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
    except (ValueError, TypeError) as error:
        raise MigrationConflict("classification_timestamp_invalid") from error
    if age < -60 or age >= 900:
        raise MigrationConflict("classification_stale")
    return cls

def _consumers(s, name):
    check = s.run_cmd([s.brew_bin(), "uses", "--installed", name],
                      timeout=90, cancellable=True)
    if check.code:
        raise MigrationConflict("consumer_check_failed", check.stderr or check.stdout)
    items = [x.strip() for x in check.stdout.splitlines() if x.strip()]
    if items:
        raise MigrationConflict("source_has_consumers", ", ".join(items))
    return items


def _target_graph(s, profile):
    graph = mp.preflight(s, list(profile.ports))
    nodes = graph.get("nodes") or []
    root = next((x for x in nodes if x.get("name") == profile.ports[0]), None)
    if root is None:
        raise MigrationConflict("target_graph_missing_root")
    return graph, root


def _route_ownership(s, name):
    registry = ownership.load_routes(s.TOOLS_ROOT)
    if registry.get("error"):
        raise MigrationConflict("route_registry_unverified")
    if any(x.get("logical_id") == name for x in registry.get("legacy", [])):
        raise MigrationConflict("legacy_route_requires_review")
    prior = next((x for x in registry["routes"] if x.get("logical_id") == name
                  and x.get("status") == "committed"), None)
    return prior, registry


def _material(s, name, *, classification=None):
    profiles.intel_only()
    if name == "ngrok" or s.is_excluded(name, "formula"):
        raise MigrationConflict("package_excluded")
    profile = profiles.get(name)
    if not profile or profile.provider != "macports":
        raise MigrationConflict("unsupported_migration_profile")
    row, audit, keg, receipt = _source(s, name)
    cls = _fresh_classification(s, name, row, provided=classification)
    _consumers(s, name)
    graph, target = _target_graph(s, profile)
    if compare_upstream(target["version"], row["installed_version"]) != 1:
        raise MigrationConflict("migration_downgrade_or_unknown")
    existing = {x["name"]: x for x in mp.inventory(s)}
    for node in graph["nodes"]:
        old = existing.get(node["name"])
        if old:
            expected = str(node["version"]) + (
                "_" + str(node["revision"]) if node.get("revision") not in ("", "0", None)
                else "")
            if old["installed_version"] != expected or old.get("installed_variants", "") != node.get("variants", ""):
                raise MigrationConflict("existing_port_variant_or_version_conflict:" + node["name"])
    prior, registry = _route_ownership(s, name)
    source_commands = _source_commands(keg, profile)
    target_commands = {key: str(path) for key, path in profiles.command_paths(profile).items()}
    owned_shims = (prior or {}).get("owned_shims", {})
    routing.preflight(s.TOOLS_ROOT, name, target_commands, owned_shims=owned_shims)
    source = {
        "name": name, "kind": "formula", "provider": "homebrew",
        "installation_id": "homebrew:formula:" + name,
        "version": row["installed_version"], "keg": str(keg),
        "receipt_sha256": _hash(receipt),
        "manager_versions": audit.get("manager_versions"),
        "commands": source_commands,
    }
    target_data = {
        "provider": "macports", "installation_id": "macports:" + profile.ports[0],
        "version": target["version"], "revision": target.get("revision"),
        "graph": graph["nodes"], "commands": target_commands,
        "ports": list(profile.ports),
    }
    core = {"source": source, "target": target_data,
            "classification": cls, "consumers": [], "profile": name,
            "profile_version": profile.version,
            "route_generation": (prior or {}).get("generation", 0),
            "owned_shims": owned_shims, "system": graph.get("system"),
            "exclusions": s.load_settings().get("excluded_packages", [])}
    return core, material_fingerprint(source, core, prior or {})


def plan(s, name, action="migrate", transaction_id=None, *, classification=None):
    if action != "migrate":
        return recovery_plan(s, transaction_id, action)
    core, fingerprint = _material(s, name, classification=classification)
    plan_id = uuid.uuid4().hex
    created = time.time()
    data = {"schema_version": SCHEMA, "id": plan_id,
            "action": "migrate", "created_at": created,
            "expires_at": created + 900,
            "fingerprint": fingerprint, **core}
    _write(_plan_path(s, plan_id), data)
    return data


def _load_plan(s, plan_id):
    plan_id = _check_id(plan_id)
    record = _read(s, _plan_path(s, plan_id))
    if not record or record.get("schema_version") != SCHEMA:
        raise MigrationConflict("migration_plan_not_found")
    if record["expires_at"] < time.time():
        raise MigrationConflict("migration_plan_expired")
    return record


def _manifest(root):
    result = []
    root = Path(root)
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        mode = path.lstat().st_mode
        kind = "link" if stat.S_ISLNK(mode) else "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "special"
        if kind == "special":
            raise MigrationConflict("unsupported_source_file_type:" + name)
        item = {"path": name, "kind": kind, "mode": stat.S_IMODE(mode)}
        if kind == "link":
            item["target"] = os.readlink(path)
        if kind == "file":
            item["sha256"] = _hash(path)
        result.append(item)
    return result


def _backup(s, journal, directory):
    source = Path(journal["plan"]["source"]["keg"])
    if not source.is_dir():
        raise MigrationConflict("source_keg_disappeared")
    target = directory / "backup" / "keg"
    if target.exists():
        raise MigrationConflict("backup_already_exists")
    initial = _manifest(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
    if _manifest(target) != initial:
        raise MigrationConflict("backup_integrity_failed")
    manifest_path = directory / "backup" / "manifest.json"
    _write(manifest_path, {"entries": initial, "source": str(source)})
    journal["backup"] = {"path": str(target), "manifest": str(manifest_path),
                         "manifest_sha256": _hash(manifest_path)}
    _write(directory / "journal.json", journal)


def _phase(journal, directory, phase, state, **values):
    if state not in ("started", "finished"):
        raise ValueError("Invalid phase status")
    journal["stage"] = phase
    journal.setdefault("phases", {})[phase] = {
        "status": state, "timestamp": time.time(), **values}
    journal["updated_at"] = time.time()
    _write(directory / "journal.json", journal)


def _verify_backup(journal):
    backup = journal.get("backup") or {}
    manifest_path = Path(backup.get("manifest") or "/nonexistent")
    saved = Path(backup.get("path") or "/nonexistent")
    if not manifest_path.is_file() or _hash(manifest_path) != backup.get("manifest_sha256"):
        raise MigrationConflict("backup_manifest_changed")
    data = json.loads(manifest_path.read_text())
    if not saved.is_dir() or _manifest(saved) != data["entries"]:
        raise MigrationConflict("backup_contents_changed")
    return saved, data["entries"]


def _target_ready(s, plan):
    profile = profiles.get(plan["profile"])
    current = {p["name"]: p for p in mp.inventory(s)}
    root = current.get(profile.ports[0])
    if not root:
        raise MigrationConflict("target_installation_missing")
    expected = plan["target"]["version"]
    observed = root["installed_version"]
    expected_revision = str(plan["target"].get("revision") or "0")
    wanted = expected + ("_" + expected_revision if expected_revision != "0" else "")
    if (observed != wanted or root.get("installed_variants", "") !=
            next((n.get("variants", "") for n in plan["target"]["graph"]
                  if n["name"] == profile.ports[0]), "")):
        raise MigrationConflict("target_version_or_variant_mismatch")
    smoke = profiles.smoke(s, profile, expected)
    return smoke


def _source_still_safe(s, plan):
    name = plan["source"]["name"]
    current, _, keg, receipt = _source(s, name)
    if str(keg) != plan["source"]["keg"] or _hash(receipt) != plan["source"]["receipt_sha256"]:
        raise MigrationConflict("source_installation_changed")
    _consumers(s, name)
    if s.is_excluded(name, "formula"):
        raise MigrationConflict("package_excluded")
    return current


def _target_postcheck(s, journal):
    target = journal["plan"]["target"]
    name = journal["plan"]["source"]["name"]
    activation = journal.get("activation") or {}
    if not activation:
        raise MigrationConflict("routing_not_activated")
    routing.verify_shell(s, s.TOOLS_ROOT, target["commands"])
    smoke = _target_ready(s, journal["plan"])
    primary = name
    managed = Path(s.TOOLS_ROOT) / "bin" / primary
    result = s.run_cmd([str(managed), *profiles.get(name).version_args],
                       timeout=30, cancellable=True)
    if result.code or not profiles.version_matches(result.stdout + result.stderr,
                                                    target["version"]):
        raise MigrationConflict("managed_command_postcheck_failed")
    return smoke


def _compensate(s, journal, directory):
    """Compensate without observing the original cancel flag."""
    activation = journal.get("activation")
    if activation:
        routing.restore(s.TOOLS_ROOT, activation)
        journal["activation_rolled_back"] = True
    source = Path(journal["plan"]["source"]["keg"])
    if not source.is_dir():
        backup, _ = _verify_backup(journal)
        if os.path.lexists(source):
            raise MigrationConflict("source_path_changed_independently")
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(backup, source, symlinks=True, copy_function=shutil.copy2)
        if _manifest(source) != _manifest(backup):
            raise MigrationConflict("restored_source_hash_mismatch")
        linked = s.run_cmd([s.brew_bin(), "link", journal["plan"]["source"]["name"]],
                           timeout=180, cancellable=False,
                           env_override={"HOMEBREW_NO_AUTO_UPDATE": "1",
                                         "HOMEBREW_NO_AUTOREMOVE": "1",
                                         "HOMEBREW_NO_INSTALL_CLEANUP": "1"})
        if linked.code:
            raise MigrationConflict("source_relink_failed", linked.stderr)
    return True


def _run(s, journal, directory, *, resume=False):
    plan_data = journal["plan"]
    profile = profiles.get(plan_data["profile"])
    try:
        if not resume:
            _phase(journal, directory, "backup", "started")
            _backup(s, journal, directory)
            _phase(journal, directory, "backup", "finished")
            _phase(journal, directory, "install", "started")
            result = mp.privileged(s, "install", plan_data["target"]["ports"])
            if result.code:
                raise MigrationConflict("target_install_failed", result.stderr or result.stdout)
            _phase(journal, directory, "install", "finished", code=result.code)
        else:
            _verify_backup(journal)
        _phase(journal, directory, "verify", "started")
        journal["smoke"] = _target_ready(s, plan_data)
        _phase(journal, directory, "verify", "finished")
        if not journal.get("activation"):
            _source_still_safe(s, plan_data)
            _phase(journal, directory, "activate", "started")
            def routing_progress(state):
                journal["activation_pending"] = state
                _write(directory / "journal.json", journal)
            journal["activation"] = routing.activate(
                s.TOOLS_ROOT, profile.name, journal["transaction_id"],
                plan_data["source"]["commands"], plan_data["target"]["commands"],
                owned_shims=plan_data.get("owned_shims"),
                progress=routing_progress)
            journal.pop("activation_pending", None)
            _phase(journal, directory, "activate", "finished")
        _phase(journal, directory, "cleanup", "started")
        source_keg = Path(plan_data["source"]["keg"])
        if source_keg.is_dir():
            _source_still_safe(s, plan_data)
            routing.verify_shell(s, s.TOOLS_ROOT, plan_data["target"]["commands"])
            _verify_backup(journal)
            result = s.run_cmd(
                [s.brew_bin(), "uninstall", "--formula", profile.name],
                timeout=300, cancellable=True,
                env_override={"HOMEBREW_NO_AUTO_UPDATE": "1",
                              "HOMEBREW_NO_AUTOREMOVE": "1",
                              "HOMEBREW_NO_INSTALL_CLEANUP": "1"})
            if result.code:
                raise MigrationConflict("source_uninstall_failed", result.stderr or result.stdout)
        _phase(journal, directory, "cleanup", "finished")
        _phase(journal, directory, "postcheck", "started")
        journal["postcheck"] = _target_postcheck(s, journal)
        _phase(journal, directory, "postcheck", "finished")
        _phase(journal, directory, "ownership", "started")
        route = {
            "logical_id": profile.name, "status": "committed",
            "source_installation_id": plan_data["source"]["installation_id"],
            "source_name": profile.name,
            "target_provider": "macports",
            "target_installation_id": plan_data["target"]["installation_id"],
            "profile_id": profile.name, "profile_version": profile.version,
            "transaction_id": journal["transaction_id"],
            "owned_shims": routing.owned_shim_hashes(s.TOOLS_ROOT, journal["activation"]),
            "active": journal["activation"]["active"],
            "evidence_path": str(directory / "journal.json"),
        }
        ownership.commit_route(s.TOOLS_ROOT, route, directory / "journal.json",
                               mp.inventory(s))
        _phase(journal, directory, "ownership", "finished")
        journal.update(status="completed", completed_at=time.time())
        _phase(journal, directory, "completed", "finished")
        return journal
    except BaseException as error:
        journal.update(status="needs_attention", error=str(error))
        _write(directory / "journal.json", journal)
        # Do not automatically destroy a potentially changed source after cleanup.
        # Before cleanup, compensate only ownership-controlled route files.
        if journal.get("stage") in ("backup", "install", "verify", "activate"):
            try:
                activation = journal.get("activation")
                if not activation:
                    pending = journal.get("activation_pending") or {}
                    if pending.get("state") == "pointer_switch_pending":
                        activation = pending
                if activation:
                    routing.restore(s.TOOLS_ROOT, activation)
                    journal["activation_rolled_back"] = True
                elif journal.get("activation_pending"):
                    raise MigrationConflict("activation_interrupted_needs_manual_recovery")
                journal["status"] = "rolled_back"
            except BaseException as rollback:
                journal["compensation_error"] = str(rollback)
                journal["status"] = "needs_attention"
            _write(directory / "journal.json", journal)
        raise


def approve(s, plan_id, request_id=None):
    old = _load_plan(s, plan_id)
    if old.get("action") in ("resume", "rollback"):
        return approve_recovery(s, old)
    if old.get("action") != "migrate":
        raise MigrationConflict("unsupported_migration_action")
    fresh, fingerprint = _material(s, old["profile"])
    if fingerprint != old.get("fingerprint"):
        raise MigrationConflict("plan_changed")
    tid = uuid.uuid4().hex
    directory = _transaction_path(s, tid)
    directory.mkdir(parents=True)
    journal = {"schema_version": SCHEMA, "transaction_id": tid,
               "plan_id": plan_id, "request_id": request_id,
               "status": "running", "stage": "preflight",
               "started_at": time.time(), "plan": old, "phases": {}}
    _write(directory / "journal.json", journal)
    return _run(s, journal, directory)


def recovery_plan(s, transaction_id, action):
    tid = _check_id(transaction_id)
    if action not in ("resume", "rollback"):
        raise MigrationConflict("unsupported_recovery_action")
    journal = _read(s, _transaction_path(s, tid) / "journal.json")
    if not journal or journal.get("schema_version") != SCHEMA:
        raise MigrationConflict("transaction_not_found")
    if journal.get("status") in FINISHED:
        raise MigrationConflict("transaction_already_resolved")
    source = Path(journal["plan"]["source"]["keg"])
    preview = {"source_present": source.is_dir(),
               "routing_active": bool(journal.get("activation")),
               "last_stage": journal.get("stage"),
               "backup_verified": False}
    try:
        _verify_backup(journal)
        preview["backup_verified"] = True
    except Exception as error:
        preview["backup_error"] = str(error)
    pid = uuid.uuid4().hex
    created = time.time()
    data = {"schema_version": SCHEMA, "id": pid, "action": action,
            "transaction_id": tid, "created_at": created,
            "expires_at": created + 900, "preview": preview,
            "journal_sha256": _hash(_transaction_path(s, tid) / "journal.json")}
    _write(_plan_path(s, pid), data)
    return data


def approve_recovery(s, approved):
    tid = approved["transaction_id"]
    directory = _transaction_path(s, tid)
    journal_path = directory / "journal.json"
    if _hash(journal_path) != approved["journal_sha256"]:
        raise MigrationConflict("recovery_plan_changed")
    journal = _read(s, journal_path)
    _verify_backup(journal)
    if approved["action"] == "rollback":
        try:
            _compensate(s, journal, directory)
            journal.update(status="rolled_back", recovery_completed_at=time.time())
            _write(journal_path, journal)
            return journal
        except Exception as exc:
            journal.update(status="needs_attention", recovery_error=str(exc))
            _write(journal_path, journal)
            raise
    if approved["action"] == "resume":
        return _run(s, journal, directory, resume=True)
    raise MigrationConflict("unsupported_recovery_action")
'''

UNIFIED_ENGINE_TESTS = r'''import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import migration_engine as engine

class MigrationContracts(unittest.TestCase):
    def test_backup_manifest_preserves_file_links_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/"source"
            root.mkdir()
            script = root/"command"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o755)
            (root/"linked").symlink_to("command")
            entries = engine._manifest(root)
            self.assertEqual({e["path"] for e in entries}, {"command", "linked"})
            self.assertEqual(next(e for e in entries if e["path"]=="linked")["target"],"command")
            self.assertEqual(next(e for e in entries if e["path"]=="command")["mode"],0o755)
    def test_recovery_refuses_invalid_ids(self):
        for value in ("../../../etc/passwd", "bad", "", None):
            with self.assertRaises(engine.MigrationConflict):
                engine._check_id(value)
    def test_profile_unknown_fails_closed(self):
        self.assertIsNone(engine.profiles.get("unknown-tool"))
    def test_backup_hash_changes_if_file_mutated(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)/"x"
            p.write_text("first")
            first = engine._hash(p)
            p.write_text("second")
            self.assertNotEqual(first,engine._hash(p))

if __name__=="__main__":unittest.main()
'''

UNIFIED_T04_PLAN = "# T04 — תוכנית ביצוע: גילוי והשוואת ספקי עדכון ל־Mac Intel\n\nתאריך: 20.10.2026. מיושם בענף `feat/unified-intel-134-foundations` בלבד. מבוסס על [התוכנית המחייבת](INTEL_UPDATE_IMPLEMENTATION_PLAN.md), סעיף 7, ועל [דוח המצב](UNIFIED_IMPLEMENTATION_STATUS.md).\n\n## יעד ומסירה\n\nסריקה יחידה מציגה לכל התקנה פיזית מועמדות מהספק הנוכחי ומחלופות מוכרות Homebrew/MacPorts/Pixi גם אם Homebrew אינו מסמן `outdated`. בחירה היא גרסת upstream היציבה והגבוהה ביותר **מבין מסלולים בינאריים מאומתים שניתן לבצע**, ובשוויון שומרים בעלים נוכחי. סטטוס ספק שנכשל נשמר כמידע חסר, ללא המצאת ״העדכני ביותר״. פעולה לא נתמכת נשארת ברשימת חלופות כחסומה.\n\n## סדר צעדי המימוש\n\n1. **איסוף ואמת־מקור**: להוציא איסוף חלופות לפונקציה שניתן לבדוק עם fake service; לאסוף Homebrew גם כשה־outdated=false; MacPorts גם אם אין `offer` ישן ורק למיפוי מאומת; Pixi למיפויי CLI בעלי התאמה גם כשהמקור Homebrew. לשמור זהויות `homebrew:formula`, `macports:`, `pixi:` נפרדות. Cask מקבל חסם מפורש ולא מסלול משוער.\n2. **T02 ו־scope**: עבור כל אפשרות מעבר לבצע סיווג מקור עדכני מתוך `installation_evidence` המשותף, ללא חיפוש פרויקטים פרטיים; לאפשר רק standalone ללא consumers/roles/gaps, וללא טענת התאמה אם הסריקה חלקית. לא להשתמש במיפוי שם או `--force-bottle` לבדם כראיה. להחריג ngrok ותלויות מוחרגות.\n3. **בינאריים/ביצוע**: לשמר preflight Homebrew bottle ו־MacPorts archives, ולגלות Pixi osx-64 עם lock מדויק, provenance, hashes וסגירת תלויות. מסלול מעבר `ready` ידרוש פרופיל מותקן, preflight מאומת, גיבוי, בדיקת שימוש, הפעלה אטומית, cleanup ושחזור; ללא כל אלה `execution_supported=false` והסיבה מוצגת. אין לגזור נתיבי shell ממשתמש או מהרשת.\n4. **אלגוריתם/תצוגה**: לגרום ל־`generate_plan` להזין `update_selection.select` בכל החלופות; לתת `discovery_complete` אמת רק כאשר שלושת המסלולים הרלוונטיים נבדקו או ידוע שאינם רלוונטיים; שגיאה בספק יחיד אינה חוסמת עדכון כשיר מספק אחר. להוסיף `reason_codes` מובחנים ותיאורי מסלול בעברית/אנגלית.\n5. **בדיקות וביקורת**: fixtures לכל מצב (Brew current with newer port, Brew binary available but Port higher, Pixi newer but unsupported, failed provider + valid provider, excluded, duplicate physical install, version suffix, stale plan). בדיקות מבודדות בלבד; CI syntax+tests focused+guarded `unified-check`. לבחון hash generator vs emitted files, תיעוד, היעדר שינויי התקנה אמיתיים, ו־`main` שלא שונה.\n\n## גבולות בטיחות מפורשים\n\n- אין מעבר אוטומטי לספק אחר שאין לו פרופיל עסקה בטוח; מיפוי `PIXI_TOOLS` לבדו אינו הרשאה להסרת Homebrew.\n- כאשר פיילוט מעבר מלא תלוי בתיקוני T03 (recovery/ownership), העבודה תחסום את הביצוע ותציין חסם ממוקד ולא תכריז על T04 סגור.\n- אין להתקין Pixi, MacPorts או Homebrew, להסיר חבילות, לשנות PATH, לפרסם release או להריץ קבלה אמיתית במהלך המימוש.\n- מסמך תוכנית זה אינו דוח הצלחה; דוח הביקורת יתווסף רק עם קומיטים/תוצאות שנבדקו.\n\n## נקודות ביקורת סופיות\n\n- [ ] סריקה אוספת לפחות את כל הספקים **הרלוונטיים** לכל התקנה שמיפויה מוכר, ללא תלות ב־Brew outdated.\n- [ ] החלופות כוללות גם חסמים מפורטים, ואין ״ready״ ללא adapter ועסקה תואמת.\n- [ ] מצבים של שוויון/כשל ספק/שתי התקנות/unknown version/מניעת downgrade נבדקו.\n- [ ] generated files תואמים ל־generator ובדיקת CI ממוקדת עברה.\n- [ ] נרשמו בבירור מסלולים שעוד אינם מבצעים מעבר ודרישות בדיקת הקבלה.\n"
UNIFIED_PRODUCT_GOAL_CURRENT = "# מטרת המוצר והחלטות העבודה\n\n> **עדכון מצב 20.10.2026:** המטרה במסמך אינה דוח ביצוע. [דוח המימוש](UNIFIED_IMPLEMENTATION_STATUS.md) מעדכן את היסטוריית 1.3.3 שלמטה: בענף 1.3.4 קיימים מנגנוני החלטות ו־API/UI, אך עדיין אין גילוי מלא או מעבר Homebrew→Pixi כללי.\n\nעודכן: 20.10.2026, בעקבות הבהרת המשתמש. זהו מקור ההחלטות למימוש הבא.\nמסמך זה מתאר את היעד; מצב המימוש נמצא ב־[מסמך המשימות](MAC_MANAGER_TASKS.md).\nאם תכנון קודם דורש אישור מעבר נפרד לכל חבילה או בדיקות מלאות בידי הסוכן,\nההחלטות במסמך זה מחליפות אותו.\n\n## חוויית השימוש הנדרשת\n\n1. המשתמש לוחץ ״התחל בדיקה״.\n2. המערכת מזהה התקנות ועדכונים ובודקת את הספקים הרלוונטיים.\n3. לכל חבילה היא בוחרת מסלול עם חבילה מוכנה וכל תלויותיה, המתאים לגרסת\n   macOS ול־Mac Intel, עם הגרסה העדכנית ביותר מבין המסלולים הנתמכים שנבדקו.\n4. התוצאות מציגות גרסה מותקנת, גרסת יעד, ספק נוכחי, ספק מבצע וסיבה לבחירה.\n   מעבר ספק מתוכנן מוצג כחלק מהעדכון לפני הלחיצה.\n5. המשתמש בוחר הכול או חלק ולוחץ ״עדכן הכול״ או ״עדכן נבחרים״.\n   הלחיצה מאשרת את הפעולות המוצגות, כולל המעברים; אין אשף או אישור נוסף\n   לכל חבילה. דרישת סיסמת מנהל מערכת של macOS עשויה להישאר כשנחוצה.\n6. המערכת מתקינה, מאמתת ומסכמת מה עודכן, מה דולג ומה נכשל, עם הסיבה.\n7. מעבר מוצלח נשמר: בסריקה הבאה ובפתיחה מחדש החבילה מזוהה אצל הספק החדש,\n   והעדכון הבא נבדק מולו, גם כשעותק Homebrew כבר אינו קיים.\n\n## מדיניות הבחירה\n\n- היעד הנוכחי הוא Mac Intel אמיתי. אין להרחיב קבלה ל־Apple Silicon/Rosetta\n  כחלק מהעבודה הזאת. המנגנון כללי למחשבי Intel, בלי רשימת התקנות של משתמש\n  מסוים, גרסאות קבועות או נתיבי משתמש מקובעים.\n- Homebrew, MacPorts ו־Pixi הם ספקים רצויים. קיימת תמיכה חלקית בלבד כיום;\n  אין להציג יעד מוצר כאילו כל הספקים כבר תומכים במעבר כל חבילה.\n- מסלול דורש התאמת זהות החבילה, ארכיטקטורה, מערכת, גרסה וכל תלויות ההרצה.\n  זמינות קובץ בלבד אינה מספיקה. אין נפילה לבנייה מקוד מקור.\n- מבין מסלולים שניתן לבצע ולבדוק, לבחור בגרסת upstream החדשה המתאימה\n  ביותר; לא לדרוש התאמה ליעד Homebrew כשאינו מסופק בינארית. אין ירידה בגרסה.\n  revisions נבדקים בתוך ספק; אין להשוות מספרי אריזה בין ספקים כגרסאות תוכנה.\n- בשוויון גרסה והתאמה, לשמר את הספק שכבר מנהל את ההתקנה כדי למנוע מעברים\n  מיותרים. כשאין לו מסלול מתאים או כשיש חלופה חדשה מתאימה, לבדוק חלופות מחדש.\n- רישום ספק חדש מתבצע אחרי אימות הצלחה, ומייצג בעלות; הגרסה והעדכניות\n  ממשיכות להגיע מהמלאי והקטלוג החיים של הספק, ולא מרשומה שמורה בלבד.\n- כשל תקשורת או מידע חסר יוצגו כלא מאומתים, ולא כ״אין חבילה״.\n- מסלול לא נתמך או תלויים/שירותים שלא טופלו יחסמו רק את החבילה המושפעת,\n  עם סיבה ברורה. פישוט הממשק אינו הרשאה להסיר התקנה בלי אימות ושחזור.\n- לשמר החרגות, ובפרט ngrok, הגדרות, תזמון, היסטוריה וזהות הרשאת ההתראות.\n  אין לסרוק פרויקטים פרטיים או לחסום רק בשל אפשרות לסקריפט פרטי.\n\n## משמעות הפיילוטים\n\n`terminal-notifier` ו־`include-what-you-use` הם מקרי בדיקה טכניים למעבר.\nהם אינם המטרה העסקית ואינם תפריט שהמשתמש צריך ללמוד. הצלחתם אינה הוכחה\nלמעבר ספריות, שירותים או כל חבילה אחרת. לא מחליפים פיילוט או מעבירים חבילה\nאמיתית בזמן פיתוח תיעוד או קוד בלי ההרשאה המתאימה.\n\n## חלוקת העבודה בבדיקות\n\n- הסוכן מממש קוד, כותב בדיקות ומרכז אותן בסקריפט. במהלך הפיתוח מבצע רק\n  אימות קצר וממוקד הנדרש לשינוי, כגון תחביר, תקינות קישורים או בדיקה נקודתית.\n- המשתמש מריץ בפקודה אחת את חבילת הבדיקות המלאה: Python, HTTP, ממשק,\n  רגרסיות, אריזה/קבלה לפי השינוי, ודוח שמור. אין להריץ את כולן מראש ואז\n  למסור למשתמש סקריפט שחוזר על אותה עבודה, בלי בקשה מפורשת ממנו.\n- בדיקות מבודדות אינן משנות חבילות או הגדרות אמיתיות. פעולה אמיתית בסקריפט\n  דורשת הצגת הפעולות ואישור כולל אחד. ההפרדה היא שלב בטיחות בסקריפט הקבלה,\n  ואינה דרישה לאישורי מעבר נוספים בממשק העדכון הרגיל.\n- הדוח יבדיל בין בדיקות קוד, סימולציה ואימות במחשב, ויתעד גרסאות וחתימות,\n  כשל, דילוג או מצב לא מאומת. דילוג אינו PASS; ניתוק אינו הוכחת הצלחה או כשל.\n- קוראים את הדוח ומתקנים כשלים ממוקדים. אין להריץ מחדש הכול כשאותו קוד כבר\n  נבדק, אלא אם שינוי או כשל מצדיקים זאת. עלות הטוקנים הקודמת לא נמדדה.\n- מסירה ללא ריצת המשתמש תסומן ״מימוש נמסר; בדיקות מלאות טרם הורצו״.\n  אין לטעון שהמטרה הושגה לפני דוח שמוכיח את תנאי הקבלה הרלוונטיים.\n\n## גבול המצב הנוכחי\n\nגרסת המקור היא 1.3.3. קיימים מלאי משולב, מסלולי עדכון, סיווג מועמדות\nותשתית מעבר חלקית. עדיין צריך לחבר בחירת ספק, מעבר ושמירת בעלות למסלול\n״עדכן נבחרים/הכול״ ולחבר Pixi לגילוי ולבחירה הכלליים.\nהסקריפט הקיים `tools/verify-migration.py` מוגבל לפיילוט T03; הוא אינו\nבדיקת הקבלה הכוללת ליעד המתואר כאן. פירוט ב־[דוח T03](T03_VALIDATION.md).\n\n## תוכנית ביצוע\n\n[תוכנית המימוש המפורטת](INTEL_UPDATE_IMPLEMENTATION_PLAN.md) מתרגמת את ההחלטות\nלחוזי קוד, שלבי עבודה ותנאי קבלה. סטטוס הביצוע נשאר במסמך המשימות.\n"
UNIFIED_PLAN_CURRENT = "# תוכנית מימוש — עדכון אחיד ובחירת ספק ל־Mac Intel\n\n> **מצב 20.10.2026:** המסמך מפרט את התוכנית המקורית, כולל ניסוחים עתידיים שהיו נכונים לפני בניית הקוד. [דוח המימוש המעודכן](UNIFIED_IMPLEMENTATION_STATUS.md) מפריד בין דרישה, קוד קיים ובדיקת קבלה. `--full` כבר כתוב בענף אך לא נבדק באופן מלא על Mac ולכן אין להפעילו כרגע.\n\nתאריך: 20.10.2026. מצב: תוכנית לביצוע; שינויי המוצר המפורטים כאן טרם מומשו.\nבסיס הקוד: `6158c0a`, גרסה 1.3.3. יעד המסירה המקומית: 1.3.4.\nהמשימות בתוכנית: T03, T04, T07; שימור T01/T02. אין הרחבה ל־T05 או תיקוני T06.\n\nמסמכי יסוד: [מטרת המוצר](PRODUCT_GOAL.md), [משימות וסטטוס](MAC_MANAGER_TASKS.md),\n[מצב המימוש](MAC_MANAGER.md), [פערי T03](T03_VALIDATION.md).\nהחלטות המוצר האחרונות גוברות על תוכנית T03 הישנה ועל הודעות קומיט היסטוריות.\n\n## 1. התוצאה שהמשתמש יקבל\n\n1. ״התחל בדיקה״ מזהה התקנות ומחפש עדכונים בספקים הנתמכים.\n2. ליד כל חבילה מופיעים הגרסה המותקנת, הגרסה שנבחרה, הספק המבצע והנימוק.\n   אם מתוכנן מעבר, מוצג למשל ״Homebrew → MacPorts״ בתוך אותה שורת עדכון.\n3. המשתמש מסמן חבילות או בוחר הכול ולוחץ עדכון. הלחיצה מאשרת את הפעולות\n   המוצגות; אין confirm נוסף ואין אישור מעבר נפרד לכל חבילה.\n4. המערכת מבצעת כל פעולה דרך הספק שנבחר, מאמתת, ומציגה תוצאה סופית.\n5. לאחר מעבר מוצלח, הספק החדש נשמר. פתיחה מחדש וסריקות עתידיות ממשיכות\n   לזהות ולעדכן את ההתקנה אצלו גם לאחר שהעותק הקודם הוסר.\n\nהבחירה מבוססת על הגרסה המתאימה החדשה ביותר מבין מסלולים שניתנים לביצוע\nעם חבילות מוכנות וכל התלויות. אין בנייה מקוד מקור, ירידה בגרסה או החלפת\nספק רק כדי לקבל את אותה גרסת תוכנה. הרשאת מנהל מערכת של macOS נשארת כשנחוצה.\n\n## 2. נקודת הבסיס המוכחת וגבולותיה\n\nהמשתמש הריץ `tools/verify-migration.py --checks` וקיבל PASS ב־19 שלבים,\nכולל 83 בדיקות Python, תחביר, HTTP ושני תרחישי Electron בעברית ובאנגלית.\nהדוח נשמר ב־[תיקיית הריצה](/Users/tzoharlary/.local/share/brew-update-manager/recovery/t03-1.3.3/checks-1792496033916688000/summary.json).\nחתימות 42 קובצי הקוד התאימו בעת קריאת הדוח. זהו בסיס היסטורי מתאריך זה,\nלא אישור לשינויי התוכנית החדשים. לא בוצע מעבר אמיתי בריצה זו.\n\nפערים שנקראו בקוד הנוכחי:\n\n| רכיב | מצב קיים | שינוי נדרש |\n| --- | --- | --- |\n| `mac_manager._combined` | מלאי וסיווג; רוב החלופות נבדקות אחרי חסם Homebrew | הצעות לכל התקנה רלוונטית, גם כשמסלול Homebrew תקין |\n| `Engine.plan/run` | בוחר בעיקר לפי סוג, הגדרות ויעד Homebrew | שימוש בהחלטה שהוצגה בסריקה, עם ספק ויעד מדויקים |\n| `update_routes` | מסלולי Pixi ממופים ו־installed.json; activate כותב shims | adapter ל־Pixi ובעלות מוגנת, בלי דריסת קבצים זרים |\n| `migration_engine` | שלד תוכנית/התקנה/יומן | הפעלה, שחזור, חזרתיות, בעלות ואימות מלאים |\n| `migration_profiles` | שני פרופילים ובדיקת IWYU חלקית | חוזה פרופיל משותף ובדיקות שימוש תקינות |\n| `installation_evidence.annotate` | איסוף וסיווג בתוך לולאת סריקה | חילוץ איסוף של מועמדת יחידה לשימוש חוזר |\n| `macports_provider.upgrade` | אימות גרסת מלאי לאחר port | גם בדיקת שימוש לפרופיל שהועבר |\n| renderer | כפתור סקירת מעבר ו־confirm לעדכון נבחרים | רשימת עדכון אחת ולחיצה אחת |\n| `/health` | hash השירות נקרא מהדיסק בבקשה | זהות קוד שנלכדת בתחילת התהליך |\n| סקריפט הבדיקות | בדיקות מבודדות עובדות; קבלה ישנה מוגבלת לפיילוט | קבלה אחידה של גרסה ארוזה ושל זרימת העדכון |\n\nאין למחוק את הקוד הקיים ולהתחיל מחדש. יש להשתמש בפונקציות הספקים,\nבמלאי, באיסוף הראיות ובמנגנון התוצאות של T01 שכבר קיימים.\n\n## 3. היקף מדויק והחלטות שאינן נתונות לבחירה\n\n- יעד קבלה: macOS על חומרת Intel; לזהות גם Rosetta ולדחות מסלול מעבר חדש על ARM.\n- מסלולי ביצוע: עדכון אצל ספק נוכחי — Homebrew, MacPorts, Pixi; מעבר חדש\n  של כלי עצמאי מ־Homebrew ל־MacPorts או Pixi. מעבר שמקורו MacPorts לספק\n  אחר, ספריות, שירותים ומערכות פיתוח משותפות נשארים חסומים עם נימוק מפורש.\n- מיפויים ופרופילים הם קבועים בקוד. אין להסיק זהות חבילה מדמיון בשם בלבד.\n  אין לקבל פקודת shell או בדיקת שימוש מ־HTTP או מקובץ משתמש.\n- המסלולים העצמאיים הקיימים ל־Node, Deno ו־uv ממשיכים לעבוד כפי שהם;\n  אין להוסיף ספקי מתקינים רשמיים חדשים במסגרת התוכנית.\n- לשמר Pixi קיים וסביבות שהמשתמש מנהל. אין להמיר סביבת משתמש לנכס של המנגנון.\n- `terminal-notifier` הוא רגרסיה של מסלול קיים; helper וזהות הרשאת ההתראות נשמרים.\n  `include-what-you-use` הוא מקרה הקבלה למעבר הנוסף ל־MacPorts, ללא טיפול ב־LLVM Homebrew.\n- אין לבחור פיילוט חלופי אוטומטית אם IWYU חסום. תמיכת Pixi תיבדק מבודד;\n  שינוי אמיתי נוסף דרך Pixi יוצג בנפרד לבחירת המשתמש במסגרת רשימת הפעולות הכוללת.\n- ngrok נשאר מוחרג בכל ספק ובסגירת תלויות. לשמר כל יתר ההחרגות וההגדרות.\n- אין לשנות shell startup files או prefix של Homebrew לצורך ניתוב חדש.\n- אין לבצע מעבר, הסרה או עדכון חבילות אמיתי במהלך המימוש בידי הסוכן.\n- אין להריץ חבילת בדיקות מלאה בידי הסוכן. אין להריץ אוטומטית pack/install\n  שגוררים verify מלא; המשתמש יבצע זאת במסלול הקבלה המתואר בסעיף 14.\n\n## 4. חלוקת אחריות וקבצים\n\n| קובץ | אחריות לאחר השינוי |\n| --- | --- |\n| חדש: `backend/update_selection.py` | חוזי נתונים, השוואה, בחירה ו־fingerprint מהותי; ללא כתיבת התקנות |\n| חדש: `backend/provider_ownership.py` | רישום בעלות, מיזוג למלאי וניתוב פקודות מוגן |\n| חדש: `backend/pixi_provider.py` | גילוי גרסאות Pixi, lock, אימות graph, התקנה ומלאי של סביבות מנוהלות |\n| חדש: `backend/operation_lock.py` | נעילת קובץ משותפת לפעולות משנות מצב |\n| `backend/migration_engine.py` | עסקה, גיבוי, הפעלה, ניקוי, פיצוי ו־recovery |\n| `backend/migration_profiles.py` | מיפויים נתמכים, פקודות חובה, version probes ו־smoke probes |\n| `backend/installation_evidence.py` | אותו איסוף וסיווג לסריקה ולבדיקה טרם ביצוע |\n| `backend/mac_manager.py` | מלאי משולב, discovery והצעות; ללא מימוש עסקה נוסף |\n| `backend/update_engine.py` | ביצוע רציף של בחירת חבילות דרך החלטות אחידות |\n| `backend/update_routes.py` | תאימות למסלולים הישנים; להעביר ל־adapter החדש בהדרגה |\n| `backend/macports_provider.py` | לשמר preflight והתקנה בינארית; לאמת את היעד המדויק ובדיקת שימוש לאחר עדכון |\n| `backend/brew-updates-service.py` | API, זהות שירות, נעילה וסיכום תוצאות |\n| `electron/main.js`, `electron/preload.js` | העברת מזהי הצעות וסבבי בדיקה, בלי החלטת ספק בצד הלקוח |\n| `renderer/app.js`, `mac-manager.js`, `index.html`, `i18n.js` | שורה אחידה, בחירה, ביצוע ותוצאה בשתי שפות |\n| `tools/verify-current-code.py`, `tools/verify-migration.py` | בדיקות מלאות וקבלה בידי המשתמש; דוח אחד מפורט |\n\nאין ליצור service נוסף, מסווג נוסף או engine נוסף. פונקציות קיימות עם אותה\nאחריות יועברו או ייקראו דרך המתאם; אין להשאיר שתי דרכי ביצוע שונות לאותו עדכון.\n\n## 5. שלב א — חבילת שינוי ושחזור קוד\n\nלהוסיף בגנרטור bundle 004 עבור 1.3.4. להתחיל מהקבצים הנוכחיים, כולל תוכנית\nזו ותיקון סקריפט הבדיקות. אין לשנות manifest או גיבוי של bundles קודמים.\n\nפקודות החבילה: `unified-check`, `unified-apply`, `unified-restore`,\n`unified-pack`, `unified-install`. הבדיקות המלאות נשארות ב־`--checks`.\n\n1. לפני עריכת הגנרטור לשמור עותק שלו, manifest של קובצי היעד ועותקים מדויקים\n   תחת `~/.local/share/brew-update-manager/recovery/unified-intel-1.3.4/`.\n2. לערוך רק את `tools/implement-mac-manager.py` באמצעות apply_patch ולהפיק ממנו\n   קוד, בדיקות ותיעוד. `unified-check` בודק hashes ללא כתיבה.\n3. לאמת את כל תנאי הקדם לפני כתיבה. החלה חוזרת זהה לא משנה דבר; עריכה עצמאית\n   מאז ההחלה מחייבת עצירה ולא דריסה. שחזור חייב לסרב לעריכה מאוחרת.\n4. `unified-pack/install` יקבלו דוח בדיקות תואם לחתימת הקוד והבדיקות, ולא יריצו\n   בדיקות מחדש בעצמם. דוח ישן או קובץ חדש שלא נכלל בחתימה אינם תקפים.\n5. לשמור README כפי שהוא, בלי reset או ניקוי עבודה קיימת. לציין פקודת שחזור\n   והבדל בין שחזור קוד לבין rollback של עסקת חבילה.\n\n**תוצר:** bundle שניתן להחיל ולשחזר במבודד, בלי לגעת בהתקנות אמיתיות.\n\n## 6. שלב ב — חוזי הצעה, החלטה ובעלות\n\n### 6.1 זהויות\n\n`installation_id` מזהה התקנה אמיתית: `homebrew:formula:<name>`,\n`homebrew:cask:<name>`, `macports:<port>` או `pixi:<managed-id>`.\n`logical_id` הוא מזהה תוכנה מהמיפוי בקוד. שתי התקנות קיימות של אותה תוכנה\nאינן הופכות אוטומטית להתקנה אחת ואינן מוסרות רק מפני ששמותיהן חופפים.\n\nהפניה מפורשת של route חדש ל־installation_id של היעד מאפשרת לזהות שהמקור\nהוחלף. אין ליצור רשומת tool נוספת עבור port אמיתי כדי לייצג את המעבר.\n\n### 6.2 חוזה Candidate\n\nכל adapter יחזיר JSON שניתן לשמירה, עם השדות הבאים:\n\n```json\n{\n  \"schema_version\": 1,\n  \"logical_id\": \"<mapped-tool>\",\n  \"source_installation_id\": \"homebrew:formula:<name>\",\n  \"provider\": \"macports\",\n  \"provider_names\": [\"<port>\"],\n  \"upstream_version\": \"<version>\",\n  \"package_version\": \"<provider-version>\",\n  \"revision\": \"<provider-revision-or-null>\",\n  \"action\": \"migrate\",\n  \"availability\": \"verified\",\n  \"execution_supported\": true,\n  \"profile_id\": \"<profile>\",\n  \"profile_version\": 1,\n  \"system\": {\"os\": \"macos\", \"arch\": \"x86_64\", \"release\": \"<detected>\"},\n  \"closure\": [],\n  \"evidence_refs\": [],\n  \"checked_at\": \"<timestamp>\",\n  \"blockers\": []\n}\n```\n\n`action`: `update` / `migrate` / `none`. `availability`: `verified` /\n`unavailable` / `unverified`. רשומת closure כוללת זהות, גרסה, build/revision,\nvariants, ארכיטקטורה, תלויות, URL או archive ID ו־checksum כשמפורסם.\nשדות חסרים אינם מוחלפים בערך שנראה חיובי. חתימת MacPorts זמינה ואימות\nחתימה שבוצע הם שני שדות שונים.\n\n### 6.3 החלטה שמוצגת ומאושרת בלחיצה\n\nלהוסיף לכל התקנה `update_decision` עם:\n`decision_id`, `plan_id`, `logical_id`, `source_installation_id`, `current_provider`,\n`target_provider`, `installed_version`, `target_version`, `action`, `status`,\n`selected_candidate`, `alternatives`, `reason_codes`, `blockers`, `created_at`,\n`expires_at`, `material_fingerprint`.\n\n`status`: `ready` / `current` / `blocked` / `unverified` / `needs_recovery`.\nTTL של תוכנית: 15 דקות. הנתונים נשמרים תחת `DATA_DIR/update-plans/<id>.json`.\n`GET /api/state` ו־`GET /api/update-plan` מציגים נתונים שמורים ואינם מתחילים\nסריקת חלופות מלאה. כאשר פג תוקף, אפשר לקרוא את הנתונים אך אין לבצע אותם.\n\n### 6.4 רישום בעלות\n\nלהשתמש ב־`TOOLS_ROOT/migrations/routes.json` עם schema מפורש ו־`routes`:\nכל רשומה כוללת `logical_id`, מקור, ספק/זהות יעד, פרופיל וגרסתו, transaction ID,\ngeneration של הניתוב, פקודות בבעלות המנגנון, נתיב evidence ותאריך אימות.\nלשמור את שם המקור ל־provenance, לא כבסיס לגרסה עתידית.\n\nלתמוך בקריאת הצורה הישנה ללא schema כנתון legacy; להמיר אטומית רק רשומה\nשניתן לאמת את בעלותה במלאי. רשומה לא מאומתת נשמרת ומסומנת, לא נמחקת.\n`installed.json` ימשיך לתאר כלים קיימים; אין להוסיף אליו port. סביבות Pixi\nמנוהלות קיימות נקראות דרכו עד שהadapter אימת את המלאי האמיתי ואת השיוך.\nכתיבות לשני קבצים יהיו מתועדות ביומן בר־התאוששות; אין להניח שהן אטומיות יחד.\n\n## 7. שלב ג — גילוי ובחירת ספק\n\n### 7.1 אלגוריתם מחייב\n\nלממש ב־`update_selection.select(installed, candidates, ownership)`:\n\n1. לסנן החרגות ושגיאות זהות. חבילה מוחרגת אינה מקבלת מועמדת לביצוע.\n2. מועמדת חייבת להתאים ל־macOS/Intel, לספק גרסה שניתנת להשוואה, להיות\n   בעלת closure בינארית מלאה ולהיות נתמכת לביצוע הפעולה המבוקשת.\n3. מעבר מחייב סיווג עצמאי עדכני ללא shared/service roles, פערים או תלויים.\n   עדכון רגיל באותו ספק אינו מקבל חסם מעבר רק משום שהחבילה היא ספרייה.\n4. לפסול downgrade. מעבר לגרסת upstream זהה אינו עדכון; revision חדש אצל\n   אותו ספק יכול להיות עדכון אם הספק מדווח עליו ככזה.\n5. לבחור את הגרסה הגבוהה מבין המועמדות הכשירות. בשוויון לשמר את הספק\n   הנוכחי; אם אינו מועמד כשיר, סדר שוויון קבוע: Homebrew, MacPorts, Pixi.\n6. חלופה חדשה יותר שאינה נתמכת אינה מעלימה עדכון כשיר אצל הספק הנוכחי:\n   מציגים אותה ב־alternatives עם חסם, והבחירה נשארת המסלול שניתן לבצע.\n7. ספק שכשל אינו נחשב כמי שאין לו גרסה חדשה יותר. אם יש מסלול כשיר,\n   מציגים אותו עם אזהרה שההשוואה חלקית; אין לטעון ״החדש ביותר״ באופן מוחלט.\n   אם אין מסלול כשיר, התוצאה unverified כשחסר מידע, blocked כשיש חסם ידוע,\n   ו־current רק כשכל הבדיקות הנדרשות הושלמו ואינן מציעות עדכון.\n\nלהפריד שלוש פעולות: `normalize_upstream`, `compare_upstream`,\n`compare_same_provider_package`. אין להשתמש ב־version_key הנוכחית להשוואה\nכללית, משום שהיא מאבדת משמעות של suffix ו־revision.\nבשלב זה השוואה בין ספקים תומכת בגרסאות יציבות מספריות, עם v אופציונלי\nורכיבי נקודות. להשלים אפסים להשוואת 1.2 ו־1.2.0. suffix לא מוכר או prerelease\nמחזירים `comparison_unknown`; אין להסיר אותם כדי להמציא סדר. הספקים ממשיכים\nלהחליט עדכניות אריזה בתוך הספק באמצעות המטא־נתונים שלהם.\n\n### 7.2 איסוף בתוך הסריקה\n\n- להרחיב `_combined` כך שחלופות נאספות לכל התקנה ממופה רלוונטית, גם כאשר\n  Homebrew מספק bottle. אחרת לא ניתן למצוא חלופה בינארית חדשה יותר.\n- לא לסנן מועמדות רק לפי `Homebrew outdated`. `outdated` בתוצאה המוצגת\n  נגזר מ־update_decision מתאים, גם אם העדכון זוהה אצל ספק אחר.\n- שגיאת Brew לא תעצור גילוי יעד עבור port או סביבת Pixi שכבר מנוהלים.\n- לחלץ `collect_candidate(s, installation, context, force_refresh)` מתוך\n  `installation_evidence.annotate`; הפונקציה תחזיר classification ו־refs.\n  הסריקה וה־preflight משתמשים בה; אין מסווג נוסף במנוע.\n- לצבור נתוני receipt ותלויים לכל הסריקה, לבדוק קבצים רק למועמדת הרלוונטית,\n  ולחלוק תוצאות באותה ריצה. לפני שינוי התקנה לרענן נתונים מהותיים, לא להסתפק ב־cache.\n- timeout לכל command/network probe, ביטול והתייחסות לכשל מקומי כתוצאה חלקית.\n  לשמר מדיניות catalog_refresh הקיימת; לרענן בעת צורך, לא לשאול את המשתמש\n  אם צריך רענון קטלוג. כשל הזדהות יוצג כחסם ממוקד.\n\n### 7.3 Homebrew ו־MacPorts\n\nHomebrew: להשתמש ב־catalog, verified_dependencies ו־bottle_blockers הקיימים;\nלקשור את היעד ל־bottle tag/hash ולמטא־נתונים שנבדקו. קיומו של `--force-bottle`\nאינו חוזה מלא האוסר בניית תלות: להשלים preflight לכל closure ולבדוק שהקטלוג\nלא השתנה לפני הביצוע. אין להוסיף fallback של build-from-source.\n\nMacPorts: להשתמש ב־preflight וב־privileged הקיימים, עם מצב `-b`. לשמר את\nגרף הגרסאות, revisions ו־variants המדויק בתוכנית. לא לבצע מעבר שמחייב לשנות\nגרסה/variants של port שכבר מותקן. שינוי קטלוג בין הבדיקות מחייב החלטה חדשה.\nאחרי התקנה להשוות לגרף שאושר; יציאה 0 בלי היעד שנבחר אינה הצלחה.\n\n### 7.4 Pixi — adapter נפרד\n\n1. מקור המיפויים הראשוני הוא `PIXI_TOOLS` הקיים. מיפוי אינו פרופיל מעבר:\n   רק פרופיל עם פקודות חובה ובדיקת שימוש יוכל לקבל execution_supported.\n2. לגלות את Pixi runtime מהגדרות/runtime קיימים, לקרוא גרסה ויכולות; לא להתקין\n   runtime ולא לשנות סביבות בזמן סריקה. runtime חסר הוא חסם Pixi בלבד.\n3. לבדוק את הפלט של ה־CLI המותקן מול parser מוגדר, עם fixture מייצג.\n   לפעול מתוך scratch בבעלות המנגנון; `--no-config` לבדו אינו מבטל config מקומי.\n4. discovery: חיפוש שם ממופה מדויק ב־conda-forge עבור osx-64, בפלט JSON\n   ובכל הגרסאות. לא לקחת רק חמש גרסאות ברירת מחדל ולא לרשת channels מפרויקט.\n5. לנסות גרסאות יציבות בסדר יורד עד שמתקבל lock תקין למערכת המקומית.\n   כשל solver עבור הגרסה הגבוהה אינו הוכחה שאין גרסה נמוכה יותר מתאימה.\n   כל נסיונות ה־solve כפופים לביטול ותקציב זמן מצטבר של 180 שניות לכלי;\n   timeout לפני השלמת ההשוואה מסומן במפורש כהשוואה חלקית.\n6. manifest חדש מקבע את גרסת השורש שנבחרה. אין constraint מסוג >= שיכול\n   להביא גרסה אחרת בביצוע. לא לכלול PyPI, editable/local dependencies או build sources.\n7. לשמור lock, manifest וייצוא explicit בפורמט שה־CLI המקומי תומך בו.\n   closure תכלול osx-64/noarch בלבד, ערוצי HTTPS מאושרים ו־hashes זמינים.\n   noarch מותר כתלות, לא כהוכחה שכלי native מתאים ל־Intel.\n8. לאמת דרישות מערכת/virtual packages דרך ה־solver ללא overrides שמעמידים\n   פנים שגרסת macOS או הארכיטקטורה שונה. לאמת נגישות ארטיפקטים לפני ready.\n9. התקנה בסביבת generation חדשה מתוך ה־lock המאושר במצב `--locked`,\n   ללא solve מחדש. אם ה־CLI אינו תומך בחוזה זה, להחזיר חסם יכולת מפורש.\n10. לקרוא את גרסת השורש והחבילות מ־conda-meta ולהשוות ל־lock; לאמת פקודות\n    ובדיקת שימוש לפני החלפת ניתוב. לא לשנות או לנקות את סביבת הדור הקודם.\n11. עדכון Pixi עתידי מגלה יעד ב־Pixi, יוצר lock ודור חדש ומחליף מצביע רק אחרי\n    אימות. הוא אינו מבקש את latest_version של Homebrew כדי לבחור יעד.\n\n## 8. שלב ד — תוכנית בלתי משתנה, נעילה וחזרתיות\n\n### 8.1 Fingerprint מהותי\n\nלחשב SHA-256 על JSON קנוני של: זהויות מקור ויעד; גרסאות ו־receipt hash;\nקג פעיל; graph/lock/variants/artifact hashes; מערכת; פרופיל וגרסתו; החלטת סיווג,\nתלויים ופערים; החרגות רלוונטיות; בעלות קבצי הפעלה ומצב ניתוב קודם.\nלא לכלול זמני checked_at, נתיב קובץ ראיות חדש או מזהה תוכנית אקראי.\n\nלפני שינוי ראשון לרענן ולהשוות. שינוי מהותי מחזיר 409 עם\n`error=plan_changed`, הצעה חדשה וסיבה. אין לבצע אותה אוטומטית תחת הלחיצה\nהקודמת. תצוגה מעודכנת מאפשרת לחיצה חדשה אחת; אין אשף אישורים לכל חבילה.\n\nלפני תחילת batch יש לאמת את כל ההחלטות שנבחרו. חבילה שחסומה כבר בתצוגה\nמדולגת עם נימוק; שינוי מהותי בהצעה מוכנה מחייב חזרה לרשימה לפני שינוי batch.\nלאמת שוב כל item לפני ביצועו, מאחר שפעולה קודמת עשויה לשנות תלויות משותפות.\n\n### 8.2 נעילה בין תהליכים\n\nלשלב `fcntl.flock(LOCK_EX | LOCK_NB)` בקובץ תחת TOOLS_ROOT, לפני OP_LOCK\nובאותו סדר בכל הכניסות. הנעילה משותפת גם ל־DATA_DIR חלופי כדי למנוע שינוי\nאותן התקנות משני backends. אין למחוק קובץ נעילה כדי להשתלט על תהליך חי.\nלשמור בעלים/PID/operation ID לצורכי תצוגה, בלי לראות בקיום הקובץ נעילה פעילה.\n\nלעטוף את כל מסלולי השינוי — update-one/selected/all, אישור migration,\nrecovery ו־sync. אין לקנן נעילה חדשה כאשר batch כבר מחזיק אותה: לקרוא\nלמתודה פנימית עם context בעלות מפורש. לא לעקוף נעילה במשתנה גלובלי.\nבדיקה מקבילה קיימת בזמן עדכון רגיל נשמרת; מרגע התחלת batch עם migration\nאין להתחיל בדיקה מקבילה חדשה, ובדיקה קיימת חייבת להסתיים לפני mutation.\n\n### 8.3 זהות בקשה ועסקה\n\nכל לחיצה יוצרת `request_id` פעם אחת בצד הממשק. backend שומר batch journal\nוהשיוך ל־plan לפני שינוי. בקשה חוזרת עם אותו ID ותוכן תחזיר מצב/תוצאה קיימים;\nאותו ID עם תוכן אחר יחזיר 409. אין ליצור transaction חדש לאחר timeout.\nעסקה לא פתורה חוסמת פעולה מתנגשת בלבד ודורשת recovery מפורש; אין auto-resume בהפעלה מחדש.\n\n## 9. שלב ה — מנוע מעבר והפעלה בטוחים\n\n### 9.1 מצבי עסקה\n\n`preflight → backup → install → verify → activate → cleanup → postcheck → completed`.\nכל שינוי שלב נכתב לפני ואחרי ב־journal.json עם סטטוס started/finished,\nתוצאות ונתיבי גיבוי. כתיבה אטומית עם קובץ זמני ייחודי, flush, fsync לקובץ,\nos.replace ו־fsync לתיקייה. להשתמש באותו helper לנתיבי תוכנית/בעלות רגישים;\nלתקן את atomic_write_json הקיים שאינו יוצר parent שרירותי ואינו מבצע fsync.\n\nמצבים סופיים: completed, canceled, rolled_back, needs_attention.\nהפסקה בין שני רישומים אינה הוכחה שהשלב לא בוצע: recovery בודק מצב אמיתי.\n\n### 9.2 גיבוי\n\n- לאפשר במעבר החדש קג מקור יחיד בלבד, receipt תקין וקישור שניתן לשחזר.\n- להעתיק קג עם הרשאות וקישורים, לתעד את כל הקבצים: relative path, סוג,\n  mode, SHA-256 של קובץ רגיל או יעד קישור. לבדוק שלמות ההעתק מול manifest.\n- לתעד opt/link state וקישורים ש־brew link מנהל, וכן מצביעי ניתוב ו־shims קיימים.\n  אין לקרוא קישורי symlink כקובץ רגיל ולגבות בטעות עץ חיצוני.\n- לבצע בדיקת שימוש מקור לפני מעבר ולשמור תוצאה. מקור פגום או מצב שלא ניתן\n  לאמת חסומים; rollback אינו מבטיח לתקן בעיה שהייתה במקור קודם.\n- גיבוי פגום/העתקה חלקית עוצרים לפני התקנת יעד והסרת מקור.\n\n### 9.3 התקנה ואימות\n\nה־engine קורא ל־adapter של היעד, בלי ליצור מתקין חלופי.\nהוא משתמש ברכיב MacPorts קיים רק אם הגרסה/variants תואמים בדיוק.\nרכיבי MacPorts שנוספו מתועדים ונשארים אחרי כשל; אין הסרת תלויות אוטומטית.\nPixi משתמש בסביבה חדשה וב־lock שנבדק. בלי החלפת ניתוב עד שכל פקודות החובה,\nגרסה ובדיקות שימוש הצליחו בנתיבי היעד המפורשים.\n\n### 9.4 ניתוב פקודות\n\nלהשתמש ב־`TOOLS_ROOT/bin` ו־`TOOLS_ROOT/migrations/active/<logical-id>`.\nכל דור הוא תיקייה שמכילה wrappers לכל פקודות הפרופיל; המצביע active מוחלף\nאטומית. shim ב־bin מפנה ל־active, כך שהחלפת המצביע מעבירה יחד את כל הפקודות.\n\nלפני כתיבה לבדוק lstat וגם קישור שבור. לדרוס רק קובץ שמניפסט בעלות קיים\nמוכיח שהוא של המנגנון ושה־hash שלו לא השתנה. אין להסתפק ב״זה symlink״.\nאם לא כל shims קיימים, להתקינם כש־active עדיין מפנה למקור שנבדק, ואז לבצע\nאת החלפת המצביע היחידה; במקרה כשל לשחזר רק קבצים שהמנגנון עצמו כתב.\nהבדיקה הזו תשתמש גם ב־update_routes.activate הישן כדי למנוע מסלול עוקף בעלות.\n\nלא לשנות PATH בקובצי shell. להשוות resolution של כל פקודה ב־login shell\nרגיל לנתיב המנוהל, וגם בהרצה מפורשת. PATH שלא נותן קדימות ל־TOOLS_ROOT/bin\nחוסם מעבר ומציג הוראה ממוקדת; לא להסיר מקור בתקווה שה־shell יבחר יעד אחר.\nלא להסתיר את הבעיה באמצעות prepend זמני ב־postcheck.\n\nל־IWYU הפקודה הראשית מפנה למעטפת MacPorts, לא לבינארי Clang פנימי.\nסקריפטי Python משתמשים ב־Python 3.14 שזוהה ונבדק; אין לקבע נתיב משתמש.\n`iwyu_tool.py` מקבל IWYU_BINARY המצביע למעטפת המאומתת. argv מופרדים,\nוכל קובץ shell שנוצר משתמש ב־shlex.quote, בלי להריץ טקסט HTTP.\n\n### 9.5 cleanup ו־commit של הבעלות\n\nלרענן תלויים, receipt, קג, החרגות ופקודות לפני הסרת מקור. שינוי חוסם הסרה;\nלשחזר ניתוב אם ניתן, ולרשום needs_attention כאשר נדרשת החלטה חדשה.\nהסרה רק לחבילת המקור המדויקת, בלי force, autoremove, cleanup כללי או תלויות,\nעם HOMEBREW_NO_AUTO_UPDATE, HOMEBREW_NO_AUTOREMOVE,\nHOMEBREW_NO_INSTALL_CLEANUP בערך 1.\n\nלאחר הסרה לבדוק מלאי ו־smoke דרך הפקודות הרגילות. רק אחרי postcheck לכתוב\nבעלות committed ולסיים completed. מידע tentative נשאר ביומן ולא הופך לבעלות\nשהסריקה סומכת עליה. כשל כתיבת בעלות אחרי הסרה הוא מצב התאוששות, לא הצלחה.\nכשל רענון תצוגה אחרי transaction שהושלם הוא אזהרה נפרדת, לא ״התקנה נכשלה״.\n\n## 10. שלב ו — כשל, ביטול, המשך ושחזור\n\n| נקודת כשל/ביטול | התנהגות נדרשת |\n| --- | --- |\n| לפני install | מקור וניתוב כפי שהיו; canceled/rolled_back לאחר אימות |\n| install או verify | אין הפעלה; המקור נשמר; לתעד יעדים שנוספו |\n| אחרי activate ולפני הסרה | להחזיר מצביע וקבצים למצב הקודם, בכפוף לבעלות |\n| הסרה חלקית או אחרי הסרה | לבדוק מה נשאר; לשחזר מקור מהגיבוי רק כשאין התנגשות |\n| יעד/קג/shim ששונו עצמאית | לא לדרוס; לשמר עותק תקין ולסיים needs_attention |\n| תהליך שנפסק | לזהות יומן לא סופי, לבדוק כל postcondition ולהציג recovery |\n\nלפצות ב־context נפרד שאינו נקטע בגלל דגל הביטול המקורי. אין לשחרר נעילה\nולהודיע canceled בזמן שעדיין רץ command משנה התקנות. פיצוי מקבל timeouts\nמשלה ולא מריץ command ללא גבול. timeout מותיר needs_attention אם אין הוכחה.\n\n`recovery_plan(transaction_id, action)` ו־approve חייבים לממש resume/rollback,\nלא רק לשמור תוכנית. תוכנית recovery מכילה מצב עדכני, פעולות ושינויים צפויים.\nresume מדלג על שלב רק לאחר שהוכיח את postcondition שלו; אינו מסיר מקור שוב.\nrollback מחזיר קג רק לנתיב המקורי, בודק היעדר בעלים אחר ו־hashes, ומשתמש\nב־brew link בלי overwrite/force כשצריך. אימות מול מצב הבסיס נשמר בדוח.\nשחזור אינו מסיר רכיבי MacPorts חדשים או סביבות עצמאיות של המשתמש.\n\nבממשק רגיל אין מסכי אישור מעבר. רק במקרה של תקלה לא פתורה מוצגים\n״סקירת המשך״/״סקירת שחזור״ עם הפעולה העדכנית ואישור כולל להליך ההתאוששות.\n\n## 11. שלב ז — פרופילים ובדיקות שימוש\n\nלהרחיב Profile עם: version, שמות ספקים, כל פקודות החובה, version argv/parser,\nsmoke function, נתיבים מוגדרים ושיטת הפעלה. מימושים נבחרים מקוד, לא מתוכן חיצוני.\nפרופילים קטנים ו־adapters משותפים מאפשרים להוסיף תמיכה בלי להעתיק עסקה.\nפרופיל חסר הוא execution_unsupported; אין להתקין חבילה רק לפי סיווג עצמאי.\n\nלהעביר את terminal-notifier לממשק המשותף דרך adapter של התנהגותו הקיימת,\nבלי להעביר אותו מחדש או להחליף את notification helper. לשמר קריאת journal ישן\nכראיה היסטורית; לא להריץ תוכנית legacy ללא fingerprint חדש.\n\nלהגדיר פרופילי Pixi למיפויים הקיימים רק כאשר יש פקודות חובה ו־smoke ידוע בקוד.\nכלי שגם מייצא סביבת פיתוח/ספרייה ייחסם למעבר לפי T02 גם אם הוא בטבלת PIXI_TOOLS.\nעדכוני כלי Pixi שכבר מנוהל ממשיכים בפרופיל הקיים, ללא הסרת סביבת מקור ישנה.\n\n### IWYU — תיקון בדיקת הקבלה הקיימת\n\nכל הקבצים בתיקייה זמנית בלבד. ליצור needed.h, unused.h, probe.cc עם vector\nו־compile_commands.json; להשתמש ב־arguments array, לא command string לא מצוטט.\nלבחור נתיב עם רווחים גם ב־fixture כדי לבדוק quoting. לכתוב newlines אמיתיים.\n\n1. SDK באמצעות xcrun --show-sdk-path; לבדוק תקינות ומערכת, לא להשתמש בערך ריק.\n2. מעטפת IWYU עם std=c++17, isysroot מפורש ו־`-Xiwyu --error=17`.\n   לדרוש exit 17 ופסקת המלצה שמסירה include של unused.h; לדחות שגיאות\n   parsing/compile/dyld ונתיבים חסרים גם אם שם הקובץ מופיע בפלט.\n3. iwyu_tool.py עם Python מאומת, IWYU_BINARY מפורש ופרמטרי IWYU שמחזירים\n   אותו חוזה exit/ניתוח. לא לקבל exit 0 כתחליף ל־17 כשנתבקשה הפרת includes.\n4. להזין את פלט הניתוח ב־stdin ל־fix_includes.py --dry_run עם סינון לקובץ הבדיקה.\n   לדרוש exit 1 ו־diff שמסיר include מיותר; hash המקור נשאר ללא שינוי.\n5. להתאים גרסת IWYU וגרסת Clang מתוך metadata וגרף MacPorts, בלי קיבוע גרסה.\n6. לתעד stdout/stderr/returncode לכל command. timeout גרסה 30 שניות ושימוש\n   90 שניות לכל בדיקה. לחזור על הבדיקות אחרי הפעלה ובקבלה לאחר restart.\n\nאם run_cmd אינו תומך stdin, להוסיף input_data/bytes מפורש ולהעביר ל־Popen\nבלי shell, תוך שימור ניקוז stdout/stderr וביטול. להוסיף בדיקה ממוקדת לכך.\n\n## 12. שלב ח — מלאי, API וממשק\n\n### 12.1 מלאי ועדכונים עתידיים\n\nלמזג בעלות רק על גבי התקנת יעד שנמצאת בפועל. port נשאר kind=port וסביבת\nPixi היא kind=tool עם provider=pixi. מקור שחסר אחרי מעבר committed אינו\nמוחזר כ־formula מדומה. עותק Homebrew אחר שקיים באמת נשאר רשומה נפרדת.\n\nתקלה בקריאת routes אינה מוחקת את המלאי; מסמנת אימות בעלות חסר וחוסמת\nפעולה שתלויה בו. `macports_provider.upgrade` ו־Pixi upgrade קוראים smoke\nשל פרופיל הבעלות. כשל smoke מחזיר עדכון שנכשל גם אם המתקין החזיר 0;\nאין לשמור גרסה מוצלחת מלאכותית. גרסה חיה נשמרת לצד כשל התפקוד.\n\n### 12.2 API ותאימות\n\nלשמר שמות endpoints ו־IPC קיימים ולהוסיף שדות אופציונליים:\n\n```text\nPOST /api/check                         {}\nGET  /api/state\nGET  /api/update-plan\nPOST /api/update-selected               {request_id, plan_id, decisions:[{decision_id, material_fingerprint}]}\nPOST /api/update-all                    {request_id, plan_id, decisions:[...]}\nPOST /api/update-one                    {request_id, plan_id, decision_id, material_fingerprint}\nPOST /api/migrations/plan               {name}\nPOST /api/migrations/approve            {id, request_id}\nPOST /api/migrations/recovery-plan      {transaction_id, action:\"resume\"|\"rollback\"}\n```\n\nupdate-all שולח בדיוק את כל ההחלטות המוכנות שהוצגו, לא בחירה אחרת שהופיעה\nבזמן ההמתנה. בקשה ישנה עם name/kind נשארת תקפה רק לעדכון באותו ספק;\nהיא לעולם אינה מאשרת מעבר שלא הוצג. לבקשה ללא plan שמחייבת מעבר להחזיר\n409 `review_required`. פרמטרים זרים או זהות עמומה אינם נבחרים לפי השם הראשון.\nכל endpoint עובר לאותו executor; אין לבצע לולאת HTTP של migrate ואז update.\n\n400 לקלט לא תקין; 409 להתנגשות, תוכנית ישנה/משתנה או recovery נדרש;\n500 לכשל בלתי צפוי. ביטול מחזיר תוצאה מפורשת ויומן. לשמר בדיקות Origin/JSON\nולהחילן על כל כניסה חדשה. אין לקבל נתיבי גיבוי, יעד executable או shell command מלקוח.\nתגובות סופיות כוללות progress שנלכד לפני שחרור הנעילה.\n\nלאסוף fingerprint של כל קובצי backend הרלוונטיים ב־startup פעם אחת ולשמור\nבזיכרון. `/health` מחזיר version, PID, process_started_at, DATA_DIR, capabilities,\nbackend_code_sha256 ו־build ID מתוך החבילה. capabilities מציינות רק יכולת שבאמת\nממומשת. hash מהדיסק בזמן בקשה אינו משקף בהכרח את הקוד שטעון בתהליך.\n\n### 12.3 ממשק\n\n- לעדכן selectedPackagesFromSnapshot, pruneSelectedPackages וסינון שורות\n  לפי installation_id/decision, תוך תאימות לרשומות היסטוריות name/kind.\n- לעדכן preload/main להעברת payload של ההחלטות; backend הוא מקור האמת לבחירה.\n- ״עדכן הכול״ בוחר את כל ready, ״עדכן נבחרים״ את המסומנים. חסומים אינם נבחרים\n  אוטומטית. המונים יבדילו עדכון זמין, ניתן לביצוע וחסום.\n- להסיר confirm/update dialog ואשף migration רגיל. להציג בפרטי השורה לפני\n  הלחיצה ספק קודם/חדש, גרסאות, תלויות, גיבוי והסרה כשמתוכננים.\n- להשתמש ב־beginOperationRequest/finishOperationRequest בכל פעולה, כולל recovery.\n- 409 מחזיר לרשימה המעודכנת עם הסבר. ניתוק מציג ״לא ניתן לאמת סטטוס״,\n  ממשיך מעקב לפי request ID ואינו שולח פעולה חדשה אוטומטית.\n- לשמר סיכום אחרי סגירת חלונית, רענון ושינוי שפה. להציג מה עודכן, הועבר,\n  דולג, נכשל או לא בוצע. אין ״ממתין״ אחרי תוצאה סופית.\n- לתרגם את כל reason_codes ב־i18n.js בעברית ובאנגלית; בלי raw traceback\n  בתצוגה הראשית. גרסאות/נתיבים ב־LTR; ראיות טכניות בפרטים הנפתחים.\n\n## 13. שלב ט — בדיקות שהמממש יכתוב והמשתמש יריץ\n\nאין להריץ את הטבלה במהלך המימוש. להוסיף את קובצי הבדיקה לסקריפט ולמסור\nאותם למשתמש. אימות המממש מוגבל לתחביר הקבצים ששונו, קישורי התיעוד,\ngit diff --check ובדיקה נקודתית רק כאשר נחוצה לפתרון שגיאה מזוהה.\n\n| קובץ/תחום | תרחישים מחייבים |\n| --- | --- |\n| חדש: test_update_selection.py | שלושה ספקים, חדש ביותר כשיר, שוויון שומר בעלים, revision באותו ספק, suffix לא מוכר, downgrade, ספק כושל ומסלול תקין, חלופה חדשה שאינה נתמכת |\n| חדש: test_pixi_provider.py | JSON ומיפוי, כל הגרסאות, גרסה גבוהה שאינה נפתרת, osx-64/noarch, lock מדויק, URL/hash חסר, channel זר, metadata חסר, אין PyPI/local build, אין runtime, ביטול |\n| חדש: test_provider_ownership.py | port יחיד ללא tool כפול, בלי Brew אחרי מעבר, Pixi מתוך conda-meta, legacy records, שגיאת routes, עדכון עתידי מספק חדש, כשל smoke אחרי update |\n| חדש: test_migration_engine.py | הצלחה וכל שלבי הכשל, העתקה חלקית, hash/link/mode, התנגשות shim וקישור שבור, PATH, תלוי חדש, הסרה חלקית, ביטול ופיצוי |\n| אותו engine | resume/rollback לפני/אחרי הסרה, תהליך שנקטע, שינוי עצמאי, double request, expiry, שינוי receipt/graph/variant/החרגות, timestamps בלבד אינם שינוי |\n| חדש: test_operation_lock.py | שני תהליכים עם DATA_DIR שונים מול אותם TOOLS_ROOT, שחרור אחרי יציאת תהליך, אין נעילה מקוננת, חסימת parallel scan בזמן מעבר |\n| הרחבת test_migration_profiles.py | native Intel/ARM/Rosetta, שלוש פקודות IWYU, version/Clang, stdin ודיפ אמיתי, quotes, source hash unchanged, timeouts ופקודה חסרה |\n| HTTP | מקור/JSON, 400/409/500, legacy אינו מאשר מעבר, selected/all אותה תוכנית, final progress, hash startup אינו משתנה לאחר שינוי דיסק |\n| חדש: unified-update-ui.cjs | כפתור נבחרים/הכול שולח בקשה אחת, ספק ונימוק, חסומים, שינוי תוכנית, תשובה מאוחרת, ניתוק/חזרה, recovery, עברית/אנגלית, תוצאה נשמרת |\n| רגרסיה | כל 83 בדיקות הבסיס, תרחישי T01/T02, זהות התראות, ngrok, הגדרות, היסטוריה ושמות התקנות כפולים |\n| bundle 004 | no-write check, replay ללא שינוי, restore מדויק וסירוב לעריכה מאוחרת; על עותק זמני בלבד |\n\nבדיקות מנוע משתמשות ב־fake providers, תהליכי fixture ונתיבים זמניים, בלי\nתקלות יזומות בהתקנות האמיתיות. HTTP אמיתי נגד backend מבודד עם provider\nfixtures בלבד. אין לפתוח backend נוסף מול נתוני הייצור לשם בדיקה.\nבדיקות update עתידי משתמשות בקטלוג מבוקר, לא ממציאות גרסה אמיתית שאינה זמינה.\n\nלהרחיב את signature collector: רשימה מלאה וממוינת של כל קובצי מוצר/בדיקות/\ngenerator ו־package/lock רלוונטיים, לרבות קבצים חדשים ו־JSON mappings.\nלחשב לפני ואחרי, ולהכשיל reuse אם הרשימה או תוכן קובץ השתנו. שינוי מסמך MD\nבלבד אינו מחייב הרצה מחדש אם אינו חלק מקוד/fixture; אין להשתמש ב־commit SHA\nכתחליף לחתימת התוכן. דוח 83 הבדיקות הוא baseline, לא דוח reusable לקוד החדש.\n\n## 14. שלב י — סקריפט משתמש אחד ובדיקת קבלה\n\n### 14.1 ממשק הפקודות לאחר המימוש בלבד\n\nלשמר את הפקודה העובדת הנוכחית:\n\n```text\ntools/verify-migration.py --checks\n```\n\nולהוסיף מסלול מלא אחד:\n\n```text\ntools/verify-migration.py --full\n```\n\n`--checks` מריץ בדיקות מבודדות ושומר דוח בלי שינוי התקנות אמיתיות.\n`--full` מריץ אותן או משתמש בדוח תואם, אורז ומחליף את האפליקציה המקומית\nלאחר גיבוי כשהיא פנויה, ואז מציג עדכונים ומבקש אישור כולל אחד לפני שינוי חבילות.\nהפעלת --full היא הרשאה להחלפת האפליקציה המקומית; בלי YES לא משתנות חבילות,\nאך האפליקציה עצמה כבר עשויה להיות מעודכנת. לציין זאת בתחילת הריצה ובדוח.\n\nהדגל הישן `--acceptance --pilot ...` נשאר מזוהה אך מפנה למסלול הקבלה החדש\nעם הגבלת אותה חבילה. אין להפעיל את המימוש החלקי הישן דרך fallback.\nאין להוסיף פקודת התקנה עוקפת API בתוך הסקריפט.\n\n### 14.2 סדר הריצה המלאה\n\n1. לפתוח תיקיית דוח חדשה, לאמת Intel, Python, Node/Electron וכלי אריזה\n   קיימים. לא להתקין תלויות משתמש אוטומטית. חסר יכולת מייצר exit 2 ודוח.\n2. לאתר דוח בדיקות תואם ולוודא שכל שלב חובה עבר. אם אין — להריץ את\n   הבדיקות המבודדות. כשל עוצר לפני pack או החלפת אפליקציה.\n3. לארוז x64 בלבד בלי פרסום. להפיק build-manifest עם hashes של backend,\n   renderer, preload ו־version. לחלץ מהחבילה ולאמת מול הקוד שנבדק.\n4. לזהות את אפליקציית Mac Update Manager ואת backend שלה לפי נתיב, health,\n   PID, process start ו־DATA_DIR. אם הזהות אינה ודאית, לעצור; אין pkill כללי.\n5. לוודא שאין operation, parallel check או transaction פעיל; לגבות אפליקציה,\n   הגדרות, היסטוריה וניתובים. לסגור רק Mac Update Manager ולהחליף מקומית.\n6. לוודא שהתהליך הישן הסתיים, לפתוח את החדשה ולוודא version/build ID/\n   fingerprint startup תואמים. לא להסתפק בכך שהדיסק מכיל קוד חדש.\n7. לבצע ״התחל בדיקה״ דרך ה־API הרגיל ולשמור state/plan/מלאי ספקים לפני שינוי.\n   להציג בעברית את הפעולות, הגרסאות, המעברים, התלויות והחסמים.\n8. ברירת הקבלה למעבר היא IWYU אם הוא כשיר. עדכונים נוספים אינם נכללים\n   אוטומטית; אפשרות בחירה של כל/חלק תוצג לפני האישור הכולל. אם IWYU חסום,\n   לתעד את הסיבה ולא לבחור כלי חלופי כדי לסמן מעבר כעובר.\n9. לבקש YES אחד הקשור ל־plan ID, fingerprints ולרשימת הפריטים. אם המשתמש\n   לא מאשר — אין שינוי חבילות; לשמור מצב canceled/approval_required ו־exit 2.\n10. לבצע דרך מסלול selected הרגיל. תרחיש הממשק יכול ללחוץ את הכפתור פעם אחת\n    אחרי אישור הסקריפט, ולהשתמש ב־renderer/preload הארוזים מול השירות האמיתי.\n    אסור לבצע פעם ב־API ואז שוב דרך כפתור. request_id קבוע לריצה.\n11. לנטר progress ולשמור transaction IDs, תוצאות, journal וראיות. לוגים נשמרים\n    בלי צורך בהעתקת פלט. timeout/ניתוק מחייב query לפי ID; אין retry של mutation.\n12. לאמת מלאי אחרי, פקודות, smoke, בעלות, הסרת מקור מבוקרת וגיבוי. לבדוק\n    תוצאה בשתי שפות; ביצוע אמיתי פעם אחת בלבד, והשפה השנייה בודקת אותה תוצאה.\n    כפתורי שני המצבים נבדקים בשתי שפות ב־fixtures המבודדים.\n13. כשהאפליקציה פנויה, לסגור ולפתוח שוב רק אותה; לבדוק PID חדש וחתימות,\n    פקודות/שימוש מחדש, בעלות, מלאי ללא כפילות ותוכנית עדכון מהספק החדש.\n14. להשוות baseline: שאר חבילות Homebrew, החרגות, binary_only, תזמון,\n    הגדרות והיסטוריה. ההיסטוריה רשאית לקבל את הרשומות הצפויות בלבד.\n    תלויות MacPorts שהוצגו ותיקיות/רשומות מנוהלות צפויות הן שינויים מותרים.\n15. לשמור סיכום שמשווה מטרות לתוצאות. אין להסיק הצלחת מסלול Pixi אמיתי\n    מסימולציה בלבד. אם אין עדכון עתידי, מספיק לתכנן אותו מתוך ספק היעד\n    ולוודא מבודד התנהגות גרסה חדשה; לא להתקין אותה גרסה שוב כדי לדמות עדכון.\n\nכל התקשרות לשירות צריכה deadline והדפסת עדכון קצר בזמן המתנה. סקריפט הקבלה\nאינו הורג פעולת חבילות כדי לעמוד בזמן. אם התהליך פעיל או התוצאה אינה מאומתת,\nלשמור מזהה עסקה ו־exit 2. במצב recovery אין להתקין מחדש; להציג תוכנית\nrecovery חדשה ולקבל אישור כולל אחד להליך שנבחר.\n\nהרצה חוזרת אחרי completed מאמתת מצב קיים ואינה מתקינה/מסירה שוב.\nאם הקוד השתנה מאז, בדיקות הקוד מתעדכנות, אבל transaction מוצלח אינו משוכפל.\nאין לסגור Codex, להפעיל DevTools remote debugging או לשנות הגדרות MCP.\n\n### 14.3 דוח וקודי יציאה\n\nלשמור תחת `recovery/unified-intel-1.3.4/runs/<run-id>/`:\nsummary.json, summary.md בעברית, code/build manifests, לוג לכל שלב,\nתוכניות, request/transaction IDs, journals, baseline/after inventories,\nתוצאות smoke וצילומי ממשק. summary מפריד בין code_checks, packaged_app,\nsimulated_scenarios, live_migration ו־restart_verification.\n\nלכל שלב status: pass / fail / blocked / not_run / unverified; לתעד גם scope,\nארגומנטים, זמני ריצה ונתיבי ראיות. אין ערך PASS לבדיקה שדולגה.\n\n- 0: כל הבדיקות הנדרשות למצב המבוקש עברו.\n- 1: כשל בדיקה או ביצוע שאומת.\n- 2: תנאי קדם, אישור שלא ניתן, בדיקה שנחסמה או מצב שלא ניתן לאמת.\n\nאם --checks עבר אבל קבלה אמיתית חסומה, לשמור code_checks=pass והקבלה\nblocked, לא לשנות את אחת התוצאות כדי לקבל סימון כולל ירוק.\n\n## 15. סדר קומיטים ומסירה\n\nכל קבוצה כוללת שינויי גנרטור, קוד ובדיקות תואמים; הקומיט לא טוען שסט בדיקות\nשלא הורץ עבר. אין ליצור קומיט שבו import חדש מצביע למודול שעוד לא קיים.\n\n1. `feat(selection): add provider decisions and durable ownership` — חוזים,\n   fingerprints, בעלות ותמיכת legacy, בדיקות הבחירה והמלאי.\n2. `feat(pixi): discover and install exact Intel binary routes` — adapter,\n   נעילה של גרסאות, discovery וסגירת תלויות, בדיקות מבוקרות.\n3. `fix(migrations): complete activation rollback and recovery` — נעילות,\n   גיבוי, ניתוב, פיצוי, פרופילים ובדיקות כשל; legacy notifier נשמר.\n4. `feat(updates): execute displayed provider routes from update actions` —\n   שילוב snapshot/engine/API/preload/UI, ללא אישור נוסף לכל מעבר.\n5. `feat(validation): add one-command packaged Intel acceptance` — script,\n   package 1.3.4 ו־lockfile, build identity, דוח ושימור הגדרות.\n6. `docs: record delivered scope and pending user acceptance` — מצב אמיתי,\n   פקודת משתמש וקישור לדוח, בלי לסגור משימות מוקדם.\n\nשמות אלו הם כוונת חלוקה; מותר לפצל קבוצה כשיש תלות שמחייבת זאת, אך אין\nלאחד הכול לקומיט עמום. push של קוד אינו release; אין tag או פרסום חבילה.\nהתקנה מקומית נעשית במסלול המשתמש לאחר בדיקות תואמות.\n\n## 16. תנאי סיום — לסמן רק עם ראיות\n\n- [ ] סריקה מציגה החלטות מספקים נתמכים, עם השוואה בינארית וגרסה מתאימה ל־Intel.\n- [ ] הבחירה נכונה ב־fixtures של שלושת הספקים, שוויון, revision ומידע חסר.\n- [ ] ״עדכן נבחרים״ ו״עדכן הכול״ משתמשים בהחלטות שהוצגו, בלי מעבר סמוי או confirm נוסף.\n- [ ] מעבר נתמך בוצע ונבדק במחשב, כולל פקודות, מקור שהוסר וגיבוי; חסם סביבה נשאר חסם.\n- [ ] ביטול, המשך ו־rollback הוכחו בנתיבים מבודדים, כולל שינוי עצמאי והסרה חלקית.\n- [ ] אחרי restart הבעלות והמלאי מציגים ספק חדש ללא כפילות, ותוכנית העדכון מגיעה ממנו.\n- [ ] smoke כושל אחרי עדכון ספק אינו מסומן הצלחה; רענון UI כושל אינו מוחק עסקה מוצלחת.\n- [ ] בדיקות הקוד והממשק עברו בדוח המשתמש התואם לקוד שנמסר.\n- [ ] ngrok, התקנות לא נבחרות, הגדרות וזהות ההתראות נשמרו לפי baseline.\n- [ ] התיעוד מבדיל בין מימוש, בדיקות מבודדות וקבלה אמיתית לכל ספק.\n\nלפני דוח המשתמש: ״המימוש נמסר; הבדיקות המלאות ובדיקת הקבלה ממתינות להרצת המשתמש״.\nאחרי --checks בלבד: ״בדיקות הקוד עברו; קבלה אמיתית טרם הושלמה״.\nאין לסגור T03/T04/T07 יחד רק בגלל שהסקריפט קיים או fixture עבר.\n\n## 17. הוראות קצרות למודל המממש\n\n1. לקרוא תוכנית זו ומסמכי היסוד; לבדוק Git ו־AGENTS בלי להפעיל חבילות ישנות.\n2. להתחיל בשלב א ולהתקדם לפי הסדר: חוזים → adapters ובחירה → עסקה/שחזור →\n   חיבור API/UI → סקריפט. לכתוב את הבדיקות עם הרכיב, לא להפעיל סט מלא.\n3. לשמר שמות/ממשקים קיימים עד שהכניסה החדשה מחוברת; לא להשאיר bypass ישן\n   שמאפשר הסרה, ניתוב או התקנה בלי אותו preflight ונעילה.\n4. בחירה טכנית שלא נקבעה כאן תיעשה במימוש הפשוט שמשמר את החוזים. סתירה\n   מהותית או אפשרות לאבד התקנה תתועד כחסם; אין לשנות יעד או להמציא הצלחה.\n5. לעדכן את מסמך המשימות ודוח המימוש. לבצע אימות ממוקד בלבד ולתעד בדיוק מה נבדק.\n6. למסור את הפקודה הבאה רק אחרי שהדגל --full נוצר ונבדק נקודתית. היא עדיין\n   אינה זמינה בקוד בזמן כתיבת תוכנית זו; לא להורות למשתמש להריץ אותה עכשיו:\n\n```bash\n/usr/local/bin/python3.14 \"/Users/tzoharlary/Documents/Projects/brew-update-manager/tools/verify-migration.py\" --full\n```\n\nזו פקודת המסירה העתידית היחידה. המשתמש מריץ, מודיע שהסתיימה, והסוכן קורא\nאת הדוח המקומי ומטפל בכשלים ממוקדים בלי לדרוש העתקת פלט ארוך.\n\n## 18. מקורות טכניים לבדיקת המימוש\n\nהקוד המקומי הוא בסיס ההתאמה ל־CLI המותקן. המקורות הבאים נבדקו בתכנון,\nאך אינם תחליף ל־capability check של הגרסה המקומית:\n\n- [Pixi search](https://pixi.prefix.dev/latest/reference/cli/pixi/search/) — JSON,\n  platform, channel ו־limit; --no-config עדיין מאפשר config מקומי, ולכן משתמשים ב־scratch מנוהל.\n- [Pixi install](https://pixi.prefix.dev/latest/reference/cli/pixi/install/) —\n  --locked בודק התאמת manifest ל־lock; --frozen אינו בודק אותה באותו אופן.\n- [Pixi explicit export](https://pixi.prefix.dev/latest/reference/cli/pixi/workspace/export/conda-explicit-spec/) —\n  יצוא רשימת הארטיפקטים; parser חייב להתאים לגרסה שהותקנה.\n- [Homebrew manpage](https://docs.brew.sh/Manpage) — אפשרויות bottle והסרה;\n  preflight של כל התלויות נדרש מעבר להוספת דגל בשורת הפקודה.\n- [MacPorts guide](https://guide.macports.org/) — התקנה ומלאי, לצד ה־adapter\n  הקיים שמפעיל port במצב בינארי ומכבד את אימות החתימות שלו.\n"
UNIFIED_STATUS_DOC = "# Unified Intel updater — מימוש בענף 1.3.4\n\nעודכן 20.10.2026. ענף `feat/unified-intel-134-foundations`, [טיוטת PR #1](https://github.com/TzoharLary/brew-update-manager/pull/1). `main` והתקנות ה־Mac לא שונו; אין release.\n\n## קיים בקוד\n\n- Candidate/Decision חוצה Homebrew, MacPorts ו־Pixi, גרסאות יציבות, closure בינארית, fingerprint, בעלות ומזהי בקשות.\n- `backend/unified_updates.py`: תכנון וקבלת החלטות שמורות ל־15 דקות, endpoint מאוחד לביצוע selected/all/one, מניעת בקשה כפולה.\n- `backend/pixi_provider.py`: גילוי גרסאות, lock osx-64 ואימות סביבה מנוהלת; **רק עדכון Pixi שכבר מנוהל**, ללא מעבר Homebrew→Pixi.\n- `backend/migration_engine.py`: מעבר מוגבל Homebrew→MacPorts לכלים עם פרופיל מאומת, גיבוי, journal, ניתוב וסקירת resume/rollback.\n- `renderer` / `electron`: הצגת החלטה ושימוש בבקשה המאוחדת. תיקוני `0f2f999` ו־`855a995` מסמנים תוכנית שפג תוקפה, חוסמים עדכון ומבקשים סריקה חדשה.\n- `tools/implement-mac-manager.py` כולל Bundle 004; `tools/verify-migration.py` כולל `--checks` ו־`--full`.\n\n## הוכח בפועל\n\n[CI #39449649595](https://github.com/TzoharLary/brew-update-manager/actions/runs/39449649595) עבר: תחביר Python/JS, **10 בדיקות Python ממוקדות**, בדיקת JS לתוכנית שפג תוקפה, `unified-check` קריאה בלבד של 33 קבצים ו־packaging smoke. אין בכך אישור לסוויטות HTTP/Electron/Python המלאות או לקבלת Mac.\n\n## פערי קוד\n\n1. **T04:** הסריקה אוספת Homebrew רק כשהוא outdated, MacPorts רק למיפוי עם הצעה קיימת, ו־Pixi רק אם הוא כבר מנהל את ההתקנה. אין בחירה מלאה בין חלופות עבור כל התקנה. Homebrew→Pixi אינו נתיב מעבר ניתן לביצוע.\n2. **T07:** עדיין קיימים נתיבי עדכון legacy ואשף מעבר ישן; נדרשת אחידות בטיחות, הודעות 409, תרגום ובדיקות ממשק.\n3. **T03:** מנוע מעבר קיים, אך חסר כיסוי בדיקות מבוקר לכל מקרי failure/restart/recovery, ובדיקת קבלה אמיתית.\n4. **רוחב תמיכה:** Cask, ספריות, שירותים או מעבר ללא פרופיל מאומת נשארים חסומים/חלקיים; לא להמציא `ready`.\n5. **בדיקות ומסירה:** רגרסיות HTTP/Electron, שחזור Bundle 004 והרצת `--checks`/קבלה מקומית.\n\n## תנאי מסירה\n\nהסקריפט `--full` **נכתב**, אך הוא עשוי להחליף אפליקציה ולבקש YES לפעולות חבילה; אין להפעילו ללא מוכנות וקבלה מבוקרת. אין לסגור T03/T04/T07 או למזג לענף הראשי לפני ראיות מתאימות. [תוכנית מקור](INTEL_UPDATE_IMPLEMENTATION_PLAN.md) · [משימות](MAC_MANAGER_TASKS.md).\n"

# Safe, exact source-level refinement over the original guarded bundle patch.
UNIFIED_STALE_APP_EDITS = [["function unifiedReady(pkg) {\n  return pkg?.update_decision?.status === 'ready';\n}","function ensureFreshUnifiedPlan() {\n  if (!unifiedPlanExpired(snapshot)) return true;\n  showMessage(i18n.lang === 'he'\n    ? 'תוכנית העדכון פגה או אינה זמינה. יש לבצע בדיקת עדכונים חדשה.'\n    : 'The update plan expired or is unavailable. Run a new update check.', true);\n  applyActionAvailability();\n  return false;\n}\nfunction unifiedReady(pkg) {\n  return !unifiedPlanExpired(snapshot) && pkg?.update_decision?.status === 'ready';\n}"],["function unifiedPayload(packages) {\n  if (!snapshot?.unified_plan_id) return null;","function unifiedPayload(packages) {\n  if (unifiedPlanExpired(snapshot)) throw new Error('Update plan expired; run a new check');\n  if (!snapshot?.unified_plan_id) return null;"],["function applyActionAvailability() {\n  const allowed = isBrewPathConfigured();","function applyActionAvailability() {\n  const allowed = isBrewPathConfigured();\n  const planExpired = unifiedPlanExpired(snapshot);"],["  updateAllBtn.disabled = busyLoading || operationRunning || !allowed ||","  updateAllBtn.disabled = busyLoading || operationRunning || !allowed || planExpired ||"],["    updateSelectedBtn.disabled = busyLoading || operationRunning || !allowed ||","    updateSelectedBtn.disabled = busyLoading || operationRunning || !allowed || planExpired ||"],["    selectVisibleOutdatedBtn.disabled = busyLoading || operationRunning || !allowed ||","    selectVisibleOutdatedBtn.disabled = busyLoading || operationRunning || !allowed || planExpired ||"],["    checkbox.disabled = busyLoading || operationRunning || !allowed;","    checkbox.disabled = busyLoading || operationRunning || !allowed || planExpired;"],["    btn.disabled = busyLoading || operationRunning || !allowed;","    btn.disabled = busyLoading || operationRunning || !allowed || planExpired;"],["async function updateOne(name, kind, sourceBtn = null, row = null) {\n  if (!ensureBrewConfiguredForActions()) return;","async function updateOne(name, kind, sourceBtn = null, row = null) {\n  if (!ensureBrewConfiguredForActions() || !ensureFreshUnifiedPlan()) return;"],["async function updateSelected() {\n  if (!ensureBrewConfiguredForActions()) return;","async function updateSelected() {\n  if (!ensureBrewConfiguredForActions() || !ensureFreshUnifiedPlan()) return;"],["async function updateAll() {\n  if (!ensureBrewConfiguredForActions()) return;","async function updateAll() {\n  if (!ensureBrewConfiguredForActions() || !ensureFreshUnifiedPlan()) return;"]]

def _unified_stale_app_edit(content, reverse=False):
    pairs = reversed(UNIFIED_STALE_APP_EDITS) if reverse else UNIFIED_STALE_APP_EDITS
    for before, after in pairs:
        original, replacement = (after, before) if reverse else (before, after)
        if content.count(replacement) == 1 and content.count(original) == 0:
            continue
        if content.count(original) != 1:
            raise RuntimeError('Stale plan app patch has an unexpected source layout')
        content = content.replace(original, replacement, 1)
    return content

UNIFIED_STALE_UI_TESTS = r''''use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../renderer/mac-manager.js'), 'utf8');
const sandbox = { Date, i18n: { lang: 'he' } };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'mac-manager.js' });
const expired = (state, now) => sandbox.unifiedPlanExpired(state, now);
assert.equal(expired({}, 1500000), false, 'initial scan is not a stale plan');
assert.equal(expired({ unified_plan_id: 'abc', unified_plan_expires_at: 1000 }, 999999), false);
assert.equal(expired({ unified_plan_id: 'abc', unified_plan_expires_at: 1000 }, 1000000), true);
assert.equal(expired({ unified_plan_id: 'abc', unified_plan_expires_at: 2000, unified_plan_stale: true }, 0), true);
assert.equal(expired({ unified_plan_id: 'abc' }, 0), true, 'missing expiration fails closed');
assert.equal(expired({ unified_plan_id: 'abc', unified_plan_expires_at: 'bogus' }, 0), true);
assert.match(sandbox.unifiedReasonLabel('plan_expired'), /תוכנית העדכון/);
sandbox.i18n.lang = 'en';
assert.match(sandbox.unifiedReasonLabel('plan_expired'), /expired/);
console.log('Stale Intel plan UI helpers: passed');
'''

UNIFIED_RECOVERY = Path(os.environ.get(
    'MAC_MANAGER_UNIFIED_RECOVERY',
    str(Path.home() / '.local/share/brew-update-manager/recovery/unified-intel-1.3.4')
))

def _unified_patched_outputs():
    outputs = {}
    for relative, record in UNIFIED_PATCH_DATA.items():
        data = (ROOT / relative).read_bytes()
        current = hashlib.sha1(('blob %d\0' % len(data)).encode() + data).hexdigest()
        if current == record['target']:
            outputs[relative] = data.decode('utf-8')
            continue
        if relative == 'renderer/app.js':
            original = _unified_stale_app_edit(data.decode('utf-8'), reverse=True)
            payload = original.encode('utf-8')
            old_sha = hashlib.sha1(('blob %d\0' % len(payload)).encode() + payload).hexdigest()
            if old_sha == record['target']:
                outputs[relative] = original
                continue
        if current != record['origin']:
            raise RuntimeError('Protected bundle refuses independent edit: ' + relative)
        lines = data.decode('utf-8').splitlines(keepends=True)
        for start, count, replacement in reversed(record['hunks']):
            lines[start:start + count] = replacement
        result = ''.join(lines)
        encoded = result.encode('utf-8')
        after = hashlib.sha1(('blob %d\0' % len(encoded)).encode() + encoded).hexdigest()
        if after != record['target']:
            raise RuntimeError('Generated patch target mismatch: ' + relative)
        outputs[relative] = result
    return outputs


def unified_foundation_outputs():
    outputs = {
        "docs/UNIFIED_IMPLEMENTATION_STATUS.md": UNIFIED_STATUS_DOC,
        "docs/T04_PROVIDER_DISCOVERY_EXECUTION_PLAN.md": UNIFIED_T04_PLAN,
        "docs/PRODUCT_GOAL.md": UNIFIED_PRODUCT_GOAL_CURRENT,
        "docs/INTEL_UPDATE_IMPLEMENTATION_PLAN.md": UNIFIED_PLAN_CURRENT,
        "backend/update_selection.py": UNIFIED_FOUNDATION_BACKEND_UPDATE_SELECTION_PY,
        "backend/pixi_provider.py": UNIFIED_PIXI_PROVIDER,
        "backend/unified_updates.py": UNIFIED_UPDATES,
        "backend/migration_profiles.py": UNIFIED_PROFILE_CODE,
        "backend/migration_engine.py": UNIFIED_ENGINE,
        "tests/test_migration_engine.py": UNIFIED_ENGINE_TESTS,
        "tests/test_migration_profiles.py": UNIFIED_PROFILE_TESTS,
        "backend/routing_activation.py": UNIFIED_ROUTING,
        "tests/test_routing_activation.py": UNIFIED_ROUTING_TESTS,
        "tests/test_unified_updates.py": UNIFIED_UPDATES_TESTS,
        "tests/stale-plan-ui.cjs": UNIFIED_STALE_UI_TESTS,
        "tests/test_pixi_provider.py": UNIFIED_PIXI_TESTS,
        "backend/provider_ownership.py": UNIFIED_FOUNDATION_BACKEND_PROVIDER_OWNERSHIP_PY,
        "backend/operation_lock.py": UNIFIED_FOUNDATION_BACKEND_OPERATION_LOCK_PY,
        "tests/test_update_selection.py": UNIFIED_FOUNDATION_TESTS_TEST_UPDATE_SELECTION_PY,
        "tests/test_provider_ownership.py": UNIFIED_FOUNDATION_TESTS_TEST_PROVIDER_OWNERSHIP_PY,
        "tests/test_operation_lock.py": UNIFIED_FOUNDATION_TESTS_TEST_OPERATION_LOCK_PY,
        **_unified_patched_outputs(),
        **UNIFIED_MODIFIED_FILES,
    }
    outputs["renderer/app.js"] = _unified_stale_app_edit(outputs["renderer/app.js"])
    return outputs

def _unified_atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.unified-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def unified_pack_or_install(mode):
    """User-only packaging/install: reuse a matching full code report, no test rerun.

    This is deliberately separate from --full: it replaces the app only,
    without approving or mutating any managed packages.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'unified_intel_verifier', ROOT / 'tools/verify-migration.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    report = verifier.Report(mode)
    try:
        verifier.prerequisites(report)
        wanted = verifier.signature()
        matching = verifier.matching_code_report(wanted)
        if matching is None:
            raise RuntimeError('Matching complete code check report required before packaging')
        report.data['checks_report'] = str(matching)
        report.data['code_checks'] = 'pass'
        report.save()
        if mode == 'unified-pack':
            verifier.package_application(report, wanted)
            report.data['result'] = 'pass'
            report.data['exit_code'] = 0
            report.save()
            print('Intel package checked; no application or package installation changed.')
            return
        if mode != 'unified-install':
            raise RuntimeError('Unsupported unified bundle mode')
        # Do not invoke packaging here; verify the artifact already exists.
        info = verifier.metadata(verifier.BUILT)
        if str(info.get('CFBundleShortVersionString')) != verifier.VERSION:
            raise RuntimeError('Built application version does not match source')
        backend = verifier.BUILT / 'Contents/Resources/app.asar.unpacked/backend'
        if not backend.is_dir():
            raise RuntimeError('Packaged backend missing')
        for relative, sha256 in wanted.items():
            if relative.startswith('backend/'):
                path = backend / relative.removeprefix('backend/')
                if not path.is_file() or verifier.sha(path) != sha256:
                    raise RuntimeError('Code changed since packaging: ' + relative)
        product = {'version': verifier.VERSION,
                   'backend_code_sha256': verifier.backend_digest(backend),
                   'build_id': 'unified-intel-1.3.4'}
        live = verifier.find_service()
        verifier.check_idle(live)
        old_pid = live[1]['pid'] if live else None
        verifier.install_packaged(report, live)
        verifier.command(['/usr/bin/open', str(verifier.APPLICATION)], report,
                         'open-installed-app', timeout=30)
        verifier.wait_for_health(report, product, previous_pid=old_pid)
        report.data['packaged_app'] = 'pass'
        report.data['result'] = 'pass'
        report.data['exit_code'] = 0
        report.save()
        print('1.3.4 installed and process verified; NO managed packages were changed.')
    except BaseException as error:
        report.data.update(result='blocked', error=str(error), exit_code=2)
        report.save()
        raise

def unified_foundation_main(mode):
    """Protected, no-package-mutation bundle for independently testable foundations."""
    if mode not in ('unified-check', 'unified-apply', 'unified-restore',
                    'unified-pack', 'unified-install'):
        raise ValueError('Unknown unified bundle operation')
    if mode in ('unified-pack', 'unified-install'):
        unified_foundation_main('unified-check')
        return unified_pack_or_install(mode)
    outputs = {relative: content.encode('utf-8')
               for relative, content in unified_foundation_outputs().items()}
    manifest_path = UNIFIED_RECOVERY / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
    if manifest and set(manifest.get('files', {})) != set(outputs):
        raise RuntimeError('Bundle manifest does not match the generator output set')
    prepared = []
    for relative, desired in outputs.items():
        path = ROOT / relative
        before = path.read_bytes() if path.is_file() else None
        current_hash = digest(before) if before is not None else None
        after_hash = digest(desired)
        if manifest:
            record = manifest['files'][relative]
            if record['after'] != after_hash or current_hash != after_hash:
                raise RuntimeError('Protected bundle refuses independent edits: ' + relative)
            backup = UNIFIED_RECOVERY / 'before' / relative
            if record['before'] is not None and (
                    not backup.is_file() or digest(backup.read_bytes()) != record['before']):
                raise RuntimeError('Protected backup missing or changed: ' + relative)
        elif before is not None and before != desired:
            expected = UNIFIED_EXISTING_BLOBS.get(relative)
            blob_sha = hashlib.sha1(('blob %d\0' % len(before)).encode() + before).hexdigest()
            if not expected or blob_sha != expected:
                raise RuntimeError('Existing file differs from trusted baseline: ' + relative)
        prepared.append((relative, path, before, desired))
    if mode == 'unified-check':
        print('Bundle 004 foundation checked without writes: ' + str(len(outputs)) + ' files.')
        return
    if mode == 'unified-restore':
        if not manifest:
            raise RuntimeError('Bundle 004 has no manifest to restore')
        for relative, path, _, _ in prepared:
            old = manifest['files'][relative]['before']
            if old is None:
                path.unlink()
            else:
                _unified_atomic_bytes(path, (UNIFIED_RECOVERY / 'before' / relative).read_bytes())
        manifest_path.rename(UNIFIED_RECOVERY / ('restored-' + str(time.time_ns()) + '.json'))
        print('Bundle 004 foundation restored exactly.')
        return
    if manifest:
        print('Bundle 004 foundation already applied; no files changed.')
        return
    # All files and baselines have been checked before the first write.
    records = {}
    for relative, path, before, desired in prepared:
        records[relative] = {'before': digest(before) if before is not None else None,
                             'after': digest(desired)}
        if before is not None:
            _unified_atomic_bytes(UNIFIED_RECOVERY / 'before' / relative, before)
    _unified_atomic_bytes(UNIFIED_RECOVERY / 'generator-before.py', Path(__file__).read_bytes())
    _unified_atomic_bytes(manifest_path, (json.dumps({
        'schema_version': 1, 'stage': 'foundation_only',
        'files': records, 'generator_sha256': digest(Path(__file__).read_bytes())
    }, sort_keys=True, indent=2) + '\n').encode())
    for relative, path, before, desired in prepared:
        if before != desired:
            _unified_atomic_bytes(path, desired)
    print('Bundle 004 foundations recorded; no package installs/updates/removals.')

if __name__=='__main__':
    try:
        if len(sys.argv)==2 and sys.argv[1].startswith('unified-'):unified_foundation_main(sys.argv[1])
        elif len(sys.argv)==2 and sys.argv[1].startswith('intel-plan-'):intel_plan_documents(sys.argv[1])
        elif sys.argv[1:]==['checks-runner-apply']:checks_runner_apply()
        elif len(sys.argv)==2 and sys.argv[1].startswith('direction-docs-'):direction_documentation(sys.argv[1])
        elif len(sys.argv)==2 and sys.argv[1].startswith('t01-'):reliability_main(sys.argv[1])
        elif len(sys.argv)==2 and sys.argv[1].startswith('t03-'):t03_main(sys.argv[1])
        else:main()
    except Exception as error:
        print('STOP: '+str(error),file=sys.stderr);sys.exit(1)
