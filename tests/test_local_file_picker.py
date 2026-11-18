import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from core.local_file_picker import (LocalPickerTask, cancel_local_picker, choose_video,
    poll_local_picker, resolve_video_path, start_local_picker)


class FakeProcess:
    def __init__(self, stdout='{"path":null}', returncode=0):
        self.stdout = stdout.encode('utf-8')
        self.returncode = returncode
        self.terminated = self.killed = False
    def poll(self): return self.returncode
    def communicate(self, timeout=None): return self.stdout, b''
    def terminate(self): self.terminated=True; self.returncode=-15
    def kill(self): self.killed=True; self.returncode=-9


class LocalFilePickerTests(unittest.TestCase):
    def test_unicode_quoted_path_and_missing_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / '上半场 中文 空格.mp4'
            source.write_bytes(b'source')
            self.assertEqual(resolve_video_path('"'+str(source)+'"'), str(source))
            self.assertEqual(resolve_video_path(str(source.with_suffix(''))), str(source))
            self.assertEqual(source.read_bytes(), b'source')
            source.with_suffix('.mkv').touch()
            with self.assertRaisesRegex(ValueError,'多个同名'):
                resolve_video_path(str(source.with_suffix('')))

    def test_native_choice_returns_existing_original_path(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'主播 中文.mp4'
            source.write_bytes(b'source')
            seen = {}
            def launch(command, **kwargs):
                seen['command'], seen['kwargs'] = command, kwargs
                request = Path(command[command.index('-RequestFile')+1])
                seen['request'] = json.loads(request.read_text(encoding='utf-8'))
                return FakeProcess(json.dumps({'path':str(source)}))
            with patch('core.local_file_picker.subprocess.Popen',side_effect=launch):
                self.assertEqual(choose_video('选择主播录像',str(source)),str(source))
                self.assertEqual(seen['request']['initial_dir'], str(source.parent.resolve()))
                self.assertEqual(seen['request']['title'], '选择主播录像')
                self.assertIn('-STA',seen['command'])
                self.assertEqual(Path(seen['command'][0]).name.lower(), 'powershell.exe')
                self.assertNotIn('-WindowStyle',seen['command'])
                self.assertEqual(seen['kwargs']['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertNotIn('shell',seen['kwargs'])
            self.assertEqual(list(Path(temp).iterdir()),[source])

    def test_cancel_failed_dialog_and_timeout(self):
        with patch('core.local_file_picker.subprocess.Popen',return_value=FakeProcess()):
            self.assertIsNone(choose_video())
        for response in (FakeProcess('{"error":"dialog unavailable"}',1), FakeProcess('bad JSON')):
            with patch('core.local_file_picker.subprocess.Popen',return_value=response),self.assertRaises(RuntimeError):
                choose_video()
        directory=tempfile.TemporaryDirectory()
        process=FakeProcess(returncode=None)
        task=LocalPickerTask(process,directory,Path(directory.name)/'result.json','video',False,started=time.monotonic()-601)
        with patch('core.local_file_picker.start_local_picker',return_value=task),self.assertRaisesRegex(RuntimeError,'超时'):
            choose_video()
        self.assertTrue(process.terminated)
        self.assertFalse(Path(directory.name).exists())

    def test_native_window_lifecycle_always_closes_on_cancel(self):
        from scripts.pick_video import pick_video
        with patch('scripts.pick_video._native_windows_choice',return_value={'path':None}) as dialog:
            self.assertEqual(pick_video('选择录像'),{'path':None})
            self.assertEqual(dialog.call_args.args,('选择录像',None,False,False,'video'))

    def test_async_pending_does_not_wait_and_cancellation_closes_only_owned_process(self):
        process=FakeProcess(returncode=None)
        with patch('core.local_file_picker.subprocess.Popen',return_value=process):
            task=start_local_picker('选择录像',multiple=True)
        directory=Path(task.directory.name)
        try:
            self.assertEqual(poll_local_picker(task),{'status':'pending','value':None})
            self.assertFalse(process.terminated)
            self.assertTrue(directory.exists())
            self.assertEqual(cancel_local_picker(task),{'status':'cancelled','value':None})
            self.assertTrue(process.terminated)
            self.assertFalse(directory.exists())
            self.assertEqual(poll_local_picker(task)['status'],'cancelled')
        finally:
            cancel_local_picker(task)

    def test_async_multi_selection_uses_utf8_result_file_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as temp:
            files=[Path(temp)/'2 下半场.mp4',Path(temp)/'1 上半场.mp4']
            for source in files:source.write_bytes(b'fixture')
            process=FakeProcess('wrong stdout',None)
            with patch('core.local_file_picker.subprocess.Popen',return_value=process):
                task=start_local_picker('中文多选',multiple=True)
            task.result_file.write_text(json.dumps({'paths':[str(p) for p in files]},ensure_ascii=False),encoding='utf-8')
            process.returncode=0
            result=poll_local_picker(task)
            self.assertEqual(result,{'status':'selected','value':[str(p.resolve()) for p in files]})
            self.assertFalse(Path(task.directory.name).exists())
            self.assertFalse(process.terminated)

    def test_start_failure_and_malformed_payload_are_actionable(self):
        with patch('core.local_file_picker.subprocess.Popen',side_effect=OSError('cannot start')),self.assertRaisesRegex(RuntimeError,'未能启动'):
            start_local_picker()
        with patch('core.local_file_picker.subprocess.Popen',return_value=FakeProcess('{"paths":"bad"}')):
            task=start_local_picker(multiple=True)
        self.assertEqual(poll_local_picker(task)['status'],'error')
        self.assertFalse(Path(task.directory.name).exists())

    def test_timer_closes_pending_process_when_browser_no_longer_polls(self):
        process=FakeProcess(returncode=None)
        with patch('core.local_file_picker.subprocess.Popen',return_value=process):
            task=start_local_picker()
        task.timer.function()
        self.assertTrue(process.terminated)
        self.assertEqual(task.result['status'],'error')
        self.assertIn('超时',task.result['error'])
        self.assertFalse(Path(task.directory.name).exists())

    def test_ui_callbacks_select_both_sources_and_create(self):
        from streamlit.testing.v1 import AppTest
        import os
        from storage.composites import CompositeStore
        from core.composite_options import normalize_composite_options
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);database=root/'db.sqlite3'
            match=root/'比赛.mp4';streamer=root/'主播.mkv';match.touch();streamer.touch()
            def create(a,b,cfg,*args):
                document={'id':'picker-created','match':{'source':a,'duration':7200},
                    'commentator':{'source':b,'duration':7300},'options':normalize_composite_options(),'outputs':[]}
                with CompositeStore(database) as db:db.insert_composite(document)
                return document
            with patch.dict(os.environ,{'DATABASE_PATH':str(database)}),patch('core.jobs.ensure_worker',return_value=False), \
                 patch('ui.composite_page.choose_video',side_effect=[str(match),str(streamer),None]), \
                 patch('ui.composite_page.create_composite',side_effect=create) as creator:
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'tests/fixtures/legacy_ui.py'),default_timeout=20).run()
                app.sidebar.radio[0].set_value('双路合成').run()
                def item(collection,label):return next(v for v in collection if v.label==label)
                item(app.button,'选择比赛录像').click().run()
                self.assertEqual(item(app.text_input,'比赛录像本地路径').value,str(match))
                item(app.button,'选择主播录像').click().run()
                self.assertEqual(item(app.text_input,'主播录像本地路径').value,str(streamer))
                item(app.button,'选择主播录像').click().run() # Cancel keeps previous choice.
                self.assertEqual(item(app.text_input,'主播录像本地路径').value,str(streamer))
                item(app.button,'创建合成项目').click().run()
                self.assertEqual(len(app.exception),0,str(app.exception))
                self.assertEqual(len(app.error),0,str(app.error))
                self.assertEqual(creator.call_args.args[:2],(str(match),str(streamer)))
                picker=next(s for s in app.selectbox if s.label=='合成项目')
                self.assertFalse(any('.mp4' in x or '.mkv' in x for x in picker.options))
