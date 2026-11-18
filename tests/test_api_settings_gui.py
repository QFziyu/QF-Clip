import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from core.config import load_config

class APISettingsGUITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch('core.config.ROOT',self.root),patch.dict(os.environ,{k:v for k,v in os.environ.items() if k in ('USERPROFILE','HOMEDRIVE','HOMEPATH','SystemRoot','TEMP','TMP','PATH')},clear=True),patch('core.jobs.ensure_worker')]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def panel(self):
        return AppTest.from_string('from core.config import load_config\nfrom ui.llm_settings import panel\npanel(load_config())',default_timeout=20).run()
    def save(self):
        app=self.panel();app.text_input(key='llm-api-base').set_value('https://fixture.example/v1')
        app.text_input(key='llm-api-model').set_value('fixture-model');app.text_input(key='llm-api-key').set_value('fixture-ui-secret')
        next(b for b in app.button if b.label=='保存并使用此来源').click().run()
        self.assertFalse(app.exception);self.assertFalse(app.error)
        self.assertEqual(app.text_input(key='llm-api-key').value,'')
        self.assertEqual(load_config()['openai']['api_key'],'fixture-ui-secret')
        return app
    def test_save_reload_masks_key_and_retains_original_when_blank(self):
        app=self.save();app.text_input(key='llm-api-model').set_value('another-model')
        next(b for b in app.button if b.label=='保存并使用此来源').click().run()
        self.assertEqual(load_config()['openai']['model'],'another-model')
        self.assertEqual(load_config()['openai']['api_key'],'fixture-ui-secret')
        self.assertFalse((self.root/'.env').exists())
    def test_connection_test_success_and_secret_redaction(self):
        app=self.save()
        with patch('core.llm_client.LLMClient.complete',return_value={'ok':True}) as call:
            app.button(key='llm-test').click().run();self.assertEqual(call.call_count,1)
        self.assertTrue(any('连接成功' in s.value for s in app.success))
        with patch('core.llm_client.LLMClient.complete',side_effect=RuntimeError('denied fixture-ui-secret')):
            app.button(key='llm-test').click().run()
        self.assertTrue(any('连接测试失败' in s.value for s in app.error))
        self.assertFalse(any('fixture-ui-secret' in s.value for s in app.error))
    def test_workbench_has_direct_entry_without_api(self):
        source=Path(__file__).resolve().parents[1]/'ui/app.py'
        app=AppTest.from_file(str(source),default_timeout=25).run();self.assertFalse(app.exception)
        app.button(key='open-api-settings').click().run();self.assertFalse(app.exception)
        self.assertEqual(app.radio(key='wb-page').value,'API 设置')
        self.assertTrue(app.text_input(key='llm-api-base'))
        self.assertFalse(load_config()['openai']['api_key'])
