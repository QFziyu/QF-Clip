"""Bounded real replay sample. Does not invoke Whisper or a paid API."""
from pathlib import Path
import json
import os
import sys
import threading
import argparse
from urllib.parse import parse_qs, urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT.parent/'软件下载/轻舟视频下载'))
from core.config import load_config
from core.video_input import run_media,probe_video
from core.utils import atomic_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True,help='Replay URL supplied by the operator; never stored in source')
    args=parser.parse_args()
    from sites import resolve_replay
    from engine import Transport,inspect,download
    from automation import DownloadManager,redact
    cfg=load_config();os.environ.update(FFMPEG_PATH=cfg['ffmpeg'],FFPROBE_PATH=cfg['ffprobe'])
    root=ROOT/'output/automation_upgrade/real_sample';root.mkdir(parents=True,exist_ok=True)
    url=args.url
    report={'source_url':url,'full_download_tested':False,'asr_llm_tested':False}
    try:
        capture=resolve_replay(url);plan=inspect(capture['url'],capture.get('headers'))
        transport=Transport(capture['url'],capture.get('headers'))
        original,base=transport.manifest(plan['url'])
        lines=[];duration=0
        for line in original.splitlines():
            if line.strip()=='#EXT-X-ENDLIST':continue
            lines.append(line)
            if line.startswith('#EXTINF:'):duration+=float(line.split(':',1)[1].split(',')[0])
            if line.strip() and not line.startswith('#') and duration>=60:break
        sample='\n'.join(lines)+'\n#EXT-X-ENDLIST\n'
        class SampleTransport:
            def manifest(self,_):return sample,base
            def open(self,url,byte_range=None):return transport.open(url,byte_range)
        manager=DownloadManager(root,{'output_dir':str(root)})
        job={'id':'real-sample','title':'海星真实源验证','cancel':threading.Event(),'require_audio':True,
             'source_identity':{'platform':'haixing','resource_id':parse_qs(urlsplit(url).query).get('recordId',['sample'])[0]}}
        job['verify_media']=lambda p:manager.verify(p,job)
        final=root/'海星真实源验证.mp4'
        if not final.exists():download(job,capture,root,ffmpeg=cfg['ffmpeg'],transport=SampleTransport())
        meta=probe_video(final,cfg)
        run_media([cfg['ffmpeg'],'-v','error','-i',str(final),'-f','null','-'])
        report.update(success=True,source_duration=plan['duration'],sample_duration=meta['duration'],video=str(final),decoded=True,
            width=meta['width'],height=meta['height'],audio_tracks=len(meta['audio_streams']))
        manager.close()
    except Exception as exc:report.update(success=False,error=redact(exc))
    atomic_json(root/'summary.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
