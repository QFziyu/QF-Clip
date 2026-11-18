"""Offline contract/recovery tests against a real loopback HTTP stub; no model/API."""
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from core.config import DEFAULTS, load_config
from core.local_asr import ZipformerHTTP, LocalASRError, MODEL_REVISION, MODEL_SHA256, validate_url
from core.asr_settings import save_settings
from core.subtitle import transcript_cues, parse_srt, cues_srt
from core.transcriber import transcribe_audio, asr_cache_key
from core.utils import fingerprint

HEALTH = {'service':'zipformer-small-zh-cpu','provider':'cpu', 'api_version':2,
          'model_revision':MODEL_REVISION,'model_sha256':MODEL_SHA256,'capabilities':['ctc_timestamps']}


@contextmanager
def service(results, health=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,status,value):
            body=json.dumps(value).encode()
            self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self): self.reply(200, HEALTH if health is None else health)
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            with wave.open(io.BytesIO(body)) as wav: duration=wav.getnframes()/wav.getframerate()
            calls.append((duration,dict(self.headers)))
            result=results.pop(0)
            if isinstance(result,int): self.reply(result,{'error':'test failure'});return
            self.reply(200,dict(model_revision=MODEL_REVISION, model_sha256=MODEL_SHA256,
                timestamp_method='ctc-byte-bpe-onsets-estimated-ends-v1',**result))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield 'http://127.0.0.1:'+str(server.server_port),calls
    finally: server.shutdown();server.server_close();thread.join()


def response(start, end, text='曼城'):
    return {'text':text,'segments':[{'start':start,'end':end,'text':text,
            'words':[{'start':start,'end':end,'word':text}]}]}


class LocalASRTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.wav=self.root/'中文 测试.wav'
        with wave.open(str(self.wav),'wb') as wav:
            wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*16000*20)
        self.cfg=copy.deepcopy(DEFAULTS);self.cfg['terms']=[]
        self.cfg['asr'].update(engine='zipformer_http',local_chunk_seconds=10,overlap_seconds=2)
        self.meta={'source':'D:/录制/中文 视频.mp4','duration':20,'selected_audio':2,'timeline_origin':5,
                   'source_signature':{'test':'unchanged'}}

    def tearDown(self): self.tmp.cleanup()

    def test_interruption_resume_cache_no_whisper_or_cloud(self):
        with service([response(.3,1),500]) as (url,calls):
            self.cfg['asr']['local_url']=url
            with patch('core.transcriber._make_model',side_effect=AssertionError('No Whisper')):
                with self.assertRaisesRegex(RuntimeError,'2/2'):
                    transcribe_audio(self.wav,self.meta,self.cfg,self.root)
                first=list((self.root/'asr').glob('*/chunk_0000.json'))[0]
                saved=first.read_bytes(),first.stat().st_mtime_ns
        self.assertFalse((self.root/'transcript.json').exists())
        self.assertTrue(first.exists()); self.assertEqual(saved,(first.read_bytes(),first.stat().st_mtime_ns))

    def test_resume_same_endpoint_and_final_cache_without_service(self):
        results=[response(.3,1),500]
        with service(results) as (url,calls):
            self.cfg['asr']['local_url']=url
            with self.assertRaises(RuntimeError): transcribe_audio(self.wav,self.meta,self.cfg,self.root)
            results.append(response(2.2,3.1,'换人'))
            data=transcribe_audio(self.wav,self.meta,self.cfg,self.root)
            self.assertEqual(len(calls),3)
            self.assertEqual(''.join(s['text'] for s in data['segments']),'曼城换人')
            self.assertAlmostEqual(data['segments'][1]['start'],10.2)
            self.assertEqual(data['provenance'],'zipformer-local-cpu')
            self.assertEqual(data['runtime']['device'],'cpu')
            self.assertEqual(data['timeline_origin'],5)
            self.assertEqual(calls[0][0],12)
            self.assertTrue(all(h.get('X-Asr-Response-Format')=='verbose_json' for _,h in calls))
            self.assertTrue(all('Authorization' not in h for _,h in calls))
            cues=transcript_cues(data,{'start':10,'end':15})
            self.assertAlmostEqual(cues[0]['start'],.2)
            self.assertTrue(parse_srt(cues_srt(cues),5))
        with patch('core.local_asr.ZipformerHTTP',side_effect=AssertionError('cache does not contact service')):
            self.assertEqual(data,transcribe_audio(self.wav,self.meta,self.cfg,self.root))

    def test_reject_text_only_wrong_model_invalid_timestamps(self):
        for result in ({'text':'文字'},response(0,float('nan')),response(0,100),
                       {'text':'different','segments':response(0,1)['segments']}):
            with self.subTest(result=result),service([result]) as (url,_):
                with self.assertRaises((ValueError,RuntimeError)):
                    ZipformerHTTP(dict(self.cfg['asr'],local_url=url)).transcribe(self.wav)
        with service([],health={'service':'other'}) as (url,_):
            with self.assertRaises(LocalASRError): ZipformerHTTP(dict(self.cfg['asr'],local_url=url))

    def test_cache_identity_separates_track_model_and_engine_and_keeps_legacy(self):
        old=copy.deepcopy(self.cfg);old['asr'].update(engine='faster_whisper')
        legacy={k:v for k,v in old['asr'].items() if k!='engine' and not k.startswith('local_')}
        key=fingerprint({'asr':legacy,'terms':[],'source':self.meta['source'],'duration':20.,'track':2,
                         'source_signature':self.meta['source_signature'],'version':2})
        self.assertEqual(asr_cache_key(self.meta,old),key)
        self.assertNotEqual(key,asr_cache_key(self.meta,self.cfg))
        self.assertNotEqual(asr_cache_key(self.meta,self.cfg),asr_cache_key(dict(self.meta,selected_audio=1),self.cfg))
        changed=copy.deepcopy(self.cfg);changed['asr']['model']='unused-whisper-model'
        self.assertEqual(asr_cache_key(self.meta,self.cfg),asr_cache_key(self.meta,changed))
        changed['asr'].update(local_url='http://127.0.0.1:9000',local_timeout=300)
        self.assertEqual(asr_cache_key(self.meta,self.cfg),asr_cache_key(self.meta,changed))

    def test_local_url_rejects_remote_credentials_redirect_like_paths(self):
        for url in ('https://example.com','http://example.com','http://127.0.0.1/path',
                    'http://user:pass@localhost','http://localhost:bad','http://localhost/?token=x'):
            with self.assertRaises(ValueError): validate_url(url)

    def test_settings_persist_without_touching_api_or_whisper_settings(self):
        env_path=self.root/'.env';env_path.write_text('WHISPER_MODEL=medium\nDEVICE=cuda\nOPENAI_API_KEY=test-only\n')
        before=env_path.read_bytes()
        with patch('core.config.ROOT',self.root),patch.dict(os.environ,{},clear=True):
            cfg=load_config()
            save_settings(cfg,'zipformer_http','http://127.0.0.1:8765',False)
            current=load_config()
            self.assertEqual(current['asr']['engine'],'zipformer_http')
            self.assertEqual(current['asr']['device'],'cuda')
            self.assertFalse(current['subtitle_review']['enabled'])
            save_settings(current,'faster_whisper','http://127.0.0.1:8765',True)
            self.assertEqual(load_config()['asr']['engine'],'faster_whisper')
            self.assertEqual(env_path.read_bytes(),before)
            self.assertNotIn('test-only',(self.root/'.env.asr').read_text(encoding='utf-8'))

    def test_settings_ui_switch_save_reload_and_connect(self):
        from streamlit.testing.v1 import AppTest
        keep={k:v for k,v in os.environ.items() if k in ('USERPROFILE','HOMEDRIVE','HOMEPATH','SystemRoot','TEMP','TMP','PATH')}
        with patch('core.config.ROOT',self.root),patch.dict(os.environ,keep,clear=True),service([]) as (url,_):
            app=AppTest.from_string('from core.config import load_config\nfrom ui.asr_settings import panel\npanel(load_config())').run()
            self.assertEqual(len(app.exception),0)
            app.selectbox[0].set_value('zipformer_http')
            app.text_input[0].input(url)
            app.checkbox[0].uncheck()
            next(b for b in app.button if b.label=='保存语音识别设置').click().run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(load_config()['asr']['engine'],'zipformer_http')
            next(b for b in app.button if b.label=='检查本地中文 ASR 连接').click().run()
            self.assertEqual(len(app.error),0)
            self.assertTrue(any('已连接' in s.value for s in app.success))

    def test_composite_does_not_reuse_old_whisper_in_local_mode(self):
        from core.compositor import _transcript
        from storage.composites import CompositeStore
        self.cfg['database']=str(self.root/'db.sqlite3')
        project_root=self.root/'prior';project_root.mkdir()
        (project_root/'transcript.json').write_text(json.dumps({'segments':[], 'provenance':'faster-whisper'}))
        with CompositeStore(self.cfg['database']) as db:
            db.upsert_project({'id':'prior','source':self.meta['source'],'name':'prior','duration':20,
                'project_dir':str(project_root),'status':'transcribed','metadata':self.meta})
        with patch('core.transcriber.transcribe_audio',return_value={'local':True}) as run:
            self.assertEqual(_transcript(self.meta,self.wav,self.root,self.cfg,False),{'local':True})
            run.assert_called_once()


if __name__=='__main__': unittest.main()
