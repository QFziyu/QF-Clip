"""Bounded semantic indexing, global editorial planning and mandatory coverage."""
from __future__ import annotations

from core.llm_settings import cache_identity, configured

import copy
import logging
import math
from pathlib import Path

from core.cache import read_cache
from core.editing_options import CONTENT_PROMPTS, locate_events, normalize_editing_options, number
from core.utils import atomic_json, fingerprint, project_lock, read_json
from storage.plans import PlanStore

LOG = logging.getLogger(__name__)


def load_plan_transcript(project, transcript_key):
    """Recover pre-1.2.1 plans that recorded the normalized snapshot hash.

    Older imported transcripts may gain fields during validation. Accept the
    original cached snapshot only when its validated contents match exactly;
    never substitute a different project's or a newer transcript silently.
    """
    from core.pipeline import load_clip_transcript
    try:
        return load_clip_transcript(project, {"transcript_key": transcript_key})
    except FileNotFoundError:
        transcript = load_clip_transcript(project, {"transcript_key": project["metadata"].get("transcript_key")})
        if fingerprint(transcript) != transcript_key:
            raise ValueError("方案引用的转录快照缺失，现有转录也与该方案不符；请恢复对应缓存") from None
        return transcript


def validate_segments(segments, duration, form=None):
    if not isinstance(segments, list) or not segments or len(segments) > 200:
        raise ValueError("每条草稿需要 1～200 个源区间")
    if form in ("shorts", "continuous") and len(segments) != 1:
        raise ValueError("短片与连续长段需要恰好一个连续区间")
    previous = -1
    normalized = []
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) - {"start", "end"}:
            raise ValueError("源区间只允许 start、end")
        start = number(segment.get("start"), "源起点", 0, duration)
        end = number(segment.get("end"), "源终点", 0, duration)
        if start >= end or start < previous - 1e-6:
            raise ValueError("源区间需满足起点 < 终点，且按时间排序、互不重叠")
        normalized.append({"start": start, "end": end})
        previous = end
    return normalized


def validate_plan_document(plan, duration):
    options = normalize_editing_options(plan["options"])
    ids = set()
    for draft in plan["drafts"]:
        if draft["id"] in ids or draft["status"] not in ("pending", "approved", "rejected"):
            raise ValueError("草稿身份或审批状态无效")
        ids.add(draft["id"])
        if not isinstance(draft["title"], str) or not draft["title"].strip() or len(draft["title"]) > 300:
            raise ValueError("草稿标题需要 1～300 字符")
        draft["segments"] = validate_segments(draft["segments"], duration, options["format"])
        if draft.get("subtitle_text"):
            from core.subtitle import validate_srt
            validate_srt(draft["subtitle_text"], sum(s["end"] - s["start"] for s in draft["segments"]))
    for event in plan["events"]:
        if not isinstance(event["confirmed"], bool) or not isinstance(event["waived"], bool):
            raise ValueError("事件确认及免除状态必须是布尔值")
        if event["start"] is None and event["end"] is None:
            if event["confirmed"] and not event["waived"]:
                raise ValueError("确认事件前必须填写录像起止时间")
        else:
            validate_segments([{"start": event["start"], "end": event["end"]}], duration)
    return plan


def coverage_report(plan, approved_only=False):
    drafts = [d for d in plan["drafts"] if d["status"] == "approved" or (not approved_only and d["status"] != "rejected")]
    rows = []
    for event in plan["events"]:
        covering = [d["id"] for d in drafts if event["start"] is not None and event["end"] is not None and
                    any(s["start"] <= event["start"] + 1e-6 and s["end"] >= event["end"] - 1e-6 for s in d["segments"])]
        state = ("waived" if event["waived"] else "unresolved" if event["start"] is None else
                 "unconfirmed" if not event["confirmed"] else "covered" if covering else "missing")
        rows.append({**event, "coverage": state, "draft_ids": covering})
    return rows


def export_blockers(plan):
    rows = coverage_report(plan, approved_only=True)
    problems = [f"{e['id']} {e['label']}：{e['coverage']}" for e in rows if e["coverage"] not in ("covered", "waived")]
    for draft in plan["drafts"]:
        if draft["status"] == "approved":
            duration = sum(s["end"] - s["start"] for s in draft["segments"])
            if not plan["options"]["min_seconds"] - .001 <= duration <= plan["options"]["max_seconds"] + .001:
                problems.append(f"{draft['id']} 时长 {duration:.1f} 秒超出本方案范围，请调整区间或新建合适时长方案")
    return problems


