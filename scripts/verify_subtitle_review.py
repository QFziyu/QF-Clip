"""Synthetic video + fake structured API; real subtitle rendering and decoding."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.llm_client import LLMClient
from core.utils import atomic_json,file_signature,fingerprint
from core.video_input import run_media,probe_video
from core.workflow_renderer import render_workflow_item
from core.subtitle import parse_srt


def main():
    root=ROOT/'output/srv';root.mkdir(exist_ok=True)
    cfg=load_config();cfg.update(cache_dir=str(root/'c'),output_dir=str(root/'o'))
    cfg['openai'].update(api_key='synthetic-test',model='synthetic-test',max_retries=0)
    cfg['subtitle_review']['enabled']=True
    cfg['render'].update(encoder='libx264',horizontal=[320,180],vertical=[180,320],preview=[240,136],fps=10,bitrate='350k')
    cfg['subtitle'].update(font_size=18,vertical_font_size=18,margin_v=12,vertical_margin_v=15)
    source=root/'素材.mp4'
    if not source.exists():run_media([cfg['ffmpeg'],'-v','error','-nostdin','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=10:duration=26',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=26','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)])
    meta=probe_video(source,cfg);meta.pop('raw_probe',None);meta['source_signature']=file_signature(source)
    transcript={'segments':[{'start':1.,'end':4.,'text':'格一前顶出球'},{'start':10.,'end':13.,'text':'瓜地奥拉换人'}]}
    key=fingerprint(transcript);project=root/'p';atomic_json(project/'transcripts'/f'{key}.json',transcript)
    item=dict(id='proofread',title='AI校对验收',segments=[{'start':0.,'end':8.},{'start':9.,'end':25.}],
        source={'kind':'single','meta':meta,'project_dir':str(project)},transcript_key=key,
        subtitle_text=None,subtitle_stale=False,status='approved',revision=0)
    calls=[]
    def transport(payload):
        calls.append(payload)
        rows=json.loads(payload['messages'][1]['content'])['subtitles']
        edits=[{'id':r['id'],'text':r['text'].replace('格一','格伊').replace('瓜地奥拉','瓜迪奥拉')} for r in rows]
        return {'choices':[{'message':{'content':json.dumps({'edits':edits},ensure_ascii=False)}}]}
    def client(options,path):return LLMClient(options,path,transport=transport,sleeper=lambda n:None)
    results=[]
    with patch('core.subtitle_review.LLMClient',side_effect=client):
        for preview,orientation in [(True,'horizontal'),(False,'horizontal'),(False,'vertical')]:
            result=render_workflow_item('srv',item,cfg,orientation,preview)
            run_media([cfg['ffmpeg'],'-v','error','-nostdin','-i',result['video'],'-f','null','-'])
            text=Path(result['subtitles']['srt']).read_text(encoding='utf-8-sig')
            assert '格伊' in text and '瓜迪奥拉' in text
            assert '格一' not in text and '瓜地奥拉' not in text
            cues=parse_srt(text,result['duration']);assert cues[0]['start']==1. and cues[1]['start']==9.
            assert result['subtitle_review']['changed']==2
            assert fingerprint(json.loads((project/'transcripts'/f'{key}.json').read_text(encoding='utf-8')))==key
            results.append({'video':result['video'],'orientation':orientation,'preview':preview,'decoded':True,'corrected':2,'duration':result['duration']})
        manual={**item,'id':'manual','subtitle_text':'1\n00:00:01,000 --> 00:00:03,000\n我保存的人工字幕\n'}
        before=len(calls);result=render_workflow_item('srv',manual,cfg,preview=True)
        assert len(calls)==before
        assert '我保存的人工字幕' in Path(result['subtitles']['srt']).read_text(encoding='utf-8-sig')
    assert len(calls)<=1
    report={'success':True,'scope':'Generated media + fake API; real FFmpeg/ASS encoding and full decoding. No live ASR/API claim.',
        'shared_api_calls':len(calls),'manual_skipped':True,'source_transcript_unchanged':True,'results':results}
    atomic_json(root/'acceptance.json',report);print(json.dumps({'success':True,'videos':4,'report':str(root/'acceptance.json')}))


if __name__=='__main__':main()
