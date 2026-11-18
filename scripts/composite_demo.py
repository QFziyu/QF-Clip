"""Reproducible synthetic two-feed CLI/queue acceptance. No ASR or API calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config
from core.utils import atomic_json
from core.video_input import run_media
from core.audio_sync import audio_features, correlation_candidates


def main():
    cfg = load_config()
    root = ROOT / 'output/composite_upgrade/demo'
    root.mkdir(parents=True, exist_ok=True)
    sample_rate, seconds, offset = 16000, 70, 12.35
    rng = np.random.default_rng(308)
    times = np.arange(sample_rate * seconds) / sample_rate
    frequencies = rng.uniform(100, 3300, seconds * 10)[(times * 10).astype(int)]
    signal = np.sin(2 * np.pi * np.cumsum(frequencies) / sample_rate) * .3
    later = np.r_[rng.normal(0, .02, round(offset*sample_rate)), .8*signal + rng.normal(0,.015,len(signal))]
    for name, audio in (('match', signal), ('commentator', later)):
        wav = root / (name+'.wav')
        with wave.open(str(wav), 'wb') as handle:
            handle.setparams((1,2,sample_rate,0,'NONE','not compressed'))
            handle.writeframes((np.clip(audio,-1,1)*32767).astype('<i2').tobytes())
        video = root / ('比赛 测试.mp4' if name == 'match' else '主播 测试.mp4')
        if not video.exists():
            run_media([cfg['ffmpeg'],'-v','error','-nostdin','-y','-f','lavfi','-i',
                'testsrc2=s=640x360:r=15' if name=='match' else 'color=red:s=640x480:r=15',
                '-i',str(wav),'-c:v','libx264','-preset','ultrafast','-c:a','aac','-shortest',str(video)])
    environment = dict(os.environ, PYTHONUTF8='1', OUTPUT_DIR=str(root/'output'), CACHE_DIR=str(root/'cache'),
                       DATABASE_PATH=str(root/'output/factory.sqlite3'), FCF_RENDER_ENCODER='libx264')
    config = root/'settings.yaml'
    import yaml
    config.write_text(yaml.safe_dump({'ffmpeg':cfg['ffmpeg'],'ffprobe':cfg['ffprobe'],
        'render':{'horizontal':[640,360],'preview':[480,270],'fps':15,'bitrate':'1200k'}}), encoding='utf-8')
    def cli(*args):
        command = [sys.executable,str(ROOT/'main.py'),'--config',str(config),*args]
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:] + result.stdout[-2000:])
        return json.loads(result.stdout)
    document = cli('compose-create',str(root/'比赛 测试.mp4'),str(root/'主播 测试.mp4'))
    identity = document['id']
    sync = cli('compose-sync','--composite',identity)
    assert sync['offset_seconds'] is not None and abs(sync['offset_seconds']-offset)<.15, sync
    options = {**document['options'],'title':'双路合成验收样片','offset_seconds':sync['offset_seconds'],'sync_confirmed':True}
    options_path, ranges_path = root/'options.json', root/'ranges.json'
    atomic_json(options_path,options)
    atomic_json(ranges_path,[{'start':5.,'end':11.},{'start':40.,'end':46.}])
    queued = cli('compose','--composite',identity,'--options',str(options_path),'--segments',str(ranges_path),'--background')
    deadline = time.monotonic()+120
    while time.monotonic()<deadline:
        job = cli('jobs','--job',queued['id'])
        if job['status'] in ('completed','failed','interrupted'):
            break
        time.sleep(.5)
    assert job['status']=='completed', job
    result = job['result']
    run_media([cfg['ffmpeg'],'-v','error','-i',result['video'],'-f','null','-'])
    decoded = root/'rendered_audio.wav'
    run_media([cfg['ffmpeg'],'-v','error','-y','-i',result['video'],'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(decoded)])
    audio_check = correlation_candidates(audio_features(root/'match.wav',.05,5,6), audio_features(decoded,.05,0,6),
                                        step=.05,minimum_seconds=4,limit=1,lag_bounds=(-.3,.3))
    assert audio_check and abs(audio_check[0]['offset_seconds'])<=.05 and audio_check[0]['score']>.75, audio_check
    report = {'synthetic':True,'expected_offset':offset,'detected_offset':sync['offset_seconds'],
              'confidence':sync['confidence'],'cli_create_sync_background_export_decode':'passed','job_id':job['id'],
              'video':result['video'],'mappings':result['mappings'],'rendered_audio_alignment':audio_check,
              'real_broadcast_test':False,'paid_api_calls':0}
    atomic_json(root/'acceptance.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
