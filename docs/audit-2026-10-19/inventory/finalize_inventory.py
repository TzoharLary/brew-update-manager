"""Finalize saved discovery using local receipts only. Never invokes package managers."""
import collections
import datetime
import json
from pathlib import Path

B = Path(__file__).resolve().parent
def load(name): return json.loads((B/name).read_text())
def save(name,d): (B/name).write_text(json.dumps(d,indent=2,ensure_ascii=False)+'\n')
raw=load('homebrew-installed-metadata.json')
app=load('app-snapshot.json')
formulae={f['name']:f for f in raw['formulae']}
cellar=Path('/usr/local/Cellar')
physical={p.resolve().name:p.resolve() for p in cellar.iterdir() if p.is_dir()}
omitted=sorted(set(physical)-set(formulae))
for n in omitted:
    versions=[]
    for p in physical[n].iterdir():
        receipt=p/'INSTALL_RECEIPT.json'
        if receipt.exists(): versions.append({'version':p.name})
    first=json.loads((physical[n]/versions[0]['version']/'INSTALL_RECEIPT.json').read_text())
    formulae[n]={'name':n,'full_name':first['source']['tap']+'/'+n,'tap':first['source']['tap'],
                'installed':versions,'audit_metadata_source':'cellar_receipts_omitted_from_brew_info',
                'outdated':None,'dependencies':None,'build_dependencies':None}
receipts=[]; edges=[]; rows=[]
aliases={f.get('full_name',n):n for n,f in formulae.items()}
def short(n): return aliases.get(n,n.rsplit('/',1)[-1])
for n,f in sorted(formulae.items()):
    opt=Path('/usr/local/opt')/n
    active=opt.resolve().name if opt.exists() else f.get('linked_keg')
    installs=[]
    for i in f['installed']:
        path=physical[n]/i['version']/'INSTALL_RECEIPT.json'
        d=json.loads(path.read_text()) if path.exists() else i
        r={k:d.get(k) for k in ['installed_on_request','installed_as_dependency','built_as_bottle',
             'poured_from_bottle','arch','time','compiler','homebrew_version']}
        r.update({'name':n,'version':i['version'],'active_keg':i['version']==active,
                 'receipt_path':str(path),'receipt_exists':path.exists(),
                 'runtime_dependencies_recorded':d.get('runtime_dependencies') is not None,
                 'runtime_dependencies':d.get('runtime_dependencies') or [],
                 'install_status':'receipt_present' if path.exists() else 'unverified_empty_keg_directory'})
        receipts.append(r)
        installs.append({k:r[k] for k in ['version','active_keg','installed_on_request',
                          'installed_as_dependency','receipt_exists','install_status']})
        for dep in r['runtime_dependencies']:
            dn=short(dep['full_name'])
            installed_versions={p.name for p in physical[dn].iterdir() if p.is_dir()} if dn in physical else set()
            required=dep.get('pkg_version',dep.get('version'))
            edges.append({'from':n,'from_version':i['version'],'active_keg':r['active_keg'],'to':dn,
                'required_version':required,'required_version_directory_present':required in installed_versions,
                'target_installed':dn in formulae,'declared_directly':dep.get('declared_directly'),
                'source':'installed_receipt_runtime_dependencies'})
    requested=any(x['installed_on_request'] is True for x in installs)
    depflag=any(x['installed_as_dependency'] is True for x in installs)
    rows.append({'id':'formula:'+n,'name':n,'kind':'formula','tap':f.get('tap'),
        'installed':installs,'installed_versions':[x['version'] for x in installs],
        'active_version':active,'linked_keg':f.get('linked_keg'), 'keg_only':f.get('keg_only'),
        'installed_on_request':requested,'installed_as_dependency':True if depflag else (
             False if all(x['installed_as_dependency'] is False for x in installs) else None),
        'role':'requested_root' if requested else ('dependency' if depflag else 'not_requested_dependency_flag_unknown'),
        'metadata_source':f.get('audit_metadata_source','brew_info_installed'),
        'latest_metadata_version':f.get('versions',{}).get('stable'),
        'current_metadata_dependencies':f.get('dependencies'),
        'current_metadata_build_dependencies':f.get('build_dependencies'),
        'outdated':f.get('outdated'),'pinned':f.get('pinned'), 'excluded_from_actions':False})
for c in raw['casks']:
    n=c['token']
    rows.append({'id':'cask:'+n,'name':n,'kind':'cask','tap':c.get('tap'),'installed':c.get('installed'),
       'role':'cask_root','outdated':c.get('outdated'),'excluded_from_actions':n=='ngrok',
       'depends_on':c.get('depends_on',{}),'artifacts':c.get('artifacts',[])})
    for kind in ['formula','cask']:
        deps=c.get('depends_on',{}).get(kind,[])
        if isinstance(deps,str): deps=[deps]
        for dn in deps: edges.append({'from':n,'from_kind':'cask','to':dn,'to_kind':kind,
             'source':'current_cask_metadata','declared_directly':True})
