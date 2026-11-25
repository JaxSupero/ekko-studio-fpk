import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingPolicyTests(unittest.TestCase):
    def test_package_user_is_not_root(self):
        privilege = json.loads((ROOT / 'package/config/privilege').read_text())
        self.assertEqual(privilege['defaults']['run-as'], 'package')

    def test_desktop_entry_admin_only(self):
        config = json.loads((ROOT / 'package/app/ui/config').read_text())
        self.assertIs(config['.url']['ekko-studio.Application']['allUsers'], False)

    def test_no_broad_process_kill_commands(self):
        paths = list((ROOT / 'package/cmd').glob('*')) + list((ROOT / 'package/app/bin').glob('*'))
        for path in paths:
            if path.is_file():
                self.assertIsNone(re.search(r'\b(pkill|killall)\b', path.read_text()), path.name)

    def test_no_separate_hermes_tracking_or_installer(self):
        workflow = (ROOT / '.github/workflows/build-fpk.yml').read_text()
        self.assertNotIn('NousResearch', workflow)
        for path in (ROOT / 'package/cmd').glob('*'):
            if path.is_file():
                source = path.read_text()
                self.assertNotIn('npm install', source)
                self.assertNotIn('pip install', source)
                self.assertNotIn('hermes-agent-install', source)

    def test_no_forced_agent_selection(self):
        source = '\n'.join(path.read_text() for path in (ROOT / 'package').rglob('*')
                           if path.is_file() and path.suffix not in ('.png', '.PNG'))
        for name in ('HERMES_BIN', 'HERMES_AGENT_ROOT', 'HERMES_AGENT_CLI_PYTHON',
                     'HERMES_RUNTIME_SELECTION_LOCKED', 'HERMES_DESKTOP_RUNTIME_DIR'):
            self.assertIsNone(re.search(r'(?:export\s+|env\.)' + name + r'\s*=', source), name)


if __name__ == '__main__':
    unittest.main()
