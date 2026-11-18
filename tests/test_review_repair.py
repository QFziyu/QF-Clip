import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core.config import load_config
from core.transcript_refine import refine_transcript,align_sentences,_units,_validate
from core.transcriber import validate_transcript
from core.editing import build_drafts
from core.editing_options import normalize_editing_options
from core.subtitle import transcript_cues,cues_srt,validate_srt
from core.workflow import visual_signature,prepare_workflow,active_variant
from core.clip_rating import rating_for_ranges
from storage.workspaces import WorkspaceStore

class RepairTests(unittest.TestCase):
 def test_scoped_review_keeps_outside_text_and_expanded_cut_needs_review(self):
  from core.workflow_quality import merge_scoped_review
  from core.subtitle_review import item_cues
  raw={'segments':[dict(start=0.,end=4.,text='开场讨论'),dict(start=50.,end=54.,text='后面的原文')]}
  scoped={'segments':raw['segments'][:1],'review_scope':{'ranges':[dict(start=0,end=4)],'context_seconds':12}}
  revised={'segments':[dict(start=0.,end=4.,text='开场讨论。')],'semantic_refinement':{'status':'complete','changes':[]}}
  merged=merge_scoped_review(raw,scoped,revised)
  self.assertEqual(merged['segments'][-1],raw['segments'][-1])
  with patch('core.workflow.item_transcript',return_value=merged):
   _,report=item_cues({'transcript_key':'x','segments':[dict(start=0,end=54)]},load_config())
   self.assertEqual(report['status'],'pending');self.assertTrue(report['scope_changed'])
   _,report=item_cues({'transcript_key':'x','segments':[dict(start=0,end=4)]},load_config())
   self.assertEqual(report['status'],'complete')
 def test_review_report_only_contains_selected_ranges(self):
  from core.subtitle_review import item_cues
  t={'segments':[dict(start=0.,end=1.,text='原文。')],'semantic_refinement':{'status':'complete','changed':2,'changes':[
   {'start':0,'end':1,'before':'原文','after':'原文。','applied':True},
   {'start':20,'end':21,'before':'另文','after':'另文。','applied':True}]}}
  with patch('core.workflow.item_transcript',return_value=t):
   _,report=item_cues({'transcript_key':'x','segments':[{'start':0,'end':3}]},load_config())
  self.assertEqual(report['changed'],1);self.assertEqual(report['source_changed'],2)
  self.assertEqual(t['semantic_refinement']['changed'],2)
 def test_failed_chunk_retries_without_rerunning_successful_chunks(self):
  from core.llm_client import LLMError
  raw={'segments':[dict(start=0.,end=4.,text='他说球队配合很好')],'provenance':'local'}
  c=load_config();c['subtitle_review']['enabled']=True
  with tempfile.TemporaryDirectory() as d,patch('core.transcript_refine.configured',return_value=True),patch('core.transcript_refine.LLMClient') as client:
   client.return_value.complete.side_effect=LLMError('temporary failure')
   _,report=refine_transcript(raw,c,d,request=True)
   self.assertEqual(report['status'],'failed')
   client.return_value.complete.side_effect=None
   client.return_value.complete.return_value={'paragraphs':[{'id':0,'sentences':['他说球队配合很好。']}]}
   _,report=refine_transcript(raw,c,d,request=True,retry_failed=True)
   self.assertEqual(report['status'],'complete')
   refine_transcript(raw,c,d,request=True,retry_failed=True)
   self.assertEqual(client.return_value.complete.call_count,2)
 def test_context_cache_isolated_by_model_and_terms(self):
  raw={'segments':[dict(start=0.,end=4.,text='球队配合很好')],'provenance':'local'}
  c=load_config();c['subtitle_review']['enabled']=True
  with tempfile.TemporaryDirectory() as d,patch('core.transcript_refine.configured',return_value=True),patch('core.transcript_refine.LLMClient') as client:
   client.return_value.complete.return_value={'paragraphs':[{'id':0,'sentences':['球队配合很好。']}]}
   refine_transcript(raw,c,d,request=True)
   c['openai']['model']='different-model'
   refine_transcript(raw,c,d,request=True)
   c['terms']=['新增专名']
   refine_transcript(raw,c,d,request=True)
   self.assertEqual(client.return_value.complete.call_count,3)
 def test_defaults_and_grounded_scores(self):
  opt=normalize_editing_options()
  self.assertEqual((opt['count'],opt['min_seconds'],opt['max_seconds'],opt['target_seconds']),(10,90,180,120))
  cs=[dict(start=30,end=150,title='完整观点',score=83,reason='观点有前后文')]
  ds,_=build_drafts(cs,[],opt,{'segments':[dict(start=0,end=240,text='足球观点')]},240)
  self.assertEqual(ds[0]['score'],83)
  self.assertEqual(rating_for_ranges([dict(start=0,end=10)],[dict(start=0,end=10,score=101)])[0],None)
 def test_context_review_cache_raw_preservation_and_clocks(self):
  raw={'segments':[dict(start=0.,end=8.,text='这个球员的跑位非常好他把空间拉开了')],'provenance':'local'}
  before=copy.deepcopy(raw)
  response={'paragraphs':[{'id':0,'sentences':['这个球员的跑位非常好，','他把空间拉开了。']}]}
  c=load_config();c['subtitle_review']['enabled']=True
  with tempfile.TemporaryDirectory() as d,patch('core.transcript_refine.configured',return_value=True),patch('core.transcript_refine.LLMClient') as client:
   client.return_value.complete.return_value=response
   result,report=refine_transcript(raw,c,d,request=True)
   again,_=refine_transcript(raw,c,d,request=True)
   self.assertEqual(client.return_value.complete.call_count,1)
   self.assertEqual(raw,before);self.assertEqual(report['status'],'complete')
   self.assertEqual(len(result['segments']),2)
   validate_transcript(result,8)
   cues=transcript_cues(result,{'start':0,'end':8})
   validate_srt(cues_srt(cues),8)
   self.assertEqual(''.join(x['text'].replace('\n','') for x in cues),'这个球员的跑位非常好，他把空间拉开了。')
   self.assertEqual(result,again)
 def test_invalid_or_hallucinated_response_preserves_local_text(self):
  raw={'segments':[dict(start=0.,end=4.,text='比赛比分是2比1')],'provenance':'local'}
  response={'paragraphs':[{'id':0,'sentences':['比赛比分是3比1。']}]}
  c=load_config();c['subtitle_review']['enabled']=True
  with tempfile.TemporaryDirectory() as d,patch('core.transcript_refine.configured',return_value=True),patch('core.transcript_refine.LLMClient') as client:
   client.return_value.complete.return_value=response
   r,report=refine_transcript(raw,c,d,request=True)
   self.assertEqual(r['segments'][0]['text'],raw['segments'][0]['text']);self.assertEqual(report['rejected'],1)
  with self.assertRaises(ValueError):_validate({'paragraphs':[{'id':2,'sentences':['无效。']}]},1)
 def test_preview_signature_ignores_rating_approval_title_but_not_subtitles(self):
  item={'source':{},'segments':[{'start':1,'end':10}],'subtitle_text':None,'transcript_key':'x','status':'pending','revision':0}
  other={**item,'status':'approved','title':'人工标题','user_rating':5,'score':90,'revision':3}
  self.assertEqual(visual_signature(item),visual_signature(other))
  self.assertNotEqual(visual_signature(item),visual_signature({**other,'subtitle_text':'人工字幕'}))
 def test_prepare_automatically_previews_pending_items_and_retry_reuses_plan(self):
  c=load_config()
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);src=p/'input.mp4';src.touch();c.update(database=str(p/'db.sqlite'),cache_dir=str(p/'cache'),output_dir=str(p/'out'))
   with WorkspaceStore(c['database']) as db:
    w=db.create_workspace('自动预览测试');w['assets']=[{'id':'a','path':str(src),'name':src.name}];db.save_workspace(w)
   settings={'organization':'single','form':'full','asset_ids':['a']}
   meta={'source':str(src),'duration':100.}
   with patch('core.video_input.probe_video',return_value=meta),patch('core.workflow.preview_workflow',return_value={'video':'preview.mp4'}) as preview:
    result=prepare_workflow(w['id'],settings,c)
    self.assertEqual(result['preview_count'],1);preview.assert_called_once()
    prepare_workflow(w['id'],settings,c)
   with WorkspaceStore(c['database']) as db:
    saved=db.get_workspace(w['id']);self.assertEqual(len(saved['variants']),1)
    self.assertEqual(active_variant(saved)['items'][0]['status'],'pending')
    self.assertEqual(db.list_media(w['id'],False),[])
if __name__=='__main__':unittest.main()
