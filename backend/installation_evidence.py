"""Bounded local installation evidence; classification never authorizes migration."""
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
