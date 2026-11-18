"""Resumable orchestration. Heavy data lives on disk; the DB stores review decisions."""
from __future__ import annotations

from core.llm_settings import cache_identity, configured

import logging
import math
from pathlib import Path
import shutil
import tempfile
import wave

from core.cache import read_cache
from core.audio_extract import extract_audio
from core.audio_events import analyze_audio
from core.clip_renderer import render_clip
from core.file_access import restore_inherited_access
from core.export_options import effective_export_config, project_export_options
from core.intro import prepend_intro, probe_intro
from core.clip_selector import CLIP_SCHEMA, select_clips, validate_clip_payload
from core.subtitle import write_subtitles
from core.transcriber import transcribe_audio, validate_transcript
from core.utils import (atomic_json, file_signature, fingerprint, named_video_path,
                        project_lock, read_json, rename_video_for_title)
from core.video_input import probe_video, discover_videos
from storage.database import Database

log = logging.getLogger(__name__)


def _identity(source, audio_track, cfg, mock, transcript_path):
    signature = file_signature(source)
    identity = {"source": signature, "track": audio_track, "mock": mock,
                "pipeline_version": 2}
    return fingerprint(identity), signature


def load_clip_transcript(project, clip):
    """Human-reviewed clips retain the exact transcript used when selected."""
    root = Path(project["project_dir"])
    key = clip.get("transcript_key") or project["metadata"].get("transcript_key")
    snapshot = root / "transcripts" / f"{key}.json" if key else root / "transcript.json"
    if not snapshot.exists() and key == project["metadata"].get("transcript_key"):
        snapshot = root / "transcript.json"  # compatibility with pre-snapshot projects
    data = read_json(snapshot)
    # Snapshot identity refers to the saved JSON. Validation may normalize old
    # records (e.g. integer word bounds to floats), which is not corruption.
    if key and fingerprint(data) != key:
        raise ValueError("该切片绑定的转录快照已损坏；请重新处理恢复缓存后再导出。")
    return validate_transcript(data, project["duration"])


def _validate_decisions(value, duration, transcript):
    if not isinstance(value, dict) or not isinstance(value.get("clips"), list):
        raise ValueError("剪辑决策必须包含 clips 数组")
    required = CLIP_SCHEMA["properties"]["clips"]["items"]["required"]
    seen = set()
    for clip in value["clips"]:
        validate_clip_payload({"clips": [{field: clip[field] for field in required}]})
        if (clip["end"] > duration or not isinstance(clip.get("id"), str) or clip["id"] in seen
                or not clip["id"] or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in clip["id"])):
            raise ValueError("剪辑决策含越界时间或重复/非法 ID")
        seen.add(clip["id"])
        if clip.get("source") == "audio":
            event = clip["audio_event"]
            if not 0 <= event["start"] < event["end"] <= duration:
                raise ValueError("声音提示越界")
        else:
            text = ["".join(s["text"].split()) for s in transcript["segments"]
                    if s["end"] > clip["start"] and s["start"] < clip["end"]]
            if any(not any("".join(quote.split()) in line for line in text) for quote in clip["evidence"]):
                raise ValueError("缓存候选的引用无法在对应时间范围内得到证实")


def _validate_events(events, duration):
    if not isinstance(events, list):
        raise ValueError("声音事件缓存必须是数组")
    for event in events:
        values = [event["start"], event["end"], event["strength"]]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError("声音事件含无效数值")
        if not 0 <= values[0] < values[1] <= duration or values[2] < 0 or event.get("kind") != "energy_rise":
            raise ValueError("声音事件时间或类型无效")


def _validate_source(project):
    source = Path(project["source"])
    if not source.is_file():
        raise FileNotFoundError(f"原始素材已移动或删除: {source}。请恢复原路径后重试。")
    saved = project["metadata"].get("source_signature")
    if saved and file_signature(source) != saved:
        raise RuntimeError("原始文件已经变化，请重新 process 导入；不能沿用旧时间轴导出。")


