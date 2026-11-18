"""Batch count, cache reuse, source boundaries and full-length review regressions."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.batch_timeline import virtual_timeline, split_ranges
from core.config import load_config
from core.utils import atomic_json, fingerprint, file_signature
from core.workflow import prepare_workflow, active_variant, _analysis_project, item_transcript
from core.transcriber import validate_transcript
from storage.workspaces import WorkspaceStore


class BatchTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='batch-')
        self.root=Path(self.temp.name);self.cfg=load_config()
        self.cfg['subtitle_review']['enabled']=False
        self.cfg.update(database=str(self.root/'db.sqlite3'),cache_dir=str(self.root/'cache'),output_dir=str(self.root/'out'))
        self.metas=[];self.projects={}
        for i in range(3):
            source=self.root/f'素材 {i}.mp4';source.touch()
            meta=dict(source=str(source),duration=100.,source_signature=file_signature(source))
            self.metas.append(meta)
            transcript=validate_transcript({'segments':[{'start':j,'end':j+20.,'text':'完整的足球讨论',
                'words':[{'start':j,'end':j+1.,'word':'足球'}]} for j in (1.,30.,60.)]},100.)
            key=fingerprint(transcript);directory=self.root/f'p{i}'
            atomic_json(directory/'transcripts'/f'{key}.json',transcript)
            project=dict(id=f'p{i}',source=str(source),duration=100.,name=source.name,project_dir=str(directory),
                metadata={**meta,'transcript_key':key},status='transcribed')
            self.projects[str(source)]=project
            with WorkspaceStore(self.cfg['database']) as db:db.upsert_project(project)
        with WorkspaceStore(self.cfg['database']) as db:
            w=db.create_workspace('整批比赛')
            w['assets']=[dict(id=str(i),name=Path(m['source']).name,path=m['source'],duration=100.,meta=m) for i,m in enumerate(self.metas)]
            self.workspace_id=w['id'];db.save_workspace(w)
        self.settings=dict(organization='single',form='montage',asset_ids=['0','1','2'],
            editing_options=dict(format='montage',count=2,min_seconds=15,target_seconds=45,max_seconds=80))

    def tearDown(self):self.temp.cleanup()

    def process(self,source,*args,**kwargs):
        return {'project_id':self.projects[str(source)]['id']}

    def select(self,transcript,meta,cfg,directory,**kwargs):
        clips=[dict(start=s['start'],end=s['end'],title='缓存话题',score=80) for s in transcript['segments']]
        atomic_json(Path(directory)/'selection'/'decisions.json',{'clips':clips})
        return clips

    def run_plan(self,settings,select=None):
        with patch('core.video_input.probe_video',side_effect=lambda path,*a,**k:copy.deepcopy(next(m for m in self.metas if m['source']==path))), \
             patch('core.pipeline.process_video',side_effect=self.process), \
             patch('core.clip_selector.select_clips',side_effect=select or self.select):
            return prepare_workflow(self.workspace_id,settings,self.cfg)

    def test_combined_default_applies_count_once_and_reuses_cached_analysis(self):
        separate=self.run_plan({**self.settings,'batch_scope':'separate'})
        self.assertEqual(separate['candidate_count'],6)
        combined=self.run_plan(self.settings,select=AssertionError('缓存命中后不得请求 LLM'))
        self.assertEqual(combined['candidate_count'],2)
        with WorkspaceStore(self.cfg['database']) as db:w=db.get_workspace(self.workspace_id)
        self.assertEqual(len(w['variants']),2)
        variant=active_variant(w)
        self.assertEqual(w['source_options']['batch_scope'],'combined')
        self.assertTrue(all(i['status']=='pending' for i in variant['items']))
        for item in variant['items']:
            self.assertTrue(item['source']['meta']['virtual_timeline'])
            self.assertGreater(sum(s['end']-s['start'] for s in item['segments']),40.)
        transcript=item_transcript(variant['items'][0],self.cfg)
        self.assertEqual(len(transcript['segments']),9)
        self.assertEqual(transcript['segments'][3]['words'][0]['start'],101.)
        repeated=self.run_plan(self.settings,select=AssertionError('不得重复分析'))
        self.assertEqual(combined['variant_id'],repeated['variant_id'])
        with WorkspaceStore(self.cfg['database']) as db:self.assertEqual(len(db.get_workspace(self.workspace_id)['variants']),2)

    def test_sequence_analysis_is_virtual_and_preserves_selected_file_order(self):
        result=self.run_plan({**self.settings,'organization':'sequence','asset_ids':['2','0','1']})
        self.assertEqual(result['candidate_count'],2)
        with WorkspaceStore(self.cfg['database']) as db:w=db.get_workspace(self.workspace_id)
        meta=active_variant(w)['items'][0]['source']['meta']
        self.assertEqual([p['source'] for p in meta['parts']],[self.metas[i]['source'] for i in (2,0,1)])

    def test_cross_file_ranges_retain_global_and_local_clocks(self):
        meta=virtual_timeline(self.metas,self.cfg)
        cuts=split_ranges(meta,[{'start':90.,'end':120.},{'start':190.,'end':205.}])
        self.assertEqual([(c['start'],c['end']) for c in cuts],[(90.,100.),(0.,20.),(90.,100.),(0.,5.)])
        self.assertEqual(sum(c['end']-c['start'] for c in cuts),45.)
        self.assertEqual([c['timeline_start'] for c in cuts],[90.,100.,190.,200.])

    def test_review_ignores_old_sample_preview(self):
        from ui.workbench import _media
        with WorkspaceStore(self.cfg['database']) as db:
            w=db.get_workspace(self.workspace_id)
            item={'id':'i','segments':[{'start':0.,'end':80.}],'title':'完整片'}
            sampled=self.root/'sample.mp4';sampled.touch()
            db.record_workflow_media(w['id'],'i',{},dict(video=str(sampled),item_signature=fingerprint(item),packaging_signature=fingerprint({}),preview_note='每段最多预览前 20 秒'),preview=True)
        self.assertEqual(_media(w,item,self.cfg),(None,None))

    def test_style_change_invalidates_preview_and_preview_uses_saved_packaging(self):
        from core.workflow import preview_workflow
        from ui.workbench import _media
        self.run_plan(self.settings)
        with WorkspaceStore(self.cfg['database']) as db:
            w=db.get_workspace(self.workspace_id);item=active_variant(w)['items'][0]
            old=self.root/'old.mp4';old.touch()
            db.record_workflow_media(w['id'],item['id'],{},dict(video=str(old),item_signature=fingerprint(item),
                preview_complete=True,packaging_signature=fingerprint({})),preview=True)
            w['packaging']={'subtitles':True,'export_options':{'subtitle':{'font_size':34,'margin_v':36}}}
            w=db.save_workspace(w)
        self.assertEqual(_media(w,item,self.cfg),(None,None))
        with patch('core.workflow_renderer.render_workflow_item',return_value={'video':'new.mp4'}) as renderer:
            preview_workflow(w['id'],item['id'],self.cfg)
        self.assertEqual(renderer.call_args.kwargs['snapshot']['packaging'],w['packaging'])


if __name__=='__main__':unittest.main()
