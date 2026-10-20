"""Profiles for conservative Intel MacPorts migrations."""
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