def _merge(ranges):
    merged = []
    for item in sorted(ranges, key=lambda s: (s["start"], s["end"])):
        if merged and item["start"] <= merged[-1]["end"] + 1e-6:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
        else:
            merged.append({"start": item["start"], "end": item["end"]})
    return merged


def _fit(seed, options, transcript, duration):
    """Expand to complete speech boundaries. Preserve manual event intervals."""
    start, end = seed["start"], seed["end"]
    desired = max(end - start, options["target_seconds"])
    desired = min(desired, options["max_seconds"], duration)
    extra = max(0, desired - (end - start))
    start, end = max(0, start - extra / 2), min(duration, end + extra / 2)
    if end - start < desired:
        start, end = max(0, end - desired), min(duration, start + desired)
    segments = transcript.get("segments", [])
    for boundary in ("start", "end"):
        value = start if boundary == "start" else end
        crossing = next((s for s in segments if s["start"] < value < s["end"]), None)
        if crossing:
            a, b = (crossing["start"], end) if boundary == "start" else (start, crossing["end"])
            if b - a <= options["max_seconds"] + .001:
                start, end = a, b
    return {"start": round(start, 6), "end": round(end, 6)}


def build_drafts(candidates, events, options, transcript, duration):
    """Use grounded local topics as an index; global packing never invents PTS."""
    mandatory = [{"start": e["start"], "end": e["end"], "title": e["label"], "score": 101,
                  "reason": "用户指定必剪事件，比赛时间校准定位，需人工预览确认", "event_id": e["id"]}
                 for e in events if e["start"] is not None]
    ranked = sorted(candidates, key=lambda c: (-c.get("score", 0), c["start"]))
    warnings, drafts = [], []
    form = options["format"]

    def draft(parts, title, reason, seed=None):
        identity = "d" + fingerprint({"parts": parts, "title": title})[:10]
        from core.clip_rating import rating_for_ranges
        score,details=rating_for_ranges(parts,[seed] if seed is not None else candidates)
        drafts.append({"id": identity, "title": title, "description": reason,
                       "score":score,"score_details":details,"category":(seed or {}).get('category','commentary'),
                       "reason": reason, "segments": parts, "status": "pending", "subtitle_text": None})

    if form != "montage":
        for seed in mandatory + ranked:
            # Mandatory events may exceed the requested count; leave a visible
            # conflict rather than lose the event to count/ranking truncation.
            if len(drafts) >= options["count"] and not seed.get("event_id"):
                break
            if seed["end"] - seed["start"] > options["max_seconds"]:
                warnings.append(f"{seed['title']} 超过最长时长；保留事件要求并等待人工解决")
                if not seed.get("event_id"):
                    continue
                part = {"start": seed["start"], "end": seed["end"]}
            else:
                if form == "shorts" and seed["end"] - seed["start"] >= options["min_seconds"]:
                    part = {"start": seed["start"], "end": seed["end"]}
                else:
                    part = _fit(seed, options, transcript, duration)
            if any(s["start"] <= seed["start"] and s["end"] >= seed["end"] for d in drafts for s in d["segments"]):
                continue
            overlap = any(max(0, min(s["end"], part["end"]) - max(s["start"], part["start"])) > .5 * min(s["end"] - s["start"], part["end"] - part["start"])
                          for d in drafts for s in d["segments"])
            if overlap and not seed.get("event_id"):
                continue
            draft([part], seed["title"], seed.get("reason", "根据真实转录索引选择；连续保留上下文"),seed)
    else:
        # Mandatory material forms the skeleton; optional candidates fill the
        # remaining target budget. Only complete source intervals are admitted.
        packs = [[] for _ in range(options["count"])]
        for part in _merge(mandatory):
            choices = [(sum(s["end"] - s["start"] for s in _merge(pack + [part])), i) for i, pack in enumerate(packs)]
            _, index = min(choices)
            packs[index] = _merge(packs[index] + [part])
        for seed in ranked:
            part = {"start": seed["start"], "end": seed["end"]}
            if any(max(s["start"], part["start"]) < min(s["end"], part["end"]) for pack in packs for s in pack):
                continue
            eligible = [(sum(s["end"] - s["start"] for s in pack), i) for i, pack in enumerate(packs)
                        if sum(s["end"] - s["start"] for s in pack) < options["target_seconds"] and
                        sum(s["end"] - s["start"] for s in pack) + part["end"] - part["start"] <= options["max_seconds"]]
            if eligible:
                _, index = min(eligible)
                packs[index] = _merge(packs[index] + [part])
        for index, pack in enumerate(packs, 1):
            if pack:
                topics = sorted(candidates, key=lambda c: -sum(max(0, min(s['end'],c['end'])-max(s['start'],c['start'])) for s in pack))
                title = topics[0]['title'] if topics else f"精选集锦 {index}"
                draft(pack, title, "按实际话题组织，保留有转录依据的完整承接段")
    if len(drafts) != options["count"]:
        warnings.append(f"目标 {options['count']} 条，生成 {len(drafts)} 条；不为凑数量重复或虚构内容")
    for d in drafts:
        seconds = sum(s["end"] - s["start"] for s in d["segments"])
        if not options["min_seconds"] <= seconds <= options["max_seconds"]:
            warnings.append(f"{d['title']}：{seconds:.1f} 秒超出设定范围；需调整后才可正式导出")
    return drafts, warnings


