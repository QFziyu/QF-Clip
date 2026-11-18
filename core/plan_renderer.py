"""Render a multi-range editorial draft with measured cuts and mapped captions."""
from __future__ import annotations

import copy
import logging
from pathlib import Path
import tempfile

from core.clip_renderer import render_clip, _has_nvenc, _dimensions
from core.export_options import effective_export_config, project_export_options
from core.file_access import restore_inherited_access
from core.intro import probe_intro, prepend_intro
from core.subtitle import composition_cues, cues_srt, write_subtitles
from core.utils import atomic_json, fingerprint, named_video_path, project_lock, read_json
from core.video_input import run_media
from storage.plans import PlanStore

LOG = logging.getLogger(__name__)


def compose_parts(parts, destination, cfg, subtitles=None):
    """Read one normalized part at a time through FFmpeg's concat demuxer.

    Intermediate PCM audio has no per-part AAC priming. Explicit frame-based
    durations and exact audio trims keep every cut and caption boundary aligned.
    Disk intermediates bound memory even for dozens of long source ranges.
    """
    destination = Path(destination)
    encoder = cfg["render"]["encoder"]
    if encoder == "auto":
        encoder = "h264_nvenc" if _has_nvenc(cfg["ffmpeg"]) else "libx264"
    entries = ["ffconcat version 1.0"]
    for i, part in enumerate(parts):
        normalized = destination.parent / f"n{i}.nut"
        duration = part["duration"]
        run_media([cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-i", str(part["path"]), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                   "-af", f"apad,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS",
                   "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-t", f"{duration:.9f}", str(normalized)])
        entries += [f"file n{i}.nut", f"duration {duration:.9f}"]
    (destination.parent / "composition.ffconcat").write_text("\n".join(entries) + "\n", encoding="utf-8")
    if subtitles:
        import shutil
        shutil.copyfile(subtitles, destination.parent / "captions.ass")
    total = sum(p["duration"] for p in parts)
    base = [cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "1", "-i", "composition.ffconcat",
            "-map", "0:v:0", "-map", "0:a:0", "-vf", "subtitles=filename=captions.ass" if subtitles else "null",
            "-af", f"apad,atrim=duration={total:.9f},asetpts=PTS-STARTPTS",
            "-t", f"{total:.9f}", "-r", str(cfg["render"]["fps"])]

    def encode(chosen):
        run_media(base + ["-c:v", chosen, "-preset", "p4" if chosen == "h264_nvenc" else "medium",
                          "-b:v", str(cfg["render"]["bitrate"]), "-pix_fmt", "yuv420p", "-c:a", "aac",
                          "-ar", "48000", "-ac", "2", "-b:a", "192k", "-movflags", "+faststart", str(destination)],
                  cwd=destination.parent)
    try:
        encode(encoder)
    except RuntimeError:
        if encoder != "h264_nvenc":
            raise
        LOG.warning("集锦合成 NVENC 失败，回退 libx264")
        encode("libx264")
    return probe_intro(destination, cfg)