def _render_fingerprint(clip, project, cfg, orientation, preview):
    return fingerprint({"start": clip["start"], "end": clip["end"], "subtitle": clip.get("subtitle_text"),
                        "reviewed_subtitle": clip.get('_automatic_srt'),
                        "render": cfg["render"], "style": cfg["subtitle"], "orientation": orientation,
                        "preview": preview, "source": project["metadata"].get("source_signature"),
                        "transcript": clip.get("transcript_key") or project["metadata"].get("transcript_key"), "version": 2})


def _caption_text(clip):
    return clip.get('subtitle_text') if clip.get('subtitle_text') is not None else clip.get('_automatic_srt')


def _prepare_captions(project,clip,cfg):
    if clip.get('subtitle_text') is not None or not cfg.get('subtitle_review',{}).get('enabled'):
        return clip
    from core.subtitle import transcript_cues,cues_srt
    from core.subtitle_review import proofread_cues
    transcript=load_clip_transcript(project,clip)
    cues=transcript_cues(transcript,clip,cfg['subtitle'].get('max_chars',18))
    cues,report=proofread_cues(cues,cfg,project['project_dir'],request=True,
        mock=bool(transcript.get('mock') or project['metadata'].get('mock')))
    return {**clip,'_automatic_srt':cues_srt(cues),'_subtitle_review':report}


def _preview(project, clip, cfg, db):
    clip=_prepare_captions(project,clip,cfg)
    key = _render_fingerprint(clip, project, cfg, "horizontal", True)
    existing = clip.get("preview_path")
    if clip.get("preview_fingerprint") == key and existing and Path(existing).is_file():
        return str(existing)
    root = Path(project["project_dir"])
    transcript = load_clip_transcript(project, clip)
    directory = root / "previews"
    directory.mkdir(exist_ok=True)
    base = directory / f"{clip['id']}_{key[:8]}"
    subs = write_subtitles(transcript, clip, base.with_suffix(".srt"), base.with_suffix(".ass"), cfg,
                           edited_srt=_caption_text(clip))
    destination = base.with_suffix(".mp4")
    render_clip(project["metadata"], clip, destination, cfg, preview=True,
                subtitle_path=subs["ass"] if cfg["render"]["burn_subtitles"] else None)
    db.set_preview(clip["id"], str(destination), key, expected_revision=clip["revision"])
    return str(destination)


def render_preview(project_id, clip_id, cfg):
    with Database(cfg["database"]) as db:
        project = db.get_project(project_id)
        clip = db.get_clip(clip_id)
        if not project or not clip or clip["project_id"] != project_id:
            raise ValueError("找不到项目或切片")
        with project_lock(project["project_dir"]):
            _validate_source(project)
            return _preview(project, clip, cfg, db)


