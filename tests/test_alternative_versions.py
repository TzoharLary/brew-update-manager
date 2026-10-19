import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_update_policy import service as s
import automatic_alternatives as aa
import mac_manager as mm
import macports_provider as mp

class AlternativeVersions(unittest.TestCase):
    def setUp(self):
        self.probe=mp.load_probe()

    def test_candidate_between_installed_and_brew_is_eligible(self):
        result=aa.compare_versions(self.probe,'1.0_4','3.0_2',{'first':'2.0'})
        self.assertEqual(result['status'],'version_eligible')
        self.assertTrue(result['below_homebrew_target'])

    def test_downgrade_and_equal_are_not_updates(self):
        self.assertEqual(aa.compare_versions(self.probe,'2','4',{'first':'1'})['status'],'older_than_installed')
        self.assertEqual(aa.compare_versions(self.probe,'2_3','4',{'first':'2.0'})['status'],'not_newer')

    def test_all_mapped_components_must_avoid_downgrade(self):
        self.assertEqual(aa.compare_versions(self.probe,'2','4',{'first':'3','second':'1'})['status'],'older_than_installed')
        self.assertEqual(aa.compare_versions(self.probe,'2','4',{'first':'3','second':'2'})['status'],'version_eligible')

    def test_unknown_baseline_blocks_but_unknown_brew_target_does_not(self):
        self.assertEqual(aa.compare_versions(self.probe,'unknown','3',{'first':'2'})['status'],'version_unverified')
        self.assertEqual(aa.compare_versions(self.probe,'1','unknown',{'first':'2'})['status'],'version_eligible')
        self.assertEqual(aa.compare_versions(self.probe,'1','3',{'first':'2-rc1'})['status'],'version_unverified')

    def test_explicit_migration_uses_installed_baseline_and_never_downgrades(self):
        pkg=dict(name='terminal-notifier',kind='formula',installed_version='1',latest_version='3')
        node=dict(name='terminal-notifier',version='2',revision='0',variants='')
        from update_engine import Engine
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'DATA_DIR',Path(tmp)), \
             patch.object(s,'is_excluded',return_value=False), \
             patch.object(s,'fetch_outdated_index',return_value={}), \
             patch.object(s,'fetch_installed_inventory',return_value=[pkg]), \
             patch.object(Engine,'catalog',return_value={}),patch.object(Engine,'installed',return_value={}), \
             patch.object(mm.routes,'bottle_blockers',return_value=[{'reason':'source_required'}]), \
             patch.object(mp,'preflight',return_value={'nodes':[node]}),patch.object(mp,'checked',return_value=''), \
             patch.object(s,'brew_bin',return_value='/fixture/brew'):
            plan=mm.migration_plan(s,'terminal-notifier')
            self.assertEqual(plan['target'],'2')
            self.assertTrue(plan['below_homebrew_target'])
            pkg['installed_version']='2.5'
            with self.assertRaisesRegex(ValueError,'downgrade'):mm.migration_plan(s,'terminal-notifier')

    def test_cached_offer_requires_same_policy_and_installed_baseline(self):
        from update_engine import Engine
        for policy,baseline,expected in [(1,'1','checking_pending'),(2,'0.5','checking_pending'),(2,'1','binary_available')]:
            pkg=dict(name='fixture',kind='formula',provider='homebrew',installed_version='1',latest_version='3',outdated=True)
            prior=dict(name='fixture',ports=['fixture'],homebrew_target='3',installed_version=baseline,
                       target='2',version_policy=policy,checked_at=s.now_iso(),status='binary_available')
            with tempfile.TemporaryDirectory() as tmp,patch.object(s,'DATA_DIR',Path(tmp)), \
                 patch.object(s,'STATE_FILE',Path(tmp)/'state.json'),patch.object(s,'read_json',return_value={'migrations':[prior]}), \
                 patch.object(mm.catalog_refresh,'ensure',return_value={'fresh':True,'status':'fresh'}), \
                 patch.object(mp,'inventory',return_value=[]),patch.object(mm,'mapping',return_value={'fixture':['fixture']}), \
                 patch.object(mm.routes,'read_records',return_value={}), \
                 patch.object(Engine,'catalog',return_value={}),patch.object(Engine,'installed',return_value={}), \
                 patch.object(mm.routes,'bottle_blockers',return_value=[{'reason':'source_required'}]):
                state=mm.combined(s,lambda *a,**kw:{'packages':[pkg]},False,False,False)
            self.assertEqual(state['migrations'][0]['status'],expected)
