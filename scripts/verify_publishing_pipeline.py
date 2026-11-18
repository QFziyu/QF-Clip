"""Real FFmpeg + paired HTTP publishing acceptance, using a local fake platform.

Creates isolated fixture databases under output/publishing_upgrade. It never uses
paid APIs, real accounts, user project records, or Douyin's website.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / 'FootballPublisher'))

from core.config import load_config
from core.deliveries import dispatch_delivery_outbox, list_pending_deliveries
from core.publisher_client import PublisherClient, ensure_configuration
from core.subtitle import parse_srt
from core.utils import atomic_json, file_signature
from core.video_input import probe_video, run_media
from core.workflow import _item, export_snapshot, run_workflow_export
from storage.workspaces import WorkspaceStore
from football_publisher.config import Config
from football_publisher.service import Publisher, Server


class FixtureAdapter:
    """Only records a private upload copy; never talks to a real platform."""
    def __init__(self):
        self.identity = {'creator_id': 'synthetic-fixture', 'label': '本地验收账号'}
        self.uploads = []
        self.submissions = []

    def observe_identity(self, account):
        return self.identity.copy()

    def open_login(self, account):
        pass

    def prepare(self, job, media, text):
        with Path(media).open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.uploads.append({'job_id': job['id'], 'sha256': digest, 'text': text})
        return {'prepared': True, 'fixture': True}

    def submit(self, job):
        self.submissions.append(job['id'])
        return {'submitted': True, 'receipt_verified': False}

    def close(self):
        pass


def run(platform='douyin'):
    if platform not in ('douyin','bilibili'): raise ValueError('Unsupported test platform')
    cfg = load_config()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    root = ROOT / 'output/publishing_upgrade' / ('pipeline-' + run_id)
    root.mkdir(parents=True)
    cfg.update(database=str(root/'factory.sqlite3'), cache_dir=str(root/'cache'),
               output_dir=str(root/'exports'), profile='cpu')
    cfg['asr']['device'] = 'cpu'
    cfg['render'].update(encoder='libx264', horizontal=[320,180], vertical=[180,320], fps=15,
                         bitrate='500k')
    cfg['subtitle'].update(font_size=20, vertical_font_size=18)
    cfg['subtitle_review']['enabled'] = False
    cfg['openai']['api_key'] = ''
    source = root/'中文 空格 原片.mp4'
    run_media([cfg['ffmpeg'], '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=15',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
        '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], timeout=60)
    meta = probe_video(source, cfg)
    meta['source_signature'] = file_signature(source)
    subtitle = '1\n00:00:00,000 --> 00:00:01,500\n人工修改后的字幕\n\n2\n00:00:02,000 --> 00:00:03,800\n第二句保持原样\n'
    title = '人工修改后标题_中文验收'
    item = _item('fixture-item', title, [{'start':0.,'end':4.}],
        {'id':'fixture-project','source':str(source),'duration':4.,'metadata':meta,
         'project_dir':str(root/'analysis')}, subtitle, 'approved')
    item['description'] = '人工修改后的说明保持不变'
    if platform=='bilibili':
        item['platform_titles']={'douyin':'抖音观点标题','bilibili':'【足球杂谈】B站专用人工标题'}
    expected_title=item.get('platform_titles',{}).get(platform,title)
    with WorkspaceStore(cfg['database']) as db:
        workspace = db.create_workspace('发布链路合成验收', 'publisher-fixture')
        workspace.update(variants=[{'id':'variant','items':[item],'events':[],'options':{},'form':'full'}],
            active_variant='variant', packaging={'subtitles':True}, step=5)
        db.save_workspace(workspace)
    connection = ensure_configuration(cfg, 'cpu')
    adapter = FixtureAdapter()
    pconfig = Config(connection['publisher']['token'], 'cpu', (Path(cfg['output_dir']).resolve(),),
        root/'publisher/publisher.sqlite3', root/'publisher', port=0)
    publisher = Publisher(pconfig, adapter)
    account = publisher.store.connect(platform)
    publisher.store.take_connect()
    publisher.store.observe_account(account['id'], adapter.identity, adapter.identity['creator_id'])
    intent = {'platform':platform,'account_id':account['id'],'mode':'prepare',
              'orientation':'horizontal','hashtags':['足球','人工验收']}
    if platform=='bilibili':intent.update(copyright=2,source='https://example.invalid/original',category_path=['运动','足球'])
    snapshot = export_snapshot(workspace, cfg, ['horizontal','vertical'], {}, cfg['output_dir'], publication=intent)
    assert snapshot['frozen_subtitles'][item['id']]['text'] == subtitle
    result = run_workflow_export(workspace['id'], snapshot, cfg)
    assert result['errors'] == [], result['errors']
    assert len(result['deliveries']) == 2
    media_reports = []
    for export in result['exports']:
        video = Path(export['video'])
        assert title in video.name, video.name
        assert parse_srt(Path(export['subtitles']['srt']).read_text(encoding='utf-8'), 4.) == parse_srt(subtitle, 4.)
        run_media([cfg['ffmpeg'], '-v', 'error', '-i', str(video), '-f', 'null', '-'], timeout=60)
        measured = probe_video(video, cfg)
        media_reports.append({'orientation':export['orientation'],'path':str(video),
            'duration':measured['duration'],'resolution':[measured['width'],measured['height']],
            'fully_decoded':True,'manual_title_and_subtitles_preserved':True})
    assert len(list_pending_deliveries(cfg, 'cpu')) == 1, 'only selected orientation may be authorized'
    server = Server(publisher, ('127.0.0.1', 0))
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    connection['publisher'].update(url=f'http://127.0.0.1:{port}', port=port)
    atomic_json(Path(cfg['output_dir'])/'.automation/connection.json', connection)
    client = PublisherClient(cfg)
    accepted = []
    def lose_response(delivery, publication):
        accepted.append(client.submit_job(delivery, publication)['id'])
        raise RuntimeError('synthetic lost response after acceptance')
    try:
        first = dispatch_delivery_outbox(cfg, lose_response, 'cpu')
        assert len(first['errors']) == 1 and len(publisher.store.jobs()) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()
    # Reconstruct service/database after a successful acceptance with lost response.
    restarted = Publisher(replace(pconfig, port=port), adapter)
    server = Server(restarted, ('127.0.0.1', port))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        resumed = dispatch_delivery_outbox(cfg, client.submit_job, 'cpu')
        repeated = dispatch_delivery_outbox(cfg, client.submit_job, 'cpu')
        assert len(resumed['sent']) == 1 and repeated == {'sent':[],'errors':[]}
        jobs = restarted.store.jobs()
        assert len(jobs) == 1 and jobs[0]['id'] == accepted[0]
        assert jobs[0]['delivery']['media']['orientation'] == 'horizontal'
        assert restarted.worker.work_once()
        ready = restarted.store.get(accepted[0])
        assert ready['state'] == 'prepared', ready
        assert len(adapter.uploads) == 1 and not adapter.submissions
        assert adapter.uploads[0]['sha256'] == jobs[0]['delivery']['media']['sha256']
        assert adapter.uploads[0]['text'] == {'title':expected_title,'description':item['description'],'hashtags':intent['hashtags']}
        assert not restarted.worker.work_once()
        report = {'success':True,'platform':platform,'run_id':run_id,'scope':'real FFmpeg and localhost HTTP; fake publishing adapter, no real platform operation',
            'paid_api_calls':0,'real_platform_publications':0,'source':str(source),'media':media_reports,
            'selected_orientation_only':True,'durable_lost_response_restart':True,
            'accepted_job_count':len(jobs),'private_upload_count':len(adapter.uploads),
            'prepare_did_not_submit':True,'manual_publication_text_preserved':True,
            'approved_subtitles_frozen':True,'isolated_database':str(root/'factory.sqlite3')}
        atomic_json(root/'report.json', report)
        atomic_json(ROOT/'output/publishing_upgrade'/('pipeline_report.json' if platform=='douyin' else 'pipeline_report_bilibili.json'), report)
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        server.shutdown(); server.server_close(); thread.join(); restarted.worker.close()
    return report


if __name__ == '__main__':
    run()