def process_video(path, cfg, audio_track=0, skip_render=False, mock=False,
                  transcript_path=None, allow_cpu=False, editing_options=None, transcript_only=False):
    if editing_options is not None:
        from core.editing_options import normalize_editing_options
        from core.editing import create_editing_plan
        options = normalize_editing_options(editing_options)
        prepared = process_video(path, cfg, audio_track, True, mock, transcript_path,
                                 allow_cpu, transcript_only=True)
        result = create_editing_plan(prepared["project_id"], cfg, options, mock, skip_render)
        return {**prepared, **result, "status": "planned"}
    source = discover_videos(path)
    if len(source) != 1 or not Path(path).is_file():
        raise ValueError("process 需要单个视频文件；目录请使用 batch")
    if mock and not transcript_path:
        raise ValueError("--mock 必须同时提供 --transcript 已有带时间戳的 JSON；可用 demo 命令生成明确标注的测试素材。")
    project_id, signature = _identity(source[0], audio_track, cfg, mock, transcript_path)
    # Migrate pre-v2 identities and keep one project's reviews/budget across
    # imported transcript revisions; source/audio-track changes still isolate it.
    with Database(cfg["database"]) as db:
        existing_project = db.get_project(project_id)
        if not existing_project:
            prior = next((p for p in db.list_projects()
                          if p["metadata"].get("source_signature") == signature
                          and p["metadata"].get("audio_track", 0) == audio_track
                          and p["metadata"].get("mock", False) == bool(mock)), None)
            if prior:
                project_id = prior["id"]
                existing_project = prior
    project_dir = (Path(existing_project["project_dir"]) if existing_project and existing_project.get("project_dir")
                   else Path(cfg["cache_dir"]) / project_id)
    with project_lock(project_dir), Database(cfg["database"]) as db:
        meta = probe_video(source[0], cfg, audio_track)
        old = db.get_project(project_id)
        meta = {**(old["metadata"] if old else {}), **meta}
        meta.update(source_signature=signature, mock=bool(mock), audio_track=audio_track,
                    process_options={"path": str(source[0]), "audio_track": audio_track,
                                     "mock": bool(mock), "transcript_path": str(Path(transcript_path).resolve()) if transcript_path else None,
                                     "allow_cpu": bool(allow_cpu), "skip_render": bool(skip_render)})
        project = {"id": project_id, "source": str(source[0]), "name": source[0].name,
                   "duration": meta["duration"], "metadata": meta, "project_dir": str(project_dir),
                   "status": "imported", "error": None}
        db.upsert_project(project)
        atomic_json(project_dir / "source.json", meta)
        log.info("项目 %s · %.1f 分钟 · 音轨 %s", project_id, meta["duration"] / 60, audio_track)

        def stage(name):
            project["status"] = name
            db.upsert_project(project)
            log.info("阶段: %s", name)

        try:
            # Save the current valid transcription before any reanalysis changes it.
            # The snapshot remains available to already-reviewed candidates.
            # Migrate against the recorded identity even if the old cache was
            # deleted/corrupted: reviewed clips must never inherit the new text.
            prior_transcript_key = (old or {}).get("metadata", {}).get("transcript_key")
            if prior_transcript_key:
                db.bind_transcript(project_id, prior_transcript_key)
            previous = read_cache(project_dir / "transcript.json", lambda data: validate_transcript(data, meta["duration"]))
            if previous:
                prior_key = fingerprint(previous)
                atomic_json(project_dir / "transcripts" / f"{prior_key}.json", previous)
                db.bind_transcript(project_id, prior_key)
            stage("extracting")
            wav_path = project_dir / "audio.wav"
            valid_wav = False
            if wav_path.exists():
                try:
                    with wave.open(str(wav_path), "rb") as wav:
                        valid_wav = wav.getframerate() == 16000 and wav.getnchannels() == 1 and abs(wav.getnframes()/16000 - meta["duration"]) < 0.1
                except (wave.Error, EOFError):
                    pass
            if not valid_wav:
                extract_audio(meta, wav_path, cfg)
            else:
                log.info("复用音频缓存")
            stage("transcribing")
            mode = cfg["selection"].get("mode", "hybrid")
            if mode == "audio" and not transcript_path:
                transcript = previous or {"video": meta["source"], "segments": [], "provenance": "audio-only",
                                          "timeline_origin": meta.get("timeline_origin", 0)}
                atomic_json(project_dir / "transcript.json", transcript)
                log.info("声音模式：无需 Whisper 或 API；已有转录可用于字幕")
            else:
                transcript = transcribe_audio(wav_path, meta, cfg, project_dir,
                                              allow_cpu=allow_cpu, transcript_path=transcript_path)
            if mock:
                transcript["mock"] = True
                atomic_json(project_dir / "transcript.json", transcript)
            meta["transcript_key"] = fingerprint(transcript)
            atomic_json(project_dir / "transcripts" / f"{meta['transcript_key']}.json", transcript)
            if transcript_only:
                project["status"] = "transcribed"
                db.upsert_project(project)
                return {"project_id": project_id, "status": "transcribed", "project_dir": str(project_dir), "errors": []}
            stage("selecting")
            events_path = project_dir / "audio_events.json"
            events_key_path = project_dir / "audio_events.cache.json"
            events_key = fingerprint({"source": signature, "track": audio_track,
                                      "analysis": cfg.get("audio_analysis", {}), "version": 1})
            events = []
            if mode == "audio" or (mode == "hybrid" and cfg["selection"]["audio_events"]):
                cached_events = read_cache(events_path, lambda data: _validate_events(data, meta["duration"]))
                cached_key = read_cache(events_key_path, lambda data: data["key"])
                if cached_events is not None and cached_key and cached_key.get("key") == events_key:
                    events = cached_events
                else:
                    events = analyze_audio(wav_path, cfg)
                    atomic_json(events_path, events)
                    atomic_json(events_key_path, {"key": events_key})
            decision_path = project_dir / "decisions.json"
            decision_key = fingerprint({"transcript": meta["transcript_key"], "selection": cfg["selection"],
                                        "prompt": Path(cfg["prompt_path"]).read_text(encoding="utf-8") if mode != "audio" else None,
                                        "model": cfg["openai"]["model"] if mode != "audio" else None,
                                        "provider": cfg["openai"]["base_url"] if mode != "audio" else None,
                                        "mock": mock, "events": events, "version": 1, **cache_identity(cfg["openai"])})
            cached_decisions = read_cache(decision_path, lambda data: _validate_decisions(data, meta["duration"], transcript)
                                          if data.get("cache_key") == decision_key else None) or {}
            if cached_decisions.get("cache_key") == decision_key:
                clips = cached_decisions["clips"]
                log.info("复用全局剪辑决策: %d 条", len(clips))
            else:
                clips = select_clips(transcript, meta, cfg, project_dir, mock=mock, audio_events=events)
                for clip in clips:
                    clip["id"] = project_id + "_" + fingerprint({"selector_id": clip.get("id"),
                        "start": clip["start"], "end": clip["end"], "category": clip["category"],
                        "evidence": clip.get("evidence"), "title": clip["title"],
                        "transcript": meta["transcript_key"]})[:12]
                    clip["transcript_key"] = meta["transcript_key"]
                atomic_json(decision_path, {"project_id": project_id, "cache_key": decision_key,
                                           "mock": mock, "timeline": "video_first_frame_zero",
                                           "source": str(source[0]), "clips": clips})
            db.upsert_clips(project_id, clips, replace_pending=True)
            errors = []
            if not skip_render:
                stage("previewing")
                for index, clip in enumerate(db.list_clips(project_id), 1):
                    try:
                        log.info("预览 %d/%d: %s", index, len(clips), clip["title"])
                        _preview(project, clip, cfg, db)
                    except Exception as exc:
                        log.exception("预览失败，继续其他候选: %s", clip["id"])
                        errors.append({"clip_id": clip["id"], "error": str(exc)})
            atomic_json(project_dir / "preview_errors.json", errors)
            project["status"] = "preview_partial" if errors else "planned" if skip_render else "ready"
            project["error"] = f"{len(errors)} 条预览失败；重新运行可恢复" if errors else None
            db.upsert_project(project)
            return {"project_id": project_id, "candidate_count": len(clips), "status": project["status"],
                    "project_dir": str(project_dir), "errors": errors}
        except Exception as exc:
            project.update(status="failed", error=str(exc))
            db.upsert_project(project)
            raise


