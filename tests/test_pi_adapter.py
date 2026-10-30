import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PiAdapterTests(unittest.TestCase):
    def test_prompt_stays_on_stdin_and_planner_has_read_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / 'pi'
            executable.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                                  'print(json.dumps({"argv":sys.argv[1:],"stdin":sys.stdin.read(),'
                                  '"offline":os.environ.get("PI_OFFLINE")}))\n')
            executable.chmod(0o755)
            env = dict(os.environ, PATH=temp + os.pathsep + os.environ['PATH'],
                       LLMBAN_PI_PROVIDER='test-provider', LLMBAN_PI_MODEL='test-model', PI_OFFLINE='1')
            result = subprocess.run([sys.executable, str(ROOT / 'bin' / 'pi_adapter.py'), '--role', 'planner'],
                                    input='private task content' * 10000, text=True, capture_output=True, check=True, env=env)
            data = json.loads(result.stdout)
            self.assertEqual(data['stdin'], 'private task content' * 10000)
            self.assertNotIn('private task content', ' '.join(data['argv']))
            self.assertEqual(data['argv'][data['argv'].index('--tools') + 1], 'read,grep,find,ls')
            self.assertIn('--no-extensions', data['argv'])
            self.assertEqual(data['offline'], '1')

    def test_missing_model_fails_without_starting_agent(self):
        env = dict(os.environ, LLMBAN_PI_PROVIDER='', LLMBAN_PI_MODEL='')
        result = subprocess.run([sys.executable, str(ROOT / 'bin' / 'pi_adapter.py'), '--role', 'doer'],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Set LLMBAN_PI_PROVIDER', result.stderr)
