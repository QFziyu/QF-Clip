"""Workbench migration, guarded editing, mapping and the real production shell."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import load_config
from core.workflow import (adopt_legacy, active_variant, edit_item, map_transcript, export_snapshot,
                           prepare_workflow, run_workflow_export, _item)
from storage.workspaces import WorkspaceStore


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='工作台-')
        self.root=Path(self.temp.name)
        self.cfg=load_config()
        self.cfg.update(database=str(self.root/'db.sqlite3'),cache_dir=str(self.root/'cache'),output_dir=str(self.root/'out'))
        self.source=self.root/'中文 原片.mp4'; self.source.touch()
        self.project=dict(id='legacy-p',source=str(self.source),duration=180,name='测试项目',metadata={},project_dir=str(self.root/'cache'))
        with WorkspaceStore(self.cfg['database']) as store:
            store.upsert_project(self.project)
            store.upsert_clips('legacy-p',[dict(id='c1',start=10,end=40,title='原标题',score=80,tags=[])])
            store.review_clip('c1','approved',title='人工标题',subtitle_text='1\n00:00:00,000 --> 00:00:01,000\n人工字幕\n')
        adopt_legacy(self.cfg)

    def tearDown(self): self.temp.cleanup()

    def workspace(self):
        with WorkspaceStore(self.cfg['database']) as store: return store.get_workspace('legacy-p')

    def test_migration_is_additive_idempotent_and_retains_human_data(self):
        adopt_legacy(self.cfg)
        with WorkspaceStore(self.cfg['database']) as store:
            self.assertEqual(len(store.list_workspaces()),1)
            self.assertEqual(len(store.review_history('c1')),1)
            self.assertEqual(store.get_clip('c1')['title'],'人工标题')
        item=active_variant(self.workspace())['items'][0]
        self.assertEqual(item['id'],'c1');self.assertEqual(item['title'],'人工标题')
        self.assertIn('人工字幕',item['subtitle_text'])

    def test_range_edit_preserves_manual_captions_and_blocks_export_until_confirmed(self):
        edit_item('legacy-p','c1',{'segments':[{'start':11.,'end':40.}]},self.cfg)
        w=self.workspace();item=active_variant(w)['items'][0]
        self.assertTrue(item['subtitle_stale']);self.assertIn('人工字幕',item['subtitle_text'])
        with self.assertRaisesRegex(ValueError,'核对'):
            export_snapshot(w,self.cfg,['horizontal'],{},str(self.root/'out'))
        edit_item('legacy-p','c1',{'subtitle_text':item['subtitle_text']},self.cfg)
        snapshot=export_snapshot(self.workspace(),self.cfg,['horizontal'],{},str(self.root/'out'))
        self.assertFalse(snapshot['items'][0]['subtitle_stale'])

    def test_optimistic_workspace_edit_cannot_overwrite_parallel_change(self):
        with WorkspaceStore(self.cfg['database']) as store:
            before=store.get_workspace('legacy-p');other=copy.deepcopy(before)
            before['step']=4;store.save_workspace(before)
            with self.assertRaisesRegex(ValueError,'刷新'):store.save_workspace(other)
        self.assertEqual(self.workspace()['step'],4)

    def test_queue_export_checks_saved_revision_before_rendering(self):
        snapshot=export_snapshot(self.workspace(),self.cfg,['horizontal'],{},str(self.root/'out'))
        edit_item('legacy-p','c1',{'title':'提交之后新标题'},self.cfg)
        with patch('core.workflow_renderer.render_workflow_item') as render:
            with self.assertRaisesRegex(ValueError,'上游内容已修改'):
                run_workflow_export('legacy-p',snapshot,self.cfg)
            render.assert_not_called()

    def test_preview_export_history_records_do_not_overwrite_old_files(self):
        with WorkspaceStore(self.cfg['database']) as store:
            for i in range(2):store.record_workflow_media('legacy-p','c1',{'revision':i},{'video':f'{i}.mp4'})
            self.assertEqual(len(store.list_media('legacy-p',False)),2)

    def test_legacy_plan_with_string_preview_and_interrupted_import_recovers(self):
        from storage.plans import PlanStore
        with PlanStore(self.cfg['database']) as plans:
            plans.upsert_project({**self.project,'id':'older-plan-project'})
            plans.insert_plan(dict(id='older-plan',project_id='older-plan-project',transcript_key='key',
                options={'format':'montage'},events=[],drafts=[dict(id='d1',title='旧集锦',segments=[{'start':1.,'end':5.}],
                status='approved',subtitle_text='人工字幕',preview='old-preview.mp4')]))
        with WorkspaceStore(self.cfg['database']) as store:store.create_workspace('中断的导入','older-plan-project')
        adopt_legacy(self.cfg)
        with WorkspaceStore(self.cfg['database']) as store:
            w=store.get_workspace('older-plan-project')
            self.assertEqual(w['variants'][0]['items'][0]['legacy_preview'],'old-preview.mp4')
            self.assertEqual(w['variants'][0]['items'][0]['subtitle_text'],'人工字幕')
        adopt_legacy(self.cfg)
        with WorkspaceStore(self.cfg['database']) as store:self.assertEqual(len(store.get_workspace('older-plan-project')['variants']),1)

    def test_dual_mapping_handles_both_offset_signs_words_and_overlap(self):
        raw={'segments':[{'start':10.,'end':14.,'text':'进球反应','words':[{'start':11.,'end':12.,'word':'进球'}]},
                         {'start':50.,'end':52.,'text':'范围外'}]}
        mapped=map_transcript(raw,8.,{'start':3.,'end':40.})
        self.assertEqual(len(mapped['segments']),1)
        self.assertEqual(mapped['segments'][0]['start'],3.)
        self.assertEqual(mapped['segments'][0]['words'][0]['start'],3.)
        self.assertEqual(map_transcript(raw,-5.,{'start':5.,'end':100.})['segments'][0]['start'],15.)
        self.assertEqual(raw['segments'][0]['start'],10.)


class WorkbenchUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='wb-ui-')
        self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'DATABASE_PATH':str(self.root/'db.sqlite3'),'CACHE_DIR':str(self.root/'cache'),'OUTPUT_DIR':str(self.root/'out')})
        self.env.start();self.worker=patch('core.jobs.ensure_worker',return_value=False);self.worker.start()

    def tearDown(self):self.worker.stop();self.env.stop();self.temp.cleanup()

    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(Path(__file__).resolve().parents[1]/'ui/app.py'),default_timeout=20).run()

    def button(self,app,label):return next(b for b in app.button if b.label==label)
    def healthy(self,app):
        self.assertEqual(len(app.exception),0,str(app.exception));self.assertEqual(len(app.error),0,str(app.error))

    def test_unique_import_export_routes_and_browser_restart_recovers_stage(self):
        app=self.app();self.healthy(app)
        self.assertTrue(any(b.label=='打开文件…' for b in app.button))
        self.assertFalse(any(b.label=='开始导出' for b in app.button))
        self.button(app,'04  字幕与包装').click().run();self.healthy(app)
        self.assertTrue(any(h.value=='字幕与包装' for h in app.header))
        restarted=self.app();self.healthy(restarted)
        self.assertTrue(any(h.value=='字幕与包装' for h in restarted.header))
        self.assertFalse(any(b.label=='打开文件…' for b in restarted.button))

    def test_picker_cancel_keeps_selection_and_no_path_needed(self):
        app=self.app()
        with patch('ui.file_picker.start_local_picker',return_value=object()) as picker, \
             patch('ui.file_picker.poll_local_picker',return_value={'status':'cancelled','value':None}):
            self.button(app,'打开文件…').click().run()
        self.healthy(app);picker.assert_called_once()
        with WorkspaceStore(load_config()['database']) as store:self.assertEqual(store.list_workspaces()[0]['assets'],[])
        self.assertTrue(self.button(app,'下一步：选择剪辑方式').disabled)

    def seed(self):
        cfg=load_config(); source=self.root/'中文 文件.mp4';source.touch()
        with WorkspaceStore(cfg['database']) as store:
            w=store.create_workspace('UI 测试')
            item=_item('item-1','旧标题',[{'start':1.,'end':12.}],dict(id='p',source=str(source),duration=20.,metadata={},project_dir=str(self.root)))
            w.update(step=3,assets=[dict(id='a',path=str(source),name=source.name,duration=20.,meta={})],
                     active_variant='v',variants=[dict(id='v',name='方案一',items=[item],form='shorts',events=[],warnings=[])])
            store.save_workspace(w);store.preference('current_workspace',w['id'])
            return w['id']

    def test_review_title_persists_and_final_export_only_on_step5(self):
        identity=self.seed();app=self.app();self.healthy(app)
        next(i for i in app.text_input if i.label=='标题').input('我修改的标题')
        next(r for r in app.radio if r.label=='审批决定').set_value('approved')
        self.button(app,'保存审核').click().run();self.healthy(app)
        with WorkspaceStore(load_config()['database']) as store:
            item=active_variant(store.get_workspace(identity))['items'][0]
            self.assertEqual(item['title'],'我修改的标题');self.assertEqual(item['status'],'approved')
        self.assertFalse(any('开始导出'==b.label for b in app.button))
        self.button(app,'下一步：字幕与包装').click().run();self.healthy(app)
        next(i for i in app.checkbox if i.label=='为本批成品添加字幕').uncheck().run();self.healthy(app)
        self.button(app,'保存包装并前往导出').click().run();self.healthy(app)
        self.assertFalse(self.button(app,'开始导出').disabled)
        with patch('core.jobs.submit_job',return_value={'id':'queued123'}) as queue:
            self.button(app,'开始导出').click().run()
        self.healthy(app);self.assertEqual(queue.call_args.args[0],'export_batch')
        self.assertEqual(queue.call_args.args[1]['plan']['groups'][0]['snapshot']['items'][0]['title'],'我修改的标题')

    def test_review_next_saves_rating_title_and_keeps_preview(self):
        from ui.workbench import _media
        from core.utils import fingerprint
        identity=self.seed();cfg=load_config();preview=self.root/'preview.mp4';preview.write_bytes(b'preview')
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity);first=active_variant(w)['items'][0];first['score']=81
            second=copy.deepcopy(first);second.update(id='item-2',title='第二条')
            active_variant(w)['items'].append(second);store.save_workspace(w)
            store.record_workflow_media(identity,first['id'],{'item':copy.deepcopy(first)},
                {'video':str(preview),'preview_complete':True,'packaging_signature':fingerprint({}),'item_signature':fingerprint(first)},preview=True)
        app=self.app();self.healthy(app)
        self.assertTrue(any(m.value=='81 / 100' for m in app.metric))
        next(t for t in app.text_input if t.label=='标题').input('修改后标题')
        next(t for t in app.selectbox if t.label=='我的评分').set_value(4)
        self.button(app,'下一条').click().run();self.healthy(app)
        self.assertEqual(next(t for t in app.text_input if t.label=='标题').value,'第二条')
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity);first=active_variant(w)['items'][0]
            self.assertEqual(first['title'],'修改后标题');self.assertEqual(first['user_rating'],4)
            self.assertEqual(first['status'],'pending')
            self.assertEqual(_media(w,first,cfg)[0],str(preview))
        self.button(app,'上一条').click().run();self.healthy(app)
        self.assertEqual(next(t for t in app.text_input if t.label=='标题').value,'修改后标题')

    def test_caption_replace_persists_after_browser_restart(self):
        identity=self.seed();cfg=load_config()
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity);w['step']=3
            w['packaging']={'subtitles':True,'proofread_subtitles':False}
            first=active_variant(w)['items'][0]
            first['subtitle_text']='1\n00:00:01,000 --> 00:00:02,000\n首条字幕保持原样\n'
            second=copy.deepcopy(first)
            second.update(id='item-2',title='第二条字幕',subtitle_text='1\n00:00:01,000 --> 00:00:02,000\n洛桑传球<unk>\n')
            active_variant(w)['items'].append(second)
            store.save_workspace(w)
        app=self.app();self.healthy(app)
        next(r for r in app.radio if r.label=='成品列表').set_value('item-2').run()
        self.button(app,'修改字幕').click().run();self.healthy(app)
        self.assertEqual(next(s for s in app.selectbox if s.label=='逐条编辑字幕').value,'item-2')
        self.button(app,'查找与替换').click().run()
        next(t for t in app.text_input if t.label=='查找文字').input('洛桑')
        next(t for t in app.text_input if t.label=='替换为').input('胡桑诺夫').run()
        self.button(app,'全部替换').click().run();self.healthy(app)
        self.button(app,'清理识别标记').click().run();self.healthy(app)
        self.button(app,'保存此条人工字幕').click().run();self.healthy(app)
        with WorkspaceStore(cfg['database']) as store:
            items=active_variant(store.get_workspace(identity))['items']
            saved=items[1]['subtitle_text']
            self.assertIn('首条字幕保持原样',items[0]['subtitle_text'])
        self.assertIn('胡桑诺夫传球',saved);self.assertNotIn('<unk>',saved)
        fresh=self.app();self.healthy(fresh)
        next(s for s in fresh.selectbox if s.label=='逐条编辑字幕').set_value('item-2').run()
        self.assertEqual(next(t for t in fresh.text_area if t.label.startswith('字幕文字')).value,saved)

    def test_multiselection_order_and_modes_have_no_new_raw_import_controls(self):
        self.seed();app=self.app();self.button(app,'02  选择剪辑方式').click().run();self.healthy(app)
        self.assertFalse(any(b.label=='打开文件…' for b in app.button))
        self.assertEqual(len([i for i in app.selectbox if i.label=='成品形式']),1)
        next(i for i in app.selectbox if i.label=='成品形式').set_value('montage').run();self.healthy(app)
        next(i for i in app.selectbox if i.label=='成品形式').set_value('full').run();self.healthy(app)
        self.assertFalse(any(b.label=='开始导出' for b in app.button))

    def test_batch_montage_range_defaults_to_combined_and_can_be_saved_separately(self):
        identity=self.seed();cfg=load_config()
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity)
            w['assets'].append(dict(id='b',path=str(self.root/'第二份.mp4'),name='第二份.mp4',duration=20.,meta={}))
            w.update(step=2,source_options={'organization':'single','form':'montage','asset_ids':['a','b']})
            store.save_workspace(w)
        app=self.app();self.healthy(app)
        scope=next(r for r in app.radio if r.label=='多素材制作范围')
        self.assertEqual(scope.value,'combined')
        self.assertTrue(any('整批总计 2 条成品' in i.value for i in app.info))
        scope.set_value('separate').run();self.healthy(app)
        self.assertTrue(any('每份素材 2 条成品' in i.value for i in app.info))
        self.button(app,'保存当前设置，稍后处理').click().run();self.healthy(app)
        with WorkspaceStore(cfg['database']) as store:self.assertEqual(store.get_workspace(identity)['source_options']['batch_scope'],'separate')

    def test_long_montage_review_lists_final_videos_and_collapses_ranges(self):
        identity=self.seed();cfg=load_config()
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity);variant=active_variant(w)
            variant.update(form='montage',options={'count':2},warnings=['重复警告','重复警告'])
            first=variant['items'][0];first['segments']=[{'start':1.,'end':4.},{'start':6.,'end':10.}]
            variant['items'].append({**copy.deepcopy(first),'id':'item-2','title':'集锦二'})
            store.save_workspace(w)
        app=self.app();self.healthy(app)
        self.assertEqual(len(next(r for r in app.radio if r.label=='成品列表').options),2)
        self.assertTrue(any('最终成品 2 条' in c.value for c in app.caption))
        self.assertTrue(any(b.label=='重试生成预览' for b in app.button))
        self.assertTrue(any(e.label=='需要处理的问题 · 1 项' for e in app.expander))
        self.assertTrue(any(e.label.startswith('修改组成区间') and not e.proto.expanded for e in app.expander))
        self.assertTrue(any(b.label=='保存审核' for b in app.button))

    def test_subtitle_proofreading_uses_background_job_and_preserves_manual_edits(self):
        identity=self.seed();cfg=load_config()
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity);w['step']=4
            w['packaging']={'proofread_subtitles':True}
            store.save_workspace(w)
        app=self.app();self.healthy(app)
        self.assertTrue(next(c for c in app.checkbox if c.label=='自动字幕生成后使用 AI 校对').value)
        with patch('ui.workbench.submit_job',return_value={'id':'proofreading-job'}) as queue:
            self.button(app,'AI 校对本批自动字幕（失败项可重试）').click().run();self.healthy(app)
        self.assertEqual(queue.call_args.args[0],'workflow_subtitle_review')
        self.assertTrue(queue.call_args.args[1]['retry_failed'])
        with WorkspaceStore(cfg['database']) as store:
            self.assertTrue(store.get_workspace(identity)['packaging']['proofread_subtitles'])

    def test_new_project_switch_and_sidebar_are_usable(self):
        app=self.app();self.button(app,'＋ 新建项目').click().run();self.healthy(app)
        app.sidebar.radio[0].set_value('项目库').run();self.healthy(app)
        self.assertEqual(len([b for b in app.button if b.label=='打开工作台']),2)
        self.button(app,'打开工作台').click().run();self.healthy(app)
        self.assertEqual(app.sidebar.radio[0].value,'工作台')

    def test_dual_stage_uses_imported_assets_and_offset_edit_requires_reconfirmation(self):
        from storage.composites import CompositeStore
        from core.composite_options import normalize_composite_options
        identity=self.seed();cfg=load_config()
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(identity)
            w['assets'].append({'id':'b','path':str(self.root/'主播.mp4'),'name':'主播.mp4','duration':30.,'meta':{}})
            w.update(step=2,source_options={'organization':'dual','form':'full','asset_ids':['a','b'],'match_id':'a',
                    'commentator_ids':['b'],'composite_id':'dual-test'})
            store.save_workspace(w)
        with CompositeStore(cfg['database']) as store:
            store.insert_composite({'id':'dual-test','match':{'source':'比赛.mp4','duration':20.},
                'commentator':{'source':'主播.mp4','duration':30.},'options':normalize_composite_options({'sync_confirmed':True}),
                'outputs':[]})
        app=self.app();self.healthy(app)
        confirmed=next(c for c in app.checkbox if c.label=='已预览核对同步位置')
        self.assertTrue(confirmed.value)
        next(n for n in app.number_input if n.label.startswith('同步偏移')).set_value(2.).run();self.healthy(app)
        self.assertFalse(next(c for c in app.checkbox if c.label=='已预览核对同步位置').value)
        self.assertFalse(any(b.label=='打开文件…' or b.label=='开始导出' for b in app.button))

    def test_cpu_settings_show_actual_primary_env_path_without_markdown_escaping(self):
        with patch.dict(os.environ,{'FCF_PRIMARY_ROOT':str(self.root/'主目录')}):
            app=self.app();app.sidebar.radio[0].set_value('设置').run();self.healthy(app)
            self.assertTrue(any(c.value==str(self.root/'主目录'/'.env') for c in app.code))
