"""Local, durable human review UI. Run: streamlit run ui/app.py."""

from __future__ import annotations

import json
import os
import hashlib
import html
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import streamlit as st
except ImportError as exc:
    raise SystemExit("缺少 Streamlit。请先运行 python -m pip install -r requirements.txt") from exc

from core.config import load_config
from core.jobs import cancel_job, ensure_worker, get_job, list_jobs, retry_job, submit_job, tail_job_log
from core.export_options import normalize_export_options
from core.subtitle_styles import SUBTITLE_PRESETS
from core.version import RELEASE, VERSION
from storage.database import Database

STATUS_LABELS = {"pending": "待审核", "approved": "已通过", "rejected": "已拒绝"}
JOB_LABELS = {"queued": "等待处理", "running": "正在处理", "completed": "已完成",
              "failed": "处理失败", "interrupted": "意外中断", "cancelling": "正在取消", "cancelled": "已取消"}
JOB_KINDS = {"process": "处理录像", "batch": "批量处理", "export": "导出成品", "preview": "生成预览"}
JOB_KINDS.update(plan="生成剪辑方案", plan_preview="方案预览", plan_export="方案导出")
JOB_KINDS.update(compose_sync='双路自动对轴', compose_render='双路合成渲染')
JOB_KINDS.update(compose_prepare='主播素材顺序拼接', compose_visual='比赛事件画面对轴')
JOB_KINDS.update(workflow_prepare='工作台生成方案',workflow_preview='工作台预览',workflow_transcribe='工作台转录',workflow_export='统一成品导出')
JOB_KINDS.update(workflow_titles='生成平台标题', workflow_subtitle_review='字幕校对')
CATEGORY_LABELS = {
    "goal_reaction": "进球相关反应", "tactical_analysis": "战术分析", "tactics": "战术观点",
    "player_review": "球员评价", "player_evaluation": "球员评价", "coach_review": "教练评价",
    "referee_controversy": "裁判争议", "funny": "搞笑吐槽", "humor": "搞笑吐槽",
    "debate": "激烈争论", "surprise": "意外反应", "halftime_summary": "中场总结",
    "postmatch_review": "赛后复盘", "opinion": "完整观点", "chat_interaction": "弹幕互动",
    "audio_event": "声音提示", "other": "其他",
    "commentary": "解说观点",
}


