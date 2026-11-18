"""Editorial choices, match-clock calibration, plan review and safe queued export."""
from __future__ import annotations

from pathlib import Path
import math
import streamlit as st

from core.editing import coverage_report, export_blockers
from core.editing_options import (CONTENT_LABELS, FORMAT_LABELS, PERIOD_LABELS,
                                  normalize_editing_options)
from core.jobs import submit_job
from core.match_events import format_event_text, parse_event_text
from core.subtitle import composition_cues, cues_srt, validate_srt
from core.utils import fingerprint
from storage.plans import PlanStore


def _format_changed(prefix):
    long = st.session_state[prefix + "format"] != "shorts"
    for field, value in {"count": 2 if long else 10, "min_seconds": 300 if long else 90,
                         "target_seconds": 600 if long else 120, "max_seconds": 1200 if long else 180}.items():
        st.session_state[prefix + field] = value if field == "count" else float(value)


def editorial_controls(cfg, prefix="editing-", forced_format=None, initial_options=None):
    """Usable both during import and while reusing an existing transcript."""
    with PlanStore(cfg["database"]) as store:
        templates = store.list_templates()
    template = st.selectbox("个人模板", ["不载入模板"] + list(templates), key=prefix + "template")
    if st.button("载入模板", key=prefix + "load", disabled=template == "不载入模板"):
        for field, value in templates[template].items():
            if field not in ("anchors", "events"):
                if field == 'clock_mode':
                    st.session_state[prefix + 'auto-clock'] = value == 'auto'
                else:
                    st.session_state[prefix + field] = value
        st.success("已载入内容与时长偏好；本场比赛校准和事件需单独填写。")
    defaults = normalize_editing_options(initial_options) if initial_options else normalize_editing_options()
    for field in ("content", "format", "count", "min_seconds", "target_seconds", "max_seconds", "prompt", "event_before", "event_after"):
        st.session_state.setdefault(prefix + field, defaults[field])
    left, right = st.columns(2)
    content = left.selectbox("内容标准", list(CONTENT_LABELS), format_func=CONTENT_LABELS.get, key=prefix + "content")
    if forced_format:
        if st.session_state[prefix + 'format'] != forced_format:
            st.session_state[prefix + 'format'] = forced_format
            _format_changed(prefix)
        form = forced_format
        right.caption('成品形式：' + FORMAT_LABELS[form])
    else:
        form = right.selectbox("成品形式", list(FORMAT_LABELS), format_func=FORMAT_LABELS.get, key=prefix + "format",
                               on_change=_format_changed, args=(prefix,))
    a, b = st.columns(2)
    count = a.number_input("目标条数", 1, 60, key=prefix + "count", step=1)
    target = b.number_input("每条目标时长（秒）", 1.0, 10800.0, key=prefix + "target_seconds", step=1.0)
    a, b = st.columns(2)
    low = a.number_input("每条最短时长（秒）", 1.0, 10800.0, key=prefix + "min_seconds", step=1.0)
    high = b.number_input("每条最长时长（秒）", 1.0, 10800.0, key=prefix + "max_seconds", step=1.0)
    prompt = st.text_area("自定义筛选提示词" if content == "custom" else "额外筛选要求（可选）",
                          key=prefix + "prompt", height=110,
                          placeholder="例如：重点保留球员表现与争论，保留观点前因后果，略过重复读弹幕。")
    st.caption("条数与目标时长是选片偏好，素材不足时不凑数。内部转录块大小不影响成品长度。")
    anchors, events, event_error = [], [], None
    with st.expander("比赛分钟校准与必剪事件（可选）", expanded=bool(defaults['events'])):
        st.caption('G = 进球 · R = 红牌 · V = VAR。事件用空格分隔，斜杠前是上半场，斜杠后是下半场；下半场使用累计分钟。')
        original_text = format_event_text(defaults['events'])
        st.session_state.setdefault(prefix + 'event-text', original_text)
        event_text = st.text_input('必剪事件（一行填写）', key=prefix + 'event-text',
            placeholder='G23:10 R40 / G61 V90+3:20',
            help='也支持 23:10G、40R。只填下半场时先写 /；补时写 45+2:10 或 90+3:20。可用 E35[事件说明] 填其他事件；再用 / 可填写加时上下半场。')
        try:
            events = parse_event_text(event_text)
            if event_text == original_text:
                events = defaults['events']  # Preserve old per-event buffers and descriptions.
            if events:
                st.caption('已识别 ' + str(len(events)) + ' 个事件：' + '；'.join(
                    f"{PERIOD_LABELS[e['period']]} {e['match_time']} {e['label']}" for e in events))
        except ValueError as error:
            event_error = str(error)
        st.session_state.setdefault(prefix + 'auto-clock', defaults.get('clock_mode', 'auto' if not defaults['anchors'] else 'manual') == 'auto')
        automatic = st.checkbox('自动识别记分牌并对轴', key=prefix + 'auto-clock',
            help='生成方案时自动抽取录像顶部画面，通过当前选择的模型来源读取比赛时间，分别核对上下半场及补时。')
        if automatic:
            st.caption('生成方案时自动识别，无需填写录像分秒。使用「设置 → 模型与 API」中当前保存的来源，发送少量抽帧，消耗该来源的额度；已完成识别会缓存。')
        else:
            st.caption('高级手动校准：比赛时间填累计分钟，录像时间填秒数或 HH:MM:SS；有删节可分别填写连续区间。')
            initial_anchors=[{'阶段':PERIOD_LABELS[a['period']],'比赛时间':a['match_time'],'录像时间':str(a['video_time']),
                              '录像区间开始':str(a['video_start']) if 'video_start' in a else '',
                              '录像区间结束':str(a['video_end']) if 'video_end' in a else ''} for a in defaults['anchors']]
            anchor_rows = st.data_editor(initial_anchors or [{"阶段": "上半场", "比赛时间": "", "录像时间": "", "录像区间开始": "", "录像区间结束": ""}],
                num_rows='dynamic', hide_index=True, key=prefix + 'anchors',
                column_config={'阶段': st.column_config.SelectboxColumn(options=list(PERIOD_LABELS.values()), required=True)})
            reverse = {v: k for k, v in PERIOD_LABELS.items()}
            for row in anchor_rows:
                if row.get('比赛时间') or row.get('录像时间'):
                    item = {'period': reverse.get(row.get('阶段'), ''), 'match_time': row.get('比赛时间', ''), 'video_time': row.get('录像时间', '')}
                    for label, field in (('录像区间开始', 'video_start'), ('录像区间结束', 'video_end')):
                        if row.get(label):
                            item[field] = row[label]
                    anchors.append(item)
        a, b = st.columns(2)
        before = a.number_input("事件前保留（秒）", 0.0, 600.0, key=prefix + "event_before", step=1.0)
        after = b.number_input("事件后保留（秒）", 0.0, 600.0, key=prefix + "event_after", step=1.0)
        st.caption("按这里设置的前后秒数保留事件范围；生成草稿后，可预览核对并确认。")
    options = None
    try:
        if event_error:
            raise ValueError(event_error)
        options = normalize_editing_options({"content": content, "format": form, "count": int(count),
            "target_seconds": target, "min_seconds": low, "max_seconds": high, "prompt": prompt,
            "anchors": anchors, "events": events, "event_before": before, "event_after": after,
            "clock_mode": 'auto' if automatic else 'manual'})
    except ValueError as error:
        st.warning(str(error))
    with st.expander("保存为个人模板"):
        name = st.text_input("模板名称", key=prefix + "template-name")
        if st.button("保存个人模板", key=prefix + "save-template", disabled=options is None or not name.strip()):
            with PlanStore(cfg["database"]) as store:
                store.save_template(name, options)
            st.success("模板已保存；仅保存内容与时长偏好，不复用本场比赛的校准点和事件。")
    return options


