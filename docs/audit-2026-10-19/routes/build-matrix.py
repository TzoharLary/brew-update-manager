"""Build a research matrix from retained evidence. Does not install or execute packages."""
import csv
import datetime
import hashlib
import json
import pathlib
import re

OUT = pathlib.Path(__file__).resolve().parent
OLD = pathlib.Path('/Users/tzoharlary/.codex/visualizations/2026/10/18/01a14f20-7de2-7f93-a403-c15d751ba011/alternative-audit')
APP = pathlib.Path.home() / 'Library/Application Support/Homebrew Update Manager'
plan_raw = (APP / 'update-plan.json').read_bytes()
state_raw = (APP / 'state.json').read_bytes()
plan = json.loads(plan_raw)
state = json.loads(state_raw)
catalog = {i['name']: i for i in json.loads((OLD / 'conda-coverage.json').read_text())['items']}
solves_doc = json.loads((OLD / 'solver-results.json').read_text())
solves = {i['homebrew_name']: i for i in solves_doc['items']}
old_ports = {i['name']: i for i in json.loads((OLD / 'macports-archives.json').read_text())['items']}
fresh_doc = json.loads((OUT / 'fresh-metadata.json').read_text())
fresh = {(i['kind'], i['name']): i for i in fresh_doc['items']}
inventory = {i['name']: i for i in state['packages'] if i['kind'] == 'formula'}
inventory_path = OUT.parent / 'inventory/homebrew-rows.json'
receipt_inventory = {i['name']:i for i in json.loads(inventory_path.read_text())['rows'] if i['kind']=='formula'}
CONSUMER_GROUPS = {
 'cryptography':['legacy-ansible-python'], 'gnupg':['pdf-poppler'], 'gpgme':['pdf-poppler'],
 'gpgmepp':['pdf-poppler'], 'nss':['pdf-poppler'], 'p11-kit':['pdf-poppler'], 'unbound':['pdf-poppler'],
 'poppler':['pdf-poppler'], 'openjpeg':['pdf-poppler','ocr-tesseract'],
 'pango':['ocr-tesseract'], 'tesseract':['ocr-tesseract'],
 'net-snmp':['php-runtime'], 'openldap':['php-runtime'], 'php':['php-runtime'],
 'merve':['legacy-homebrew-node'], 'simdjson':['legacy-homebrew-node'],
 'llvm':['compiler-iwyu'], 'include-what-you-use':['compiler-iwyu'],
 'open-mpi':['mpi-arpack-igraph'], 'postgresql@17':['postgresql17-service'],
 'py3cairo':['python-cairo-project'], 'libtensorflow':['tensorflow-c-project'],
 'nmap':['standalone-command'], 'socat':['standalone-command'], 'wget':['standalone-command'],
 'terminal-notifier':['notification-integration'], 'whisper.cpp':['speech-model-runtime']}

