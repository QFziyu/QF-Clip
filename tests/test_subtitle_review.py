"""Proofreading changes text only; bounded requests, shared budgets and recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import DEFAULTS
from core.llm_client import LLMClient
from core.subtitle_review import proofread_cues,item_cues


class SubtitleReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='字幕校对-');self.root=Path(self.temp.name)
        self.cfg=copy.deepcopy(DEFAULTS)
        self.cfg['subtitle_review'].update(enabled=True,chunk_cues=2,max_chunk_chars=100,max_retries=0)
        self.cfg['openai'].update(api_key='test-key',model='test-model',max_retries=0,budget_usd=50,max_calls=50)
        self.cfg['terms']=['格伊','瓜迪奥拉','多纳鲁马']
        self.cues=[{'start':0.,'end':2.,'text':'格一的前顶出球'},
            {'start':2.,'end':4.,'text':'瓜地奥拉这次换人很关键'},
            {'start':5.,'end':7.,'text':'比分还是2比1'}]
        self.calls=[]

    def tearDown(self):self.temp.cleanup()

    def client(self,transport):
        def construct(options,path):return LLMClient(options,path,transport=transport,sleeper=lambda n:None)
        return patch('core.subtitle_review.LLMClient',side_effect=construct)

    def transport(self,payload):
        self.calls.append(payload)
        data=json.loads(payload['messages'][1]['content'])
        replacements={'格一的前顶出球':'格伊的前顶出球','瓜地奥拉这次换人很关键':'瓜迪奥拉这次换人很关键'}
        edits=[{'id':s['id'],'text':replacements[s['text']]} for s in data['subtitles'] if s['text'] in replacements]
        return {'choices':[{'message':{'content':json.dumps({'edits':edits},ensure_ascii=False)}}],
            'usage':{'prompt_tokens':100,'completion_tokens':50}}

    def test_text_only_corrections_and_original_audit(self):
        original=copy.deepcopy(self.cues)
        with self.client(self.transport):cues,report=proofread_cues(self.cues,self.cfg,self.root,request=True)
        self.assertEqual(cues[0]['text'],'格伊的前顶出球')
        self.assertEqual(cues[1]['text'],'瓜迪奥拉这次换人很关键')
        self.assertEqual(self.cues,original)
        self.assertEqual([(c['start'],c['end']) for c in cues],[(c['start'],c['end']) for c in original])
        self.assertEqual((report['status'],report['changed']),('complete',2))
        self.assertTrue((Path(report['cache_dir'])/'input.json').exists())
        self.assertEqual(report['changes'][0]['before'],original[0]['text'])
        self.assertEqual(len(self.calls),2)
        second=json.loads(self.calls[1]['messages'][1]['content'])
        self.assertEqual(second['context_before'],[c['text'] for c in original[:2]])
        self.assertNotIn('start',second['subtitles'][0])

    def test_refresh_and_frame_rounding_reuse_cache_without_api(self):
        with self.client(self.transport):expected,_=proofread_cues(self.cues,self.cfg,self.root,request=True)
        shifted=[{**c,'start':c['start']+.1,'end':c['end']+.1} for c in self.cues]
        self.cfg['openai']['api_key']='' # cached corrections remain readable offline
        with patch('core.subtitle_review.LLMClient',side_effect=AssertionError('must reuse cache')):
            cues,report=proofread_cues(shifted,self.cfg,self.root,request=False)
        self.assertEqual([c['text'] for c in cues],[c['text'] for c in expected])
        self.assertEqual(report['status'],'complete');self.assertEqual(cues[1]['start'],2.1)

    def test_no_network_from_editor_and_mock_or_disabled_modes(self):
        with patch('core.subtitle_review.LLMClient',side_effect=AssertionError('read-only')):
            cues,report=proofread_cues(self.cues,self.cfg,self.root)
            self.assertEqual(report['status'],'pending');self.assertEqual(cues,self.cues)
            self.assertEqual(proofread_cues(self.cues,self.cfg,self.root,request=True,mock=True)[1]['status'],'mock')
            self.assertEqual(proofread_cues(self.cues,self.cfg,self.root,request=True,enabled=False)[1]['status'],'disabled')
            self.assertEqual(item_cues({'subtitle_text':'manual'},self.cfg,request=True)[1]['status'],'manual')

    def test_failed_chunk_reuses_successful_chunks_and_requires_explicit_retry(self):
        first=[True]
        def unreliable(payload):
            data=json.loads(payload['messages'][1]['content'])
            if data['subtitles'][0]['text']=='比分还是2比1' and first[0]:
                first[0]=False
                raise ValueError('simulated timeout/response failure')
            return self.transport(payload)
        with self.client(unreliable):cues,report=proofread_cues(self.cues,self.cfg,self.root,request=True)
        self.assertEqual((report['status'],report['completed'],report['failed']),('partial',1,1))
        with patch('core.subtitle_review.LLMClient',side_effect=AssertionError('no implicit retry')):
            self.assertEqual(proofread_cues(self.cues,self.cfg,self.root,request=True)[1]['failed'],1)
        with self.client(self.transport):fixed,report=proofread_cues(self.cues,self.cfg,self.root,request=True,retry_failed=True)
        self.assertEqual(report['status'],'complete');self.assertEqual(len(self.calls),2)
        self.assertEqual(fixed[0]['text'],cues[0]['text'])

    def test_numbers_and_large_rewrites_are_rejected_without_blocking_other_corrections(self):
        self.cfg['subtitle_review']['chunk_cues']=10
        def unsafe(payload):
            return {'choices':[{'message':{'content':json.dumps({'edits':[
                {'id':0,'text':'格伊的前顶出球'},{'id':1,'text':'这是一场毫无疑问的精彩胜利'},
                {'id':2,'text':'比分还是3比1'}]},ensure_ascii=False)}}]}
        with self.client(unsafe):cues,report=proofread_cues(self.cues,self.cfg,self.root,request=True)
        self.assertEqual(cues[0]['text'],'格伊的前顶出球');self.assertEqual(cues[1:],self.cues[1:])
        self.assertEqual((report['changed'],report['rejected']),(1,2))

    def test_invalid_ids_duplicates_and_control_content_fall_back_to_raw(self):
        for edits in [[{'id':9,'text':'错误'}],[{'id':0,'text':'格伊'},{'id':0,'text':'格伊'}],
            [{'id':False,'text':'错误'}],[{'id':0,'text':'字幕\n00:00:00,000 --> 00:00:01,000'}]]:
            with self.subTest(edits=edits),tempfile.TemporaryDirectory(dir=self.root) as root:
                with patch('core.subtitle_review.LLMClient.complete',return_value={'edits':edits}):
                    cues,report=proofread_cues(self.cues[:2],self.cfg,root,request=True)
                self.assertEqual(cues,self.cues[:2]);self.assertEqual(report['status'],'failed')

    def test_budget_exhaustion_preserves_completed_changes_and_does_not_reset_ledger(self):
        self.cfg['openai']['max_calls']=1
        with self.client(self.transport):cues,report=proofread_cues(self.cues,self.cfg,self.root,request=True)
        self.assertEqual(report['status'],'partial');self.assertEqual(cues[0]['text'],'格伊的前顶出球')
        ledger=json.loads((self.root/'selection/api_budget.json').read_text(encoding='utf-8'))
        self.assertEqual(len(ledger['attempts']),1)
        with self.client(self.transport):proofread_cues(self.cues,self.cfg,self.root,request=True,retry_failed=True)
        self.assertEqual(len(self.calls),1)

    def test_changed_wording_model_or_dictionary_invalidates_cache(self):
        with self.client(self.transport):proofread_cues(self.cues[:2],self.cfg,self.root,request=True)
        self.cfg['terms'].append('哈兰德')
        with self.client(self.transport):proofread_cues(self.cues[:2],self.cfg,self.root,request=True)
        self.assertEqual(len(self.calls),2)


if __name__=='__main__':unittest.main()
