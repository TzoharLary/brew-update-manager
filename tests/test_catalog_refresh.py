import tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_update_policy import service as s
import catalog_refresh as catalog
import macports_provider as mp

class CatalogRefresh(unittest.TestCase):
    def test_fresh_index_needs_no_prompt(self):
        with patch.object(catalog,'fingerprint',return_value={'mtime_ns':int(time.time()*1e9),'size':1}), \
             patch.object(s,'read_json',return_value={}),patch.object(mp,'privileged') as auth:
            self.assertTrue(catalog.ensure(s,True,True)['fresh'])
            auth.assert_not_called()

    def test_scheduled_stale_catalog_never_prompts(self):
        with patch.object(catalog,'inspect',return_value={'fresh':False,'status':'stale'}), \
             patch.object(Path,'is_file',return_value=True),patch.object(mp,'privileged') as auth:
            self.assertEqual(catalog.ensure(s,True,False)['status'],'awaiting_authorization')
            auth.assert_not_called()

    def test_declined_authorization_is_not_fresh(self):
        with patch.object(catalog,'inspect',return_value={'fresh':False,'status':'stale'}), \
             patch.object(Path,'is_file',return_value=True),patch.object(s,'raise_if_cancel_requested'), \
             patch.object(s,'append_service_log'),patch.object(mp,'privileged',side_effect=RuntimeError('declined')):
            self.assertEqual(catalog.ensure(s,True,True)['status'],'refresh_failed')

    def test_successful_unchanged_sync_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(s,'DATA_DIR',Path(tmp)), \
             patch.object(catalog,'fingerprint',return_value={'mtime_ns':1,'size':1}):
            catalog.mark_success(s)
            self.assertTrue(catalog.inspect(s)['fresh'])
            with patch.object(catalog,'fingerprint',return_value={'mtime_ns':2,'size':1}):
                self.assertFalse(catalog.inspect(s)['fresh'])
