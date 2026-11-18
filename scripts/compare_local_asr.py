"""Re-render a saved single-source edit with local ASR, preserving its original.

Creates a separate review workspace and full-duration preview, never approves,
publishes, or calls LLM. Reuses the canonical extracted audio when available.
"""
from pathlib import Path
import argparse
import copy
import json
import logging
import shutil
import sys
import time
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.local_asr import ensure_service
from core.transcriber import transcribe_audio, asr_cache_key
from core.utils import atomic_json, fingerprint, file_signature, safe_filename_stem
from core.workflow import active_variant, preview_workflow
from core.video_input import probe_video, run_media
from storage.workspaces import WorkspaceStore


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    args=parser.parse_args()
    cfg=load_config();cfg['asr']['engine']='zipformer_http';cfg['subtitle_review']['enabled']=False
    ensure_service(cfg)
    with WorkspaceStore(cfg['database']) as db:
        original=db.get_workspace(args.workspace)
    if not original or not active_variant(original):raise ValueError('没有已保存的剪辑方案')
    variant=active_variant(original)
    items=[copy.deepcopy(i) for i in variant['items'] if i['status']!='rejected']
    if not items or any(i['source']['kind']!='single' or i['source']['meta'].get('virtual_timeline') for i in items):
        raise ValueError('本对照命令仅适用于已保存的单路录像剪辑范围')
    identity='local-asr-'+fingerprint({'workspace':args.workspace,'variant':variant['id'],
        'items':[(i['id'],i['segments']) for i in items],'asr':[asr_cache_key(i['source']['meta'],cfg) for i in items]})
    root=Path(cfg['cache_dir'])/'local_asr_comparisons'/identity
    root.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=str(root/'run.log'),level=logging.INFO,format='%(asctime)s %(message)s',encoding='utf-8')
    progress={'workspace_id':identity,'original_workspace':args.workspace,'stage':'transcribing','llm_calls':0}
    atomic_json(root/'progress.json',progress)
    print(json.dumps({'workspace':identity,'log':str(root/'run.log'),'stage':'transcribing'},ensure_ascii=False),flush=True)
    cfg['render'].update(preview=[1280,720],preview_bitrate='2M')
    begin=time.perf_counter();projects={}
    # ASR-only comparison: no model selection, automatic title, or proofreading.
    with patch('core.llm_client.LLMClient.complete',side_effect=AssertionError('No LLM in local ASR comparison')):
        for item in items:
            source=item['source'];meta=copy.deepcopy(source['meta'])
            signature=file_signature(meta['source'])
            if signature!=meta.get('source_signature'):raise ValueError('原始录像身份已经变化')
            project_id='local-asr-'+fingerprint({'source':signature,'asr':asr_cache_key(meta,cfg)})
            directory=root/'transcripts'/project_id
            directory.mkdir(parents=True,exist_ok=True)
            if project_id not in projects:
                wav=Path(source['project_dir'])/'audio.wav'
                if not wav.is_file():
                    from core.audio_extract import extract_audio
                    wav=extract_audio(meta,directory/'audio.wav',cfg)
                with wave.open(str(wav)) as audio:
                    if abs(audio.getnframes()/audio.getframerate()-meta['duration'])>.1:
                        raise ValueError('缓存音频时长与原视频时间轴不匹配')
                transcript=transcribe_audio(wav,meta,cfg,directory)
                key=fingerprint(transcript)
                atomic_json(directory/'transcripts'/f'{key}.json',transcript)
                meta['transcript_key']=key
                project={'id':project_id,'source':meta['source'],'name':original['name']+' · 本地中文转录',
                    'duration':meta['duration'],'project_dir':str(directory),'status':'transcribed','metadata':meta}
                with WorkspaceStore(cfg['database']) as db:db.upsert_project(project)
                projects[project_id]=project
            project=projects[project_id]
            item.update(id=identity[:22]+'-'+fingerprint(item['id'])[:8],status='pending',revision=0,
                        subtitle_text=None,subtitle_stale=False,transcript_key=project['metadata']['transcript_key'])
            # Manual subtitles remain on the original; the comparison intentionally
            # uses raw local recognition so its quality can be inspected.
            for field in ('subtitle_review','legacy_outputs'):item.pop(field,None)
            item['source'].update(project_id=project_id,project_dir=project['project_dir'],meta=project['metadata'])
        with WorkspaceStore(cfg['database']) as db:
            w=db.get_workspace(identity)
            if not w:
                w=db.create_workspace(original['name']+' · 本地中文字幕对照',identity)
                v=copy.deepcopy(variant);v.update(id=identity+'-variant',name='原剪辑范围 · Zipformer 中文字幕',items=items)
                w.update(assets=copy.deepcopy(original['assets']),source_options=copy.deepcopy(original['source_options']),
                    variants=[v],active_variant=v['id'],step=3,status='待审核',
                    packaging={**copy.deepcopy(original.get('packaging') or {}),'subtitles':True,'proofread_subtitles':False},
                    comparison={'original_workspace':args.workspace,'engine':'zipformer_http','reuse_edit_ranges':True})
                w=db.save_workspace(w)
            items=active_variant(w)['items']
        progress.update(stage='rendering',transcription_seconds=time.perf_counter()-begin)
        atomic_json(root/'progress.json',progress)
        print(json.dumps(progress,ensure_ascii=False),flush=True)
        output=Path(cfg['output_dir'])/'本地ASR字幕对照'/identity
        output.mkdir(parents=True,exist_ok=True);results=[]
        for item in items:
            result=preview_workflow(identity,item['id'],cfg)
            source_video=Path(result['video'])
            target=output/(safe_filename_stem(item['title'])+' · 本地中文字幕.mp4')
            if not target.is_file() or target.stat().st_size!=source_video.stat().st_size:
                shutil.copy2(source_video,target)
            for extension in ('srt','ass'):
                shutil.copy2(result['subtitles'][extension],target.with_suffix('.'+extension))
            mapping={**result,'video':str(target),'comparison_workspace':identity,'llm_calls':0}
            atomic_json(target.with_suffix('.timeline.json'),mapping)
            meta=probe_video(target,cfg)
            if abs(meta['duration']-result['duration'])>.15:raise ValueError('对照成品时长不匹配')
            run_media([cfg['ffmpeg'],'-hide_banner','-v','error','-nostdin','-i',str(target),'-f','null','-'])
            results.append({'video':str(target),'srt':str(target.with_suffix('.srt')),'duration':meta['duration']})
        with WorkspaceStore(cfg['database']) as db:
            if db.get_workspace(args.workspace)!=original:raise ValueError('对照期间原项目被修改，请核对；没有覆盖原项目')
            db.preference('current_workspace',identity)
        progress.update(stage='complete',results=results,total_seconds=time.perf_counter()-begin)
        atomic_json(root/'progress.json',progress)
        print(json.dumps(progress,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
