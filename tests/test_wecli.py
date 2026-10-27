import subprocess
import unittest
from unittest.mock import patch

from wereply.core import Wecli


class WecliTests(unittest.TestCase):
    def test_missing_module_explained_without_stdout(self):
        result = subprocess.CompletedProcess([], 1, 'private chat content',
                                             '/env/python: No module named wecli\n')
        with patch('wereply.core.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'No module named wecli') as exc:
                Wecli('/env/python -m wecli').call('doctor')
        self.assertNotIn('private chat content', str(exc.exception))

    def test_nonzero_doctor_with_valid_json_accepted(self):
        result = subprocess.CompletedProcess([], 1, '{"read_available":true}', '')
        with patch('wereply.core.subprocess.run', return_value=result):
            self.assertTrue(Wecli('wecli').call('doctor', allow_failure=True)['read_available'])

    def test_missing_executable(self):
        with patch('wereply.core.subprocess.run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, '找不到 wecli 命令'):
                Wecli('/missing/wecli').call('doctor')

    def test_timeout(self):
        with patch('wereply.core.subprocess.run', side_effect=subprocess.TimeoutExpired('wecli', 120)):
            with self.assertRaisesRegex(RuntimeError, '超过 120 秒'):
                Wecli('wecli').call('doctor')

    def test_empty_command(self):
        with self.assertRaises(ValueError):
            Wecli(' ')

    def test_expand_executable_home(self):
        with patch('wereply.core.os.path.expanduser', return_value='/home/test/bin/python'):
            self.assertEqual(Wecli('~/bin/python -m wecli').command[0], '/home/test/bin/python')

    def test_bare_conda_python_adds_module(self):
        self.assertEqual(Wecli('/env/bin/python3.11').command, ['/env/bin/python3.11','-m','wecli'])
        self.assertEqual(Wecli('/env/bin/python -m wecli').command, ['/env/bin/python','-m','wecli'])

    def test_contact_rejects_fuzzy_match(self):
        client = Wecli('wecli')
        with patch.object(client, 'call', return_value={'username':'wxid_other'}):
            with self.assertRaisesRegex(ValueError, '搜索并选择'):
                client.contact('alias')

    def test_contact_accepts_exact_id(self):
        client = Wecli('wecli')
        with patch.object(client, 'call', return_value={'username':'wxid_friend'}):
            self.assertEqual(client.contact('wxid_friend')['username'], 'wxid_friend')
