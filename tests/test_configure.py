from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import json

ROOT = Path(__file__).resolve().parents[1]


class ConfigureTests(unittest.TestCase):
    def test_fresh_native_install_and_second_run_preserves_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'scripts').mkdir()
            (root / 'config').mkdir()
            for name in ['scripts/configure.py', '.env.example', 'config/agents.example.json']:
                shutil.copyfile(ROOT / name, root / name)
            command = [sys.executable, str(root / 'scripts/configure.py'), '--native']
            subprocess.run(command, check=True, capture_output=True)
            text = (root / '.env').read_text()
            password = next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('LLMBAN_PASSWORD='))
            self.assertGreaterEqual(len(password), 32)
            self.assertEqual(stat.S_IMODE((root / '.env').stat().st_mode), 0o600)
            config = json.loads((root / 'config' / 'agents.json').read_text())
            self.assertEqual(config['projects']['demo'], str(root / 'workspaces' / 'demo'))
            self.assertEqual(config['commands']['doer'][1], str(root / 'bin' / 'pi_adapter.py'))
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual((root / '.env').read_text(), text)
