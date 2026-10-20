"""Read-only, batched MacPorts archive observations for automatic recommendations.

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

VERSION_POLICY = 2

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


def check(s, offers, packages, catalog_fresh, report=False):
    """Return annotated offers; attach matching annotations to package inventory rows.

    Resolve all eligible roots once per scan, including scans served by HEAD cache.
    The current resolver graph, exclusions, versions, arch, and installed variants
    must still pass on every scan. No metadata result itself grants install approval.
    """
    checked_at = s.now_iso()
    output = [dict(offer, checked_at=checked_at, archive_count=0, version_policy=VERSION_POLICY,
                   status='requires_live_check', detail=None,
                   executable=bool(offer.get('executable')) and offer.get('name') in {'terminal-notifier','include-what-you-use'}) for offer in offers]
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
        offer['installed_version'] = package_by_name.get(name, {}).get('installed_version')
        offer['target'] = None
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
                    offer['target'] = versions[selected_roots[0]]
                    comparison = compare_versions(probe, offer['installed_version'], offer['homebrew_target'], versions)
                    offer['below_homebrew_target'] = comparison.get('below_homebrew_target', False)
                    offer['version_detail'] = comparison['detail']
                    if comparison['status'] != 'version_eligible':
                        mark(offer, comparison['status'], comparison['detail'])
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
                    mark(offer, 'binary_available', offer['version_detail'] + '; all runtime archives and signatures are available; installation and integrity remain unverified')
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
            row.update(alternative_status=offer['status'], alternative_detail=offer['detail'],
                       alternative_target=offer.get('target'), alternative_below_homebrew=offer.get('below_homebrew_target', False))
    return output
