"""Real FFmpeg checks for virtual multi-file cuts and unabridged review media."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.utils import atomic_json,file_signature,fingerprint
from core.video_input import run_media,probe_video
from core.batch_timeline import virtual_timeline
from core.workflow_renderer import render_workflow_item
from core.subtitle import parse_srt


def verify():
    root=ROOT/'output/mf';root.mkdir(exist_ok=True)
    cfg=load_config();cfg.update(cache_dir=str(root/'cache'),output_dir=str(root/'out'))
    cfg['render'].update(encoder='libx264',horizontal=[320,180],vertical=[180,320],preview=[240,136],fps=10,bitrate='350k',preview_bitrate='250k')
    cfg['subtitle'].update(font_size=18,vertical_font_size=18,margin_v=10,vertical_margin_v=15)
    metas=[]
    for i,color in enumerate(['red','blue']):
        source=root/f'中文 素材{i}.mp4'
        if not source.exists():
            run_media([cfg['ffmpeg'],'-v','error','-nostdin','-y','-f','lavfi','-i',f'color=c={color}:size=320x180:rate=10:duration=36',
                '-f','lavfi','-i',f'sine=frequency={440+i*200}:sample_rate=48000:duration=36',
                '-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)])
        meta=probe_video(source,cfg);meta.pop('raw_probe',None);meta['source_signature']=file_signature(source);metas.append(meta)
    timeline=virtual_timeline(metas,cfg)
    transcript={'segments':[{'start':33.,'end':39.,'text':'跨文件字幕测试。'},{'start':50.,'end':52.,'text':'末段仍然保留。'}]}
    key=fingerprint(transcript);project=root/'project';atomic_json(project/'transcripts'/f'{key}.json',transcript)
    item=dict(id='batch-test',title='人工长集锦标题',description='生成素材验收',segments=[{'start':5.,'end':30.},{'start':33.,'end':63.}],
        source={'kind':'single','meta':timeline,'project_dir':str(project)},transcript_key=key,subtitle_text=None,subtitle_stale=False,revision=0,status='approved')
    records=[]
    for manual in (False,True):
        selected={**item,'id':f'batch-{manual}'}
        if manual:selected['subtitle_text']='1\n00:00:26,000 --> 00:00:31,000\n人工字幕跨文件保留\n'
        for preview,orientation in [(True,'horizontal'),(False,'horizontal'),(False,'vertical')]:
            result=render_workflow_item('btest',selected,cfg,orientation,preview)
            meta=probe_video(result['video'],cfg)
            run_media([cfg['ffmpeg'],'-v','error','-nostdin','-i',result['video'],'-f','null','-'])
            assert abs(meta['duration']-55.)<.15,(meta['duration'],preview)
            assert len(result['segments'])==3
            assert [s['source_start'] for s in result['segments']]==[5.,33.,0.]
            assert abs(result['segments'][2]['timeline_start']-36.)<.01
            cues=parse_srt(Path(result['subtitles']['srt']).read_text(encoding='utf-8-sig'),result['duration'])
            assert cues and max(c['end'] for c in cues)<=55.01
            if manual:
                assert all('人工字幕' in c['text'] for c in cues)
                assert abs(cues[0]['start']-26.)<.02 and abs(cues[-1]['end']-31.)<.02
            if preview:assert result['preview_complete']
            records.append(dict(preview=preview,orientation=orientation,manual_subtitles=manual,duration=meta['duration'],
                decoded=True,video=result['video'],source_parts=len(result['segments'])))
    atomic_json(root/'acceptance.json',dict(success=True,scope='Generated media, real FFmpeg. No API/ASR calls.',results=records))
    print(json.dumps({'success':True,'videos':len(records),'report':str(root/'acceptance.json')}))


if __name__=='__main__':verify()