def _maybe_number(value):
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else value


def _plan_subtitle_default(project, plan, draft, cfg, segments):
    from core.editing import validate_segments, load_plan_transcript
    segments = validate_segments(segments, project["duration"], plan["options"]["format"])
    if draft.get("subtitle_text") is not None and segments == draft["segments"]:
        return draft["subtitle_text"]
    transcript = load_plan_transcript(project, plan["transcript_key"])
    return cues_srt(composition_cues(transcript, segments, cfg["subtitle"].get("max_chars", 18)))


def _begin_subtitle_edit(project, plan, draft, cfg, segments, key, regenerate=False):
    try:
        source = {**draft, "subtitle_text": None} if regenerate else draft
        text = _plan_subtitle_default(project, plan, source, cfg, segments)
        st.session_state[key + "srt"] = text
        st.session_state[key + "srt-ranges"] = fingerprint(segments)
        st.session_state[key + "automatic"] = False
        st.session_state.pop(key + "subtitle-error", None)
    except (OSError, ValueError, KeyError) as error:
        st.session_state[key + "subtitle-error"] = str(error)


def _import_plan_srt(key, segments):
    uploaded = st.session_state.get(key + "srt-upload")
    if uploaded is None:
        return
    try:
        data = uploaded.getvalue()
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("SRT 文件请控制在 2 MiB 以内")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")
        validate_srt(text, sum(s["end"] - s["start"] for s in segments))
        st.session_state[key + "srt"] = text
        st.session_state[key + "srt-ranges"] = fingerprint(segments)
        st.session_state.pop(key + "subtitle-error", None)
    except (ValueError, UnicodeError, KeyError) as error:
        st.session_state[key + "subtitle-error"] = f"字幕未载入，原编辑内容已保留：{error}"


