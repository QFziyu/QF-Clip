"""Synthetic CLI/background acceptance for ordered sources and visual contact sheets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config
from core.utils import atomic_json
from core.video_input import run_media
from core.audio_sync import audio_features, correlation_candidates


def main():
    cfg = load_config()
    root = ROOT / 'output/sequence_visual_upgrade/demo'
    root.mkdir(parents=True, exist_ok=True)
    rate, seconds, offset = 16000, 70, 7.5
    rng = np.random.default_rng(490)
    t = np.arange(rate * seconds) / rate
    frequencies = rng.uniform(100, 3300, seconds * 10)[(t * 10).astype(int)]
    signal = np.sin(2 * np.pi * np.cumsum(frequencies) / rate) * .3
    wav = root / 'source.wav'
    with wave.open(str(wav), 'wb') as f:
        f.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes((signal * 32767).astype('<i2').tobytes())
    match = root / '比赛 合成测试.mp4'
    if not match.is_file():
        run_media([cfg['ffmpeg'],'-v','error','-y','-f','lavfi','-i','testsrc2=s=320x180:r=10',
            '-i',str(wav),'-c:v','libx264','-preset','ultrafast','-c:a','aac','-shortest',str(match)])
    first, second = root/'主播1.mp4', root/'主播2.mp4'
    if not first.is_file():
        run_media([cfg['ffmpeg'],'-v','error','-y','-i',str(match),'-t','30','-vf',f'tpad=start_duration={offset}:start_mode=add',
            '-af',f'adelay={round(offset*1000)}:all=1','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(first)])
    # Source 1 contains 7.5 s padding + first 22.5 s of match, then source 2 continues.
    if not second.is_file():
        run_media([cfg['ffmpeg'],'-v','error','-y','-ss','22.5','-i',str(match),
            '-vf','scale=400:226','-r','15','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(second)])
    config=root/'settings.yaml'
    config.write_text(yaml.safe_dump({'ffmpeg':cfg['ffmpeg'],'ffprobe':cfg['ffprobe'],
        'render':{'horizontal':[320,180],'preview':[320,180],'fps':10,'bitrate':'800k'}}),encoding='utf-8')
    environment=dict(os.environ,PYTHONUTF8='1',OUTPUT_DIR=str(root/'output'),CACHE_DIR=str(root/'cache'),
        DATABASE_PATH=str(root/'output/factory.sqlite3'),FCF_RENDER_ENCODER='libx264',OPENAI_API_KEY='')
    def cli(*args):
        result=subprocess.run([sys.executable,str(ROOT/'main.py'),'--config',str(config),*args],
            cwd=ROOT,env=environment,capture_output=True,text=True,encoding='utf-8',errors='replace')
        if result.returncode: raise RuntimeError(result.stderr[-3000:]+result.stdout[-1000:])
        return json.loads(result.stdout)
    def finish(job):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            job=cli('jobs','--job',job['id'])
            if job['status'] in ('completed','failed','interrupted'):break
            time.sleep(.5)
        assert job['status']=='completed',job
        return job['result']
    document=finish(cli('compose-create',str(match),str(first),'--append-commentator',str(second),'--background'))
    identity=document['id']
    assert len(document['commentator']['parts'])==2
    sync=cli('compose-sync','--composite',identity)
    assert sync['offset_seconds'] is not None and abs(sync['offset_seconds']-offset)<=.15,sync
    search=root/'visual_search.json'
    atomic_json(search,{'event':'kickoff','match_start':4.,'match_end':8.,
        'commentator_start':11.5,'commentator_end':15.5,'samples':8})
    visual=finish(cli('compose-visual-sync','--composite',identity,'--search',str(search),'--frames-only','--background'))
    assert all(Path(p).is_file() for p in visual['sheets']['match'])
    assert visual['offset_seconds'] is None
    options=root/'options.json'; ranges=root/'ranges.json'
    atomic_json(options,{**document['options'],'offset_seconds':sync['offset_seconds'],'sync_confirmed':True,'title':'主播两段拼接验收'})
    atomic_json(ranges,[{'start':20.,'end':26.}])  # Crosses the physical source boundary at match t=22.5.
    result=finish(cli('compose','--composite',identity,'--options',str(options),'--segments',str(ranges),'--background'))
    assert len(result['mappings'][0]['commentator_parts'])==2
    run_media([cfg['ffmpeg'],'-v','error','-i',result['video'],'-f','null','-'])
    decoded=root/'rendered.wav'
    run_media([cfg['ffmpeg'],'-v','error','-y','-i',result['video'],'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(decoded)])
    checks=[]
    for source_time, output_time in ((20.,0.),(23.,3.)):
        peaks=correlation_candidates(audio_features(wav,.05,source_time,2),audio_features(decoded,.05,output_time,2),
            step=.05,minimum_seconds=1.5,limit=1,lag_bounds=(-.3,.3))
        assert peaks and abs(peaks[0]['offset_seconds'])<=.05 and peaks[0]['score']>.75,peaks
        checks.append(peaks[0])
    report={'synthetic':True,'paid_api_calls':0,'real_match_or_vision_api_test':False,
        'ordered_source_files':[str(first),str(second)],'source_boundary_seconds':30.,
        'expected_offset':offset,'detected_offset':sync['offset_seconds'],'confidence':sync['confidence'],
        'audio_checks_before_and_after_join':checks,'video':result['video'],'duration':result['duration'],
        'mappings':result['mappings'],'sheets':visual['sheets'],
        'cli_and_detached_queue_create_visual_export_decode':'passed'}
    atomic_json(root/'acceptance.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
