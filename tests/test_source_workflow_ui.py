"""Source input keeps the existing unique import/export flow."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from core.production import FactoryService
from core.config import load_config
from core.workflow import _item
from storage.workspaces import WorkspaceStore
import ui.source_workflow  # Load intake before temporarily patching its source client.


class SourceUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'DATABASE_PATH':str(self.root/'factory.sqlite3'),'OUTPUT_DIR':str(self.root/'out'),'CACHE_DIR':str(self.root/'cache')})
        self.env.start();self.worker=patch('core.jobs.ensure_worker',return_value=False);self.worker.start()
    def tearDown(self):self.worker.stop();self.env.stop();self.temp.cleanup()
    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).resolve().parents[1]/'ui/app.py'),default_timeout=20).run()
    def test_missing_service_is_actionable_and_manual_import_remains_available(self):
        app=self.app();next(r for r in app.radio if r.label=='来源方式').set_value('视频来源').run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(any('自动工作流尚未启动' in i.value for i in app.info))
        next(r for r in app.radio if r.label=='来源方式').set_value('本地文件').run()
        self.assertTrue(any(b.label=='打开文件…' for b in app.button))
    def test_source_form_uses_fixed_template_revision_and_has_no_export_action(self):
        templates=FactoryService(load_config()).templates()
        calls=[]
        def call(path,data=None):
            calls.append((path,data))
            if path=='health':return {'api_version':1}
            if path=='templates':return {'templates':templates}
            if path=='connections':return {'lightboat':{'browser_connected':False}}
            if path=='flows':return {'id':'flow'} if data else {'flows':[]}
        with patch('ui.source_workflow.AutomationClient') as client:
            client.return_value.call.side_effect=call
            app=self.app();next(r for r in app.radio if r.label=='来源方式').set_value('视频来源').run()
            self.assertEqual(len(app.exception),0)
            self.assertFalse(any(b.label in ('开始导出','完成本批审核并自动导出') for b in app.button))
            next(t for t in app.text_input if t.label=='视频来源网页').input('https://ymx168.top/live/1?recordId=2')
            next(b for b in app.button if b.label=='提交来源并自动生成候选').click().run()
            payload=next(data for path,data in calls if path=='flows' and data)
            self.assertEqual(payload['template_id'],'match-shorts');self.assertEqual(payload['template_revision'],1)
            self.assertIn('idempotency_key',payload)
            self.assertEqual(len(app.exception),0)

    def test_batch_confirmation_only_on_step5_uses_current_title_subtitle_and_template_render(self):
        cfg=load_config();source=self.root/'素材.mp4';source.touch()
        with WorkspaceStore(cfg['database']) as db:
            w=db.create_workspace('自动批次')
            item=_item('one','人工标题',[{'start':1.,'end':12.}],
                dict(id='p',source=str(source),duration=20.,metadata={},project_dir=str(self.root)),
                subtitle='1\n00:00:00,100 --> 00:00:01,000\n手工文字\n')
            item['status']='approved'
            w.update(step=5,automation={'flow_id':'test-flow'},active_variant='v',
                variants=[dict(id='v',name='一批',form='shorts',items=[item],events=[],options={})],
                export_settings={'orientations':['vertical'],'resolution':'原配置','output_dir':str(self.root/'成品'),
                    'render':{'horizontal':[640,360],'vertical':[360,640],'fps':25,'encoder':'libx264','bitrate':'900k'}})
            db.save_workspace(w);db.preference('current_workspace',w['id'])
        with patch('core.automation_client.AutomationClient') as client:
            app=self.app()
            next(r for r in app.radio if r.label=='导出范围').set_value('当前项目导出与发布').run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(next(s for s in app.selectbox if s.label=='编码器').value,'libx264')
            self.assertEqual(next(s for s in app.number_input if s.label=='帧率').value,25)
            self.assertFalse(any(b.label=='开始导出' for b in app.button))
            next(b for b in app.button if b.label=='完成本批审核并自动导出').click().run()
            path,data=client.return_value.call.call_args.args
            self.assertEqual(path,'flows/test-flow/confirm')
            self.assertEqual(data['snapshot']['render']['vertical'],[360,640])
            self.assertEqual(data['snapshot']['items'][0]['title'],'人工标题')
            self.assertIn('手工文字',data['snapshot']['items'][0]['subtitle_text'])


if __name__=='__main__':unittest.main()
