"""Durable, profile-bound Homebrew to MacPorts transactions."""
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
