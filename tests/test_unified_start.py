import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.releases import current_environment
from core.version import VERSION
from scripts.start import launch_command


class UnifiedStartTests(unittest.TestCase):
    def test_repeated_launch_reuses_verified_current_fallback_port(self):
        from scripts.launch import existing_current_port
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state/'final-8503.json').touch()
            with patch('scripts.launch.owned_server',return_value=True) as owned:
                self.assertEqual(existing_current_port(state,state,8501,'final'),8503)
                owned.assert_called_once_with(state/'final-8503.json',state,8503,'final')
            with patch('scripts.launch.owned_server',return_value=False):
                self.assertIsNone(existing_current_port(state,state,8501,'final'))

    def test_both_devices_use_current_source_and_separate_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'factory'
            root.mkdir()
            for profile, device in [('cpu', 'cpu'), ('gpu', 'cuda')]:
                code, env = current_environment(root, profile, {})
                self.assertEqual(code, root.resolve())
                self.assertEqual(env['DEVICE'], device)
                self.assertEqual(env['FCF_RELEASE_VERSION'], f'v{VERSION}-{profile}')
                if profile == 'cpu':
                    self.assertEqual(env['FCF_RENDER_ENCODER'], 'libx264')
                    self.assertEqual(Path(env['OUTPUT_DIR']), root/'runtime-cpu/output')

    def test_cpu_reuses_existing_reviews_from_latest_cpu_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'factory'
            root.mkdir()
            db = Path(tmp)/'factory_Releases/release-data/v1.7.1-cpu/output/factory.sqlite3'
            db.parent.mkdir(parents=True)
            db.write_bytes(b'preserve')
            code, env = current_environment(root, 'cpu', {})
            self.assertEqual(code, root.resolve())
            self.assertEqual(Path(env['DATABASE_PATH']), db)
            self.assertEqual(db.read_bytes(), b'preserve')

    def test_four_choices_select_expected_service_and_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'factory'
            for profile in ['cpu', 'gpu']:
                for publishing in [False, True]:
                    with patch('scripts.start.ROOT', root):
                        cmd = launch_command(profile, publishing, True)
                    self.assertEqual(cmd[cmd.index('--profile')+1], profile)
                    self.assertEqual(Path(cmd[1]).name, 'launch_publishing.py' if publishing else 'launch.py')
                    self.assertNotIn('--version', cmd)
                    self.assertIn('--no-browser', cmd)
            for filename in ['FootballWorkflow/main.py', '软件下载/轻舟视频下载/server.py']:
                file = root.parent/filename
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch()
            with patch('scripts.start.ROOT', root):
                self.assertNotIn('--publishing', launch_command('cpu'))
                self.assertIn('--publishing', launch_command('cpu', True))


if __name__ == '__main__':
    unittest.main()