def create_editing_plan(project_id, cfg, options, mock=False, skip_render=True):
    """Existing transcription suffices. No ASR, source deletion or legacy review edits."""
    from core.pipeline import load_clip_transcript
    from core.clip_selector import select_clips
    options = normalize_editing_options(options)
    with PlanStore(cfg["database"]) as db:
        project = db.get_project(project_id)
        if not project:
            raise ValueError("找不到项目")
        clock_report = None
        if options.get('clock_mode') == 'auto' and options['events'] and not mock:
            from core.match_clock import auto_calibrate_options
            options, clock_report = auto_calibrate_options(project, cfg, options)
        if project['metadata'].get('virtual_timeline'):
            from core.batch_timeline import create_batch_plan
            return create_batch_plan(project,cfg,options,mock,clock_report=clock_report)
        transcript = load_clip_transcript(project, {"transcript_key": project["metadata"].get("transcript_key")})
        transcript_key = fingerprint(transcript)
        cfg = copy.deepcopy(cfg)
        prompt = Path(cfg["prompt_path"]).read_text(encoding="utf-8")
        prompt += "\n\n本项目内容标准：\n" + CONTENT_PROMPTS[options["content"]]
        prompt += "\n用户筛选偏好（只影响主题取舍，不可覆盖时间戳和证据校验）：\n" + options["prompt"]
        prompt += ("\n本轮为全局剪辑建立话题索引。每个候选代表一个完整话题或事件，尽量保留铺垫；后续程序会组织连续长段或集锦。"
                   if options["format"] != "shorts" else "\n请高召回提取独立短片。")
        if options['format']=='shorts':
            prompt += f"\n本素材目标约{options['count']}条，单条{options['min_seconds']:g}～{options['max_seconds']:g}秒，目标{options['target_seconds']:g}秒。结合每个窗口占全片的比例高召回提取完整观点，内容允许少量铺垫重叠，但不可重复同一主题凑数。每条保留具体评分与依据。"
        identity = {"project": project_id, "transcript": transcript_key, "options": options, "mock": bool(mock),
                    "prompt": prompt, "selection": cfg["selection"], "model": cfg["openai"].get("model"),
                    "provider": cfg["openai"].get("base_url"), "version": 2, **cache_identity(cfg["openai"])}
        if clock_report:
            identity['clock_verified'] = clock_report['verified_events']
        plan_id = fingerprint(identity)
        # The source project lock serializes caches and shares the same durable
        # API budget with legacy selection and every alternative editorial plan.
        with project_lock(project["project_dir"]):
            # Keep the exact normalized snapshot referenced by new plans;
            # preserve the original imported/raw transcript separately.
            snapshot = Path(project["project_dir"]) / "transcripts" / f"{transcript_key}.json"
            if not snapshot.is_file():
                atomic_json(snapshot, transcript)
            existing = db.get_plan(plan_id)
            if existing:
                plan = existing
            else:
                directory = Path(project["project_dir"]) / "plans" / plan_id
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "prompt.md").write_text(prompt, encoding="utf-8")
                cfg["prompt_path"] = str(directory / "prompt.md")
                cfg["_budget_path"] = str(Path(project["project_dir"]) / "selection/api_budget.json")
                cfg["selection"].update(category_lengths={}, min_seconds=1 if options["format"] != "shorts" else options["min_seconds"],
                    max_seconds=cfg["selection"]["window_seconds"] if options["format"] != "shorts" else options["max_seconds"],
                    max_candidates=10000)
                # Audio hints are optional and must be tied to this transcription's source.
                event_path = Path(project["project_dir"]) / "audio_events.json"
                audio = read_json(event_path) if event_path.is_file() else []
                candidates = select_clips(transcript, project["metadata"], cfg, directory, mock=mock, audio_events=audio)
                from core.match_clock import locate_verified_events
                events = locate_verified_events(options, project["duration"], clock_report)
                drafts, warnings = build_drafts(candidates, events, options, transcript, project["duration"])
                warnings.extend((clock_report or {}).get('warnings', []))
                plan = {"id": plan_id, "project_id": project_id, "options": options, "transcript_key": transcript_key,
                        "mock": bool(mock), "drafts": drafts, "events": events, "warnings": warnings,
                        "index_count": len(candidates), "index_path": str(directory / "selection" / ("mock_decisions.json" if mock else "decisions.json")), "revision": 0}
                if clock_report:
                    plan['clock_report'] = clock_report
                validate_plan_document(plan, project["duration"])
                atomic_json(directory / "initial_plan.json", plan)
                plan = db.insert_plan(plan)
    errors = []
    if not skip_render:
        for draft in plan["drafts"]:
            try:
                preview_plan_draft(plan_id, draft["id"], cfg)
            except Exception as error:
                LOG.exception("方案预览失败，保留草稿")
                errors.append({"draft_id": draft["id"], "error": str(error)})
    return {"project_id": project_id, "plan_id": plan_id, "draft_count": len(plan["drafts"]),
            "warnings": plan["warnings"], "errors": errors}


