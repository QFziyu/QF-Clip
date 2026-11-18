"""Real FFmpeg, four approved items / three projects, no online service."""
import copy,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core.config import load_config
from core.workflow import _item,edit_item
from core.export_center import catalog,plan_exports,run_export_batch
from core.video_input import run_media,probe_video
from core.utils import atomic_json
from storage.workspaces import WorkspaceStore


def main():
    root=ROOT/'output/acceptance_190';root.mkdir(exist_ok=True)
    cfg=load_config();cfg.update(database=str(root/'fixture.sqlite3'),output_dir=str(root/'media'),cache_dir=str(root/'cache'))
    cfg['openai'].update(provider='api',api_key='',model='')
    cfg['render'].update(encoder='libx264',horizontal=[320,180],vertical=[180,320],preview=[240,136],fps=10,bitrate='400k')
    source=root/'中文 样片.mp4'
    if not source.exists():
        run_media([cfg['ffmpeg'],'-v','error','-nostdin','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=10:duration=5',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=5','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)])
    meta=probe_video(source,cfg)
    with WorkspaceStore(cfg['database']) as db:
        for n,count in enumerate((2,1,1)):
            w=db.create_workspace('跨项目样片'+str(n),'acceptance-'+str(n))
            if w['variants']:continue
            items=[_item('clip'+str(k),'人工标题 '+str(n)+'-'+str(k),[{'start':.5,'end':3.5}],
                    dict(id='source',source=str(source),duration=5,metadata=meta,project_dir=str(root)),
                    subtitle='1\n00:00:00,100 --> 00:00:02,500\n手工字幕保持不变\n',status='approved') for k in range(count)]
            pending=copy.deepcopy(items[0]);pending.update(id='pending',status='pending');items.append(pending)
            w.update(step=5,active_variant='v',variants=[dict(id='v',name='验收方案',form='full',items=items,events=[],options={})])
            db.save_workspace(w)
    rows=catalog(cfg);assert len(rows)==4
    plan=plan_exports(cfg,[r['key'] for r in rows],['horizontal','vertical'])
    assert not plan['issues'],plan['issues']
    result=run_export_batch(plan,cfg);assert not result['errors'],result['errors']
    assert len(result['exports'])==8
    before={r['video']:Path(r['video']).stat().st_mtime_ns for r in result['exports']}
    again=run_export_batch(plan,cfg);assert not again['errors']
    assert before=={r['video']:Path(r['video']).stat().st_mtime_ns for r in again['exports']}
    for record in result['exports']:
        probe=probe_video(record['video'],cfg)
        assert [probe['width'],probe['height']]==cfg['render'][record['orientation']]
        assert abs(probe['duration']-3)<.2
        assert '人工标题' in Path(record['video']).name
        run_media([cfg['ffmpeg'],'-v','error','-i',record['video'],'-f','null','-'])
    assert all(r['export_status']=='exported' for r in catalog(cfg))
    report=dict(success=True,projects=3,approved=4,videos=8,full_decode=True,retry_without_rerender=True,api_used=False,exports=result['exports'])
    atomic_json(root/'summary.json',report);print(json.dumps({k:v for k,v in report.items() if k!='exports'},ensure_ascii=False))


if __name__=='__main__':main()