def batch_process(directory, cfg, recursive=False, **kwargs):
    if not Path(directory).is_dir():
        raise ValueError("batch 需要目录")
    results = []
    for path in discover_videos(directory, recursive):
        try:
            results.append(dict(source=str(path), **process_video(path, cfg, **kwargs)))
        except Exception as exc:
            log.exception("批处理失败，继续下一个文件: %s", path)
            results.append({"source": str(path), "status": "failed", "error": str(exc)})
    report = Path(cfg["output_dir"]) / "batch_report.json"
    atomic_json(report, results)
    return results


def _package_video(directory, clip_id, render_key):
    """Trust saved render ownership, never a coincidentally matching filename."""
    try:
        record = read_json(directory / "metadata.json")
        video = Path(record["video"]).resolve()
        if (record["clip_id"] == clip_id and record["render_key"] == render_key
                and video.parent == directory.resolve() and video.is_file()):
            return video
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def _reuse_rendered_export(clip_root, directory, destination, clip_id, render_key):
    """A title edit changes the package, while unchanged media can be copied."""
    for package in sorted(clip_root.iterdir()):
        if not package.is_dir() or package == directory:
            continue
        previous = _package_video(package, clip_id, render_key)
        if previous is None:
            continue
        old_mapping = previous.with_suffix(".timeline.json")
        try:
            mapping = read_json(old_mapping) if old_mapping.is_file() else None
        except (OSError, ValueError, TypeError):
            log.warning("历史导出时间轴不可用，跳过复用: %s", old_mapping)
            continue
        if mapping is not None and not isinstance(mapping, dict):
            continue
        # Exclusive creation prevents an unexpected name collision from ever
        # replacing an existing video; copied files inherit this directory's ACL.
        with previous.open("rb") as source, destination.open("xb") as output:
            try:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            except Exception:
                output.close()
                destination.unlink(missing_ok=True)
                raise
        if mapping is not None:
            atomic_json(destination.with_suffix(".timeline.json"), {**mapping, "output": str(destination)})
        log.info("复用已渲染视频，更换发布标题: %s", destination.name)
        return True
    return False


