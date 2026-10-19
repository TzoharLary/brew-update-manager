"""Build planning artifacts from saved evidence only; never invoke a package manager."""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(relative):
    return json.loads((ROOT / relative).read_text())


def write(relative, value):
    (ROOT / relative).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


inventory = read('inventory/homebrew-rows.json')
snapshot = read('inventory/app-snapshot.json')
routes = {r['name']: r for r in read('routes/deferred-route-matrix.json')['items']}
prior = read('prior-root-candidates.json')
prior_by_name = {r['name']: r for r in prior['items']}
app_rows = {(r['kind'], r['name']): r for r in snapshot['state.json']['packages']}
managed = snapshot['managed_registry']
result = []

for row in inventory['rows']:
    name, kind = row['name'], row['kind']
    app = app_rows.get((kind, name), {})
    detail = routes.get(name) if kind == 'formula' else None
    older = prior_by_name.get(name) if kind == 'formula' else None
    excluded = bool(row.get('excluded_from_actions'))
    formula = kind == 'formula'
    version = row.get('active_version') if formula else row.get('installed')
    entry = {
        'installation_id': f"homebrew:{kind}:{row.get('tap')}/{name}@/usr/local",
        'name': name, 'kind': kind, 'owner': 'homebrew', 'scope': '/usr/local',
        'active_version': version,
        'retained_versions': row.get('installed_versions', []),
        'role': row['role'], 'excluded': excluded,
        'inventory_source': row.get('metadata_source', 'brew_info_installed'),
        'provider_outdated': row.get('outdated'),
        'app_outdated': app.get('outdated'),
        'app_target': app.get('latest_version'),
        'also_has_managed_installation': formula and name in managed,
        'active_direct_consumers': row.get('active_keg_direct_reverse_dependents', []),
        'receipt_warning': [r['version'] for r in row.get('installed', [])
                            if isinstance(r, dict) and r.get('receipt_exists') is False] if formula else [],
        'candidate_route': None, 'candidate_evidence': None,
        'target_verified_by_installation_in_this_audit': False,
        'next_action': 'Keep existing owner; discover releases through its adapter and resolve dependencies before updating.',
    }
    if older:
        entry.update(candidate_route=older['recommended_route'],
                     candidate_evidence={'level': older['category'], 'date': prior['generated_at'],
                                         'versions': older.get('resolved_versions'),
                                         'scope': 'Historical candidate only; not revalidated for current target.'})
    if detail:
        entry.update(candidate_route=detail['recommended_route'],
                     candidate_evidence={'level': 'candidate-research-only',
                                         'detail_file': 'routes/deferred-route-matrix.json',
                                         'target': detail['target_version']},
                     next_action=detail['recommended_next_action'])
    elif formula and name in managed:
        entry['next_action'] = 'Reconcile retained Brew keg and its consumers independently of the active managed CLI. No removal inferred.'
    elif formula and row['role'] != 'requested_root':
        entry['next_action'] = 'Plan with owning consumer/environment; do not migrate a dependency by matching its name. Unknown usage is not removable.'
    if row.get('metadata_source') == 'cellar_receipts_omitted_from_brew_info':
        entry['next_action'] = 'Restore inventory coverage using receipts; diagnose provider metadata omission and service/data dependencies before planning a change.'
    if excluded:
        entry.update(candidate_route=None, candidate_evidence=None,
                     next_action='EXCLUDED: retain visible installed record; no update, migration, installation or removal action.')
    result.append(entry)

for name, row in managed.items():
    result.append({
        'installation_id': f"managed:{row['provider']}:{name}@{row['prefix']}",
        'name': name, 'kind': 'managed_tool', 'owner': row['provider'],
        'scope': row['prefix'], 'active_version': row['version'], 'retained_versions': [],
        'role': 'user_cli', 'excluded': False,
        'inventory_source': 'managed_registry', 'provider_outdated': None,
        'app_outdated': app_rows.get(('formula', name), {}).get('outdated'),
        'app_target': app_rows.get(('formula', name), {}).get('latest_version'),
        'also_has_managed_installation': False, 'active_direct_consumers': [],
        'receipt_warning': [], 'candidate_route': row['provider'],
        'candidate_evidence': {'level': 'existing_activation_receipt', 'verified_at': row['verified_at'],
                               'scope': 'Previous activation verification; no new runtime validation in this audit.'},
        'target_verified_by_installation_in_this_audit': False,
        'next_action': 'Discover independently of Brew; query owning provider releases, preserve channel/major policy and verify all exports before activation.',
    })

result.sort(key=lambda r: (r['name'], r['owner'], r['kind']))
assert len({r['installation_id'] for r in result}) == len(result)
assert len(inventory['rows']) == 240
assert len(result) == 245
assert len(routes) == 27
assert {r['name'] for r in snapshot['state.json']['packages'] if r.get('outdated')} == set(routes)
assert all(r['excluded'] for r in result if r['name'] == 'ngrok')
assert not any(r['candidate_route'] for r in result if r['name'] == 'ngrok')
assert all(any(r['name'] == n and r['owner'] == 'homebrew' for r in result) for n in managed)

write('installation-plan-matrix.json', {
    'schema_version': 1, 'inventory_captured_at': inventory['captured_at'],
    'scope': '240 Homebrew package identities plus 5 managed alternative installations. Retained kegs are nested, not extra rows. Other-manager/native-app discovery is separate; this is not a count of all software on the Mac.',
    'counts': {'rows': len(result), 'homebrew': 240, 'managed_alternatives': 5,
               'deferred_app_updates': len(routes), 'excluded': sum(r['excluded'] for r in result)},
    'rows': result,
})
columns = ['name', 'kind', 'owner', 'active_version', 'role', 'excluded',
           'provider_outdated', 'app_outdated', 'app_target', 'inventory_source',
           'active_direct_consumers', 'candidate_route', 'candidate_evidence', 'next_action', 'scope']
with (ROOT / 'installation-plan-matrix.tsv').open('w', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=columns, delimiter='\t', extrasaction='ignore')
    writer.writeheader()
    for entry in result:
        writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                         for key, value in entry.items()})

inputs = ['inventory/homebrew-rows.json', 'inventory/app-snapshot.json',
          'routes/deferred-route-matrix.json', 'prior-root-candidates.json']
write('validation.json', {
    'result': 'passed', 'checks': ['240 unique Homebrew identities', '5 separate managed installations',
        '27 deferred targets match application snapshot', 'ngrok excluded with no candidate route',
        'all managed tools retain separately visible Brew records', 'unique installation IDs'],
    'scope': 'Artifact consistency only; no installation, benchmark, dependency solve or runtime test.',
    'input_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in inputs},
    'row_counts_by_owner': dict(Counter(r['owner'] for r in result)),
})
print(json.dumps({'rows': len(result), 'deferred': len(routes), 'validation': 'passed'}))
