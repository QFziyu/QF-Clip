"""Verified immutable export packages and a durable publisher delivery outbox."""
from __future__ import annotations

import hashlib
import copy
import json
import logging
import math
from pathlib import Path

from core.utils import atomic_json, read_json
from storage.workspaces import WorkspaceStore

LOG = logging.getLogger(__name__)
RENDER_VERSION = 5


class HistoricalDelivery(ValueError):
    pass


def content_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def text_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def file_record(path):
    path = Path(path).resolve(strict=True)
    before = path.stat()
    if not path.is_file():
        raise ValueError('交付文件不是普通文件：' + str(path))
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('校验期间交付文件发生变化')
    return {'path': str(path), 'bytes': after.st_size, 'sha256': digest.hexdigest()}


def verify_file(record):
    if file_record(record['path']) != record:
        raise ValueError('交付文件已被替换或修改，请重新导出：' + str(record['path']))
    return record


def profile_for(cfg):
    return cfg.get('profile') or ('cpu' if cfg.get('asr', {}).get('device') == 'cpu' else 'gpu')


def _media_record(video, cfg, orientation, expected_duration=None, expected_dimensions=None):
    from core.video_input import probe_video
    record = file_record(video)
    if record['bytes'] <= 0:
        raise ValueError('交付视频为空')
    meta = probe_video(video, cfg, require_audio=False)
    width, height, duration = int(meta['width']), int(meta['height']), float(meta['duration'])
    if not math.isfinite(duration) or duration <= 0 or min(width, height) <= 0:
        raise ValueError('成品媒体参数无效')
    if orientation not in ('horizontal', 'vertical') or (orientation == 'horizontal' and width < height) or (orientation == 'vertical' and height < width):
        raise ValueError('交付画幅与成品画面不符')
    if expected_dimensions is not None and [width, height] != list(expected_dimensions):
        raise ValueError('成品分辨率与已批准设置不符')
    tolerance = max(.12, 2 / float(cfg.get('render', {}).get('fps', 30)))
    if expected_duration is not None and abs(duration - expected_duration) > tolerance:
        raise ValueError('成品实际时长与时间轴不符')
    # Detect replacement during FFprobe too.
    verify_file(record)
    return {**record, 'duration': duration, 'width': width, 'height': height, 'orientation': orientation}


def seal_render_package(result, cfg, files, expected_dimensions=None):
    """Write the completion marker only after every required sidecar exists."""
    media = _media_record(result['video'], cfg, result['orientation'], result['duration'], expected_dimensions)
    records = [file_record(path) for path in files]
    manifest = {'schema_version': 1, 'render_key': result['render_key'], 'media': media, 'files': records}
    atomic_json(result['verification_path'], manifest)
    return manifest


def verify_render_package(result, cfg):
    manifest = read_json(result['verification_path'])
    if manifest.get('schema_version') != 1 or manifest.get('render_key') != result.get('render_key'):
        raise ValueError('成品校验记录与渲染版本不符')
    if Path(manifest['media']['path']).resolve() != Path(result['video']).resolve():
        raise ValueError('成品校验记录指向不同视频')
    for record in manifest['files']:
        verify_file(record)
    media = manifest['media']
    current = _media_record(media['path'], cfg, media['orientation'], media['duration'], [media['width'], media['height']])
    if current != media:
        raise ValueError('交付视频完整校验失败')
    return manifest


def make_delivery(snapshot, item, result, cfg, profile=None, export_job_id=None):
    """No historical media can enter the delivery register."""
    if result.get('preview') or result.get('historical'):
        raise ValueError('预览或历史成品不能交付发布')
    manifest = verify_render_package(result, cfg)
    export_root = Path(snapshot['output_dir']).resolve() / 'workflow_exports' / snapshot['workspace_id'][:16]
    if not Path(result['video']).resolve().is_relative_to(export_root):
        raise ValueError('交付视频不在已批准导出目录中')
    if any(Path(record['path']).resolve().parent != Path(result['video']).resolve().parent for record in manifest['files']):
        raise ValueError('交付附件不在已批准成品目录中')
    approval = content_digest(snapshot)
    identity = 'delivery-' + content_digest({'approval': approval, 'item': item['id'],
        'revision': item.get('revision', 0), 'render_key': result['render_key'], 'version': RENDER_VERSION})[:40]
    from core.titles import publication_title
    platform = (snapshot.get('publication') or {}).get('platform')
    delivery = dict(schema_version=1, delivery_id=identity, workspace_id=snapshot['workspace_id'],
        variant_id=snapshot['variant_id'], item_id=item['id'], item_revision=item.get('revision', 0),
        approval_digest=approval, profile=profile or profile_for(cfg), media=manifest['media'],
        publication={'title': publication_title(item,platform), 'description': item.get('description', '')}, verified=True)
    if item.get('platform_titles'):
        delivery['publication']['platform_titles'] = copy.deepcopy(item['platform_titles'])
    if export_job_id is not None:
        delivery['export_job_id'] = export_job_id
    if snapshot['packaging'].get('subtitles', True):
        paths = result.get('subtitles') or {}
        delivery['subtitles'] = {name: file_record(path) for name, path in paths.items() if name in ('srt', 'ass')}
        frozen = snapshot.get('frozen_subtitles', {}).get(item['id'])
        if frozen:
            delivery['subtitles']['frozen_sha256'] = frozen['sha256']
    return delivery