# name: migration scope, recommended route, validation order, reason, concrete next validation
CONFIG = {
 'cryptography': ('python-library', 'pixi-project', 'P2-library', 'uv has no Intel macOS wheel for 50.0.2 in the checked PyPI release; conda has an osx-64 artifact. A separate Python environment does not update existing Homebrew Python imports.', 'Identify importing Python interpreters/projects; solve cryptography=50.0.2 with their Python ABI; then test imports and the actual TLS/signing workflows in a separate environment.'),
 'gnupg': ('cli-and-agent-state', 'pixi-project', 'P2-keyring', 'The command can move independently only after agent/pinentry/keyring integration is checked. No target dependency solution was performed here.', 'Solve gnupg=2.5.24; inventory agent, pinentry and configuration; validate list/verify/sign with a disposable test keyring before selecting an executable.'),
 'gpgme': ('linked-c-library', 'macports-cohort', 'P2-library', 'A matching MacPorts archive exists, but consumers linked to /usr/local/opt/gpgme cannot use /opt/local automatically; GnuPG runtime integration is separate.', 'Enumerate reverse consumers and ABI paths; verify a complete MacPorts binary dependency graph including GnuPG, then rebuild/test one consumer in that prefix.'),
 'gpgmepp': ('linked-cpp-library', 'macports-cohort', 'P2-library', 'No conda gpgmepp/libgpgmepp 2.2.0 release was found in targeted checks. MacPorts has the exact upstream archive; it remains an unverified C++ ABI migration.', 'Migrate gpgmepp, gpgme and GnuPG as one consumer-tested cohort; verify all recursive archives and C++ ABI before building a consumer.'),
 'include-what-you-use': ('project-toolchain-command', 'pixi-project', 'P1-command', 'The 2026-10-18 lock solved 0.27, but its matching Clang/LLVM headers, target and project compile commands were not exercised.', 'Revalidate the retained 0.27 lock and run the tool against a small real compile_commands.json fixture using its matching Clang resources.'),
 'libtensorflow': ('linked-c-library', 'hold-no-target-binary', 'P3-version-gap', 'Target is 2.21.0_1. Cached conda osx-64 offers 2.18.0; current 2.21.0 release metadata has no osx-64 files. Official TensorFlow C docs identify 2.16 as the final macOS x86 release.', 'Identify actual C API consumers and required TensorFlow features; decide whether a source build, supported remote runtime, or explicit application migration is justified. Do not downgrade automatically.'),
 'llvm': ('toolchain-and-linked-library', 'pixi-project-or-macports-cohort', 'P0-broken-linkage', 'The app labels installed LLVM as 23.1.3_2, equal to its target, but fresh receipt inventory identifies active 22.1.5 and a nonactive 23.1.3_2 directory without INSTALL_RECEIPT. Retained linkage evidence reports missing libz3.4.15. A conda artifact and a MacPorts archive at 23.1.3 exist, without runtime/consumer verification. Official release has macOS ARM64 binaries only.', 'Reconcile the damaged/incomplete LLVM installation and the missing z3 linkage first; solve the needed clang/llvm/lld components together, then compile/link a representative project. An llvm metapackage alone may not provide every Homebrew tool.'),
 'merve': ('linked-cpp-library', 'hold-source-or-consumer-migration', 'P0-broken-linkage', 'Retained linkage evidence reports missing libsimdutf.34. Checked conda/MacPorts names do not provide a matching binary; upstream v1.2.2 assets are source files/singleheader.zip.', 'Identify reverse consumers of the broken Homebrew library; choose a coordinated source rebuild with the correct simdutf ABI or source-vendored consumer migration. The source-only singleheader release does not repair the installed dylib.'),
 'net-snmp': ('library-cli-and-possible-service', 'pixi-project', 'P2-library', 'Conda has 5.9.5.2 metadata, but Homebrew revision _2 and PHP SNMP integration are not represented by the upstream version alone.', 'Identify CLI versus PHP library versus snmpd use; solve the exact version and test required SNMP versions/options in an isolated fixture; service configuration needs its own migration.'),
 'nmap': ('cli-suite-and-privileged-features', 'native-official', 'P1-command', 'Official x86-64 installer 7.992 exists. A DMG installer changes more than an isolated command shim; package payload, signatures, privilege requirements and Tahoe behavior are not verified.', 'Fetch with valid TLS and verify the publisher signature/digest; inspect the installer payload/prefix and component choices, then test version and a local authorized scan before changing command selection.'),
 'nss': ('linked-security-library', 'macports-cohort', 'P2-library', 'Conda cached 3.118 is below target 3.131 and a targeted 3.131 release lookup returned 404; MacPorts has Darwin25 Intel 3.131.', 'Verify the full MacPorts graph and consumer NSS/NSPR ABI plus any certificate database formats; migrate the consumer closure rather than symlinking /usr/local to /opt/local.'),
 'open-mpi': ('project-mpi-toolchain-and-library', 'pixi-project', 'P2-library', 'Conda has openmpi 5.0.11 metadata, but an MPI launcher, compiler wrappers and linked application must agree on the implementation and ABI.', 'Solve openmpi=5.0.11 with the actual project compilers and MPI variant; build and execute a two-process local sample in that environment.'),
 'openjpeg': ('linked-library-and-cli', 'pixi-project', 'P2-library', 'Conda has upstream 2.5.4, while Homebrew requests a revision rebuild. Existing Poppler/other dylib consumers remain linked to their original prefix.', 'Identify direct consumers; solve and rebuild a consumer in the chosen environment; compare a JPEG2000 decode fixture and library linkage.'),
 'openldap': ('linked-library-and-possible-service', 'hold-no-target-binary', 'P3-version-gap', 'Conda and the freshly checked MacPorts Intel index offer 2.6.13, below target 2.7.1. No target binary was established in the sources checked.', 'Separate LDAP client/PHP extension use from slapd service use; check target 2.7.1 binary publication again before a coordinated consumer/service migration. Source compilation is a separate decision.'),
 'p11-kit': ('linked-library-and-trust-configuration', 'pixi-project', 'P2-library', 'Conda has 0.26.5 artifacts only at catalog evidence level. PKCS#11 modules/trust configuration and linked consumers cannot be switched by PATH.', 'Solve 0.26.5 with a consumer and validate required modules, trust roots and lookup paths in that prefix.'),
 'pango': ('linked-text-rendering-library', 'pixi-project', 'P2-library', 'Conda has 1.58.2 artifacts; changing the library alone does not migrate Cairo/fontconfig/Harfbuzz consumers or fonts.', 'Solve the rendering consumer closure and validate Hebrew/Latin text, fonts and output against a focused fixture.'),
 'php': ('runtime-extensions-and-possible-service', 'macports-cohort', 'P2-runtime-service', 'The old conda solve is 8.5.9 below target 8.5.11_1. MacPorts php85 8.5.11 archive exists, but extensions, php.ini, FPM/web-service and required LDAP/SNMP functionality differ by build.', 'Inventory php -m/ini/service consumers without secrets; verify php85 and each needed extension binary graph, then run project tests and any FPM integration in a separate prefix.'),
 'postgresql@17': ('database-service-and-client', 'pixi-project-or-macports-service', 'P2-database', 'Old conda lock resolves 17.11 within the same major; MacPorts also has postgresql17 17.11, but the checked server subport has no Darwin25 Intel archive. Data/service/extensions are not migrated by a command replacement.', 'Inventory service, PGDATA, extensions, locale and backups; preserve major 17 and test the candidate server/extensions with a restored disposable copy before planning a cutover.'),
 'py3cairo': ('python-native-extension', 'pixi-project', 'P2-library', 'Old conda lock solves pycairo 1.29.2 with Python 3.15, which may not match consumers. PyPI 1.29.2 has zero macOS wheels in the fresh release response.', 'Identify the Python ABI used by actual imports; solve pycairo=1.29.2 with that interpreter and Cairo closure; import cairo and render a fixture before migrating the project.'),
 'simdjson': ('linked-cpp-library', 'pixi-project', 'P2-library', 'Conda has 5.0.5 artifacts without a solve. C++ consumers, headers and ABI must migrate together; simdjson is distinct from the missing simdutf library in merve.', 'Map reverse consumers, solve the project closure, rebuild and run JSON parse fixtures; do not treat this package as a fix for merve -> libsimdutf.34.'),
 'socat': ('cli', 'pixi-isolated-cli', 'P1-command', 'Old lock solves exact upstream 1.8.1.3; runtime, TLS options and exit-code behavior remain untested.', 'Refresh/reuse the retained exact lock and validate a local TCP/stdio round trip plus any required TLS feature before selecting a shim.'),
 'terminal-notifier': ('notification-app-command', 'native-official-or-macports', 'P1-command', 'Upstream 3.1.0 ZIP exists and README declares a universal binary; matching MacPorts Intel archive exists. Architecture is publisher-declared, not inspected here; version 3 changes notification APIs/options.', 'Verify ZIP digest and executable slices; inspect signature/quarantine and bundle identity; test notification delivery from the packaged app and inspect removed sender/appIcon/ignoreDnD options and new exit codes.'),
 'tesseract': ('ocr-cli-library-and-language-data', 'pixi-isolated-cli', 'P1-command', 'Old lock solves 5.5.3. Homebrew Pango blocker does not establish that a separate provider needs the same graph; language files, OCR models and any library clients still need validation.', 'Validate the 5.5.3 lock and list required traineddata; test Hebrew/English OCR fixtures and data lookup; treat libtesseract consumers as a separate coordinated migration.'),
 'unbound': ('dns-service-and-library', 'macports-service', 'P2-runtime-service', 'No target conda release was found in the checks; MacPorts has 1.26.1 Darwin25 Intel archive. Listening ports, trust anchors, config, launchd and libunbound consumers are a migration boundary.', 'Verify full binary dependency graph, inspect current config/service use, then validate unbound-checkconf and a separate unprivileged-port resolver fixture before any service cutover.'),
 'wget': ('cli', 'pixi-isolated-cli', 'P1-command', 'Old lock solves upstream 1.25.0. Homebrew _2 is a provider revision and cannot be equated to conda build _1 by string comparison.', 'Inspect the reason for the Homebrew revision, validate the retained conda lock and test HTTPS/certificates/redirects with a local fixture before selecting a command shim.'),
 'poppler': ('pdf-cli-and-linked-library', 'hold-no-target-binary', 'P3-version-gap', 'Current application solve failed at >=26.10.0. Old conda lock is 26.09.0; fresh target release lookup returned 404 and MacPorts Intel index is 26.02.0.', 'Wait for/recheck target 26.10.0 osx-64 publication or choose an explicitly scoped source build. A 26.09 CLI candidate must not mark target 26.10 completed; later test extraction/render fixtures and separate library consumers.'),
 'whisper.cpp': ('cli-and-model-runtime', 'hold-no-target-binary', 'P3-version-gap', 'Current application solve failed at >=1.9.5; old conda lock is 1.9.3. Upstream v1.9.5 release currently has no attached assets; a target binary was not established.', 'Recheck osx-64 target publication; otherwise scope a source build or explicit application alternative. Preserve model hashes/options and test transcription on an Intel sample before selecting a runtime.'),
}

