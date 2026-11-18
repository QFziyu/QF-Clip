from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.launch_automation import health_matches, lightboat_command_matches, start_service
from scripts.start import main


class LaunchServiceTests(unittest.TestCase):
    def test_same_api_newer_lightboat_is_compatible_but_factory_is_exact(self):
        health = dict(service='lightboat', api_version=1, version='1.2.1')
        self.assertTrue(health_matches(health,'lightboat',version='1.2.0',compatible=True))
        for version in ['1.1.9','2.0.0','broken']:
            self.assertFalse(health_matches({**health,'version':version},'lightboat',version='1.2.0',compatible=True))
        self.assertFalse(health_matches({**health,'api_version':2},'lightboat',version='1.2.0',compatible=True))
        self.assertFalse(health_matches(dict(service='football-factory',api_version=1,version='1.8.3',profile='gpu'),'football-factory','gpu','1.8.4'))

    def test_independent_lightboat_command_flags_and_explicit_port(self):
        script = Path.cwd()/'lightboat/server.py'
        self.assertTrue(lightboat_command_matches(['python.exe','-B','-u',str(script),'--no-browser','--port','17653'],script,17653))
        self.assertTrue(lightboat_command_matches(['python.exe',str(script),'--no-browser'],script,17653))
        for command in [
            ['python.exe',str(script.parent/'other.py'),'--port','17653'],
            ['python.exe','server.py','--port','17653'],
            ['python.exe',str(script),'--port','9999'],
            ['python.exe','-c',str(script)],
            ['other.exe',str(script)],
            ['python.exe',str(script),'--unexpected'],
        ]:
            self.assertFalse(lightboat_command_matches(command,script,17653),command)

    def test_realistic_stale_record_reuses_external_121_without_taking_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            script=state/'server.py'
            record=state/'lightboat.json'
            record.write_text('{"pid": 123, "command": ["stale"]}')
            original=record.read_bytes()
            actual=['python.exe','-B','-u',str(script),'--no-browser','--port','17653']
            with patch('scripts.launch_automation.port_available',return_value=False), patch('scripts.launch_automation.listener_pids',return_value={456}), patch('scripts.launch_automation.process_command',return_value=actual), patch('scripts.launch_automation.read_health',return_value=dict(service='lightboat',api_version=1,version='1.2.1')), patch('scripts.launch_automation.subprocess.Popen') as spawn:
                start_service('lightboat',['python.exe',str(script),'--no-browser'],state,{},dict(port=17653),state,'lightboat',expected_version='1.2.0')
            spawn.assert_not_called()
            self.assertEqual(record.read_bytes(),original)

    def test_wrong_process_or_failed_authentication_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            command=['python.exe',str(state/'server.py'),'--no-browser']
            common=[patch('scripts.launch_automation.port_available',return_value=False),patch('scripts.launch_automation.listener_pids',return_value={456})]
            for p in common:p.start()
            try:
                with patch('scripts.launch_automation.process_command',return_value=['python.exe',str(state/'foreign.py')]), patch('scripts.launch_automation.read_health') as health:
                    with self.assertRaises(RuntimeError):
                        start_service('lightboat',command,state,{},dict(port=17653),state,'lightboat',expected_version='1.2.0')
                    health.assert_not_called()
                with patch('scripts.launch_automation.process_command',return_value=command), patch('scripts.launch_automation.read_health',side_effect=OSError('authentication failed')):
                    with self.assertRaises(OSError):
                        start_service('lightboat',command,state,{},dict(port=17653),state,'lightboat',expected_version='1.2.0')
            finally:
                for p in reversed(common):p.stop()

    def test_all_four_choices_open_workbench_if_auxiliary_start_fails(self):
        for profile in ['cpu','gpu']:
            for publishing in [False,True]:
                args=['--profile',profile,'--no-browser']+(['--publishing'] if publishing else [])
                with patch('scripts.start.launch_command',return_value=['python.exe','launch_automation.py','--profile',profile]), patch('scripts.start.subprocess.run',side_effect=[SimpleNamespace(returncode=1),SimpleNamespace(returncode=0)]) as run:
                    self.assertEqual(main(args),0)
                    fallback=run.call_args_list[1].args[0]
                    self.assertEqual(Path(fallback[1]).name,'launch.py')
                    self.assertEqual(fallback[fallback.index('--profile')+1],profile)
                    self.assertIn('--no-browser',fallback)


if __name__=='__main__':unittest.main()