reverse=collections.defaultdict(set); directreverse=collections.defaultdict(set); activereverse=collections.defaultdict(set)
for e in edges:
    if e['source']=='installed_receipt_runtime_dependencies':
        reverse[e['to']].add(e['from'])
        if e['declared_directly']: directreverse[e['to']].add(e['from'])
        if e['declared_directly'] and e['active_keg']: activereverse[e['to']].add(e['from'])
for r in rows:
    n=r['name'];r['installed_reverse_dependents']=sorted(reverse[n]) if r['kind']=='formula' else []
    r['installed_direct_reverse_dependents']=sorted(directreverse[n]) if r['kind']=='formula' else []
    r['active_keg_direct_reverse_dependents']=sorted(activereverse[n]) if r['kind']=='formula' else []
    r['installed_reverse_dependent_count']=len(r['installed_reverse_dependents'])
save('homebrew-receipts.json',receipts)
save('homebrew-rows.json',{'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'rows':rows})
semantics='Receipt runtime requirements across all retained installed kegs, not measured dylib linkage/health. declared_directly true edges form the direct graph; false edges are separately recorded transitive requirements. active_keg identifies opt-target parent keg. Missing llvm23 receipt is unverified, not proof of a complete installation. Cask dependencies use current metadata.'
save('homebrew-edges.json',{'edge_semantics':semantics,'edges':edges})
save('homebrew-direct-edges.json',{'edge_semantics':semantics,'edges':[e for e in edges if e.get('declared_directly') is True]})
save('homebrew-cellar-reconciliation.json',{'brew_info_formula_count':len(raw['formulae']),
    'cellar_directory_count':len([p for p in cellar.iterdir() if p.is_dir()]),
    'canonical_cellar_formula_count':len(physical),'omitted_formulae':omitted,
    'omission_cause':'Not proven. Both omitted receipts name mongodb/brew; runtime warned separately that tap was untrusted.',
    'alias_directories':[{'path':str(p),'canonical_path':str(p.resolve())} for p in cellar.iterdir() if p.is_symlink()]})
old=load('summary.json')
old.update({'formula_count':len(formulae),'cask_count':len(raw['casks']),'homebrew_physical_package_count':len(rows),
    'brew_info_formula_count':len(raw['formulae']),'formula_roles':dict(collections.Counter(r['role'] for r in rows if r['kind']=='formula')),
    'installed_formula_receipt_count':sum(r['receipt_exists'] for r in receipts),'formula_version_records':len(receipts),
    'runtime_dependency_edge_count':sum(e['source']=='installed_receipt_runtime_dependencies' for e in edges),
    'direct_runtime_dependency_edge_count':sum(e['source']=='installed_receipt_runtime_dependencies' and e['declared_directly'] is True for e in edges),
    'transitive_runtime_requirement_edge_count':sum(e['source']=='installed_receipt_runtime_dependencies' and e['declared_directly'] is False for e in edges),
    'active_keg_direct_runtime_edge_count':sum(e['source']=='installed_receipt_runtime_dependencies' and e['declared_directly'] is True and e['active_keg'] for e in edges),
    'missing_receipts':[{'name':r['name'],'version':r['version'],'active_keg':r['active_keg']} for r in receipts if not r['receipt_exists']],
    'omitted_from_brew_info':omitted,
    'missing_installed_targets':sorted({e['to'] for e in edges if e.get('target_installed') is False}),
    'required_version_directory_missing_edge_count':sum(e.get('required_version_directory_present') is False for e in edges),
    'top_reverse_dependents':sorted([{'name':n,'count':len(v)} for n,v in reverse.items()],key=lambda x:(-x['count'],x['name']))[:25]})
save('summary.json',old)
manager=load('other-managers.json')
manager['incidental_effects_and_limits']={
    'old_uv':'Old uv tool list reported evicting malformed interpreter cache entries. Cache entries cannot be reconstructed; no package install/remove/update invoked.',
    'bun':'bun pm ls -g unexpectedly enumerated home project (284 packages,6 top level), reported reading .env and migrating package-lock. Output is wrong scope, NOT a global Bun inventory. No .env values were printed or retained. No persisted Bun lock exists in home, repo, or ~/.bun/install/global; home package and npm lock mtime remain January2026. No user files restored/deleted.',
    'reproduction':'Collector excludes Bun list and only probes current managed uv tool list; finalize script invokes no managers.'}
manager['pipx_filesystem_metadata']=[]
for p in [Path.home()/'.local/pipx/venvs/imagesorcery-mcp/pipx_metadata.json']:
    if p.exists():
        d=json.loads(p.read_text()); main=d.get('main_package',{})
        manager['pipx_filesystem_metadata'].append({'metadata_path':str(p),'package':main.get('package'),
             'package_version':main.get('package_version'),'apps':main.get('apps'),'python_version':d.get('python_version')})
save('other-managers.json',manager)
print(json.dumps(old,indent=2))
