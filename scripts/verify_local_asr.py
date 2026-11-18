"""Actual local-ASR -> subtitle -> playable sample verification, with no LLM calls.

Default uses official Chinese WAVs. --audio selects an existing recording WAV;
only --seconds starting at --start is read into the isolated verification area.
Original audio, project approvals, subtitles and finished videos are untouched.
"""
from pathlib import Path
import argparse
import copy
import json
import logging
import sys
import time
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.local_asr import ZipformerHTTP
from core.transcriber import transcribe_audio, _slice_wav, validate_transcript
from core.utils import atomic_json, file_signature
from core.video_input import run_media, probe_video
from core.pipeline import process_video, load_clip_transcript
from core.subtitle import write_subtitles, parse_srt
from core.clip_renderer import render_clip
from storage.database import Database


def main():
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio',type=Path)
    parser.add_argument('--start',type=float,default=0)
    parser.add_argument('--seconds',type=float,default=125)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    cfg=load_config();cfg['asr']['engine']='zipformer_http'
    root=ROOT/'cache/local_asr_validation'/time.strftime('%Y%m%d-%H%M%S');root.mkdir(parents=True)
    cfg.update(database=str(root/'validation.sqlite3'),output_dir=str(root/'output'),cache_dir=str(root/'cache'))
    cfg['render']['encoder']='libx264';cfg['subtitle_review']['enabled']=False
    client=ZipformerHTTP(cfg['asr']);report={'service':client.health,'samples':[],'llm_calls':0}
    official=ROOT.parent/'Hugging Face/tests/test_wavs'
    for path in sorted(official.glob('*.wav')):
        start=time.perf_counter();segments,_=client.transcribe(path)
        raw=client.last_raw;validate_transcript(raw,raw['duration'])
        report['samples'].append({'file':path.name,'seconds':raw['duration'],'elapsed':time.perf_counter()-start,
                                  'text':raw['text'],'segments':len(raw['segments'])})
    if args.audio:
        path=args.audio.resolve()
        with wave.open(str(path)) as wav: length=wav.getnframes()/wav.getframerate()
        if not 0<=args.start<length or args.seconds<=0: raise ValueError('Invalid test range')
        audio=root/'真实音频节选.wav'
        _slice_wav(path,audio,args.start,min(length,args.start+args.seconds))
        report['source']={'kind':'existing-commentary-audio','path':str(path),'start':args.start}
    else:
        audio=official/'0.wav';report['source']={'kind':'official-short-audio','path':str(audio)}
    # A simple generated background, not match footage. Audio is an actual WAV.
    video=root/'验证输入.mp4'
    run_media([cfg['ffmpeg'],'-hide_banner','-loglevel','error','-nostdin','-y','-f','lavfi','-i',
        'color=c=0x10212f:s=640x360:r=25','-i',str(audio),'-c:v','libx264','-preset','ultrafast',
        '-c:a','aac','-shortest','-movflags','+faststart',str(video)])
    begin=time.perf_counter()
    # Fails immediately if the transcription-only path accidentally contacts LLM.
    with patch('core.llm_client.LLMClient.complete',side_effect=AssertionError('No LLM in ASR validation')):
        result=process_video(video,cfg,skip_render=True,transcript_only=True)
    if result.get('errors'): raise RuntimeError(str(result))
    report['transcription_elapsed']=time.perf_counter()-begin
    with Database(cfg['database']) as db: project=db.get_project(result['project_id'])
    transcript=load_clip_transcript(project,{})
    report.update(duration=project['duration'],provenance=transcript['provenance'],
        segments=len(transcript['segments']),text=''.join(s['text'] for s in transcript['segments']),
        transcript=str(Path(project['project_dir'])/'transcript.json'))
    # Cache reuse must not need an ASR server or construct Whisper/LLM.
    with patch('core.local_asr.ZipformerHTTP',side_effect=AssertionError('ASR cache should suffice')):
        repeat=transcribe_audio(Path(project['project_dir'])/'audio.wav',project['metadata'],cfg,project['project_dir'])
    if repeat!=transcript: raise AssertionError('Cached transcript changed')
    report['cached_without_service']=True
    clip={'start':0,'end':min(project['duration'],25)}
    subtitles=write_subtitles(transcript,clip,root/'验证字幕.srt',root/'验证字幕.ass',cfg)
    cues=parse_srt(Path(subtitles['srt']).read_text(encoding='utf-8-sig'),clip['end'])
    preview=root/'本地中文识别验证.mp4'
    render_clip(project['metadata'],clip,preview,cfg,preview=True,subtitle_path=subtitles['ass'])
    output_meta=probe_video(preview,cfg)
    run_media([cfg['ffmpeg'],'-v','error','-nostdin','-i',str(preview),'-f','null','-'])
    report.update(preview=str(preview),preview_seconds=output_meta['duration'],subtitles=subtitles,
                  subtitle_cues=len(cues),cache_key=transcript['cache_key'],status='passed')
    atomic_json(root/'report.json',report)
    print(json.dumps({'status':'passed','report':str(root/'report.json'),'preview':str(preview),
        'duration':report['duration'],'transcription_elapsed':report['transcription_elapsed'],
        'segments':report['segments'],'cached_without_service':True,'llm_calls':0},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
