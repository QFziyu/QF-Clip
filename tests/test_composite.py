"""Dual-feed clocks, false matches, real FFmpeg composition, and durable UI."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from core.audio_sync import correlation_candidates, match_audio
from core.composite_options import normalize_composite_options, overlap_range, validate_ranges
from core.compositor import create_composite, render_composite, synchronize_composite
from core.config import DEFAULTS
from core.face_tracker import crop_track
from core.jobs import _validate_payload, _execute
from core.llm_client import LLMClient
from core.semantic_sync import semantic_alignment, validate_links
from core.video_input import run_media
from storage.composites import CompositeStore


class CompositeUnitTests(unittest.TestCase):
    def test_offsets_overlap_validation(self):
        self.assertEqual(overlap_range(100, 200, 40), {'start': 0., 'end': 100})
        self.assertEqual(overlap_range(100, 200, -40), {'start': 40, 'end': 100})
        for v in ({'offset_seconds': float('nan')}, {'width_ratio': True}, {'audio': 'x'}, {'sync_confirmed': 1}, {'api_key': 'x'}):
            with self.assertRaises(ValueError):
                normalize_composite_options(v)
        with self.assertRaises(ValueError):
            overlap_range(10, 10, 20)
        for ranges in ([{'start': 0, 'end': 11}], [{'start': 2, 'end': 5}, {'start': 4, 'end': 6}], []):
            with self.assertRaises(ValueError):
                validate_ranges(ranges, {'start': 0, 'end': 10})

    def test_three_hour_features_large_positive_and_negative_offset(self):
        rng = np.random.default_rng(72)
        original = rng.normal(size=(21600, 32)).astype(np.float32)
        shifted = np.r_[rng.normal(size=(3500, 32)), original[:14000]].astype(np.float32)
        a = correlation_candidates(original, shifted)[0]
        b = correlation_candidates(shifted, original)[0]
        self.assertEqual(a['offset_seconds'], 1750.)
        self.assertEqual(b['offset_seconds'], -1750.)
        self.assertGreater(a['score'], .99)

    def test_silence_and_unrelated_features(self):
        self.assertEqual(correlation_candidates(np.zeros((200, 32)), np.zeros((200, 32))), [])
        rng = np.random.default_rng(4)
        self.assertLess(correlation_candidates(rng.normal(size=(300, 32)), rng.normal(size=(300, 32)))[0]['score'], .1)

    def test_crop_contains_faces_and_fallback(self):
        track = crop_track([[150, 200, 80, 90], None, [300, 190, 80, 90]], 640, 480, 1)
        self.assertEqual(track['mode'], 'face')
        for p, box in zip(track['points'], [[150, 200, 80, 90], [150, 200, 80, 90], [300, 190, 80, 90]]):
            x,y,w,h = box
            self.assertLessEqual(p['x'], x)
            self.assertLessEqual(p['y'], y)
            self.assertGreaterEqual(p['x']+track['crop_width'], x+w)
            self.assertGreaterEqual(p['y']+track['crop_height'], y+h)
        self.assertEqual(crop_track([None], 640, 360, 1)['mode'], 'fit')
        self.assertEqual(crop_track([[0, 0, 300, 450]], 320, 480, 1)['mode'], 'fit')

    def test_strict_queue_snapshot(self):
        value = _validate_payload('compose_render', {'composite_id': 'x', 'options': {}, 'preview': True})
        self.assertEqual(value['options']['width_ratio'], .25)
        for payload in ({'composite_id': 'x', 'options': {}, 'preview': 1}, {'composite_id': 'x', 'options': {}, 'preview_start': float('nan')},
                        {'composite_id': 'x', 'options': {}, 'cmd': 'anything'}):
            with self.assertRaises(ValueError):
                _validate_payload('compose_render', payload)

    def test_semantic_real_ids_consensus_and_client_format(self):
        transcript = {'segments': [{'start': t, 'end': t+2, 'text': f'第 {i} 个明确事件'} for i,t in enumerate([10, 100, 200])]}
        later = copy.deepcopy(transcript)
        for r in later['segments']:
            r['start'] += 300
            r['end'] += 300
        links = {'links': [{'match_id': str(i), 'commentator_id': str(i)} for i in range(3)]}
        with tempfile.TemporaryDirectory() as temp:
            client = LLMClient({**DEFAULTS['openai'], 'api_key': 'test', 'max_retries': 0}, Path(temp)/'ledger.json',
                               transport=lambda p: {'choices': [{'message': {'content': json.dumps(links)}}]})
            with patch('core.semantic_sync.LLMClient', return_value=client):
                result = semantic_alignment(transcript, later, {'openai': {}}, Path(temp)/'ledger.json')
        self.assertEqual(result['offset_seconds'], 300)
        self.assertEqual(result['confidence'], 'low')
        with self.assertRaises(ValueError):
            validate_links({'links': [{'match_id': 'invented', 'commentator_id': '0'}]}, [{'id':'0','time':1,'text':'x'}], [{'id':'0','time':2,'text':'x'}])


class CompositeMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get('FCF_TEST_FFMPEG', 'ffmpeg')
        cls.ffprobe = os.environ.get('FCF_TEST_FFPROBE', 'ffprobe')
        try:
            run_media([cls.ffmpeg, '-version'], timeout=10)
        except RuntimeError:
            raise unittest.SkipTest('FFmpeg not available')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='双路 合成-')
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(database=str(self.root/'db.sqlite3'), output_dir=str(self.root/'out'), cache_dir=str(self.root/'cache'),
                        ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        self.cfg['render'].update(horizontal=[320,180], preview=[320,180], encoder='libx264', fps=10)
        self.cfg['terms'] = []
        self.match, self.commentator = self.root/'比赛 主画面.mp4', self.root/'主播 小窗.mp4'
        for path, colour, length in ((self.match, 'blue', 8), (self.commentator, 'red', 11)):
            run_media([self.ffmpeg, '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i', f'color={colour}:s=320x180:r=10:d={length}',
                       '-f', 'lavfi', '-i', f'sine=frequency=440:duration={length}', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(path)])
        self.document = create_composite(self.match, self.commentator, self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_composition_position_audio_clock_mapping_and_cache(self):
        options = normalize_composite_options({'offset_seconds': 3., 'sync_confirmed': True, 'title': '我的合成标题'})
        result = render_composite(self.document['id'], self.cfg, options, [{'start': 1., 'end': 5.}])
        self.assertTrue(Path(result['video']).name.startswith('我的合成标题'))
        self.assertAlmostEqual(result['duration'], 4., delta=.12)
        self.assertEqual(result['mappings'][0]['commentator_start'], 4.)
        raw = self.root/'frame.rgb'
        run_media([self.ffmpeg, '-v', 'error', '-y', '-i', result['video'], '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', str(raw)])
        frame = np.frombuffer(raw.read_bytes(), dtype=np.uint8).reshape(180,320,3)
        self.assertGreater(frame[160,20,0], 200)  # red streamer, lower left
        self.assertGreater(frame[20,200,2], 200)  # blue match elsewhere
        run_media([self.ffmpeg, '-v', 'error', '-i', result['video'], '-f', 'null', '-'])
        stat = Path(result['video']).stat().st_mtime_ns
        again = render_composite(self.document['id'], self.cfg, options, [{'start': 1., 'end': 5.}])
        self.assertEqual(again, result)
        self.assertEqual(Path(result['video']).stat().st_mtime_ns, stat)
        with CompositeStore(self.cfg['database']) as db:
            self.assertEqual(len(db.get_composite(self.document['id'])['outputs']), 1)

    def test_muting_match_retains_streamer_or_produces_complete_silence(self):
        for audio in ('mix','match'):
            options=normalize_composite_options({'audio':audio,'match_audio_mode':'mute'})
            with patch('core.vocal_remover.prepare_background',side_effect=AssertionError('mute needs no AI')):
                result=render_composite(self.document['id'],self.cfg,options,preview=True)
            decoded=self.root/f'{audio}.wav'
            run_media([self.ffmpeg,'-v','error','-y','-i',result['video'],'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(decoded)])
            with wave.open(str(decoded),'rb') as wav:
                samples=np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').astype(float)
            rms=float(np.sqrt(np.mean(samples**2)))
            self.assertGreater(len(samples),7*16000)
            if audio=='mix':self.assertGreater(rms,100.)
            else:self.assertLess(rms,1.)

    def test_ai_dependency_failure_does_not_restore_unwanted_match_audio(self):
        options=normalize_composite_options({'audio':'mix','match_audio_mode':'remove_vocals'})
        with patch('core.vocal_remover.prepare_background',side_effect=RuntimeError('missing test model')):
            with self.assertRaisesRegex(RuntimeError,'missing test model'):
                render_composite(self.document['id'],self.cfg,options,preview=True)

    def test_confirmation_negative_offset_and_multiple_ranges(self):
        with self.assertRaises(ValueError):
            render_composite(self.document['id'], self.cfg)
        options = normalize_composite_options({'offset_seconds': -2., 'sync_confirmed': True, 'audio': 'mix'})
        result = render_composite(self.document['id'], self.cfg, options, [{'start': 2., 'end': 3.}, {'start': 5., 'end': 7.}])
        self.assertAlmostEqual(result['duration'], 3., delta=.15)
        self.assertEqual(result['mappings'][1]['output_start'], 1.)
        self.assertEqual(result['mappings'][0]['commentator_start'], 0.)

    def test_face_sendcmd_and_no_face_decode_fallback(self):
        from core.face_tracker import track_faces
        meta = self.document['commentator']
        track = track_faces(meta, 0, 2, self.cfg, self.root)
        self.assertEqual(track['mode'], 'fit')
        fake = {'mode': 'face', 'crop_width': 160, 'crop_height': 90,
                'points': [{'time':0.,'x':0,'y':0}, {'time':.5,'x':100,'y':80}]}
        with patch('core.compositor.track_faces', return_value=fake):
            result = render_composite(self.document['id'], self.cfg, normalize_composite_options({'layout':'face'}), preview=True, preview_start=0)
        self.assertTrue(Path(result['video']).is_file())

    def test_source_mutation_detected_and_audit_preserves_options(self):
        with CompositeStore(self.cfg['database']) as db:
            new = normalize_composite_options({'title': '用户刚刚修改'})
            db.update_composite(self.document['id'], options=new)
            db.update_composite(self.document['id'], sync={'confidence':'low'})
            self.assertEqual(db.get_composite(self.document['id'])['options'], new)
        self.match.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            render_composite(self.document['id'], self.cfg, preview=True)

    def test_nvenc_failure_falls_back_to_real_software_render(self):
        self.cfg['render']['encoder'] = 'h264_nvenc'
        attempts = []
        def run(command, **kwargs):
            if '-c:v' in command:
                encoder = command[command.index('-c:v')+1]
                attempts.append(encoder)
                if encoder == 'h264_nvenc':
                    raise RuntimeError('simulated unavailable encoder')
            return run_media(command, **kwargs)
        with patch('core.compositor.run_media', side_effect=run):
            result = render_composite(self.document['id'], self.cfg, preview=True)
        self.assertEqual(attempts, ['h264_nvenc','libx264'])
        self.assertTrue(Path(result['video']).is_file())

    def test_silent_match_can_compose_and_asr_failure_keeps_audio_result(self):
        self.cfg['openai'].update(api_key='unit-secret-key',model='test')
        silent = self.root/'无声比赛.mp4'
        run_media([self.ffmpeg,'-v','error','-y','-i',str(self.match),'-map','0:v:0','-c','copy',str(silent)])
        document = create_composite(silent,self.commentator,self.cfg)
        result = render_composite(document['id'],self.cfg,preview=True)
        self.assertTrue(Path(result['video']).is_file())
        with self.assertRaises(ValueError):
            synchronize_composite(document['id'],self.cfg)
        with patch('core.compositor._transcript',side_effect=RuntimeError('test ASR unavailable')):
            result = synchronize_composite(self.document['id'],self.cfg,semantic=True)
        self.assertIn('test ASR unavailable',result['semantic_error'])
        with CompositeStore(self.cfg['database']) as db:
            self.assertEqual(db.get_composite(self.document['id'])['sync'], result)


class CompositeAudioTests(unittest.TestCase):
    def test_optional_vocal_cache_frames_resume_without_model(self):
        from core.vocal_remover import prepare_background, RATE
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            cfg={'cache_dir':str(root),'ffmpeg':'not-used'}
            meta={'source_signature':{'path':'mock'},'selected_audio':1,'duration':65.,'source':'mock','timeline_origin':0.,'format_start':0.}
            class Separator:
                device='cpu'
                def __init__(self,*args):pass
                def separate(self,a):return a*.5
            def fake_extract(command):
                from types import SimpleNamespace
                import re
                expression=command[command.index('-af')+1]
                seconds=float(re.findall(r'atrim=duration=([\d.]+)',expression)[-1])
                with wave.open(command[-1],'wb') as wav:
                    wav.setparams((2,2,RATE,0,'NONE',''))
                    wav.writeframes(np.full((round(seconds*RATE),2),1000,dtype='<i2').tobytes())
                return SimpleNamespace()
            with patch('core.vocal_remover.BackgroundSeparator',Separator),patch('core.vocal_remover.run_media',side_effect=fake_extract) as runner:
                path,info=prepare_background(meta,29.,32.,cfg,root)
                self.assertEqual(runner.call_count,2)
                with wave.open(str(path),'rb') as wav:
                    self.assertEqual(wav.getnframes(),3*RATE)
                again,_=prepare_background(meta,29.,32.,cfg,root)
                self.assertEqual(path,again)
                self.assertEqual(runner.call_count,2)
                # Overlapping range reuses the same fixed source chunks.
                prepare_background(meta,28.,31.,cfg,root)
                self.assertEqual(runner.call_count,2)

    def test_shared_wave_offset_precision_and_nonmatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rng = np.random.default_rng(22)
            rate, duration, offset = 16000, 70, 7.35
            t = np.arange(rate * duration)/rate
            # Rapidly changing spectral events, rather than a periodic tone.
            freq = rng.uniform(120, 3500, duration*10)[np.minimum((t*10).astype(int), duration*10-1)]
            signal = np.sin(2*np.pi*np.cumsum(freq)/rate)*.3
            shifted = np.r_[rng.normal(0,.02,round(offset*rate)), signal*.7 + rng.normal(0,.02,len(signal))]
            def save(name, values):
                path = root/name
                with wave.open(str(path), 'wb') as wav:
                    wav.setparams((1,2,rate,0,'NONE','not compressed'))
                    wav.writeframes((np.clip(values,-1,1)*32767).astype('<i2').tobytes())
                return path
            a,b = save('a.wav', signal),save('b.wav',shifted)
            result = match_audio(a,b,root/'positive')
            self.assertAlmostEqual(result['offset_seconds'], offset, delta=.1)
            self.assertIn(result['confidence'], ('high','medium'))
            silence = save('silent.wav', np.zeros(rate*40))
            self.assertIsNone(match_audio(silence,silence,root/'silent')['offset_seconds'])
