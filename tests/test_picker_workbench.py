"""Actual workbench callbacks remain usable while the native dialog is pending."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import load_config
from core.workflow import _item
from storage.workspaces import WorkspaceStore


class NonblockingPickerUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='文件窗口UI-')
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DATABASE_PATH':str(self.root/'db.sqlite3'),
            'OUTPUT_DIR':str(self.root/'out'), 'CACHE_DIR':str(self.root/'cache')})
        self.env.start()
        self.patchers = [patch('core.jobs.ensure_worker', return_value=False),
            patch('ui.file_picker.start_local_picker', return_value={'test_task':True}),
            patch('ui.file_picker.poll_local_picker', return_value={'status':'pending'}),
            patch('ui.file_picker.cancel_local_picker', return_value={'status':'cancelled','value':None}),
            patch('ui.workbench._thumb', return_value=None)]
        self.mocks = [p.start() for p in self.patchers]
        self.start, self.poll, self.cancel = self.mocks[1:4]

    def tearDown(self):
        for p in reversed(self.patchers): p.stop()
        self.env.stop(); self.temp.cleanup()

    @staticmethod
    def button(app, label): return next(button for button in app.button if button.label == label)

    def healthy(self, app):
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertEqual(len(app.error), 0, str(app.error))

    def app(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'ui/app.py'), default_timeout=15).run()
        self.healthy(app); return app

    def workspace(self, identity=None):
        with WorkspaceStore(load_config()['database']) as store:
            return store.get_workspace(identity or store.preference('current_workspace'))

    def source(self, name='中文 空格 录像.mp4'):
        path = self.root/name; path.write_bytes(b'original media fixture'); return path

    def select(self, app, value):
        self.poll.return_value = {'status':'selected','value':value}
        app.run(); self.healthy(app)
        self.poll.return_value = {'status':'pending'}
        return app

    def test_new_project_open_returns_pending_ui_and_blocks_duplicate_dialog(self):
        app = self.app()
        self.button(app, '＋ 新建项目').click().run(); self.healthy(app)
        self.button(app, '打开文件…').click().run(); self.healthy(app)
        self.assertTrue(any('等待选择' in item.value for item in app.info))
        self.assertTrue(any(button.label == '取消文件选择' for button in app.button))
        self.assertFalse(self.button(app, '02  选择剪辑方式').disabled)
        self.button(app, '打开文件…').click().run(); self.healthy(app)
        self.start.assert_called_once()
        self.button(app, '取消文件选择').click().run(); self.healthy(app)
        self.cancel.assert_called_once()
        self.assertEqual(self.workspace()['assets'], [])
        self.assertFalse(any(button.label == '取消文件选择' for button in app.button))

    def test_same_name_new_project_keeps_selection_through_file_roundtrip(self):
        app = self.app()
        original = self.workspace()['id']
        self.button(app, '＋ 新建项目').click().run(); self.healthy(app)
        current = self.workspace()['id']
        self.assertNotEqual(current, original)
        selector = next(item for item in app.selectbox if item.label == '当前项目')
        # Streamlit sends formatted labels over the browser wire. Duplicate
        # names must not deserialize to a different project's ID.
        self.assertEqual(len(selector.options), len(set(selector.options)))
        self.assertEqual(selector.value, current)
        self.button(app, '打开文件…').click().run(); self.healthy(app)
        self.assertEqual(self.workspace()['id'], current)
        self.assertEqual(self.start.call_count, 1)
        source = self.source()
        with patch('core.video_input.probe_video', return_value={'duration':25.,'width':320,'height':180}):
            self.select(app, [str(source)])
        self.assertEqual(self.workspace()['id'], current)
        self.assertEqual([asset['path'] for asset in self.workspace()['assets']], [str(source)])
        self.assertEqual(self.workspace(original)['assets'], [])

    def test_selected_multiple_paths_import_originals_in_order_without_copy(self):
        first, second = self.source('第一份 中文.mp4'), self.source('第二份 空格.mkv')
        app = self.app(); self.button(app, '打开文件…').click().run()
        with patch('core.video_input.probe_video', return_value={'duration':25.,'width':320,'height':180}):
            self.select(app, [str(first),str(second)])
        assets = self.workspace()['assets']
        self.assertEqual([a['path'] for a in assets], [str(first),str(second)])
        self.assertFalse(self.button(app, '下一步：选择剪辑方式').disabled)
        self.assertEqual(first.read_bytes(), b'original media fixture')
        self.assertFalse(list((self.root/'out').rglob('*.mp4')))
        before = copy.deepcopy(assets)
        self.button(app, '打开文件…').click().run()
        self.poll.return_value = {'status':'cancelled','value':None}
        app.run(); self.healthy(app)
        self.assertEqual(self.workspace()['assets'], before)

    def test_folder_choice_imports_supported_files_and_error_keeps_page_usable(self):
        first, second = self.source('01.mp4'), self.source('02.mkv')
        (self.root/'notes.txt').write_text('ignore')
        app = self.app(); self.button(app, '打开文件夹…').click().run()
        with patch('core.video_input.probe_video', return_value={'duration':25.,'width':320,'height':180}):
            self.select(app, str(self.root))
        self.assertEqual([a['path'] for a in self.workspace()['assets']], [str(first),str(second)])
        self.button(app, '打开文件…').click().run()
        self.poll.return_value = {'status':'error','error':'选择窗口启动失败，可重试'}
        app.run(); self.healthy(app)
        self.assertTrue(any('窗口启动失败' in item.value for item in app.info))
        self.assertFalse(self.button(app, '下一步：选择剪辑方式').disabled)

    def test_pending_selection_remains_bound_to_original_project_after_switch(self):
        source = self.source()
        app = self.app()
        original = self.workspace()['id']
        self.button(app, '打开文件…').click().run(); self.healthy(app)
        self.button(app, '＋ 新建项目').click().run(); self.healthy(app)
        current = self.workspace()['id']
        self.assertNotEqual(original, current)
        with patch('core.video_input.probe_video', return_value={'duration':25.,'width':320,'height':180}):
            self.select(app, [str(source)])
        self.assertEqual([a['path'] for a in self.workspace(original)['assets']], [str(source)])
        self.assertEqual(self.workspace(current)['assets'], [])

    def test_start_failure_is_actionable_and_does_not_block_retry(self):
        app = self.app()
        self.start.side_effect = RuntimeError('系统窗口启动失败')
        self.button(app, '打开文件…').click().run(); self.healthy(app)
        self.assertTrue(any('文件窗口启动失败' in message.value for message in app.info))
        self.assertFalse(any(button.label == '取消文件选择' for button in app.button))
        self.start.side_effect = None
        self.button(app, '打开文件…').click().run(); self.healthy(app)
        self.assertEqual(self.start.call_count, 2)
        self.assertTrue(any(button.label == '取消文件选择' for button in app.button))

    def test_srt_and_output_directory_are_applied_before_existing_widgets(self):
        source = self.source()
        cfg = load_config()
        with WorkspaceStore(cfg['database']) as store:
            w = store.create_workspace('字幕文件检查')
            item = _item('i1','成品',[{'start':0.,'end':5.}],
                dict(id='p',source=str(source),duration=5.,metadata={},project_dir=str(self.root)),
                subtitle='1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n',status='approved')
            w.update(step=4,active_variant='v',variants=[dict(id='v',name='方案',form='full',items=[item],events=[],options={})])
            store.save_workspace(w); store.preference('current_workspace',w['id'])
        srt = self.root/'人工 字幕.srt'
        srt.write_text('1\n00:00:00,000 --> 00:00:02,000\n手工校验字幕\n',encoding='utf-8-sig')
        app = self.app(); self.button(app, '打开 SRT…').click().run()
        self.select(app, str(srt))
        text = next(t for t in app.text_area if t.label.startswith('字幕文字与时间'))
        self.assertIn('手工校验字幕', text.value)
        self.button(app, '保存此条人工字幕').click().run(); self.healthy(app)
        self.assertIn('手工校验字幕', self.workspace(w['id'])['variants'][0]['items'][0]['subtitle_text'])
        self.button(app,'05  统一导出').click().run(); self.healthy(app)
        self.button(app,'选择输出文件夹…').click().run()
        output = self.root/'自选 输出'; output.mkdir()
        self.select(app,str(output))
        self.assertEqual(app.session_state['global-export-folder'],str(output))


if __name__ == '__main__': unittest.main()
