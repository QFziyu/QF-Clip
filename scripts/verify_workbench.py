"""Reproducible FFmpeg acceptance with generated footage, no API or ASR download."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.utils import atomic_json
from core.video_input import run_media, probe_video
from core.workflow import (import_assets,prepare_workflow,active_variant,edit_item,export_snapshot,
                           run_workflow_export,preview_workflow)
from core.export_options import normalize_export_options
from core.composite_options import normalize_composite_options
from core.subtitle import parse_srt
from storage.workspaces import WorkspaceStore


def verify(destination=None):
    root=Path(destination or ROOT/'output/workbench_upgrade/media').resolve();root.mkdir(parents=True,exist_ok=True)
    cfg=load_config()
    cfg.update(database=str(root/'acceptance.sqlite3'),cache_dir=str(root/'cache'),output_dir=str(root/'output'))
    cfg['selection'].update(mode='semantic',audio_events=False,context_before=0,context_after=0,min_seconds=1)
    cfg['render'].update(encoder='libx264',horizontal=[320,180],vertical=[180,320],preview=[240,136],fps=10,bitrate='450k')
    cfg['subtitle'].update(font_size=18,vertical_font_size=18,margin_v=10,vertical_margin_v=15)
    assets=[]
    for name,duration,color in [('比赛 中文 测试',18,'testsrc2'),('主播 第二路',23,'testsrc2'),('自选片头',1.2,'color=c=red')]:
        path=root/(name+'.mp4')
        if not path.is_file():
            run_media([cfg['ffmpeg'],'-v','error','-nostdin','-y','-f','lavfi','-i',f'{color}:size=320x180:rate=10:duration={duration}' if color!='testsrc2' else f'testsrc2=size=320x180:rate=10:duration={duration}',
                       '-f','lavfi','-i',f'sine=frequency=440:sample_rate=48000:duration={duration}',
                       '-c:v','libx264','-preset','ultrafast','-c:a','aac',str(path)])
        assets.append(path)
    transcript=root/'测试转录.json';commentary=root/'主播转录.json'
    texts=[{'start':1.,'end':3.,'text':'这是人工生成的足球观点测试。'},
           {'start':9.,'end':12.,'text':'第二段讨论战术与球员表现。'}]
    atomic_json(transcript,{'segments':texts})
    atomic_json(commentary,{'segments':[{**s,'start':s['start']+5,'end':s['end']+5} for s in texts]})
    records=[]
    for organization,form in [('single','shorts'),('single','continuous'),('single','montage'),('sequence','full'),('dual','full'),('dual','shorts')]:
        label=organization+'-'+form
        with WorkspaceStore(cfg['database']) as store:w=store.create_workspace('合成验收 '+label)
        imported=assets[:2] if organization in ('dual','sequence') else assets[:1]
        w=import_assets(w['id'],[str(p) for p in imported],cfg)
        settings=dict(organization=organization,form=form,asset_ids=[a['id'] for a in w['assets']],
                      transcript_path=str(commentary if organization=='dual' else transcript),mock=True,allow_cpu=True,
                      editing_options={'format':form if form!='full' else 'shorts','count':1,'min_seconds':1,'target_seconds':5,'max_seconds':16})
        if form=='full': settings.pop('editing_options')
        if organization=='dual':
            settings.update(match_id=w['assets'][0]['id'],commentator_ids=[w['assets'][1]['id']],
                            composite_options=normalize_composite_options({'offset_seconds':5.,'sync_confirmed':True}))
        prepare_workflow(w['id'],settings,cfg)
        with WorkspaceStore(cfg['database']) as store:w=store.get_workspace(w['id'])
        variant=active_variant(w)
        if not variant['items']:raise AssertionError('未生成候选：'+label)
        item=variant['items'][0]
        segments=[{'start':1.,'end':3.},{'start':9.,'end':12.}] if form=='montage' else item['segments']
        edit_item(w['id'],item['id'],dict(title='人工修改标题 '+label,segments=segments,status='approved',
                  subtitle_text='1\n00:00:00,100 --> 00:00:01,000\n验收人工字幕\n'),cfg)
        with WorkspaceStore(cfg['database']) as store:
            w=store.get_workspace(w['id'])
            w['packaging']={'subtitles':True,'export_options':normalize_export_options({'subtitle':cfg['subtitle'],'intro':{'enabled':True,'path':str(assets[2])}})}
            w=store.save_workspace(w)
        preview=preview_workflow(w['id'],item['id'],cfg)
        render={k:cfg['render'][k] for k in ('horizontal','vertical','fps','bitrate','encoder')}
        snapshot=export_snapshot(w,cfg,['horizontal','vertical'],render,cfg['output_dir'])
        result=run_workflow_export(w['id'],snapshot,cfg)
        if result['errors']:raise AssertionError(result['errors'])
        for output in result['exports']:
            video=Path(output['video']);meta=probe_video(video,cfg)
            run_media([cfg['ffmpeg'],'-v','error','-nostdin','-i',str(video),'-f','null','-'])
            expected=cfg['render'][output['orientation']]
            assert [meta['width'],meta['height']]==expected,(label,meta)
            assert '人工修改标题' in video.stem
            captions=parse_srt(Path(output['subtitles']['srt']).read_text(encoding='utf-8-sig'),output['duration'])
            assert captions[0]['start']>=output['intro_duration'] and '验收人工字幕' in captions[0]['text']
            assert abs(meta['duration']-output['duration'])<.16
            if organization=='dual':
                content=[m for m in output['segments'] if 'match_start' in m]
                assert all(abs(m['commentator_start']-m['match_start']-5)<1e-6 for m in content)
            records.append({'mode':label,'orientation':output['orientation'],'video':str(video),'duration':meta['duration'],
                            'width':meta['width'],'height':meta['height'],'decoded':True,'manual_captions':True,'intro':True,'preview':preview['video']})
        # Idempotent files on rerun; the same saved snapshot reuses its render key.
        cached=run_workflow_export(w['id'],snapshot,cfg)
        assert [o['video'] for o in cached['exports']]==[o['video'] for o in result['exports']]
    report={'success':True,'scope':'Generated footage + supplied transcript + mock LLM selection; real FFmpeg rendering/decoding. No real broadcast/ASR/API claim.',
            'modes':6,'exports':records}
    atomic_json(root/'summary.json',report)
    print(json.dumps({'success':True,'modes':6,'videos':len(records),'report':str(root/'summary.json')},ensure_ascii=False))
    return report


if __name__=='__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, help='Acceptance data directory; use a short path on Windows')
    verify(parser.parse_args().destination)