def _plan_subtitle_editor(project, plan, draft, cfg, segments, key):
    st.markdown("**字幕编辑**")
    st.session_state.setdefault(key + "automatic", draft.get("subtitle_text") is None)
    automatic = st.checkbox("使用自动字幕", key=key + "automatic",
                            help="取消勾选或点击「编辑字幕」，会载入整条拼接正文的字幕供修改。")
    if automatic:
        st.caption("可以在自动识别的字幕基础上修改，也可以导入自己的 SRT。")
        st.button("编辑字幕", key=key + "edit-subtitles", on_click=_begin_subtitle_edit,
                  args=(project, plan, draft, cfg, segments, key))
        if st.session_state.get(key + "subtitle-error"):
            st.warning(st.session_state[key + "subtitle-error"])
        return None, False
    if key + "srt" not in st.session_state:
        try:
            st.session_state[key + "srt"] = _plan_subtitle_default(project, plan, draft, cfg, segments)
        except (OSError, ValueError, KeyError) as error:
            st.session_state[key + "srt"] = draft.get("subtitle_text") or ""
            st.session_state[key + "subtitle-error"] = f"自动字幕无法载入，可粘贴或导入自己的 SRT：{error}"
        st.session_state[key + "srt-ranges"] = fingerprint(segments)
    st.caption("时间从整条拼接正文的 00:00:00 开始，已自动去掉各段间被剪掉的时间；不包含片头。修改后点击「保存草稿审核」。")
    from ui.subtitle_tools import subtitle_tools
    subtitle_tools(key+'srt',sum(s['end']-s['start'] for s in segments))
    subtitle = st.text_area("字幕（SRT，可直接修改文字和时间）", key=key + "srt", height=280)
    st.button("按当前分段重新生成字幕", key=key + "regenerate-subtitles", on_click=_begin_subtitle_edit,
              args=(project, plan, draft, cfg, segments, key, True), help="用当前分段的自动字幕替换编辑框内容。")
    with st.expander("导入 / 下载 SRT"):
        upload = st.file_uploader("导入自己的 SRT 字幕", type=["srt"], key=key + "srt-upload",
                                  help="支持 UTF-8 或 GB18030；时间需相对于整条拼接正文，最大 2 MiB。")
        st.button("载入上传的 SRT", disabled=upload is None, key=key + "import-srt", on_click=_import_plan_srt,
                  args=(key, segments))
        st.download_button("下载当前字幕 SRT", data=subtitle.encode("utf-8-sig"),
                           file_name=f"{draft['id']}.srt", mime="application/x-subrip", key=key + "download-srt")
    if st.session_state.get(key + "subtitle-error"):
        st.warning(st.session_state[key + "subtitle-error"])
    stale = st.session_state.get(key + "srt-ranges") != fingerprint(segments)
    if stale:
        st.warning("分段范围已改变。请按当前分段重新生成字幕，或导入已按新时间轴校准的 SRT，再保存。")
    return subtitle, stale