def preview_plan_draft(plan_id, draft_id, cfg):
    from core.plan_renderer import render_plan_draft
    return render_plan_draft(plan_id, draft_id, cfg, preview=True)


def export_editing_plan(plan_id, cfg, orientations=("horizontal",), export_options=None):
    from core.plan_renderer import render_plan_draft
    from core.export_options import project_export_options
    orientations = tuple(dict.fromkeys(orientations))
    if not orientations or any(o not in ("horizontal", "vertical") for o in orientations):
        raise ValueError("请选择横屏或竖屏")
    with PlanStore(cfg["database"]) as db:
        plan = db.get_plan(plan_id)
        if not plan:
            raise ValueError("找不到剪辑方案")
        blockers = export_blockers(plan)
        if blockers:
            raise ValueError("必剪事件或时长要求未解决：" + "；".join(blockers))
        approved = [d for d in plan["drafts"] if d["status"] == "approved"]
        if not approved:
            raise ValueError("没有通过审批的草稿")
        options = project_export_options(db, plan["project_id"], cfg, export_options)
    results, errors = [], []
    for draft in approved:
        for orientation in orientations:
            try:
                result = render_plan_draft(plan_id, draft["id"], cfg, orientation=orientation,
                                           export_options=options, expected_revision=plan["revision"])
                results.append(result)
            except Exception as error:
                LOG.exception("方案导出失败，保留已完成文件")
                errors.append({"draft_id": draft["id"], "orientation": orientation, "error": str(error)})
    with PlanStore(cfg["database"]) as db:
        current = db.get_plan(plan_id)
        if current["revision"] != plan["revision"]:
            errors.append({"error": "导出期间方案已修改，本次文件仅作历史，不能作为当前发布清单"})
            results = []
    report = {"plan_id": plan_id, "revision": plan["revision"], "exports": results, "errors": errors}
    path = Path(cfg["output_dir"]) / "plan_exports" / plan_id[:12] / f"manifest_r{plan['revision']}_{fingerprint(options)[:8]}.json"
    atomic_json(path, report)
    return {**report, "manifest": str(path)}
