"""Combined inventory and explicit one-time migration transactions."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
import shutil
import tempfile
import time
import threading

SCAN_CONTEXT = threading.local()
import macports_provider as mp
import update_routes as routes
import catalog_refresh
import automatic_alternatives
import installation_evidence

def mapping():
    return json.loads((Path(__file__).with_name('macports-map.json')).read_text())

def combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):
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


def _combined(s, brew_scan, refresh, track_progress=False, enrich_details=True):
    installation_evidence.reset()
    previous = s.read_json(s.STATE_FILE) or {}
    status, errors = {}, []
    try:
        state = brew_scan(refresh, track_progress=track_progress, enrich_details=enrich_details, complete_progress=False)
        packages = state['packages']
        errors.extend(state.get('errors', []))
        status['homebrew'] = {'ok': not bool(state.get('errors')), 'checked_at': s.now_iso(),
                              'error': '; '.join(state.get('errors', []))}
    except s.OperationCancelledError:
        raise
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
    if track_progress:
        s.raise_if_cancel_requested()
        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',
                             current_package='MacPorts', done=1, total=3, eta_seconds=None)
    interactive = track_progress and s.read_current_operation().get('name') == 'check_now'
    catalog_state = catalog_refresh.ensure(s, refresh=refresh, interactive=interactive)
    if track_progress:
        s.set_check_progress(phase='collecting_macports', message='Checking MacPorts inventory',
                             current_package='MacPorts', eta_seconds=None)
    try:
        ports = mp.inventory(s)
        if not catalog_state['fresh']:
            ports = [dict(p, inventory_stale=True, update_block_reason='inventory_unavailable') for p in ports]
        packages.extend(ports)
        status['macports'] = {'ok': catalog_state['fresh'], 'checked_at': s.now_iso(),
                             'catalog': catalog_state, 'catalog_note': catalog_state['status']}
    except s.OperationCancelledError:
        raise
    except Exception as error:
        errors.append('MacPorts: ' + str(error))
        packages.extend(dict(p, inventory_stale=True, update_block_reason='inventory_unavailable')
                        for p in previous.get('packages', []) if p.get('provider') == 'macports')
        status['macports'] = {'ok': False, 'error': str(error), 'catalog': catalog_state,
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
    if track_progress:
        s.raise_if_cancel_requested()
        s.set_check_progress(phase='preflight', message='Checking binary routes and migration proposals',
                             current_package=None, done=2, total=3, eta_seconds=None)
    offers = []
    try:
        from update_engine import Engine
        engine = Engine(s)
        catalog, installed = engine.catalog(), engine.installed()
        for p in packages:
            if p['provider'] == 'homebrew' and p['kind'] == 'formula' and p.get('outdated') and not p.get('excluded'):
                blockers = routes.bottle_blockers(p['name'], catalog, installed, s.load_settings()['excluded_packages'])
                if blockers:
                    if s.load_settings()['binary_only']:
                        p['update_block_reason'] = 'source_required'
                    offers.append(dict(name=p['name'], ports=links.get(p['name'], []),
                        status='requires_live_check' if links.get(p['name']) else 'mapping_unverified',
                        executable=p['name']=='terminal-notifier', source_blockers=blockers))
    except s.OperationCancelledError:
        raise
    except Exception as error:
        errors.append('Migration recommendations: ' + str(error))
    if refresh:
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
                    and prior.get('version_policy') == automatic_alternatives.VERSION_POLICY
                    and prior.get('installed_version') == pkg.get('installed_version')
                    and prior.get('status') not in {'requires_live_check','checking_pending'}
                    and catalog_refresh.recent(prior.get('checked_at'), 6*3600)):
                offer.update(prior)
            else:
                offer.update(status='checking_pending' if offer['ports'] else 'mapping_unverified')
            pkg.update(alternative_status=offer['status'], alternative_detail=offer.get('detail',''),
                       alternative_target=offer.get('target'), alternative_below_homebrew=offer.get('below_homebrew_target', False))
    offers = installation_evidence.annotate(s, offers, packages, refresh, report=track_progress)
    journal = s.read_json(s.DATA_DIR / 'migration-journal.json')
    state = dict(app=s.APP_NAME, updated_at=s.now_iso(), packages=packages,
                 counts=s.summarize_counts(packages), errors=errors, providers=status,
                 partial=bool(errors) or not all(p['ok'] for p in status.values()), migrations=offers,
                 migration_journal=journal)
    if track_progress:
        s.raise_if_cancel_requested()
    s.atomic_write_json(s.STATE_FILE, state)
    if track_progress:
        failed = not any(provider['ok'] for provider in status.values())
        s.set_check_progress(running=False, phase='error' if failed else 'completed',
            message='All inventory checks failed' if failed else
                    ('Inventory checked with warnings' if state['partial'] else 'Package inventories checked'),
            done=3, total=3, current_package=None, error='; '.join(errors) if errors else None,
            completed_at=s.now_iso(), eta_seconds=None if failed else 0)
    return state

def migration_plan(s, name):
    if name == 'ngrok' or s.is_excluded(name, 'formula'):
        raise ValueError('Package is excluded')
    roots = mapping().get(name, [])
    if not roots:
        raise ValueError('No verified package mapping')
    current = s.fetch_installed_inventory(s.fetch_outdated_index())
    pkg = next((p for p in current if p['name'] == name and p['kind'] == 'formula'), None)
    if pkg and (pkg.get('inventory_stale') or pkg.get('inventory_verification', {}).get('status', 'verified') != 'verified'):
        raise ValueError('Installation evidence is unverified; scan and resolve the reported inventory gap')
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
    candidates = {root: next(n['version'] for n in graph['nodes'] if n['name'] == root) for root in roots}
    comparison = automatic_alternatives.compare_versions(probe, pkg['installed_version'], pkg['latest_version'], candidates)
    if comparison['status'] != 'version_eligible':
        raise ValueError(comparison['detail'])
    consumers = mp.checked(s, [s.brew_bin(), 'uses', '--installed', name], timeout=90).splitlines()
    plan = dict(name=name, roots=roots, installed_version=pkg['installed_version'], target=target,
        version_policy=automatic_alternatives.VERSION_POLICY,
        homebrew_target=pkg['latest_version'], below_homebrew_target=comparison['below_homebrew_target'],
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
    for key in ('installed_version', 'target', 'consumers', 'remove_brew', 'version_policy'):
        if old.get(key) != fresh.get(key):
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
