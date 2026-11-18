import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.composite_options import normalize_composite_options
from storage.composites import CompositeStore


class CompositeUITests(unittest.TestCase):
    def test_save_manual_anchors_and_queue_preview_snapshot(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as temp:
            database = str(Path(temp)/'db.sqlite3')
            with CompositeStore(database) as db:
                db.insert_composite({'id': 'ui-dual', 'match': {'source': '比赛.mp4', 'duration': 7200},
                    'commentator': {'source': '主播.mp4', 'duration': 10800}, 'options': normalize_composite_options(), 'outputs': [],
                    'sync': {'confidence':'high','offset_seconds':500.,'note':'测试证据'}})
            with patch.dict(os.environ, {'DATABASE_PATH':database}), patch('core.jobs.ensure_worker', return_value=False), patch('ui.composite_page.submit_job', return_value={'id':'testjob'}) as queue:
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'tests/fixtures/legacy_ui.py'), default_timeout=20).run()
                app.sidebar.radio[0].set_value('双路合成').run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(len(app.error), 0)
                def item(collection, label):
                    return next(v for v in collection if v.label == label)
                item(app.number_input, '事件在比赛录像中的秒数').set_value(100.)
                item(app.number_input, '事件在主播录像中的秒数').set_value(400.)
                item(app.checkbox, '本次保存时使用两个事件位置计算偏移').check()
                item(app.button, '保存合成设置').click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(len(app.error), 0)
                with CompositeStore(database) as db:
                    self.assertEqual(db.get_composite('ui-dual')['options']['offset_seconds'], 300.)
                item(app.button, '生成短预览').click().run()
                self.assertEqual(queue.call_args.args[0], 'compose_render')
                self.assertEqual(queue.call_args.args[1]['options']['offset_seconds'], 300.)
                self.assertTrue(queue.call_args.args[1]['preview'])
                item(app.button, '采用建议偏移（仍需预览）').click().run()
                self.assertEqual(item(app.number_input,'同步偏移（秒，可正可负）').value, 500.)
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(len(app.error), 0)

    def test_empty_workspace(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'DATABASE_PATH': str(Path(temp)/'db.sqlite3')}), patch('core.jobs.ensure_worker',return_value=False):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'tests/fixtures/legacy_ui.py'), default_timeout=20).run()
            app.sidebar.radio[0].set_value('双路合成').run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(len(app.error),0)
            self.assertTrue(any('添加比赛录像' in v.value for v in app.info))
