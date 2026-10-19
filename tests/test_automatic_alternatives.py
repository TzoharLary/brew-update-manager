import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import automatic_alternatives as aa

class Cancelled(Exception):
    pass

class AutomaticAlternativesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {}
        self.cancel = False
        self.calls = []
        self.s = SimpleNamespace(DATA_DIR=Path(self.tmp.name), OperationCancelledError=Cancelled,
            now_iso=lambda: '2026-10-19T00:00:00Z', is_excluded=lambda name, kind: False,
            raise_if_cancel_requested=self.check_cancel,
            read_json=lambda path: self.saved.get(str(path), {}), atomic_write_json=self.save)
        self.nodes = [self.node('first', '1.2', 'shared'), self.node('second', '1.2', 'shared'), self.node('shared')]
        self.closures = {'first': ['first', 'shared'], 'second': ['second', 'shared']}
        self.probe_calls = []
        self.statuses = {}
        self.probe = SimpleNamespace(version=lambda v: None if v is None or not str(v).replace('.', '').isdigit() else tuple(map(int, v.split('.'))),
                                     check_node=self.head)
        self.patches = [patch.object(aa.mp, 'checked', side_effect=self.resolve),
                        patch.object(aa.mp, 'load_probe', return_value=self.probe),
                        patch.object(aa.platform, 'machine', return_value='x86_64'),
                        patch.object(aa.platform, 'release', return_value='23.0')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def node(self, name, version='1.2', requires=''):
        return dict(name=name, version=version, revision='0', variants='', arch='x86_64',
                    archive=name+'-1.2_0.darwin_23.x86_64.tbz2', requires=requires, porturl='file:///metadata')

    def check_cancel(self):
        if self.cancel:
            raise Cancelled()

    def save(self, path, payload):
        self.saved[str(path)] = payload

    def head(self, row):
        self.probe_calls.append(row['name'])
        return dict(row, archive_status=self.statuses.get(row['name'], 'available'))

    def resolve(self, s, args, **kwargs):
        self.calls.append(args[3:])
        directory = Path(args[2])
        def write(name, fields, rows):
            with (directory/name).open('w') as stream:
                writer = csv.DictWriter(stream, fields, delimiter='\t')
                writer.writeheader()
                writer.writerows(rows)
        write('nodes.tsv', list(self.nodes[0]), self.nodes)
        write('root-results.tsv', ['root','status','error'],
              [dict(root=r,status='0',error='') for r in args[3:]])
        by_name = {r['name']:r for r in self.nodes}
        write('root-nodes.tsv', ['root','name','archive'],
              [dict(root=root,name=name,archive=by_name[name]['archive'])
               for root in args[3:] for name in self.closures[root]])

    def run_check(self, roots=('first','second'), fresh=True):
        packages = [dict(name=root, provider='homebrew', kind='formula', installed_version='1.0', latest_version='1.2') for root in roots]
        offers = [dict(name=root,ports=[root]) for root in roots]
        return aa.check(self.s,offers,packages,fresh), packages

    def test_union_resolver_shared_archives_and_exact_cache(self):
        result, packages = self.run_check()
        self.assertEqual(self.calls, [['first','second']])
        self.assertCountEqual(self.probe_calls, ['first','second','shared'])
        self.assertTrue(all(r['status']=='binary_available' and r['archive_count']==2 for r in result))
        self.assertTrue(all(p['alternative_status']=='binary_available' for p in packages))
        self.run_check()
        self.assertEqual(len(self.calls), 2)  # metadata is resolved on every scan
        self.assertEqual(len(self.probe_calls), 3)
        self.nodes[-1]['variants']='+new'
        self.run_check()
        self.assertEqual(self.probe_calls.count('shared'), 2)

    def test_ready_candidate_may_be_below_homebrew_target(self):
        packages=[dict(name='first',provider='homebrew',kind='formula',installed_version='1.0',latest_version='3')]
        result=aa.check(self.s,[dict(name='first',ports=['first'])],packages,True)[0]
        self.assertEqual(result['status'],'binary_available')
        self.assertEqual(result['target'],'1.2')
        self.assertEqual(result['homebrew_target'],'3')
        self.assertTrue(result['below_homebrew_target'])
        self.assertEqual(packages[0]['alternative_target'],'1.2')

    def test_missing_transitive_is_distinct_from_network_unknown(self):
        self.statuses['shared']='missing_from_checked_sites'
        self.assertTrue(all(r['status']=='archive_missing' for r in self.run_check()[0]))
        self.saved.clear()
        self.statuses['shared']='unknown'
        self.assertTrue(all(r['status']=='archive_unknown' for r in self.run_check()[0]))
        self.run_check()
        self.assertEqual(self.probe_calls.count('shared'), 3)  # unknown never cached

    def test_mapping_catalog_and_old_version(self):
        result = aa.check(self.s,[dict(name='other',ports=[])],[],False)
        self.assertEqual(result[0]['status'],'mapping_unverified')
        self.assertEqual(self.run_check(fresh=False)[0][0]['status'],'catalog_unverified')
        self.assertFalse(self.calls)
        self.nodes[0]['version']='0.9'
        self.assertEqual(self.run_check()[0][0]['status'],'older_than_installed')

    def test_excluded_dependency_and_missing_closure_do_not_probe(self):
        self.s.is_excluded=lambda name,kind: name=='shared'
        self.assertTrue(all(r['status']=='excluded' for r in self.run_check()[0]))
        self.assertFalse(self.probe_calls)
        self.s.is_excluded=lambda *args: False
        self.closures['first']=['first']
        result,_=self.run_check()
        self.assertEqual(result[0]['status'],'resolution_failed')

    def test_cancel_before_resolver_and_on_completion(self):
        self.cancel=True
        with self.assertRaises(Cancelled): self.run_check()
        self.assertFalse(self.calls)
        self.cancel=False
        def cancel_head(row):
            self.cancel=True
            return dict(row,archive_status='available')
        self.probe.check_node=cancel_head
        with self.assertRaises(Cancelled): self.run_check()
        self.assertFalse(self.saved)

    def test_cache_age_host_and_variants_are_bounded(self):
        self.run_check()
        with patch.object(aa.time,'time',return_value=aa.time.time()+21601): self.run_check()
        self.assertEqual(len(self.probe_calls),6)
        with patch.object(aa.platform,'release',return_value='24.0'): self.run_check()
        self.assertEqual(len(self.probe_calls),9)

    def test_missing_observation_expires_after_thirty_minutes(self):
        self.statuses['shared']='missing_from_checked_sites'
        self.run_check()
        self.run_check()
        self.assertEqual(self.probe_calls.count('shared'),1)
        with patch.object(aa.time,'time',return_value=aa.time.time()+1801): self.run_check()
        self.assertEqual(self.probe_calls.count('shared'),2)
        self.assertEqual(self.probe_calls.count('first'),1)

    def test_architecture_and_installed_variant_mismatch(self):
        self.nodes[-1]['arch']='arm64'
        self.assertTrue(all(r['status']=='architecture_unverified' for r in self.run_check()[0]))
        self.assertFalse(self.probe_calls)
        self.nodes[-1]['arch']='noarch'
        packages=[dict(name='first',provider='homebrew',kind='formula',installed_version='1.0',latest_version='1.2'),
                  dict(name='shared',provider='macports',installed_variants='+custom')]
        result=aa.check(self.s,[dict(name='first',ports=['first'])],packages,True)
        self.assertEqual(result[0]['status'],'variants_unverified')

if __name__ == '__main__':
    unittest.main()
