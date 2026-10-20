#!/usr/bin/env python3
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
