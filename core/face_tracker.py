"""Sampled Haar face tracking; bounded raw-frame memory, no downloaded model."""
from __future__ import annotations

import logging
from pathlib import Path
import subprocess
import tempfile

import numpy as np
from core.utils import atomic_json, fingerprint, read_json

LOG = logging.getLogger(__name__)


def crop_track(detections, width, height, interval):
    visible = [box for box in detections if box is not None]
    if not visible:
        return {'mode': 'fit', 'warning': '未检测到可靠人脸，本段保持完整画面缩放。', 'points': []}
    desired = max(max(w * 4, h * 3.2 * 16 / 9) for x, y, w, h in visible)
    crop_w = int(min(width, height * 16 / 9, desired)) // 2 * 2
    crop_h = int(crop_w * 9 / 16) // 2 * 2
    if min(crop_w, crop_h) < 16 or any(w + 4 > crop_w or h + 4 > crop_h for x, y, w, h in visible):
        return {'mode': 'fit', 'warning': '人脸区域太小，本段保持完整缩放。', 'points': []}
    x, y, w, h = visible[0]
    cx, cy = x + w / 2, y + h * .8
    points, last = [], visible[0]
    for index, box in enumerate(detections):
        if box is not None:
            last = box
        x, y, w, h = last
        cx, cy = .65 * cx + .35 * (x + w / 2), .65 * cy + .35 * (y + h * .8)
        left = float(np.clip(cx - crop_w / 2, max(0, x + w - crop_w), min(width - crop_w, x)))
        top = float(np.clip(cy - crop_h / 2, max(0, y + h - crop_h), min(height - crop_h, y)))
        points.append({'time': round(index * interval, 3), 'x': int(left) // 2 * 2, 'y': int(top) // 2 * 2})
    return {'mode': 'face', 'crop_width': crop_w, 'crop_height': crop_h, 'points': points,
            'detected_samples': len(visible), 'total_samples': len(detections),
            'warning': '采用抽样人脸检测和位置平滑；多人场景可能选错人，短时漏检沿用最近位置，请预览。'}


def track_faces(meta, start, end, cfg, cache_dir, interval=1.):
    try:
        import cv2
    except ImportError:
        return {'mode': 'fit', 'points': [], 'warning': '未安装 OpenCV，使用完整画面缩放。安装 requirements-face.txt 可启用人脸跟踪。'}
    identity = fingerprint({'source': meta['source_signature'], 'start': start, 'end': end, 'interval': interval, 'version': 1})
    cache = Path(cache_dir) / f'face_{identity}.json'
    if cache.is_file():
        return read_json(cache)
    classifier = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'))
    if classifier.empty():
        return {'mode': 'fit', 'points': [], 'warning': 'OpenCV 人脸检测器不可用，保持完整画面缩放。'}
    width, height = meta['width'], meta['height']
    sw, sh = 320, max(32, round(height * 320 / width / 2) * 2)
    absolute = meta['timeline_origin'] + start
    command = [cfg['ffmpeg'], '-v', 'error', '-nostdin', '-copyts']
    seek = absolute - meta['format_start'] - 2
    if seek > 0:
        command += ['-ss', str(seek)]
    command += ['-i', meta['source'], '-map', f"0:{meta['video_stream']}", '-an', '-vf',
                f'trim=start={absolute}:end={meta["timeline_origin"] + end},setpts=PTS-({absolute})/TB,fps=1/{interval},scale={sw}:{sh}',
                '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
    detections, previous = [], None
    LOG.info('抽样追踪主播人脸 %.2f–%.2fs，每 %.1f 秒一帧', start, end, interval)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            frame_bytes = sw * sh * 3
            while True:
                data = bytearray()
                while len(data) < frame_bytes:
                    block = process.stdout.read(frame_bytes - len(data))
                    if not block:
                        break
                    data.extend(block)
                if not data:
                    break
                if len(data) != frame_bytes:
                    raise RuntimeError('人脸分析帧数据不完整')
                frame = np.frombuffer(data, dtype=np.uint8).reshape(sh, sw, 3)
                boxes = classifier.detectMultiScale(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), scaleFactor=1.1,
                                                    minNeighbors=5, minSize=(20, 20))
                selected = None
                if len(boxes):
                    def rank(box):
                        x, y, w, h = box
                        distance = ((x + w / 2 - previous[0]) ** 2 + (y + h / 2 - previous[1]) ** 2) ** .5 if previous else 0
                        return float(w * h) / (1 + distance / 40)
                    x, y, w, h = max(boxes, key=rank)
                    previous = (x + w / 2, y + h / 2)
                    selected = [float(x * width / sw), float(y * height / sh), float(w * width / sw), float(h * height / sh)]
                detections.append(selected)
            if process.wait(timeout=30):
                errors.seek(0)
                raise RuntimeError('人脸分析解码失败：' + errors.read().decode('utf-8', errors='replace')[-1500:])
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    result = crop_track(detections, width, height, interval)
    atomic_json(cache, result)
    return result