def _render_final_export(project, clip, destination, cfg, orientation, intro_path=None):
    """Burn captions only into the body; opening frames never get body captions."""
    if not intro_path:
        subs = write_subtitles(load_clip_transcript(project, clip), clip,
                               destination.parent / "subtitles.srt", destination.parent / "subtitles.ass",
                               cfg, orientation=orientation, edited_srt=_caption_text(clip))
        render_clip(project["metadata"], clip, destination, cfg, orientation=orientation,
                    subtitle_path=subs["ass"] if cfg["render"]["burn_subtitles"] else None)
        return
    with tempfile.TemporaryDirectory(prefix=".body-", dir=destination.parent) as directory:
        body = Path(directory) / "body.mp4"
        subs = write_subtitles(load_clip_transcript(project, clip), clip,
                               body.with_suffix(".srt"), body.with_suffix(".ass"), cfg,
                               orientation=orientation, edited_srt=_caption_text(clip))
        render_clip(project["metadata"], clip, body, cfg, orientation=orientation,
                    subtitle_path=subs["ass"] if cfg["render"]["burn_subtitles"] else None)
        assembly = prepend_intro(body, intro_path, destination, cfg, orientation=orientation)
        mapping = read_json(body.with_suffix(".timeline.json"))
        offset = assembly["intro_duration"]
        mapping.update(output=str(destination), duration=assembly["duration"], content_start=offset,
                       content_duration=assembly["body_duration"], intro_duration=offset,
                       intro_source=assembly["intro_source"], intro_signature=assembly["intro_signature"],
                       encoder=assembly["encoder"], subtitle_offset=offset,
                       source_to_output_offset=offset - clip["start"],
                       segments=[{"kind": "intro", "output_start": 0, "output_end": offset,
                                  "source": assembly["intro_source"], "source_start": 0},
                                 {"kind": "content", "output_start": offset, "output_end": assembly["duration"],
                                  "source": project["source"], "source_start": clip["start"],
                                  "source_end": clip["end"]}])
        atomic_json(destination.with_suffix(".timeline.json"), mapping)