def verify_delivery(delivery, cfg):
    """Recheck full content immediately before crossing the publisher boundary."""
    if delivery.get('schema_version') != 1 or delivery.get('verified') is not True:
        raise ValueError('交付记录未经验证')
    video = Path(delivery['media']['path']).resolve()
    # Approved alternate output directories are persisted by the export register;
    # arbitrary API requests never supply delivery documents or file paths.
    with WorkspaceStore(cfg['database']) as store:
        registered = store.get_delivery(delivery['delivery_id'], delivery['profile'])
    if registered != delivery:
        raise ValueError('交付记录与工厂登记内容不符')
    if not delivery_is_current(delivery, cfg):
        raise HistoricalDelivery('上游已修改，历史交付不能自动发送')
    media = delivery['media']
    actual = _media_record(video, cfg, media['orientation'], media['duration'], [media['width'], media['height']])
    if actual != media:
        raise ValueError('交付视频与批准内容不符')
    for record in delivery.get('subtitles', {}).values():
        if isinstance(record, dict):
            if Path(record['path']).resolve().parent != video.parent:
                raise ValueError('字幕不在已登记成品目录中')
            verify_file(record)
    return delivery


def delivery_is_current(delivery, cfg):
    from core.workflow import snapshot_is_current
    with WorkspaceStore(cfg['database']) as store:
        snapshot = store.get_delivery_snapshot(delivery['delivery_id'])
        workspace = store.get_workspace(delivery['workspace_id'])
    return bool(snapshot and workspace and content_digest(snapshot) == delivery['approval_digest']
                and snapshot_is_current(workspace, snapshot, cfg))


def list_pending_deliveries(cfg, profile=None, limit=100):
    with WorkspaceStore(cfg['database']) as store:
        entries = store.pending_deliveries(profile, limit)
    current = []
    for entry in entries:
        if delivery_is_current(entry['delivery'], cfg):
            current.append(entry)
        else:
            with WorkspaceStore(cfg['database']) as store:
                store.supersede_delivery(entry['delivery']['delivery_id'])
    return current


def acknowledge_delivery(cfg, delivery_id, receipt=None):
    with WorkspaceStore(cfg['database']) as store:
        store.acknowledge_delivery(delivery_id, receipt)


def fail_delivery(cfg, delivery_id, error):
    with WorkspaceStore(cfg['database']) as store:
        store.fail_delivery(delivery_id, error)


def dispatch_delivery_outbox(cfg, client, profile=None, limit=5):
    sent, errors = [], []
    for entry in list_pending_deliveries(cfg, profile, limit):
        delivery = entry['delivery']
        # Ordinary exports are discoverable deliverables, without publication
        # authorization. Keep them locally until the user authorizes a new export.
        if entry['publication'] is None:
            continue
        try:
            verify_delivery(delivery, cfg)
            receipt = client(delivery, entry['publication'])
            if not isinstance(receipt, dict) or not (receipt.get('id') or receipt.get('job_id')):
                raise ValueError('发布助手未返回任务受理凭据')
            acknowledge_delivery(cfg, delivery['delivery_id'], receipt)
            sent.append(delivery['delivery_id'])
        except Exception as exc:
            fail_delivery(cfg, delivery['delivery_id'], exc)
            errors.append({'delivery_id': delivery['delivery_id'], 'error': str(exc)})
            LOG.info('交付保留待发送：%s（%s）', delivery['delivery_id'], type(exc).__name__)
    return {'sent': sent, 'errors': errors}
