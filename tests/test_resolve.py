import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('resolve', ROOT / 'scripts/resolve.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def release(version='0.8.31'):
    tag = 'v' + version
    names = ['hermes-web-ui-' + version + suffix for suffix in ('.tar.gz', '.json', '.tar.gz.sha256')]
    return {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': [
        {'name': name, 'state': 'uploaded', 'size': 100, 'digest': 'sha256:' + 'a' * 64,
         'browser_download_url': 'https://github.com/EKKOLearnAI/ekko-studio/releases/download/' + tag + '/' + name}
        for name in names]}


class ResolveTests(unittest.TestCase):
    def test_semantic_order_not_publication_date_or_latest_flag(self):
        releases = [release('0.9.1'), release('0.10.0'), release('0.8.31')]
        self.assertEqual(module.select_release(releases)['tag_name'], 'v0.10.0')

    def test_ignore_prerelease_and_draft(self):
        preview, draft = release('9.0.0'), release('8.0.0')
        preview['prerelease'], draft['draft'] = True, True
        self.assertEqual(module.select_release([preview, draft, release()])['tag_name'], 'v0.8.31')

    def test_no_matching_stable_version_fails(self):
        with self.assertRaises(ValueError):
            module.select_release([{'tag_name': 'nightly'}])

    def test_required_uploaded_asset(self):
        data = release()
        data['assets'][0]['state'] = 'new'
        with self.assertRaises(ValueError):
            module.asset_metadata(data, data['assets'][0]['name'])

    def test_missing_digest_fails_closed(self):
        data = release()
        del data['assets'][0]['digest']
        with self.assertRaises(ValueError):
            module.asset_metadata(data, data['assets'][0]['name'])

    def test_unexpected_download_host_fails_closed(self):
        data = release()
        data['assets'][0]['browser_download_url'] = 'https://example.invalid/asset'
        with self.assertRaises(ValueError):
            module.asset_metadata(data, data['assets'][0]['name'])

    @patch.object(module, 'request', return_value=b"node-version: '24.22.1'\n")
    @patch.object(module, 'api', return_value={'sha': 'b' * 40})
    def test_immutable_metadata_and_upstream_node_version(self, api, request):
        data = module.resolve(release(), 2)
        self.assertEqual(data['version'], '0.8.31-2')
        self.assertEqual(data['node_version'], '24.22.1')
        self.assertEqual(data['source_commit'], 'b' * 40)
        self.assertNotIn('hermes_agent_version', data)

    @patch.object(module, 'request', return_value=b"node-version: '24'\n")
    @patch.object(module, 'api', return_value={'sha': 'b' * 40})
    def test_unpinned_upstream_node_version_requires_review(self, api, request):
        with self.assertRaises(ValueError):
            module.resolve(release(), 1)


if __name__ == '__main__':
    unittest.main()
