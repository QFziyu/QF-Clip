"""Real media boundaries and bounded vision requests, with no paid API calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np

from core.config import DEFAULTS
from core.compositor import create_composite, render_composite, synchronize_visual, _load
from core.composite_options import normalize_composite_options
from core.jobs import _validate_payload, _execute
from core.llm_client import LLMClient
from core.local_file_picker import choose_videos
from core.source_sequence import prepare_sequence, sequence_mapping, source_paths
from core.utils import read_json
from core.video_input import run_media, probe_video
from core.visual_sync import normalize_search, validate_frame_links, visual_alignment
from storage.composites import CompositeStore


class SequenceVisualUnits(unittest.TestCase):
    def test_known_ids_only_and_strict_search(self):
        a = [{'id': 'M00', 'time': 1.}]; b = [{'id': 'C00', 'time': 10.}]
        for value in ({'links': [{'match_id': 'M99', 'commentator_id': 'C00', 'evidence': 'x'}]},
                      {'links': [{'match_id': 'M00', 'commentator_id': 'C00', 'evidence': '', 'time': 99}]}):
            with self.assertRaises(ValueError):
                validate_frame_links(value, a, b)
        valid = {'event': 'goal', 'match_start': 1., 'match_end': 30., 'commentator_start': 200., 'commentator_end': 230.}
        self.assertEqual(normalize_search(valid)['samples'], 20)
        for override in ({'samples': True}, {'match_end': float('nan')}, {'match_end': 700}, {'event': 'imaginary'}, {'api_key': 'x'}):
            with self.assertRaises(ValueError): normalize_search({**valid, **override})
        for payload in ({'composite_id': 'x', 'search': valid, 'analyze': 1}, {'composite_id': 'x', 'search': valid, 'api_key': 'x'}):
            with self.assertRaises(ValueError): _validate_payload('compose_visual', payload)

    def test_multi_picker_keeps_returned_order_and_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp)/name for name in ('2.mp4', '1.mp4')]
            for p in paths: p.touch()
            with patch('core.local_file_picker.choose_local', return_value=list(map(str, paths))) as runner:
                self.assertEqual(choose_videos(), list(map(str, paths)))
                self.assertTrue(runner.call_args.args[2])
            with patch('core.local_file_picker.choose_local', return_value=[]):
                self.assertEqual(choose_videos(), [])
            with self.assertRaises(ValueError): source_paths([paths[0], paths[0]])
            from scripts.pick_video import pick_video
            # Windows now uses a native PowerShell dialog, not Tk. Mock both
            # backends so this unit test never opens an actual modal window.
            with patch('scripts.pick_video._native_windows_choice', return_value={'paths':list(map(str,paths))}), \
                 patch('tkinter.Tk'), patch('tkinter.filedialog.askopenfilenames', return_value=list(map(str, paths))):
                self.assertEqual(pick_video('选择', multiple=True)['paths'], list(map(str, paths)))


class SequenceVisualMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get('FCF_TEST_FFMPEG', 'ffmpeg')
        cls.ffprobe = os.environ.get('FCF_TEST_FFPROBE', 'ffprobe')
        try: run_media([cls.ffmpeg, '-version'], timeout=10)
        except RuntimeError: raise unittest.SkipTest('FFmpeg not available')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='拼接 画面-')
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(ffmpeg=self.ffmpeg, ffprobe=self.ffprobe, database=str(self.root/'db.sqlite3'),
                        cache_dir=str(self.root/'cache'), output_dir=str(self.root/'output'))
        self.cfg['render'].update(encoder='libx264', fps=10, horizontal=[320,180], preview=[320,180])
        self.a, self.b = self.root/'主播 1.mp4', self.root/'主播 2.mp4'
        run_media([self.ffmpeg, '-v','error','-y','-f','lavfi','-i','color=red:s=320x180:r=10:d=2',
            '-f','lavfi','-i','sine=frequency=440:duration=2','-c:v','libx264','-c:a','aac','-shortest',str(self.a)])
        run_media([self.ffmpeg, '-v','error','-y','-f','lavfi','-i','color=blue:s=240x240:r=15:d=3',
            '-c:v','libx264',str(self.b)])

    def tearDown(self): self.temp.cleanup()

    def _pixel(self, path, when):
        raw = self.root/'frame.rgb'
        run_media([self.ffmpeg, '-v','error','-y','-ss',str(when),'-i',str(path),'-frames:v','1',
            '-vf','scale=2:2','-f','rawvideo','-pix_fmt','rgb24',str(raw)])
        return np.frombuffer(raw.read_bytes(), dtype=np.uint8).reshape(2,2,3).mean(axis=(0,1))

    def test_actual_join_order_boundary_audio_cache_mapping_and_composition(self):
        joined = prepare_sequence([self.a,self.b], self.cfg)
        self.assertAlmostEqual(joined['duration'],5.,delta=.12)
        self.assertGreater(self._pixel(joined['source'],1)[0],200)
        self.assertGreater(self._pixel(joined['source'],3)[2],100)
        wav = self.root/'joined.wav'
        run_media([self.ffmpeg,'-v','error','-y','-i',joined['source'],'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(wav)])
        with wave.open(str(wav),'rb') as f: samples=np.frombuffer(f.readframes(f.getnframes()),dtype='<i2').astype(float)
        self.assertGreater(np.sqrt(np.mean(samples[8000:24000]**2)),100)
        self.assertLess(np.sqrt(np.mean(samples[40000:60000]**2)),1)
        mapping=sequence_mapping(joined,1.,4.)
        self.assertEqual([p['source_start'] for p in mapping],[1.,0.])
        self.assertEqual([p['source_end'] for p in mapping],[2.,2.])
        with patch('core.source_sequence.run_media',side_effect=AssertionError('reuse cached sequence')):
            self.assertEqual(prepare_sequence([self.a,self.b],self.cfg),joined)
        document=create_composite(self.a,[self.a,self.b],self.cfg)
        options=normalize_composite_options({'sync_confirmed':True,'offset_seconds':1.5})
        result=render_composite(document['id'],self.cfg,options,[{'start':0.,'end':1.}])
        self.assertEqual(len(result['mappings'][0]['commentator_parts']),2)
        run_media([self.ffmpeg,'-v','error','-i',result['video'],'-f','null','-'])
        self.b.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'原始主播'): _load(document['id'],self.cfg)

    def test_interrupt_resumes_completed_normalized_parts(self):
        real=run_media
        def fail_second(command,**kwargs):
            if str(self.b) in command and '-filter_complex' in command: raise RuntimeError('test interrupted')
            return real(command,**kwargs)
        with patch('core.source_sequence.run_media',side_effect=fail_second),self.assertRaisesRegex(RuntimeError,'interrupted'):
            prepare_sequence([self.a,self.b],self.cfg)
        part=next((self.root/'cache/source_sequences').rglob('part_000.nut'))
        modified=part.stat().st_mtime_ns
        joined=prepare_sequence([self.a,self.b],self.cfg)
        self.assertEqual(part.stat().st_mtime_ns,modified)
        self.assertEqual(len(joined['parts']),2)

    def test_real_frame_extraction_vision_request_cache_and_error_preservation(self):
        document=create_composite(self.a,self.b,self.cfg)
        search={'event':'goal','match_start':0.,'match_end':2.,'commentator_start':1.,'commentator_end':3.,'samples':8}
        result=synchronize_visual(document['id'],self.cfg,search,analyze=False)
        self.assertIsNone(result['offset_seconds'])
        self.assertTrue(all(Path(p).is_file() for p in result['sheets']['match']))
        self.assertGreater(self._pixel(result['frames']['match'][0]['path'],0)[0],200)
        links={'links':[{'match_id':'M01','commentator_id':'C01','evidence':'测试用真实帧 ID'}]}
        payloads=[]
        def transport(payload):
            payloads.append(payload)
            return {'choices':[{'message':{'content':json.dumps(links)}}]}
        self.cfg['openai'].update(api_key='unit-vision-secret',model='unit-vision',max_retries=0,budget_usd=10)
        client=LLMClient(self.cfg['openai'],self.root/'mock_ledger.json',transport=transport)
        with patch('core.visual_sync.LLMClient',return_value=client):
            result=synchronize_visual(document['id'],self.cfg,search)
        self.assertAlmostEqual(result['offset_seconds'],1.)
        self.assertEqual(result['confidence'],'low')
        self.assertTrue(any(v['type']=='image_url' for v in payloads[0]['messages'][1]['content']))
        with patch('core.visual_sync.LLMClient',side_effect=AssertionError('cache')):
            self.assertEqual(synchronize_visual(document['id'],self.cfg,search),result)
        self.cfg['openai']['model']='conflicting-links'
        conflicting={'links':[
            {'match_id':'M00','commentator_id':'C07','evidence':'test first'},
            {'match_id':'M07','commentator_id':'C00','evidence':'test second'}]}
        with patch('core.visual_sync.LLMClient') as factory:
            factory.return_value.complete.return_value=conflicting
            conflict=synchronize_visual(document['id'],self.cfg,search)
        self.assertIsNone(conflict['offset_seconds'])
        self.assertIn('不一致',conflict['note'])
        self.cfg['openai']['model']='unavailable'
        with CompositeStore(self.cfg['database']) as db:
            db.update_composite(document['id'],options=normalize_composite_options({'offset_seconds':2.}),sync={'offset_seconds':2.,'confidence':'low'})
        with patch('core.visual_sync.LLMClient',side_effect=RuntimeError('unit-vision-secret test no images')):
            failed=synchronize_visual(document['id'],self.cfg,search)
        self.assertNotIn('unit-vision-secret',json.dumps(failed))
        self.assertIsNone(failed['offset_seconds'])
        with CompositeStore(self.cfg['database']) as db:
            saved=db.get_composite(document['id'])
            self.assertEqual(saved['options']['offset_seconds'],2.)
            self.assertEqual(saved['sync']['offset_seconds'],2.)


class SequenceVisualUI(unittest.TestCase):
    def test_ordered_multi_source_and_visual_queue(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); database=root/'db.sqlite3'
            paths=[root/f'{n}.mp4' for n in ('比赛','主播1','主播2')]
            for path in paths:path.touch()
            with CompositeStore(database) as db:
                db.insert_composite({'id':'ui-sequence','match':{'source':str(paths[0]),'duration':100.},
                    'commentator':{'source':str(paths[1]),'duration':200.},'options':normalize_composite_options(),'outputs':[]})
            with patch.dict(os.environ,{'DATABASE_PATH':str(database)}),patch('core.jobs.ensure_worker',return_value=False), \
                 patch('ui.composite_page.choose_videos',return_value=list(map(str,paths[1:]))), \
                 patch('ui.composite_page.submit_job',return_value={'id':'queued-visual'}) as queue:
                app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'tests/fixtures/legacy_ui.py'),default_timeout=20).run()
                app.sidebar.radio[0].set_value('双路合成').run()
                def item(collection,label):return next(x for x in collection if x.label==label)
                item(app.checkbox,'主播端有多个素材：按列表顺序首尾拼接').check().run()
                item(app.button,'选择多个主播素材').click().run()
                app.button(key='source_up_1').click().run()
                self.assertEqual(app.session_state['composite_sequence_paths'],[str(paths[2]),str(paths[1])])
                item(app.text_input,'比赛录像本地路径').set_value(str(paths[0]))
                item(app.button,'创建合成项目').click().run()
                self.assertEqual(queue.call_args.args[0],'compose_prepare')
                self.assertEqual(queue.call_args.args[1]['commentator'],[str(paths[2]),str(paths[1])])
                item(app.button,'生成事件画面对照图（不调用 API）').click().run()
                self.assertEqual(queue.call_args.args[0],'compose_visual')
                self.assertFalse(queue.call_args.args[1]['analyze'])
                item(app.button,'AI 匹配事件画面').click().run()
                self.assertTrue(queue.call_args.args[1]['analyze'])
                frames={'match':[{'id':'M00','time':2.,'path':str(root/'missing.jpg')}],
                    'commentator':[{'id':'C00','time':42.,'path':str(root/'missing.jpg')},
                                   {'id':'C01','time':82.,'path':str(root/'missing.jpg')}]}
                with CompositeStore(database) as db:
                    db.update_composite('ui-sequence',visual_sync={'note':'模拟帧选择', 'offset_seconds':40.,
                        'confidence':'low','uncertainty_seconds':1.,'frames':frames,'sheets':{'match':[],'commentator':[]},'anchors':[]})
                item(app.button,'刷新画面结果').click().run()
                item(app.button,'采用画面建议偏移（仍需预览）').click().run()
                self.assertEqual(item(app.number_input,'同步偏移（秒，可正可负）').value,40.)
                item(app.selectbox,'主播事件帧').set_value(1).run()
                item(app.button,'使用这两个画面位置计算偏移').click().run()
                with CompositeStore(database) as db:
                    saved=db.get_composite('ui-sequence')['options']
                    self.assertEqual(saved['offset_seconds'],80.)
                    self.assertFalse(saved['sync_confirmed'])
                self.assertEqual(len(app.exception),0,str(app.exception))
                self.assertEqual(len(app.error),0,str(app.error))
