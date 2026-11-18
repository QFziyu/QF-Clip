---
name: football-clip-factory
description: Operate this local Football Clip Factory project to process recordings, resume cached work, review candidates and export approved videos through its Python CLI and five-step Streamlit workbench.
---

# Football Clip Factory 1.9.0

For AI operation or changes to subtitles, scoring, previews or exports, read [docs/AI_OPERATIONS.md](docs/AI_OPERATIONS.md) first. Use `scripts/agent_control.py capabilities` and `summary` to identify the actual environment. Step 5 defaults to all approved projects; the single-project publishing mode remains explicitly separate. Local CPU ASR followed by LLM text review is not independent audio cross-checking. Do not bypass a failed review, hide scores, overwrite human work, or require a second manual action for initial previews.

Use the existing Python program in this directory. The current source includes GPU and CPU launch profiles; preserve user media, API configuration, model caches, approvals and manual subtitles. Read [README.md](README.md) for the five-step workflow and [docs/WORKBENCH_ACCEPTANCE.md](docs/WORKBENCH_ACCEPTANCE.md) for actual validation boundaries.

## Environment and inputs

Windows 11, Python 3.11+, FFmpeg/FFprobe, faster-whisper, OpenAI-compatible API, Streamlit, SQLite. Prefer `.venv/Scripts/python.exe`. Local MP4/MKV/FLV and folders are supported. Quote Chinese or space-containing paths. Audio-track indexes start at zero. Keys belong in `.env` or environment, never source or tool output.

## Operation

```powershell
.venv/Scripts/python.exe main.py doctor
.venv/Scripts/python.exe main.py probe "D:/videos/live.mp4"
.venv/Scripts/python.exe main.py process "D:/videos/live.mp4" --skip-render --allow-cpu --background
.venv/Scripts/python.exe main.py batch "D:/videos" --recursive --allow-cpu --background
.venv/Scripts/python.exe main.py jobs --job JOB_ID
.venv/Scripts/python.exe main.py jobs --retry JOB_ID
.venv/Scripts/python.exe main.py export --project PROJECT_ID --approved-only --orientation both --background
.venv/Scripts/python.exe main.py workspaces
.venv/Scripts/python.exe main.py workflow-export --workspace WORKSPACE_ID --orientation both --background
```

`launch.cmd --profile gpu` / `launch.cmd` opens port 8501. `launch.cmd --profile cpu` opens 8502 with isolated runtime data and CPU/int8/software encoding. `streamlit run ui/app.py` launches the five-step workbench with normal configured data. Keep configuration edits in `.env` or config files; never disclose keys.

Workbench entry: import → method → preview/review → subtitles/packaging → export. Native file/folder/SRT/intro pickers read local files directly; cancellation keeps previous selections. All branches reuse imported assets. Final export occurs only in step 5. New workbench edits use an additive association layer; `workflow-export` reads those revisions, while the original `export` and plan commands still operate their original tables.

Single-file shorts, continuous clips and montage use cached transcription and existing editorial rules. Multiple sources default to a combined batch; explicitly select `batch_scope=separate` to process each independently. Ordered sequence mode keeps the chosen input order. Full output bypasses LLM selection but still needs approval. Full outputs without a transcript can transcribe in step 4, import manual SRT, or disable subtitles.

Dual-source mode first prepares selected sources and commentator order, then calibrates within step 2. Clock invariant: commentator time = match time + offset. Audio/semantic/visual suggestions are advisory; preview and explicitly confirm before export. A fixed offset cannot repair missing intervals or variable drift. Dual AI editing maps the selected audio transcript into the match clock and restricts choices to overlap; render selected ranges only, never an entire combined match just to reimport it. Existing `compose-create`, `compose-sync`, `compose-visual-sync` and `compose` commands remain supported; use `--help` for arguments.

Manual SRT times refer to the assembled body, excluding intro. Range changes preserve manual text and flag it for review. Do not silently regenerate human subtitles. Exports snapshot approved items, revisions and packaging; single-project stale submissions reject changed items. Global local-export batches may finish their frozen version as history, never as fresh publishing authorization. Historical files remain intact. A supplied title is used for the filename with Windows-safe normalization.

## Recovery and verification

For URL-driven production, run `launch.cmd --profile gpu` (or `launch.cmd --profile cpu`). This starts independent Lightboat, coordinator and factory HTTP services on loopback ports 17653/17654/17655. The coordinator resides in sibling `FootballWorkflow`; Lightboat is in sibling `软件下载/轻舟视频下载`. Interfaces, idempotency and recovery contracts are in [docs/AUTOMATION_API.md](docs/AUTOMATION_API.md). Never access another component's SQLite from the coordinator.

Use step 1 “视频来源” with a supported Migu/Haixing replay URL and frozen template revision. Migu needs Edge Lightboat extension 1.2.0, one-time “连接自动工作流”, normal site permissions and playback session. Assistance means explicitly resolving login/playback/resource choice, not guessing or bypassing access. A popup Send reattaches the selected resource to the waiting flow. Downloaded files must have a verified completion receipt before import; never infer completion from file existence or stable size.

At step 5 “完成本批审核并自动导出”, submit the current approval snapshot. Do not approve on the user's behalf. Pending clips stay excluded. New upstream revisions require another confirmation. Stop via `stop_automation.cmd` before changing automation GPU/CPU profile; it checks process ownership. Stopping flow progression does not cancel already-confirmed export work. Windows restart recovery is on relaunch; no startup service or live monitoring is installed. See [docs/AUTOMATION_ACCEPTANCE.md](docs/AUTOMATION_ACCEPTANCE.md) for real-source versus fixture evidence.