def version_key(v):
    return tuple(int(n) for n in re.findall(r'\d+', v))

rows = []
for item in plan['items']:
    if item.get('ready') or item['name'] not in CONFIG:
        continue
    name = item['name']
    inv = inventory[name]
    receipt_row = receipt_inventory.get(name,{})
    scope, route, priority, why, next_action = CONFIG[name]
    target = item['version']
    upstream = re.sub(r'_\d+$', '', target)
    candidates = []
    for match in catalog.get(name, {}).get('matches', []):
        versions = [v for v in match['versions'] if re.match(r'^\d', v)]
        if not versions:
            continue
        latest = max(versions, key=version_key)
        artifacts = [a for a in match['artifacts'] if a['version'] == latest and a['subdir'] in ('osx-64', 'noarch')]
        candidates.append({'provider': 'conda-forge', 'package': match['package'], 'version': latest,
            'evidence_status': 'catalog-artifact', 'evidence_date': '2026-10-18',
            'meets_upstream_target': version_key(latest) >= version_key(upstream),
            'provider_revision_equivalence': 'not-established', 'artifacts': artifacts,
            'source': str(OLD / 'conda-coverage.json'), 'runtime_verified': False})
    solve = solves.get(name)
    if solve:
        exact_urls = [u for u in solve.get('locked_conda_urls', []) if '/'+solve['conda_package']+'-' in u]
        candidates.append({'provider': 'pixi/conda-forge', 'package': solve['conda_package'],
            'evidence_status': 'dependency-solved' if solve['solved'] else 'dependency-solve-failed',
            'evidence_date': solves_doc['checked_at'], 'constraint': solve['constraint'],
            'locked_urls': exact_urls, 'lockfile': solve.get('lockfile'), 'source': str(OLD / 'solver-results.json'),
            'runtime_verified': False, 'migration_verified': False})
    relevant_ports = {'gpgme':['gpgme'], 'gpgmepp':['gpgmepp'], 'llvm':['llvm-23'], 'nss':['nss'],
        'unbound':['unbound'], 'php':['php85'], 'openldap':['openldap'], 'postgresql@17':['postgresql17','postgresql17-server'],
        'terminal-notifier':['terminal-notifier'], 'poppler':['poppler']}.get(name, [])
    if not relevant_ports:
        relevant_ports = [p['port'] for p in old_ports.get(name, {}).get('ports', [])]
    for port in relevant_ports:
        record = fresh.get(('macports-index', port))
        if record:
            archives = record.get('darwin25_intel_archives', [])
            src, when = str(OUT/'fresh-metadata.json'), record['checked_at']
        else:
            record = next((p for p in old_ports.get(name,{}).get('ports',[]) if p['port']==port), {})
            archives = sorted(set(record.get('darwin_25_intel_archive_candidates', [])))
            src, when = str(OLD/'macports-archives.json'), '2026-10-18'
        candidates.append({'provider':'macports','package':port,'evidence_status':'archive-index' if archives else 'no-matching-archive-in-checked-index',
            'evidence_date':when,'archives':archives,'source':src,'url':record.get('url'),
            'dependency_solution_verified':False,'runtime_verified':False,'migration_verified':False})
    repo = {'terminal-notifier':'julienXX/terminal-notifier','merve':'nodejs/merve','whisper.cpp':'ggml-org/whisper.cpp'}.get(name)
    if repo:
        record=fresh[('github-latest',repo)]
        candidates.append({'provider':'native-upstream','package':repo,'version':record.get('tag'),
            'evidence_status': 'release-assets-metadata', 'evidence_date':record['checked_at'],
            'assets':record.get('assets',[]),'source':str(OUT/'fresh-metadata.json'),
            'runtime_verified':False,'architecture_verified':False})
    if name=='nmap':
        candidates.append({'provider':'native-upstream','package':'nmap','version':'7.992','evidence_status':'publisher-download-page',
            'evidence_date':'2026-10-19','url':'https://nmap.org/download','artifact_url':'https://nmap.org/dist/nmap-7.992.dmg',
            'architecture':'x86-64 (publisher documented)','artifact_downloaded':False,'runtime_verified':False,
            'local_metadata_fetch_limitation':'urllib TLS certificate validation failed; page independently retrieved through web tool; no TLS bypass performed'})
    rows.append({'name':name,'installed_version':inv.get('installed_version'),'target_version':target,
        'installed_version_semantics':'app display snapshot; compare inventory_active_version and retained receipt states',
        'inventory_active_version':receipt_row.get('active_version'), 'retained_installation_states':receipt_row.get('installed'),
        'active_direct_reverse_dependents':receipt_row.get('active_keg_direct_reverse_dependents',[]),
        'installed_reverse_dependents':receipt_row.get('installed_reverse_dependents',[]),
        'consumer_groups':CONSUMER_GROUPS[name],'consumer_inventory_source':str(inventory_path),
        'target_upstream_version':upstream,'app_provider':item.get('provider'),'app_blocked_reason':item['reason'],
        'app_blockers':item.get('blockers',[]),'app_detail':item.get('detail'),
        'migration_scope':scope,'recommended_route':route,'priority_group':priority,
        'why_still_blocked':why,'recommended_next_action':next_action,
        'research_status':'candidate-research-only','runtime_verified':False,'migration_verified':False,
        'evidence_dates': sorted(set(str(c['evidence_date'])[:10] for c in candidates)), 'candidates':candidates})