def export_project(project_id, cfg, orientations=("horizontal",), approved_only=True, export_options=None):
    if not approved_only:
        raise ValueError("最终导出只允许通过审批的切片")
    orientations = tuple(dict.fromkeys(orientations))
    if not orientations or any(o not in ("horizontal", "vertical") for o in orientations):
        raise ValueError("导出画幅应为 horizontal 或 vertical")
    with Database(cfg["database"]) as db:
        project = db.get_project(project_id)
        if not project:
            raise ValueError(f"找不到项目 {project_id}")
        with project_lock(project["project_dir"]):
            _validate_source(project)
            options = project_export_options(db, project_id, cfg, export_options)
            cfg = effective_export_config(cfg, options)
            intro = options["intro"]
            intro_meta = probe_intro(intro["path"], cfg) if intro["enabled"] else None
            root = Path(cfg["output_dir"]) / project_id
            results, errors = [], []
            for clip in db.list_clips(project_id, status="approved"):
                for orientation in orientations:
                    try:
                        clip=_prepare_captions(project,clip,cfg)
                        transcript = load_clip_transcript(project, clip)
                        key = _render_fingerprint(clip, project, cfg, orientation, False)
                        if intro_meta:
                            key = fingerprint({"body_render": key, "intro": intro_meta["signature"], "version": 1})
                        # New publication metadata gets a fresh package; history is retained.
                        package_key = fingerprint({"render": key, "title": clip["title"], "description": clip["description"]})
                        directory = root / clip["id"] / f"{orientation}_{package_key[:10]}"
                        directory.mkdir(parents=True, exist_ok=True)
                        previous = _package_video(directory, clip["id"], key)
                        legacy = directory / "video.mp4"
                        # Packages from earlier versions used this fixed name.
                        # Their fingerprint directory already identifies the media.
                        if previous is None and legacy.is_file():
                            previous = legacy
                        destination = (rename_video_for_title(previous, clip["title"], clip["id"])
                                       if previous else named_video_path(directory, clip["title"], clip["id"]))
                        if not destination.exists():
                            if not _reuse_rendered_export(root / clip["id"], directory, destination, clip["id"], key):
                                _render_final_export(project, clip, destination, cfg, orientation,
                                                     intro["path"] if intro_meta else None)
                        mapping_path = destination.with_suffix(".timeline.json")
                        mapping = read_json(mapping_path) if mapping_path.is_file() else {}
                        if intro_meta and "intro_duration" not in mapping:
                            raise ValueError("带片头成品缺少字幕偏移时间轴，请保留该文件并重新导出。")
                        offset = mapping.get("intro_duration", 0)
                        subs = write_subtitles(transcript, clip, directory / "subtitles.srt", directory / "subtitles.ass",
                                               cfg, orientation=orientation, edited_srt=_caption_text(clip),
                                               time_offset=offset)
                        restore_inherited_access(destination)
                        if destination.with_suffix(".timeline.json").is_file():
                            restore_inherited_access(destination.with_suffix(".timeline.json"))
                        metadata = {"project_id": project_id, "clip_id": clip["id"], "source": project["source"],
                                    "start": clip["start"], "end": clip["end"], "title": clip["title"],
                                    "description": clip["description"], "tags": clip["tags"], "score": clip["score"],
                                    "reason": clip["reason"], "original": clip["original"], "revision": clip["revision"],
                                    "transcript_key": clip.get("transcript_key"),
                                    "orientation": orientation, "render_key": key, "mock": project["metadata"].get("mock", False),
                                    "video": str(destination), "subtitles": subs,
                                    "subtitle_review":clip.get('_subtitle_review',{'status':'manual' if clip.get('subtitle_text') is not None else 'disabled'}),
                                    "export_options": options,
                                    "intro": {"enabled": bool(intro_meta), "path": intro["path"] if intro_meta else "",
                                              "duration": offset, "signature": intro_meta["signature"] if intro_meta else None},
                                    "duration": mapping.get("duration", clip["end"] - clip["start"] + offset),
                                    "subtitle_offset": offset}
                        atomic_json(directory / "metadata.json", metadata)
                        (directory / "publication.txt").write_text(clip["title"] + "\n\n" + clip["description"] +
                            "\n\n" + " ".join("#" + str(t) for t in clip["tags"]), encoding="utf-8")
                        db.set_export(clip["id"], orientation, str(destination), metadata,
                                      expected_revision=clip["revision"])
                        results.append(metadata)
                    except Exception as exc:
                        log.exception("导出失败，继续其他切片: %s/%s", clip["id"], orientation)
                        errors.append({"clip_id": clip["id"], "orientation": orientation, "error": str(exc)})
            return db.write_export_manifest(project_id, root / "export_manifest.json", results, errors)
