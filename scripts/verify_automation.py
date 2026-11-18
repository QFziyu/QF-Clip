"""Three real loopback HTTP services + actual HLS/FFmpeg, fixture source/ASR/LLM."""
from __future__ import annotations
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from unittest.mock import patch
from urllib import request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT.parent/'FootballWorkflow'))
LIGHTBOAT=ROOT.parent/'软件下载/轻舟视频下载'
sys.path.insert(0,str(LIGHTBOAT))
from core.config import load_config
from core.local_api import make_server
from core.production import FactoryService,ProductionStore
from core.jobs import _connection,_execute,_result_error
from core.video_input import run_media,probe_video
from core.workflow import active_variant,edit_item,export_snapshot
from core.utils import atomic_json
from core.export_options import normalize_export_options
from flowhub.service import Hub,serve,Client
import engine


class Response(io.BytesIO):
    def __init__(self,data):super().__init__(data);self.headers={'Content-Length':str(len(data))};self.status=200


class MediaTransport:
    def __init__(self,root):self.root=root
    def manifest(self,url):return (self.root/'index.m3u8').read_text(),'https://fixture.example/index.m3u8'
    def open(self,url,byte_range=None):return Response((self.root/url.rsplit('/',1)[-1]).read_bytes())


def run_queued(cfg):
    with _connection(cfg['database']) as db:
        row=db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:return False
        job=dict(row)
        db.execute("UPDATE jobs SET status='running',worker_pid=?,attempt=attempt+1 WHERE id=?",(os.getpid(),job['id']))
    result=_execute(job['kind'],json.loads(job['payload_json']),cfg)
    if _result_error(result):raise AssertionError(result)
    with _connection(cfg['database']) as db:
        db.execute("UPDATE jobs SET status='completed',result_json=? WHERE id=?",(json.dumps(result,ensure_ascii=False),job['id']))
    return True


