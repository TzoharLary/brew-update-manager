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
    current_version = row.get("upstream_version") or row.get("installed_version")
    cmp = (compare_same_provider_package(
        current_version, row.get("revision"),
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
        current = row.get("upstream_version") or row.get("installed_version")
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
            return (padded, int(c.get("revision") or 0)
                    if c["provider"] == provider else 0,
                    int(c["provider"] == provider),
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
    if row.get("kind") != "formula" or not row.get("outdated") or row.get("inventory_stale"):
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
                         execution_supported=True, blockers=[])
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


def collect_candidates(s, snapshot):
    """Run only during an explicit check. Cached state GET never scans providers."""
    rows = snapshot.get("packages", [])
    offers = {o["name"]: o for o in snapshot.get("migrations", [])}
    mapping = json.loads((Path(__file__).with_name("macports-map.json")).read_text())
    candidates = []
    catalog, installed = None, None
    settings = s.load_settings()
    try:
        from update_engine import Engine
        engine = Engine(s)
        catalog, installed = engine.catalog(), engine.installed()
    except Exception:
        pass
    for row in rows:
        provider = row.get("provider") or "homebrew"
        if row.get("excluded") or row.get("name") == "ngrok" or row.get("inventory_stale"):
            continue
        if provider == "homebrew":
            if row.get("kind") == "formula" and row.get("outdated"):
                candidates.append(_homebrew(s, row, catalog, installed, settings))
            offer = offers.get(row.get("name"))
            roots = mapping.get(row.get("name"), [])
            if row.get("kind") == "formula" and roots and offer and offer.get("status") == "binary_available":
                row["classification"] = offer.get("classification") or {}
                candidate = _ports(s, row, roots, "migrate")
                candidate["logical_id"] = row["name"]
                candidate["profile_id"] = row["name"] if row["name"] in (
                    "terminal-notifier", "include-what-you-use") else None
                candidate["profile_version"] = 1 if candidate["profile_id"] else None
                candidates.append(candidate)
        elif provider == "macports" and row.get("kind") == "port" and row.get("outdated"):
            candidates.append(_ports(s, row, [row["name"]], "update"))
        elif provider == "pixi" and row.get("kind") == "tool":
            # Do not mistake cached Homebrew latest_version for a Pixi catalog result.
            candidate = _base_candidate(row, "pixi", "update", version=None)
            candidate["blockers"] = ["pixi_discovery_required"]
            candidates.append(candidate)
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
    plan = read_plan(s)
    if not plan:
        return snapshot
    by_id = {d["source_installation_id"]: d for d in plan["decisions"]}
    for row in snapshot.get("packages", []):
        row["update_decision"] = by_id.get(_ids(row))
        if row["update_decision"] and row["update_decision"]["status"] == "ready":
            row["outdated"] = True
    snapshot["update_decisions"] = plan["decisions"]
    snapshot["unified_plan_id"] = plan["id"]
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
    if plan.get("expires_at", 0) < time.time():
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
            raise PlanError("migration_execution_not_yet_integrated")
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
    journal = {"schema_version": 1, "request_id": request_id, "plan_id": plan["id"],
               "request_hash": signature, "status": "running", "results": [],
               "started_at": s.now_iso()}
    provider_ownership.atomic_write_json(path, journal)
    try:
        for decision in decisions:
            s.raise_if_cancel_requested()
            source = rows[decision["source_installation_id"]]
            provider = decision["target_provider"]
            if provider != decision["current_provider"]:
                raise PlanError("review_required")
            candidate = decision["selected_candidate"]
            # Fail closed on changed binary closure; no silent source fallback.
            if provider == "homebrew":
                catalog, installed = None, None
                from update_engine import Engine
                engine = Engine(s)
                catalog, installed = engine.catalog(), engine.installed()
                renewed = _homebrew(s, source, catalog, installed, s.load_settings())
                if not renewed.get("closure_verified") or renewed.get("closure") != candidate.get("closure"):
                    raise PlanError("plan_changed")
                result = s.run_cmd([s.brew_bin(), "upgrade", "--force-bottle", source["name"]],
                                   timeout=s.load_settings().get("upgrade_timeout_seconds") or None,
                                   cancellable=True,
                                   env_override=engine.brew_env)
                outcome = {"ok": result.code == 0, "name": source["name"],
                           "kind": source["kind"], "provider": provider,
                           "code": result.code, "stdout": result.stdout[-3000:],
                           "stderr": result.stderr[-3000:]}
            elif provider == "macports":
                latest = _ports(s, source, candidate["provider_names"], "update")
                if (not latest["execution_supported"] or
                    latest["closure"] != candidate["closure"]):
                    raise PlanError("plan_changed")
                outcome = macports_provider.upgrade(s, source)
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


if __name__ == "__main__":
    unittest.main()
'''


UNIFIED_MODIFIED_FILES = {
    "renderer/app.js": "const i18n = window.brewI18n;\n\nlet snapshot = null;\nlet activeTab = 'outdated';\nconst TABLE_COLUMN_COUNT = 10;\nconst COLUMN_WIDTH_STORAGE_KEY = 'bum_column_widths_v1';\nconst APP_UPDATE_META_STORAGE_KEY = 'bum_app_update_meta_v1';\nconst TABLE_SORT_STORAGE_KEY = 'bum_table_sort_v1';\nconst SORTABLE_COLUMN_KEYS = new Set([\n  'package',\n  'type',\n  'currentVersion',\n  'latestVersion',\n  'currentDate',\n  'latestDate',\n  'daysOutdated',\n  'status',\n]);\n\nconst cardsEl = document.getElementById('cards');\nconst tbodyEl = document.getElementById('tbody');\nconst tableTitleEl = document.getElementById('tableTitle');\nconst updatedAtEl = document.getElementById('updatedAt');\nconst msgEl = document.getElementById('message');\nconst loaderEl = document.getElementById('loader');\nconst loaderTextEl = document.getElementById('loaderText');\nconst loaderPercentEl = document.getElementById('loaderPercent');\nconst loaderProgressEl = document.getElementById('loaderProgress');\nconst loaderProgressMetaEl = document.getElementById('loaderProgressMeta');\nconst loaderProgressFillEl = document.getElementById('loaderProgressFill');\nconst loaderPhaseValueEl = document.getElementById('loaderPhaseValue');\nconst loaderCurrentValueEl = document.getElementById('loaderCurrentValue');\nconst loaderEtaValueEl = document.getElementById('loaderEtaValue');\nconst loaderStepEls = Array.from(document.querySelectorAll('#loaderSteps [data-step]'));\nconst loaderCancelBtn = document.getElementById('loaderCancelBtn');\nconst updateLivePanelEl = document.getElementById('updateLivePanel');\nconst updateLiveTotalEl = document.getElementById('updateLiveTotal');\nconst updateLiveCompletedEl = document.getElementById('updateLiveCompleted');\nconst updateLiveRemainingEl = document.getElementById('updateLiveRemaining');\nconst updateLiveFailedEl = document.getElementById('updateLiveFailed');\nconst updateLiveCompletedItemsEl = document.getElementById('updateLiveCompletedItems');\nconst updateLiveRemainingItemsEl = document.getElementById('updateLiveRemainingItems');\nconst updateLiveFailedItemsEl = document.getElementById('updateLiveFailedItems');\nconst checkNowBtn = document.getElementById('checkNowBtn');\nconst updateAllBtn = document.getElementById('updateAllBtn');\nconst updateSelectedBtn = document.getElementById('updateSelectedBtn');\nconst refreshStatusBtn = document.getElementById('refreshStatusBtn');\nconst operationStatusBadgeEl = document.getElementById('operationStatusBadge');\nconst settingsBtn = document.getElementById('settingsBtn');\nconst settingsModalEl = document.getElementById('settingsModal');\nconst settingsModalBackdropEl = document.getElementById('settingsModalBackdrop');\nconst settingsModalCloseBtn = document.getElementById('settingsModalCloseBtn');\nconst settingsModalBodyEl = document.getElementById('settingsModalBody');\nconst settingsPanelEl = document.querySelector('.settings-panel');\nconst settingsAccordionEl = document.querySelector('.settings-accordion');\nconst languageSelect = document.getElementById('languageSelect');\nconst checkAppUpdateBtn = document.getElementById('checkAppUpdateBtn');\nconst installAppUpdateBtn = document.getElementById('installAppUpdateBtn');\nconst appUpdateStatusEl = document.getElementById('appUpdateStatus');\nconst appUpdateProgressBoxEl = document.getElementById('appUpdateProgressBox');\nconst appUpdateProgressPhaseEl = document.getElementById('appUpdateProgressPhase');\nconst appUpdateProgressPercentEl = document.getElementById('appUpdateProgressPercent');\nconst appUpdateProgressFillEl = document.getElementById('appUpdateProgressFill');\nconst appUpdateProgressMetaEl = document.getElementById('appUpdateProgressMeta');\nconst settingsSummaryEl = document.getElementById('settingsSummary');\nconst scheduleEnabledEl = document.getElementById('scheduleEnabled');\nconst scheduleFrequencyEl = document.getElementById('scheduleFrequency');\nconst scheduleTimeRowEl = document.getElementById('scheduleTimeRow');\nconst scheduleTimeEl = document.getElementById('scheduleTime');\nconst scheduleWeekdayRowEl = document.getElementById('scheduleWeekdayRow');\nconst scheduleWeekdayEl = document.getElementById('scheduleWeekday');\nconst scheduleIntervalRowEl = document.getElementById('scheduleIntervalRow');\nconst scheduleIntervalHoursEl = document.getElementById('scheduleIntervalHours');\nconst saveScheduleBtn = document.getElementById('saveScheduleBtn');\nconst scheduleStatusEl = document.getElementById('scheduleStatus');\nconst brewPathInputEl = document.getElementById('brewPathInput');\nconst detectBrewPathBtn = document.getElementById('detectBrewPathBtn');\nconst saveBrewPathBtn = document.getElementById('saveBrewPathBtn');\nconst clearBrewPathBtn = document.getElementById('clearBrewPathBtn');\nconst brewPathStatusEl = document.getElementById('brewPathStatus');\nconst updateHistoryListEl = document.getElementById('updateHistoryList');\nconst updateHistoryStatusEl = document.getElementById('updateHistoryStatus');\nconst packageSearchInputEl = document.getElementById('packageSearchInput');\nconst packageSearchClearBtnEl = document.getElementById('packageSearchClearBtn');\nconst packageTypeFilterEl = document.getElementById('packageTypeFilter');\nconst packageStatusFilterEl = document.getElementById('packageStatusFilter');\nconst packageFilterResetBtnEl = document.getElementById('packageFilterResetBtn');\nconst selectVisibleOutdatedBtn = document.getElementById('selectVisibleOutdatedBtn');\nconst clearSelectionBtn = document.getElementById('clearSelectionBtn');\nconst selectedSummaryEl = document.getElementById('selectedSummary');\nconst historySearchInputEl = document.getElementById('historySearchInput');\nconst historyKindFilterEl = document.getElementById('historyKindFilter');\nconst historyStatusFilterEl = document.getElementById('historyStatusFilter');\nconst historySortSelectEl = document.getElementById('historySortSelect');\nconst historyFiltersClearBtnEl = document.getElementById('historyFiltersClearBtn');\n\nlet progressPollTimer = null;\nlet progressPollInFlight = false;\nlet settingsState = null;\nlet busyLoading = false;\nlet appUpdateState = null;\nlet appUpdateAutoCheckTimer = null;\nlet updateHistoryItems = [];\nlet historyLoadInFlight = false;\nlet historyLoadError = '';\nlet appUpdateMeta = loadAppUpdateMeta();\nlet packageSearchTerm = '';\nlet packageTypeFilter = 'all';\nlet packageStatusFilter = 'all';\nlet historySearchTerm = '';\nlet historyKindFilter = 'all';\nlet historyStatusFilter = 'all';\nlet historySortBy = 'newest';\nlet appUpdateProgressState = null;\nlet appUpdateProgressEnabled = false;\nlet disposeAppUpdateProgress = null;\nlet tableSortState = loadSavedTableSort();\nconst selectedPackageKeys = new Set();\nlet cancelInFlight = false;\nlet settingsModalOpen = false;\nlet lastKnownOperationRunning = false;\nlet lastKnownOperationName = '';\nlet lastKnownParallelCheckRunning = false;\nlet lastKnownParallelCheckFinishedAt = '';\nconst failedItemOpenKeys = new Set();\n\nconst APP_UPDATE_AUTO_CHECK_INTERVAL_MS = 60 * 60 * 1000;\nconst LOADER_PHASE_ORDER = [\n  'idle',\n  'starting',\n  'preparing',\n  'brew_update',\n  'updating_packages',\n  'collecting_outdated',\n  'collecting_installed',\n  'resolving_dates',\n  'translating_descriptions',\n  'canceled',\n  'completed',\n  'error',\n];\n\nfunction formatBytes(value) {\n  const bytes = Number(value || 0);\n  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';\n  const units = ['B', 'KB', 'MB', 'GB'];\n  let size = bytes;\n  let idx = 0;\n  while (size >= 1024 && idx < units.length - 1) {\n    size /= 1024;\n    idx += 1;\n  }\n  const digits = size >= 100 || idx === 0 ? 0 : 1;\n  return `${i18n.formatNumber(Number(size.toFixed(digits)))} ${units[idx]}`;\n}\n\nfunction appUpdatePhaseLabel(phase) {\n  const map = {\n    check_metadata: i18n.t('updates.phaseCheckMetadata'),\n    check_done: i18n.t('updates.phaseCheckDone'),\n    download_delta: i18n.t('updates.phaseDownloadDelta'),\n    verify_delta: i18n.t('updates.phaseVerifyDelta'),\n    apply_delta: i18n.t('updates.phaseApplyDelta'),\n    fallback_full: i18n.t('updates.phaseFallbackFull'),\n    download_full: i18n.t('updates.phaseDownloadFull'),\n    verify_full: i18n.t('updates.phaseVerifyFull'),\n    schedule_restart: i18n.t('updates.phaseScheduleRestart'),\n    error: i18n.t('updates.phaseError'),\n  };\n  return map[String(phase || '')] || i18n.t('updates.progressUnknown');\n}\n\nfunction clearAppUpdateProgress() {\n  appUpdateProgressState = null;\n  if (appUpdateProgressBoxEl) appUpdateProgressBoxEl.classList.remove('visible');\n  if (appUpdateProgressPercentEl) appUpdateProgressPercentEl.textContent = '0%';\n  if (appUpdateProgressFillEl) appUpdateProgressFillEl.style.width = '0%';\n  if (appUpdateProgressPhaseEl) appUpdateProgressPhaseEl.textContent = i18n.t('updates.progressUnknown');\n  if (appUpdateProgressMetaEl) appUpdateProgressMetaEl.textContent = '';\n}\n\nfunction renderAppUpdateProgress() {\n  if (!appUpdateProgressBoxEl) return;\n  if (!appUpdateProgressEnabled || !appUpdateProgressState) {\n    appUpdateProgressBoxEl.classList.remove('visible');\n    return;\n  }\n\n  appUpdateProgressBoxEl.classList.add('visible');\n  const rawPercent = Number(appUpdateProgressState.percent || 0);\n  const percent = Math.max(0, Math.min(100, Math.round(rawPercent)));\n  const phase = String(appUpdateProgressState.phase || '');\n\n  if (appUpdateProgressPercentEl) appUpdateProgressPercentEl.textContent = `${percent}%`;\n  if (appUpdateProgressFillEl) appUpdateProgressFillEl.style.width = `${percent}%`;\n  if (appUpdateProgressPhaseEl) appUpdateProgressPhaseEl.textContent = appUpdatePhaseLabel(phase);\n\n  const meta = [];\n  const downloadedBytes = Number(appUpdateProgressState.downloadedBytes ?? appUpdateProgressState.transferred ?? 0);\n  const totalBytes = Number(appUpdateProgressState.totalBytes ?? appUpdateProgressState.total ?? 0);\n  if (totalBytes > 0) {\n    meta.push(`${formatBytes(downloadedBytes)} / ${formatBytes(totalBytes)}`);\n  }\n\n  const speedBps = Number(appUpdateProgressState.speedBps || 0);\n  if (speedBps > 0) {\n    meta.push(i18n.t('updates.progressSpeed', { value: formatBytes(speedBps) }));\n  }\n\n  const etaSeconds = Number(appUpdateProgressState.etaSeconds || 0);\n  if (etaSeconds > 0) {\n    meta.push(i18n.t('updates.progressEta', { value: formatEta(Math.round(etaSeconds)) }));\n  } else if (phase === 'download_delta' || phase === 'download_full') {\n    meta.push(i18n.t('updates.progressNoEta'));\n  }\n\n  const mode = String(appUpdateProgressState.mode || appUpdateProgressState.modeUsed || '');\n  if (mode === 'delta') {\n    meta.push(i18n.t('updates.modeDelta'));\n  } else if (mode === 'full') {\n    meta.push(i18n.t('updates.modeFull'));\n  }\n\n  if (appUpdateProgressMetaEl) {\n    appUpdateProgressMetaEl.textContent = meta.join(' • ');\n  }\n}\n\nfunction shouldAllowInstall(state) {\n  if (!state?.updateAvailable) return false;\n  return !!(state.canInstall ?? state.hasInstallAsset);\n}\n\nfunction handleAppUpdateProgress(payload) {\n  if (!payload || typeof payload !== 'object') return;\n  if (!appUpdateProgressEnabled && payload.phase !== 'error') return;\n  appUpdateProgressState = payload;\n\n  if (payload.phase === 'fallback_full' && payload.fallbackReason) {\n    setInlineStatus(appUpdateStatusEl, i18n.t('updates.fallbackToFull', {\n      reason: String(payload.fallbackReason),\n    }));\n  }\n\n  renderAppUpdateProgress();\n}\n\nfunction initAppUpdateProgressBridge() {\n  if (disposeAppUpdateProgress || !window.brewApp?.onAppUpdateProgress) return;\n  disposeAppUpdateProgress = window.brewApp.onAppUpdateProgress(handleAppUpdateProgress);\n}\n\nfunction renderAppUpdateStatus() {\n  if (!appUpdateState) {\n    setInlineStatus(appUpdateStatusEl, i18n.t('updates.ready'));\n    installAppUpdateBtn.disabled = true;\n    renderSettingsSummary();\n    renderAppUpdateProgress();\n    return;\n  }\n\n  if (appUpdateState.updateAvailable) {\n    if (shouldAllowInstall(appUpdateState)) {\n      const lines = [\n        i18n.t('updates.available', {\n          latest: appUpdateState.latestVersion,\n          current: appUpdateState.currentVersion,\n        }),\n      ];\n\n      if (appUpdateState.supportsDelta) {\n        lines.push(i18n.t('updates.availableDelta'));\n      } else {\n        lines.push(i18n.t('updates.availableFullOnly'));\n      }\n\n      setInlineStatus(appUpdateStatusEl, lines.join('\\n'));\n      installAppUpdateBtn.disabled = false;\n    } else {\n      setInlineStatus(appUpdateStatusEl, i18n.t('updates.noInstallAsset'), true);\n      installAppUpdateBtn.disabled = true;\n    }\n  } else {\n    setInlineStatus(appUpdateStatusEl, i18n.t('updates.upToDate', {\n      current: appUpdateState.currentVersion,\n    }));\n    installAppUpdateBtn.disabled = true;\n  }\n\n  renderSettingsSummary();\n  renderAppUpdateProgress();\n}\n\nfunction loadAppUpdateMeta() {\n  try {\n    const raw = localStorage.getItem(APP_UPDATE_META_STORAGE_KEY);\n    if (!raw) {\n      return { status: 'unknown', checkedAt: '' };\n    }\n    const parsed = JSON.parse(raw);\n    return {\n      status: String(parsed?.status || 'unknown'),\n      checkedAt: String(parsed?.checkedAt || ''),\n    };\n  } catch {\n    return { status: 'unknown', checkedAt: '' };\n  }\n}\n\nfunction saveAppUpdateMeta() {\n  localStorage.setItem(APP_UPDATE_META_STORAGE_KEY, JSON.stringify(appUpdateMeta));\n}\n\nfunction markAppUpdateCheck(status) {\n  appUpdateMeta = {\n    status: String(status || 'unknown'),\n    checkedAt: new Date().toISOString(),\n  };\n  saveAppUpdateMeta();\n}\n\nasync function checkAppUpdate({ silent = false } = {}) {\n  checkAppUpdateBtn.disabled = true;\n  appUpdateProgressEnabled = !silent;\n  if (!silent) {\n    clearAppUpdateProgress();\n  }\n\n  if (!silent) {\n    installAppUpdateBtn.disabled = true;\n    setInlineStatus(appUpdateStatusEl, i18n.t('updates.checking'));\n  }\n\n  try {\n    const payload = await window.brewApp.checkAppUpdate();\n    appUpdateState = payload;\n    markAppUpdateCheck(payload?.updateAvailable ? 'update-available' : 'up-to-date');\n    renderAppUpdateStatus();\n  } catch (err) {\n    markAppUpdateCheck('error');\n    if (!silent) {\n      appUpdateState = null;\n      setInlineStatus(appUpdateStatusEl, i18n.t('updates.failed', { error: err.message }), true);\n    }\n    renderSettingsSummary();\n  } finally {\n    appUpdateProgressEnabled = false;\n    clearAppUpdateProgress();\n    checkAppUpdateBtn.disabled = false;\n  }\n}\n\nfunction startAutoAppUpdateChecks() {\n  if (appUpdateAutoCheckTimer) {\n    clearInterval(appUpdateAutoCheckTimer);\n  }\n  appUpdateAutoCheckTimer = setInterval(() => {\n    checkAppUpdate({ silent: true });\n  }, APP_UPDATE_AUTO_CHECK_INTERVAL_MS);\n}\n\nfunction stopAutoAppUpdateChecks() {\n  if (!appUpdateAutoCheckTimer) return;\n  clearInterval(appUpdateAutoCheckTimer);\n  appUpdateAutoCheckTimer = null;\n}\n\nasync function installAppUpdate() {\n  if (!shouldAllowInstall(appUpdateState)) {\n    return;\n  }\n\n  appUpdateProgressEnabled = true;\n  clearAppUpdateProgress();\n  checkAppUpdateBtn.disabled = true;\n  installAppUpdateBtn.disabled = true;\n  installAppUpdateBtn.textContent = i18n.t('updates.installing');\n  setInlineStatus(appUpdateStatusEl, i18n.t('updates.downloading'));\n\n  try {\n    const payload = await window.brewApp.downloadAndInstallAppUpdate();\n    appUpdateState = payload;\n\n    if (payload?.restartScheduled) {\n      setInlineStatus(appUpdateStatusEl, i18n.t('updates.restartingSoon'));\n      showMessage(i18n.t('updates.restartingSoon'));\n      return;\n    }\n\n    const modeUsed = String(payload?.modeUsed || payload?.mode || '').toLowerCase();\n    const modeText = modeUsed === 'delta' ? i18n.t('updates.modeDelta') : i18n.t('updates.modeFull');\n    setInlineStatus(\n      appUpdateStatusEl,\n      `${i18n.t('updates.downloaded', { path: payload.downloadedPath })}\\n${modeText}`,\n    );\n    installAppUpdateBtn.disabled = !shouldAllowInstall(appUpdateState);\n    appUpdateProgressEnabled = false;\n  } catch (err) {\n    setInlineStatus(appUpdateStatusEl, i18n.t('updates.downloadFailed', { error: err.message }), true);\n    installAppUpdateBtn.disabled = !shouldAllowInstall(appUpdateState);\n    appUpdateProgressEnabled = false;\n  } finally {\n    installAppUpdateBtn.textContent = i18n.t('updates.install');\n    checkAppUpdateBtn.disabled = false;\n    renderAppUpdateProgress();\n  }\n}\n\nfunction isBrewPathConfigured() {\n  return !!String(settingsState?.settings?.brew_path || '').trim() || !!snapshot?.providers?.macports?.ok;\n}\n\nfunction applyActionAvailability() {\n  const allowed = isBrewPathConfigured();\n  const selectedCount = selectedPackagesFromSnapshot().length;\n  const visibleOutdatedCount = getFilteredPackages().filter((pkg) => pkg?.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg))).length;\n  const operationRunning = !!lastKnownOperationRunning || operationStatusUnverified;\n  const checkBlockedByBusy = !!busyLoading && !isUpdateOperationRunning();\n\n  checkNowBtn.disabled = checkBlockedByBusy || !allowed;\n  updateAllBtn.disabled = busyLoading || operationRunning || !allowed || (!!snapshot?.unified_plan_id && !snapshot?.update_decisions?.some(d => d.status === 'ready'));\n\n  if (updateSelectedBtn) {\n    updateSelectedBtn.disabled = busyLoading || operationRunning || !allowed || selectedCount === 0;\n  }\n\n  if (selectVisibleOutdatedBtn) {\n    selectVisibleOutdatedBtn.disabled = busyLoading || operationRunning || !allowed || visibleOutdatedCount === 0;\n  }\n\n  if (clearSelectionBtn) {\n    clearSelectionBtn.disabled = busyLoading || operationRunning || selectedCount === 0;\n  }\n\n  document.querySelectorAll('.row-update-btn').forEach((btn) => {\n    btn.disabled = busyLoading || operationRunning || !allowed;\n  });\n\n  document.querySelectorAll('.row-select-checkbox').forEach((checkbox) => {\n    checkbox.disabled = busyLoading || operationRunning || !allowed;\n  });\n}\n\nfunction ensureBrewConfiguredForActions() {\n  if (isBrewPathConfigured()) return true;\n  showMessage(i18n.t('message.brewPathRequired'), true);\n  return false;\n}\n\n\n// Operation presentation is independent of the busy overlay.\nlet retainedOperationProgress = null;\nlet operationStatusUnverified = false;\nlet operationRequestGeneration = 0;\nlet operationStartedFloor = 0;\nlet runtimeRefreshGeneration = 0;\n\nfunction runtimeText(he, en) { return i18n.lang === 'he' ? he : en; }\n\nfunction presentOperationStatus() {\n  const p = retainedOperationProgress;\n  const terminal = p && !p.running && ['completed','error','canceled'].includes(p.phase);\n  const reportStatus = terminal ? p.phase === 'error' ? 'failed' : p.phase === 'canceled' ? 'canceled'\n    : p.partial || p.error || (p.deferred_packages || []).length ? 'warning' : 'completed' : 'idle';\n  let status = p?.running || lastKnownParallelCheckRunning || (busyLoading && !terminal) ? 'running' : terminal ? reportStatus : 'idle';\n  if (operationStatusUnverified) status = 'unverified';\n  const labels = {\n    idle:i18n.t('actions.statusIdle'),running:i18n.t('actions.statusRunning'),\n    completed:i18n.t('actions.statusCompleted'),failed:i18n.t('actions.statusFailed'),\n    warning:runtimeText('הושלם עם אזהרות','Completed with warnings'),\n    canceled:runtimeText('בוטל','Canceled'),unverified:runtimeText('לא ניתן לאמת סטטוס','Status could not be verified'),\n  };\n  operationStatusBadgeEl.removeAttribute('data-i18n');\n  operationStatusBadgeEl.className = `status-badge ${status === 'warning' || status === 'unverified' ? 'idle' : status === 'failed' ? 'error' : status === 'canceled' ? 'completed' : status}`;\n  operationStatusBadgeEl.dataset.status = status;\n  operationStatusBadgeEl.textContent = labels[status];\n  // A completed report has its own persistent panel, never an active loader.\n  if (loaderEl) loaderEl.classList.toggle('visible', !terminal && (busyLoading || !!p?.running) && !operationStatusUnverified);\n  let panel = document.getElementById('operationResultSummary');\n  if (!panel) {\n    panel = document.createElement('section'); panel.id = 'operationResultSummary'; panel.className = 'panel';\n    panel.style.padding = '16px'; panel.style.marginBlock = '12px'; panel.setAttribute('aria-live','polite');\n    document.querySelector('.tabs').before(panel);\n  }\n  panel.hidden = !terminal;\n  if (!terminal) return;\n  panel.replaceChildren();\n  const heading = document.createElement('strong'); heading.textContent = labels[reportStatus]; panel.appendChild(heading);\n  if (String(p.operation_name || '').startsWith('update')) {\n    const items = p.completed_packages || [];\n    const skipped = items.filter(x => x.skipped);\n    const updated = items.filter(x => x.ok && !x.skipped);\n    const failed = items.filter(x => !x.ok && !x.skipped && !x.canceled);\n    const canceled = items.filter(x => x.canceled);\n    const deferred = p.deferred_packages || [];\n    const remaining = p.remaining_packages || [];\n    const text = document.createElement('p');\n    text.textContent = runtimeText(\n      `עודכנו: ${updated.length} · נכשלו: ${failed.length} · דולגו: ${skipped.length + deferred.length} · הופסקו: ${canceled.length} · לא בוצעו: ${remaining.length}`,\n      `Updated: ${updated.length} · Failed: ${failed.length} · Skipped: ${skipped.length + deferred.length} · Canceled: ${canceled.length} · Not attempted: ${remaining.length}`);\n    panel.appendChild(text);\n    const list = document.createElement('ul');\n    for (const item of [...deferred,...skipped,...failed,...canceled,...remaining]) {\n      const line = document.createElement('li');\n      const reason = item.reason || (item.canceled ? 'canceled' : !item.ok && !remaining.includes(item) ? 'failed' : 'not_attempted');\n      line.textContent = `${item.name || '—'}: ${migrationReasonLabel(reason)}`;\n      if (item.blockers?.length) line.textContent += ' · ' + item.blockers.map(b=>`${b.name}: ${migrationReasonLabel(b.reason)}`).join(', ');\n      list.appendChild(line);\n    }\n    if (list.children.length) panel.appendChild(list);\n  } else {\n    const text = document.createElement('p');\n    text.textContent = p.phase === 'completed' ? i18n.t(p.partial ? 'message.checkPartial' : 'message.checkSuccess')\n      : p.phase === 'canceled' ? i18n.t('message.checkCanceled') : (p.error || i18n.t('actions.statusFailed'));\n    panel.appendChild(text);\n  }\n}\n\nfunction acceptOperationProgress(progress, authoritative = false) {\n  if (!progress) return false;\n  const stamp = Date.parse(progress.started_at || '') || 0;\n  const previousStamp = Date.parse(retainedOperationProgress?.started_at || '') || 0;\n  if (!authoritative && stamp && stamp < operationStartedFloor) return false;\n  if (stamp && previousStamp && stamp < previousStamp) return false;\n  if (!authoritative && retainedOperationProgress && !retainedOperationProgress.running\n      && ['completed','error','canceled'].includes(retainedOperationProgress.phase)\n      && progress.running && stamp && stamp === previousStamp) return false;\n  if (!stamp && retainedOperationProgress?.started_at && progress.phase === 'idle') return false;\n  retainedOperationProgress = progress;\n  operationStatusUnverified = false;\n  lastKnownOperationRunning = !!progress.running;\n  lastKnownOperationName = String(progress.operation_name || '').toLowerCase();\n  presentOperationStatus();\n  applyActionAvailability();\n  return true;\n}\n\nfunction beginOperationRequest() {\n  operationRequestGeneration += 1;\n  runtimeRefreshGeneration += 1;\n  operationStartedFloor = Date.now();\n  retainedOperationProgress = null;\n  operationStatusUnverified = false;\n  presentOperationStatus();\n}\n\nfunction statusUnavailable() {\n  operationStatusUnverified = true;\n  presentOperationStatus();\n}\n\nasync function finishOperationRequest(response, generation) {\n  if (generation !== operationRequestGeneration) return;\n  // The response progress was captured while the backend still held its lock.\n  if (response?.progress) {\n    if (acceptOperationProgress(response.progress, true)) {\n      renderCheckProgress(response.progress);\n      if (!response.progress.running && !lastKnownParallelCheckRunning) stopProgressPolling();\n      return;\n    }\n  }\n  try {\n    const payload = await window.brewApp.getProgress();\n    if (generation !== operationRequestGeneration) return;\n    if (!payload?.progress || !acceptOperationProgress(payload.progress)) throw new Error('Unverified operation status');\n    renderCheckProgress(payload.progress);\n    await syncParallelCheckState(payload.parallel_check, {showFeedback:false});\n    if (!payload.progress.running && !payload.parallel_check?.running) stopProgressPolling();\n  } catch {\n    if (generation !== operationRequestGeneration) return;\n    statusUnavailable();\n    if (!progressPollTimer) startProgressPolling();\n  }\n}\n\nfunction migrationReasonLabel(reason) {\n  const labels = {\n    source_required:['אין מסלול בינארי מלא','No complete binary route'],\n    inventory_unavailable:['נדרש אימות ההתקנה','Installation verification required'],\n    binary_route_unavailable:['המסלול הבינארי אינו מאומת','Binary route is unverified'],\n    excluded:['מוחרג','Excluded'],not_outdated:['כבר מעודכן','Already current'],\n    canceled:['בוטל','Canceled'],failed:['נכשל','Failed'],not_attempted:['לא בוצע','Not attempted'],\n    service_definition_or_registration:['הוגדר שירות','Service definition or registration'],\n    documented_installed_consumers:['נמצאו תלויים מותקנים','Installed consumers found'],\n    exported_library_interface:['ממשק ספרייה משותף','Exported library interface'],\n    development_environment_interface:['ממשק סביבת פיתוח','Development environment interface'],\n    complete_local_evidence_without_shared_roles:['הראיות המקומיות הושלמו ללא תפקיד משותף','Local evidence complete without shared roles'],\n    required_evidence_incomplete:['חסרות ראיות נדרשות','Required evidence is incomplete'],\n  };\n  return (labels[reason] || [reason,reason])[i18n.lang==='he'?0:1];\n}\n\nfunction setLoading(on, messageKey = 'message.loadingState', params = {}) {\n  busyLoading = !!on;\n  if (on) loaderTextEl.textContent = i18n.t(messageKey, params);\n  if (on) {\n    resetLoaderProgress();\n    if (loaderEl) {\n      loaderEl.classList.add('visible');\n    }\n    setOperationStatusBadge('running');\n  }\n\n  if (!on && loaderEl && !lastKnownOperationRunning) {\n    loaderEl.classList.remove('visible');\n  }\n\n  applyActionAvailability();\n\n  presentOperationStatus();\n}\n\nfunction showMessage(text, error = false) {\n  msgEl.textContent = text;\n  msgEl.style.display = 'block';\n  msgEl.classList.toggle('error', !!error);\n}\n\nfunction clearMessage() {\n  msgEl.style.display = 'none';\n  msgEl.textContent = '';\n  msgEl.classList.remove('error');\n}\n\nfunction setInlineStatus(el, text, isError = false) {\n  if (!el) return;\n  el.textContent = String(text || '').trim();\n  el.style.color = isError ? 'var(--danger)' : 'var(--muted)';\n}\n\nfunction applyLocalizedAttributes() {\n  document.querySelectorAll('[data-i18n-title]').forEach((node) => {\n    const key = node.getAttribute('data-i18n-title');\n    if (!key) return;\n    node.setAttribute('title', i18n.t(key));\n  });\n\n  if (settingsBtn) {\n    settingsBtn.setAttribute('aria-label', i18n.t('actions.settingsLabel'));\n  }\n}\n\nfunction mountRuntimePanels() {\n  if (settingsPanelEl && settingsModalBodyEl && settingsPanelEl.parentElement !== settingsModalBodyEl) {\n    settingsModalBodyEl.appendChild(settingsPanelEl);\n  }\n\n  if (settingsAccordionEl) {\n    settingsAccordionEl.open = true;\n  }\n\n  if (cardsEl && loaderEl && loaderEl.parentElement !== cardsEl.parentElement) {\n    cardsEl.insertAdjacentElement('afterend', loaderEl);\n  }\n}\n\nfunction setSettingsModalOpen(open) {\n  settingsModalOpen = !!open;\n  if (!settingsModalEl) return;\n\n  settingsModalEl.classList.toggle('open', settingsModalOpen);\n  settingsModalEl.setAttribute('aria-hidden', settingsModalOpen ? 'false' : 'true');\n  document.body.style.overflow = settingsModalOpen ? 'hidden' : '';\n}\n\nfunction toggleSettingsModal() {\n  setSettingsModalOpen(!settingsModalOpen);\n}\n\nfunction operationBadgeClassName(status) {\n  const value = String(status || '').trim().toLowerCase();\n  if (value === 'running') return 'running';\n  if (value === 'failed' || value === 'error') return 'error';\n  if (value === 'completed' || value === 'canceled') return 'completed';\n  return 'idle';\n}\n\nfunction isUpdateOperationRunning() {\n  return !!lastKnownOperationRunning && String(lastKnownOperationName || '').startsWith('update');\n}\n\nfunction setOperationStatusBadge(status) {\n  presentOperationStatus();\n}\n\nasync function refreshRuntimeStatus({ showFeedback = true, reloadState = true } = {}) {\n  const generation = ++runtimeRefreshGeneration;\n  const actionGeneration = operationRequestGeneration;\n  if (refreshStatusBtn) {\n    refreshStatusBtn.disabled = true;\n  }\n\n  try {\n    const progressPayload = await window.brewApp.getProgress();\n    if (generation !== runtimeRefreshGeneration || actionGeneration !== operationRequestGeneration) return;\n    const progress = progressPayload?.progress || null;\n    const parallelCheck = progressPayload?.parallel_check || null;\n\n    if (progress) {\n      renderCheckProgress(progress);\n      if (progress.running) {\n        startProgressPolling();\n        if (showFeedback) {\n          showMessage(i18n.t('message.operationRunningNow', {\n            name: progress.current_package || i18n.t('progress.currentNone'),\n          }));\n        }\n      } else {\n        stopProgressPolling();\n        if (!busyLoading && loaderEl) {\n          loaderEl.classList.remove('visible');\n        }\n        if (String(progress.phase || '') === 'error') {\n          setOperationStatusBadge('failed');\n        } else if (String(progress.phase || '') === 'completed' || String(progress.phase || '') === 'canceled') {\n          setOperationStatusBadge('completed');\n        } else {\n          setOperationStatusBadge('idle');\n        }\n        if (showFeedback) {\n          showMessage(i18n.t('message.statusRefreshed'));\n        }\n      }\n    }\n\n    await syncParallelCheckState(parallelCheck, { showFeedback });\n\n    await loadSettings();\n    await loadUpdateHistory();\n\n    if (reloadState && !progress?.running && !parallelCheck?.running) {\n      await loadState({ showLoader: false });\n    }\n  } catch (err) {\n    if (generation !== runtimeRefreshGeneration || actionGeneration !== operationRequestGeneration) return;\n    statusUnavailable();\n    if (!progressPollTimer) startProgressPolling();\n    if (showFeedback) {\n      showMessage(i18n.t('message.refreshFailed', { error: err.message }), true);\n    }\n  } finally {\n    if (refreshStatusBtn) {\n      refreshStatusBtn.disabled = false;\n    }\n  }\n}\n\nasync function syncParallelCheckState(parallelCheck, { showFeedback = false } = {}) {\n  const payload = parallelCheck && typeof parallelCheck === 'object' ? parallelCheck : {};\n  const running = !!payload.running;\n  const finishedAt = String(payload.finished_at || '').trim();\n  const lastError = String(payload.last_error || '').trim();\n\n  if (running) {\n    lastKnownParallelCheckRunning = true;\n    presentOperationStatus();\n    if (showFeedback) {\n      showMessage(i18n.t('message.checkParallelRunning'));\n    }\n    return;\n  }\n\n  const justFinished = !!finishedAt && finishedAt !== lastKnownParallelCheckFinishedAt;\n  if (!justFinished) {\n    lastKnownParallelCheckRunning = false;\n    presentOperationStatus();\n    return;\n  }\n\n  lastKnownParallelCheckFinishedAt = finishedAt;\n  const wasRunning = lastKnownParallelCheckRunning;\n  lastKnownParallelCheckRunning = false;\n  presentOperationStatus();\n\n  await loadState({ showLoader: false });\n\n  if (lastError) {\n    showMessage(i18n.t('message.checkParallelFailed', { error: lastError }), true);\n    return;\n  }\n\n  if (showFeedback || wasRunning) {\n    showMessage(i18n.t('message.checkParallelCompleted'));\n  }\n}\n\nfunction parseBusyOperationInfo(errorMessage) {\n  const raw = String(errorMessage || '');\n  const match = raw.match(/Operation '([^']+)' is already running since ([^\\n]+)/i);\n  if (!match) {\n    return null;\n  }\n\n  return {\n    name: String(match[1] || '').trim(),\n    sinceRaw: String(match[2] || '').trim(),\n  };\n}\n\nfunction formatOperationErrorMessage(errorLike) {\n  const raw = String(errorLike?.message || errorLike || '').trim();\n  const busy = parseBusyOperationInfo(raw);\n  if (!busy) {\n    return raw;\n  }\n\n  return i18n.t('message.operationBusy', {\n    name: busy.name || i18n.t('common.unknown'),\n    since: formatDate(busy.sinceRaw),\n  });\n}\n\nfunction progressPackageLabel(item) {\n  if (!item || typeof item !== 'object') {\n    return String(item || '').trim();\n  }\n  const label = String(item.label || '').trim();\n  if (label) return label;\n  const name = String(item.name || '').trim();\n  const kind = String(item.kind || '').trim();\n  if (name && kind) return `${kind}:${name}`;\n  return name || kind;\n}\n\nfunction clearNode(node) {\n  if (!node) return;\n  while (node.firstChild) {\n    node.removeChild(node.firstChild);\n  }\n}\n\nfunction makeProgressEmptyNode() {\n  const empty = document.createElement('div');\n  empty.className = 'update-live-empty';\n  empty.textContent = i18n.t('progress.noItems');\n  return empty;\n}\n\nfunction completedItemPrefix(item) {\n  if (item?.canceled) return '🛑';\n  if (item?.skipped) return '⏭';\n  if (item?.ok) return '✅';\n  return '❌';\n}\n\nfunction failureItemKey(item) {\n  return [\n    String(item?.kind || '').trim(),\n    String(item?.name || '').trim(),\n    String(item?.started_at || '').trim(),\n    String(item?.completed_at || '').trim(),\n    String(item?.code ?? ''),\n  ].join('|');\n}\n\nfunction failureTechnicalText(item) {\n  if (item?.canceled) {\n    return i18n.t('progress.failureCanceled');\n  }\n\n  const detail = String(item?.error || '').trim();\n  if (detail) {\n    return detail;\n  }\n\n  if (item?.reason === 'not_outdated') {\n    return i18n.t('message.skippedReasonNotOutdated');\n  }\n\n  const code = Number(item?.code || 0);\n  if (code === 124) {\n    return i18n.t('progress.failureTimeout');\n  }\n  if (Number.isFinite(code) && code !== 0) {\n    return i18n.t('progress.failureExitCode', { code: i18n.formatNumber(code) });\n  }\n\n  return i18n.t('progress.failureUnknown');\n}\n\nfunction failureHumanExplanation(item) {\n  if (item?.canceled) {\n    return i18n.t('progress.failureExplainCanceled');\n  }\n\n  const raw = String(item?.error || '').toLowerCase();\n  const code = Number(item?.code || 0);\n\n  if (raw.includes('ssl connection timeout') || raw.includes('curl: (28)') || raw.includes('download failed')) {\n    return i18n.t('progress.failureExplainNetworkTimeout');\n  }\n  if (raw.includes('command timed out after')) {\n    return i18n.t('progress.failureExplainCommandTimeout');\n  }\n  if (raw.includes('post-install step did not complete successfully')) {\n    return i18n.t('progress.failureExplainPostInstall');\n  }\n  if (raw.includes('command line tools release is available') || raw.includes('xcode-select --install')) {\n    return i18n.t('progress.failureExplainCliTools');\n  }\n  if (raw.includes('tier 2 configuration')) {\n    return i18n.t('progress.failureExplainTier2');\n  }\n\n  if (code === 124) {\n    return i18n.t('progress.failureExplainCommandTimeout');\n  }\n  if (Number.isFinite(code) && code !== 0) {\n    return i18n.t('progress.failureExplainGenericCode', { code: i18n.formatNumber(code) });\n  }\n\n  return i18n.t('progress.failureExplainGeneric');\n}\n\nfunction failureReasonText(item) {\n  const explanation = failureHumanExplanation(item);\n  const technical = failureTechnicalText(item);\n  if (!technical || technical === explanation) {\n    return explanation;\n  }\n  return `${explanation}\\n\\n${i18n.t('progress.failureTechnicalPrefix')}\\n${technical}`;\n}\n\nfunction renderSimpleProgressList(container, items, { mode = 'generic' } = {}) {\n  if (!container) return;\n  clearNode(container);\n\n  const list = Array.isArray(items) ? items : [];\n  if (!list.length) {\n    container.appendChild(makeProgressEmptyNode());\n    return;\n  }\n\n  list.forEach((item) => {\n    const row = document.createElement('div');\n    row.className = 'update-live-item';\n\n    const label = document.createElement('div');\n    label.className = 'update-live-item-label';\n    const base = progressPackageLabel(item) || i18n.t('common.unknown');\n\n    if (mode === 'completed') {\n      label.textContent = `${completedItemPrefix(item)} ${base}`;\n    } else {\n      label.textContent = `• ${base}`;\n    }\n\n    row.appendChild(label);\n\n    if (mode === 'completed') {\n      const noteText = failureReasonText(item);\n      if (item?.ok || item?.skipped || item?.canceled) {\n        const note = document.createElement('div');\n        note.className = 'update-live-item-note';\n        note.textContent = item?.ok ? i18n.t('progress.completedOk') : noteText;\n        row.appendChild(note);\n      } else if (noteText) {\n        const note = document.createElement('div');\n        note.className = 'update-live-item-note';\n        note.textContent = noteText;\n        row.appendChild(note);\n      }\n    }\n\n    container.appendChild(row);\n  });\n}\n\nfunction renderFailedProgressList(container, items) {\n  if (!container) return;\n\n  container.querySelectorAll('details[data-failure-key][open]').forEach((node) => {\n    const key = String(node.getAttribute('data-failure-key') || '').trim();\n    if (key) {\n      failedItemOpenKeys.add(key);\n    }\n  });\n\n  clearNode(container);\n\n  const list = (Array.isArray(items) ? items : []).filter((item) => !item?.ok);\n  if (!list.length) {\n    failedItemOpenKeys.clear();\n    container.appendChild(makeProgressEmptyNode());\n    return;\n  }\n\n  const validKeys = new Set();\n\n  list.forEach((item) => {\n    const key = failureItemKey(item);\n    validKeys.add(key);\n\n    const details = document.createElement('details');\n    details.className = 'update-live-failure';\n    details.setAttribute('data-failure-key', key);\n    if (failedItemOpenKeys.has(key)) {\n      details.open = true;\n    }\n\n    const summary = document.createElement('summary');\n    summary.textContent = `❌ ${progressPackageLabel(item) || i18n.t('common.unknown')}`;\n\n    const reason = document.createElement('div');\n    reason.className = 'update-live-failure-reason';\n    reason.textContent = failureReasonText(item);\n\n    details.appendChild(summary);\n    details.appendChild(reason);\n\n    details.addEventListener('toggle', () => {\n      if (details.open) {\n        failedItemOpenKeys.add(key);\n      } else {\n        failedItemOpenKeys.delete(key);\n      }\n    });\n\n    container.appendChild(details);\n  });\n\n  Array.from(failedItemOpenKeys).forEach((key) => {\n    if (!validKeys.has(key)) {\n      failedItemOpenKeys.delete(key);\n    }\n  });\n}\n\nfunction renderPackageActivity(progress) {\n  const panel = document.getElementById('packageActivity');\n  if (!panel) return;\n  const activity = progress?.package_activity;\n  panel.hidden = !(progress?.running && progress.phase === 'updating_packages' && activity);\n  if (panel.hidden) return;\n  const subject = String(activity.subject || activity.target || '');\n  const target = String(activity.target || '');\n  const stage = ['preparing', 'downloading', 'dependency', 'building', 'installing', 'finishing']\n    .includes(activity.stage) ? activity.stage : 'preparing';\n  const dependency = subject !== target;\n  document.getElementById('packageActivityTitle').textContent = i18n.t(`progress.activityStage.${stage}`, { name: subject });\n  document.getElementById('packageActivityExplanation').textContent = i18n.t(\n    activity.provider && activity.provider !== 'homebrew' ? 'policy.providerActivity' : stage === 'building'\n      ? (dependency ? 'progress.activityBuildDependency' : 'progress.activityBuildTarget')\n      : (dependency ? 'progress.activityDependency' : 'progress.activityTarget'),\n    { name: subject, target, provider: activity.provider },\n  );\n  const secondsSince = (value) => {\n    const timestamp = Date.parse(value || '');\n    return Number.isFinite(timestamp) ? Math.max(0, Math.floor((Date.now() - timestamp) / 1000)) : 0;\n  };\n  const duration = (seconds) => i18n.t('progress.activityDuration', {\n    minutes: i18n.formatNumber(Math.floor(seconds / 60)),\n    seconds: i18n.formatNumber(seconds % 60),\n  });\n  document.getElementById('packageActivityTiming').textContent = i18n.t('progress.activityTiming', {\n    packageTime: duration(secondsSince(activity.started_at)),\n    stageTime: duration(secondsSince(activity.stage_started_at)),\n  });\n  const quietSeconds = secondsSince(activity.last_output_at || activity.started_at);\n  document.getElementById('packageActivityQuiet').textContent = quietSeconds >= 30\n    ? i18n.t('progress.activityQuiet', { time: duration(quietSeconds) })\n    : i18n.t('progress.activityNoEstimate');\n  const technical = document.getElementById('packageActivityTechnical');\n  const output = Array.isArray(activity.technical_tail) ? activity.technical_tail.join('\\n') : '';\n  if (technical.textContent !== output) technical.textContent = output;\n  const measured = activity.build_progress;\n  document.getElementById('packageActivityBuildProgress').textContent = measured\n    ? i18n.t(measured.kind === 'reported_percent' ? 'policy.buildPercent' : 'policy.buildProgress', { done: i18n.formatNumber(measured.done), total: i18n.formatNumber(measured.total), percent: i18n.formatNumber(measured.percent) }) : '';\n}\n\nfunction renderUpdateLivePanel(progress) {\n  renderPackageActivity(progress);\n  if (!updateLivePanelEl) return;\n\n  const operationName = String(progress?.operation_name || '').trim().toLowerCase();\n  const planned = Array.isArray(progress?.planned_packages) ? progress.planned_packages : [];\n  const completed = Array.isArray(progress?.completed_packages) ? progress.completed_packages : [];\n  const remaining = Array.isArray(progress?.remaining_packages) ? progress.remaining_packages : [];\n  const failed = Array.isArray(progress?.failed_packages) ? progress.failed_packages : [];\n  const deferred = Array.isArray(progress?.deferred_packages) ? progress.deferred_packages : [];\n  const deferredEl = document.getElementById('deferredUpdateSummary');\n  deferredEl.hidden = deferred.length === 0;\n  deferredEl.textContent = i18n.t('policy.deferred', { count: i18n.formatNumber(deferred.length), names: deferred.map(p => p.name).join(', ') });\n  const isUpdateOperation = operationName.startsWith('update');\n\n  if (!isUpdateOperation) {\n    updateLivePanelEl.classList.remove('visible');\n    return;\n  }\n\n  updateLivePanelEl.classList.add('visible');\n\n  const total = planned.length || Number(progress?.total || 0);\n  const done = completed.filter(item => item.ok && !item.skipped).length;\n  const remainingCount = remaining.length;\n  const failedCount = failed.length;\n\n  if (updateLiveTotalEl) updateLiveTotalEl.textContent = i18n.formatNumber(total);\n  if (updateLiveCompletedEl) updateLiveCompletedEl.textContent = i18n.formatNumber(done);\n  if (updateLiveRemainingEl) updateLiveRemainingEl.textContent = i18n.formatNumber(remainingCount);\n  if (updateLiveFailedEl) updateLiveFailedEl.textContent = i18n.formatNumber(failedCount);\n\n  renderSimpleProgressList(updateLiveCompletedItemsEl, completed, { mode: 'completed' });\n  renderSimpleProgressList(updateLiveRemainingItemsEl, remaining, { mode: 'generic' });\n  renderFailedProgressList(updateLiveFailedItemsEl, failed);\n}\n\nfunction setUpdateHistoryStatus(text, isError = false) {\n  if (!updateHistoryStatusEl) return;\n  const value = String(text || '').trim();\n  updateHistoryStatusEl.style.display = value ? 'block' : 'none';\n  setInlineStatus(updateHistoryStatusEl, value, isError);\n}\n\nfunction withTimeout(promise, timeoutMs, timeoutMessage) {\n  return new Promise((resolve, reject) => {\n    const timer = setTimeout(() => {\n      reject(new Error(timeoutMessage));\n    }, timeoutMs);\n\n    promise\n      .then((value) => {\n        clearTimeout(timer);\n        resolve(value);\n      })\n      .catch((error) => {\n        clearTimeout(timer);\n        reject(error);\n      });\n  });\n}\n\nfunction parseTimeValue(value) {\n  const raw = String(value || '').trim();\n  const [h, m] = raw.split(':');\n  const hour = Number(h);\n  const minute = Number(m);\n  if (!Number.isInteger(hour) || !Number.isInteger(minute)) {\n    return { hour: 9, minute: 0 };\n  }\n  return {\n    hour: Math.min(23, Math.max(0, hour)),\n    minute: Math.min(59, Math.max(0, minute)),\n  };\n}\n\nfunction schedulerPayloadFromForm() {\n  const { hour, minute } = parseTimeValue(scheduleTimeEl.value);\n  return {\n    enabled: !!scheduleEnabledEl.checked,\n    frequency: String(scheduleFrequencyEl.value || 'daily'),\n    hour,\n    minute,\n    weekday: Number(scheduleWeekdayEl.value || 1),\n    interval_hours: Number(scheduleIntervalHoursEl.value || 24),\n  };\n}\n\nfunction schedulerSummaryText(scheduler) {\n  if (!scheduler) return i18n.t('settings.loading');\n\n  const autoText = scheduler.active\n    ? i18n.t('settings.summaryAutoEnabled')\n    : i18n.t('settings.summaryAutoDisabled');\n  const brewText = isBrewPathConfigured()\n    ? i18n.t('settings.summaryBrewPathSet')\n    : i18n.t('settings.summaryBrewPathMissing');\n\n  let appStatusText = i18n.t('settings.summaryAppUnknown');\n  if (appUpdateMeta.status === 'up-to-date') {\n    appStatusText = i18n.t('settings.summaryAppUpToDate');\n  } else if (appUpdateMeta.status === 'update-available') {\n    appStatusText = i18n.t('settings.summaryAppUpdateAvailable');\n  } else if (appUpdateMeta.status === 'error') {\n    appStatusText = i18n.t('settings.summaryAppCheckFailed');\n  }\n\n  const checkedText = appUpdateMeta.checkedAt\n    ? i18n.t('settings.summaryAppUpdateCheckedAt', { date: formatDate(appUpdateMeta.checkedAt) })\n    : i18n.t('settings.summaryAppUpdateNeverChecked');\n\n  return [autoText, brewText, appStatusText, checkedText].join(' • ');\n}\n\nfunction renderSettingsSummary() {\n  const scheduler = settingsState?.scheduler;\n  if (!scheduler) {\n    settingsSummaryEl.style.color = 'var(--muted)';\n    settingsSummaryEl.textContent = i18n.t('settings.loading');\n    return;\n  }\n\n  settingsSummaryEl.style.color = 'var(--muted)';\n  settingsSummaryEl.textContent = schedulerSummaryText(scheduler);\n}\n\nfunction updateSchedulerFieldVisibility() {\n  const enabled = !!scheduleEnabledEl.checked;\n  const frequency = String(scheduleFrequencyEl.value || 'daily');\n\n  scheduleFrequencyEl.disabled = !enabled;\n  scheduleTimeEl.disabled = !enabled;\n  scheduleWeekdayEl.disabled = !enabled;\n  scheduleIntervalHoursEl.disabled = !enabled;\n\n  scheduleTimeRowEl.classList.toggle('hidden', frequency === 'interval');\n  scheduleWeekdayRowEl.classList.toggle('hidden', frequency !== 'weekly');\n  scheduleIntervalRowEl.classList.toggle('hidden', frequency !== 'interval');\n}\n\nfunction renderSettings() {\n  const policy = settingsState?.settings || {};\n  document.getElementById('binaryOnlyInput').checked = policy.binary_only !== false;\n  document.getElementById('excludedPackagesInput').value = (policy.excluded_packages || []).join(', ');\n  const scheduler = settingsState?.scheduler;\n  const brewPath = String(settingsState?.settings?.brew_path || '');\n\n  if (!scheduler) {\n    settingsSummaryEl.textContent = i18n.t('settings.loading');\n    setInlineStatus(scheduleStatusEl, '');\n    setInlineStatus(brewPathStatusEl, '');\n    return;\n  }\n\n  scheduleEnabledEl.checked = !!scheduler.enabled;\n  scheduleFrequencyEl.value = String(scheduler.frequency || 'daily');\n  scheduleTimeEl.value = `${String(Number(scheduler.hour ?? 9)).padStart(2, '0')}:${String(Number(scheduler.minute ?? 0)).padStart(2, '0')}`;\n  scheduleWeekdayEl.value = String(Number(scheduler.weekday ?? 1));\n  scheduleIntervalHoursEl.value = String(Number(scheduler.interval_hours ?? 24));\n\n  brewPathInputEl.value = brewPath;\n  renderSettingsSummary();\n  updateSchedulerFieldVisibility();\n  applyActionAvailability();\n}\n\nasync function loadSettings() {\n  try {\n    const payload = await window.brewApp.getSettings();\n    settingsState = payload;\n    renderSettings();\n  } catch (err) {\n    settingsSummaryEl.textContent = i18n.t('settings.schedulerFailed', { error: err.message });\n    settingsSummaryEl.style.color = 'var(--danger)';\n  }\n}\n\nasync function saveScheduler() {\n  const scheduler = schedulerPayloadFromForm();\n  setInlineStatus(scheduleStatusEl, i18n.t('settings.loading'));\n\n  try {\n    const payload = await window.brewApp.updateScheduler(scheduler);\n    settingsState = {\n      ...(settingsState || {}),\n      scheduler: payload.scheduler,\n      settings: {\n        ...(settingsState?.settings || {}),\n      },\n    };\n    renderSettings();\n    setInlineStatus(scheduleStatusEl, i18n.t('settings.schedulerSaved'));\n  } catch (err) {\n    setInlineStatus(scheduleStatusEl, i18n.t('settings.schedulerFailed', { error: err.message }), true);\n  }\n}\n\nasync function saveBrewPath() {\n  const raw = String(brewPathInputEl.value || '').trim();\n  setInlineStatus(brewPathStatusEl, i18n.t('settings.loading'));\n\n  try {\n    const payload = await window.brewApp.updateBrewPath(raw);\n    settingsState = {\n      ...(settingsState || {}),\n      settings: {\n        ...(settingsState?.settings || {}),\n        brew_path: payload.brew_path || '',\n      },\n      scheduler: settingsState?.scheduler,\n    };\n    brewPathInputEl.value = String(payload.brew_path || '');\n    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaved'));\n    renderSettingsSummary();\n    applyActionAvailability();\n    await loadState();\n  } catch (err) {\n    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaveFailed', { error: err.message }), true);\n  }\n}\n\nasync function clearBrewPath() {\n  brewPathInputEl.value = '';\n  await saveBrewPath();\n  setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathCleared'));\n  snapshot = { counts: {}, packages: [] };\n  selectedPackageKeys.clear();\n  renderCards(snapshot);\n  renderTable();\n  updatedAtEl.textContent = '';\n  showMessage(i18n.t('message.brewPathRequired'), true);\n  applyActionAvailability();\n}\n\nasync function detectBrewPath() {\n  setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathScanning'));\n\n  try {\n    const payload = await window.brewApp.autoDetectBrewPath();\n    const path = String(payload?.recommended_path || '');\n    const count = i18n.formatNumber((payload?.candidates || []).length);\n    const scanTime = payload?.scan_timestamp ? formatDate(payload.scan_timestamp) : i18n.t('common.unavailable');\n\n    if (!path) {\n      setInlineStatus(\n        brewPathStatusEl,\n        i18n.t('settings.brewPathDetectNone', { count, time: scanTime }),\n        true,\n      );\n      return;\n    }\n\n    setInlineStatus(\n      brewPathStatusEl,\n      `${i18n.t('settings.brewPathDetected', { path })}\\n${i18n.t('settings.brewPathScanSummary', { count, time: scanTime })}`,\n    );\n    if (confirm(i18n.t('settings.useDetectedConfirm', { path }))) {\n      brewPathInputEl.value = path;\n      await saveBrewPath();\n    }\n  } catch (err) {\n    setInlineStatus(brewPathStatusEl, i18n.t('settings.brewPathSaveFailed', { error: err.message }), true);\n  }\n}\n\nfunction resetLoaderProgress() {\n  loaderProgressEl.style.display = 'none';\n  loaderProgressMetaEl.textContent = '';\n  loaderProgressFillEl.style.width = '0%';\n  if (loaderPercentEl) loaderPercentEl.textContent = '0%';\n  if (loaderPhaseValueEl) loaderPhaseValueEl.textContent = i18n.t('progress.phase.idle');\n  if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = i18n.t('progress.currentNone');\n  if (loaderEtaValueEl) loaderEtaValueEl.textContent = i18n.t('common.dash');\n  loaderStepEls.forEach((stepEl) => {\n    stepEl.classList.remove('active', 'done');\n  });\n\n  if (loaderCancelBtn) {\n    loaderCancelBtn.disabled = true;\n  }\n\n  if (updateLivePanelEl) {\n    updateLivePanelEl.classList.remove('visible');\n  }\n  if (updateLiveTotalEl) updateLiveTotalEl.textContent = '0';\n  if (updateLiveCompletedEl) updateLiveCompletedEl.textContent = '0';\n  if (updateLiveRemainingEl) updateLiveRemainingEl.textContent = '0';\n  if (updateLiveFailedEl) updateLiveFailedEl.textContent = '0';\n  renderSimpleProgressList(updateLiveCompletedItemsEl, []);\n  renderSimpleProgressList(updateLiveRemainingItemsEl, []);\n  renderFailedProgressList(updateLiveFailedItemsEl, []);\n}\n\nfunction formatEta(seconds) {\n  const total = Math.max(0, Number(seconds || 0));\n  if (!Number.isFinite(total) || total <= 0) {\n    return '';\n  }\n  if (total < 60) {\n    return `${i18n.formatNumber(total)}s`;\n  }\n  const mins = Math.floor(total / 60);\n  const secs = total % 60;\n  if (mins >= 60) {\n    const hours = Math.floor(mins / 60);\n    const remMins = mins % 60;\n    return `${i18n.formatNumber(hours)}h ${i18n.formatNumber(remMins)}m`;\n  }\n  return `${i18n.formatNumber(mins)}m ${i18n.formatNumber(secs)}s`;\n}\n\nfunction progressPhaseLabel(phase) {\n  const key = `progress.phase.${String(phase || 'idle')}`;\n  const translated = i18n.t(key);\n  return translated === key ? i18n.t('progress.phase.idle') : translated;\n}\n\nfunction renderLoaderSteps(phase) {\n  const currentIndex = LOADER_PHASE_ORDER.indexOf(String(phase || 'idle'));\n  loaderStepEls.forEach((stepEl) => {\n    stepEl.classList.remove('active', 'done');\n    const step = String(stepEl.dataset.step || '').trim();\n    const stepIndex = LOADER_PHASE_ORDER.indexOf(step);\n    if (stepIndex === -1 || currentIndex === -1) return;\n\n    if (stepIndex < currentIndex) {\n      stepEl.classList.add('done');\n      return;\n    }\n\n    if (stepIndex === currentIndex) {\n      stepEl.classList.add('active');\n    }\n  });\n}\n\nfunction renderCheckProgress(progress) {\n  if (!progress || !acceptOperationProgress(progress) || !loaderEl) {\n    return;\n  }\n\n  lastKnownOperationRunning = !!progress.running;\n  lastKnownOperationName = String(progress.operation_name || '').trim().toLowerCase();\n  if (progress.running) {\n    loaderEl.classList.add('visible');\n  }\n\n  if (!progress.running) {\n    renderUpdateLivePanel(progress);\n    if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = '—';\n    if (loaderEtaValueEl) loaderEtaValueEl.textContent = progress.phase === 'completed' ? i18n.t('progress.doneNow') : '—';\n    if (loaderCancelBtn) loaderCancelBtn.disabled = true;\n    presentOperationStatus();\n    return;\n  }\n\n  loaderProgressEl.style.display = 'block';\n\n  const done = Number(progress.done || 0);\n  const total = Number(progress.total || 0);\n  const percent = Number(progress.percent ?? (total > 0 ? Math.round((done / total) * 100) : 0));\n  const clampedPercent = Math.max(0, Math.min(100, percent));\n  loaderProgressFillEl.style.width = `${clampedPercent}%`;\n\n  const phaseLabel = progressPhaseLabel(progress.phase);\n  const countLabel = total > 0\n    ? i18n.t('progress.summary', {\n      done: i18n.formatNumber(done),\n      total: i18n.formatNumber(total),\n      percent: i18n.formatNumber(clampedPercent),\n    })\n    : i18n.t('progress.summaryUnknown');\n  const etaText = typeof progress.eta_seconds === 'number' && progress.eta_seconds > 0\n    ? i18n.t('progress.eta', { eta: formatEta(progress.eta_seconds) })\n    : (progress.running ? i18n.t('progress.noEta') :\n        (progress.phase === 'completed' ? i18n.t('progress.doneNow') : '—'));\n  const current = String(progress.current_package || '').trim();\n\n  loaderTextEl.textContent = progress.message || i18n.t('message.runningCheck');\n  if (loaderPercentEl) loaderPercentEl.textContent = `${clampedPercent}%`;\n  if (loaderPhaseValueEl) loaderPhaseValueEl.textContent = phaseLabel;\n  if (loaderCurrentValueEl) loaderCurrentValueEl.textContent = current || (progress.running ? i18n.t('progress.currentNone') : '—');\n  if (loaderEtaValueEl) loaderEtaValueEl.textContent = etaText;\n\n  if (loaderCancelBtn) {\n    const operationName = String(progress.operation_name || '').trim().toLowerCase();\n    const canCancel = !!progress.running && !!operationName;\n    const cancelAlreadyRequested = !!progress.cancel_requested;\n    loaderCancelBtn.disabled = !canCancel || cancelInFlight || cancelAlreadyRequested;\n  }\n\n  renderLoaderSteps(progress.phase);\n  renderUpdateLivePanel(progress);\n\n  loaderProgressMetaEl.textContent = countLabel;\n\n  if (progress.running) {\n    setOperationStatusBadge('running');\n  } else if (String(progress.phase || '') === 'error') {\n    setOperationStatusBadge('failed');\n  } else if (String(progress.phase || '') === 'completed' || String(progress.phase || '') === 'canceled') {\n    setOperationStatusBadge('completed');\n  } else {\n    setOperationStatusBadge('idle');\n  }\n}\n\nlet progressPollEpoch = 0;\n\nasync function pollCheckProgress() {\n  const epoch = progressPollEpoch;\n  if (progressPollInFlight) return;\n  progressPollInFlight = true;\n  try {\n    const payload = await window.brewApp.getProgress();\n    if (epoch !== progressPollEpoch) return;\n    if (payload?.progress) {\n      if (!acceptOperationProgress(payload.progress)) return;\n      renderCheckProgress(payload.progress);\n    }\n    await syncParallelCheckState(payload?.parallel_check, { showFeedback: false });\n    if (!busyLoading && !payload?.progress?.running && !payload?.parallel_check?.running && payload?.progress) stopProgressPolling();\n  } catch {\n    if (epoch === progressPollEpoch) statusUnavailable();\n  } finally {\n    if (epoch === progressPollEpoch) progressPollInFlight = false;\n  }\n}\n\nfunction startProgressPolling() {\n  stopProgressPolling();\n  pollCheckProgress();\n  progressPollTimer = setInterval(pollCheckProgress, 750);\n}\n\nfunction stopProgressPolling() {\n  progressPollEpoch += 1;\n  if (progressPollTimer) {\n    clearInterval(progressPollTimer);\n    progressPollTimer = null;\n  }\n  progressPollInFlight = false;\n}\n\nasync function cancelCurrentOperation() {\n  if (cancelInFlight) return;\n  cancelInFlight = true;\n\n  if (loaderCancelBtn) {\n    loaderCancelBtn.disabled = true;\n  }\n\n  try {\n    const payload = await window.brewApp.cancelOperation();\n    if (payload?.stale_progress_cleared) {\n      showMessage(i18n.t('message.staleProgressCleared'));\n    } else {\n      showMessage(i18n.t('message.cancelRequestSent'));\n    }\n  } catch (err) {\n    showMessage(i18n.t('message.cancelRequestFailed', { error: formatOperationErrorMessage(err) }), true);\n  } finally {\n    cancelInFlight = false;\n    await pollCheckProgress();\n  }\n}\n\nfunction formatDate(value) {\n  return i18n.formatDate(value);\n}\n\nfunction updateTableFilterControls() {\n  if (packageSearchInputEl) {\n    packageSearchInputEl.placeholder = i18n.t('table.searchPlaceholder');\n    packageSearchInputEl.setAttribute('aria-label', i18n.t('table.searchLabel'));\n  }\n\n  if (packageTypeFilterEl) {\n    packageTypeFilterEl.value = packageTypeFilter;\n  }\n\n  if (packageStatusFilterEl) {\n    packageStatusFilterEl.value = packageStatusFilter;\n  }\n\n  if (packageSearchClearBtnEl) {\n    packageSearchClearBtnEl.disabled = !packageSearchTerm;\n  }\n\n  if (packageFilterResetBtnEl) {\n    const hasAnyFilter = !!packageSearchTerm || packageTypeFilter !== 'all' || packageStatusFilter !== 'all';\n    packageFilterResetBtnEl.disabled = !hasAnyFilter;\n  }\n}\n\nfunction updateHistoryFilterControls() {\n  if (historySearchInputEl) {\n    historySearchInputEl.placeholder = i18n.t('history.searchPlaceholder');\n    historySearchInputEl.setAttribute('aria-label', i18n.t('history.searchLabel'));\n  }\n\n  if (historyKindFilterEl) {\n    historyKindFilterEl.value = historyKindFilter;\n  }\n\n  if (historyStatusFilterEl) {\n    historyStatusFilterEl.value = historyStatusFilter;\n  }\n\n  if (historySortSelectEl) {\n    historySortSelectEl.value = historySortBy;\n  }\n\n  if (historyFiltersClearBtnEl) {\n    const hasAnyFilter = !!historySearchTerm\n      || historyKindFilter !== 'all'\n      || historyStatusFilter !== 'all'\n      || historySortBy !== 'newest';\n    historyFiltersClearBtnEl.disabled = !hasAnyFilter;\n  }\n}\n\nfunction updateSearchControls() {\n  updateTableFilterControls();\n  updateHistoryFilterControls();\n}\n\nfunction packageMatchesSearch(pkg, normalizedTerm) {\n  if (!normalizedTerm) return true;\n  const text = [\n    pkg?.name,\n    pkg?.description,\n    pkg?.description_he,\n    pkg?.kind,\n    pkg?.installed_version,\n    pkg?.latest_version,\n  ]\n    .map((value) => String(value || '').toLowerCase())\n    .join(' ');\n  return text.includes(normalizedTerm);\n}\n\nfunction packageKindValue(pkg) {\n  return String(pkg?.kind || 'formula').trim().toLowerCase();\n}\n\n\nfunction packageSelectionKey(name, kind) {\n  const normalizedName = String(name || '').trim();\n  const normalizedKind = String(kind || '').trim().toLowerCase();\n  if (!normalizedName || !normalizedKind) return '';\n  return `${normalizedKind}:${normalizedName}`;\n}\n\nfunction packageSelectionKeyFromPkg(pkg) {\n  return String(pkg?.update_decision?.source_installation_id || pkg?.installation_id || packageSelectionKey(pkg?.name, pkg?.kind));\n}\nfunction unifiedReady(pkg) {\n  return pkg?.update_decision?.status === 'ready';\n}\nfunction unifiedPayload(packages) {\n  if (!snapshot?.unified_plan_id) return null;\n  if (packages.some(p => !unifiedReady(p))) throw new Error(\n    i18n.lang === 'he' ? 'החבילה חסומה. יש לבצע בדיקה חדשה.' : 'Package is blocked. Run another check.');\n  if (!globalThis.crypto?.randomUUID) throw new Error('Secure request identifiers are unavailable');\n  return {\n    request_id: globalThis.crypto.randomUUID().replaceAll('-', ''),\n    plan_id: snapshot.unified_plan_id,\n    decisions: packages.map(p => ({\n      decision_id: p.update_decision.decision_id,\n      material_fingerprint: p.update_decision.material_fingerprint,\n    })),\n  };\n}\n\nfunction selectedPackagesFromSnapshot() {\n  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n  return pkgs\n    .filter((pkg) => !pkg?.excluded && !pkg?.update_block_reason && pkg?.outdated && (!snapshot?.unified_plan_id || unifiedReady(pkg)) && selectedPackageKeys.has(packageSelectionKeyFromPkg(pkg)))\n    .map((pkg) => ({\n      name: String(pkg?.name || '').trim(),\n      kind: packageKindValue(pkg),\n      installation_id: pkg?.installation_id,\n      update_decision: pkg?.update_decision,\n    }))\n    .filter((pkg) => pkg.name && (pkg.kind === 'formula' || pkg.kind === 'cask' || pkg.kind === 'port' || pkg.kind === 'tool'));\n}\n\nfunction pruneSelectedPackages() {\n  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n  const validKeys = new Set(\n    pkgs\n      .filter((pkg) => pkg?.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg)))\n      .map((pkg) => packageSelectionKeyFromPkg(pkg))\n      .filter(Boolean),\n  );\n\n  let changed = false;\n  Array.from(selectedPackageKeys).forEach((key) => {\n    if (!validKeys.has(key)) {\n      selectedPackageKeys.delete(key);\n      changed = true;\n    }\n  });\n\n  return changed;\n}\n\nfunction updateSelectionSummary(filteredList = null) {\n  const selectedCount = selectedPackagesFromSnapshot().length;\n  const selectedCountText = i18n.formatNumber(selectedCount);\n\n  if (selectedSummaryEl) {\n    selectedSummaryEl.textContent = i18n.t('table.selectedCount', { count: selectedCountText });\n  }\n\n  if (updateSelectedBtn) {\n    updateSelectedBtn.textContent = i18n.t('actions.updateSelectedCount', { count: selectedCountText });\n  }\n\n  const visibleList = Array.isArray(filteredList) ? filteredList : getFilteredPackages();\n  const visibleOutdated = visibleList.filter((pkg) => pkg?.outdated && !pkg.excluded && !pkg.update_block_reason).length;\n  const operationRunning = !!lastKnownOperationRunning || operationStatusUnverified;\n\n  if (selectVisibleOutdatedBtn) {\n    selectVisibleOutdatedBtn.disabled = busyLoading || operationRunning || !isBrewPathConfigured() || visibleOutdated === 0;\n  }\n\n  if (clearSelectionBtn) {\n    clearSelectionBtn.disabled = busyLoading || operationRunning || selectedCount === 0;\n  }\n}\n\nfunction togglePackageSelected(pkg, shouldSelect) {\n  const key = packageSelectionKeyFromPkg(pkg);\n  if (!key) return;\n  if (shouldSelect) {\n    selectedPackageKeys.add(key);\n    return;\n  }\n  selectedPackageKeys.delete(key);\n}\n\nfunction selectVisibleOutdated() {\n  const visible = getFilteredPackages().filter((pkg) => pkg?.outdated && !pkg.excluded && !pkg.update_block_reason);\n  if (!visible.length) return;\n  visible.forEach((pkg) => {\n    const key = packageSelectionKeyFromPkg(pkg);\n    if (key) selectedPackageKeys.add(key);\n  });\n  renderTable();\n}\n\nfunction clearSelection() {\n  if (!selectedPackageKeys.size) return;\n  selectedPackageKeys.clear();\n  renderTable();\n}\n\nfunction packageMatchesTypeFilter(pkg) {\n  if (packageTypeFilter === 'all') return true;\n  return packageKindValue(pkg) === packageTypeFilter;\n}\n\nfunction packageMatchesStatusFilter(pkg) {\n  if (packageStatusFilter === 'all') return true;\n  if (packageStatusFilter === 'needs_update') return !!pkg?.outdated;\n  if (packageStatusFilter === 'up_to_date') return !pkg?.outdated;\n  return true;\n}\n\nfunction isSortableColumnKey(key) {\n  return SORTABLE_COLUMN_KEYS.has(String(key || '').trim());\n}\n\nfunction normalizeSortText(value) {\n  return String(value || '').trim();\n}\n\nfunction parseSortDate(value) {\n  const raw = String(value || '').trim();\n  if (!raw) return null;\n  const stamp = Date.parse(raw);\n  return Number.isFinite(stamp) ? stamp : null;\n}\n\nfunction compareTextValues(left, right, direction = 'asc') {\n  const a = normalizeSortText(left);\n  const b = normalizeSortText(right);\n  const cmp = a.localeCompare(b, i18n.lang === 'he' ? 'he' : 'en', {\n    numeric: true,\n    sensitivity: 'base',\n  });\n  return direction === 'desc' ? -cmp : cmp;\n}\n\nfunction compareNumberValues(left, right, direction = 'asc') {\n  const hasLeft = Number.isFinite(left);\n  const hasRight = Number.isFinite(right);\n\n  if (!hasLeft && !hasRight) return 0;\n  if (!hasLeft) return 1;\n  if (!hasRight) return -1;\n\n  const cmp = left === right ? 0 : (left > right ? 1 : -1);\n  return direction === 'desc' ? -cmp : cmp;\n}\n\nfunction comparePackagesByColumn(a, b, key, direction) {\n  let cmp = 0;\n\n  switch (key) {\n    case 'package':\n      cmp = compareTextValues(a?.name, b?.name, direction);\n      break;\n    case 'type':\n      cmp = compareTextValues(a?.kind, b?.kind, direction);\n      break;\n    case 'currentVersion':\n      cmp = compareTextValues(a?.installed_version, b?.installed_version, direction);\n      break;\n    case 'latestVersion':\n      cmp = compareTextValues(a?.latest_version, b?.latest_version, direction);\n      break;\n    case 'currentDate':\n      cmp = compareNumberValues(parseSortDate(a?.current_release_date), parseSortDate(b?.current_release_date), direction);\n      break;\n    case 'latestDate':\n      cmp = compareNumberValues(\n        parseSortDate(a?.latest_release_date || a?.release_date),\n        parseSortDate(b?.latest_release_date || b?.release_date),\n        direction,\n      );\n      break;\n    case 'daysOutdated': {\n      const daysA = Number(a?.days_since_outdated);\n      const daysB = Number(b?.days_since_outdated);\n      cmp = compareNumberValues(\n        Number.isFinite(daysA) ? daysA : null,\n        Number.isFinite(daysB) ? daysB : null,\n        direction,\n      );\n      break;\n    }\n    case 'status':\n      cmp = compareNumberValues(a?.outdated ? 0 : 1, b?.outdated ? 0 : 1, direction);\n      break;\n    default:\n      cmp = 0;\n      break;\n  }\n\n  if (cmp !== 0) return cmp;\n  return compareTextValues(a?.name, b?.name, 'asc');\n}\n\nfunction applyTableSort(list) {\n  const key = String(tableSortState?.key || '').trim();\n  const direction = tableSortState?.direction === 'desc' ? 'desc' : 'asc';\n\n  if (!key || !isSortableColumnKey(key)) {\n    return list;\n  }\n\n  return [...list].sort((a, b) => comparePackagesByColumn(a, b, key, direction));\n}\n\nfunction loadSavedTableSort() {\n  try {\n    const raw = localStorage.getItem(TABLE_SORT_STORAGE_KEY);\n    if (!raw) {\n      return { key: '', direction: 'asc' };\n    }\n\n    const parsed = JSON.parse(raw);\n    const key = String(parsed?.key || '').trim();\n    const direction = parsed?.direction === 'desc' ? 'desc' : 'asc';\n\n    if (!isSortableColumnKey(key)) {\n      return { key: '', direction };\n    }\n\n    return { key, direction };\n  } catch {\n    return { key: '', direction: 'asc' };\n  }\n}\n\nfunction saveTableSort() {\n  localStorage.setItem(TABLE_SORT_STORAGE_KEY, JSON.stringify({\n    key: String(tableSortState?.key || '').trim(),\n    direction: tableSortState?.direction === 'desc' ? 'desc' : 'asc',\n  }));\n}\n\nfunction toggleTableSort(key) {\n  if (!isSortableColumnKey(key)) {\n    return;\n  }\n\n  const normalizedKey = String(key);\n  if (tableSortState?.key === normalizedKey) {\n    tableSortState = {\n      key: normalizedKey,\n      direction: tableSortState.direction === 'asc' ? 'desc' : 'asc',\n    };\n  } else {\n    tableSortState = {\n      key: normalizedKey,\n      direction: 'asc',\n    };\n  }\n\n  saveTableSort();\n}\n\nfunction renderTableSortIndicators() {\n  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n\n  headers.forEach((th) => {\n    const key = String(th.dataset.colKey || '').trim();\n    const label = th.querySelector('.th-label');\n    const sortable = isSortableColumnKey(key);\n\n    th.classList.remove('sortable', 'sorted-asc', 'sorted-desc');\n\n    if (!sortable) {\n      th.removeAttribute('aria-sort');\n      if (label) {\n        label.dataset.sortIndicator = '';\n      }\n      return;\n    }\n\n    th.classList.add('sortable');\n    const isActive = tableSortState?.key === key;\n    const direction = tableSortState?.direction === 'desc' ? 'desc' : 'asc';\n\n    if (isActive) {\n      th.classList.add(direction === 'desc' ? 'sorted-desc' : 'sorted-asc');\n      th.setAttribute('aria-sort', direction === 'desc' ? 'descending' : 'ascending');\n    } else {\n      th.setAttribute('aria-sort', 'none');\n    }\n\n    if (label) {\n      label.dataset.sortIndicator = isActive\n        ? (direction === 'desc' ? ' ▼' : ' ▲')\n        : '';\n    }\n  });\n}\n\nfunction initTableSorting() {\n  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n\n  headers.forEach((th) => {\n    const key = String(th.dataset.colKey || '').trim();\n    if (!isSortableColumnKey(key)) {\n      return;\n    }\n\n    th.setAttribute('tabindex', '0');\n\n    const onSort = () => {\n      toggleTableSort(key);\n      renderTable();\n    };\n\n    th.addEventListener('click', (event) => {\n      if (event.target.closest('[data-resizer]')) {\n        return;\n      }\n      onSort();\n    });\n\n    th.addEventListener('keydown', (event) => {\n      if (event.key !== 'Enter' && event.key !== ' ') {\n        return;\n      }\n      event.preventDefault();\n      onSort();\n    });\n  });\n\n  renderTableSortIndicators();\n}\n\nfunction findPackageInSnapshot(name, kind) {\n  const list = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n  return list.find((pkg) => String(pkg?.name || '') === String(name || '') && String(pkg?.kind || '') === String(kind || ''));\n}\n\nfunction historyStatusMeta(item) {\n  if (!item?.ok) {\n    return { text: i18n.t('history.statusFail'), className: 'fail' };\n  }\n  if (item?.verified_latest) {\n    return { text: i18n.t('history.statusLatest'), className: 'ok' };\n  }\n  return { text: i18n.t('history.statusOk'), className: 'warn' };\n}\n\nfunction historyStatusKey(item) {\n  if (!item?.ok) return 'fail';\n  if (item?.verified_latest) return 'latest';\n  return 'pending';\n}\n\nfunction historyMatchesSearch(item, normalizedTerm) {\n  if (!normalizedTerm) return true;\n  const text = [\n    item?.name,\n    item?.kind,\n    item?.latest_version,\n    item?.installed_version,\n    historyStatusKey(item),\n  ]\n    .map((value) => String(value || '').toLowerCase())\n    .join(' ');\n  return text.includes(normalizedTerm);\n}\n\nfunction historyMatchesKindFilter(item) {\n  if (historyKindFilter === 'all') return true;\n  return String(item?.kind || 'formula').trim().toLowerCase() === historyKindFilter;\n}\n\nfunction historyMatchesStatusFilterItem(item) {\n  if (historyStatusFilter === 'all') return true;\n  return historyStatusKey(item) === historyStatusFilter;\n}\n\nfunction historyTimestamp(item) {\n  const stamp = Date.parse(String(item?.timestamp || '').trim());\n  return Number.isFinite(stamp) ? stamp : 0;\n}\n\nfunction sortHistoryItems(items) {\n  const list = [...items];\n  switch (historySortBy) {\n    case 'oldest':\n      return list.sort((a, b) => historyTimestamp(a) - historyTimestamp(b));\n    case 'name_asc':\n      return list.sort((a, b) => compareTextValues(a?.name, b?.name, 'asc'));\n    case 'name_desc':\n      return list.sort((a, b) => compareTextValues(a?.name, b?.name, 'desc'));\n    case 'newest':\n    default:\n      return list.sort((a, b) => historyTimestamp(b) - historyTimestamp(a));\n  }\n}\n\nfunction getFilteredHistoryItems() {\n  const base = Array.isArray(updateHistoryItems) ? updateHistoryItems : [];\n  const filtered = base.filter((item) => {\n    if (!historyMatchesSearch(item, historySearchTerm)) return false;\n    if (!historyMatchesKindFilter(item)) return false;\n    if (!historyMatchesStatusFilterItem(item)) return false;\n    return true;\n  });\n\n  return sortHistoryItems(filtered);\n}\n\nfunction renderUpdateHistory(items = [], { filtered = false } = {}) {\n  if (!updateHistoryListEl) return;\n\n  updateHistoryListEl.innerHTML = '';\n  if (!Array.isArray(items) || items.length === 0) {\n    const empty = document.createElement('div');\n    empty.className = 'muted small';\n    empty.textContent = filtered ? i18n.t('history.emptyFiltered') : i18n.t('history.empty');\n    updateHistoryListEl.appendChild(empty);\n    return;\n  }\n\n  items.slice(0, 40).forEach((item) => {\n    const status = historyStatusMeta(item);\n\n    const root = document.createElement('div');\n    root.className = 'history-item';\n\n    const top = document.createElement('div');\n    top.className = 'history-top';\n\n    const name = document.createElement('div');\n    name.className = 'history-name';\n    name.textContent = String(item?.name || i18n.t('common.unknown'));\n\n    const when = document.createElement('div');\n    when.className = 'history-when';\n    when.textContent = formatDate(item?.timestamp);\n\n    top.appendChild(name);\n    top.appendChild(when);\n\n    const meta = document.createElement('div');\n    meta.className = 'history-meta';\n\n    const info = document.createElement('div');\n    const kindText = i18n.t('history.kind', { kind: kindLabel(item?.kind) });\n    const version = String(item?.latest_version || item?.installed_version || i18n.t('common.unknown'));\n    const versionText = i18n.t('history.version', { version });\n    info.textContent = `${kindText} • ${versionText}`;\n\n    const badge = document.createElement('span');\n    badge.className = `history-badge ${status.className}`;\n    badge.textContent = status.text;\n\n    meta.appendChild(info);\n    meta.appendChild(badge);\n\n    root.appendChild(top);\n    root.appendChild(meta);\n    updateHistoryListEl.appendChild(root);\n  });\n}\n\nfunction renderHistoryPanel() {\n  const hasFilters = !!historySearchTerm\n    || historyKindFilter !== 'all'\n    || historyStatusFilter !== 'all'\n    || historySortBy !== 'newest';\n\n  const items = getFilteredHistoryItems();\n  renderUpdateHistory(items, { filtered: hasFilters });\n\n  if (historyLoadInFlight) {\n    setUpdateHistoryStatus(i18n.t('history.loading'));\n  } else if (historyLoadError) {\n    setUpdateHistoryStatus(i18n.t('history.loadFailed', { error: historyLoadError }), true);\n  } else {\n    setUpdateHistoryStatus('');\n  }\n\n  updateHistoryFilterControls();\n}\n\nasync function loadUpdateHistory() {\n  historyLoadInFlight = true;\n  historyLoadError = '';\n  renderHistoryPanel();\n\n  try {\n    const payload = await withTimeout(\n      window.brewApp.getUpdateHistory(),\n      5000,\n      i18n.t('history.loadTimeout'),\n    );\n    updateHistoryItems = Array.isArray(payload?.items) ? payload.items : [];\n  } catch (err) {\n    historyLoadError = String(err?.message || i18n.t('common.unknown'));\n  } finally {\n    historyLoadInFlight = false;\n    renderHistoryPanel();\n  }\n}\n\nfunction formatDaysOutdated(pkg) {\n  if (!pkg?.outdated) {\n    return i18n.t('common.dash');\n  }\n  if (pkg.days_since_outdated === null || pkg.days_since_outdated === undefined) {\n    return i18n.t('common.unavailable');\n  }\n  return i18n.t('table.daysValue', { days: i18n.formatNumber(pkg.days_since_outdated) });\n}\n\nfunction kindLabel(kind) {\n  if (kind === 'port') return 'MacPorts';\n  if (kind === 'tool') return i18n.lang === 'he' ? 'כלי מנוהל' : 'Managed tool';\n  const value = String(kind || 'formula');\n  if (i18n.lang !== 'he') {\n    return value;\n  }\n  return value === 'cask' ? 'אפליקציית mac' : 'חבילת בנייה';\n}\n\nfunction packageDescription(pkg) {\n  const he = String(pkg?.description_he || '').trim();\n  const en = String(pkg?.description || '').trim();\n  if (i18n.lang === 'he') {\n    return he || en;\n  }\n  return en || he;\n}\n\nfunction skippedReasonLabel(reason) {\n  const value = String(reason || '').trim();\n  if (value === 'not_outdated') {\n    return i18n.t('message.skippedReasonNotOutdated');\n  }\n  return i18n.t('common.unknown');\n}\n\nfunction renderCards(data) {\n  const counts = data?.counts || {};\n  const cards = [\n    [i18n.t('cards.total'), counts.total ?? 0],\n    [i18n.t('cards.outdated'), counts.outdated_total ?? 0],\n    [i18n.t('cards.formulae'), counts.formulae ?? 0],\n    [i18n.t('cards.casks'), counts.casks ?? 0],\n    [i18n.t('cards.outdatedFormulae'), counts.outdated_formulae ?? 0],\n    [i18n.t('cards.outdatedCasks'), counts.outdated_casks ?? 0],\n  ];\n\n  cardsEl.innerHTML = '';\n  cards.forEach(([label, value]) => {\n    const div = document.createElement('div');\n    div.className = 'card';\n    div.innerHTML = `<div class=\"k\">${label}</div><div class=\"v\">${i18n.formatNumber(value)}</div>`;\n    cardsEl.appendChild(div);\n  });\n}\n\nfunction getFilteredPackages() {\n  const pkgs = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n  const byTab = activeTab === 'outdated'\n    ? pkgs.filter((pkg) => pkg.outdated)\n    : pkgs;\n\n  const byType = byTab.filter((pkg) => packageMatchesTypeFilter(pkg));\n  const byStatus = byType.filter((pkg) => packageMatchesStatusFilter(pkg));\n\n  const filtered = packageSearchTerm\n    ? byStatus.filter((pkg) => packageMatchesSearch(pkg, packageSearchTerm))\n    : byStatus;\n\n  return applyTableSort(filtered);\n}\n\nfunction renderTable() {\n  renderMacManager();\n  const deferred = (snapshot?.update_plan?.items || []).filter(p => !p.ready && (snapshot?.packages || []).some(x => x.outdated && x.name === p.name));\n  document.getElementById('updatePolicySummary').textContent = i18n.t('policy.summary', {\n    excluded: i18n.formatNumber(snapshot?.counts?.excluded_total || 0), managed: i18n.formatNumber(snapshot?.counts?.managed_total || 0), deferred: i18n.formatNumber(deferred.length),\n  });\n  renderTableSortIndicators();\n  pruneSelectedPackages();\n\n  const all = Array.isArray(snapshot?.packages) ? snapshot.packages : [];\n  const list = getFilteredPackages();\n  const outdatedCount = all.filter((pkg) => pkg.outdated).length;\n  const searchRaw = String(packageSearchInputEl?.value || '').trim();\n  const hasAdvancedFilters = packageTypeFilter !== 'all' || packageStatusFilter !== 'all';\n\n  if (packageSearchTerm) {\n    tableTitleEl.textContent = activeTab === 'outdated'\n      ? i18n.t('table.showingOutdatedSearch', {\n        count: i18n.formatNumber(list.length),\n        query: searchRaw,\n      })\n      : i18n.t('table.showingAllSearch', {\n        count: i18n.formatNumber(list.length),\n        query: searchRaw,\n      });\n  } else if (hasAdvancedFilters) {\n    tableTitleEl.textContent = activeTab === 'outdated'\n      ? i18n.t('table.showingOutdatedFiltered', { count: i18n.formatNumber(list.length) })\n      : i18n.t('table.showingAllFiltered', { count: i18n.formatNumber(list.length) });\n  } else {\n    tableTitleEl.textContent = activeTab === 'outdated'\n      ? i18n.t('table.showingOutdated', { count: i18n.formatNumber(list.length) })\n      : i18n.t('table.showingAll', {\n        total: i18n.formatNumber(all.length),\n        outdated: i18n.formatNumber(outdatedCount),\n      });\n  }\n\n  tbodyEl.innerHTML = '';\n  if (!list.length) {\n    const tr = document.createElement('tr');\n    const td = document.createElement('td');\n    td.colSpan = TABLE_COLUMN_COUNT;\n    td.className = 'muted';\n    if (packageSearchTerm) {\n      td.textContent = i18n.t('table.noSearchResults', { query: searchRaw || packageSearchTerm });\n    } else if (hasAdvancedFilters) {\n      td.textContent = i18n.t('table.noFilteredResults');\n    } else {\n      td.textContent = activeTab === 'outdated' ? i18n.t('table.noUpdates') : i18n.t('table.noPackages');\n    }\n    tr.appendChild(td);\n    tbodyEl.appendChild(tr);\n    updateSelectionSummary(list);\n    applyActionAvailability();\n    return;\n  }\n\n  list.forEach((pkg) => {\n    const tr = document.createElement('tr');\n\n    const tdName = document.createElement('td');\n    tdName.innerHTML = `\n      <div class=\"name-main\">${pkg.name || i18n.t('common.unknown')}</div>\n      <div class=\"name-sub\">${packageDescription(pkg)}</div>\n    `;\n\n    const tdKind = document.createElement('td');\n    tdKind.innerHTML = `<span class=\"kind\">${kindLabel(pkg.kind)}</span>`;\n    if (pkg.provider) {\n      const provider = document.createElement('div');\n      provider.className = 'small muted';\n      provider.textContent = i18n.t('policy.provider', { provider: pkg.provider });\n      tdKind.appendChild(provider);\n    }\n\n    const tdCurrentVersion = document.createElement('td');\n    tdCurrentVersion.className = 'mono small';\n    const tdCurrentVersionValue = document.createElement('bdi');\n    tdCurrentVersionValue.dir = pkg.installed_version && pkg.installed_version !== 'unknown' ? 'ltr' : 'auto';\n    tdCurrentVersionValue.textContent = pkg.installed_version || i18n.t('common.unknown');\n    tdCurrentVersion.appendChild(tdCurrentVersionValue);\n\n    const tdLatestVersion = document.createElement('td');\n    tdLatestVersion.className = 'mono small';\n    const tdLatestVersionValue = document.createElement('bdi');\n    tdLatestVersionValue.dir = pkg.latest_version && pkg.latest_version !== 'unknown' ? 'ltr' : 'auto';\n    tdLatestVersionValue.textContent = pkg.latest_version || i18n.t('common.unknown');\n    tdLatestVersion.appendChild(tdLatestVersionValue);\n\n    const tdCurrentDate = document.createElement('td');\n    tdCurrentDate.className = 'small';\n    tdCurrentDate.textContent = formatDate(pkg.current_release_date);\n\n    const tdLatestDate = document.createElement('td');\n    tdLatestDate.className = 'small';\n    tdLatestDate.textContent = formatDate(pkg.latest_release_date || pkg.release_date);\n\n    const tdDaysOutdated = document.createElement('td');\n    tdDaysOutdated.className = 'small mono';\n    tdDaysOutdated.textContent = formatDaysOutdated(pkg);\n\n    const tdStatus = document.createElement('td');\n    tdStatus.innerHTML = pkg.outdated\n      ? `<span class=\"status-warn\">${i18n.t('status.needsUpdate')}</span>`\n      : `<span class=\"status-ok\">${i18n.t('status.upToDate')}</span>`;\n    if (pkg.excluded) tdStatus.textContent = i18n.t('policy.excludedStatus');\n    else if (pkg.update_block_reason) tdStatus.textContent = i18n.t('policy.deferredStatus');\n\n    if (pkg.update_decision) {\n      const d = pkg.update_decision;\n      const info = document.createElement('div');\n      info.className = 'small muted'; info.dir = 'auto';\n      info.textContent = (d.current_provider === d.target_provider ? d.target_provider\n        : d.current_provider + ' → ' + d.target_provider) + ' · ' + d.status\n        + ' · ' + (d.reason_codes || []).join(', ');\n      tdStatus.appendChild(info);\n    }\n    if (pkg.alternative_status) {\n      const alternative = document.createElement('div'); alternative.className='small muted';\n      alternative.textContent = macAlternativeLabel(pkg.alternative_status);\n      if(pkg.alternative_target)alternative.textContent+=` · ${pkg.installed_version} → ${pkg.alternative_target}`;\n      alternative.title = pkg.alternative_detail || '';\n      tdStatus.appendChild(alternative);\n    }\n    const tdSelect = document.createElement('td');\n    if (pkg.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg))) {\n      const wrap = document.createElement('label');\n      wrap.className = 'row-select-wrap';\n\n      const checkbox = document.createElement('input');\n      checkbox.type = 'checkbox';\n      checkbox.className = 'row-select-checkbox';\n      checkbox.checked = selectedPackageKeys.has(packageSelectionKeyFromPkg(pkg));\n      checkbox.setAttribute('aria-label', i18n.t('table.selectPackage', { name: pkg.name || '' }));\n      checkbox.addEventListener('change', () => {\n        togglePackageSelected(pkg, checkbox.checked);\n        updateSelectionSummary(list);\n        applyActionAvailability();\n      });\n\n      wrap.appendChild(checkbox);\n      tdSelect.appendChild(wrap);\n    } else {\n      tdSelect.innerHTML = `<span class=\"muted small\">${i18n.t('common.dash')}</span>`;\n    }\n\n    const tdAction = document.createElement('td');\n    if (pkg.outdated && !pkg.excluded && !pkg.update_block_reason && (!snapshot?.unified_plan_id || unifiedReady(pkg))) {\n      const btn = document.createElement('button');\n      btn.className = 'row-update-btn';\n      btn.textContent = i18n.t('buttons.update');\n      btn.addEventListener('click', () => updateOne(pkg.name, pkg.kind, btn, pkg));\n      tdAction.appendChild(btn);\n    } else {\n      tdAction.innerHTML = `<span class=\"muted small\">${i18n.t('common.dash')}</span>`;\n    }\n\n    [\n      tdName,\n      tdKind,\n      tdCurrentVersion,\n      tdLatestVersion,\n      tdCurrentDate,\n      tdLatestDate,\n      tdDaysOutdated,\n      tdStatus,\n      tdSelect,\n      tdAction,\n    ].forEach((td) => tr.appendChild(td));\n\n    tbodyEl.appendChild(tr);\n  });\n\n  updateSelectionSummary(list);\n  applyActionAvailability();\n}\n\nfunction refreshStaticText() {\n  i18n.applyStatic();\n  applyLocalizedAttributes();\n  languageSelect.value = i18n.lang;\n  document.title = i18n.t('app.title');\n  if (snapshot?.updated_at) {\n    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n  }\n  renderCards(snapshot || { counts: {} });\n  renderTable();\n  renderSettings();\n  renderHistoryPanel();\n  renderAppUpdateStatus();\n  updateSearchControls();\n  applyActionAvailability();\n\n  const currentBadgeState = operationStatusBadgeEl?.classList?.contains('running')\n    ? 'running'\n    : operationStatusBadgeEl?.classList?.contains('error')\n      ? 'failed'\n      : operationStatusBadgeEl?.classList?.contains('completed')\n        ? 'completed'\n        : 'idle';\n  setOperationStatusBadge(currentBadgeState);\n}\n\nasync function loadState({ showLoader = true } = {}) {\n  clearMessage();\n\n  if (!isBrewPathConfigured()) {\n    snapshot = { counts: {}, packages: [] };\n    selectedPackageKeys.clear();\n    renderCards(snapshot);\n    renderTable();\n    updatedAtEl.textContent = '';\n    showMessage(i18n.t('message.brewPathRequired'), true);\n    applyActionAvailability();\n    return;\n  }\n\n  if (showLoader) {\n    setLoading(true, 'message.loadingState');\n  }\n  try {\n    snapshot = await window.brewApp.getState();\n    renderCards(snapshot);\n    renderTable();\n    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n  } catch (err) {\n    showMessage(i18n.t('message.loadFailed', { error: err.message }), true);\n  } finally {\n    if (showLoader) {\n      setLoading(false, 'message.loadingState');\n    }\n  }\n}\n\nfunction showCheckResult(state) {\n  showMessage(i18n.t(state?.partial ? 'message.checkPartial' : 'message.checkSuccess'), !!state?.partial);\n}\n\nasync function checkNow() {\n  if (!ensureBrewConfiguredForActions()) return;\n  clearMessage();\n\n  let liveProgress = null;\n  try {\n    const payload = await window.brewApp.getProgress();\n    liveProgress = payload?.progress || null;\n  } catch {\n    liveProgress = null;\n  }\n\n  if (liveProgress) {\n    lastKnownOperationRunning = !!liveProgress.running;\n    lastKnownOperationName = String(liveProgress.operation_name || '').trim().toLowerCase();\n    renderCheckProgress(liveProgress);\n    applyActionAvailability();\n  }\n\n  const updateRunning = !!liveProgress?.running\n    && String(liveProgress.operation_name || '').trim().toLowerCase().startsWith('update');\n\n  if (updateRunning) {\n    try {\n      const response = await window.brewApp.runCheckNow();\n      if (response?.parallel) {\n        if (response?.started) {\n          showMessage(i18n.t('message.checkParallelStarted'));\n        } else {\n          showMessage(i18n.t('message.checkParallelAlreadyRunning'));\n        }\n        await syncParallelCheckState(response?.parallel_check, { showFeedback: false });\n      } else if (response?.snapshot) {\n        snapshot = response.snapshot;\n        renderCards(snapshot);\n        renderTable();\n        updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n        showCheckResult(snapshot);\n      } else {\n        showMessage(i18n.t('message.statusRefreshed'));\n      }\n      await refreshRuntimeStatus({ showFeedback: false, reloadState: false });\n    } catch (err) {\n      showMessage(i18n.t('message.checkFailed', { error: formatOperationErrorMessage(err) }), true);\n    }\n    return;\n  }\n\n  setLoading(true, 'message.runningCheck');\n  beginOperationRequest();\n  const actionGeneration = operationRequestGeneration;\n  let operationResponse = null;\n  startProgressPolling();\n  try {\n    const res = operationResponse = await window.brewApp.runCheckNow();\n    if (res?.canceled) {\n      showMessage(i18n.t('message.checkCanceled'));\n      return;\n    }\n    snapshot = res.snapshot || res;\n    renderCards(snapshot);\n    renderTable();\n    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n    showCheckResult(snapshot);\n  } catch (err) {\n    showMessage(i18n.t('message.checkFailed', { error: formatOperationErrorMessage(err) }), true);\n    if (/fetch failed/i.test(String(err?.message || ''))) {\n      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n    }\n  } finally {\n    await finishOperationRequest(operationResponse, actionGeneration);\n    setLoading(false, 'message.loadingState');\n  }\n}\n\nasync function updateOne(name, kind, sourceBtn = null, row = null) {\n  if (!ensureBrewConfiguredForActions()) return;\n  const request = row && snapshot?.unified_plan_id ? unifiedPayload([row]) : null;\n  if (!request && !confirm(i18n.t('confirm.updateOne', { name, kind: kindLabel(kind) }))) return;\n\n  if (sourceBtn) {\n    sourceBtn.textContent = i18n.t('buttons.updating');\n  }\n\n  clearMessage();\n  showMessage(i18n.t('message.updatingOne', { name }));\n  setLoading(true, 'message.updatingOne', { name });\n  beginOperationRequest();\n  const actionGeneration = operationRequestGeneration;\n  let operationResponse = null;\n  startProgressPolling();\n\n  try {\n    const res = operationResponse = await window.brewApp.updateOne(name, kind, request);\n    snapshot = res.snapshot;\n    renderCards(snapshot);\n    renderTable();\n    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n\n    if (res?.result?.ok || (request && res?.ok)) {\n      let messageText = i18n.t('message.updateOneSuccess', { name });\n      if (res?.inventory_refresh_error) {\n        messageText = `${messageText}\\n${i18n.t('message.inventoryRefreshWarning', {\n          error: res.inventory_refresh_error,\n        })}`;\n      }\n      showMessage(messageText);\n\n      const pkg = findPackageInSnapshot(name, kind);\n      const verifiedLatest = !!(pkg && !pkg.outdated);\n      if (verifiedLatest) {\n        const version = pkg.latest_version || pkg.installed_version || i18n.t('common.unknown');\n        alert(i18n.t('message.updateOneLatestConfirmed', { name, version }));\n      }\n    } else {\n      const detail = res?.result?.stderr || res?.result?.stdout || i18n.t('common.unknown');\n      showMessage(i18n.t('message.updateOneFailed', { name, error: detail }), true);\n    }\n\n    await loadUpdateHistory();\n  } catch (err) {\n    showMessage(i18n.t('message.updateOneRequestFailed', { error: formatOperationErrorMessage(err) }), true);\n    if (/fetch failed/i.test(String(err?.message || ''))) {\n      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n    }\n  } finally {\n    await finishOperationRequest(operationResponse, actionGeneration);\n    setLoading(false, 'message.loadingState');\n    if (sourceBtn) {\n      sourceBtn.textContent = i18n.t('buttons.update');\n    }\n  }\n}\n\nasync function updateSelected() {\n  if (!ensureBrewConfiguredForActions()) return;\n\n  const selected = selectedPackagesFromSnapshot();\n  if (!selected.length) {\n    showMessage(i18n.t('message.noSelectedPackages'), true);\n    return;\n  }\n\n  const selectedRows = (snapshot?.packages || []).filter(pkg =>\n    selected.some(item => item.installation_id === pkg.installation_id));\n  const request = snapshot?.unified_plan_id ? unifiedPayload(selectedRows) : null;\n  if (!request && !confirm(i18n.t('confirm.updateSelected', { count: i18n.formatNumber(selected.length) }))) return;\n\n  if (updateSelectedBtn) {\n    updateSelectedBtn.textContent = i18n.t('buttons.updating');\n  }\n\n  clearMessage();\n  showMessage(i18n.t('message.updatingSelected', { count: i18n.formatNumber(selected.length) }));\n  setLoading(true, 'message.updatingSelected', { count: i18n.formatNumber(selected.length) });\n  beginOperationRequest();\n  const actionGeneration = operationRequestGeneration;\n  let operationResponse = null;\n  startProgressPolling();\n\n  try {\n    const res = operationResponse = await window.brewApp.updateSelected(request || selected);\n    snapshot = res.snapshot;\n    pruneSelectedPackages();\n    renderCards(snapshot);\n    renderTable();\n    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n\n    let messageText = i18n.t('message.updateSelectedSuccess', {\n      success: i18n.formatNumber(res.updated_count || 0),\n      failed: i18n.formatNumber(res.failed_count || 0),\n      skipped: i18n.formatNumber(res.skipped_count || 0),\n    });\n\n    const results = Array.isArray(res?.results) ? res.results : [];\n    const skippedResults = results.filter((item) => item?.skipped);\n    if (skippedResults.length > 0) {\n      const detailParts = skippedResults\n        .slice(0, 5)\n        .map((item) => `${String(item?.name || i18n.t('common.unknown'))} (${skippedReasonLabel(item?.reason)})`);\n\n      const remaining = skippedResults.length - detailParts.length;\n      if (remaining > 0) {\n        detailParts.push(i18n.t('message.moreItems', { count: i18n.formatNumber(remaining) }));\n      }\n\n      messageText = `${messageText}\\n${i18n.t('message.updateSelectedSkippedDetails', {\n        packages: detailParts.join(', '),\n      })}`;\n    }\n\n    if (res?.inventory_refresh_error) {\n      messageText = `${messageText}\n${i18n.t('message.inventoryRefreshWarning', {\n        error: res.inventory_refresh_error,\n      })}`;\n    }\n\n    showMessage(messageText, Number(res?.failed_count || 0) > 0);\n\n    const verifiedLatestCount = results.reduce((acc, result) => {\n      if (!result?.ok || result?.skipped) return acc;\n      const pkg = findPackageInSnapshot(result?.name, result?.kind);\n      return pkg && !pkg.outdated ? acc + 1 : acc;\n    }, 0);\n    if (verifiedLatestCount > 0) {\n      alert(i18n.t('message.updateSelectedLatestConfirmed', { count: i18n.formatNumber(verifiedLatestCount) }));\n    }\n\n    await loadUpdateHistory();\n  } catch (err) {\n    showMessage(i18n.t('message.updateSelectedFailed', { error: formatOperationErrorMessage(err) }), true);\n    if (/fetch failed/i.test(String(err?.message || ''))) {\n      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n    }\n  } finally {\n    await finishOperationRequest(operationResponse, actionGeneration);\n    setLoading(false, 'message.loadingState');\n    updateSelectionSummary();\n  }\n}\n\nasync function updateAll() {\n  if (!ensureBrewConfiguredForActions()) return;\n  const request = snapshot?.unified_plan_id ? unifiedPayload(\n    (snapshot?.packages || []).filter(unifiedReady)) : null;\n  if (!request && !confirm(i18n.t('confirm.updateAll'))) return;\n\n  updateAllBtn.textContent = i18n.t('buttons.updating');\n\n  clearMessage();\n  showMessage(i18n.t('message.updatingAll'));\n  setLoading(true, 'message.updatingAll');\n  beginOperationRequest();\n  const actionGeneration = operationRequestGeneration;\n  let operationResponse = null;\n  startProgressPolling();\n  try {\n    const res = operationResponse = await window.brewApp.updateAll(request);\n    snapshot = res.snapshot;\n    renderCards(snapshot);\n    renderTable();\n    updatedAtEl.textContent = i18n.t('updatedAt', { date: formatDate(snapshot.updated_at) });\n    showMessage(i18n.t('message.updateAllSuccess', {\n      success: i18n.formatNumber(res.updated_count || 0),\n      failed: i18n.formatNumber(res.failed_count || 0),\n    }));\n\n    if (res?.inventory_refresh_error) {\n      showMessage(`${msgEl.textContent}\\n${i18n.t('message.inventoryRefreshWarning', {\n        error: res.inventory_refresh_error,\n      })}`);\n    }\n\n    const results = Array.isArray(res?.results) ? res.results : [];\n    const verifiedLatestCount = results.reduce((acc, result) => {\n      if (!result?.ok) return acc;\n      const pkg = findPackageInSnapshot(result?.name, result?.kind);\n      return pkg && !pkg.outdated ? acc + 1 : acc;\n    }, 0);\n    if (verifiedLatestCount > 0) {\n      alert(i18n.t('message.updateAllLatestConfirmed', { count: i18n.formatNumber(verifiedLatestCount) }));\n    }\n\n    await loadUpdateHistory();\n  } catch (err) {\n    showMessage(i18n.t('message.updateAllFailed', { error: formatOperationErrorMessage(err) }), true);\n    if (/fetch failed/i.test(String(err?.message || ''))) {\n      await refreshRuntimeStatus({ showFeedback: true, reloadState: false });\n    }\n  } finally {\n    await finishOperationRequest(operationResponse, actionGeneration);\n    setLoading(false, 'message.loadingState');\n    updateAllBtn.textContent = i18n.t('actions.updateAll');\n  }\n}\n\nfunction loadSavedColumnWidths() {\n  try {\n    const raw = localStorage.getItem(COLUMN_WIDTH_STORAGE_KEY);\n    if (!raw) return {};\n    const parsed = JSON.parse(raw);\n    return parsed && typeof parsed === 'object' ? parsed : {};\n  } catch {\n    return {};\n  }\n}\n\nfunction saveColumnWidths() {\n  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n  const payload = {};\n  headers.forEach((th) => {\n    const key = String(th.dataset.colKey || '').trim();\n    if (!key) return;\n    payload[key] = Math.round(th.getBoundingClientRect().width);\n  });\n  localStorage.setItem(COLUMN_WIDTH_STORAGE_KEY, JSON.stringify(payload));\n}\n\nfunction initColumnResizing() {\n  const headers = Array.from(document.querySelectorAll('thead th[data-col-key]'));\n  const saved = loadSavedColumnWidths();\n\n  headers.forEach((th) => {\n    const key = String(th.dataset.colKey || '').trim();\n    if (!key) return;\n    const savedWidth = Number(saved[key]);\n    if (Number.isFinite(savedWidth) && savedWidth > 40) {\n      th.style.width = `${savedWidth}px`;\n    }\n\n    const handle = th.querySelector('[data-resizer]');\n    if (!handle) return;\n\n    handle.addEventListener('pointerdown', (event) => {\n      event.preventDefault();\n\n      const minWidth = Number(th.dataset.minWidth || 80);\n      const startX = event.clientX;\n      const startWidth = th.getBoundingClientRect().width;\n      const isRtl = document.documentElement.dir === 'rtl';\n      const direction = isRtl ? -1 : 1;\n\n      handle.classList.add('dragging');\n      document.body.style.cursor = 'col-resize';\n      document.body.style.userSelect = 'none';\n\n      const onMove = (moveEvent) => {\n        const delta = (moveEvent.clientX - startX) * direction;\n        const nextWidth = Math.max(minWidth, Math.round(startWidth + delta));\n        th.style.width = `${nextWidth}px`;\n      };\n\n      const onUp = () => {\n        document.removeEventListener('pointermove', onMove);\n        document.removeEventListener('pointerup', onUp);\n        handle.classList.remove('dragging');\n        document.body.style.cursor = '';\n        document.body.style.userSelect = '';\n        saveColumnWidths();\n      };\n\n      document.addEventListener('pointermove', onMove);\n      document.addEventListener('pointerup', onUp);\n    });\n  });\n}\n\ndocument.querySelectorAll('.tab').forEach((tab) => {\n  tab.addEventListener('click', () => {\n    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));\n    tab.classList.add('active');\n    activeTab = tab.dataset.tab;\n    renderTable();\n  });\n});\n\nlanguageSelect.addEventListener('change', () => {\n  i18n.setLang(languageSelect.value);\n  refreshStaticText();\n});\n\nif (packageSearchInputEl) {\n  packageSearchInputEl.addEventListener('input', () => {\n    packageSearchTerm = String(packageSearchInputEl.value || '').trim().toLowerCase();\n    updateSearchControls();\n    renderTable();\n  });\n}\n\nif (packageSearchClearBtnEl) {\n  packageSearchClearBtnEl.addEventListener('click', () => {\n    if (!packageSearchInputEl) return;\n    packageSearchInputEl.value = '';\n    packageSearchTerm = '';\n    updateSearchControls();\n    renderTable();\n    packageSearchInputEl.focus();\n  });\n}\n\nif (packageTypeFilterEl) {\n  packageTypeFilterEl.addEventListener('change', () => {\n    packageTypeFilter = String(packageTypeFilterEl.value || 'all');\n    updateSearchControls();\n    renderTable();\n  });\n}\n\nif (packageStatusFilterEl) {\n  packageStatusFilterEl.addEventListener('change', () => {\n    packageStatusFilter = String(packageStatusFilterEl.value || 'all');\n    updateSearchControls();\n    renderTable();\n  });\n}\n\nif (packageFilterResetBtnEl) {\n  packageFilterResetBtnEl.addEventListener('click', () => {\n    packageSearchTerm = '';\n    packageTypeFilter = 'all';\n    packageStatusFilter = 'all';\n    if (packageSearchInputEl) {\n      packageSearchInputEl.value = '';\n      packageSearchInputEl.focus();\n    }\n    updateSearchControls();\n    renderTable();\n  });\n}\n\nif (historySearchInputEl) {\n  historySearchInputEl.addEventListener('input', () => {\n    historySearchTerm = String(historySearchInputEl.value || '').trim().toLowerCase();\n    renderHistoryPanel();\n  });\n}\n\nif (historyKindFilterEl) {\n  historyKindFilterEl.addEventListener('change', () => {\n    historyKindFilter = String(historyKindFilterEl.value || 'all');\n    renderHistoryPanel();\n  });\n}\n\nif (historyStatusFilterEl) {\n  historyStatusFilterEl.addEventListener('change', () => {\n    historyStatusFilter = String(historyStatusFilterEl.value || 'all');\n    renderHistoryPanel();\n  });\n}\n\nif (historySortSelectEl) {\n  historySortSelectEl.addEventListener('change', () => {\n    historySortBy = String(historySortSelectEl.value || 'newest');\n    renderHistoryPanel();\n  });\n}\n\nif (historyFiltersClearBtnEl) {\n  historyFiltersClearBtnEl.addEventListener('click', () => {\n    historySearchTerm = '';\n    historyKindFilter = 'all';\n    historyStatusFilter = 'all';\n    historySortBy = 'newest';\n    if (historySearchInputEl) {\n      historySearchInputEl.value = '';\n      historySearchInputEl.focus();\n    }\n    renderHistoryPanel();\n  });\n}\n\ncheckNowBtn.addEventListener('click', checkNow);\nupdateAllBtn.addEventListener('click', updateAll);\nif (updateSelectedBtn) updateSelectedBtn.addEventListener('click', updateSelected);\nif (selectVisibleOutdatedBtn) selectVisibleOutdatedBtn.addEventListener('click', selectVisibleOutdated);\nif (clearSelectionBtn) clearSelectionBtn.addEventListener('click', clearSelection);\nif (refreshStatusBtn) refreshStatusBtn.addEventListener('click', () => refreshRuntimeStatus({ showFeedback: true, reloadState: true }));\nif (settingsBtn) settingsBtn.addEventListener('click', toggleSettingsModal);\nif (settingsModalCloseBtn) settingsModalCloseBtn.addEventListener('click', () => setSettingsModalOpen(false));\nif (settingsModalBackdropEl) settingsModalBackdropEl.addEventListener('click', () => setSettingsModalOpen(false));\nif (loaderCancelBtn) loaderCancelBtn.addEventListener('click', cancelCurrentOperation);\ncheckAppUpdateBtn.addEventListener('click', () => checkAppUpdate({ silent: false }));\ninstallAppUpdateBtn.addEventListener('click', installAppUpdate);\nscheduleEnabledEl.addEventListener('change', updateSchedulerFieldVisibility);\nscheduleFrequencyEl.addEventListener('change', updateSchedulerFieldVisibility);\nsaveScheduleBtn.addEventListener('click', saveScheduler);\ndetectBrewPathBtn.addEventListener('click', detectBrewPath);\nsaveBrewPathBtn.addEventListener('click', saveBrewPath);\nclearBrewPathBtn.addEventListener('click', clearBrewPath);\ndocument.addEventListener('keydown', (event) => {\n  if (event.key === 'Escape' && settingsModalOpen) {\n    setSettingsModalOpen(false);\n  }\n});\nwindow.addEventListener('beforeunload', () => {\n  stopAutoAppUpdateChecks();\n  if (typeof disposeAppUpdateProgress === 'function') {\n    disposeAppUpdateProgress();\n    disposeAppUpdateProgress = null;\n  }\n});\n\nmountRuntimePanels();\ninitColumnResizing();\ninitTableSorting();\nupdateSchedulerFieldVisibility();\nupdateSearchControls();\nsetOperationStatusBadge('idle');\nrefreshStaticText();\ninitAppUpdateProgressBridge();\n\nasync function initializeApp() {\n  await loadSettings();\n  await loadUpdateHistory();\n  applyActionAvailability();\n  await loadState();\n  await refreshRuntimeStatus({ showFeedback: false, reloadState: false });\n  renderAppUpdateStatus();\n  await checkAppUpdate({ silent: true });\n  startAutoAppUpdateChecks();\n}\n\ndocument.getElementById('saveUpdatePolicyBtn').addEventListener('click', async () => {\n  const status = document.getElementById('updatePolicyStatus');\n  try {\n    const excluded = document.getElementById('excludedPackagesInput').value.split(',').map(x => x.trim()).filter(Boolean);\n    const payload = await window.brewApp.updatePolicy({\n      excluded_packages: excluded, binary_only: document.getElementById('binaryOnlyInput').checked,\n    });\n    settingsState = { ...settingsState, settings: payload.settings };\n    status.textContent = i18n.t('policy.saved');\n    await loadState();\n  } catch (error) { status.textContent = error.message; }\n});\n\ninitializeApp();\n",
    "backend/brew-updates-service.py": "#!/usr/local/bin/python3\n\"\"\"Homebrew Update Manager backend service.\n\nFeatures:\n- Collect installed + outdated inventory (formulae + casks)\n- Compute both current-version and latest-version release dates\n- Optional EN->HE package description translation with local cache\n- Local API for desktop app actions\n- Daily check mode with clickable macOS notification\n\"\"\"\n\nfrom __future__ import annotations\n\nimport argparse\nimport hashlib\nfrom contextlib import contextmanager\nimport datetime as dt\nimport json\nimport os\nimport plistlib\nimport re\nimport shutil\nimport shlex\nimport signal\nimport subprocess\nimport sys\nimport threading\nimport time\nimport platform\nfrom dataclasses import dataclass\nfrom http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\nfrom pathlib import Path\nfrom typing import Any, Callable, Iterator\nfrom urllib.parse import quote\nfrom urllib.request import Request, urlopen\n\nsys.path.insert(0, str(Path(__file__).resolve().parent))\nimport update_routes as routes\nimport macports_provider\nimport mac_manager\nimport installation_evidence\nimport unified_updates\nimport operation_lock\nimport provider_ownership\nimport copy\n\ntry:\n    import fcntl\nexcept Exception:  # pragma: no cover - non-POSIX fallback\n    fcntl = None\n\nAPP_NAME = \"Mac Update Manager\"\nREQUIRED_PYTHON = (3, 14)\nDEFAULT_HOST = \"127.0.0.1\"\nDEFAULT_PORT = 8765\nRELEASE_CACHE_TTL_DAYS = 7\nMAX_TRANSLATIONS_PER_RUN = 40\nGITHUB_COMMITS_PER_FILE = 80\nMAX_GITHUB_FALLBACK_PER_RUN = 25\nVERSION_SEARCH_COMMITS_LIMIT = 60\nGITHUB_PATH_CANDIDATES_LIMIT = 5\nBREW_SCAN_TIMEOUT_SECONDS = 20\nBREW_SCAN_MAX_RESULTS = 50\n\nPROJECT_ROOT = Path(__file__).resolve().parents[1]\nBACKEND_STARTUP_SHA = hashlib.sha256(b''.join(\n    p.name.encode() + b'\\0' + hashlib.sha256(p.read_bytes()).digest()\n    for p in sorted((PROJECT_ROOT / 'backend').glob('*.py')))).hexdigest()\nPROCESS_STARTED_AT = now_start = time.time()\nif (PROJECT_ROOT / \"package.json\").exists():\n    DATA_DIR = PROJECT_ROOT / \"data\"\nelse:\n    DATA_DIR = Path.home() / \"Library\" / \"Application Support\" / \"Homebrew Update Manager\"\nDATA_DIR = Path(os.environ.get(\"BREW_UPDATE_DATA_DIR\", str(DATA_DIR)))\nLOG_DIR = DATA_DIR / \"logs\"\nCACHE_DIR = DATA_DIR / \"cache\"\n\nSTATE_FILE = DATA_DIR / \"state.json\"\nCHECK_LOG_FILE = LOG_DIR / \"brew-check.log\"\nSERVICE_LOG_FILE = LOG_DIR / \"brew-service.log\"\nRELEASE_CACHE_FILE = CACHE_DIR / \"release-dates.json\"\nDESCRIPTION_HE_CACHE_FILE = CACHE_DIR / \"description-he.json\"\nSETTINGS_FILE = DATA_DIR / \"settings.json\"\nUPDATE_HISTORY_FILE = DATA_DIR / \"update-history.json\"\nRUN_JOURNAL_FILE = DATA_DIR / \"last-update-run.json\"\nUPDATE_PLAN_FILE = DATA_DIR / \"update-plan.json\"\nTOOLS_ROOT = Path.home() / \".local\" / \"share\" / \"brew-update-manager\"\nUPDATE_HISTORY_LOCK_FILE = UPDATE_HISTORY_FILE.with_suffix(UPDATE_HISTORY_FILE.suffix + \".lock\")\nUPDATE_HISTORY_LOCK_TIMEOUT_SECONDS = 8.0\nOPEN_APP_SCRIPT = PROJECT_ROOT / \"tools\" / \"open-brew-updates-app.sh\"\n\nLAUNCH_AGENT_LABEL = \"com.local.brew-outdated-check\"\nLAUNCH_AGENT_FILE = Path.home() / \"Library\" / \"LaunchAgents\" / f\"{LAUNCH_AGENT_LABEL}.plist\"\nLAUNCH_AGENT_DOMAIN = f\"gui/{os.getuid()}\"\nLAUNCH_AGENT_SERVICE = f\"{LAUNCH_AGENT_DOMAIN}/{LAUNCH_AGENT_LABEL}\"\n\nOP_LOCK = threading.Lock()\nCURRENT_OPERATION_LOCK = threading.Lock()\nCURRENT_OPERATION: dict[str, Any] = {\n    \"running\": False,\n    \"name\": None,\n    \"started_at\": None,\n}\n\nCANCEL_STATE_LOCK = threading.Lock()\nCANCEL_STATE: dict[str, Any] = {\n    \"requested\": False,\n    \"requested_at\": None,\n}\n\nACTIVE_COMMAND_LOCK = threading.Lock()\nACTIVE_COMMAND: dict[str, Any] = {\n    \"process\": None,\n    \"args\": [],\n}\n\nCHECK_PROGRESS_LOCK = threading.Lock()\n\nDEFERRED_CHECK_LOCK = threading.Lock()\nDEFERRED_CHECK_STATE: dict[str, Any] = {\n    \"requested\": False,\n    \"requested_at\": None,\n    \"reason\": None,\n}\n\nPARALLEL_CHECK_LOCK = threading.Lock()\nPARALLEL_CHECK_STATE: dict[str, Any] = {\n    \"running\": False,\n    \"started_at\": None,\n    \"finished_at\": None,\n    \"last_error\": None,\n    \"reason\": None,\n}\n\n\ndef new_check_progress_state() -> dict[str, Any]:\n    return {\n        \"running\": False,\n        \"operation_name\": None,\n        \"phase\": \"idle\",\n        \"message\": \"idle\",\n        \"started_at\": None,\n        \"updated_at\": None,\n        \"completed_at\": None,\n        \"done\": 0,\n        \"total\": 0,\n        \"percent\": 0,\n        \"eta_seconds\": None,\n        \"current_package\": None,\n        \"package_activity\": None,\n        \"planned_packages\": [],\n        \"completed_packages\": [],\n        \"failed_packages\": [],\n        \"remaining_packages\": [],\n        \"cancel_requested\": False,\n        \"canceled\": False,\n        \"error\": None,\n    }\n\n\nCHECK_PROGRESS: dict[str, Any] = new_check_progress_state()\nUNSET = object()\n\nREPO_CACHE: dict[str, Path] = {}\n\nTAP_GITHUB_REPO = {\n    \"homebrew/core\": \"Homebrew/homebrew-core\",\n    \"homebrew/cask\": \"Homebrew/homebrew-cask\",\n}\n\nSAFE_NAME_RE = re.compile(r\"^[A-Za-z0-9@._+\\-]+$\")\n\nDEFAULT_SETTINGS: dict[str, Any] = {\n    \"brew_path\": \"\",\n    \"excluded_packages\": [],\n    \"binary_only\": True,\n    \"binary_tools\": [],\n    \"upgrade_timeout_seconds\": 0,\n    \"scheduler\": {\n        \"enabled\": False,\n        \"frequency\": \"daily\",  # daily | weekly | interval\n        \"hour\": 9,\n        \"minute\": 0,\n        \"weekday\": 1,  # 0=Sun .. 6=Sat\n        \"interval_hours\": 24,\n    },\n}\n\nVALID_SCHEDULER_FREQUENCIES = {\"daily\", \"weekly\", \"interval\"}\nMAX_UPDATE_HISTORY_ITEMS = 100\n\n\n@dataclass\nclass CommandResult:\n    code: int\n    stdout: str\n    stderr: str\n\n\nclass BusyError(RuntimeError):\n    \"\"\"Raised when another mutating operation is already running.\"\"\"\n\n\nclass OperationCancelledError(RuntimeError):\n    \"\"\"Raised when the current operation was cancelled by user request.\"\"\"\n\n\ndef now_iso() -> str:\n    return dt.datetime.now(dt.timezone.utc).isoformat()\n\n\ndef parse_iso_datetime(value: str | None) -> dt.datetime | None:\n    raw = str(value or \"\").strip()\n    if not raw:\n        return None\n    try:\n        parsed = dt.datetime.fromisoformat(raw)\n    except ValueError:\n        return None\n    if parsed.tzinfo is None:\n        parsed = parsed.replace(tzinfo=dt.timezone.utc)\n    return parsed.astimezone(dt.timezone.utc)\n\n\ndef read_check_progress() -> dict[str, Any]:\n    with CHECK_PROGRESS_LOCK:\n        return dict(CHECK_PROGRESS)\n\n\ndef read_current_operation() -> dict[str, Any]:\n    with CURRENT_OPERATION_LOCK:\n        return dict(CURRENT_OPERATION)\n\n\ndef read_parallel_check_state() -> dict[str, Any]:\n    with PARALLEL_CHECK_LOCK:\n        return dict(PARALLEL_CHECK_STATE)\n\n\ndef runtime_status_payload():\n    # Same lock order as operation startup: ownership first, then progress.\n    # A stale caller snapshot must never terminate a newly started operation.\n    with CURRENT_OPERATION_LOCK:\n        operation = dict(CURRENT_OPERATION)\n        progress = _normalize_stale_progress_locked(operation)\n    return {'ok':True,'operation':operation,'progress':progress,'parallel_check':read_parallel_check_state()}\n\n\ndef normalize_stale_progress_state(operation: dict[str, Any] | None = None) -> dict[str, Any]:\n    return runtime_status_payload()['progress']\n\n\ndef _normalize_stale_progress_locked(operation: dict[str, Any]) -> dict[str, Any]:\n    operation_state = operation if isinstance(operation, dict) else read_current_operation()\n    progress = read_check_progress()\n\n    if not progress.get(\"running\"):\n        return progress\n\n    if operation_state.get(\"running\"):\n        return progress\n\n    done = int(progress.get(\"done\") or 0)\n    total = int(progress.get(\"total\") or 0)\n    interrupted = total > 0 and done < total\n\n    set_check_progress(\n        running=False,\n        phase=\"error\" if interrupted else \"completed\",\n        message=(\n            \"Operation status was stale and has been reset\"\n            if interrupted\n            else \"Operation already finished\"\n        ),\n        eta_seconds=0,\n        completed_at=now_iso(),\n        cancel_requested=False,\n        current_package=None,\n        canceled=bool(progress.get(\"canceled\")),\n        error=(\n            str(progress.get(\"error\") or \"stale_progress_state_reset\")\n            if interrupted\n            else progress.get(\"error\")\n        ),\n    )\n    append_service_log(\"Stale progress state detected and normalized\")\n    return read_check_progress()\n\n\ndef is_cancel_requested() -> bool:\n    with CANCEL_STATE_LOCK:\n        return bool(CANCEL_STATE.get(\"requested\"))\n\n\ndef reset_cancel_state() -> None:\n    with CANCEL_STATE_LOCK:\n        CANCEL_STATE[\"requested\"] = False\n        CANCEL_STATE[\"requested_at\"] = None\n\n\ndef mark_cancel_requested() -> None:\n    with CANCEL_STATE_LOCK:\n        if not CANCEL_STATE.get(\"requested\"):\n            CANCEL_STATE[\"requested\"] = True\n            CANCEL_STATE[\"requested_at\"] = now_iso()\n\n\ndef request_deferred_check(reason: str) -> bool:\n    with DEFERRED_CHECK_LOCK:\n        already_requested = bool(DEFERRED_CHECK_STATE.get(\"requested\"))\n        if not already_requested:\n            DEFERRED_CHECK_STATE[\"requested\"] = True\n            DEFERRED_CHECK_STATE[\"requested_at\"] = now_iso()\n            DEFERRED_CHECK_STATE[\"reason\"] = str(reason or \"manual_check\")\n            return True\n        return False\n\n\ndef consume_deferred_check_request() -> dict[str, Any] | None:\n    with DEFERRED_CHECK_LOCK:\n        if not DEFERRED_CHECK_STATE.get(\"requested\"):\n            return None\n        payload = {\n            \"requested_at\": str(DEFERRED_CHECK_STATE.get(\"requested_at\") or \"\"),\n            \"reason\": str(DEFERRED_CHECK_STATE.get(\"reason\") or \"manual_check\"),\n        }\n        DEFERRED_CHECK_STATE[\"requested\"] = False\n        DEFERRED_CHECK_STATE[\"requested_at\"] = None\n        DEFERRED_CHECK_STATE[\"reason\"] = None\n        return payload\n\n\ndef _deferred_check_worker(context: dict[str, Any]) -> None:\n    reason = str(context.get(\"reason\") or \"manual_check\")\n    append_service_log(f\"Starting deferred check after update (reason={reason})\")\n    append_check_log(f\"Starting deferred check after update (reason={reason})\")\n\n    for attempt in range(1, 4):\n        try:\n            run_exclusive(\n                \"check_now_deferred\",\n                lambda: compute_snapshot(run_brew_update=True, track_progress=False),\n            )\n            append_service_log(\"Deferred check completed successfully\")\n            append_check_log(\"Deferred check completed successfully\")\n            return\n        except BusyError as exc:\n            append_service_log(f\"Deferred check attempt {attempt} busy: {exc}\")\n            time.sleep(0.8)\n        except Exception as exc:\n            append_service_log(f\"Deferred check failed: {exc}\")\n            append_check_log(f\"Deferred check failed: {exc}\")\n            return\n\n    append_service_log(\"Deferred check dropped after busy retries\")\n    append_check_log(\"Deferred check dropped after busy retries\")\n\n\ndef maybe_schedule_deferred_check(trigger_operation_name: str) -> None:\n    name = str(trigger_operation_name or \"\")\n    if not name.startswith(\"update\"):\n        return\n\n    context = consume_deferred_check_request()\n    if not context:\n        return\n\n    worker = threading.Thread(\n        target=_deferred_check_worker,\n        args=(context,),\n        daemon=True,\n    )\n    worker.start()\n\n\ndef _parallel_check_worker(*, run_brew_update: bool, reason: str) -> None:\n    error_text: str | None = None\n\n    append_service_log(f\"Parallel check started (reason={reason}, run_brew_update={run_brew_update})\")\n    append_check_log(f\"Parallel check started (reason={reason}, run_brew_update={run_brew_update})\")\n\n    try:\n        compute_snapshot(run_brew_update=run_brew_update, track_progress=False)\n    except Exception as exc:\n        error_text = str(exc)\n        append_service_log(f\"Parallel check failed: {error_text}\")\n        append_check_log(f\"Parallel check failed: {error_text}\")\n    finally:\n        with PARALLEL_CHECK_LOCK:\n            PARALLEL_CHECK_STATE[\"running\"] = False\n            PARALLEL_CHECK_STATE[\"finished_at\"] = now_iso()\n            PARALLEL_CHECK_STATE[\"last_error\"] = error_text\n\n        if not error_text:\n            append_service_log(\"Parallel check completed successfully\")\n            append_check_log(\"Parallel check completed successfully\")\n\n\ndef start_parallel_check(*, reason: str, run_brew_update: bool) -> tuple[bool, dict[str, Any]]:\n    with PARALLEL_CHECK_LOCK:\n        if PARALLEL_CHECK_STATE.get(\"running\"):\n            return False, dict(PARALLEL_CHECK_STATE)\n\n        PARALLEL_CHECK_STATE[\"running\"] = True\n        PARALLEL_CHECK_STATE[\"started_at\"] = now_iso()\n        PARALLEL_CHECK_STATE[\"finished_at\"] = None\n        PARALLEL_CHECK_STATE[\"last_error\"] = None\n        PARALLEL_CHECK_STATE[\"reason\"] = str(reason or \"manual_check\")\n\n        state_snapshot = dict(PARALLEL_CHECK_STATE)\n\n    worker = threading.Thread(\n        target=_parallel_check_worker,\n        kwargs={\n            \"run_brew_update\": bool(run_brew_update),\n            \"reason\": str(reason or \"manual_check\"),\n        },\n        daemon=True,\n    )\n    worker.start()\n    return True, state_snapshot\n\n\ndef set_check_progress(\n    *,\n    running: bool | None = None,\n    operation_name: str | None | object = UNSET,\n    phase: str | None = None,\n    message: str | None = None,\n    started_at: str | None = None,\n    completed_at: str | None = None,\n    done: int | None = None,\n    total: int | None = None,\n    eta_seconds: int | None | object = UNSET,\n    current_package: str | None | object = UNSET,\n    planned_packages: list[dict[str, Any]] | None | object = UNSET,\n    completed_packages: list[dict[str, Any]] | None | object = UNSET,\n    failed_packages: list[dict[str, Any]] | None | object = UNSET,\n    remaining_packages: list[dict[str, Any]] | None | object = UNSET,\n    cancel_requested: bool | None | object = UNSET,\n    canceled: bool | None | object = UNSET,\n    error: str | None | object = UNSET,\n) -> None:\n    with CHECK_PROGRESS_LOCK:\n        if running is not None:\n            CHECK_PROGRESS[\"running\"] = bool(running)\n        if operation_name is not UNSET:\n            CHECK_PROGRESS[\"operation_name\"] = operation_name\n        if phase is not None:\n            CHECK_PROGRESS[\"phase\"] = phase\n        if message is not None:\n            CHECK_PROGRESS[\"message\"] = message\n        if started_at is not None:\n            CHECK_PROGRESS[\"started_at\"] = started_at\n        if completed_at is not None:\n            CHECK_PROGRESS[\"completed_at\"] = completed_at\n        if done is not None:\n            CHECK_PROGRESS[\"done\"] = max(0, int(done))\n        if total is not None:\n            CHECK_PROGRESS[\"total\"] = max(0, int(total))\n        if eta_seconds is not UNSET:\n            if eta_seconds is None:\n                CHECK_PROGRESS[\"eta_seconds\"] = None\n            else:\n                CHECK_PROGRESS[\"eta_seconds\"] = max(0, int(eta_seconds))\n        if current_package is not UNSET:\n            CHECK_PROGRESS[\"current_package\"] = current_package\n        if planned_packages is not UNSET:\n            CHECK_PROGRESS[\"planned_packages\"] = list(planned_packages or [])\n        if completed_packages is not UNSET:\n            CHECK_PROGRESS[\"completed_packages\"] = list(completed_packages or [])\n        if failed_packages is not UNSET:\n            CHECK_PROGRESS[\"failed_packages\"] = list(failed_packages or [])\n        if remaining_packages is not UNSET:\n            CHECK_PROGRESS[\"remaining_packages\"] = list(remaining_packages or [])\n        if cancel_requested is not UNSET:\n            CHECK_PROGRESS[\"cancel_requested\"] = bool(cancel_requested)\n        if canceled is not UNSET:\n            CHECK_PROGRESS[\"canceled\"] = bool(canceled)\n        if error is not UNSET:\n            CHECK_PROGRESS[\"error\"] = error\n\n        progress_total = int(CHECK_PROGRESS.get(\"total\") or 0)\n        progress_done = int(CHECK_PROGRESS.get(\"done\") or 0)\n        if progress_total > 0:\n            percent = int(round(min(100.0, max(0.0, (progress_done / progress_total) * 100.0))))\n        else:\n            percent = 0\n        CHECK_PROGRESS[\"percent\"] = percent\n        CHECK_PROGRESS[\"updated_at\"] = now_iso()\n\n\ndef reset_check_progress_for_run() -> None:\n    started = now_iso()\n    with CHECK_PROGRESS_LOCK:\n        CHECK_PROGRESS.clear()\n        CHECK_PROGRESS.update(new_check_progress_state())\n        CHECK_PROGRESS[\"running\"] = True\n        CHECK_PROGRESS[\"phase\"] = \"starting\"\n        CHECK_PROGRESS[\"message\"] = \"Starting check\"\n        CHECK_PROGRESS[\"started_at\"] = started\n        CHECK_PROGRESS[\"updated_at\"] = started\n\n\ndef estimate_eta_seconds(started_at_iso: str | None, done: int, total: int) -> int | None:\n    started_at = parse_iso_datetime(started_at_iso)\n    if not started_at or done <= 0 or total <= 0 or done >= total:\n        return 0 if total > 0 and done >= total else None\n\n    elapsed = (dt.datetime.now(dt.timezone.utc) - started_at).total_seconds()\n    if elapsed <= 0:\n        return None\n\n    rate = done / elapsed\n    if rate <= 0:\n        return None\n\n    remaining = max(0.0, (total - done) / rate)\n    return int(round(remaining))\n\n\ndef estimate_eta_from_completed_packages(\n    completed_packages: list[dict[str, Any]],\n    total_packages: int,\n) -> int | None:\n    done = max(0, len(completed_packages or []))\n    remaining = max(0, int(total_packages or 0) - done)\n    if remaining <= 0:\n        return 0\n    if done <= 0:\n        return None\n\n    durations: list[float] = []\n    for item in completed_packages:\n        start = parse_iso_datetime(str(item.get(\"started_at\") or \"\"))\n        end = parse_iso_datetime(str(item.get(\"completed_at\") or \"\"))\n        if not start or not end:\n            continue\n        seconds = (end - start).total_seconds()\n        if seconds > 0:\n            durations.append(seconds)\n\n    if not durations:\n        return None\n\n    avg_seconds = sum(durations) / len(durations)\n    return int(round(max(0.0, avg_seconds * remaining)))\n\n\ndef compute_days_since(iso_value: str | None) -> int | None:\n    parsed = parse_iso_datetime(iso_value)\n    if not parsed:\n        return None\n    delta = dt.datetime.now(dt.timezone.utc) - parsed\n    if delta.total_seconds() <= 0:\n        return 0\n    return int(delta.days)\n\n\ndef ensure_python_314() -> None:\n    current = (sys.version_info.major, sys.version_info.minor)\n    if current != REQUIRED_PYTHON:\n        raise RuntimeError(\n            \"This service must run with Python 3.14. \"\n            f\"Current interpreter: {sys.executable} ({sys.version.split()[0]}).\"\n        )\n\n\ndef ensure_dirs() -> None:\n    DATA_DIR.mkdir(parents=True, exist_ok=True)\n    LOG_DIR.mkdir(parents=True, exist_ok=True)\n    CACHE_DIR.mkdir(parents=True, exist_ok=True)\n\n\ndef settings_defaults() -> dict[str, Any]:\n    # cheap deep-copy for nested defaults without importing copy\n    return json.loads(json.dumps(DEFAULT_SETTINGS))\n\n\ndef normalize_scheduler_payload(raw: Any) -> dict[str, Any]:\n    base = settings_defaults()[\"scheduler\"]\n    source = raw if isinstance(raw, dict) else {}\n\n    enabled = bool(source.get(\"enabled\", base[\"enabled\"]))\n    frequency = str(source.get(\"frequency\", base[\"frequency\"]) or base[\"frequency\"]).strip().lower()\n    if frequency not in VALID_SCHEDULER_FREQUENCIES:\n        raise ValueError(\"scheduler frequency must be one of: daily, weekly, interval\")\n\n    try:\n        hour = int(source.get(\"hour\", base[\"hour\"]))\n        minute = int(source.get(\"minute\", base[\"minute\"]))\n        weekday = int(source.get(\"weekday\", base[\"weekday\"]))\n        interval_hours = int(source.get(\"interval_hours\", base[\"interval_hours\"]))\n    except (TypeError, ValueError) as exc:\n        raise ValueError(\"scheduler numeric fields must be integers\") from exc\n\n    if hour < 0 or hour > 23:\n        raise ValueError(\"scheduler hour must be between 0 and 23\")\n    if minute < 0 or minute > 59:\n        raise ValueError(\"scheduler minute must be between 0 and 59\")\n    if weekday < 0 or weekday > 6:\n        raise ValueError(\"scheduler weekday must be between 0 (Sunday) and 6 (Saturday)\")\n    if interval_hours < 1 or interval_hours > 168:\n        raise ValueError(\"scheduler interval_hours must be between 1 and 168\")\n\n    return {\n        \"enabled\": enabled,\n        \"frequency\": frequency,\n        \"hour\": hour,\n        \"minute\": minute,\n        \"weekday\": weekday,\n        \"interval_hours\": interval_hours,\n    }\n\n\ndef read_launch_agent_scheduler() -> dict[str, Any] | None:\n    if not LAUNCH_AGENT_FILE.exists():\n        return None\n\n    try:\n        payload = plistlib.loads(LAUNCH_AGENT_FILE.read_bytes())\n    except Exception:\n        return None\n\n    schedule = settings_defaults()[\"scheduler\"]\n    schedule[\"enabled\"] = True\n\n    start_interval = payload.get(\"StartInterval\") if isinstance(payload, dict) else None\n    if isinstance(start_interval, int) and start_interval > 0:\n        schedule[\"frequency\"] = \"interval\"\n        schedule[\"interval_hours\"] = max(1, min(168, int(round(start_interval / 3600))))\n        return schedule\n\n    start_calendar = payload.get(\"StartCalendarInterval\") if isinstance(payload, dict) else None\n    if isinstance(start_calendar, dict):\n        try:\n            schedule[\"hour\"] = int(start_calendar.get(\"Hour\", schedule[\"hour\"]))\n            schedule[\"minute\"] = int(start_calendar.get(\"Minute\", schedule[\"minute\"]))\n        except (TypeError, ValueError):\n            pass\n\n        if \"Weekday\" in start_calendar:\n            schedule[\"frequency\"] = \"weekly\"\n            try:\n                wd = int(start_calendar.get(\"Weekday\", schedule[\"weekday\"]))\n                schedule[\"weekday\"] = wd if 0 <= wd <= 6 else schedule[\"weekday\"]\n            except (TypeError, ValueError):\n                pass\n        else:\n            schedule[\"frequency\"] = \"daily\"\n\n    return schedule\n\n\ndef load_settings() -> dict[str, Any]:\n    ensure_dirs()\n    settings = settings_defaults()\n    raw = read_json(SETTINGS_FILE)\n\n    if isinstance(raw, dict):\n        settings.update(normalize_update_policy(raw))\n        settings[\"brew_path\"] = str(raw.get(\"brew_path\") or \"\").strip()\n        try:\n            settings[\"scheduler\"] = normalize_scheduler_payload(raw.get(\"scheduler\"))\n        except ValueError:\n            settings[\"scheduler\"] = settings_defaults()[\"scheduler\"]\n        return settings\n\n    # Backward compatibility: if we have no settings file but an existing launch agent,\n    # we infer scheduler defaults from that agent.\n    inferred = read_launch_agent_scheduler()\n    if inferred:\n        settings[\"scheduler\"] = inferred\n\n    return settings\n\n\ndef save_settings(settings: dict[str, Any]) -> None:\n    ensure_dirs()\n    normalized = settings_defaults()\n    normalized.update(normalize_update_policy(settings))\n    normalized[\"brew_path\"] = str(settings.get(\"brew_path\") or \"\").strip()\n    normalized[\"scheduler\"] = normalize_scheduler_payload(settings.get(\"scheduler\"))\n    atomic_write_json(SETTINGS_FILE, normalized)\n\n\ndef normalize_update_policy(raw: dict[str, Any]) -> dict[str, Any]:\n    excluded = raw.get(\"excluded_packages\", [])\n    if not isinstance(excluded, list):\n        raise ValueError(\"excluded_packages must be an array\")\n    for value in excluded:\n        if not isinstance(value, str) or not re.fullmatch(r\"(?:formula|cask|port):[A-Za-z0-9@._+\\-]+\", value):\n            raise ValueError(\"Excluded packages must use formula:name or cask:name\")\n    names = raw.get(\"binary_tools\", [])\n    allowed = set(routes.PIXI_TOOLS) | {\"azure-cli\"}\n    if not isinstance(names, list) or any(name not in allowed for name in names):\n        raise ValueError(\"Unsupported binary tool route\")\n    timeout = int(raw.get(\"upgrade_timeout_seconds\", 0))\n    if timeout != 0 and not 60 <= timeout <= 86400:\n        raise ValueError(\"upgrade_timeout_seconds must be 0 or between 60 and 86400\")\n    return {\"excluded_packages\": sorted(set(excluded)), \"binary_only\": bool(raw.get(\"binary_only\", True)),\n            \"binary_tools\": sorted(set(names)), \"upgrade_timeout_seconds\": timeout}\n\n\ndef is_excluded(name: str, kind: str) -> bool:\n    return f\"{kind}:{name}\" in load_settings()[\"excluded_packages\"]\n\n\ndef apply_update_policy(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:\n    settings = load_settings()\n    records = routes.read_records(TOOLS_ROOT)\n    for pkg in packages:\n        name, kind = pkg.get(\"name\"), pkg.get(\"kind\")\n        excluded = f\"{'formula' if kind == 'tool' else kind}:{name}\" in settings[\"excluded_packages\"] or (kind == \"port\" and name == \"ngrok\")\n        pkg[\"excluded\"] = excluded\n        if kind not in {\"port\", \"tool\"}:\n            pkg[\"provider\"] = \"homebrew\"\n        if excluded:\n            pkg[\"excluded_outdated\"] = bool(pkg.get(\"excluded_outdated\") or pkg.get(\"outdated\"))\n            pkg[\"outdated\"] = False\n        elif pkg.pop(\"excluded_outdated\", False):\n            pkg[\"outdated\"] = True\n        record = records.get(name) if kind == \"formula\" and not pkg.get(\"installation_id\") else None\n        if record:\n            pkg[\"brew_installed_version\"] = pkg.get(\"brew_installed_version\", pkg[\"installed_version\"])\n            pkg[\"installed_version\"] = record[\"version\"]\n            pkg[\"provider\"] = record[\"provider\"]\n            pkg[\"provider_verified_at\"] = record[\"verified_at\"]\n            pkg[\"outdated\"] = not excluded and routes.version_key(record[\"version\"]) < routes.version_key(pkg.get(\"latest_version\"))\n        pkg[\"update_available\"] = bool(pkg.get(\"outdated\")) and not excluded\n    return packages\n\n\ndef load_update_history() -> list[dict[str, Any]]:\n    with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):\n        return load_update_history_unlocked()\n\n\ndef load_update_history_unlocked() -> list[dict[str, Any]]:\n    raw = read_json(UPDATE_HISTORY_FILE)\n    if raw is None:\n        if UPDATE_HISTORY_FILE.exists():\n            quarantine_corrupted_json_file(UPDATE_HISTORY_FILE, reason=\"invalid_json\")\n        return []\n\n    items = raw.get(\"items\") if isinstance(raw, dict) else []\n    if not isinstance(items, list):\n        return []\n\n    out: list[dict[str, Any]] = []\n    for item in items:\n        if not isinstance(item, dict):\n            continue\n        out.append(\n            {\n                \"timestamp\": str(item.get(\"timestamp\") or \"\").strip(),\n                \"name\": str(item.get(\"name\") or \"\").strip(),\n                \"kind\": str(item.get(\"kind\") or \"\").strip(),\n                \"ok\": bool(item.get(\"ok\", False)),\n                \"verified_latest\": bool(item.get(\"verified_latest\", False)),\n                \"installed_version\": str(item.get(\"installed_version\") or \"\").strip(),\n                \"latest_version\": str(item.get(\"latest_version\") or \"\").strip(),\n                \"note\": str(item.get(\"note\") or \"\").strip(),\n            }\n        )\n    return out\n\n\ndef save_update_history(items: list[dict[str, Any]], *, lock: bool = True) -> None:\n    ensure_dirs()\n    payload = {\"items\": items[:MAX_UPDATE_HISTORY_ITEMS]}\n    if lock:\n        with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):\n            atomic_write_json(UPDATE_HISTORY_FILE, payload)\n        return\n    atomic_write_json(UPDATE_HISTORY_FILE, payload)\n\n\ndef append_update_history_entries(entries: list[dict[str, Any]]) -> None:\n    if not entries:\n        return\n    with acquire_file_lock(UPDATE_HISTORY_LOCK_FILE):\n        current = load_update_history_unlocked()\n        merged = list(entries) + current\n        save_update_history(merged, lock=False)\n\n\n@contextmanager\ndef acquire_file_lock(lock_path: Path, timeout_seconds: float = UPDATE_HISTORY_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:\n    ensure_dirs()\n    lock_path.parent.mkdir(parents=True, exist_ok=True)\n    with lock_path.open(\"a+\", encoding=\"utf-8\") as lock_fp:\n        if fcntl is None:\n            yield\n            return\n\n        deadline = time.monotonic() + max(0.1, float(timeout_seconds))\n        while True:\n            try:\n                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n                break\n            except BlockingIOError:\n                if time.monotonic() >= deadline:\n                    raise RuntimeError(f\"Timed out acquiring lock for {lock_path}\")\n                time.sleep(0.05)\n\n        try:\n            yield\n        finally:\n            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)\n\n\ndef quarantine_corrupted_json_file(path: Path, *, reason: str) -> None:\n    if not path.exists():\n        return\n\n    stamp = dt.datetime.now(dt.timezone.utc).strftime(\"%Y%m%dT%H%M%SZ\")\n    quarantine_path = path.with_suffix(path.suffix + f\".corrupt-{stamp}\")\n\n    try:\n        path.replace(quarantine_path)\n        append_service_log(\n            f\"Corrupted JSON moved to quarantine ({reason}): {path} -> {quarantine_path}\"\n        )\n    except Exception as exc:\n        append_service_log(\n            f\"Failed to quarantine corrupted JSON ({reason}) for {path}: {exc}\"\n        )\n\n\ndef detect_brew_candidates() -> list[str]:\n    quick_hints = [\n        os.environ.get(\"BREW_BIN\") or \"\",\n        \"/opt/homebrew/bin/brew\",\n        \"/usr/local/bin/brew\",\n        shutil.which(\"brew\") or \"\",\n    ]\n\n    path_hints = []\n    for segment in str(process_env().get(\"PATH\") or \"\").split(\":\"):\n        seg = str(segment or \"\").strip()\n        if seg:\n            path_hints.append(str(Path(seg) / \"brew\"))\n\n    which_hints: list[str] = []\n    which_all = run_cmd([\"/usr/bin/which\", \"-a\", \"brew\"], timeout=10)\n    if which_all.stdout:\n        which_hints.extend(line.strip() for line in which_all.stdout.splitlines() if line.strip())\n\n    scan_roots: list[Path] = [\n        Path(\"/opt/homebrew\"),\n        Path(\"/usr/local\"),\n        Path(\"/opt\"),\n    ]\n    existing_roots = [root for root in scan_roots if root.exists()]\n\n    scanned_hints: list[str] = []\n    if existing_roots:\n        find_cmd = [\n            \"/usr/bin/find\",\n            *[str(root) for root in existing_roots],\n            \"-type\",\n            \"f\",\n            \"-name\",\n            \"brew\",\n        ]\n        find_res = run_cmd(find_cmd, timeout=BREW_SCAN_TIMEOUT_SECONDS)\n        scanned_hints = [\n            line.strip()\n            for line in (find_res.stdout or \"\").splitlines()\n            if line.strip()\n        ]\n        if find_res.code != 0 and find_res.stderr:\n            append_service_log(f\"brew auto-detect find warning: {find_res.stderr}\")\n\n    combined = quick_hints + path_hints + which_hints + scanned_hints\n\n    unique_candidates: list[str] = []\n    seen: set[str] = set()\n    for item in combined:\n        raw = str(item or \"\").strip()\n        if not raw:\n            continue\n        try:\n            normalized = str(Path(raw).expanduser().resolve())\n        except Exception:\n            normalized = str(Path(raw).expanduser())\n        if normalized in seen:\n            continue\n        seen.add(normalized)\n        unique_candidates.append(normalized)\n\n    verified: list[str] = []\n    for candidate in unique_candidates:\n        path_obj = Path(candidate)\n        if not path_obj.exists() or not os.access(candidate, os.X_OK):\n            continue\n        probe = run_cmd([candidate, \"--version\"], timeout=20)\n        text = f\"{probe.stdout}\\n{probe.stderr}\".lower()\n        if probe.code == 0 and \"homebrew\" in text:\n            verified.append(candidate)\n        if len(verified) >= BREW_SCAN_MAX_RESULTS:\n            break\n\n    preferred_order = {\n        \"/opt/homebrew/bin/brew\": 0,\n        \"/usr/local/bin/brew\": 1,\n    }\n    verified.sort(key=lambda item: (preferred_order.get(item, 9), len(item), item))\n    return verified\n\n\ndef normalize_brew_path(raw_path: Any) -> str:\n    value = str(raw_path or \"\").strip()\n    if not value:\n        return \"\"\n\n    candidate = str(Path(value).expanduser())\n    if not Path(candidate).exists() or not os.access(candidate, os.X_OK):\n        raise ValueError(f\"Provided brew path is not executable: {candidate}\")\n\n    probe = run_cmd([candidate, \"--version\"], timeout=20)\n    if probe.code != 0:\n        raise ValueError(f\"Provided brew path is not valid: {candidate}\")\n\n    return candidate\n\n\ndef launch_agent_payload_for_scheduler(scheduler: dict[str, Any]) -> dict[str, Any]:\n    payload: dict[str, Any] = {\n        \"Label\": LAUNCH_AGENT_LABEL,\n        \"ProgramArguments\": [\n            str(sys.executable),\n            str(Path(__file__).resolve()),\n            \"--check-only\",\n            \"--notify\",\n        ],\n        \"StandardOutPath\": str(LOG_DIR / \"brew-scheduled-stdout.log\"),\n        \"StandardErrorPath\": str(LOG_DIR / \"brew-scheduled-stderr.log\"),\n        \"WorkingDirectory\": str(Path.home()),\n        \"KeepAlive\": False,\n        \"RunAtLoad\": False,\n    }\n\n    frequency = str(scheduler.get(\"frequency\") or \"daily\")\n    if frequency == \"interval\":\n        payload[\"StartInterval\"] = int(scheduler.get(\"interval_hours\", 24)) * 3600\n        return payload\n\n    start = {\n        \"Hour\": int(scheduler.get(\"hour\", 9)),\n        \"Minute\": int(scheduler.get(\"minute\", 0)),\n    }\n    if frequency == \"weekly\":\n        start[\"Weekday\"] = int(scheduler.get(\"weekday\", 1))\n\n    payload[\"StartCalendarInterval\"] = start\n    return payload\n\n\ndef is_launch_agent_loaded() -> bool:\n    res = run_cmd([\"launchctl\", \"print\", LAUNCH_AGENT_SERVICE], timeout=20)\n    return res.code == 0\n\n\ndef apply_scheduler_settings(scheduler: dict[str, Any]) -> dict[str, Any]:\n    normalized = normalize_scheduler_payload(scheduler)\n\n    # Always attempt to unload existing service first; ignore failures.\n    run_cmd([\"launchctl\", \"bootout\", LAUNCH_AGENT_DOMAIN, str(LAUNCH_AGENT_FILE)], timeout=20)\n\n    if not normalized[\"enabled\"]:\n        try:\n            if LAUNCH_AGENT_FILE.exists():\n                LAUNCH_AGENT_FILE.unlink()\n        except Exception as exc:\n            raise RuntimeError(f\"Failed to disable scheduler: {exc}\") from exc\n\n        return {\n            **normalized,\n            \"active\": False,\n            \"launch_agent_path\": str(LAUNCH_AGENT_FILE),\n        }\n\n    try:\n        LAUNCH_AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)\n        payload = launch_agent_payload_for_scheduler(normalized)\n        with LAUNCH_AGENT_FILE.open(\"wb\") as fp:\n            plistlib.dump(payload, fp)\n    except Exception as exc:\n        raise RuntimeError(f\"Failed to write LaunchAgent file: {exc}\") from exc\n\n    bootstrap = run_cmd([\"launchctl\", \"bootstrap\", LAUNCH_AGENT_DOMAIN, str(LAUNCH_AGENT_FILE)], timeout=20)\n    if bootstrap.code != 0:\n        fallback = run_cmd([\"launchctl\", \"load\", \"-w\", str(LAUNCH_AGENT_FILE)], timeout=20)\n        if fallback.code != 0:\n            detail = bootstrap.stderr or bootstrap.stdout or fallback.stderr or fallback.stdout or \"unknown error\"\n            raise RuntimeError(f\"Failed to enable scheduler via launchctl: {detail}\")\n\n    return {\n        **normalized,\n        \"active\": is_launch_agent_loaded(),\n        \"launch_agent_path\": str(LAUNCH_AGENT_FILE),\n    }\n\n\ndef scheduler_status_payload(settings: dict[str, Any] | None = None) -> dict[str, Any]:\n    raw = settings if isinstance(settings, dict) else load_settings()\n    scheduler = normalize_scheduler_payload(raw.get(\"scheduler\"))\n    return {\n        **scheduler,\n        \"active\": is_launch_agent_loaded(),\n        \"launch_agent_path\": str(LAUNCH_AGENT_FILE),\n    }\n\n\ndef read_json(path: Path) -> dict[str, Any] | None:\n    if not path.exists():\n        return None\n    try:\n        return json.loads(path.read_text(encoding=\"utf-8\"))\n    except Exception:\n        return None\n\n\ndef atomic_write_json(path: Path, payload: dict[str, Any]) -> None:\n    provider_ownership.atomic_write_json(path, payload)\n\n\ndef append_log(path: Path, message: str) -> None:\n    ensure_dirs()\n    ts = dt.datetime.now().strftime(\"%Y-%m-%d %H:%M:%S\")\n    with path.open(\"a\", encoding=\"utf-8\") as fp:\n        fp.write(f\"[{ts}] {message}\\n\")\n\n\ndef append_check_log(message: str) -> None:\n    append_log(CHECK_LOG_FILE, message)\n\n\ndef append_service_log(message: str) -> None:\n    append_log(SERVICE_LOG_FILE, message)\n\n\ndef process_env() -> dict[str, str]:\n    env = {\n        \"HOME\": str(Path.home()),\n        \"PATH\": \"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\",\n        \"LANG\": \"en_US.UTF-8\",\n        \"LC_ALL\": \"en_US.UTF-8\",\n    }\n    for key, value in os.environ.items():\n        if key.startswith(\"HOMEBREW_\"):\n            env[key] = value\n    # Refresh metadata once through Check Now, not once per package. Retain old\n    # kegs while a batch is in progress so unrelated tools keep their libraries.\n    env[\"HOMEBREW_NO_AUTO_UPDATE\"] = \"1\"\n    env[\"HOMEBREW_NO_INSTALL_CLEANUP\"] = \"1\"\n    return env\n\n\ndef terminate_process(proc: subprocess.Popen[str] | None, *, force_kill_after_seconds: float = 4.0) -> bool:\n    if proc is None:\n        return False\n\n    try:\n        if proc.poll() is not None:\n            return False\n\n        used_process_group = False\n        if hasattr(os, \"killpg\"):\n            try:\n                pgid = os.getpgid(proc.pid)\n                os.killpg(pgid, signal.SIGTERM)\n                used_process_group = True\n            except Exception:\n                used_process_group = False\n\n        if not used_process_group:\n            proc.terminate()\n\n        proc.wait(timeout=force_kill_after_seconds)\n        return True\n    except subprocess.TimeoutExpired:\n        used_process_group = False\n        if hasattr(os, \"killpg\"):\n            try:\n                pgid = os.getpgid(proc.pid)\n                os.killpg(pgid, signal.SIGKILL)\n                used_process_group = True\n            except Exception:\n                used_process_group = False\n\n        if not used_process_group:\n            proc.kill()\n        try:\n            proc.wait(timeout=2.0)\n        except Exception:\n            pass\n        return True\n    except Exception:\n        return False\n\n\ndef run_cmd(args: list[str], timeout: int | None = 1800, *, cancellable: bool = False,\n            on_output: Callable[[str, str], None] | None = None,\n            env_override: dict[str, str] | None = None,\n            input_data: str | None = None) -> CommandResult:\n    tracked_scan = getattr(mac_manager.SCAN_CONTEXT, 'active', False)\n    if tracked_scan:\n        raise_if_cancel_requested()\n    result = _run_cmd_impl(args, timeout, cancellable=cancellable or tracked_scan,\n                           on_output=on_output, env_override=env_override, input_data=input_data)\n    if tracked_scan:\n        raise_if_cancel_requested()\n    return result\n\n\ndef _run_cmd_impl(args: list[str], timeout: int | None = 1800, *, cancellable: bool = False,\n            on_output: Callable[[str, str], None] | None = None,\n            env_override: dict[str, str] | None = None,\n            input_data: str | None = None) -> CommandResult:\n    if input_data is not None and on_output is not None:\n        raise ValueError(\"Streaming progress and explicit stdin cannot be combined\")\n    proc = subprocess.Popen(\n        args,\n        stdin=subprocess.PIPE if input_data is not None else None,\n        stdout=subprocess.PIPE,\n        stderr=subprocess.PIPE,\n        text=True,\n        env={**process_env(), **(env_override or {})},\n        start_new_session=True,\n    )\n\n    if cancellable:\n        with ACTIVE_COMMAND_LOCK:\n            ACTIVE_COMMAND[\"process\"] = proc\n            ACTIVE_COMMAND[\"args\"] = list(args)\n\n    try:\n        if on_output is not None:\n            # Drain both pipes concurrently so a quiet stdout cannot block stderr.\n            captured: dict[str, list[str]] = {\"stdout\": [], \"stderr\": []}\n\n            def drain(stream: Any, source: str) -> None:\n                try:\n                    for line in stream:\n                        captured[source].append(line)\n                        try:\n                            on_output(line, source)\n                        except Exception as exc:\n                            append_service_log(f\"Upgrade progress observer failed: {exc}\")\n                finally:\n                    stream.close()\n\n            readers = [threading.Thread(target=drain, args=(stream, source), daemon=True)\n                       for stream, source in ((proc.stdout, \"stdout\"), (proc.stderr, \"stderr\"))]\n            for reader in readers:\n                reader.start()\n            timed_out = False\n            try:\n                proc.wait(timeout=timeout)\n            except subprocess.TimeoutExpired:\n                timed_out = True\n                terminate_process(proc)\n            for reader in readers:\n                reader.join()\n            stdout = \"\".join(captured[\"stdout\"]).strip()\n            stderr = \"\".join(captured[\"stderr\"]).strip()\n            if timed_out:\n                stderr = f\"{stderr}\\nCommand timed out after {int(timeout)}s\".strip()\n            return CommandResult(124 if timed_out else proc.returncode, stdout, stderr)\n        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)\n        return CommandResult(proc.returncode, (stdout or \"\").strip(), (stderr or \"\").strip())\n    except subprocess.TimeoutExpired:\n        terminate_process(proc)\n        stdout, stderr = proc.communicate()\n        timeout_text = f\"Command timed out after {int(timeout)}s\"\n        merged_stderr = (stderr or \"\").strip()\n        if merged_stderr:\n            merged_stderr = f\"{merged_stderr}\\n{timeout_text}\".strip()\n        else:\n            merged_stderr = timeout_text\n        return CommandResult(124, (stdout or \"\").strip(), merged_stderr)\n    finally:\n        if cancellable:\n            with ACTIVE_COMMAND_LOCK:\n                if ACTIVE_COMMAND.get(\"process\") is proc:\n                    ACTIVE_COMMAND[\"process\"] = None\n                    ACTIVE_COMMAND[\"args\"] = []\n\n\ndef brew_bin() -> str:\n    settings = load_settings()\n    raw_path = str(settings.get(\"brew_path\") or \"\").strip()\n    if not raw_path:\n        raise RuntimeError(\n            \"Homebrew path is not configured yet. Open app settings, detect/select a brew path, then try again.\"\n        )\n\n    try:\n        return normalize_brew_path(raw_path)\n    except ValueError as exc:\n        raise RuntimeError(\n            f\"Configured Homebrew path is invalid: {exc}. Update it in app settings.\"\n        ) from exc\n\n\ndef raise_if_cancel_requested() -> None:\n    if is_cancel_requested():\n        raise OperationCancelledError(\"Operation cancelled by user\")\n\n\ndef request_cancel_current_operation() -> dict[str, Any]:\n    operation = read_current_operation()\n    if not operation.get(\"running\"):\n        progress_before = read_check_progress()\n        progress = normalize_stale_progress_state(operation)\n\n        if progress_before.get(\"running\") and not progress.get(\"running\"):\n            return {\n                \"ok\": True,\n                \"running\": False,\n                \"message\": \"No active operation process found; stale progress state was reset\",\n                \"operation\": operation,\n                \"terminated_command\": False,\n                \"stale_progress_cleared\": True,\n            }\n\n        return {\n            \"ok\": False,\n            \"running\": False,\n            \"message\": \"No operation is currently running\",\n            \"operation\": operation,\n            \"terminated_command\": False,\n        }\n\n    mark_cancel_requested()\n\n    with ACTIVE_COMMAND_LOCK:\n        active_proc = ACTIVE_COMMAND.get(\"process\")\n        active_args = list(ACTIVE_COMMAND.get(\"args\") or [])\n\n    terminated = terminate_process(active_proc)\n\n    operation_name = str(operation.get(\"name\") or \"operation\")\n    message = f\"Cancellation requested for {operation_name}\"\n    if terminated:\n        append_check_log(f\"Cancellation requested and active command terminated: {' '.join(active_args) if active_args else '(unknown command)'}\")\n        append_service_log(f\"Cancellation requested for {operation_name}; active command terminated\")\n    else:\n        append_check_log(f\"Cancellation requested for {operation_name}\")\n        append_service_log(f\"Cancellation requested for {operation_name}; no active command handle\")\n\n    set_check_progress(\n        message=message,\n        cancel_requested=True,\n    )\n\n    return {\n        \"ok\": True,\n        \"running\": True,\n        \"message\": message,\n        \"operation\": operation,\n        \"terminated_command\": terminated,\n    }\n\n\ndef run_with_progress(op_name, fn, *, snapshot=False):\n    \"\"\"Capture a deep final progress snapshot before releasing the operation lock.\"\"\"\n    def operation():\n        try:\n            value = fn()\n        except OperationCancelledError:\n            set_check_progress(running=False, phase='canceled', canceled=True,\n                               current_package=None, eta_seconds=None, completed_at=now_iso())\n            return {'ok':False, 'canceled':True, 'snapshot':read_json(STATE_FILE) or {},\n                    'progress':copy.deepcopy(read_check_progress())}\n        except Exception as error:\n            set_check_progress(running=False, phase='error', error=str(error),\n                               current_package=None, eta_seconds=None, completed_at=now_iso())\n            error.operation_progress=copy.deepcopy(read_check_progress())\n            raise\n        progress=copy.deepcopy(read_check_progress())\n        progress['partial']=bool(value.get('partial')) if snapshot else bool(value.get('inventory_refresh_error'))\n        with CHECK_PROGRESS_LOCK:\n            CHECK_PROGRESS['partial']=progress['partial']\n        return {'ok':True,'snapshot':value,'progress':progress} if snapshot else {**value,'progress':progress}\n    return run_exclusive(op_name, operation)\n\n\ndef run_exclusive(op_name: str, fn):\n    if not OP_LOCK.acquire(blocking=False):\n        active_operation = read_current_operation()\n        raise BusyError(\n            f\"Operation '{active_operation.get('name')}' is already running since {active_operation.get('started_at')}\"\n        )\n\n    reset_cancel_state()\n    with CURRENT_OPERATION_LOCK:\n        CURRENT_OPERATION[\"running\"] = True\n        CURRENT_OPERATION[\"name\"] = op_name\n        CURRENT_OPERATION[\"started_at\"] = now_iso()\n\n    prior_scan = getattr(mac_manager.SCAN_CONTEXT, 'active', False)\n    if op_name in {'update_plan', 'migration'}:\n        mac_manager.SCAN_CONTEXT.active = True\n    try:\n        if op_name.startswith((\"update\", \"migration\", \"macports\")) and op_name != \"update_plan\":\n            with operation_lock.OperationLock(TOOLS_ROOT, op_name):\n                return fn()\n        return fn()\n    except OperationCancelledError:\n        set_check_progress(running=False, phase='canceled', message='Operation canceled',\n                           canceled=True, current_package=None, eta_seconds=None, completed_at=now_iso())\n        raise\n    finally:\n        mac_manager.SCAN_CONTEXT.active = prior_scan\n        reset_cancel_state()\n        with CURRENT_OPERATION_LOCK:\n            CURRENT_OPERATION[\"running\"] = False\n            CURRENT_OPERATION[\"name\"] = None\n            CURRENT_OPERATION[\"started_at\"] = None\n        OP_LOCK.release()\n        maybe_schedule_deferred_check(op_name)\n\n\ndef normalize_versions(raw: Any) -> list[str]:\n    if raw is None:\n        return []\n    if isinstance(raw, str):\n        return [raw]\n    if isinstance(raw, list):\n        out: list[str] = []\n        for item in raw:\n            if isinstance(item, str):\n                out.append(item)\n            elif isinstance(item, dict):\n                version = item.get(\"version\")\n                if version:\n                    out.append(str(version))\n        return out\n    return []\n\n\ndef fetch_outdated_index() -> dict[tuple[str, str], dict[str, str]]:\n    b = brew_bin()\n    res = run_cmd([b, \"outdated\", \"--json=v2\"], timeout=300)\n    if res.code != 0:\n        append_check_log(f\"Warning: brew outdated failed: {res.stderr or res.stdout}\")\n        raise RuntimeError(f\"Cannot verify outdated inventory: {res.stderr or res.stdout}\")\n\n    try:\n        payload = json.loads(res.stdout or \"{}\")\n    except json.JSONDecodeError:\n        append_check_log(\"Warning: failed to parse brew outdated JSON\")\n        raise RuntimeError(\"Cannot verify outdated inventory: invalid Homebrew JSON\")\n\n    index: dict[tuple[str, str], dict[str, str]] = {}\n\n    for row in payload.get(\"formulae\", []) or []:\n        if not isinstance(row, dict):\n            continue\n        name = str(row.get(\"name\") or \"\").strip()\n        if not name:\n            continue\n        installed_versions = normalize_versions(row.get(\"installed_versions\"))\n        index[(\"formula\", name)] = {\n            \"installed_version\": installed_versions[-1] if installed_versions else \"unknown\",\n            \"latest_version\": str(row.get(\"current_version\") or row.get(\"version\") or \"unknown\"),\n        }\n\n    for row in payload.get(\"casks\", []) or []:\n        if not isinstance(row, dict):\n            continue\n        token = str(row.get(\"name\") or row.get(\"token\") or \"\").strip()\n        if not token:\n            continue\n        installed_versions = normalize_versions(row.get(\"installed_versions\") or row.get(\"installed\"))\n        index[(\"cask\", token)] = {\n            \"installed_version\": installed_versions[-1] if installed_versions else \"unknown\",\n            \"latest_version\": str(row.get(\"current_version\") or row.get(\"version\") or \"unknown\"),\n        }\n\n    return index\n\n\ndef fetch_installed_inventory(outdated_index: dict[tuple[str, str], dict[str, str]]) -> list[dict[str, Any]]:\n    b = brew_bin()\n    res = run_cmd([b, \"info\", \"--json=v2\", \"--installed\"], timeout=1800)\n    if res.code != 0:\n        raise RuntimeError(f\"brew info --installed failed: {res.stderr or res.stdout}\")\n\n    payload = json.loads(res.stdout or \"{}\")\n    try:\n        evidence_context = installation_evidence.collect(sys.modules[__name__], payload)\n        payload.setdefault('formulae', []).extend(evidence_context['omitted'])\n    except OperationCancelledError:\n        raise\n    except Exception as error:\n        evidence_context = None\n        installation_evidence.reset()\n        append_service_log('Installation evidence unavailable: ' + str(error))\n    packages: list[dict[str, Any]] = []\n\n    for row in payload.get(\"formulae\", []) or []:\n        if not isinstance(row, dict):\n            continue\n\n        name = str(row.get(\"name\") or \"\").strip()\n        if not name:\n            continue\n\n        installed_entries = row.get(\"installed\") or []\n        installed_versions = normalize_versions(installed_entries)\n        installation_fields = installation_evidence.inventory_fields(sys.modules[__name__], row, evidence_context)\n        installed_version = installation_fields['installed_version']\n        latest_version = str(((row.get(\"versions\") or {}).get(\"stable\") or \"unknown\"))\n\n        outdated_meta = outdated_index.get((\"formula\", name))\n        outdated = bool(outdated_meta) or bool(row.get(\"outdated\", False))\n        if outdated_meta:\n            installation_fields['manager_outdated_version'] = outdated_meta.get('installed_version')\n            latest_version = outdated_meta.get(\"latest_version\") or latest_version\n\n        packages.append(\n            {\n                \"name\": name,\n                \"kind\": \"formula\",\n                **installation_fields,\n                \"display_name\": str(row.get(\"full_name\") or name),\n                \"description\": str(row.get(\"desc\") or \"\"),\n                \"description_he\": \"\",\n                \"homepage\": str(row.get(\"homepage\") or \"\"),\n                \"installed_version\": installed_version,\n                \"latest_version\": latest_version,\n                \"installed_versions\": installed_versions,\n                \"outdated\": outdated,\n                \"current_release_date\": None,\n                \"current_release_date_source\": None,\n                \"latest_release_date\": None,\n                \"latest_release_date_source\": None,\n                \"outdated_since\": None,\n                \"days_since_outdated\": None,\n                \"release_date\": None,\n                \"release_date_source\": None,\n                \"tap\": str(row.get(\"tap\") or \"homebrew/core\"),\n                \"ruby_source_path\": str(row.get(\"ruby_source_path\") or \"\"),\n            }\n        )\n\n    for row in payload.get(\"casks\", []) or []:\n        if not isinstance(row, dict):\n            continue\n\n        token = str(row.get(\"token\") or row.get(\"name\") or \"\").strip()\n        if not token:\n            continue\n\n        installed_versions = normalize_versions(row.get(\"installed\"))\n        installed_version = installed_versions[-1] if installed_versions else \"unknown\"\n        latest_version = str(row.get(\"version\") or \"unknown\")\n\n        outdated_meta = outdated_index.get((\"cask\", token))\n        outdated = bool(outdated_meta) or bool(row.get(\"outdated\", False))\n        if outdated_meta:\n            installed_version = outdated_meta.get(\"installed_version\") or installed_version\n            latest_version = outdated_meta.get(\"latest_version\") or latest_version\n\n        desc = row.get(\"desc\")\n        if isinstance(desc, list):\n            description = \" • \".join(str(item) for item in desc if str(item).strip())\n        else:\n            description = str(desc or \"\")\n\n        packages.append(\n            {\n                \"name\": token,\n                \"kind\": \"cask\",\n                \"display_name\": token,\n                \"description\": description,\n                \"description_he\": \"\",\n                \"homepage\": str(row.get(\"homepage\") or \"\"),\n                \"installed_version\": installed_version,\n                \"latest_version\": latest_version,\n                \"installed_versions\": installed_versions,\n                \"outdated\": outdated,\n                \"current_release_date\": None,\n                \"current_release_date_source\": None,\n                \"latest_release_date\": None,\n                \"latest_release_date_source\": None,\n                \"outdated_since\": None,\n                \"days_since_outdated\": None,\n                \"release_date\": None,\n                \"release_date_source\": None,\n                \"tap\": str(row.get(\"tap\") or \"homebrew/cask\"),\n                \"ruby_source_path\": str(row.get(\"ruby_source_path\") or \"\"),\n            }\n        )\n\n    known = {(pkg[\"kind\"], pkg[\"name\"]) for pkg in packages}\n    for (kind, name), meta in outdated_index.items():\n        if (kind, name) in known:\n            continue\n        packages.append(\n            {\n                \"name\": name,\n                \"kind\": kind,\n                \"display_name\": name,\n                \"description\": \"\",\n                \"description_he\": \"\",\n                \"homepage\": \"\",\n                \"installed_version\": meta.get(\"installed_version\") or \"unknown\",\n                \"latest_version\": meta.get(\"latest_version\") or \"unknown\",\n                \"installed_versions\": [],\n                \"outdated\": True,\n                \"current_release_date\": None,\n                \"current_release_date_source\": None,\n                \"latest_release_date\": None,\n                \"latest_release_date_source\": None,\n                \"outdated_since\": None,\n                \"days_since_outdated\": None,\n                \"release_date\": None,\n                \"release_date_source\": None,\n                \"tap\": \"homebrew/core\" if kind == \"formula\" else \"homebrew/cask\",\n                \"ruby_source_path\": \"\",\n            }\n        )\n\n    # Managed tools remain installed after their redundant Brew keg is removed.\n    # Query catalog metadata separately; never infer \"current\" from a missing row.\n    records = routes.read_records(TOOLS_ROOT)\n    existing = {(pkg[\"kind\"], pkg[\"name\"]) for pkg in packages}\n    missing = sorted(name for name in records if (\"formula\", name) not in existing)\n    if missing:\n        for name in missing:\n            validate_package_name(name)\n        result = run_cmd([b, \"info\", \"--json=v2\", \"--formula\", *missing], timeout=120)\n        if result.code:\n            raise RuntimeError(\"Cannot check catalog metadata for separately installed tools: \" + result.stderr)\n        catalog = {row[\"name\"]: row for row in json.loads(result.stdout).get(\"formulae\", [])}\n        for name in missing:\n            meta = catalog.get(name)\n            if not meta:\n                raise RuntimeError(f\"Missing catalog metadata for separately installed tool: {name}\")\n            latest = str(meta[\"versions\"][\"stable\"])\n            if meta.get(\"revision\"):\n                latest += \"_\" + str(meta[\"revision\"])\n            record = records[name]\n            packages.append({\"name\": name, \"kind\": \"formula\", \"display_name\": name,\n                             \"description\": meta.get(\"desc\", \"\"), \"description_he\": \"\",\n                             \"homepage\": meta.get(\"homepage\", \"\"), \"tap\": meta.get(\"tap\", \"homebrew/core\"),\n                             \"installed_version\": record[\"version\"], \"installed_versions\": [record[\"version\"]],\n                             \"latest_version\": latest, \"outdated\": False, \"brew_installation_present\": False,\n                             \"current_release_date\": None, \"latest_release_date\": None,\n                             \"outdated_since\": None, \"days_since_outdated\": None,\n                             \"release_date\": None, \"release_date_source\": None})\n    return packages\n\n\ndef normalize_tap(tap: str, kind: str) -> str:\n    value = str(tap or \"\").strip()\n    if value:\n        return value\n    return \"homebrew/core\" if kind == \"formula\" else \"homebrew/cask\"\n\n\ndef resolve_repo_path(tap: str) -> Path | None:\n    key = tap.strip()\n    if not key:\n        return None\n    if key in REPO_CACHE:\n        return REPO_CACHE[key]\n\n    b = brew_bin()\n    res = run_cmd([b, \"--repo\", key], timeout=20)\n    if res.code != 0 or not res.stdout:\n        return None\n\n    path = Path(res.stdout.strip())\n    if not path.exists():\n        return None\n\n    REPO_CACHE[key] = path\n    return path\n\n\ndef find_formula_or_cask_file(repo: Path, pkg: dict[str, Any]) -> Path | None:\n    ruby_source_path = str(pkg.get(\"ruby_source_path\") or \"\").strip()\n    if ruby_source_path:\n        src = Path(ruby_source_path)\n        if src.is_absolute() and src.exists():\n            return src\n        candidate = repo / ruby_source_path\n        if candidate.exists():\n            return candidate\n\n    name = str(pkg.get(\"name\") or \"\").strip()\n    if not name:\n        return None\n\n    if str(pkg.get(\"kind\")) == \"formula\":\n        candidates = [\n            repo / \"Formula\" / f\"{name}.rb\",\n            repo / \"Formula\" / name[:1] / f\"{name}.rb\",\n            repo / \"HomebrewFormula\" / f\"{name}.rb\",\n        ]\n        for candidate in candidates:\n            if candidate.exists():\n                return candidate\n\n        for candidate in (repo / \"Formula\").glob(f\"**/{name}.rb\"):\n            if candidate.exists():\n                return candidate\n        return None\n\n    candidates = [\n        repo / \"Casks\" / f\"{name}.rb\",\n        repo / \"Casks\" / name[:1] / f\"{name}.rb\",\n    ]\n    for candidate in candidates:\n        if candidate.exists():\n            return candidate\n\n    for candidate in (repo / \"Casks\").glob(f\"**/{name}.rb\"):\n        if candidate.exists():\n            return candidate\n\n    return None\n\n\ndef git_last_commit_iso(repo: Path, target_file: Path) -> str | None:\n    if not target_file.exists():\n        return None\n\n    res = run_cmd([\"git\", \"-C\", str(repo), \"log\", \"-1\", \"--format=%cI\", \"--\", str(target_file)], timeout=20)\n    if res.code != 0 or not res.stdout:\n        return None\n    return res.stdout.splitlines()[0].strip()\n\n\ndef repo_relative_file_path(repo: Path, target_file: Path) -> str | None:\n    try:\n        return target_file.resolve().relative_to(repo.resolve()).as_posix()\n    except Exception:\n        return None\n\n\ndef version_in_text(text: str, version: str) -> bool:\n    source = str(version or \"\").strip()\n    if not source:\n        return False\n    pattern = re.compile(rf\"(?<![0-9A-Za-z]){re.escape(source)}(?![0-9A-Za-z])\")\n    return bool(pattern.search(text))\n\n\ndef git_file_contains_version_at_commit(repo: Path, relative_path: str, commit_hash: str, version: str) -> bool:\n    if not relative_path or not commit_hash:\n        return False\n    res = run_cmd(\n        [\"git\", \"-C\", str(repo), \"show\", f\"{commit_hash}:{relative_path}\"],\n        timeout=20,\n    )\n    if res.code != 0:\n        return False\n    return version_in_text(res.stdout, version)\n\n\ndef git_pickaxe_commits(repo: Path, target_file: Path, needle: str) -> list[tuple[str, str]]:\n    if not target_file.exists() or not needle:\n        return []\n    res = run_cmd(\n        [\n            \"git\",\n            \"-C\",\n            str(repo),\n            \"log\",\n            \"--format=%H|%cI\",\n            \"--reverse\",\n            f\"-S{needle}\",\n            \"--\",\n            str(target_file),\n        ],\n        timeout=40,\n    )\n    if res.code != 0 or not res.stdout:\n        return []\n\n    commits: list[tuple[str, str]] = []\n    for line in res.stdout.splitlines():\n        if \"|\" not in line:\n            continue\n        commit_hash, commit_iso = line.split(\"|\", 1)\n        commit_hash = commit_hash.strip()\n        commit_iso = commit_iso.strip()\n        if commit_hash and commit_iso:\n            commits.append((commit_hash, commit_iso))\n\n    if len(commits) > VERSION_SEARCH_COMMITS_LIMIT:\n        return commits[:VERSION_SEARCH_COMMITS_LIMIT]\n    return commits\n\n\ndef git_version_intro_iso(repo: Path, target_file: Path, version: str) -> str | None:\n    version = str(version or \"\").strip()\n    if not target_file.exists() or not version or version in {\"unknown\", \"latest\"}:\n        return None\n\n    relative_path = repo_relative_file_path(repo, target_file)\n    if relative_path:\n        for commit_hash, commit_iso in git_pickaxe_commits(repo, target_file, version):\n            if git_file_contains_version_at_commit(repo, relative_path, commit_hash, version):\n                return commit_iso\n\n    pattern = re.escape(version)\n    res = run_cmd(\n        [\"git\", \"-C\", str(repo), \"log\", \"-1\", \"--format=%cI\", \"-G\", pattern, \"--\", str(target_file)],\n        timeout=30,\n    )\n    if res.code != 0 or not res.stdout:\n        return None\n    return res.stdout.splitlines()[0].strip()\n\n\ndef github_commits_for_path(repo_slug: str, relative_path: str) -> list[dict[str, Any]]:\n    encoded_path = quote(relative_path, safe=\"\")\n    url = (\n        f\"https://api.github.com/repos/{repo_slug}/commits\"\n        f\"?path={encoded_path}&per_page={GITHUB_COMMITS_PER_FILE}\"\n    )\n    req = Request(url, headers={\"User-Agent\": \"brew-update-manager\"})\n    with urlopen(req, timeout=10) as response:\n        payload = json.loads(response.read().decode(\"utf-8\", errors=\"replace\"))\n    if isinstance(payload, list):\n        return [item for item in payload if isinstance(item, dict)]\n    return []\n\n\ndef github_candidate_paths(\n    pkg: dict[str, Any],\n    repo: Path | None,\n    target_file: Path | None,\n) -> list[str]:\n    name = str(pkg.get(\"name\") or \"\").strip()\n    kind = str(pkg.get(\"kind\") or \"\").strip()\n\n    candidates: list[str] = []\n\n    ruby_source_path = str(pkg.get(\"ruby_source_path\") or \"\").strip()\n    if ruby_source_path:\n        candidates.append(ruby_source_path)\n\n    if repo and target_file:\n        rel = repo_relative_file_path(repo, target_file)\n        if rel:\n            candidates.append(rel)\n\n    if name:\n        if kind == \"formula\":\n            candidates.extend(\n                [\n                    f\"Formula/{name}.rb\",\n                    f\"Formula/{name[:1]}/{name}.rb\",\n                ]\n            )\n        elif kind == \"cask\":\n            candidates.extend(\n                [\n                    f\"Casks/{name}.rb\",\n                    f\"Casks/{name[:1]}/{name}.rb\",\n                ]\n            )\n\n    unique: list[str] = []\n    seen: set[str] = set()\n    for path in candidates:\n        normalized = str(path or \"\").strip().lstrip(\"./\")\n        if not normalized or normalized in seen:\n            continue\n        seen.add(normalized)\n        unique.append(normalized)\n\n    return unique[:GITHUB_PATH_CANDIDATES_LIMIT]\n\n\ndef github_release_dates_for_package(\n    tap: str,\n    relative_paths: list[str],\n    installed_version: str,\n    latest_version: str,\n) -> tuple[str | None, str, str | None, str]:\n    repo_slug = TAP_GITHUB_REPO.get(tap)\n    if not repo_slug or not relative_paths:\n        return None, \"unavailable\", None, \"unavailable\"\n\n    commits: list[dict[str, Any]] = []\n    chosen_path: str | None = None\n\n    for rel_path in relative_paths:\n        try:\n            candidate_commits = github_commits_for_path(repo_slug, rel_path)\n        except Exception as exc:\n            append_service_log(f\"GitHub fallback warning ({tap} {rel_path}): {exc}\")\n            continue\n\n        if candidate_commits:\n            commits = candidate_commits\n            chosen_path = rel_path\n            break\n\n    if not commits:\n        return None, \"unavailable\", None, \"unavailable\"\n\n    head = commits[0]\n    latest_date = str((((head.get(\"commit\") or {}).get(\"committer\") or {}).get(\"date\") or \"\")).strip() or None\n    latest_source = f\"github_commits_head:{chosen_path}\" if latest_date and chosen_path else \"unavailable\"\n\n    installed = str(installed_version or \"\").strip()\n    latest = str(latest_version or \"\").strip()\n\n    if latest_date and installed and latest and installed == latest:\n        return latest_date, \"same_as_latest\", latest_date, latest_source\n\n    if installed and installed not in {\"unknown\", \"latest\"}:\n        for commit in commits:\n            message = str(((commit.get(\"commit\") or {}).get(\"message\") or \"\"))\n            if installed in message:\n                commit_date = str((((commit.get(\"commit\") or {}).get(\"committer\") or {}).get(\"date\") or \"\")).strip()\n                if commit_date:\n                    return (\n                        commit_date,\n                        f\"github_commit_message_match:{chosen_path}\",\n                        latest_date,\n                        latest_source,\n                    )\n\n    if latest_date:\n        return (\n            latest_date,\n            f\"github_fallback_latest:{chosen_path}\",\n            latest_date,\n            latest_source,\n        )\n\n    return None, \"unavailable\", None, \"unavailable\"\n\n\ndef load_release_cache() -> dict[str, Any]:\n    return read_json(RELEASE_CACHE_FILE) or {}\n\n\ndef save_release_cache(cache: dict[str, Any]) -> None:\n    atomic_write_json(RELEASE_CACHE_FILE, cache)\n\n\ndef release_cache_key(pkg: dict[str, Any]) -> str:\n    return \"|\".join(\n        [\n            str(pkg.get(\"kind\") or \"\"),\n            str(pkg.get(\"name\") or \"\"),\n            str(pkg.get(\"installed_version\") or \"\"),\n            str(pkg.get(\"latest_version\") or \"\"),\n            str(pkg.get(\"tap\") or \"\"),\n        ]\n    )\n\n\ndef cache_entry_fresh(entry: dict[str, Any]) -> bool:\n    if not entry.get(\"current_release_date\") and not entry.get(\"latest_release_date\"):\n        return False\n\n    checked_at = str(entry.get(\"checked_at\") or \"\").strip()\n    if not checked_at:\n        return False\n\n    try:\n        ts = dt.datetime.fromisoformat(checked_at)\n    except ValueError:\n        return False\n\n    if ts.tzinfo is None:\n        ts = ts.replace(tzinfo=dt.timezone.utc)\n\n    age = dt.datetime.now(dt.timezone.utc) - ts.astimezone(dt.timezone.utc)\n    return age <= dt.timedelta(days=RELEASE_CACHE_TTL_DAYS)\n\n\ndef resolve_release_dates(\n    pkg: dict[str, Any],\n    cache: dict[str, Any],\n    allow_github_fallback: bool,\n) -> tuple[str | None, str, str | None, str]:\n    key = release_cache_key(pkg)\n    cached = cache.get(key)\n    if isinstance(cached, dict) and cache_entry_fresh(cached):\n        return (\n            cached.get(\"current_release_date\"),\n            str(cached.get(\"current_release_date_source\") or \"unavailable\"),\n            cached.get(\"latest_release_date\"),\n            str(cached.get(\"latest_release_date_source\") or \"unavailable\"),\n        )\n\n    tap = normalize_tap(str(pkg.get(\"tap\") or \"\"), str(pkg.get(\"kind\") or \"\"))\n    repo = resolve_repo_path(tap)\n\n    current_release_date: str | None = None\n    latest_release_date: str | None = None\n    current_source = \"unavailable\"\n    latest_source = \"unavailable\"\n    target_file: Path | None = None\n\n    if repo:\n        target_file = find_formula_or_cask_file(repo, pkg)\n        if target_file:\n            latest_release_date = git_version_intro_iso(repo, target_file, str(pkg.get(\"latest_version\") or \"\"))\n            if latest_release_date:\n                latest_source = \"tap_git_latest_version_intro\"\n\n            if not latest_release_date:\n                latest_release_date = git_last_commit_iso(repo, target_file)\n                if latest_release_date:\n                    latest_source = \"tap_git_last_change\"\n\n            current_release_date = git_version_intro_iso(repo, target_file, str(pkg.get(\"installed_version\") or \"\"))\n            if current_release_date:\n                current_source = \"tap_git_version_intro\"\n\n    if (not latest_release_date or not current_release_date) and allow_github_fallback and bool(pkg.get(\"outdated\")):\n        candidate_paths = github_candidate_paths(pkg, repo, target_file)\n        gh_current, gh_current_source, gh_latest, gh_latest_source = github_release_dates_for_package(\n            tap,\n            candidate_paths,\n            str(pkg.get(\"installed_version\") or \"\"),\n            str(pkg.get(\"latest_version\") or \"\"),\n        )\n        if not latest_release_date and gh_latest:\n            latest_release_date = gh_latest\n            latest_source = gh_latest_source\n        if not current_release_date and gh_current:\n            current_release_date = gh_current\n            current_source = gh_current_source\n\n    if not current_release_date and str(pkg.get(\"installed_version\") or \"\") == str(pkg.get(\"latest_version\") or \"\"):\n        current_release_date = latest_release_date\n        if current_release_date:\n            current_source = \"same_as_latest\"\n\n    if not current_release_date and latest_release_date:\n        current_release_date = latest_release_date\n        current_source = \"fallback_latest\"\n\n    cache[key] = {\n        \"checked_at\": now_iso(),\n        \"current_release_date\": current_release_date,\n        \"current_release_date_source\": current_source,\n        \"latest_release_date\": latest_release_date,\n        \"latest_release_date_source\": latest_source,\n    }\n\n    return current_release_date, current_source, latest_release_date, latest_source\n\n\ndef enrich_release_dates(\n    packages: list[dict[str, Any]],\n    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,\n) -> None:\n    cache = load_release_cache()\n    changed_cache = False\n\n    outdated_keys: list[tuple[str, str]] = []\n    for pkg in packages:\n        if pkg.get(\"outdated\"):\n            outdated_keys.append((str(pkg.get(\"kind\") or \"\"), str(pkg.get(\"name\") or \"\")))\n\n    github_budget_set = set(outdated_keys[:MAX_GITHUB_FALLBACK_PER_RUN])\n\n    total = len(packages)\n\n    for index, pkg in enumerate(packages, start=1):\n        key = (str(pkg.get(\"kind\") or \"\"), str(pkg.get(\"name\") or \"\"))\n        allow_github = key in github_budget_set\n\n        current_release_date, current_source, latest_release_date, latest_source = resolve_release_dates(\n            pkg,\n            cache,\n            allow_github,\n        )\n        pkg[\"current_release_date\"] = current_release_date\n        pkg[\"current_release_date_source\"] = current_source\n        pkg[\"latest_release_date\"] = latest_release_date\n        pkg[\"latest_release_date_source\"] = latest_source\n\n        if pkg.get(\"outdated\"):\n            pkg[\"outdated_since\"] = latest_release_date\n            pkg[\"days_since_outdated\"] = compute_days_since(latest_release_date)\n        else:\n            pkg[\"outdated_since\"] = None\n            pkg[\"days_since_outdated\"] = None\n\n        # Backward-compatible fields for older UI clients.\n        pkg[\"release_date\"] = latest_release_date\n        pkg[\"release_date_source\"] = latest_source\n        changed_cache = True\n\n        if progress_callback:\n            progress_callback(index, total, pkg)\n\n    if changed_cache:\n        save_release_cache(cache)\n\n\ndef _translate_to_he(text: str) -> str | None:\n    source = str(text or \"\").strip()\n    if not source:\n        return \"\"\n\n    try:\n        url = (\n            \"https://translate.googleapis.com/translate_a/single\"\n            f\"?client=gtx&sl=en&tl=he&dt=t&q={quote(source)}\"\n        )\n        req = Request(url, headers={\"User-Agent\": \"Mozilla/5.0\"})\n        with urlopen(req, timeout=8) as response:\n            payload = json.loads(response.read().decode(\"utf-8\", errors=\"replace\"))\n\n        parts: list[str] = []\n        for chunk in payload[0] if isinstance(payload, list) and payload else []:\n            if isinstance(chunk, list) and chunk:\n                piece = str(chunk[0] or \"\")\n                if piece:\n                    parts.append(piece)\n\n        translated = \"\".join(parts).strip()\n        return translated or None\n    except Exception as exc:\n        append_service_log(f\"Translation warning: {exc}\")\n        return None\n\n\ndef enrich_hebrew_descriptions(\n    packages: list[dict[str, Any]],\n    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,\n) -> None:\n    cache = read_json(DESCRIPTION_HE_CACHE_FILE) or {}\n    if not isinstance(cache, dict):\n        cache = {}\n\n    translations_done = 0\n    cache_changed = False\n\n    total = len(packages)\n\n    for index, pkg in enumerate(packages, start=1):\n        desc_en = str(pkg.get(\"description\") or \"\").strip()\n        if not desc_en:\n            pkg[\"description_he\"] = \"\"\n            if progress_callback:\n                progress_callback(index, total, pkg)\n            continue\n\n        cached = cache.get(desc_en)\n        if isinstance(cached, str) and cached.strip():\n            pkg[\"description_he\"] = cached\n            if progress_callback:\n                progress_callback(index, total, pkg)\n            continue\n\n        if translations_done >= MAX_TRANSLATIONS_PER_RUN:\n            pkg[\"description_he\"] = desc_en\n            if progress_callback:\n                progress_callback(index, total, pkg)\n            continue\n\n        translated = _translate_to_he(desc_en)\n        if translated:\n            cache[desc_en] = translated\n            pkg[\"description_he\"] = translated\n            cache_changed = True\n        else:\n            pkg[\"description_he\"] = desc_en\n\n        translations_done += 1\n\n        if progress_callback:\n            progress_callback(index, total, pkg)\n\n    if cache_changed:\n        atomic_write_json(DESCRIPTION_HE_CACHE_FILE, cache)\n\n\ndef compute_snapshot(run_brew_update, track_progress=False, *, enrich_details=True):\n    return mac_manager.combined(sys.modules[__name__], compute_homebrew_snapshot,\n                                run_brew_update, track_progress, enrich_details)\n\n\ndef summarize_counts(packages: list[dict[str, Any]]) -> dict[str, int]:\n    formulas = [pkg for pkg in packages if pkg.get(\"kind\") == \"formula\"]\n    casks = [pkg for pkg in packages if pkg.get(\"kind\") == \"cask\"]\n    outdated = [pkg for pkg in packages if pkg.get(\"outdated\")]\n    return {\n        \"excluded_total\": sum(bool(pkg.get(\"excluded\")) for pkg in packages),\n        \"managed_total\": sum(pkg.get(\"provider\", \"homebrew\") != \"homebrew\" for pkg in packages),\n        \"total\": len(packages),\n        \"formulae\": len(formulas),\n        \"ports\": sum(p.get(\"kind\") == \"port\" for p in packages),\n        \"tools\": sum(p.get(\"kind\") == \"tool\" for p in packages),\n        \"casks\": len(casks),\n        \"outdated_total\": len(outdated),\n        \"outdated_formulae\": sum(1 for pkg in outdated if pkg.get(\"kind\") == \"formula\"),\n        \"outdated_casks\": sum(1 for pkg in outdated if pkg.get(\"kind\") == \"cask\"),\n    }\n\n\ndef reuse_cached_package_details(packages: list[dict[str, Any]]) -> None:\n    previous = read_json(STATE_FILE) or {}\n    cached = {(p.get(\"kind\"), p.get(\"name\")): p for p in previous.get(\"packages\", []) if isinstance(p, dict)}\n    for pkg in packages:\n        old = cached.get((pkg.get(\"kind\"), pkg.get(\"name\")), {})\n        same_installed = pkg.get(\"installed_version\") == old.get(\"installed_version\")\n        same_latest = pkg.get(\"latest_version\") == old.get(\"latest_version\")\n        if same_installed:\n            for key in (\"current_release_date\", \"current_release_date_source\"):\n                if key in old:\n                    pkg[key] = old[key]\n        if same_latest:\n            for key in (\"latest_release_date\", \"latest_release_date_source\"):\n                if key in old:\n                    pkg[key] = old[key]\n        if same_installed and same_latest:\n            for key in (\"release_date\", \"release_date_source\"):\n                if key in old:\n                    pkg[key] = old[key]\n        if pkg.get(\"description\") == old.get(\"description\") and old.get(\"description_he\"):\n            pkg[\"description_he\"] = old[\"description_he\"]\n        if pkg.get(\"outdated\") and pkg.get(\"latest_release_date\"):\n            pkg[\"outdated_since\"] = pkg[\"latest_release_date\"]\n            pkg[\"days_since_outdated\"] = compute_days_since(pkg[\"outdated_since\"])\n\n\ndef compute_homebrew_snapshot(run_brew_update: bool, track_progress: bool = False, *, enrich_details: bool = True, complete_progress: bool = True) -> dict[str, Any]:\n    append_check_log(\"Starting Homebrew check\")\n    errors: list[str] = []\n\n    if track_progress:\n        reset_check_progress_for_run()\n        set_check_progress(operation_name=\"check\", phase=\"preparing\", message=\"Preparing Homebrew inventory\")\n\n    progress_started_at = read_check_progress().get(\"started_at\") if track_progress else None\n\n    b = brew_bin()\n\n    try:\n        if run_brew_update:\n            if track_progress:\n                set_check_progress(\n                    phase=\"brew_update\",\n                    message=\"Running brew update\",\n                    done=0,\n                    total=1,\n                    eta_seconds=None,\n                    current_package=\"brew update\",\n                )\n\n            append_check_log(\"Running brew update...\")\n            upd = run_cmd([b, \"update\"], timeout=3600)\n            if upd.code != 0:\n                err = upd.stderr or upd.stdout or \"unknown error\"\n                errors.append(f\"brew update failed: {err}\")\n                append_check_log(f\"Warning: brew update failed: {err}\")\n            else:\n                append_check_log(\"brew update completed\")\n\n            if track_progress:\n                set_check_progress(done=1, total=1, eta_seconds=0)\n\n        if track_progress:\n            set_check_progress(\n                phase=\"collecting_outdated\",\n                message=\"Collecting outdated index\",\n                done=0,\n                total=1,\n                eta_seconds=None,\n                current_package=\"brew outdated --json=v2\",\n            )\n\n        outdated_index = fetch_outdated_index()\n\n        if track_progress:\n            set_check_progress(done=1, total=1, eta_seconds=0)\n            set_check_progress(\n                phase=\"collecting_installed\",\n                message=\"Collecting installed packages\",\n                done=0,\n                total=1,\n                eta_seconds=None,\n                current_package=\"brew info --json=v2 --installed\",\n            )\n\n        packages = fetch_installed_inventory(outdated_index)\n\n        if track_progress:\n            set_check_progress(done=1, total=1, eta_seconds=0)\n\n        total_packages = len(packages)\n\n        def _release_progress(done: int, total: int, pkg: dict[str, Any]) -> None:\n            if not track_progress:\n                return\n            raise_if_cancel_requested()\n            eta = estimate_eta_seconds(progress_started_at, done, total)\n            current = f\"{pkg.get('kind')}:{pkg.get('name')}\"\n            set_check_progress(\n                phase=\"resolving_dates\",\n                message=f\"Resolving release dates ({done}/{total})\",\n                done=done,\n                total=total,\n                eta_seconds=eta,\n                current_package=current,\n            )\n\n        if track_progress:\n            set_check_progress(\n                phase=\"resolving_dates\",\n                message=\"Resolving release dates\",\n                done=0,\n                total=total_packages,\n                eta_seconds=None,\n                current_package=\"\",\n            )\n\n        if enrich_details:\n            enrich_release_dates(packages, progress_callback=_release_progress if track_progress else None)\n        else:\n            reuse_cached_package_details(packages)\n\n        def _translation_progress(done: int, total: int, pkg: dict[str, Any]) -> None:\n            if not track_progress:\n                return\n            raise_if_cancel_requested()\n            eta = estimate_eta_seconds(progress_started_at, done, total)\n            current = f\"{pkg.get('kind')}:{pkg.get('name')}\"\n            set_check_progress(\n                phase=\"translating_descriptions\",\n                message=f\"Translating descriptions ({done}/{total})\",\n                done=done,\n                total=total,\n                eta_seconds=eta,\n                current_package=current,\n            )\n\n        if track_progress:\n            set_check_progress(\n                phase=\"translating_descriptions\",\n                message=\"Translating descriptions\",\n                done=0,\n                total=total_packages,\n                eta_seconds=None,\n                current_package=\"\",\n            )\n\n        if enrich_details:\n            enrich_hebrew_descriptions(packages, progress_callback=_translation_progress if track_progress else None)\n\n        apply_update_policy(packages)\n        packages.sort(key=lambda item: (str(item.get(\"kind\")), str(item.get(\"name\"))))\n\n        snapshot = {\n            \"app\": APP_NAME,\n            \"updated_at\": now_iso(),\n            \"counts\": summarize_counts(packages),\n            \"errors\": errors,\n            \"packages\": packages,\n        }\n\n        # The combined inventory wrapper persists a complete snapshot.\n        append_check_log(\n            f\"Check completed. total={snapshot['counts']['total']} outdated={snapshot['counts']['outdated_total']}\"\n        )\n\n        if track_progress and complete_progress:\n            set_check_progress(\n                running=False,\n                phase=\"completed\",\n                message=\"Check completed\",\n                done=total_packages,\n                total=total_packages,\n                eta_seconds=0,\n                current_package=\"\",\n                error=None,\n                completed_at=now_iso(),\n            )\n\n        return snapshot\n    except Exception as exc:\n        if track_progress and complete_progress:\n            set_check_progress(\n                running=False,\n                phase=\"error\",\n                message=f\"Check failed: {exc}\",\n                eta_seconds=0,\n                error=str(exc),\n                completed_at=now_iso(),\n            )\n        raise\n\n\ndef validate_package_name(name: str) -> None:\n    if not name or not SAFE_NAME_RE.match(name):\n        raise ValueError(\"Invalid package name\")\n\ndef normalize_selected_packages(raw_packages: Any) -> list[dict[str, str]]:\n    if not isinstance(raw_packages, list) or not raw_packages:\n        raise ValueError(\"packages must be a non-empty array\")\n\n    normalized: list[dict[str, str]] = []\n    seen: set[tuple[str, str]] = set()\n\n    for item in raw_packages:\n        if not isinstance(item, dict):\n            raise ValueError(\"each packages item must be an object\")\n\n        name = str(item.get(\"name\") or \"\").strip()\n        kind = str(item.get(\"kind\") or \"\").strip()\n\n        validate_package_name(name)\n        if kind not in {\"formula\", \"cask\", \"port\", \"tool\"}:\n            raise ValueError(\"kind must be 'formula' or 'cask'\")\n\n        key = (kind, name)\n        if key in seen:\n            continue\n\n        seen.add(key)\n        normalized.append({\"name\": name, \"kind\": kind})\n\n    if not normalized:\n        raise ValueError(\"packages must include at least one valid package\")\n\n    return normalized\n\n\ndef progress_package_item(name: str, kind: str) -> dict[str, Any]:\n    normalized_name = str(name or \"\").strip()\n    normalized_kind = str(kind or \"\").strip()\n    return {\n        \"name\": normalized_name,\n        \"kind\": normalized_kind,\n        \"label\": f\"{normalized_kind}:{normalized_name}\",\n    }\n\n\ndef summarize_result_error(result: dict[str, Any], limit: int = 220) -> str:\n    detail = str(result.get(\"stderr\") or result.get(\"stdout\") or \"\").strip()\n    if not detail:\n        return \"\"\n    if len(detail) <= limit:\n        return detail\n    return f\"{detail[: max(0, limit - 1)]}…\"\n\n\ndef progress_result_item(result: dict[str, Any], *, started_at: str | None = None) -> dict[str, Any]:\n    name = str(result.get(\"name\") or \"\").strip()\n    kind = str(result.get(\"kind\") or \"\").strip()\n    item = {\n        \"name\": name,\n        \"kind\": kind,\n        \"label\": f\"{kind}:{name}\",\n        \"ok\": bool(result.get(\"ok\", False)),\n        \"skipped\": bool(result.get(\"skipped\", False)),\n        \"canceled\": bool(result.get(\"canceled\", False)),\n        \"reason\": str(result.get(\"reason\") or \"\").strip(),\n        \"code\": int(result.get(\"code\") or 0),\n        \"error\": summarize_result_error(result),\n        \"started_at\": started_at,\n        \"completed_at\": now_iso(),\n    }\n    return item\n\nclass UpgradeActivity:\n    \"\"\"Translate explicit Homebrew output into conservative UI stage identifiers.\"\"\"\n\n    def __init__(self, name: str):\n        self.lock = threading.Lock()\n        self.started_epoch = time.time()\n        self.monitor_stop = threading.Event()\n        self.state: dict[str, Any] = {\n            \"target\": name, \"subject\": name, \"stage\": \"preparing\",\n            \"started_at\": now_iso(), \"stage_started_at\": now_iso(),\n            \"last_output_at\": None, \"technical_tail\": [],\n        }\n        self.publish()\n\n    def monitor_build_logs(self) -> None:\n        def follow():\n            last_signature = None\n            while not self.monitor_stop.wait(1.5):\n                subject = self.state[\"subject\"]\n                directory = Path.home() / \"Library/Logs/Homebrew\" / subject\n                candidates = [p for p in directory.glob(\"*.log\") if not p.name.endswith(\".cc.log\")\n                              and p.stat().st_mtime >= self.started_epoch]\n                if not candidates:\n                    continue\n                path = max(candidates, key=lambda p: p.stat().st_mtime)\n                signature = (str(path), path.stat().st_size, path.stat().st_mtime_ns)\n                if signature == last_signature:\n                    continue\n                last_signature = signature\n                try:\n                    with path.open(\"rb\") as stream:\n                        stream.seek(max(0, path.stat().st_size - 18000))\n                        tail = stream.read().decode(errors=\"replace\")\n                    tasks = re.findall(r\"\\[(\\d+)/(\\d+)\\]\", tail)\n                    percentages = re.findall(r\"\\[\\s*(\\d+)%\\]\", tail)\n                    with self.lock:\n                        self.state[\"last_output_at\"] = now_iso()\n                        if tasks:\n                            done, total = map(int, tasks[-1])\n                            self.state[\"build_progress\"] = {\"kind\": \"tasks\", \"done\": done, \"total\": total, \"percent\": round(100 * done / max(1, total))}\n                            self.state[\"stage\"] = \"building\"\n                        elif percentages:\n                            self.state[\"build_progress\"] = {\"kind\": \"reported_percent\", \"percent\": int(percentages[-1])}\n                            self.state[\"stage\"] = \"building\"\n                        self.publish()\n                except OSError:\n                    continue\n        self.monitor_thread = threading.Thread(target=follow, daemon=True)\n        self.monitor_thread.start()\n\n    def stop_monitor(self) -> None:\n        self.monitor_stop.set()\n        if hasattr(self, \"monitor_thread\"):\n            self.monitor_thread.join(timeout=2)\n\n    def publish(self) -> None:\n        with CHECK_PROGRESS_LOCK:\n            CHECK_PROGRESS[\"package_activity\"] = {\n                **self.state, \"technical_tail\": list(self.state[\"technical_tail\"]),\n            }\n\n    def observe(self, raw: str, source: str) -> None:\n        line = re.sub(r\"\\x1b\\[[0-?]*[ -/]*[@-~]\", \"\", raw).strip()\n        if not line:\n            return\n        with self.lock:\n            append_log(LOG_DIR / \"brew-upgrades.log\", f\"[{source}] {line}\")\n            self.state[\"last_output_at\"] = now_iso()\n            self.state[\"technical_tail\"] = (self.state[\"technical_tail\"] + [line[-1000:]])[-12:]\n            stage = self.state[\"stage\"]\n            subject = self.state[\"subject\"]\n            dependency = re.match(r\"==> (?:Installing|Upgrading) .+? dependency: (\\S+)\", line)\n            target = re.match(r\"==> (?:Installing|Upgrading) (\\S+)\", line)\n            if source == \"provider\" and line.startswith(\"Downloading ready-to-use\"):\n                stage = \"downloading\"\n            elif source == \"provider\" and line == \"Verifying installed commands\":\n                stage = \"finishing\"\n            elif dependency:\n                subject, stage = dependency.group(1), \"dependency\"\n            elif line.startswith(\"==> Fetching downloads for:\") or line.startswith(\"==> Downloading \"):\n                stage = \"downloading\"\n            elif line.startswith(\"==> Pouring \"):\n                stage = \"installing\"\n            elif line.startswith(\"==> \") and re.match(\n                r\"(?:\\./bootstrap|\\./configure|cmake\\b|make\\b|gmake\\b|ninja\\b|meson\\b|cargo\\b|go build\\b)\", line[4:]\n            ):\n                stage = \"building\"\n            elif target and target.group(1) == self.state[\"target\"]:\n                subject, stage = self.state[\"target\"], \"installing\"\n            elif line.startswith(\"==> Caveats\") or line.startswith(\"==> Running `brew cleanup\"):\n                stage = \"finishing\"\n            if stage != self.state[\"stage\"] or subject != self.state[\"subject\"]:\n                self.state[\"stage_started_at\"] = now_iso()\n                self.state.pop(\"build_progress\", None)\n            self.state.update(stage=stage, subject=subject)\n            self.publish()\n\n\ndef run_upgrade(name: str, kind: str) -> dict[str, Any]:\n    validate_package_name(name)\n    if is_excluded(name, kind):\n        return {\"ok\": False, \"skipped\": True, \"reason\": \"excluded\", \"name\": name, \"kind\": kind,\n                \"code\": 78, \"stdout\": \"\", \"stderr\": \"Package is excluded from updates\"}\n    if kind == \"tool\":\n        from update_engine import Engine\n        engine = Engine(sys.modules[__name__])\n        state = compute_snapshot(False, enrich_details=False)\n        pkg = package_from_snapshot(state, name, kind)\n        if not pkg:\n            raise ValueError(\"Managed installation not found\")\n        item = engine.plan([pkg])[0]\n        if not item['ready']:\n            raise ValueError(item.get('detail', item.get('reason', 'Unavailable')))\n        return dict(engine.install_tool(item), kind='tool')\n    if kind == \"port\":\n        pkg = next((p for p in macports_provider.inventory(sys.modules[__name__]) if p[\"name\"] == name), None)\n        if not pkg:\n            raise ValueError(\"MacPorts package is not installed; use migration approval\")\n        return macports_provider.upgrade(sys.modules[__name__], pkg)\n    settings = load_settings()\n    from update_engine import Engine\n    engine = Engine(sys.modules[__name__])\n    if routes.route_for(name, kind, settings) != \"homebrew\" and name not in routes.read_records(TOOLS_ROOT):\n        state = read_json(STATE_FILE) or compute_snapshot(False, enrich_details=False)\n        pkg = package_from_snapshot(state, name, kind)\n        if not pkg:\n            raise ValueError(\"Package is not in the installed inventory\")\n        item = engine.plan([pkg])[0]\n        if not item[\"ready\"]:\n            return {\"ok\": False, \"name\": name, \"kind\": kind, \"code\": 78, \"stdout\": \"\",\n                    \"stderr\": item.get(\"detail\", item[\"reason\"]), \"reason\": item[\"reason\"]}\n        return engine.install_tool(item)\n    if kind == \"formula\" and settings[\"binary_only\"]:\n        blockers = routes.bottle_blockers(name, engine.catalog(), engine.installed(), settings[\"excluded_packages\"])\n        if blockers:\n            return {\"ok\": False, \"name\": name, \"kind\": kind, \"code\": 78, \"stdout\": \"\", \"reason\": \"source_required\",\n                    \"stderr\": \"Source build was deferred: \" + \", \".join(x[\"name\"] for x in blockers)}\n    b = brew_bin()\n\n    if kind == \"formula\":\n        cmd = [b, \"upgrade\", name]\n    elif kind == \"cask\":\n        cmd = [b, \"upgrade\", \"--cask\", name]\n    else:\n        raise ValueError(\"kind must be 'formula' or 'cask'\")\n\n    append_check_log(f\"Starting upgrade: {kind}:{name}\")\n    append_log(LOG_DIR / \"brew-upgrades.log\", f\"Starting upgrade: {kind}:{name}\")\n    activity = UpgradeActivity(name)\n    activity.monitor_build_logs()\n    source_fallback = False\n\n    def observe(line: str, source: str) -> None:\n        nonlocal source_fallback\n        activity.observe(line, source)\n        if settings[\"binary_only\"] and activity.state[\"stage\"] == \"building\":\n            source_fallback = True\n            with ACTIVE_COMMAND_LOCK:\n                proc = ACTIVE_COMMAND.get(\"process\")\n            terminate_process(proc)\n\n    try:\n        res = run_cmd(cmd, timeout=settings[\"upgrade_timeout_seconds\"] or None, cancellable=True,\n                      on_output=observe, env_override=engine.brew_env)\n    finally:\n        activity.stop_monitor()\n        with CHECK_PROGRESS_LOCK:\n            CHECK_PROGRESS[\"package_activity\"] = None\n    ok = res.code == 0 and not source_fallback\n    if source_fallback:\n        res.code = 78\n        res.stderr += \"\\nUnexpected source fallback stopped by binary-only policy\"\n    append_log(LOG_DIR / \"brew-upgrades.log\", f\"Finished upgrade: {kind}:{name}; exit_code={res.code}\")\n    canceled = bool(is_cancel_requested() and not ok)\n\n    if ok:\n        append_check_log(f\"Upgrade succeeded: {kind}:{name}\")\n    elif canceled:\n        append_check_log(f\"Upgrade cancelled: {kind}:{name}: {res.stderr or res.stdout}\")\n    else:\n        append_check_log(f\"Upgrade failed: {kind}:{name}: {res.stderr or res.stdout}\")\n\n    return {\n        \"ok\": ok,\n        \"canceled\": canceled,\n        \"name\": name,\n        \"kind\": kind,\n        \"code\": res.code,\n        \"stdout\": res.stdout,\n        \"stderr\": res.stderr,\n        \"reason\": \"source_fallback\" if source_fallback else \"\",\n    }\n\n\ndef package_from_snapshot(snapshot: dict[str, Any], name: str, kind: str) -> dict[str, Any] | None:\n    for pkg in snapshot.get(\"packages\", []) if isinstance(snapshot, dict) else []:\n        if str(pkg.get(\"name\") or \"\") == str(name or \"\") and str(pkg.get(\"kind\") or \"\") == str(kind or \"\"):\n            return pkg\n    return None\n\n\ndef history_entries_from_results(\n    results: list[dict[str, Any]],\n    snapshot: dict[str, Any],\n    *,\n    snapshot_verified: bool = True,\n    snapshot_error: str | None = None,\n) -> list[dict[str, Any]]:\n    entries: list[dict[str, Any]] = []\n    for item in results:\n        name = str(item.get(\"name\") or \"\").strip()\n        kind = str(item.get(\"kind\") or \"\").strip()\n        if not name or not kind:\n            continue\n\n        pkg = package_from_snapshot(snapshot, name, kind) or {}\n        latest_version = str(pkg.get(\"latest_version\") or \"\")\n        installed_version = str(pkg.get(\"installed_version\") or \"\")\n        verified_latest = bool(\n            item.get(\"ok\") and snapshot_verified and pkg and not bool(pkg.get(\"outdated\"))\n        )\n\n        note = \"\"\n        if snapshot_error:\n            note = f\"inventory_refresh_failed: {snapshot_error}\"\n\n        entries.append(\n            {\n                \"timestamp\": now_iso(),\n                \"name\": name,\n                \"kind\": kind,\n                \"ok\": bool(item.get(\"ok\", False)),\n                \"verified_latest\": verified_latest,\n                \"installed_version\": installed_version,\n                \"latest_version\": latest_version,\n                \"note\": note,\n            }\n        )\n\n    return entries\n\n\ndef fallback_snapshot_with_error(previous_snapshot: dict[str, Any] | None, error: str) -> dict[str, Any]:\n    payload = dict(previous_snapshot) if isinstance(previous_snapshot, dict) else {}\n\n    packages = payload.get(\"packages\")\n    if not isinstance(packages, list):\n        packages = []\n    payload[\"packages\"] = packages\n\n    counts = payload.get(\"counts\")\n    if not isinstance(counts, dict):\n        counts = summarize_counts(packages)\n    payload[\"counts\"] = counts\n\n    existing_errors = payload.get(\"errors\")\n    errors_list = list(existing_errors) if isinstance(existing_errors, list) else []\n    errors_list.append(f\"snapshot refresh failed: {error}\")\n    payload[\"errors\"] = errors_list\n\n    payload[\"app\"] = str(payload.get(\"app\") or APP_NAME)\n    payload[\"updated_at\"] = now_iso()\n    return payload\n\n\ndef safe_refresh_snapshot(previous_snapshot: dict[str, Any] | None = None) -> tuple[dict[str, Any], str | None]:\n    try:\n        refreshed = compute_snapshot(run_brew_update=False, enrich_details=False)\n        return refreshed, (\"Partial provider inventory\" if refreshed.get(\"partial\") else None)\n    except Exception as exc:\n        detail = str(exc)\n        append_check_log(f\"Warning: package inventory refresh failed after update: {detail}\")\n        append_service_log(f\"Warning: package inventory refresh failed after update: {detail}\")\n        return fallback_snapshot_with_error(previous_snapshot, detail), detail\n\n\ndef run_update_all(track_progress: bool = False) -> dict[str, Any]:\n    from update_engine import Engine\n    return Engine(sys.modules[__name__]).run(track_progress=track_progress)\n\n\ndef run_update_selected(selected_packages: list[dict[str, str]], track_progress: bool = False) -> dict[str, Any]:\n    from update_engine import Engine\n    return Engine(sys.modules[__name__]).run(selected_packages, track_progress=track_progress)\n\n\ndef escape_applescript(value: str) -> str:\n    return value.replace(\"\\\\\", \"\\\\\\\\\").replace('\"', '\\\\\"')\n\n\ndef send_notification(snapshot: dict[str, Any], notifier_override=None) -> None:\n    outdated = [pkg for pkg in snapshot.get(\"packages\", []) if pkg.get(\"outdated\")]\n    if not outdated:\n        title = \"Mac Update Manager\"\n        message = \"All packages are up to date\"\n    else:\n        names = [str(pkg.get(\"name\")) for pkg in outdated]\n        preview = \", \".join(names[:5])\n        suffix = \"\" if len(names) <= 5 else f\", +{len(names)-5} more\"\n        title = \"Mac Updates Available\"\n        message = f\"{len(names)} package(s): {preview}{suffix}\"\n\n    if snapshot.get(\"notification_test\"):\n        title = \"Mac Update Manager\"\n        message = \"Notification test: the MacPorts migration was verified\"\n    helper = Path.home() / \"Applications\" / \"Homebrew Update Notifications.app\" / \"Contents\" / \"MacOS\" / \"terminal-notifier\"\n    route = read_json(DATA_DIR / 'notifier-route.json') or {}\n    selected = route.get('path') if route.get('provider') in {'macports', 'application_helper'} else None\n    notifier = notifier_override or selected or (str(helper) if helper.is_file() else shutil.which(\"terminal-notifier\"))\n    if not notifier:\n        raise RuntimeError(\"The Homebrew notification helper is not installed\")\n\n    # Version 3 uses UserNotifications and stores click actions with macOS,\n    # so this command exits without leaving a listener running.\n    version = run_cmd([notifier, \"-version\"], timeout=10)\n    version_text = f\"{version.stdout} {version.stderr}\"\n    if not re.search(r\"\\b[3-9]\\.\\d+\", version_text):\n        raise RuntimeError(\"Homebrew notifications require terminal-notifier 3 or later\")\n\n    command = \"/bin/bash \" + shlex.quote(str(OPEN_APP_SCRIPT))\n    result = run_cmd([\n        notifier, \"-title\", title,\n        \"-subtitle\", \"Mac Update Check\",\n        \"-message\", message,\n        \"-sound\", \"default\",\n        \"-group\", \"homebrew-update-manager\",\n        \"-execute\", command,\n    ], timeout=60)\n    if result.code != 0:\n        detail = result.stderr or result.stdout or f\"exit code {result.code}\"\n        append_check_log(f\"Notification failed: {detail}\")\n        raise RuntimeError(f\"macOS notification failed: {detail}\")\n    append_check_log(f\"Notification accepted by macOS: {message}\")\n\n\n\nclass BrewUpdatesHandler(BaseHTTPRequestHandler):\n    server_version = \"BrewUpdatesHTTP/2.0\"\n\n    def log_message(self, fmt: str, *args):\n        append_service_log(fmt % args)\n\n    def _send_json(self, code: int, payload: dict[str, Any]) -> None:\n        body = json.dumps(payload, ensure_ascii=False).encode(\"utf-8\")\n        self.send_response(code)\n        self.send_header(\"Content-Type\", \"application/json; charset=utf-8\")\n        self.send_header(\"Content-Length\", str(len(body)))\n        self.end_headers()\n        self.wfile.write(body)\n\n    def _read_json_body(self) -> dict[str, Any]:\n        length = int(self.headers.get(\"Content-Length\", \"0\"))\n        if length <= 0:\n            return {}\n        raw = self.rfile.read(length)\n        if not raw:\n            return {}\n        try:\n            return json.loads(raw.decode(\"utf-8\"))\n        except json.JSONDecodeError:\n            return {}\n\n    def _mac_operation(self):\n        if self.path not in {\"/api/migrations/plan\", \"/api/migrations/approve\", \"/api/migrations/recovery-plan\", \"/api/macports/sync\"}:\n            return False\n        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or self.headers.get('Origin'):\n            self._send_json(403, {'ok': False, 'detail': 'Desktop JSON requests only'})\n            return True\n        body = self._read_json_body()\n        def operation():\n            reset_check_progress_for_run()\n            set_check_progress(operation_name=\"migration\", phase=\"preflight\", running=True,\n                               message=\"Checking MacPorts ready packages\", eta_seconds=None)\n            try:\n                if self.path == \"/api/migrations/plan\":\n                    return {\"ok\": True, \"plan\": mac_manager.migration_plan(sys.modules[__name__], body.get(\"name\"))}\n                if self.path == \"/api/migrations/recovery-plan\":\n                    return {\"ok\": True, \"plan\": mac_manager.migration_recovery_plan(sys.modules[__name__], body.get(\"transaction_id\"), body.get(\"action\"))}\n                if self.path == \"/api/migrations/approve\":\n                    raise unified_updates.PlanError(\"review_required\", \"Legacy migration approval is disabled until transactional recovery is complete\")\n                    result = mac_manager.approve(sys.modules[__name__], body.get(\"id\"))\n                    return {\"ok\": True, \"migration\": result, \"snapshot\": compute_snapshot(False, enrich_details=False)}\n                result = macports_provider.privileged(sys.modules[__name__], \"sync\")\n                if result.code: raise RuntimeError(result.stderr)\n                mac_manager.catalog_refresh.mark_success(sys.modules[__name__])\n                return {\"ok\": True}\n            finally:\n                with CHECK_PROGRESS_LOCK:\n                    CHECK_PROGRESS[\"package_activity\"] = None\n                canceled = isinstance(sys.exc_info()[1], OperationCancelledError)\n                set_check_progress(running=False, phase='canceled' if canceled else ('error' if sys.exc_info()[0] else 'completed'),\n                                   canceled=canceled, current_package=None,\n                                   completed_at=now_iso(), eta_seconds=None if sys.exc_info()[0] else 0)\n        try:\n            self._send_json(200, run_with_progress(\"migration\", operation))\n        except BusyError as exc:\n            self._send_json(409, {\"ok\": False, \"detail\": str(exc)})\n        except Exception as exc:\n            self._send_json(400, {\"ok\": False, \"detail\": str(exc), \"progress\": getattr(exc, \"operation_progress\", None)})\n        return True\n\n    def _apply_unified_request(self, body, *, all_ready=False):\n        try:\n            def operation():\n                reset_check_progress_for_run()\n                set_check_progress(operation_name=\"unified_update\", phase=\"preflight\",\n                                   message=\"Validating displayed provider decisions\",\n                                   running=True, eta_seconds=None)\n                return unified_updates.execute(sys.modules[__name__], body, all_ready=all_ready)\n            response = run_with_progress(\"update_unified\", operation)\n            self._send_json(200, response)\n        except unified_updates.PlanError as exc:\n            code = 400 if exc.code in {\"invalid_request\", \"invalid_request_id\", \"ambiguous_decisions\"} else 409\n            self._send_json(code, {\"ok\": False, \"error\": exc.code, \"detail\": str(exc),\n                                   \"plan\": unified_updates.read_plan(sys.modules[__name__])})\n        except (BusyError, operation_lock.OperationBusy) as exc:\n            self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc)})\n        except Exception as exc:\n            self._send_json(500, {\"ok\": False, \"error\": \"update_failed\", \"detail\": str(exc)})\n        return True\n\n    def do_GET(self):\n        if self.path == \"/health\":\n            operation = read_current_operation()\n            self._send_json(\n                200,\n                {\n                    \"ok\": True,\n                    \"app\": APP_NAME,\n                    \"operation\": operation,\n                    \"project_root\": str(PROJECT_ROOT),\n                    \"protocol\": 6,\n                    \"version\": \"1.3.3\",\n                    \"pid\": os.getpid(),\n                    \"data_dir\": str(DATA_DIR),\n                    \"capabilities\": [\"migration-v1\", \"migration-recovery\"],\n                    \"backend_sha256\": BACKEND_STARTUP_SHA,\n                    \"backend_code_sha256\": BACKEND_STARTUP_SHA,\n                    \"process_started_at\": PROCESS_STARTED_AT,\n                    \"build_id\": \"unified-intel-foundations\",\n                },\n            )\n            return\n\n        if self.path == \"/api/settings\":\n            try:\n                settings = load_settings()\n                self._send_json(\n                    200,\n                    {\n                        \"ok\": True,\n                        \"settings\": {\n                            **settings,\n                        },\n                        \"scheduler\": scheduler_status_payload(settings),\n                    },\n                )\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"settings_unavailable\", \"detail\": str(exc)})\n            return\n\n        if self.path == \"/api/brew/auto-detect\":\n            try:\n                settings = load_settings()\n                candidates = detect_brew_candidates()\n                self._send_json(\n                    200,\n                    {\n                        \"ok\": bool(candidates),\n                        \"recommended_path\": candidates[0] if candidates else None,\n                        \"candidates\": candidates,\n                        \"scan_performed\": True,\n                        \"scan_timestamp\": now_iso(),\n                        \"current_brew_path\": str(settings.get(\"brew_path\") or \"\"),\n                    },\n                )\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"brew_auto_detect_failed\", \"detail\": str(exc)})\n            return\n\n        if self.path == \"/api/state\":\n            state = read_json(STATE_FILE)\n            if not state or state.get(\"app\") != APP_NAME or \"providers\" not in state:\n                try:\n                    state = run_exclusive(\"initial_snapshot\", lambda: compute_snapshot(run_brew_update=False, enrich_details=False))\n                except BusyError as exc:\n                    self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n                    return\n                except Exception as exc:\n                    self._send_json(500, {\"ok\": False, \"error\": \"state_unavailable\", \"detail\": str(exc)})\n                    return\n            apply_update_policy(state.get(\"packages\", []))\n            state[\"counts\"] = summarize_counts(state.get(\"packages\", []))\n            state[\"update_plan\"] = read_json(UPDATE_PLAN_FILE)\n            blocked = {(x[\"kind\"], x[\"name\"]): x for x in (state[\"update_plan\"] or {}).get(\"items\", []) if not x[\"ready\"]}\n            for pkg in state.get(\"packages\", []):\n                item = blocked.get((pkg[\"kind\"], pkg[\"name\"]))\n                if pkg.get(\"inventory_stale\"):\n                    pkg[\"update_block_reason\"] = \"inventory_unavailable\"\n                    continue\n                pkg.pop(\"update_block_reason\", None)\n                if (pkg.get('kind') == 'formula' and load_settings()['binary_only'] and\n                    any(o['name'] == pkg['name'] and o.get('source_blockers') for o in state.get('migrations', []))):\n                    pkg['update_block_reason'] = 'source_required'\n                if item and pkg.get(\"outdated\") and item[\"version\"] == pkg[\"latest_version\"]:\n                    if item[\"reason\"] != \"source_required\" or load_settings()[\"binary_only\"]:\n                        pkg[\"update_block_reason\"] = item[\"reason\"]\n            unified_updates.attach_cached(sys.modules[__name__], state)\n            self._send_json(200, state)\n            return\n\n        if self.path == \"/api/update-plan\":\n            self._send_json(200, {\"ok\": True, \"plan\": unified_updates.read_plan(sys.modules[__name__])})\n            return\n\n        if self.path == \"/api/progress\":\n            self._send_json(200, runtime_status_payload())\n            return\n\n        if self.path == \"/api/update-history\":\n            try:\n                self._send_json(\n                    200,\n                    {\n                        \"ok\": True,\n                        \"items\": load_update_history(),\n                    },\n                )\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"update_history_unavailable\", \"detail\": str(exc)})\n            return\n\n        self._send_json(404, {\"ok\": False, \"error\": \"not_found\"})\n\n    def do_POST(self):\n        if self._mac_operation():\n            return\n        if self.path == \"/api/settings/update-policy\":\n            try:\n                if read_current_operation().get(\"running\"):\n                    raise BusyError(\"Wait for the active operation before changing update policy\")\n                body = self._read_json_body()\n                settings = load_settings()\n                settings.update(normalize_update_policy({**settings, **body}))\n                save_settings(settings)\n                self._send_json(200, {\"ok\": True, \"settings\": settings})\n            except (ValueError, TypeError) as exc:\n                self._send_json(400, {\"ok\": False, \"detail\": str(exc)})\n            except BusyError as exc:\n                self._send_json(409, {\"ok\": False, \"detail\": str(exc)})\n            return\n        if self.path == \"/api/update-plan\":\n            try:\n                from update_engine import Engine\n                def prepare():\n                    reset_check_progress_for_run()\n                    set_check_progress(operation_name=\"update_plan\", phase=\"preflight\", eta_seconds=None)\n                    state = compute_snapshot(False, enrich_details=False)\n                    items = Engine(sys.modules[__name__]).plan([p for p in state[\"packages\"] if p.get(\"outdated\") and not p.get(\"excluded\")])\n                    raise_if_cancel_requested()\n                    set_check_progress(running=False, phase=\"completed\", message=\"Update routes checked\", current_package=None, completed_at=now_iso(), eta_seconds=0)\n                    return {\"ok\": True, \"items\": items}\n                self._send_json(200, run_exclusive(\"update_plan\", prepare))\n            except BusyError as exc:\n                self._send_json(409, {\"ok\": False, \"detail\": str(exc)})\n            except Exception as exc:\n                canceled = isinstance(exc, OperationCancelledError)\n                set_check_progress(running=False, phase='canceled' if canceled else 'error',\n                                   canceled=canceled, error=None if canceled else str(exc),\n                                   current_package=None, eta_seconds=None, completed_at=now_iso())\n                self._send_json(500, {\"ok\": False, \"detail\": str(exc)})\n            return\n        if self.path == \"/api/cancel-operation\":\n            payload = request_cancel_current_operation()\n            if payload.get(\"ok\"):\n                self._send_json(200, payload)\n            else:\n                self._send_json(409, payload)\n            return\n\n        if self.path == \"/api/settings/scheduler\":\n            body = self._read_json_body()\n            incoming = body.get(\"scheduler\") if isinstance(body, dict) else None\n\n            try:\n                scheduler = normalize_scheduler_payload(incoming if incoming is not None else body)\n                settings = load_settings()\n                settings[\"scheduler\"] = scheduler\n                save_settings(settings)\n                applied = apply_scheduler_settings(scheduler)\n\n                self._send_json(\n                    200,\n                    {\n                        \"ok\": True,\n                        \"scheduler\": applied,\n                    },\n                )\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"scheduler_update_failed\", \"detail\": str(exc)})\n            return\n\n        if self.path == \"/api/settings/brew-path\":\n            body = self._read_json_body()\n            raw_path = body.get(\"brew_path\") if isinstance(body, dict) else \"\"\n\n            try:\n                normalized = normalize_brew_path(raw_path)\n                settings = load_settings()\n                settings[\"brew_path\"] = normalized\n                save_settings(settings)\n\n                resolved = brew_bin()\n                self._send_json(\n                    200,\n                    {\n                        \"ok\": True,\n                        \"brew_path\": normalized,\n                        \"resolved_brew_path\": resolved,\n                    },\n                )\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"brew_path_update_failed\", \"detail\": str(exc)})\n            return\n\n        if self.path == \"/api/check\":\n            operation = read_current_operation()\n            operation_name = str(operation.get(\"name\") or \"\")\n\n            if operation.get(\"running\") and operation_name.startswith(\"update\"):\n                started, parallel_state = start_parallel_check(\n                    reason=\"manual_check_during_update\",\n                    run_brew_update=False,\n                )\n                detail = \"Parallel check started while update is running\"\n                if not started:\n                    detail = \"Parallel check is already running\"\n                self._send_json(\n                    202,\n                    {\n                        \"ok\": True,\n                        \"parallel\": True,\n                        \"started\": started,\n                        \"detail\": detail,\n                        \"operation\": operation,\n                        \"parallel_check\": parallel_state,\n                    },\n                )\n                return\n\n            active_parallel = read_parallel_check_state()\n            if active_parallel.get(\"running\"):\n                self._send_json(\n                    202,\n                    {\n                        \"ok\": True,\n                        \"parallel\": True,\n                        \"started\": False,\n                        \"detail\": \"Parallel check is already running\",\n                        \"operation\": operation,\n                        \"parallel_check\": active_parallel,\n                    },\n                )\n                return\n\n            try:\n                payload = run_with_progress(\n                    \"check_now\",\n                    lambda: compute_snapshot(run_brew_update=True, track_progress=True), snapshot=True,\n                )\n                self._send_json(200, payload)\n            except BusyError as exc:\n                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n            except OperationCancelledError:\n                self._send_json(200, {\"ok\": True, \"canceled\": True})\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"check_failed\", \"detail\": str(exc), \"progress\": getattr(exc, \"operation_progress\", None)})\n            return\n\n        if self.path == \"/api/update-one\":\n            body = self._read_json_body()\n            if body.get(\"plan_id\"):\n                return self._apply_unified_request(body)\n            name = str(body.get(\"name\") or \"\").strip()\n            kind = str(body.get(\"kind\") or \"\").strip()\n\n            try:\n                previous_snapshot = read_json(STATE_FILE) or {}\n\n                def _operation():\n                    planned = [progress_package_item(name, kind)]\n                    completed: list[dict[str, Any]] = []\n                    failed: list[dict[str, Any]] = []\n                    reset_check_progress_for_run()\n                    set_check_progress(\n                        operation_name=\"update_one\",\n                        phase=\"updating_packages\",\n                        message=f\"Updating {name}\",\n                        done=0,\n                        total=1,\n                        eta_seconds=None,\n                        current_package=f\"{kind}:{name}\",\n                        planned_packages=planned,\n                        completed_packages=completed,\n                        failed_packages=failed,\n                        remaining_packages=planned,\n                        cancel_requested=False,\n                        canceled=False,\n                        error=None,\n                    )\n\n                    package_started_at = now_iso()\n                    result = run_upgrade(name, kind)\n                    result_item = progress_result_item(result, started_at=package_started_at)\n                    completed.append(result_item)\n                    if not result.get(\"ok\"):\n                        failed.append(result_item)\n\n                    cancelled = bool(result.get(\"canceled\"))\n\n                    set_check_progress(\n                        done=1,\n                        total=1,\n                        eta_seconds=0,\n                        current_package=f\"{kind}:{name}\",\n                        completed_packages=completed,\n                        failed_packages=failed,\n                        remaining_packages=[],\n                        cancel_requested=is_cancel_requested(),\n                        canceled=cancelled,\n                    )\n\n                    set_check_progress(\n                        phase=\"preparing\",\n                        message=\"Refreshing package inventory\" if not cancelled else \"Refreshing package inventory after cancellation\",\n                        current_package=\"\",\n                        planned_packages=planned,\n                        completed_packages=completed,\n                        failed_packages=failed,\n                        remaining_packages=[],\n                        cancel_requested=is_cancel_requested(),\n                        canceled=cancelled,\n                    )\n\n                    snapshot, refresh_error = safe_refresh_snapshot(previous_snapshot=previous_snapshot)\n\n                    entries = history_entries_from_results(\n                        [result],\n                        snapshot,\n                        snapshot_verified=refresh_error is None,\n                        snapshot_error=refresh_error,\n                    )\n                    append_update_history_entries(entries)\n\n                    if result.get(\"ok\"):\n                        completed_message = f\"Update completed for {name}\"\n                        if refresh_error:\n                            completed_message = f\"Update completed for {name} (inventory refresh warning)\"\n                        set_check_progress(\n                            running=False,\n                            phase=\"completed\",\n                            message=completed_message,\n                            done=1,\n                            total=1,\n                            eta_seconds=0,\n                            current_package=\"\",\n                            planned_packages=planned,\n                            completed_packages=completed,\n                            failed_packages=failed,\n                            remaining_packages=[],\n                            cancel_requested=is_cancel_requested(),\n                            canceled=False,\n                            error=None,\n                            completed_at=now_iso(),\n                        )\n                    elif cancelled:\n                        set_check_progress(\n                            running=False,\n                            phase=\"canceled\",\n                            message=f\"Update canceled for {name}\",\n                            done=1,\n                            total=1,\n                            eta_seconds=0,\n                            current_package=\"\",\n                            planned_packages=planned,\n                            completed_packages=completed,\n                            failed_packages=failed,\n                            remaining_packages=[],\n                            cancel_requested=is_cancel_requested(),\n                            canceled=True,\n                            error=None,\n                            completed_at=now_iso(),\n                        )\n                    else:\n                        set_check_progress(\n                            running=False,\n                            phase=\"error\",\n                            message=f\"Update failed for {name}\",\n                            done=1,\n                            total=1,\n                            eta_seconds=0,\n                            current_package=\"\",\n                            planned_packages=planned,\n                            completed_packages=completed,\n                            failed_packages=failed,\n                            remaining_packages=[],\n                            cancel_requested=is_cancel_requested(),\n                            canceled=False,\n                            error=str(result.get(\"stderr\") or result.get(\"stdout\") or \"update failed\"),\n                            completed_at=now_iso(),\n                        )\n\n                    return {\n                        \"result\": result,\n                        \"snapshot\": snapshot,\n                        \"inventory_refresh_error\": refresh_error,\n                    }\n\n                payload = run_with_progress(f\"update_one:{kind}:{name}\", _operation)\n                self._send_json(200, {\"ok\": True, **payload})\n            except BusyError as exc:\n                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"update_failed\", \"detail\": str(exc), \"progress\": getattr(exc, \"operation_progress\", None)})\n            return\n\n        if self.path == \"/api/update-all\":\n            body = self._read_json_body()\n            if body.get(\"plan_id\"):\n                return self._apply_unified_request(body, all_ready=True)\n            try:\n                payload = run_with_progress(\"update_all\", lambda: run_update_all(track_progress=True))\n                self._send_json(200, payload)\n            except BusyError as exc:\n                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"update_all_failed\", \"detail\": str(exc), \"progress\": getattr(exc, \"operation_progress\", None)})\n            return\n\n        if self.path == \"/api/update-selected\":\n            body = self._read_json_body()\n            if body.get(\"plan_id\"):\n                return self._apply_unified_request(body)\n            raw_packages = body.get(\"packages\") if isinstance(body, dict) else None\n\n            try:\n                selected_packages = normalize_selected_packages(raw_packages)\n                payload = run_with_progress(\n                    f\"update_selected:{len(selected_packages)}\",\n                    lambda: run_update_selected(selected_packages, track_progress=True),\n                )\n                self._send_json(200, payload)\n            except ValueError as exc:\n                self._send_json(400, {\"ok\": False, \"error\": \"invalid_packages\", \"detail\": str(exc)})\n            except BusyError as exc:\n                self._send_json(409, {\"ok\": False, \"error\": \"busy\", \"detail\": str(exc), \"operation\": read_current_operation()})\n            except Exception as exc:\n                self._send_json(500, {\"ok\": False, \"error\": \"update_selected_failed\", \"detail\": str(exc), \"progress\": getattr(exc, \"operation_progress\", None)})\n            return\n\n        self._send_json(404, {\"ok\": False, \"error\": \"not_found\"})\n\n\ndef run_server(host: str, port: int) -> None:\n    ensure_dirs()\n    journal = read_json(RUN_JOURNAL_FILE)\n    if journal:\n        plan = journal.get(\"plan\", [])\n        results = journal.get(\"results\", [])\n        ready = [progress_package_item(p[\"name\"], p[\"kind\"]) for p in plan if p.get(\"ready\")]\n        completed = [{**progress_result_item(r, started_at=r.get(\"started_at\")), \"completed_at\": r.get(\"completed_at\")}\n                     for r in results]\n        phase = journal.get(\"status\", \"error\")\n        interrupted = phase == \"running\"\n        set_check_progress(running=False, operation_name=\"update_all\", phase=\"error\" if interrupted else phase,\n                           message=\"Previous run was interrupted; refresh installed versions before retrying\" if interrupted else \"Previous update results restored\",\n                           started_at=journal.get(\"started_at\"), completed_at=journal.get(\"completed_at\"),\n                           done=len(results), total=len(ready), planned_packages=ready, completed_packages=completed,\n                           failed_packages=[r for r in completed if not r[\"ok\"]], remaining_packages=ready[len(results):],\n                           eta_seconds=None if interrupted else 0, error=\"interrupted_run\" if interrupted else None)\n        with CHECK_PROGRESS_LOCK:\n            CHECK_PROGRESS[\"deferred_packages\"] = [p for p in plan if not p.get(\"ready\")]\n    append_service_log(f\"Starting HTTP server on http://{host}:{port}\")\n    server = ThreadingHTTPServer((host, port), BrewUpdatesHandler)\n    shutdown_requested = threading.Event()\n\n    def request_shutdown(_signum, _frame) -> None:\n        if shutdown_requested.is_set():\n            return\n        shutdown_requested.set()\n        append_service_log(\"App closed; finishing active work before backend shutdown\")\n\n        def finish_then_stop() -> None:\n            while read_current_operation().get(\"running\") or read_parallel_check_state().get(\"running\"):\n                time.sleep(0.25)\n            # shutdown() must run outside serve_forever()'s thread.\n            server.shutdown()\n\n        threading.Thread(target=finish_then_stop, daemon=True).start()\n\n    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)\n    try:\n        server.serve_forever()\n    finally:\n        signal.signal(signal.SIGTERM, previous_sigterm)\n        server.server_close()\n        append_service_log(\"HTTP server stopped\")\n\n\ndef run_check_only(notify: bool) -> int:\n    ensure_dirs()\n    snapshot = compute_snapshot(run_brew_update=True, enrich_details=False)\n    if notify:\n        send_notification(snapshot)\n\n    counts = snapshot.get(\"counts\", {})\n    print(\n        json.dumps(\n            {\n                \"ok\": True,\n                \"updated_at\": snapshot.get(\"updated_at\"),\n                \"outdated_total\": counts.get(\"outdated_total\", 0),\n                \"total\": counts.get(\"total\", 0),\n            },\n            ensure_ascii=False,\n        )\n    )\n    return 0\n\n\ndef parse_args() -> argparse.Namespace:\n    parser = argparse.ArgumentParser(description=\"Homebrew Update Manager backend\")\n    parser.add_argument(\"--serve\", action=\"store_true\", help=\"Run HTTP service\")\n    parser.add_argument(\"--check-only\", action=\"store_true\", help=\"Run check once and exit\")\n    parser.add_argument(\"--notify\", action=\"store_true\", help=\"Send macOS notification after check\")\n    parser.add_argument(\"--host\", default=DEFAULT_HOST, help=f\"HTTP host (default: {DEFAULT_HOST})\")\n    parser.add_argument(\"--port\", type=int, default=DEFAULT_PORT, help=f\"HTTP port (default: {DEFAULT_PORT})\")\n    return parser.parse_args()\n\n\ndef main() -> int:\n    ensure_python_314()\n    ensure_dirs()\n\n    args = parse_args()\n\n    if not args.serve and not args.check_only:\n        args.serve = True\n\n    if args.check_only:\n        return run_check_only(notify=args.notify)\n\n    run_server(args.host, args.port)\n    return 0\n\n\nif __name__ == \"__main__\":\n    raise SystemExit(main())\n",
    "backend/mac_manager.py": "\"\"\"Combined inventory and explicit one-time migration transactions.\"\"\"\nfrom __future__ import annotations\nimport hashlib\nimport json\nimport re\nfrom pathlib import Path\nimport shutil\nimport tempfile\nimport time\nimport threading\n\nSCAN_CONTEXT = threading.local()\nimport macports_provider as mp\nimport update_routes as routes\nimport catalog_refresh\nimport automatic_alternatives\nimport installation_evidence\nimport unified_updates\n\ndef mapping():\n    return json.loads((Path(__file__).with_name('macports-map.json')).read_text())\n\ndef combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):\n    if track_progress:\n        s.reset_check_progress_for_run()\n        s.set_check_progress(operation_name='check', phase='collecting_installed',\n                             message='Checking Homebrew inventory', current_package='Homebrew',\n                             done=0, total=3, eta_seconds=None)\n    prior = getattr(SCAN_CONTEXT, 'active', False)\n    SCAN_CONTEXT.active = prior or track_progress\n    try:\n        return _combined(s, brew_scan, refresh, track_progress, enrich_details)\n    except Exception as error:\n        if track_progress:\n            canceled = isinstance(error, s.OperationCancelledError)\n            s.set_check_progress(running=False, phase='canceled' if canceled else 'error',\n                message='Check canceled' if canceled else 'Check failed: ' + str(error),\n                canceled=canceled, error=None if canceled else str(error),\n                current_package=None, eta_seconds=None, completed_at=s.now_iso())\n        raise\n    finally:\n        SCAN_CONTEXT.active = prior\n\n\ndef _combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):\n    installation_evidence.reset()\n    previous = s.read_json(s.STATE_FILE) or {}\n    status, errors = {}, []\n    try:\n        state = brew_scan(refresh, track_progress=track_progress, enrich_details=enrich_details, complete_progress=False)\n        packages = state['packages']\n        errors.extend(state.get('errors', []))\n        status['homebrew'] = {'ok': not bool(state.get('errors')), 'checked_at': s.now_iso(),\n                              'error': '; '.join(state.get('errors', []))}\n    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('Homebrew: ' + str(error))\n        packages = [dict(p, inventory_stale=True, update_block_reason='inventory_unavailable')\n                    for p in previous.get('packages', []) if p.get('provider') != 'macports']\n        status['homebrew'] = {'ok': False, 'error': str(error),\n            'checked_at': previous.get('providers', {}).get('homebrew', {}).get('checked_at')}\n    # Existing independently managed tools and their Brew copies are separate\n    # installations. Keep legacy formula requests tied to the Brew copy.\n    extras=[]\n    for pkg in packages:\n        if pkg.get('provider','homebrew') not in {'homebrew','macports'}:\n            if pkg['kind'] != 'tool' and pkg.get('brew_installation_present') is not False and pkg.get('brew_installed_version'):\n                copy=dict(pkg,provider='homebrew',installed_version=pkg['brew_installed_version'],\n                          outdated=routes.version_key(pkg['brew_installed_version']) < routes.version_key(pkg['latest_version']))\n                extras.append(copy)\n            pkg['kind']='tool'\n    packages.extend(extras)\n    # Even without Brew, retain registered tools, but block updates until their\n    # target metadata can be verified. Never call an unknown target current.\n    present={p['name'] for p in packages if p.get('kind')=='tool'}\n    for name,record in routes.read_records(s.TOOLS_ROOT).items():\n        if name not in present:\n            packages.append(dict(name=name,kind='tool',provider=record['provider'],\n                installed_version=record['version'],latest_version='unknown',outdated=False,\n                inventory_stale=True,update_block_reason='inventory_unavailable',description='Managed tool'))\n    if track_progress:\n        s.raise_if_cancel_requested()\n        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',\n                             current_package='MacPorts', done=1, total=3, eta_seconds=None)\n    interactive = track_progress and s.read_current_operation().get('name') == 'check_now'\n    catalog_state = catalog_refresh.ensure(s, refresh=refresh, interactive=interactive)\n    if track_progress:\n        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',\n                             current_package='MacPorts', eta_seconds=None)\n    try:\n        ports = mp.inventory(s)\n        if not catalog_state['fresh']:\n            ports = [dict(p, inventory_stale=True, update_block_reason='inventory_unavailable') for p in ports]\n        packages.extend(ports)\n        status['macports'] = {'ok': True, 'checked_at': s.now_iso(),\n                             'catalog': catalog_state, 'catalog_note': catalog_state['status']}\n    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('MacPorts: ' + str(error))\n        packages.extend(dict(p, inventory_stale=True, update_block_reason='inventory_unavailable')\n                        for p in previous.get('packages', []) if p.get('provider') == 'macports')\n        status['macports'] = {'ok': False, 'error': str(error), 'catalog': catalog_state,\n            'checked_at': previous.get('providers', {}).get('macports', {}).get('checked_at')}\n    for pkg in packages:\n        provider = pkg.get('provider', 'homebrew')\n        pkg.update(provider=provider, provider_name=pkg['name'], installation_id=(\n            f\"homebrew:{pkg['kind']}:{pkg['name']}\" if provider=='homebrew' else\n            f\"macports:{pkg['name']}\" if provider=='macports' else\n            f\"pixi:{pkg['name']}\" if provider=='pixi' else\n            f\"{provider}:{pkg['kind']}:{pkg['name']}\"))\n        if provider == 'macports':\n            pkg['excluded'] = pkg['name'] == 'ngrok' or s.is_excluded(pkg['name'], 'port')\n            if pkg['excluded']: pkg['outdated'] = False\n    links = mapping()\n    for pkg in packages:\n        if pkg['provider'] != 'macports':\n            pkg['parallel_installations'] = [p['installation_id'] for p in packages\n                if p['provider'] == 'macports' and p['name'] in links.get(pkg['name'], [])]\n    if track_progress:\n        s.raise_if_cancel_requested()\n        s.set_check_progress(phase='preflight', message='Checking binary routes and migration proposals',\n                             current_package=None, done=2, total=3, eta_seconds=None)\n    offers = []\n    try:\n        from update_engine import Engine\n        engine = Engine(s)\n        catalog, installed = engine.catalog(), engine.installed()\n        for p in packages:\n            if p['provider'] == 'homebrew' and p['kind'] == 'formula' and p['name'] in links and not p.get('excluded'):\n                blockers = routes.bottle_blockers(p['name'], catalog, installed, s.load_settings()['excluded_packages'])\n                if blockers:\n                    if s.load_settings()['binary_only']:\n                        p['update_block_reason'] = 'source_required'\n                    offers.append(dict(name=p['name'], ports=links.get(p['name'], []),\n                        status='requires_live_check' if links.get(p['name']) else 'mapping_unverified',\n                        executable=p['name'] in {'terminal-notifier','include-what-you-use'}, source_blockers=blockers))\n    except s.OperationCancelledError:\n        raise\n    except Exception as error:\n        errors.append('Migration recommendations: ' + str(error))\n    if refresh:\n        try:\n            offers = automatic_alternatives.check(s, offers, packages, catalog_state['fresh'], report=track_progress)\n        except s.OperationCancelledError:\n            raise\n        except Exception as error:\n            errors.append('Alternative checks: ' + str(error))\n            for offer in offers:\n                offer.update(status='check_failed', detail=str(error), checked_at=s.now_iso())\n    else:\n        previous_offers = {o['name']: o for o in previous.get('migrations', [])}\n        for offer in offers:\n            pkg = next(p for p in packages if p['name']==offer['name'] and p['kind']=='formula')\n            prior = previous_offers.get(offer['name'], {})\n            offer['homebrew_target'] = pkg.get('latest_version')\n            if (catalog_state['fresh'] and prior.get('homebrew_target') == pkg.get('latest_version')\n                    and prior.get('version_policy') == automatic_alternatives.VERSION_POLICY\n                    and prior.get('installed_version') == pkg.get('installed_version')\n                    and prior.get('status') not in {'requires_live_check','checking_pending'}\n                    and catalog_refresh.recent(prior.get('checked_at'), 6*3600)):\n                offer.update(prior)\n            else:\n                offer.update(status='checking_pending' if offer['ports'] else 'mapping_unverified')\n            pkg.update(alternative_status=offer['status'], alternative_detail=offer.get('detail',''),\n                       alternative_target=offer.get('target'), alternative_below_homebrew=offer.get('below_homebrew_target', False))\n    offers = installation_evidence.annotate(s, offers, packages, refresh, report=track_progress)\n    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')\n    state = dict(app=s.APP_NAME, updated_at=s.now_iso(), packages=packages,\n                 counts=s.summarize_counts(packages), errors=errors, providers=status,\n                 partial=bool(errors) or not all(p['ok'] for p in status.values()), migrations=offers,\n                 migration_journal=journal)\n    if track_progress:\n        s.raise_if_cancel_requested()\n    if refresh:\n        try:\n            unified_updates.generate_plan(s, state)\n        except s.OperationCancelledError:\n            raise\n        except Exception as error:\n            errors.append('Unified plan: ' + str(error))\n            state['partial'] = True\n    else:\n        unified_updates.attach_cached(s, state)\n    s.atomic_write_json(s.STATE_FILE, state)\n    if track_progress:\n        failed = not any(provider['ok'] for provider in status.values())\n        s.set_check_progress(running=False, phase='error' if failed else 'completed',\n            message='All inventory checks failed' if failed else\n                    ('Inventory checked with warnings' if state['partial'] else 'Package inventories checked'),\n            done=3, total=3, current_package=None, error='; '.join(errors) if errors else None,\n            completed_at=s.now_iso(), eta_seconds=None if failed else 0)\n    return state\n\ndef migration_plan(s, name):\n    if name == 'ngrok' or s.is_excluded(name, 'formula'):\n        raise ValueError('Package is excluded')\n    roots = mapping().get(name, [])\n    if not roots:\n        raise ValueError('No verified package mapping')\n    current = s.fetch_installed_inventory(s.fetch_outdated_index())\n    pkg = next((p for p in current if p['name'] == name and p['kind'] == 'formula'), None)\n    if pkg and (pkg.get('inventory_stale') or pkg.get('inventory_verification', {}).get('status', 'verified') != 'verified'):\n        raise ValueError('Installation evidence is unverified; scan and resolve the reported inventory gap')\n    if not pkg or pkg.get('brew_installation_present') is False:\n        raise ValueError('The Homebrew installation is not present')\n    from update_engine import Engine\n    engine = Engine(s)\n    blockers = routes.bottle_blockers(name, engine.catalog(), engine.installed(), s.load_settings()['excluded_packages'])\n    if not blockers:\n        raise ValueError('Homebrew has a binary route; migration is unnecessary')\n    graph = mp.preflight(s, roots)\n    target = next(n['version'] for n in graph['nodes'] if n['name'] == roots[0])\n    probe = mp.load_probe()\n    # Cross-manager comparison is strictly upstream only, never revisions.\n    candidates = {root: next(n['version'] for n in graph['nodes'] if n['name'] == root) for root in roots}\n    comparison = automatic_alternatives.compare_versions(probe, pkg['installed_version'], pkg['latest_version'], candidates)\n    if comparison['status'] != 'version_eligible':\n        raise ValueError(comparison['detail'])\n    consumers = mp.checked(s, [s.brew_bin(), 'uses', '--installed', name], timeout=90).splitlines()\n    plan = dict(name=name, roots=roots, installed_version=pkg['installed_version'], target=target,\n        version_policy=automatic_alternatives.VERSION_POLICY,\n        homebrew_target=pkg['latest_version'], below_homebrew_target=comparison['below_homebrew_target'],\n        graph=graph, consumers=consumers, executable=name=='terminal-notifier',\n        paths=['/opt/local', '/usr/local'], remove_brew=not consumers,\n        requires='Notification integration test' if name=='terminal-notifier' else 'Consumer/project/data migration plan',\n        created_at=time.time())\n    plan['id'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()\n    s.atomic_write_json(s.DATA_DIR / 'migration-plan.json', plan)\n    return plan\n\ndef approve(s, plan_id):\n    old = s.read_json(s.DATA_DIR / 'migration-plan.json') or {}\n    if old.get('id') != plan_id or time.time()-old.get('created_at', 0)>900:\n        raise ValueError('Migration plan expired; request a fresh plan')\n    if not old.get('executable') or old.get('name') != 'terminal-notifier':\n        raise ValueError('This package requires its own consumer migration plan')\n    s.raise_if_cancel_requested()\n    fresh = migration_plan(s, old['name'])\n    for key in ('installed_version', 'target', 'consumers', 'remove_brew', 'version_policy'):\n        if old.get(key) != fresh.get(key):\n            raise ValueError('Migration changed; review a new plan')\n    if old['graph']['nodes'] != fresh['graph']['nodes']:\n        raise ValueError('Dependency graph changed; review a new plan')\n    journal = dict(plan=fresh, status='running', stage='backup', started_at=s.now_iso())\n    dest = s.DATA_DIR / 'migration-journal.json'\n    def stage(value):\n        journal['stage']=value\n        s.atomic_write_json(dest, journal)\n    stage('backup')\n    try:\n        # Preserve the whole original keg, its symlinks and metadata before uninstall.\n        prefix = Path(mp.checked(s, [s.brew_bin(), '--prefix', 'terminal-notifier'], timeout=30).strip()).resolve()\n        cellar = Path(mp.checked(s, [s.brew_bin(), '--cellar'], timeout=30).strip()).resolve()\n        if not prefix.is_relative_to(cellar / 'terminal-notifier') or not prefix.is_dir():\n            raise RuntimeError('Unexpected terminal-notifier keg path')\n        backup = s.TOOLS_ROOT / 'recovery' / ('notifier-' + str(time.time_ns()))\n        backup.mkdir(parents=True)\n        shutil.copytree(prefix, backup / prefix.name, symlinks=True)\n        for path in prefix.rglob('*'):\n            relative = path.relative_to(prefix)\n            copy = backup / prefix.name / relative\n            if path.is_symlink():\n                if path.readlink() != copy.readlink(): raise RuntimeError('Backup link mismatch')\n            elif path.is_file() and hashlib.sha256(path.read_bytes()).digest()!=hashlib.sha256(copy.read_bytes()).digest():\n                raise RuntimeError('Backup content mismatch')\n        journal['backup']=str(backup)\n        stage('install')\n        activity = s.UpgradeActivity('terminal-notifier')\n        activity.state['provider']='macports'\n        current = next((p for p in mp.inventory(s) if p['name']=='terminal-notifier'), None)\n        root = next(n for n in fresh['graph']['nodes'] if n['name']=='terminal-notifier')\n        expected = root['version'] + '_' + root['revision']\n        if not current or current['installed_version'] != expected or current['outdated']:\n            result = mp.privileged(s, 'install', ['terminal-notifier'], activity)\n            if result.code: raise RuntimeError(result.stderr)\n        else:\n            journal['installation_reused'] = True\n        s.raise_if_cancel_requested()\n        stage('verify')\n        installed = next((p for p in mp.inventory(s) if p['name']=='terminal-notifier'), None)\n        if not installed or installed['outdated']:\n            raise RuntimeError('MacPorts installation verification failed')\n        binary = notifier_path()\n        if not binary: raise RuntimeError('MacPorts notification executable is absent')\n        version = mp.checked(s, [str(binary), '-version'], timeout=30)\n        if not re.search(r'(?<![0-9])'+re.escape(fresh['target'])+r'(?![0-9])',version):\n            raise RuntimeError('Notifier version mismatch')\n        # The CLI package and the application's notification delivery identity\n        # are separate. Preserve the existing app helper and its user permission.\n        # Running MacPorts' default bundle ID would change that identity.\n        helper = app_notification_path()\n        s.send_notification({'packages': [], 'notification_test': True}, notifier_override=str(helper))\n        stage('activate')\n        s.atomic_write_json(s.DATA_DIR / 'notifier-route.json', {'provider':'application_helper',\n            'path':str(helper), 'cli_provider':'macports', 'cli_path':str(binary)})\n        s.raise_if_cancel_requested()\n        stage('cleanup')\n        consumers = mp.checked(s, [s.brew_bin(), 'uses', '--installed', 'terminal-notifier'], timeout=60).splitlines()\n        if not consumers:\n            journal['brew_uninstall_output'] = mp.checked(s, [s.brew_bin(), 'uninstall', '--formula', 'terminal-notifier'],\n                timeout=120, env_override={'HOMEBREW_NO_AUTOREMOVE':'1',\n                    'HOMEBREW_NO_INSTALL_CLEANUP':'1', 'HOMEBREW_NO_AUTO_UPDATE':'1'})\n        journal.update(status='completed', completed_at=s.now_iso(), preserved_for_consumers=consumers)\n        stage('completed')\n        return journal\n    except Exception as error:\n        journal.update(status='needs_attention', error=str(error), completed_at=s.now_iso())\n        s.atomic_write_json(dest, journal)\n        raise\n\ndef app_notification_path():\n    helper = Path.home() / 'Applications/Homebrew Update Notifications.app/Contents/MacOS/terminal-notifier'\n    if not helper.is_file():\n        raise RuntimeError('The application notification helper is absent; configure its notification identity before migrating')\n    return helper\n\ndef notifier_path():\n    candidates = [Path('/opt/local/bin/terminal-notifier'),\n        Path('/opt/local/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier'),\n        Path('/Applications/MacPorts/terminal-notifier.app/Contents/MacOS/terminal-notifier')]\n    return next((p for p in candidates if p.is_file()), None)\n\n# Bundle 003 keeps the verified notifier pilot intact and delegates new profiles.\n_legacy_migration_plan=migration_plan\n_legacy_approve=approve\nimport migration_engine as _migration_engine\ndef migration_plan(s,name): return _legacy_migration_plan(s,name) if name=='terminal-notifier' else _migration_engine.plan(s,name)\ndef approve(s,plan_id):\n old=s.read_json(s.DATA_DIR/'migration-plan.json') or {}\n return _legacy_approve(s,plan_id) if old.get('name')=='terminal-notifier' and not old.get('schema_version') else _migration_engine.approve(s,plan_id)\ndef migration_recovery_plan(s, transaction_id, action): return _migration_engine.recovery_plan(s, transaction_id, action)\n",
    "electron/main.js": "const { app, BrowserWindow, ipcMain, dialog } = require('electron');\nconst fs = require('node:fs');\nconst os = require('node:os');\nconst net = require('node:net');\nconst http = require('node:http');\nconst path = require('node:path');\nconst { spawn, spawnSync } = require('node:child_process');\nconst { createAppUpdater } = require('./app-updater');\n\napp.setPath('userData', path.join(os.homedir(), 'Library', 'Application Support', 'Homebrew Update Manager'));\n\nconst DEV_APP_ROOT = path.join(__dirname, '..');\nconst PACKAGED_ASAR_ROOT = path.join(process.resourcesPath, 'app.asar');\nconst PACKAGED_UNPACKED_ROOT = path.join(process.resourcesPath, 'app.asar.unpacked');\n\nfunction appRoot({ unpacked = false } = {}) {\n  if (!app.isPackaged) {\n    return DEV_APP_ROOT;\n  }\n\n  return unpacked ? PACKAGED_UNPACKED_ROOT : PACKAGED_ASAR_ROOT;\n}\n\nfunction resolveServiceScriptCandidates() {\n  return [\n    path.join(appRoot({ unpacked: true }), 'backend', 'brew-updates-service.py'),\n    path.join(appRoot({ unpacked: false }), 'backend', 'brew-updates-service.py'),\n    path.join(process.resourcesPath, 'backend', 'brew-updates-service.py'),\n  ];\n}\n\nfunction resolveServiceScriptPath() {\n  const candidates = resolveServiceScriptCandidates();\n  const resolved = candidates.find((candidate) => fs.existsSync(candidate)) || candidates[0];\n  return {\n    resolved,\n    candidates,\n  };\n}\n\nconst SERVICE_SCRIPT_INFO = resolveServiceScriptPath();\nconst SERVICE_SCRIPT = SERVICE_SCRIPT_INFO.resolved;\nconst SERVICE_HOST = '127.0.0.1';\nconst SERVICE_PORT_MIN = 8765;\nconst SERVICE_PORT_MAX = 8795;\nlet activeServicePort = SERVICE_PORT_MIN;\nconst REQUIRED_PYTHON = '3.14';\nconst FIXED_UPDATE_REPO = 'tzoharlary/brew-update-manager';\n\nlet resolvedPythonBin = null;\n\nlet mainWindow = null;\nlet appUpdater = null;\nlet ownedServiceChild = null;\n\nfunction broadcastAppUpdateProgress(payload) {\n  for (const win of BrowserWindow.getAllWindows()) {\n    if (!win.isDestroyed()) {\n      win.webContents.send('app-update:progress', payload);\n    }\n  }\n}\n\nfunction executableExists(filePath) {\n  if (!filePath) return false;\n  try {\n    fs.accessSync(filePath, fs.constants.X_OK);\n    return true;\n  } catch {\n    return false;\n  }\n}\n\nfunction isPython314(pythonBin) {\n  const probe = spawnSync(\n    pythonBin,\n    ['-c', 'import sys; raise SystemExit(0 if (sys.version_info.major, sys.version_info.minor)==(3,14) else 1)'],\n    { stdio: 'ignore' },\n  );\n  return probe.status === 0;\n}\n\nfunction resolvePythonBin() {\n  const fromEnv = process.env.PYTHON_BIN ? [process.env.PYTHON_BIN] : [];\n  const candidates = [\n    ...fromEnv,\n    path.join(os.homedir(), 'Library', 'Application Support', 'Homebrew Update Manager',\n      'runtime', 'python', 'current', 'bin', 'python3.14'),\n    '/opt/homebrew/bin/python3.14',\n    '/opt/homebrew/bin/python3',\n    '/usr/local/bin/python3.14',\n    '/usr/local/bin/python3',\n    'python3.14',\n    'python3',\n  ];\n\n  for (const candidate of candidates) {\n    if (candidate.includes('/') && !executableExists(candidate)) {\n      continue;\n    }\n    if (isPython314(candidate)) {\n      return candidate;\n    }\n  }\n\n  throw new Error(\n    `Python ${REQUIRED_PYTHON} not found. Please install Python ${REQUIRED_PYTHON} and make sure it is available in PATH.`,\n  );\n}\n\nfunction getPythonBin() {\n  if (!resolvedPythonBin) {\n    resolvedPythonBin = resolvePythonBin();\n  }\n  return resolvedPythonBin;\n}\n\nfunction delay(ms) {\n  return new Promise((resolve) => setTimeout(resolve, ms));\n}\n\nfunction serviceBase() {\n  return `http://${SERVICE_HOST}:${activeServicePort}`;\n}\n\nfunction serviceUrl(pathname) {\n  return `${serviceBase()}${pathname}`;\n}\n\nasync function fetchJson(url, options = {}) {\n  // Upgrade responses may legitimately take hours. Keep their connection open\n  // without fetch's response-header deadline; progress uses separate requests.\n  return new Promise((resolve, reject) => {\n    const headers = { ...(options.headers || {}) };\n    if (options.body) headers['Content-Length'] = Buffer.byteLength(options.body);\n    const request = http.request(url, { method: options.method || 'GET', headers, timeout: 0 }, (response) => {\n      let body = '';\n      response.setEncoding('utf8');\n      response.on('data', chunk => { body += chunk; });\n      response.on('error', reject);\n      response.on('end', () => {\n        let payload;\n        try { payload = JSON.parse(body); } catch { reject(new Error('Invalid backend response')); return; }\n        if (response.statusCode < 200 || response.statusCode >= 300) {\n          reject(new Error(payload.detail || payload.error || `HTTP ${response.statusCode}`));\n        } else resolve(payload);\n      });\n    });\n    request.on('error', reject);\n    if (options.body) request.write(options.body);\n    request.end();\n  });\n}\n\nasync function serviceHealthyOnPort(port) {\n  try {\n    const res = await fetch(`http://${SERVICE_HOST}:${port}/health`, { method: 'GET' });\n    if (!res.ok) return false;\n\n    const payload = await res.json().catch(() => null);\n    if (!payload || typeof payload !== 'object') return false;\n    return payload.ok === true && payload.app === 'Mac Update Manager' && payload.protocol === 6;\n  } catch {\n    return false;\n  }\n}\n\nfunction isPortAvailable(port) {\n  return new Promise((resolve) => {\n    const server = net.createServer();\n\n    server.once('error', () => {\n      resolve(false);\n    });\n\n    server.once('listening', () => {\n      server.close(() => resolve(true));\n    });\n\n    server.listen(port, SERVICE_HOST);\n  });\n}\n\nasync function selectServicePort() {\n  for (let port = SERVICE_PORT_MIN; port <= SERVICE_PORT_MAX; port += 1) {\n    // eslint-disable-next-line no-await-in-loop\n    if (await serviceHealthyOnPort(port)) {\n      return { port, alreadyRunning: true };\n    }\n  }\n\n  for (let port = SERVICE_PORT_MIN; port <= SERVICE_PORT_MAX; port += 1) {\n    // eslint-disable-next-line no-await-in-loop\n    if (await isPortAvailable(port)) {\n      return { port, alreadyRunning: false };\n    }\n  }\n\n  throw new Error(`No available backend port found in range ${SERVICE_PORT_MIN}-${SERVICE_PORT_MAX}`);\n}\n\nasync function ensureServiceRunning() {\n  const selection = await selectServicePort();\n  activeServicePort = selection.port;\n\n  if (selection.alreadyRunning) return;\n\n  if (!fs.existsSync(SERVICE_SCRIPT)) {\n    const tried = SERVICE_SCRIPT_INFO.candidates\n      .map((candidate) => `- ${candidate}`)\n      .join('\\n');\n    throw new Error(`Backend service script missing: ${SERVICE_SCRIPT}\\nTried:\\n${tried}`);\n  }\n\n  const pythonBin = getPythonBin();\n\n  const child = spawn(pythonBin, [\n    SERVICE_SCRIPT,\n    '--serve',\n    '--host',\n    SERVICE_HOST,\n    '--port',\n    String(activeServicePort),\n  ], {\n    detached: false,\n    stdio: 'ignore',\n  });\n  ownedServiceChild = child;\n  child.once('exit', () => {\n    if (ownedServiceChild === child) ownedServiceChild = null;\n  });\n\n  for (let i = 0; i < 60; i += 1) {\n    // eslint-disable-next-line no-await-in-loop\n    if (await serviceHealthyOnPort(activeServicePort)) return;\n    // eslint-disable-next-line no-await-in-loop\n    await delay(200);\n  }\n\n  throw new Error(`Service did not become healthy in time on port ${activeServicePort}`);\n}\n\nfunction createWindow() {\n  mainWindow = new BrowserWindow({\n    width: 1340,\n    height: 900,\n    minWidth: 1100,\n    minHeight: 700,\n    title: 'Mac Update Manager',\n    webPreferences: {\n      preload: path.join(__dirname, 'preload.js'),\n      contextIsolation: true,\n      nodeIntegration: false,\n    },\n  });\n\n  mainWindow.loadFile(path.join(appRoot(), 'renderer', 'index.html'));\n\n  mainWindow.on('closed', () => {\n    mainWindow = null;\n  });\n}\n\nfunction focusMainWindow() {\n  if (!mainWindow) {\n    createWindow();\n    return;\n  }\n\n  if (mainWindow.isMinimized()) {\n    mainWindow.restore();\n  }\n  mainWindow.focus();\n}\n\nasync function bootstrap() {\n  await ensureServiceRunning();\n  createWindow();\n}\n\n// Single-instance behavior: additional launches focus existing window.\nconst gotLock = app.requestSingleInstanceLock();\nif (!gotLock) {\n  app.quit();\n} else {\n  app.on('second-instance', () => {\n    focusMainWindow();\n  });\n}\n\napp.whenReady().then(async () => {\n  try {\n    appUpdater = createAppUpdater({\n      app,\n      repo: FIXED_UPDATE_REPO,\n    });\n\n    const marker = appUpdater.readAndConsumeUpdateMarker();\n    if (marker?.status === 'rolled_back') {\n      dialog.showErrorBox(\n        'Mac Update Manager',\n        'The previous app update failed to start correctly and was rolled back to the last working version.',\n      );\n    }\n\n    await bootstrap();\n  } catch (error) {\n    dialog.showErrorBox('Mac Update Manager', `Failed to start backend service:\\n${error.message}`);\n    app.quit();\n    return;\n  }\n\n  app.on('activate', () => {\n    if (BrowserWindow.getAllWindows().length === 0) {\n      createWindow();\n    } else {\n      focusMainWindow();\n    }\n  });\n});\n\napp.on('window-all-closed', () => {\n  app.quit();\n});\n\napp.on('before-quit', () => {\n  if (ownedServiceChild && ownedServiceChild.exitCode === null) {\n    // The backend drains any active check/update before stopping its server.\n    ownedServiceChild.kill('SIGTERM');\n    ownedServiceChild = null;\n  }\n});\n\nipcMain.handle('state:get', async () => fetchJson(serviceUrl('/api/state')));\nipcMain.handle('update:plan:get', async () => fetchJson(serviceUrl('/api/update-plan')));\nipcMain.handle('progress:get', async () => fetchJson(serviceUrl('/api/progress')));\nipcMain.handle('updates:history:get', async () => fetchJson(serviceUrl('/api/update-history')));\nipcMain.handle('settings:get', async () => fetchJson(serviceUrl('/api/settings')));\nipcMain.handle('settings:update-policy', async (_event, payload) => fetchJson(serviceUrl('/api/settings/update-policy'), {\n  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('settings:scheduler:update', async (_event, payload) => fetchJson(serviceUrl('/api/settings/scheduler'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('settings:brew-path:update', async (_event, payload) => fetchJson(serviceUrl('/api/settings/brew-path'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('settings:brew-path:auto-detect', async () => fetchJson(serviceUrl('/api/brew/auto-detect')));\nipcMain.handle('app-update:check', async () => {\n  if (!appUpdater) {\n    throw new Error('App updater is not initialized');\n  }\n\n  return appUpdater.checkForUpdate({\n    onProgress: broadcastAppUpdateProgress,\n  });\n});\nipcMain.handle('app-update:download-install', async () => {\n  if (!appUpdater) {\n    throw new Error('App updater is not initialized');\n  }\n\n  return appUpdater.downloadAndInstallUpdate({\n    onProgress: broadcastAppUpdateProgress,\n  });\n});\nipcMain.handle('check:run', async () => fetchJson(serviceUrl('/api/check'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify({}),\n}));\nipcMain.handle('update:one', async (_event, payload) => fetchJson(serviceUrl('/api/update-one'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('update:all', async (_event, payload) => fetchJson(serviceUrl('/api/update-all'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('update:selected', async (_event, payload) => fetchJson(serviceUrl('/api/update-selected'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify(payload || {}),\n}));\nipcMain.handle('operation:cancel', async () => fetchJson(serviceUrl('/api/cancel-operation'), {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify({}),\n}));\n\nfor (const [channel, endpoint] of [['migration:plan','/api/migrations/plan'],\n  ['migration:approve','/api/migrations/approve'], ['macports:sync','/api/macports/sync']]) {\n  ipcMain.handle(channel, async (_event, payload) => fetchJson(serviceUrl(endpoint), {\n    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload || {}),\n  }));\n}\n",
    "electron/preload.js": "const { contextBridge, ipcRenderer } = require('electron');\n\ncontextBridge.exposeInMainWorld('brewApp', {\n  migrationPlan: (name) => ipcRenderer.invoke('migration:plan', {name}),\n  migrationApprove: (id) => ipcRenderer.invoke('migration:approve', {id}),\n  macportsSync: () => ipcRenderer.invoke('macports:sync'),\n  getState: () => ipcRenderer.invoke('state:get'),\n  getUpdatePlan: () => ipcRenderer.invoke('update:plan:get'),\n  getProgress: () => ipcRenderer.invoke('progress:get'),\n  getUpdateHistory: () => ipcRenderer.invoke('updates:history:get'),\n  getSettings: () => ipcRenderer.invoke('settings:get'),\n  updateScheduler: (scheduler) => ipcRenderer.invoke('settings:scheduler:update', { scheduler }),\n  updateBrewPath: (brewPath) => ipcRenderer.invoke('settings:brew-path:update', { brew_path: brewPath }),\n  updatePolicy: (policy) => ipcRenderer.invoke('settings:update-policy', policy),\n  autoDetectBrewPath: () => ipcRenderer.invoke('settings:brew-path:auto-detect'),\n  checkAppUpdate: () => ipcRenderer.invoke('app-update:check'),\n  downloadAndInstallAppUpdate: () => ipcRenderer.invoke('app-update:download-install'),\n  onAppUpdateProgress: (handler) => {\n    if (typeof handler !== 'function') {\n      return () => {};\n    }\n    const listener = (_event, payload) => handler(payload);\n    ipcRenderer.on('app-update:progress', listener);\n    return () => ipcRenderer.removeListener('app-update:progress', listener);\n  },\n  runCheckNow: () => ipcRenderer.invoke('check:run'),\n  updateOne: (name, kind, decision) => ipcRenderer.invoke('update:one', decision || { name, kind }),\n  updateAll: (payload) => ipcRenderer.invoke('update:all', payload || {}),\n  updateSelected: (packages) => ipcRenderer.invoke('update:selected', Array.isArray(packages) ? { packages } : packages),\n  cancelOperation: () => ipcRenderer.invoke('operation:cancel'),\n});\n",
    "renderer/mac-manager.js": "// Automatic availability results; review and approval remain separate actions.\nfunction macAlternativeLabel(status) {\n  status=status||'checking_pending';\n  const he=i18n.lang==='he';\n  const labels={\n    binary_available:['נמצאה חבילה מוכנה ב־MacPorts','Ready package available in MacPorts'],\n    mapping_unverified:['אין התאמה מאומתת ב־MacPorts','No verified MacPorts mapping'],\n    catalog_unverified:['בדיקת החלופה ממתינה לרענון מידע','Alternative check awaits catalog refresh'],\n    checking_pending:['החלופה תיבדק אוטומטית בבדיקה הבאה','Alternative will be checked automatically on the next scan'],\n    older_than_installed:['החלופה ישנה מהגרסה המותקנת — ירידה בגרסה חסומה','Alternative is older than the installed version; downgrade blocked'],\n    not_newer:['אין גרסה חדשה יותר במסלול זה','No newer upstream version on this route'],\n    version_unverified:['לא ניתן לאמת התאמת גרסאות','Version compatibility could not be verified'],\n    archive_missing:['חבילה מוכנה או תלות חסרה באתרים שנבדקו','Ready package or dependency is missing from checked sites'],\n    archive_unknown:['לא ניתן לאמת זמינות בגלל שגיאת תקשורת','Availability could not be verified due to network errors'],\n    resolution_failed:['לא ניתן להשלים בדיקת תלויות','Dependency resolution could not be completed'],\n    architecture_unverified:['לא אומתה התאמה למחשב','Compatibility with this computer is unverified'],\n    variants_unverified:['נדרשת בדיקת אפשרויות ההתקנה הקיימת','Installed variants need separate review'],\n    excluded:['החבילה או תלות שלה מוחרגת','Package or dependency is excluded'],\n    check_failed:['בדיקת החלופה נכשלה','Alternative check failed'],\n  };\n  return (labels[status]||[status,status])[he?0:1];\n}\nfunction renderMacManager() {\n  let panel=document.getElementById('macManagerPanel');\n  if (!panel) {\n    panel=document.createElement('details');panel.id='macManagerPanel';panel.className='panel';\n    panel.style.padding='16px';panel.style.marginBlock='16px';document.querySelector('.tabs').before(panel);\n  }\n  panel.replaceChildren();const he=i18n.lang==='he';\n  const title=document.createElement('summary');\n  title.textContent=(he?'עדכונים חסומים וחלופות':'Blocked updates and alternatives')+` (${snapshot?.migrations?.length||0})`;\n  panel.appendChild(title);\n  const hint=document.createElement('p');\n  hint.textContent=he?'עדכון חבילה יוצג רק עם נתיב בינארי מאומת. פעולות מעבר שאינן מוכנות חסומות.':'An update is executable only with a verified binary route. Unfinished migration paths remain blocked.';\n  for (const decision of snapshot?.update_decisions||[]) {\n    if (decision.status==='current') continue;\n    const line=document.createElement('p');\n    const route=decision.current_provider===decision.target_provider?decision.current_provider:`${decision.current_provider} → ${decision.target_provider}`;\n    line.textContent=`${decision.logical_id}: ${decision.installed_version||'—'} → ${decision.target_version||'—'} · ${route} · ${decision.status} · ${(decision.reason_codes||[]).join(', ')}`;\n    panel.appendChild(line);\n  }\n  panel.appendChild(hint);\n  const labels={fresh:he?'עדכני':'current',refreshed:he?'רוענן אוטומטית':'refreshed automatically',stale:he?'ירוענן בבדיקה הבאה':'will refresh on next check',awaiting_authorization:he?'הרענון ממתין להזדהות; לחץ התחל בדיקה':'refresh awaits authorization; press Start Check',refresh_failed:he?'הרענון נכשל או לא אושר; התחל בדיקה ינסה שוב':'refresh failed or was declined; Start Check will retry',not_installed:he?'MacPorts אינו מותקן':'MacPorts is not installed'};\n  for(const [name,provider] of Object.entries(snapshot?.providers||{})) {\n    const status=document.createElement('p');\n    status.textContent=`${name}: ${provider.ok?'✓':'⚠'} · ${provider.checked_at||'—'}`;\n    if(provider.catalog)status.textContent+=' · '+(labels[provider.catalog.status]||provider.catalog.status);\n    if(provider.error)status.title=provider.error;\n    panel.appendChild(status);\n  }\n  if(['running','needs_attention'].includes(snapshot?.migration_journal?.status)) {\n    const warning=document.createElement('p');warning.textContent=he?'מעבר קודם דורש בדיקה לפני המשך.':'A previous migration needs review before continuing.';panel.appendChild(warning);\n  }\n  for(const offer of snapshot?.migrations||[]) {\n    const row=document.createElement('details');row.style.marginBlock='10px';\n    const summary=document.createElement('summary');\n    summary.textContent=`${offer.name} — ${macAlternativeLabel(offer.status)}`;\n    if(offer.target&&offer.installed_version)summary.textContent+=` · ${offer.installed_version} → ${offer.target}`;\n    row.appendChild(summary);\n    const detail=document.createElement('p');\n    detail.textContent=(offer.ports?.length?`${offer.ports.join(', ')} · `:'')+(offer.detail||'');\n    if(offer.checked_at)detail.textContent+=' · '+(he?'נבדק: ':'Checked: ')+offer.checked_at;\n    row.appendChild(detail);\n    const classification = offer.classification || {category:'unclassified'};\n    const installation=(snapshot?.packages||[]).find(p=>p.provider==='homebrew'&&p.kind==='formula'&&p.name===offer.name)?.inventory_verification;\n    if(installation) {\n      const evidence=document.createElement('details');const title=document.createElement('summary');\n      title.textContent=he?'ראיות ההתקנה':'Installation evidence';\n      const text=document.createElement('p');text.style.whiteSpace='pre-wrap';\n      text.textContent=(he?'גרסה לפי ההתקנה הפעילה: ':'Active installation version: ')+installation.package_version+'\\n'+\n        (he?'גרסאות שדווחו: ':'Manager versions: ')+(installation.manager_versions||[]).join(', ')+'\\n'+\n        (installation.active_keg||'')+'\\n'+(installation.command_resolution?.resolved_path||'')+'\\n'+\n        [...(installation.issues||[]),...(installation.warnings||[])].join(' · ');\n      if(installation.version_probe?.code)text.textContent+='\\n'+installation.version_probe.output;\n      evidence.append(title,text);row.appendChild(evidence);\n    }\n    const classificationLabels = {\n      service:['שירות עם הגדרות ונתונים','Service with configuration and data'],\n      shared_environment:['ספרייה או סביבת פיתוח משותפת','Shared library or development environment'],\n      standalone_candidate:['כלי עצמאי מועמד לאוטומציה','Standalone automation candidate'],\n      insufficient_information:['חסר מידע לסיווג','Insufficient classification evidence'],\n      unclassified:['טרם סווג','Not yet classified'],\n    };\n    const classificationText=document.createElement('p');\n    classificationText.className='migration-classification';\n    classificationText.textContent=(he?'סיווג: ':'Classification: ')+(classificationLabels[classification.category]||classificationLabels.unclassified)[he?0:1];\n    row.appendChild(classificationText);\n    const reasons=document.createElement('p');\n    reasons.textContent=(classification.reasons||[]).map(migrationReasonLabel).join(' · ');\n    if(classification.checked_at) reasons.textContent+=' · '+classification.checked_at;\n    row.appendChild(reasons);\n    if(classification.consumers?.length) {\n      const consumers=document.createElement('p');consumers.textContent=(he?'תלויים מתועדים: ':'Documented consumers: ')+classification.consumers.map(c=>c.name).join(', ');row.appendChild(consumers);\n    }\n    for(const [label,values] of [[he?'מידע חסר':'Missing evidence',classification.gaps],[he?'קובצי ראיות':'Evidence files',classification.evidence_refs]]) {\n      if(values?.length) {const evidence=document.createElement('details');const title=document.createElement('summary');title.textContent=label;const text=document.createElement('p');text.textContent=values.join(' · ');evidence.append(title,text);row.appendChild(evidence);}\n    }\n    if(offer.status==='binary_available') {\n      if(offer.below_homebrew_target) {\n        const version=document.createElement('p');\n        version.textContent=he?`זו גרסה חדשה מהמותקנת אצלך. יעד Homebrew הוא ${offer.homebrew_target}; אין בכך הוכחה שזו הגרסה האחרונה הנתמכת ב־Intel.`:`This is newer than your installed version. Homebrew targets ${offer.homebrew_target}; this does not prove it is the last Intel-supported release.`;\n        row.appendChild(version);\n      }\n      const availability=document.createElement('p');\n      availability.textContent=he?'נבדקה זמינות קבצים לכל התלויות. חתימות ותפקוד יאומתו רק בהתקנה.':'Archive availability was checked for all dependencies. Signatures and functionality are verified during installation.';row.appendChild(availability);\n      if(!offer.executable) {\n        const blocked=document.createElement('p');blocked.textContent=classification.category==='standalone_candidate'?(he?'מנגנון המעבר לכלי זה טרם מומש.':'The migration mechanism for this tool is not implemented yet.'):(he?'המעבר דורש טיפול בצרכנים, בנתונים או במידע החסר; אינו מבוצע בעדכון רגיל.':'Migration needs consumer, data or missing-evidence review; regular updates do not perform it.');row.appendChild(blocked);\n      } else {\n        const review=document.createElement('button');review.textContent=he?'סקירת תוכנית המעבר':'Review migration plan';\n        review.disabled=!!busyLoading||!!lastKnownOperationRunning;\n        review.onclick=async()=>{\n          review.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();\n          try {\n            const {plan}=await window.brewApp.migrationPlan(offer.name);\n            const contents=document.createElement('p');\n            contents.textContent=`${plan.installed_version} → ${plan.target} · ${plan.graph.nodes.map(n=>n.name).join(', ')} · ${plan.paths.join(', ')} · ${he?'צרכנים':'Consumers'}: ${plan.consumers.join(', ')||'—'}`;row.appendChild(contents);\n            const approve=document.createElement('button');approve.textContent=he?'אישור מעבר חד־פעמי':'Approve this migration once';\n            approve.onclick=async()=>{\n              if(!confirm(he?'לאשר התקנה, בדיקת שימוש והסרת העותק הקודם רק אם אין לו תלויים?':'Approve installation, verification, and removal of the old copy only if unused?'))return;\n              approve.disabled=true;setLoading(true,'message.loadingState');startProgressPolling();\n              try {await window.brewApp.migrationApprove(plan.id);const response=await window.brewApp.getState();snapshot=response.snapshot||response;renderCards(snapshot);renderTable();}\n              catch(error){showMessage(error.message,true);}\n              finally{stopProgressPolling();setLoading(false,'message.loadingState');approve.disabled=false;}\n            };row.appendChild(approve);\n          }catch(error){showMessage(error.message,true);}\n          finally{stopProgressPolling();setLoading(false,'message.loadingState');review.disabled=false;}\n        };row.appendChild(review);\n      }\n    }\n    panel.appendChild(row);\n  }\n}\n",
}

UNIFIED_EXISTING_BLOBS = {
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
             *, owned_shims=None):
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

UNIFIED_RECOVERY = Path(os.environ.get(
    'MAC_MANAGER_UNIFIED_RECOVERY',
    str(Path.home() / '.local/share/brew-update-manager/recovery/unified-intel-1.3.4')
))

def unified_foundation_outputs():
    return {
        "backend/update_selection.py": UNIFIED_FOUNDATION_BACKEND_UPDATE_SELECTION_PY,
        "backend/pixi_provider.py": UNIFIED_PIXI_PROVIDER,
        "backend/unified_updates.py": UNIFIED_UPDATES,
        "backend/migration_profiles.py": UNIFIED_PROFILE_CODE,
        "tests/test_migration_profiles.py": UNIFIED_PROFILE_TESTS,
        "backend/routing_activation.py": UNIFIED_ROUTING,
        "tests/test_routing_activation.py": UNIFIED_ROUTING_TESTS,
        "tests/test_unified_updates.py": UNIFIED_UPDATES_TESTS,
        "tests/test_pixi_provider.py": UNIFIED_PIXI_TESTS,
        "backend/provider_ownership.py": UNIFIED_FOUNDATION_BACKEND_PROVIDER_OWNERSHIP_PY,
        "backend/operation_lock.py": UNIFIED_FOUNDATION_BACKEND_OPERATION_LOCK_PY,
        "tests/test_update_selection.py": UNIFIED_FOUNDATION_TESTS_TEST_UPDATE_SELECTION_PY,
        "tests/test_provider_ownership.py": UNIFIED_FOUNDATION_TESTS_TEST_PROVIDER_OWNERSHIP_PY,
        "tests/test_operation_lock.py": UNIFIED_FOUNDATION_TESTS_TEST_OPERATION_LOCK_PY,
        **UNIFIED_MODIFIED_FILES,
    }

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

def unified_foundation_main(mode):
    """Protected, no-package-mutation bundle for independently testable foundations."""
    if mode not in ('unified-check', 'unified-apply', 'unified-restore',
                    'unified-pack', 'unified-install'):
        raise ValueError('Unknown unified bundle operation')
    if mode in ('unified-pack', 'unified-install'):
        raise RuntimeError('Bundle 004 foundation is not the completed product; '
                           'pack/install are deliberately blocked pending all phases and checks')
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