def editing_page(cfg):
    from ui.app import _export_customization
    st.title("剪辑方案")
    st.caption("同一转录可创建多套独立方案：选择内容 → 设置成品 → 核对事件 → 审核导出")
    with PlanStore(cfg["database"]) as store:
        projects = store.list_projects()
        if not projects:
            st.info("请先在「项目」页导入录像。可勾选使用独立剪辑方案，或仅准备转录后在这里创建。")
            return
        by_id = {p["id"]: p for p in projects}
        project_id = st.selectbox("方案所属项目", list(by_id), key="editing-project",
                                  format_func=lambda p: f"{by_id[p]['name']} · {p[:8]}")
        project = by_id[project_id]
        available = Path(project["source"]).is_file()
        if not available:
            st.info("原录像不可访问。可以利用完整转录规划方案；恢复原录像后才能生成预览和成品。")
        with st.expander("新建剪辑方案", expanded=not store.list_plans(project_id)):
            options = editorial_controls(cfg, f"new-plan-{project_id}-")
            skip = st.checkbox("先生成草稿，稍后按需生成预览", value=True, key=f"plan-skip-{project_id}")
            mock = st.checkbox("模拟选择（仅测试，不调用 LLM）", value=False, key=f"plan-mock-{project_id}")
            if st.button("生成独立方案", type="primary", disabled=options is None, key=f"plan-create-{project_id}"):
                try:
                    job = submit_job("plan", {"project_id": project_id, "options": options, "mock": mock, "skip_render": skip}, cfg)
                    st.success(f"方案任务已加入队列 · {job['id'][:8]}。不会重新识别语音；完成后刷新方案列表。")
                except Exception as error:
                    st.error(str(error))
        st.button("刷新方案列表", key="refresh-editing-plans")
        plans = store.list_plans(project_id)
        if not plans:
            st.info("尚无独立方案。先创建一套；如果提示缺少转录，请在项目页重新处理或导入对应转录。")
            return
        by_plan = {p["id"]: p for p in plans}
        plan_id = st.selectbox("选择剪辑方案", list(by_plan), key=f"choose-plan-{project_id}",
            format_func=lambda p: f"{CONTENT_LABELS[by_plan[p]['options']['content']]} · {FORMAT_LABELS[by_plan[p]['options']['format']]} · {len(by_plan[p]['drafts'])} 条 · {p[:8]}")
        plan = by_plan[plan_id]
        st.caption(f"方案 {plan_id} · 修订 {plan['revision']} · 目标 {plan['options']['count']} 条，每条 {plan['options']['min_seconds']:g}～{plan['options']['max_seconds']:g} 秒")
        if plan["mock"]:
            st.warning("这是模拟选择方案，不代表真实 LLM 分析。")
        with st.expander("初始规划提示与筛选要求"):
            st.write(plan["options"]["prompt"] or "使用所选内容标准的默认提示词")
            for warning in plan["warnings"]:
                st.write(warning)
        prefix = f"review-plan-{plan_id}-{plan['revision']}-"
        coverage = coverage_report(plan)
        if coverage:
            if plan.get('clock_report'):
                with st.expander('自动比赛对轴结果'):
                    st.dataframe(plan['clock_report']['anchors'], hide_index=True)
            st.subheader("必剪事件覆盖")
            labels = {"unresolved": "未定位", "unconfirmed": "待预览确认", "covered": "已确认且草稿覆盖", "missing": "缺少覆盖", "waived": "已明确免除"}
            st.caption("确认表示已检查事件和完整范围。若不需要此事件，请明确勾选免除；只批准草稿不会自动确认比赛事件。")
            rows = [{"事件ID": e["id"], "事件": f"{PERIOD_LABELS[e['period']]} {e['match_time']} · {e['label']}",
                     "录像起点秒": e["start"], "录像终点秒": e["end"], "已预览确认": e["confirmed"], "免除必剪": e["waived"],
                     "覆盖": labels[e["coverage"]], "定位说明": e["note"]} for e in coverage]
            edited = st.data_editor(rows, hide_index=True, key=prefix + "event-review",
                disabled=["事件ID", "事件", "覆盖", "定位说明"], column_config={
                    "录像起点秒": st.column_config.NumberColumn(min_value=0, max_value=project["duration"], step=.1),
                    "录像终点秒": st.column_config.NumberColumn(min_value=0, max_value=project["duration"], step=.1)})
            if st.button("保存事件确认与范围", key=prefix + "event-save"):
                try:
                    edits = {r["事件ID"]: {"start": _maybe_number(r["录像起点秒"]), "end": _maybe_number(r["录像终点秒"]),
                                            "confirmed": bool(r["已预览确认"]), "waived": bool(r["免除必剪"])} for r in edited}
                    store.edit_plan(plan_id, plan["revision"], event_edits=edits)
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
        if plan["drafts"]:
            st.subheader("草稿审核")
            by_draft = {d["id"]: d for d in plan["drafts"]}
            draft_id = st.selectbox("选择草稿", list(by_draft), key=f"draft-{plan_id}",
                format_func=lambda d: f"{by_draft[d]['title']} · {sum(s['end']-s['start'] for s in by_draft[d]['segments']):.1f} 秒 · {by_draft[d]['status']}")
            draft = by_draft[draft_id]
            key = prefix + draft_id
            if draft.get("preview") and Path(draft["preview"]).is_file():
                st.video(draft["preview"])
            else:
                st.caption("尚无当前修订的预览；可保存编辑后按需生成。")
            st.write(draft["reason"])
            title = st.text_input("草稿标题", value=draft["title"], key=key + "title")
            rows = st.data_editor(draft["segments"], num_rows="dynamic" if plan["options"]["format"] == "montage" else "fixed",
                                  hide_index=True, key=key + "segments", column_config={
                "start": st.column_config.NumberColumn("原录像起点（秒）", min_value=0, max_value=project["duration"], step=.1, required=True),
                "end": st.column_config.NumberColumn("原录像终点（秒）", min_value=0, max_value=project["duration"], step=.1, required=True)})
            st.caption("集锦按原录像时间排序；可删除承接段或增加区间。必剪事件范围仍需完整覆盖。")
            status_labels = {"pending": "待审核", "approved": "通过", "rejected": "拒绝"}
            status = st.selectbox("草稿审批", list(status_labels), index=list(status_labels).index(draft["status"]), format_func=status_labels.get, key=key + "status")
            subtitle, stale_subtitle = _plan_subtitle_editor(project, plan, draft, cfg, rows, key)
            if st.button("保存草稿审核", key=key + "save", disabled=stale_subtitle):
                try:
                    store.edit_plan(plan_id, plan["revision"], {draft_id: {"title": title, "status": status,
                        "segments": rows, "subtitle_text": subtitle}})
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
            if st.button("生成当前草稿预览", disabled=not available, key=key + "preview"):
                try:
                    job = submit_job("plan_preview", {"plan_id": plan_id, "draft_id": draft_id}, cfg)
                    st.success(f"预览任务已排队 · {job['id'][:8]}。使用已保存的草稿范围。")
                except Exception as error:
                    st.error(str(error))
            outputs = [r for d in plan["drafts"] for r in d.get("outputs", {}).values()]
            if outputs:
                st.dataframe([{"标题": r["title"], "画幅": r["orientation"], "文件": r["video"]} for r in outputs], hide_index=True)
        else:
            st.warning("没有生成草稿。可以调整提示词，或补充校准点后创建新方案；不会为凑数量制造内容。")
        with st.expander("导出已批准草稿"):
            blockers = export_blockers(plan)
            for blocker in blockers:
                st.warning(blocker)
            formats = st.multiselect("方案输出画幅", ["horizontal", "vertical"], default=["horizontal"],
                                    format_func=lambda f: "横屏" if f == "horizontal" else "竖屏", key=f"plan-formats-{plan_id}")
            export_options, valid = _export_customization(project, store, cfg)
            approved = any(d["status"] == "approved" for d in plan["drafts"])
            if st.button("导出方案成品", type="primary", key=f"plan-export-{plan_id}",
                         disabled=not available or not valid or not formats or not approved or bool(blockers)):
                try:
                    job = submit_job("plan_export", {"plan_id": plan_id, "orientations": formats, "export_options": export_options}, cfg)
                    st.success(f"方案导出任务已排队 · {job['id'][:8]}。在任务队列查看结果。")
                except Exception as error:
                    st.error(str(error))
