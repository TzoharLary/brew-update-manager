import os,sys,tempfile,unittest
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