ASR caches are chunked; retry after dependency repair or explicit `--allow-cpu`, without deleting completed chunks. LLM windows and decisions are cached; failed requests have bounded retries and project budgets. Check task logs for actual device/provider errors; configured GPU is not proof of inference success. Missing source files must be restored before new rendering. Back up SQLite online before structural changes; preserve existing record IDs.

```powershell
.venv/Scripts/python.exe scripts/run_tests.py
.venv/Scripts/python.exe scripts/verify_workbench.py
.venv/Scripts/python.exe scripts/verify_automation.py
```

The second command generates synthetic footage, uses supplied transcription and mock selection, and actually renders/decodes six workflows in isolated test data. Distinguish these checks from real broadcast, Whisper/GPU or API validation. This skill alone grants no publication authorization. Respect the supplied material's actual authorization scope.

## Douyin publishing pilot

Use `launch.cmd --profile gpu --publishing` / `launch.cmd --profile cpu --publishing` to start the factory and independent sibling `FootballPublisher`; the source automation launchers also start it. Publisher uses localhost port 17656, its own SQLite, and a dedicated Edge profile under `FootballPublisher/runtime/<profile>`. It never reads the factory database. For a new installation run `.venv/Scripts/python.exe -m pip install -r requirements-publish.txt`; installed Microsoft Edge is required.

Step 5 defaults to local output. A publishing batch requires explicit selection of the bound account, one orientation, prepare/auto mode, and confirmation of the frozen batch. Do not approve items or publish arbitrary historical outputs on the user's behalf. Automatic mode remains unavailable until the operator has checked a prepared sample and enabled it in Settings. Login and platform challenges require the operator's normal interaction.

Publication uses immutable verified deliveries, frozen subtitles, titles, descriptions and cover hashes. If upstream data changes, return to review and confirm a new revision. `prepared` blocks the next browser upload until checked and receipt-verified or cancelled. `submission_unknown` must be resolved using a known work ID; never retry an uncertain submission or infer a match from a similar title. A generic success banner means submitted, not publicly published. Publisher failures retain local media and do not redo transcription or rendering.

See [docs/PUBLISHING_GUIDE.md](docs/PUBLISHING_GUIDE.md) for account connection and recovery, and [docs/PUBLISHING_ACCEPTANCE.md](docs/PUBLISHING_ACCEPTANCE.md) for the real-platform validation boundary. Run `.venv/Scripts/python.exe scripts/verify_publishing_pipeline.py` for isolated real FFmpeg + mock platform HTTP acceptance; it never contacts Douyin. Real login/upload/publication requires the user's selected account and selected test work. Custom-cover controls and live page selectors await account-specific verification; stop for assistance when they do not match.

## Platform titles and Bilibili (1.8.5)

New montage work preserves real topic names and generates separate Douyin/Bilibili titles from selected subtitles. Step 3 supports per-item/batch generation, local index recovery and manual platform text. Preserve manual titles. Step 5 freezes the selected platform title, account and metadata. Bilibili uses its own browser profile and the same explicit batch authorization and sample calibration workflow as Douyin; never infer publication from a generic success message. Documentation: [PLATFORM_TITLES_AND_BILIBILI.md](docs/PLATFORM_TITLES_AND_BILIBILI.md).
## 本地中文 ASR（v1.8.6）

- 设置页选择 Zipformer 中文 CPU 引擎，持久化在 `.env.asr`；与 LLM 配置分离。也可用 `.env` 的 `ASR_ENGINE=zipformer_http` 和 `LOCAL_ASR_URL=http://127.0.0.1:8765`，但网页设置优先。
- 双击 `launch.cmd` 自动检查默认部署；直接调用 CLI 时先运行 `D:\Codex\Hugging Face\Start-ASR-Background.cmd`，再运行 `python main.py doctor`。
- `python main.py process "D:/videos/录像.mp4" --transcript-only --skip-render` 只提取音频和本地识别，不调用选片 LLM。
- `python scripts/verify_local_asr.py` 用部署中的官方 WAV 做实际识别、SRT/ASS、FFmpeg 样片和缓存验证；可用 `--audio "路径.wav" --start 180 --seconds 125` 仅检查现有音频节选，验证数据放到独立 cache/local_asr_validation，不修改用户项目。
- `python scripts/compare_local_asr.py --workspace 项目ID` 使用真实单路项目缓存音轨重新本地识别整场，沿用已保存的剪辑范围，创建独立待审核工作台与完整 720p 字幕对照成片。原项目保留，不调 LLM；输出 `output/本地ASR字幕对照/`。不要把这一对照流程称为重新执行 AI 选片。
- 连接失败：启动上述服务；旧纯文字接口提示版本不匹配：停止并重启该部署自己的服务。重试原任务保留已完成分块，不使用云端 ASR 回退。
- 需要切回 Whisper：设置页选择 Whisper；保留原 DEVICE/COMPUTE_TYPE/WHISPER_MODEL。Zipformer 不占显存，不影响 NVENC 选择。
- 选片、标题、可选字幕校对仍是独立 LLM 阶段；不得把无 API 语音识别表述为整条流程无需大模型。切换不会自动提升旧项目字幕，应重新生成方案，并保留人工字幕和历史成品。