def verify(destination=None,long=False):
    root=Path(destination or ROOT/('output/automation_upgrade/long_media' if long else 'output/automation_upgrade/media')).resolve()
    if (root/'factory.sqlite3').exists():root=ROOT/'output/automation_runs'/(('l-' if long else 's-')+uuid.uuid4().hex[:8])
    root.mkdir(parents=True,exist_ok=True)
    cfg=load_config();cfg.update(database=str(root/'factory.sqlite3'),output_dir=str(root/'output'),cache_dir=str(root/'cache'))
    cfg['selection'].update(mode='semantic',audio_events=False,min_seconds=1,context_before=0,context_after=0)
    cfg['render'].update(encoder='libx264',horizontal=[320,180],vertical=[180,320],preview=[240,136],fps=10,bitrate='350k')
    cfg['subtitle'].update(font_size=18,margin_v=10)
    media=root/'hls';media.mkdir(exist_ok=True)
    duration=3660 if long else 18
    size='64x36' if long else '320x180'
    source_fps=1 if long else 10
    if not (media/'index.m3u8').is_file():
        run_media([cfg['ffmpeg'],'-v','error','-y','-f','lavfi','-i',f'testsrc2=size={size}:rate={source_fps}:duration={duration}',
            '-f','lavfi','-i',f'sine=frequency=440:sample_rate=8000:duration={duration}','-c:v','libx264','-preset','ultrafast',
            '-g','60' if long else '30','-c:a','aac','-b:a','16k','-f','hls','-hls_time','60' if long else '3','-hls_playlist_type','vod',str(media/'index.m3u8')])
    transcript=root/'fixture_transcript.json'
    atomic_json(transcript,{'segments':[{'start':1.,'end':6.,'text':'这是测试用足球观点，球员表现非常精彩。'},
        {'start':8.,'end':13.,'text':'第二个测试观点，讨论战术安排。'}]})
    intro=root/'片头.mp4'
    if not intro.exists():run_media([cfg['ffmpeg'],'-v','error','-y','-f','lavfi','-i','color=c=blue:size=320x180:rate=10:duration=1',
        '-f','lavfi','-i','anullsrc=sample_rate=16000:channel_layout=mono','-t','1','-c:v','libx264','-c:a','aac',str(intro)])
    lb_runtime=root/'lightboat';lb_runtime.mkdir(exist_ok=True)
    (lb_runtime/'config.json').write_text(json.dumps({'token':'fixture-lightboat-token','output_dir':str(root/'downloads')}),encoding='utf-8')
    env={'LIGHTBOAT_RUNTIME_ROOT':str(lb_runtime),'FFMPEG_PATH':cfg['ffmpeg'],'FFPROBE_PATH':cfg['ffprobe']}
    with patch.dict(os.environ,env),patch('core.jobs.ensure_worker',return_value=False),patch('core.production.ensure_worker',return_value=False):
        spec=importlib.util.spec_from_file_location('fixture_lightboat_server',LIGHTBOAT/'server.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        manager=module.AUTO
        manager.resolver=lambda url: {'url':'https://fixture.example/index.m3u8','kind':'hls','headers':{}}
        manager.downloader=lambda job,capture,output:engine.download(job,capture,output,ffmpeg=cfg['ffmpeg'],transport=MediaTransport(media))
        factory=FactoryService(cfg,[root/'downloads'])
        template=factory.save_template({'name':'验收短片','settings':{'form':'shorts','editing_options':{'count':2,'min_seconds':1,'target_seconds':5,'max_seconds':16}},
            'packaging':{'subtitles':True,'export_options':normalize_export_options({'subtitle':cfg['subtitle'],'intro':{'enabled':True,'path':str(intro)}})},
            'export':{'orientations':['horizontal','vertical'],'render':{k:cfg['render'][k] for k in ('horizontal','vertical','encoder','fps','bitrate')},'output_dir':cfg['output_dir']}})
        lb_server=module.ThreadingHTTPServer(('127.0.0.1',0),module.Handler)
        factory_server=make_server(0,'fixture-factory-token',factory.dispatch,'factory')
        config={'profile':'gpu','lightboat':{'url':f'http://127.0.0.1:{lb_server.server_port}','token':'fixture-lightboat-token'},
            'factory':{'url':f'http://127.0.0.1:{factory_server.server_port}','token':'fixture-factory-token'}}
        hub=Hub(root/'flows.sqlite3',config)
        hub_server=serve(hub,0,'fixture-hub-token')
        servers=[lb_server,factory_server,hub_server]
        threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in servers]
        for thread in threads:thread.start()
        def api(path,data=None):
            req=request.Request(f'http://127.0.0.1:{hub_server.server_port}/api/v1/'+path,
                data=json.dumps(data).encode() if data is not None else None,headers={'X-Workflow-Token':'fixture-hub-token','Content-Type':'application/json'})
            with request.build_opener(request.ProxyHandler({})).open(req,timeout=60) as response:return json.load(response)
        try:
            request_data={'source_url':'https://ymx168.top/live/1?recordId=2','template_id':template['id'],'template_revision':template['revision'],
                'idempotency_key':'fixture-three-http-v1','title':'自动工作流 中文验收'}
            flow=api('flows',request_data)
            assert api('flows',request_data)['id']==flow['id']
            for _ in range(120):
                doc=hub.get(flow['id']);doc['next_poll']=0;hub.save(doc);hub.tick();manager.tick()
                for view in [hub.get(flow['id'])]:
                    if view.get('production_id'):
                        with ProductionStore(cfg['database']) as db:
                            p=db.production(view['production_id'])
                            if not p['prepare_job']:
                                # Explicit fixture injection, never accepted by a production HTTP template.
                                p['settings'].update(mock=True,transcript_path=str(transcript))
                                w=db.get_workspace(p['workspace_id']);w['source_options']=p['settings'];w=db.save_workspace(w)
                                p['prepare_revision']=w['revision'];db.save_production(p)
                factory.advance()
                while run_queued(cfg):pass
                if hub.get(flow['id'])['status']=='awaiting_review':break
                if hub.get(flow['id'])['status']=='failed':raise AssertionError(hub.get(flow['id'])['message'])
                time.sleep(.1)
            doc=api('flows/'+flow['id']);assert doc['status']=='awaiting_review',doc['status']
            with _connection(cfg['database']) as db:assert db.execute("SELECT count(*) FROM jobs WHERE kind='workflow_export'").fetchone()[0]==0
            with ProductionStore(cfg['database']) as db:w=db.get_workspace(doc['workspace_id'])
            items=active_variant(w)['items'];assert items
            for index,item in enumerate(items):
                edit_item(w['id'],item['id'],{'title':f'人工审核最终标题 {index+1}','status':'approved',
                    'subtitle_text':'1\n00:00:00,100 --> 00:00:01,000\n自动流程中的人工字幕\n'},cfg)
            with ProductionStore(cfg['database']) as db:w=db.get_workspace(w['id'])
            snapshot=export_snapshot(w,cfg,['horizontal','vertical'],template['export']['render'],cfg['output_dir'])
            confirmed=api('flows/'+flow['id']+'/confirm',{'snapshot':snapshot})
            again=api('flows/'+flow['id']+'/confirm',{'snapshot':snapshot})
            assert len(again['production']['confirmations'])==1
            factory.advance()
            while run_queued(cfg):pass
            factory.advance();doc=hub.get(flow['id']);doc['next_poll']=0;hub.save(doc);hub.tick()
            final=api('flows/'+flow['id']);assert final['status']=='completed'
            outputs=final['outputs']
            for output in outputs:
                assert '人工审核最终标题' in Path(output['video']).name
                meta=probe_video(output['video'],cfg)
                run_media([cfg['ffmpeg'],'-v','error','-i',output['video'],'-f','null','-'])
                assert meta['width']==cfg['render'][output['orientation']][0]
                assert '自动流程中的人工字幕' in Path(output['subtitles']['srt']).read_text(encoding='utf-8-sig')
            # Recreate the coordinator from its database; it must retain the same production.
            recovered=Hub(root/'flows.sqlite3',config)
            assert recovered.get(flow['id'])['production_id']==final['production_id']
            report={'success':True,'evidence_root':str(root),'source_seconds':duration,'scope':'Three actual HTTP services; generated HLS source, supplied transcript, mock LLM. Real FFmpeg download/mux/preview/render/full decode.',
                'flow_id':flow['id'],'candidates':len(items),'videos':len(outputs),'exports':outputs,
                'approval_gate':True,'duplicate_submit_deduplicated':True,'duplicate_confirmation_deduplicated':True,'coordinator_restart':True}
            atomic_json(root/'summary.json',report)
            atomic_json(ROOT/'output/automation_upgrade'/('latest_long_media.json' if long else 'latest_media.json'),report)
            print(json.dumps({k:v for k,v in report.items() if k!='exports'},ensure_ascii=False,indent=2))
            return report
        finally:
            manager.close()
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(5)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--long',action='store_true');args=parser.parse_args()
    verify(long=args.long)