def render_plan_draft(plan_id, draft_id, cfg, orientation="horizontal", preview=False,
                      export_options=None, expected_revision=None):
    from core.editing import validate_plan_document, export_blockers, load_plan_transcript
    from core.pipeline import _validate_source
    if orientation not in ("horizontal", "vertical"):
        raise ValueError("请选择横屏或竖屏")
    cfg = copy.deepcopy(cfg)
    with PlanStore(cfg["database"]) as db:
        plan = db.get_plan(plan_id)
        if not plan:
            raise ValueError("找不到剪辑方案")
        project = db.get_project(plan["project_id"])
        with project_lock(project["project_dir"]):
            if expected_revision is not None and plan["revision"] != expected_revision:
                raise ValueError("方案已更新，请重新提交导出")
            validate_plan_document(plan, project["duration"])
            draft = next((d for d in plan["drafts"] if d["id"] == draft_id), None)
            if not draft:
                raise ValueError("找不到草稿")
            if not preview:
                blockers = export_blockers(plan)
                if blockers or draft["status"] != "approved":
                    raise ValueError("只能导出已批准且满足事件及长度要求的草稿：" + "；".join(blockers))
            _validate_source(project)
            transcript = load_plan_transcript(project, plan["transcript_key"])
            options = project_export_options(db, plan["project_id"], cfg, export_options)
            cfg = effective_export_config(cfg, options)
            if preview:
                options["intro"]["enabled"] = False
                dims = _dimensions(cfg, orientation, True)
                cfg["render"][orientation] = list(dims)
                cfg["render"]["bitrate"] = cfg["render"]["preview_bitrate"]
            intro = probe_intro(options["intro"]["path"], cfg) if options["intro"]["enabled"] else None
            automatic_cues=composition_cues(transcript,draft['segments'],cfg['subtitle'].get('max_chars',18))
            review={'status':'manual'}
            if draft.get('subtitle_text') is None:
                from core.subtitle_review import proofread_cues
                automatic_cues,review=proofread_cues(automatic_cues,cfg,project['project_dir'],request=True,mock=plan['mock'])
            automatic_text=cues_srt(automatic_cues)
            key = fingerprint({"plan": plan_id, "revision": plan["revision"], "draft": {k: draft.get(k) for k in ("segments", "title", "subtitle_text")},
                               "transcript": plan["transcript_key"], "source": project["metadata"]["source_signature"],
                               "render": cfg["render"], "subtitle": cfg["subtitle"], "intro": intro,
                               "orientation": orientation, "preview": preview, 'automatic_text':automatic_text,"version": 2})
            root = Path(cfg["output_dir"]) / "plan_exports" / plan_id[:12] / draft_id
            package = root / (f"preview_{key[:10]}" if preview else f"{orientation}_{key[:10]}")
            package.mkdir(parents=True, exist_ok=True)
            metadata_path = package / "metadata.json"
            if metadata_path.is_file():
                existing = read_json(metadata_path)
                if existing.get("render_key") == key and Path(existing.get("video", "")).is_file():
                    db.record_media(plan_id, draft_id, plan["revision"], existing, preview)
                    return existing
            destination = named_video_path(package, draft["title"], draft_id)
            with tempfile.TemporaryDirectory(prefix=".compose-", dir=package) as temporary:
                temp = Path(temporary)
                parts, mappings, cues, cursor = [], [], [], 0.0
                for i, segment in enumerate(draft["segments"]):
                    part = temp / f"p{i}.mp4"
                    LOG.info("方案 %s 草稿 %s 分段 %d/%d：%.2f–%.2f", plan_id, draft_id, i + 1, len(draft["segments"]), segment["start"], segment["end"])
                    render_clip(project["metadata"], segment, part, cfg, orientation=orientation, subtitle_path=None)
                    meta = probe_intro(part, cfg)
                    duration = meta["duration"]
                    parts.append({"path": part, "meta": meta, "duration": duration})
                    mappings.append({"kind": "content", "source": project["source"], "source_start": segment["start"],
                                     "source_end": segment["end"], "output_start": cursor, "output_end": cursor + duration})
                    cursor += duration
                cues = composition_cues(transcript, draft["segments"], cfg["subtitle"].get("max_chars", 18),
                                        [part["duration"] for part in parts])
                srt = draft.get("subtitle_text") if draft.get("subtitle_text") is not None else automatic_text
                # Manual subtitles are authored against the nominal montage
                # length. Rebase at each exact frame boundary like auto cues.
                if srt:
                    from core.subtitle import parse_srt
                    nominal = 0.0
                    remapped = []
                    manual = parse_srt(srt, sum(s["end"] - s["start"] for s in draft["segments"]))
                    for seg, mapping in zip(draft["segments"], mappings):
                        length = seg["end"] - seg["start"]
                        measured = mapping["output_end"] - mapping["output_start"]
                        for cue in manual:
                            a, b = max(nominal, cue["start"]), min(nominal + min(length, measured), cue["end"])
                            if b > a:
                                remapped.append({**cue, "start": mapping["output_start"] + a - nominal,
                                                 "end": mapping["output_start"] + b - nominal})
                        nominal += length
                    srt = cues_srt(remapped)
                body_subs = write_subtitles({"segments": []}, {"start": 0, "end": cursor}, temp / "body.srt", temp / "body.ass",
                                           cfg, orientation, edited_srt=srt)
                body = temp / "body.mp4"
                actual = compose_parts(parts, body, cfg, body_subs["ass"] if cfg["render"]["burn_subtitles"] else None)
                if abs(actual["duration"] - cursor) > 1 / cfg["render"]["fps"] + .01:
                    raise ValueError("合成视频边界校验失败，未发布成品")
                offset = 0.0
                if intro:
                    assembly = prepend_intro(body, options["intro"]["path"], destination, cfg, orientation)
                    if assembly["intro_signature"] != intro["signature"]:
                        raise ValueError("渲染期间片头文件发生变化，请重试")
                    offset = assembly["intro_duration"]
                else:
                    body.replace(destination)
                    restore_inherited_access(destination)
                subs = write_subtitles({"segments": []}, {"start": 0, "end": cursor}, package / "subtitles.srt", package / "subtitles.ass",
                                       cfg, orientation, edited_srt=srt, time_offset=offset)
                for mapping in mappings:
                    mapping["output_start"] += offset
                    mapping["output_end"] += offset
                if intro:
                    mappings.insert(0, {"kind": "intro", "source": intro["source"], "source_start": 0,
                                        "output_start": 0, "output_end": offset})
                timeline = {"output": str(destination), "duration": cursor + offset, "intro_duration": offset,
                            "segments": mappings, "plan_id": plan_id, "draft_id": draft_id,
                            "source_timeline_origin": project["metadata"].get("timeline_origin", 0)}
                atomic_json(destination.with_suffix(".timeline.json"), timeline)
                metadata = {"video": str(destination), "duration": cursor + offset, "orientation": orientation,
                            "plan_id": plan_id, "draft_id": draft_id, "revision": plan["revision"], "render_key": key,
                            "title": draft["title"], "description": draft["description"], "subtitles": subs,
                            "segments": mappings, "export_options": options, "editing_options": plan["options"],
                            "mock": plan["mock"], "preview": preview,'subtitle_review':review}
                atomic_json(metadata_path, metadata)
                (package / "publication.txt").write_text(draft["title"] + "\n\n" + draft["description"], encoding="utf-8")
                db.record_media(plan_id, draft_id, plan["revision"], metadata, preview)
                return metadata