assert len(rows)==27, len(rows)
summary={'generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'host_scope':'Intel x86_64 macOS 26.7.1 / Darwin 25, as supplied by parent task',
    'policy':'Read-only metadata research. No installs, removals, updates, package execution or new dependency solves. ngrok excluded.',
    'revision_policy':'A Homebrew _N revision is not an upstream version and has no established equivalence to conda build N or MacPorts _N. Matching upstream versions are candidates, not evidence that a Homebrew revision fix is included.',
    'app_plan_path':str(APP/'update-plan.json'),'app_plan_created_at':plan.get('created_at'),
    'app_state_path':str(APP/'state.json'),'app_state_updated_at':state.get('updated_at'),
    'app_plan_sha256':hashlib.sha256(plan_raw).hexdigest(),'app_state_sha256':hashlib.sha256(state_raw).hexdigest(),
    'counts':{'packages':len(rows),'new_runtime_verified':0,'new_migrations':0}, 'items':rows}
(OUT/'deferred-route-matrix.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n')
columns=['name','installed_version','inventory_active_version','target_version','app_provider','app_blocked_reason','migration_scope','consumer_groups','recommended_route','priority_group','research_status','evidence_dates','why_still_blocked','recommended_next_action']
with (OUT/'deferred-route-matrix.tsv').open('w') as f:
    w=csv.DictWriter(f,fieldnames=columns,delimiter='\t',extrasaction='ignore');w.writeheader()
    for row in rows:
        w.writerow({**row,'evidence_dates':','.join(row['evidence_dates']),'consumer_groups':','.join(row['consumer_groups'])})
(OUT/'app-input-snapshot.json').write_text(json.dumps({'plan_created_at':plan.get('created_at'),'state_updated_at':state.get('updated_at'),
    'items':[{'plan':i,'inventory':inventory[i['name']]} for i in plan['items'] if i['name'] in CONFIG]},indent=2,ensure_ascii=False)+'\n')
table=['| חבילה | מותקן | יעד האפליקציה | מועמד להמשך | סדר/גבול מעבר |','|---|---|---|---|---|']
for r in rows:
    table.append('| '+' | '.join([r['name'],str(r['installed_version']),r['target_version'],r['recommended_route'],r['priority_group']+' / '+r['migration_scope']])+' |')
(OUT/'matrix-table.md').write_text('\n'.join(table)+'\n')
print(json.dumps({'rows':len(rows),'outputs':['deferred-route-matrix.json','deferred-route-matrix.tsv','app-input-snapshot.json','matrix-table.md'],'plan_created_at':plan.get('created_at'),'state_updated_at':state.get('updated_at')},indent=2))