def clock_time(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _project_picker(projects: list[dict], key: str) -> dict | None:
    if not projects:
        st.info("还没有项目。请在「项目」页导入本地录像，或运行 process 命令。")
        return None
    index = {p["id"]: p for p in projects}
    selected = st.selectbox("选择项目", list(index), key=key,
                            format_func=lambda project_id: f"{index[project_id]['name']} · {clock_time(index[project_id]['duration'])} · {project_id[:8]}")
    return index[selected]


def _source_available(project: dict) -> bool:
    return bool(project.get("source")) and Path(project["source"]).is_file()


def _missing_source_notice(project: dict) -> None:
    if not _source_available(project):
        st.info("原片已移除或路径不可访问；可回看已有预览和成品，审批记录与字幕仍可查看和修改。"
                "恢复原路径后才能重新生成预览或导出。")


def _current_exports(clip: dict) -> list[dict]:
    """Only media matching the current render and title revisions is a fallback."""
    if clip.get("status") != "approved" or not clip.get("active", True):
        return []
    return [record for _, record in sorted(clip.get("exports", {}).items())
            if record.get("render_revision") == clip.get("render_revision")
            and record.get("metadata_revision") == clip.get("metadata_revision")
            and record.get("path") and Path(record["path"]).is_file()]


def _playback_media(clip: dict) -> tuple[str | None, bool]:
    preview = clip.get("preview_path")
    if preview and Path(preview).is_file():
        return preview, False
    exports = _current_exports(clip)
    return (exports[0]["path"], True) if exports else (None, False)


def _save_intro_upload(upload, cfg: dict) -> str:
    """Keep a user-selected asset outside disposable processing caches."""
    import tempfile
    from core.file_access import restore_inherited_access

    limit = 200 * 1024 * 1024
    if upload.size <= 0:
        raise ValueError("片头文件为空，请重新选择视频。")
    if upload.size > limit:
        raise ValueError("片头上传上限为 200 MB；较大的文件请填写本地路径。")
    suffix = Path(upload.name).suffix.lower()
    if suffix not in {".mp4", ".mkv", ".mov", ".flv", ".webm"}:
        raise ValueError("请选择 MP4、MKV、MOV、FLV 或 WebM 视频片头。")
    digest = hashlib.sha256()
    upload.seek(0)
    while chunk := upload.read(1024 * 1024):
        digest.update(chunk)
    directory = Path(cfg["output_dir"]) / "assets" / "intros"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (digest.hexdigest() + suffix)
    if destination.is_file() and destination.stat().st_size == upload.size:
        upload.seek(0)
        return str(destination.resolve())
    temporary = None
    try:
        # A same-directory rename publishes only complete files, including when
        # two sessions upload the same hash at once. Never reuse a partial target.
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".intro-", suffix=".tmp",
                                         dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            upload.seek(0)
            written = 0
            while chunk := upload.read(1024 * 1024):
                stream.write(chunk)
                written += len(chunk)
            if written != upload.size:
                raise ValueError("片头上传不完整，请重新选择并上传。")
            stream.flush()
            os.fsync(stream.fileno())
        restore_inherited_access(temporary)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        upload.seek(0)
    return str(destination.resolve())


def _preset_changed(prefix: str) -> None:
    preset = SUBTITLE_PRESETS[st.session_state[prefix + "preset"]]
    for key, value in preset.items():
        if key != "label":
            st.session_state[prefix + key] = value


def _export_customization(project: dict, db: Database, cfg: dict) -> tuple[dict | None, bool]:
    """Project preferences and a queue-time snapshot are intentionally separate."""
    saved = db.get_export_options(project["id"])
    defaults = normalize_export_options({"subtitle": cfg["subtitle"],
                                          "burn_subtitles": cfg["render"].get("burn_subtitles", True)})
    if saved:
        defaults = normalize_export_options(saved)
    prefix = f"export-options-{project['id']}-"
    for key, value in defaults["subtitle"].items():
        st.session_state.setdefault(prefix + key, value)
    st.session_state.setdefault(prefix + "burn", defaults["burn_subtitles"])
    st.session_state.setdefault(prefix + "intro-enabled", defaults["intro"]["enabled"])
    st.session_state.setdefault(prefix + "intro-path", defaults["intro"]["path"])
    st.markdown("**字幕外观**")
    style_col, color_col, outline_col = st.columns([2, 1, 1])
    style_col.selectbox("字幕样式", list(SUBTITLE_PRESETS),
                        format_func=lambda value: SUBTITLE_PRESETS[value]["label"], key=prefix + "preset",
                        on_change=_preset_changed, args=(prefix,))
    color_col.color_picker("文字颜色", key=prefix + "primary_color")
    outline_col.color_picker("描边 / 底框颜色", key=prefix + "outline_color")
    font_col, size_col, vertical_col = st.columns([2, 1, 1])
    font_col.text_input("字幕字体", key=prefix + "font",
                        help="请填写电脑上已安装的字体名称；缺少该字体时由系统替代。")
    for field in ('font_size','vertical_font_size'):
        st.session_state[prefix+field]=float(st.session_state[prefix+field])
    size_col.number_input("横屏字号", min_value=12., max_value=240., step=.5,
                          key=prefix + "font_size")
    vertical_col.number_input("竖屏字号", min_value=12., max_value=240., step=.5,
                              key=prefix + "vertical_font_size")
    border_col, shadow_col, bold_col, burn_col = st.columns(4)
    border_col.number_input("描边粗细", min_value=0.0, max_value=20.0, step=0.5,
                            key=prefix + "outline")
    shadow_col.number_input("阴影大小", min_value=0.0, max_value=20.0, step=0.5,
                            key=prefix + "shadow")
    bold_col.checkbox("字幕加粗", key=prefix + "bold")
    burn_col.checkbox("视频烧录字幕", key=prefix + "burn",
                       help="关闭后仍会导出独立的 SRT 和 ASS 字幕文件。")
    style_preview = st.container()
    st.markdown("**统一片头**")
    intro_enabled = st.checkbox("给本批视频添加片头", key=prefix + "intro-enabled")
    upload = st.file_uploader("上传自选片头（可选）", type=["mp4", "mkv", "mov", "flv", "webm"],
                              key=prefix + "intro-upload", disabled=not intro_enabled,
                              help="上限 200 MB；文件会保存在输出目录，较大片头可直接填写本地路径。")
    upload_error = None
    if intro_enabled and upload is not None:
        upload_id = getattr(upload, "file_id", None) or (upload.name, upload.size)
        if st.session_state.get(prefix + "uploaded") != upload_id:
            try:
                st.session_state[prefix + "intro-path"] = _save_intro_upload(upload, cfg)
                st.session_state[prefix + "uploaded"] = upload_id
            except Exception as exc:
                upload_error = f"片头上传失败：{exc}"
                st.warning(upload_error)
    intro_path = st.text_input("片头文件路径", key=prefix + "intro-path", disabled=not intro_enabled,
                                placeholder="D:/videos/我的片头.mp4",
                                help="使用本机文件的完整路径，支持中文和空格；不会移动您的素材。")
    st.caption("片头批量应用到本次所有视频和画幅，自动适配尺寸；正文字幕从片头结束后开始。")
    options, valid = None, False
    try:
        options = normalize_export_options({
            "subtitle": {key: st.session_state[prefix + key] for key in defaults["subtitle"]},
            "burn_subtitles": st.session_state[prefix + "burn"],
            "intro": {"enabled": intro_enabled, "path": intro_path.strip().strip('"')},
        })
        valid = not intro_enabled or (bool(options["intro"]["path"]) and Path(options["intro"]["path"]).is_file())
        if intro_enabled and not valid:
            st.warning("请选择存在的片头视频；保存设置或提交导出前需恢复此路径。")
        if intro_enabled and valid:
            with st.expander("预览所选片头", expanded=False):
                st.video(options["intro"]["path"])
        style = options["subtitle"]
        foreground, edge = style["primary_color"], style["outline_color"]
        font = html.escape(json.dumps(style["font"], ensure_ascii=False), quote=True)
        thickness = min(float(style["outline"]), 3)
        shadows = ",".join(f"{x}px {y}px 0 {edge}" for x, y in
                            ((-thickness, 0), (thickness, 0), (0, -thickness), (0, thickness)))
        background = edge if style["preset"] == "boxed" else "#24313D"
        with style_preview:
            st.markdown(f'<div style="padding:14px;text-align:center;background:{background};border-radius:4px;'
                        f'color:{foreground};font-family:{font},sans-serif;font-size:24px;'
                        f'font-weight:{700 if style["bold"] else 400};text-shadow:{shadows}">'
                        '进球了！这一刻属于足球</div>', unsafe_allow_html=True)
            st.caption("字幕样式示意；最终字体与比例以渲染成品为准。")
    except (ValueError, TypeError, KeyError) as exc:
        st.warning(f"导出设置需要调整：{exc}")
    valid = valid and not upload_error
    if st.button("保存导出设置", key=prefix + "save"):
        if options is None or not valid:
            st.error("无法保存：请先修正字幕设置，并选择可访问的片头视频。")
        else:
            db.set_export_options(project["id"], options)
            st.success("导出设置已保存，下次打开此项目会继续使用。")
    return options, valid


def _export_panel(project: dict, db: Database, cfg: dict) -> None:
    clips = db.list_clips(project["id"], "approved")
    with st.expander(f"批量导出 · {len(clips)} 条已通过", expanded=False):
        formats = st.multiselect("输出画幅", ["horizontal", "vertical"], default=["horizontal"],
                                 format_func=lambda x: "横屏 16:9" if x == "horizontal" else "竖屏 9:16 · 模糊背景",
                                 key=f"formats-{project['id']}")
        options, valid_options = _export_customization(project, db, cfg)
        st.caption("按当前审批结果输出高清视频、字幕、标题和说明。已编辑的片段会重新渲染。")
        if not _source_available(project):
            st.caption("原片已移除，已停用重新导出；下方保留已有成品的文件位置。")
        if st.button("导出已通过的切片", type="primary", disabled=not clips or not formats or not _source_available(project) or not valid_options,
                     key=f"export-{project['id']}"):
            try:
                job = submit_job("export", {"project_id": project["id"], "orientations": formats,
                                            "export_options": options}, cfg)
                st.success(f"导出任务已加入队列 · {job['id'][:8]}。在「任务队列」查看进度与结果。")
            except Exception as exc:
                st.error(f"无法提交导出：{exc}")
        outputs = []
        for clip in clips:
            for orientation, record in clip.get("exports", {}).items():
                outputs.append({"标题": clip["title"], "画幅": "横屏" if orientation == "horizontal" else "竖屏", "文件": record["path"]})
        if outputs:
            st.dataframe(outputs, use_container_width=True, hide_index=True)


def project_page(db: Database, cfg: dict) -> None:
    st.title("项目")
    st.caption("导入录像 → 自动生成候选 → 人工审批 → 导出成品")
    projects = db.list_projects()
    with st.expander("导入本地录像", expanded=not projects):
        with st.container():
            import_mode = st.selectbox("导入方式", ["单个录像", "录像文件夹"])
            modes = ["hybrid", "semantic", "audio"]
            selection_mode = st.selectbox("选片模式", modes, index=modes.index(cfg["selection"].get("mode", "hybrid")),
                format_func=lambda mode: {"hybrid": "语义 + 声音辅助（高召回）", "semantic": "语义驱动", "audio": "仅声音辅助（不调用 LLM）"}[mode])
            path = st.text_input("录像路径", placeholder="D:/videos/曼城 vs 利物浦.mp4")
            transcript_path = st.text_input("已有转录 JSON 路径（可选；模拟模式必填）", placeholder="D:/videos/transcript.json",
                                             help="复用已有时间戳转录，跳过语音识别。模拟模式仅使用已有转录验证选片与渲染。")
            c1, c2 = st.columns(2)
            with c1:
                track = st.number_input("音轨序号（从 0 开始）", min_value=0, value=0, step=1,
                                        help="0 为第一条音轨。OBS 多音轨录像可指定含解说的音轨。")
                skip_render = st.checkbox("暂不生成视频预览", value=False)
                recursive = st.checkbox("文件夹包含子目录", value=False)
            with c2:
                allow_cpu = st.checkbox("CUDA 不可用时允许 CPU 回退", value=False,
                    help="缺少 cuBLAS / cuDNN DLL 或 GPU 显存不足时，可用 CPU 继续。CPU 转录更慢；修复 CUDA 环境后可切回 GPU。")
                mock = st.checkbox("模拟模式（仅测试流程）", value=False,
                                   help="需要提供已有转录 JSON；生成明确标记的模拟候选，不代表真实 AI 分析结果。")
            editorial = None
            use_editorial = st.checkbox("使用独立剪辑方案（比赛 / 对话 / 自定义；短片 / 长段 / 集锦）", value=False)
            transcript_only = st.checkbox("仅准备转录，稍后创建方案", value=False)
            if use_editorial:
                from ui.plans_page import editorial_controls
                editorial = editorial_controls(cfg, "import-editing-")
            submitted = st.button("开始处理", type="primary", disabled=(use_editorial and editorial is None) or (use_editorial and transcript_only))
        if submitted:
            if not path.strip():
                st.error("请输入本地录像的完整路径。")
            elif mock and not transcript_path.strip():
                st.error("模拟模式需要已有转录 JSON。可先运行 python main.py demo，自动生成测试视频、转录和候选。")
            else:
                try:
                    payload = {"audio_track": int(track), "skip_render": skip_render, "mock": mock,
                               "allow_cpu": allow_cpu, "selection_mode": selection_mode,
                               "transcript_path": transcript_path.strip().strip('"') or None}
                    if use_editorial:
                        payload["editing_options"] = editorial
                    if transcript_only:
                        payload["transcript_only"] = True
                    if import_mode == "录像文件夹":
                        if mock or transcript_path.strip():
                            raise ValueError("文件夹批量处理请使用真实转录；已有转录 JSON 请逐个导入对应录像，避免错配。")
                        payload.update(directory=path.strip().strip('"'), recursive=recursive)
                        kind = "batch"
                    else:
                        payload["path"] = path.strip().strip('"')
                        kind = "process"
                    job = submit_job(kind, payload, cfg)
                    st.success(f"任务已加入队列 · {job['id'][:8]}。可关闭网页；在「任务队列」查看进度，完成后进入审批。")
                except Exception as exc:
                    st.error(f"无法提交任务：{exc}")
    if not projects:
        st.info("支持 MP4、MKV、FLV。建议先用 5～10 分钟素材验证音轨与字幕。")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("录像", len(projects))
    c2.metric("候选", sum(p["candidate_count"] for p in projects))
    c3.metric("待审核", sum(p["pending_count"] for p in projects))
    c4.metric("已通过", sum(p["approved_count"] for p in projects))
    st.dataframe([{"录像": p["name"], "时长": clock_time(p["duration"]), "处理状态": p["status"],
                   "候选": p["candidate_count"], "待审核": p["pending_count"], "通过": p["approved_count"],
                   "拒绝": p["rejected_count"], "项目 ID": p["id"]} for p in projects],
                 use_container_width=True, hide_index=True)
    project = _project_picker(projects, "project-details")
    st.caption(f"源文件：{project['source']}")
    _missing_source_notice(project)
    if project.get("error"):
        st.warning(f"上次处理未完成：{project['error']}")
    with st.expander("项目信息与处理记录"):
        st.json(project.get("metadata", {}))
        st.code(f'python main.py process "{project["source"]}"', language="bash")
    _export_panel(project, db, cfg)


def _subtitle_default(clip: dict, project: dict | None = None, cfg: dict | None = None) -> str:
    if clip.get("subtitle_text") is not None:
        return clip["subtitle_text"]
    if clip.get("preview_path"):
        candidate = Path(clip["preview_path"]).with_suffix(".srt")
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8-sig")
    for record in _current_exports(clip):
        video = Path(record["path"])
        for candidate in (video.with_suffix(".srt"), video.parent / "subtitles.srt"):
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8-sig")
    if project and project.get("project_dir") and cfg:
        from core.pipeline import load_clip_transcript
        from core.subtitle import transcript_cues
        cues = transcript_cues(load_clip_transcript(project, clip), clip, cfg["subtitle"].get("max_chars", 18))

        def timestamp(seconds):
            total = max(0, round(seconds * 1000))
            hours, rest = divmod(total, 3600000)
            minutes, rest = divmod(rest, 60000)
            secs, millis = divmod(rest, 1000)
            return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

        return "\n\n".join(f"{i}\n{timestamp(cue['start'])} --> {timestamp(cue['end'])}\n{cue['text']}"
                           for i, cue in enumerate(cues, 1)) + ("\n" if cues else "")
    return ""


def review_page(db: Database, cfg: dict) -> None:
    st.header("审批工作台")
    project_col, filter_col = st.columns([3, 2])
    with project_col:
        project = _project_picker(db.list_projects(), "review-project")
    if not project:
        return
    _missing_source_notice(project)
    all_clips = db.list_clips(project["id"])
    if not all_clips:
        st.info("此项目还没有候选。请先完成选片；处理失败时可从原命令恢复。")
        if project.get("error"):
            st.error(project["error"])
        return
    approved = sum(c["status"] == "approved" for c in all_clips)
    pending = sum(c["status"] == "pending" for c in all_clips)
    st.caption(f"{len(all_clips)} 条候选 · {pending} 条待审核 · {approved} 条通过 · 原录像 {clock_time(project['duration'])}")
    with filter_col:
        selected_status = st.radio("筛选状态", ["pending", "all", "approved", "rejected"], horizontal=True,
                                   format_func=lambda x: "全部" if x == "all" else STATUS_LABELS[x])
    clips = all_clips if selected_status == "all" else [c for c in all_clips if c["status"] == selected_status]
    if not clips:
        st.success("当前筛选下没有候选。可切换到其他状态，或导出已通过的片段。")
        _export_panel(project, db, cfg)
        return
    lookup = {c["id"]: c for c in clips}
    options = list(lookup)
    selection_key = f"clip-selection-{project['id']}-{selected_status}"
    next_id = st.session_state.pop("_next_clip_id", None)
    if next_id in options:
        st.session_state[selection_key] = next_id
    elif st.session_state.get(selection_key) not in options:
        st.session_state[selection_key] = options[0]
    clip_id = st.selectbox("候选片段", options, key=selection_key,
                           format_func=lambda cid: f"{options.index(cid) + 1:02} · {lookup[cid]['title']} · {clock_time(lookup[cid]['start'])}")
    clip = lookup[clip_id]
    index = options.index(clip_id)
    prior, progress, following = st.columns([1, 4, 1])
    if prior.button("上一条", disabled=index == 0, use_container_width=True):
        st.session_state["_next_clip_id"] = options[index - 1]
        st.rerun()
    progress.progress((index + 1) / len(options), text=f"当前 {index + 1} / {len(options)}")
    if following.button("下一条", disabled=index + 1 >= len(options), use_container_width=True):
        st.session_state["_next_clip_id"] = options[index + 1]
        st.rerun()
    left, right = st.columns([1.4, 1], gap="large")
    with left:
        media_path, is_export = _playback_media(clip)
        if media_path:
            st.video(media_path)
            if is_export:
                st.caption("正在播放与当前审批版本一致的已导出成品。")
            st.caption("使用播放器拖动进度。播放速度选项由浏览器播放器提供。")
        elif not _source_available(project):
            st.info("当前范围没有可播放的预览或匹配当前审批版本的成品；原片已移除，无法重建。"
                    "历史成品仍保留在原输出文件夹。")
        else:
            st.info("当前范围尚无预览。生成预览后即可播放；修改起止时间或字幕后需要重新生成。")
        if st.button("生成 / 刷新此条预览", key=f"render-{clip_id}", disabled=not _source_available(project)):
            try:
                job = submit_job("preview", {"project_id": project["id"], "clip_id": clip_id}, cfg)
                st.success(f"预览任务已加入队列 · {job['id'][:8]}。任务完成后刷新本页即可播放。")
            except Exception as exc:
                st.error(f"无法提交预览：{exc}")
        c1, c2, c3 = st.columns(3)
        c1.metric("时长", f"{clip['end'] - clip['start']:.1f} 秒")
        c2.metric("推荐评分", f"{clip['score']:g}")
        c3.metric("状态", STATUS_LABELS[clip["status"]])
        st.caption(f"原片 {clock_time(clip['start'])} — {clock_time(clip['end'])} · {CATEGORY_LABELS.get(clip['category'], clip['category'])}")
        st.write(clip["description"])
        st.markdown("**选片依据**")
        st.write(clip["reason"])
        if clip.get("tags"):
            st.caption(" · ".join(clip["tags"]))
        st.caption("评分仅供排序；声音增强提示不等于真实进球事件，请结合画面判断。")
    with right:
        st.subheader("修改与决定")
        default_subtitles = _subtitle_default(clip, project, cfg)
        edit_key = f"{clip_id}-{clip['revision']}"
        automatic = st.checkbox("使用自动字幕", value=clip.get("subtitle_text") is None,
                                key=f"auto-subtitle-{edit_key}", help="启用后会根据当前时间范围重新生成字幕。关闭后可编辑 SRT。")
        subtitle_key=f'legacy-subtitle-{edit_key}'
        st.session_state.setdefault(subtitle_key,default_subtitles)
        if not automatic:
            from ui.subtitle_tools import subtitle_tools
            subtitle_tools(subtitle_key,clip['end']-clip['start'])
        with st.form(f"review-{edit_key}"):
            title = st.text_input("标题", value=clip["title"], max_chars=180)
            start_col, end_col = st.columns(2)
            start = start_col.number_input("原片开始（秒）", min_value=0.0, max_value=float(project["duration"]),
                                           value=float(clip["start"]), step=0.1, format="%.3f")
            end = end_col.number_input("原片结束（秒）", min_value=0.0, max_value=float(project["duration"]),
                                       value=float(clip["end"]), step=0.1, format="%.3f")
            subtitle_text = st.text_area("字幕（SRT，时间相对于切片开头）", key=subtitle_key,
                                         height=220, disabled=automatic,
                                         help="格式：序号、00:00:01,000 --> 00:00:03,000、字幕文字；每段之间空一行。")
            advance = st.checkbox("决定后自动切换下一条", value=True)
            a, b = st.columns(2)
            approve = a.form_submit_button("通过", type="primary", use_container_width=True)
            reject = b.form_submit_button("拒绝", use_container_width=True)
            defer = st.form_submit_button("暂不决定", use_container_width=True)
            save = st.form_submit_button("仅保存修改", use_container_width=True)
        if approve or reject or defer or save:
            status = "approved" if approve else "rejected" if reject else "pending" if defer else clip["status"]
            try:
                if not automatic and (start, end) != (clip["start"], clip["end"]) and subtitle_text == default_subtitles:
                    raise ValueError("时间范围已改变，请选择自动字幕，或同步调整手工 SRT 时间轴。")
                db.review_clip(clip_id, status, title=title, start=start, end=end,
                               subtitle_text=None if automatic else subtitle_text, reset_subtitle=automatic)
                st.session_state["_review_notice"] = "审批与修改已保存。"
                if advance and not save and len(options) > 1:
                    st.session_state["_next_clip_id"] = options[(index + 1) % len(options)]
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(f"无法保存：{exc}")
        with st.expander("修改记录"):
            history = db.review_history(clip_id)
            if not history:
                st.caption("尚未修改；当前值来自最初的 AI 候选。")
            else:
                st.dataframe([{"时间（UTC）": h["created_at"], "状态": STATUS_LABELS[h["after"]["status"]],
                               "标题": h["after"]["title"], "开始": h["after"]["start"], "结束": h["after"]["end"]}
                              for h in history], use_container_width=True, hide_index=True)
            st.json(clip["original"])
    _export_panel(project, db, cfg)


def stats_page(db: Database) -> None:
    st.title("数据统计")
    st.caption("按内容类别比较候选数量、审批通过率和平均时长，用于调整后续选片规则。")
    stats = db.stats()
    if not stats:
        st.info("处理第一场录像后，这里会显示候选与审批统计。")
        return
    st.dataframe([{"类别": CATEGORY_LABELS.get(r["category"], r["category"]), "候选": r["candidate_count"],
                   "待审": r["pending_count"], "通过": r["approved_count"], "拒绝": r["rejected_count"],
                   "平均时长（秒）": round(r["average_duration"], 1),
                   "全部候选通过率": f"{r['approval_rate']:.0%}",
                   "已审核通过率": "—" if r["reviewed_approval_rate"] is None else f"{r['reviewed_approval_rate']:.0%}"}
                  for r in stats], use_container_width=True, hide_index=True)
    st.caption("全部候选通过率 = 通过 ÷ 全部候选；已审核通过率 = 通过 ÷（通过 + 拒绝）。待审不会被当作拒绝。")
    st.download_button("下载统计 JSON", json.dumps(stats, ensure_ascii=False, indent=2),
                       file_name="clip_statistics.json", mime="application/json")


def jobs_page(cfg: dict) -> None:
    st.title("任务队列")
    st.caption("任务在后台顺序执行。可在列表右侧取消任务，已完成的缓存和文件保留。关闭网页不影响处理；意外中断可手动重试。")
    automatic = st.checkbox("每 3 秒刷新进度", value=True)

    @st.fragment(run_every=3 if automatic else None)
    def content():
        ensure_worker(cfg)
        jobs = list_jobs(cfg)
        if st.button("立即刷新任务"):
            st.rerun()
        if not jobs:
            st.info("暂无任务。到「项目」导入录像，或在审批后提交导出。")
            return
        waiting, running, finished = st.columns(3)
        waiting.metric("等待", sum(j["status"] == "queued" for j in jobs))
        running.metric("处理中", sum(j["status"] in ("running", "cancelling") for j in jobs))
        finished.metric("完成", sum(j["status"] == "completed" for j in jobs))
        with st.container(height=380, border=True):
            for j in jobs:
                name, source, status, action = st.columns([3, 4, 2, 1])
                name.write(j['id'][:8] + ' · ' + JOB_KINDS[j['kind']])
                name.caption('执行次数：' + str(j['attempt']) + ' · ' + j['created_at'])
                source.caption(j['payload'].get('path') or j['payload'].get('directory') or j['payload'].get('project_id') or j['payload'].get('workspace_id') or '—')
                status.write(JOB_LABELS[j['status']])
                if j['status'] in ('queued', 'running', 'cancelling'):
                    if action.button('取消中…' if j['status']=='cancelling' else '取消', key='cancel-'+j['id'], disabled=j['status']=='cancelling'):
                        try:
                            cancel_job(j['id'], cfg)
                            st.rerun()
                        except ValueError as error:
                            st.warning(str(error))
                elif j['status'] in ('failed', 'interrupted'):
                    from ui.budget_approval import budget_action
                    with action:
                        budget_action(j, cfg, 'queue-budget-'+j['id'], compact=True)
        by_id = {j["id"]: j for j in jobs}
        selected = st.selectbox("任务详情", list(by_id), format_func=lambda jid: f"{jid[:8]} · {JOB_KINDS[by_id[jid]['kind']]} · {JOB_LABELS[by_id[jid]['status']]}")
        job = by_id[selected]
        if job["status"] == "completed":
            st.success("任务已完成。处理录像后可进入审批工作台；导出任务的文件路径见下方结果。")
        elif job["status"] in ("failed", "interrupted", "cancelled"):
            (st.info if job['status']=='cancelled' else st.error)(job.get("error") or "任务未完成。")
            st.caption("已完成的缓存、预览和审批记录会保留。调整配置或恢复素材路径后重试。")
            from ui.budget_approval import budget_action
            needs_approval = budget_action(job, cfg, 'details-budget-'+job['id'])
            if not needs_approval and st.button("重新执行此任务" if job['status']=='cancelled' else "重试此任务", key=f"retry-{job['id']}"):
                retry_job(job["id"], cfg)
                st.rerun()
        elif job['status'] == 'cancelling':
            st.info('正在停止当前任务及其子进程，已完成的缓存和文件会保留。')
        elif job.get("error"):
            st.warning(job["error"])
        else:
            st.info(JOB_LABELS[job["status"]] + "；转录与渲染进度见实时日志。")
        if job.get("result") is not None:
            result = job["result"]
            if isinstance(result, dict) and result.get("exports"):
                st.caption(f"本次已输出 {len(result['exports'])} 个视频；每个视频所在文件夹同时包含字幕与发布文案。")
                st.dataframe([{"标题": item.get("title", ""),
                               "画幅": "横屏" if item.get("orientation") == "horizontal" else "竖屏",
                               "文件": item.get("video", "")} for item in result["exports"]],
                             use_container_width=True, hide_index=True)
            elif isinstance(result, dict) and "candidate_count" in result:
                st.caption(f"已保存 {result['candidate_count']} 条候选，可在审批工作台选择该项目。")
            with st.expander("完整任务结果", expanded=False):
                st.json(result)
        with st.expander("任务参数"):
            st.json(job["payload"])
        st.markdown("**最新日志**")
        with st.container(height=320):
            st.code(tail_job_log(job, cfg), language="log")

    content()


def legacy_main() -> None:
    st.set_page_config(page_title="Football Clip Factory", page_icon="⚽", layout="wide")
    st.sidebar.title("Football Clip Factory")
    selected_release = os.environ.get("FCF_RELEASE_VERSION", RELEASE)
    st.sidebar.caption(f"足球直播 · 本地切片工厂 · v{VERSION} · {selected_release}")
    page = st.sidebar.radio("工作区", ["项目", "审批工作台", "剪辑方案", "双路合成", "任务队列", "数据统计"])
    st.sidebar.divider()
    st.sidebar.caption("审批自动保存在本地。通过后再输出高清成品。")
    notice = st.session_state.pop("_review_notice", None)
    if notice:
        st.success(notice)
    try:
        cfg = load_config()
        mode = {"hybrid": "语义 + 声音辅助", "semantic": "语义驱动", "audio": "声音辅助"}.get(cfg["selection"].get("mode", "hybrid"), "语义 + 声音辅助")
        st.sidebar.caption(f"默认选片：{mode}")
        st.sidebar.caption(f"语音识别配置：{cfg['asr']['model']} · {cfg['asr']['device']}（请求设备）")
        st.sidebar.caption("配置值不代表 GPU 已可用。实际运行设备见任务日志；可运行 python main.py doctor 检查环境。")
        if cfg["asr"]["device"] != "cpu":
            st.sidebar.caption("遇到 CUDA / DLL 错误：导入时勾选「允许 CPU 回退」，或在 .env 设置 DEVICE=cpu。")
        if cfg["openai"].get("api_key") and cfg["openai"].get("model"):
            st.sidebar.caption("LLM 密钥与模型：已配置（尚未验证连接）")
        else:
            st.sidebar.caption("LLM 密钥或模型：未配置；可运行模拟演示，或仅使用声音辅助模式。")
        ensure_worker(cfg)
        with Database(cfg["database"]) as db:
            if page == "项目":
                project_page(db, cfg)
            elif page == "审批工作台":
                review_page(db, cfg)
            elif page == "任务队列":
                jobs_page(cfg)
            elif page == '双路合成':
                from ui.composite_page import composite_page
                composite_page(cfg)
            elif page == "剪辑方案":
                from ui.plans_page import editing_page
                editing_page(cfg)
            else:
                stats_page(db)
    except Exception as exc:
        st.error(f"无法读取项目配置或数据：{exc}")
        st.info("请检查 config/settings.yaml 和数据库所在目录权限，然后刷新。")


def main():
    from ui.workbench import main as workbench_main
    workbench_main()


if __name__ == "__main__":
    main()
