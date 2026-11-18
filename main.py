from __future__ import annotations

import argparse
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import subprocess
import sys

from core.config import load_config
from core.version import VERSION, RELEASE


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def doctor(cfg):
    report = {"version": VERSION, "release": RELEASE, "python": sys.version.split()[0], "executable": sys.executable, "tools": {}, "packages": {},
              "api": {"key_configured": bool(cfg["openai"]["api_key"]), "model": cfg["openai"]["model"],
                      "base_url": cfg["openai"]["base_url"]}, "model_download": "本检查不下载模型"}
    for name in ("ffmpeg", "ffprobe"):
        try:
            result = subprocess.run([cfg[name], "-version"], capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            report["tools"][name] = {"ok": result.returncode == 0, "path": cfg[name], "version": result.stdout.splitlines()[0]}
        except (OSError, subprocess.SubprocessError, IndexError) as exc:
            report["tools"][name] = {"ok": False, "error": str(exc)}
    local = cfg['asr'].get('engine') == 'zipformer_http'
    packages = ('yaml','numpy','streamlit') if local else ('yaml','numpy','streamlit','faster_whisper','ctranslate2')
    for package in packages:
        report["packages"][package] = importlib.util.find_spec(package) is not None
    if local:
        from core.local_asr import ZipformerHTTP
        try:
            report['asr'] = ZipformerHTTP(cfg['asr']).health
            ready = True
        except RuntimeError as exc:
            report['asr'] = {'ok':False, 'error':str(exc)}
            ready = False
        dependencies = all(report['packages'].values())
        media = all(x['ok'] for x in report['tools'].values())
        report['ok'] = dependencies and media and ready
        report['readiness'] = {'media_tools': media, 'dependencies': dependencies,
            'selected_engine': 'zipformer_http', 'selected_device': 'cpu', 'selected_device_ready': ready,
            'cloud_asr': False, 'note': '本地 CPU 中文语音识别；选片和可选文字校对仍使用单独的 LLM 设置。'}
        return report
    from core.gpu_runtime import prepare_cuda_runtime
    report["cuda_runtime"] = prepare_cuda_runtime()
    try:
        import ctranslate2
        report["cuda_devices"] = ctranslate2.get_cuda_device_count()
        report["cpu_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cpu"))
        if report["cuda_devices"]:
            report["cuda_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
    except Exception as exc:
        report["cuda_error"] = str(exc)
    cuda_ready = (bool(report.get("cuda_devices")) and bool(report.get("cuda_compute_types"))
                  and report["cuda_runtime"]["usable"] is not False)
    selected_device_ready = (bool(report.get("cpu_compute_types")) if cfg["asr"]["device"] == "cpu" else cuda_ready)
    requires_asr = cfg["selection"].get("mode", "hybrid") != "audio"
    report["ok"] = (all(x["ok"] for x in report["tools"].values()) and all(report["packages"].values())
                    and (selected_device_ready or not requires_asr))
    report["readiness"] = {"media_tools": all(x["ok"] for x in report["tools"].values()),
                           "dependencies": all(report["packages"].values()),
                           "llm_configured": bool(cfg["openai"]["api_key"] and cfg["openai"]["model"]),
                           "whisper_model": cfg["asr"]["model"], "selected_device": cfg["asr"]["device"],
                           "cuda_device_visible": bool(report.get("cuda_devices")),
                           "cuda_runtime_dlls_usable": report["cuda_runtime"]["usable"],
                           "cuda_ready": cuda_ready, "selected_device_ready": selected_device_ready,
                           "selection_mode": cfg["selection"].get("mode", "hybrid"),
                           "note": "分别检查显卡可见性与 DLL 可加载性；模型实际 GPU 推理和 API 仍需任务验证。"}
    return report


def build_parser():
    parser = argparse.ArgumentParser(description="Football Clip Factory · 足球直播 AI 切片与人工审批")
    parser.add_argument("--config", help="自定义 settings.yaml 路径（放在子命令前）")
    parser.add_argument("--version", action="version", version=f"Football Clip Factory {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    def editorial_arguments(p):
        p.add_argument("--editing-options", help="剪辑方案 JSON（含比赛时间校准和必剪事件）")
        p.add_argument("--content", choices=["match", "conversation", "custom"])
        p.add_argument("--format", choices=["shorts", "continuous", "montage"])
        p.add_argument("--target-count", type=int)
        p.add_argument("--target-seconds", type=float)
        p.add_argument("--min-seconds", type=float)
        p.add_argument("--max-seconds", type=float)
        p.add_argument("--prompt-file", help="UTF-8 自定义筛选提示词")
    for name in ("process", "batch"):
        p = sub.add_parser(name, help="处理单个视频" if name == "process" else "依次处理目录视频；单个失败不阻断队列")
        p.add_argument("path")
        p.add_argument("--audio-track", type=int, default=0, help="音轨序号，从 0 开始；先用 probe 查看")
        p.add_argument("--skip-render", action="store_true")
        p.add_argument("--allow-cpu", action="store_true", help="CUDA 失败时允许 CPU/int8 回退")
        p.add_argument("--mode", choices=["semantic", "hybrid", "audio"], help="语义、混合或纯声音候选；覆盖配置")
        p.add_argument("--background", action="store_true", help="加入持久后台队列，关闭窗口后任务继续")
        p.add_argument("--transcript-only", action="store_true", help="仅准备音频和转录，不调用 LLM")
        editorial_arguments(p)
        if name == "process":
            p.add_argument("--transcript", help="导入已有转录 JSON；时间必须以原视频首帧为零")
            p.add_argument("--mock", action="store_true", help="模拟选片；必须搭配 --transcript，不访问 API")
        else:
            p.add_argument("--recursive", action="store_true")
    p = sub.add_parser("probe", help="读取视频及音轨信息")
    p.add_argument("path")
    p.add_argument("--audio-track", type=int, default=0)
    p = sub.add_parser('workspaces', help='列出五步工作台项目与当前进度')
    p = sub.add_parser('workflow-export', help='导出工作台当前方案中已通过的成品')
    p.add_argument('--workspace', required=True)
    p.add_argument('--orientation', choices=['horizontal','vertical','both'], default='horizontal')
    p.add_argument('--background', action='store_true')
    p = sub.add_parser("export", help="只导出通过审批的切片")
    p.add_argument("--project", required=True)
    p.add_argument("--approved-only", action="store_true", default=True)
    p.add_argument("--orientation", choices=["horizontal", "vertical", "both"], default="horizontal")
    opening = p.add_mutually_exclusive_group()
    opening.add_argument("--intro", help="为本次每条成品添加自己的片头视频")
    opening.add_argument("--no-intro", action="store_true", help="本次不使用已保存的片头")
    p.add_argument("--subtitle-preset", choices=["classic", "gold", "cyan", "comic", "boxed"])
    p.add_argument("--subtitle-color", help="字幕文字颜色，例如 #FFD34E")
    p.add_argument("--subtitle-font", help="已安装的字体名称，例如 Microsoft YaHei")
    p.add_argument("--burn-subtitles", action=argparse.BooleanOptionalAction, default=None,
                   help="是否烧录字幕；关闭时仍输出 SRT/ASS")
    p.add_argument("--background", action="store_true", help="加入后台渲染队列")
    p = sub.add_parser("jobs", help="查看持久任务队列或重试失败任务")
    p.add_argument("--retry", metavar="JOB_ID", help="重新排队失败或中断的任务")
    p.add_argument("--job", metavar="JOB_ID", help="查看单项任务及日志")
    p = sub.add_parser("plan", help="复用项目转录生成独立剪辑方案")
    p.add_argument("--project", required=True)
    editorial_arguments(p)
    p.add_argument("--mock", action="store_true", help="只使用模拟选择，不调用模型")
    p.add_argument("--skip-render", action="store_true")
    p.add_argument("--background", action="store_true")
    p = sub.add_parser("plans", help="列出项目方案，或查看方案和事件覆盖")
    p.add_argument("--project")
    p.add_argument("--plan")
    p = sub.add_parser("plan-review", help="审批草稿、修改源区间或确认必剪事件")
    p.add_argument("--plan", required=True)
    p.add_argument("--draft")
    p.add_argument("--status", choices=["approved", "rejected", "pending"])
    p.add_argument("--title")
    p.add_argument("--segments", help="有序源区间 JSON 文件")
    p.add_argument("--srt", help="相对整条草稿的 SRT")
    p.add_argument("--reset-subtitles", action="store_true")
    p.add_argument("--events", help="事件编辑 JSON：事件 id 到 start/end/confirmed/waived 的对象")
    p = sub.add_parser("plan-preview", help="生成剪辑方案草稿预览")
    p.add_argument("--plan", required=True)
    p.add_argument("--draft", required=True)
    p.add_argument("--background", action="store_true")
    p = sub.add_parser("plan-export", help="仅导出已批准并满足事件要求的方案草稿")
    p.add_argument("--plan", required=True)
    p.add_argument("--orientation", choices=["horizontal", "vertical", "both"], default="horizontal")
    p.add_argument("--export-options", help="可选字幕/片头设置 JSON；默认读取项目已保存设置")
    p.add_argument("--background", action="store_true")
    p = sub.add_parser('compose-create', help='创建比赛录像 + 主播录像的双路合成')
    p.add_argument('match')
    p.add_argument('commentator')
    p.add_argument('--append-commentator', action='append', default=[], metavar='VIDEO', help='追加主播素材，可重复指定，按参数顺序首尾拼接')
    p.add_argument('--background', action='store_true')
    p.add_argument('--match-track', type=int, default=0)
    p.add_argument('--commentator-track', type=int, default=0)
    p = sub.add_parser('composites', help='列出双路合成项目')
    p.add_argument('--composite')
    p = sub.add_parser('compose-sync', help='全片声音匹配；可选 ASR/LLM 事件辅助')
    p.add_argument('--composite', required=True)
    p.add_argument('--semantic', action='store_true')
    p.add_argument('--allow-cpu', action='store_true')
    p.add_argument('--background', action='store_true')
    p = sub.add_parser('compose-visual-sync', help='从限定范围抽帧，用开球/进球等同一画面辅助对轴')
    p.add_argument('--composite', required=True)
    p.add_argument('--search', required=True, help='搜索参数 JSON，含两路录像 start/end、event、samples')
    p.add_argument('--frames-only', action='store_true', help='只生成画面对照图，不调用 API')
    p.add_argument('--background', action='store_true')
    p = sub.add_parser('compose', help='预览或导出双路画中画，偏移定义为主播时间减比赛时间')
    p.add_argument('--composite', required=True)
    p.add_argument('--options', help='合成设置 JSON；正式导出需要 sync_confirmed=true')
    p.add_argument('--segments', help='比赛录像区间数组 JSON；默认全部公共时间')
    p.add_argument('--preview', action='store_true')
    p.add_argument('--preview-start', type=float)
    p.add_argument('--background', action='store_true')
    sub.add_parser("projects", help="列出项目与审批数量")
    p = sub.add_parser("clips", help="列出项目切片")
    p.add_argument("--project", required=True)
    p = sub.add_parser("review", help="保存一条审批或人工修改")
    p.add_argument("--clip", required=True)
    p.add_argument("--status", choices=["approved", "rejected", "pending"], required=True)
    p.add_argument("--title")
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--srt", help="自定义相对时间轴 SRT 文件")
    p.add_argument("--reset-subtitles", action="store_true")
    p = sub.add_parser("preview", help="按当前人工修改重新生成预览")
    p.add_argument("--project", required=True)
    p.add_argument("--clip", required=True)
    sub.add_parser("stats", help="按内容类别统计审批率")
    sub.add_parser("doctor", help="检查媒体工具、依赖、CUDA 和 API 配置（不调用 API）")
    p = sub.add_parser("demo", help="生成 90 秒测试图视频、模拟转录和真实预览；无需 API/模型")
    p.add_argument("--approve", action="store_true", help="仅对模拟项目自动登记前三条测试审批")
    p.add_argument("--export", action="store_true", help="同时导出横屏和竖屏测试成品")
    p.add_argument("--full-hd", action="store_true", help="使用配置的高清分辨率；默认演示为 960x540 / 540x960")
    return parser


def editing_arguments(args, force=False):
    from core.editing_options import normalize_editing_options
    value = json.loads(Path(args.editing_options).read_text(encoding="utf-8-sig")) if args.editing_options else {}
    for arg, field in [("content", "content"), ("format", "format"), ("target_count", "count"),
                       ("target_seconds", "target_seconds"), ("min_seconds", "min_seconds"), ("max_seconds", "max_seconds")]:
        if getattr(args, arg, None) is not None:
            value[field] = getattr(args, arg)
    if args.prompt_file:
        value["prompt"] = Path(args.prompt_file).read_text(encoding="utf-8-sig")
    return normalize_editing_options(value) if value or force else None


def main(argv=None):
    # Windows redirected console streams otherwise often use a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        logs = Path(cfg["output_dir"]) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            handlers=[logging.StreamHandler(), RotatingFileHandler(logs / "factory.log", maxBytes=5_000_000,
                                                                                   backupCount=3, encoding="utf-8")])
        from core.pipeline import process_video, batch_process, export_project, render_preview
        from storage.database import Database
        if getattr(args, "mode", None):
            cfg["selection"]["mode"] = args.mode
        editorial = editing_arguments(args, args.command == "plan") if args.command in ("process", "batch", "plan") else None
        if args.command in ("process", "batch") and args.transcript_only and editorial is not None:
            raise ValueError("--transcript-only 与剪辑方案参数不能同时使用")
        if args.command in ('workspaces','workflow-export'):
            from storage.workspaces import WorkspaceStore
            from core.workflow import adopt_legacy, export_snapshot, run_workflow_export
            adopt_legacy(cfg)
            with WorkspaceStore(cfg['database']) as store:
                if args.command == 'workspaces':
                    result = [{'id':w['id'],'name':w['name'],'step':w['step'],'status':w['status']} for w in store.list_workspaces()]
                else:
                    w=store.get_workspace(args.workspace)
                    if not w: raise ValueError('找不到工作台项目')
                    render={k:cfg['render'][k] for k in ('horizontal','vertical','encoder','fps','bitrate')}
                    snapshot=export_snapshot(w,cfg,['horizontal','vertical'] if args.orientation=='both' else [args.orientation],render,cfg['output_dir'])
            if args.command=='workflow-export':
                from core.jobs import submit_job
                result=(submit_job('workflow_export',{'workspace_id':args.workspace,'snapshot':snapshot},cfg) if args.background else
                        run_workflow_export(args.workspace,snapshot,cfg))
        elif args.command == 'compose-create':
            from core.compositor import create_composite
            from core.jobs import submit_job
            sources = [args.commentator, *args.append_commentator]
            payload = {'match': args.match, 'commentator': sources, 'match_track': args.match_track, 'commentator_track': args.commentator_track}
            result = submit_job('compose_prepare', payload, cfg) if args.background else create_composite(
                args.match, sources if len(sources) > 1 else sources[0], cfg, args.match_track, args.commentator_track)
        elif args.command == 'compose-visual-sync':
            from core.compositor import synchronize_visual
            from core.jobs import submit_job
            search = json.loads(Path(args.search).read_text(encoding='utf-8-sig'))
            payload = {'composite_id': args.composite, 'search': search, 'analyze': not args.frames_only}
            result = submit_job('compose_visual', payload, cfg) if args.background else synchronize_visual(args.composite, cfg, search, not args.frames_only)
        elif args.command == 'composites':
            from storage.composites import CompositeStore
            with CompositeStore(cfg['database']) as db:
                result = db.get_composite(args.composite) if args.composite else db.list_composites()
        elif args.command in ('compose-sync', 'compose'):
            from core.compositor import synchronize_composite, render_composite, _load
            from core.composite_options import normalize_composite_options
            from core.jobs import submit_job
            if args.command == 'compose-sync':
                payload = {'composite_id': args.composite, 'semantic': args.semantic, 'allow_cpu': args.allow_cpu}
                result = submit_job('compose_sync', payload, cfg) if args.background else synchronize_composite(args.composite, cfg, args.semantic, args.allow_cpu)
            else:
                document, _ = _load(args.composite, cfg)
                options = normalize_composite_options(json.loads(Path(args.options).read_text(encoding='utf-8-sig')) if args.options else document['options'])
                segments = json.loads(Path(args.segments).read_text(encoding='utf-8-sig')) if args.segments else None
                payload = {'composite_id': args.composite, 'options': options, 'segments': segments,
                           'preview': args.preview, 'preview_start': args.preview_start}
                result = submit_job('compose_render', payload, cfg) if args.background else render_composite(args.composite, cfg, options, segments, args.preview, args.preview_start)
        elif args.command == "process":
            if args.background:
                from core.jobs import submit_job
                payload = {"path": str(Path(args.path).resolve()), "audio_track": args.audio_track,
                    "skip_render": args.skip_render, "mock": args.mock,
                    "transcript_path": str(Path(args.transcript).resolve()) if args.transcript else None,
                    "allow_cpu": args.allow_cpu, "selection_mode": cfg["selection"]["mode"],
                    "transcript_only": args.transcript_only}
                if editorial is not None:
                    payload["editing_options"] = editorial
                result = submit_job("process", payload, cfg)
            else:
                result = process_video(args.path, cfg, audio_track=args.audio_track, skip_render=args.skip_render,
                                       mock=args.mock, transcript_path=args.transcript, allow_cpu=args.allow_cpu,
                                       editing_options=editorial, transcript_only=args.transcript_only)
        elif args.command == "batch":
            if args.background:
                from core.jobs import submit_job
                result = submit_job("batch", {"directory": str(Path(args.path).resolve()), "recursive": args.recursive,
                    "audio_track": args.audio_track, "skip_render": args.skip_render,
                    "allow_cpu": args.allow_cpu, "selection_mode": cfg["selection"]["mode"],
                    **({"editing_options": editorial} if editorial is not None else {}), "transcript_only": args.transcript_only}, cfg)
            else:
                result = batch_process(args.path, cfg, recursive=args.recursive, audio_track=args.audio_track,
                                       skip_render=args.skip_render, allow_cpu=args.allow_cpu,
                                       editing_options=editorial, transcript_only=args.transcript_only)
        elif args.command in ("plan", "plans", "plan-review", "plan-preview", "plan-export"):
            from core.editing import create_editing_plan, coverage_report, preview_plan_draft, export_editing_plan
            from storage.plans import PlanStore
            from core.jobs import submit_job
            with PlanStore(cfg["database"]) as db:
                if args.command == "plan":
                    result = (submit_job("plan", {"project_id": args.project, "options": editorial,
                               "mock": args.mock, "skip_render": args.skip_render}, cfg) if args.background else
                              create_editing_plan(args.project, cfg, editorial, args.mock, args.skip_render))
                elif args.command == "plans":
                    if args.plan:
                        result = db.get_plan(args.plan)
                        if not result:
                            raise ValueError("找不到方案")
                        result["coverage"] = coverage_report(result)
                    elif args.project:
                        result = db.list_plans(args.project)
                    else:
                        raise ValueError("需要 --project 或 --plan")
                elif args.command == "plan-review":
                    plan = db.get_plan(args.plan)
                    if not plan:
                        raise ValueError("找不到方案")
                    changes = {}
                    if args.draft:
                        for field in ("title", "status"):
                            if getattr(args, field) is not None:
                                changes[field] = getattr(args, field)
                        if args.segments:
                            changes["segments"] = json.loads(Path(args.segments).read_text(encoding="utf-8-sig"))
                            changes["subtitle_text"] = None
                        if args.srt:
                            changes["subtitle_text"] = Path(args.srt).read_text(encoding="utf-8-sig")
                        if args.reset_subtitles:
                            changes["subtitle_text"] = None
                    elif any((args.title, args.status, args.segments, args.srt, args.reset_subtitles)):
                        raise ValueError("修改草稿需要 --draft")
                    events = json.loads(Path(args.events).read_text(encoding="utf-8-sig")) if args.events else None
                    result = db.edit_plan(args.plan, plan["revision"], {args.draft: changes} if args.draft else None, events)
                elif args.command == "plan-preview":
                    result = (submit_job("plan_preview", {"plan_id": args.plan, "draft_id": args.draft}, cfg) if args.background else
                              preview_plan_draft(args.plan, args.draft, cfg))
                else:
                    plan = db.get_plan(args.plan)
                    if not plan:
                        raise ValueError("找不到方案")
                    from core.export_options import project_export_options
                    explicit = json.loads(Path(args.export_options).read_text(encoding="utf-8-sig")) if args.export_options else None
                    options = project_export_options(db, plan["project_id"], cfg, explicit)
                    orientations = ["horizontal", "vertical"] if args.orientation == "both" else [args.orientation]
                    result = (submit_job("plan_export", {"plan_id": args.plan, "orientations": orientations, "export_options": options}, cfg)
                              if args.background else export_editing_plan(args.plan, cfg, orientations, options))
        elif args.command == "probe":
            from core.video_input import probe_video
            result = probe_video(args.path, cfg, args.audio_track)
        elif args.command == "export":
            orientation = ("horizontal", "vertical") if args.orientation == "both" else (args.orientation,)
            from core.export_options import normalize_export_options, project_export_options
            from core.subtitle_styles import SUBTITLE_PRESETS
            with Database(cfg["database"]) as db:
                options = project_export_options(db, args.project, cfg)
            if args.subtitle_preset:
                options["subtitle"].update({k: v for k, v in SUBTITLE_PRESETS[args.subtitle_preset].items() if k != "label"})
                options["subtitle"]["preset"] = args.subtitle_preset
            if args.subtitle_color:
                options["subtitle"]["primary_color"] = args.subtitle_color
            if args.subtitle_font:
                options["subtitle"]["font"] = args.subtitle_font
            if args.burn_subtitles is not None:
                options["burn_subtitles"] = args.burn_subtitles
            if args.intro:
                options["intro"] = {"enabled": True, "path": args.intro}
            if args.no_intro:
                options["intro"]["enabled"] = False
            options = normalize_export_options(options)
            if args.background:
                from core.jobs import submit_job
                result = submit_job("export", {"project_id": args.project, "orientations": list(orientation),
                                                "export_options": options}, cfg)
            else:
                result = export_project(args.project, cfg, orientations=orientation, export_options=options)
        elif args.command == "jobs":
            from core.jobs import list_jobs, get_job, retry_job, tail_job_log
            if args.retry:
                result = retry_job(args.retry, cfg)
            elif args.job:
                result = get_job(args.job, cfg)
                if not result:
                    raise ValueError("找不到该任务")
                result = {**result, "log_tail": tail_job_log(result, cfg)}
            else:
                result = list_jobs(cfg)
        elif args.command == "preview":
            result = {"preview": render_preview(args.project, args.clip, cfg)}
        elif args.command == "doctor":
            result = doctor(cfg)
        elif args.command == "demo":
            from core.demo import create_demo
            result = create_demo(cfg, args.approve, args.export, args.full_hd)
        else:
            with Database(cfg["database"]) as db:
                if args.command == "projects":
                    result = db.list_projects()
                elif args.command == "clips":
                    result = db.list_clips(args.project)
                elif args.command == "stats":
                    result = db.stats()
                else:
                    srt = Path(args.srt).read_text(encoding="utf-8-sig") if args.srt else None
                    result = db.review_clip(args.clip, args.status, title=args.title, start=args.start, end=args.end,
                                            subtitle_text=srt, reset_subtitle=args.reset_subtitles)
        _print(result)
        if isinstance(result, dict) and (result.get("errors") or result.get("export", {}).get("errors") or result.get("ok") is False):
            return 1
        if isinstance(result, list) and any(x.get("status") in ("failed", "preview_partial") for x in result):
            return 1
        return 0
    except KeyboardInterrupt:
        print("已中断；完整音频、已完成转录块与选片窗口均可在重跑时复用。", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
