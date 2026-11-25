import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_resolve import module, release


class ReleaseNotesTests(unittest.TestCase):
    def test_body_is_preserved_as_data(self):
        body = '# 更新\r\n\n```sh\n$(touch /tmp/never-run)\n${{ secrets.GITHUB_TOKEN }}\n```\n  '
        data = release()
        data['body'] = body
        notes = module.release_notes(data)
        self.assertTrue(notes.startswith(body + '\n\n---\n'))
        self.assertIn('https://github.com/EKKOLearnAI/ekko-studio/releases/tag/v0.8.31', notes)
        self.assertIn('admin / 123456', notes)
        self.assertIn('不单独锁定、安装或自动更新 Hermes Agent', notes)
        self.assertEqual(module.release_notes(data), notes)

    def test_missing_null_empty_and_whitespace_body_fallback(self):
        for body in (None, '', ' \n\t'):
            data = release()
            data['body'] = body
            self.assertIn('上游未提供此版本的更新日志', module.release_notes(data))
        self.assertIn('上游未提供此版本的更新日志', module.release_notes(release()))

    def run_main(self, explicit=False, existing=False):
        selected, older, android = release(), release('0.8.30'), release('0.8.27')
        selected['body'], older['body'] = '# SELECTED\n原版更新', '# WRONG older'
        android['tag_name'], android['body'] = 'v1.1.0', '# WRONG Android'
        def api(path):
            if '/commits/' in path:
                return {'sha': 'b' * 40}
            if path.startswith('repos/wrapper/'):
                return {'assets': [{'name': n, 'state': 'uploaded'} for n in (
                    'ekko-studio_v0.8.31-1_x86_64.fpk', 'ekko-studio_v0.8.31-1_x86_64.fpk.sha256')]}
            if '/releases/tags/' in path:
                return selected
            return [android, older, selected]
        with tempfile.TemporaryDirectory() as tmp:
            metadata = Path(tmp) / 'metadata.json'
            notes = Path(tmp) / 'upstream-release-notes.md'
            args = ['resolve.py', '--output', str(metadata), '--notes-output', str(notes)]
            if explicit:
                args += ['--tag', 'v0.8.31']
            if existing:
                args += ['--check-existing']
            with patch('sys.argv', args), patch.object(module, 'api', side_effect=api), \
                    patch.object(module, 'request', return_value=b"node-version: '24.22.1'"), \
                    patch.dict(os.environ, {'GITHUB_REPOSITORY': 'wrapper/repo', 'GITHUB_OUTPUT': ''}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                module.main()
            self.assertEqual(json.loads(metadata.read_text())['tag'], 'v0.8.31')
            self.assertNotIn('body', json.loads(metadata.read_text()))
            self.assertTrue(notes.read_text().startswith(selected['body'] + '\n\n---'))
            self.assertNotIn('WRONG', notes.read_text())
            self.assertEqual(json.loads(output.getvalue())['should_build'], not existing)

    def test_auto_selection_notes_match_assets(self):
        self.run_main()

    def test_explicit_selection_notes_match_assets(self):
        self.run_main(explicit=True)

    def test_completed_release_still_skips_scheduled_build(self):
        self.run_main(existing=True)
