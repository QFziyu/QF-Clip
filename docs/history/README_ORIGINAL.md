> 历史安装记录。路径、端口、版本和通过数量仅描述当时环境；新安装以根目录 README 为准。

## 2026-11-08 · v1.9.0 使用入口

固定工作台：http://127.0.0.1:8501/ 。程序目录 `D:\Codex\FootballClipFactory`。使用 `launch.cmd --profile gpu`；CPU 独立环境使用 `launch.cmd --profile cpu`（8502）。日期目录中的启动按钮已转到主目录。

- 字幕：本地 CPU 识别 → 已配置的 API 上下文校对与断句 → 自动完整预览。侧栏分别显示识别器和复核开关；每条显示实际完成状态。
- 审核：AI 推荐分/理由、人工星级、上一条/下一条已恢复。旧项目可用“修复本方案字幕、评分并生成预览”，保留手工字幕及审批。
- 第五步默认显示全部项目的已通过成品；可按项目、日期、导出状态筛选。已有成品保留，问题条目取消勾选后再导出其他项。单项目发布和自动来源批次确认在“当前项目导出与发布”中。
- AI 操作使用 [统一控制指南](../../docs/AI_OPERATIONS.md)，先查询 capabilities / summary；不再以手写数据库脚本作为日常操作方法。
- [本次验收报告](../../docs/ACCEPTANCE_1.9.0.md)。

# Football Clip Factory 1.8.8

字幕修改新增“查找与替换”：在第三步点“修改字幕”进入第四步，点“查找与替换”（快捷键 **Ctrl+Shift+F**），输入错字和正确人名，检查匹配数后点“全部替换”。只修改当前视频的字幕正文，保留时间轴；支持撤销。然后“保存此条人工字幕”，回到预览审核重新生成完整预览。

自动字幕在分句前过滤 `<unk>`、`<|zh|>` 等模型标记，保留原始转录缓存。已有人工或导入字幕可点“清理识别标记”；默认保留正常英文、比分和数字，勾选“仅保留汉字与标点”后再清理则会删除英文和数字。批量操作需保存后生效，旧视频保留。范围限于当前视频，不会更改其他成品，也不会自动记为永久人名词典。

本版统一启动入口，CPU／GPU 共用当前源码。剪辑形式统一为“切片”“全场集锦”“连续长段”，所有形式共用一行必剪事件输入。设置支持配置文件 API 与官方 Codex 登录两种来源。详见 [统一启动与模型双模式](../../docs/UNIFIED_START.md)。

本地足球录像 AI 切片与人工审核工具。Windows 11、Python 3.11+、FFmpeg / FFprobe、faster-whisper、OpenAI 兼容 API、Streamlit。

## 启动

- 双击 `D:\Codex\FootballClipFactory\launch.cmd`，菜单中选择 GPU／CPU、本地制作／制作与发布。
- 命令行可用 `launch.cmd --profile gpu` 或 `launch.cmd --profile cpu`，需要发布服务时加 `--publishing`。GPU 默认 8501、CPU 默认 8502，工作台端口占用时自动选择空闲端口。
- GPU、CPU 入口保留在同一个最新版源码目录；CPU 的缓存与数据库独立，界面中不会自动出现 GPU 环境的项目。
- 依赖已安装时也可执行 `.venv\Scripts\python.exe -m streamlit run ui/app.py`。

首次安装：执行 `powershell -ExecutionPolicy Bypass -File scripts/setup.ps1`，然后复制 `.env.example` 为 `.env`。在 `.env` 配置 API Key、API 的 `/v1` 地址及服务商实际提供的模型 ID。模型名称不能根据 ChatGPT 或 Codex 的产品显示名猜测。

```dotenv
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=
WHISPER_MODEL=medium
DEVICE=cuda
COMPUTE_TYPE=int8_float16
OUTPUT_DIR=./output
```

FFmpeg 不在 PATH 时配置 `FFMPEG_PATH`、`FFPROBE_PATH`。GPU 依赖见 `requirements-gpu.txt`；运行 `python main.py doctor` 可检查 DLL 和设备可见性，实际推理仍以任务日志为准。首次 Whisper 模型下载需要网络和磁盘空间。不要将密钥写入源码或发送到日志。

## 抖音发布试点（1.7.0）

双击 `launch.cmd` 后选择“制作与发布”（或终端执行 `launch.cmd --profile gpu --publishing`），在“设置”连接独立 Edge 发布账号。第五步选择“仅导出本地 / 导出并发布到抖音”；先用“准备发布”检查样片，校准后可启用自动提交。每批只发布选定的一种画幅，人工标题、字幕、文案和封面内容都与审核快照绑定。

新增独立 `D:\Codex\FootballPublisher`，发布失败可恢复，不重做剪辑。提交结果不明会暂停核验，防止重复投稿；待检查的准备表单会暂停后续上传。真实抖音页面及自定义封面控件仍待你的账号校准，本地模拟验收通过不代表已经在真实平台发布成功。详见 [使用说明](../../docs/PUBLISHING_GUIDE.md) 和 [验收报告](../../docs/PUBLISHING_ACCEPTANCE.md)。

新安装需补装 `.venv\Scripts\python.exe -m pip install -r requirements-publish.txt`，并安装 Microsoft Edge。程序不提取日常 Edge 的 Cookie，源码包不含发布会话和本机令牌。

## 轻舟 → 自动生产（新版）

双击 `launch.cmd` 选择设备。本机安装了轻舟与 FootballWorkflow 时，入口启动轻舟 17653、独立调度器 17654、工厂控制服务 17655，以及五步工作台；选择“制作与发布”时额外启动发布服务 17656。仅监听本机，配对令牌保存在各自运行目录。自动服务固定端口有未知进程时停止并报错，不会强行接管。

首次使用咪咕：打开 Edge 的 `edge://extensions/`，重新加载「轻舟 · 视频识别」，确认版本 **1.2.0**，扩展目录为 `D:\Codex\软件下载\轻舟视频下载\extension`。在扩展弹窗点击 **“连接自动工作流”**，按浏览器提示授权站点。使用自己的正常登录会话，保持 Edge 运行。海星公开回放可直接解析，不依赖扩展。新安装的源码包需先运行轻舟目录的 `python setup_pairing.py` 生成本机配置，再加载扩展；本机现有配对配置已保留。

1. 第一步选择 **“视频来源”**，粘贴咪咕回放网页或含 `recordId` 的海星回放链接，选制作模板，点“提交来源并自动生成候选”。默认比赛模板要求至少 90 分钟，防止误用集锦/试看；短样片可选对话模板，或保存一个不限时长的自定义模板。
2. 后台完成下载、音视频校验、引用导入、转录、选片和低码率预览。来源任务显示进度；“需要协助”时打开原网页，登录/选择全场并播放，从轻舟扩展“开始识别当前页”再“发送到本机，检查时长”；会接回原任务。多个候选时明确选定资源，避免猜错场次。
3. “进入本批工作台”后按第三、四步修改标题、范围、人工字幕及包装。第五步检查输出画幅与问题，点 **“完成本批审核并自动导出”**。只导出通过项，待决定与拒绝项均不输出。后续修改生成新修订，需再次确认，旧成品保留为历史。

第五步“保存为自动制作模板”可复用内容标准、短片/长片/集锦/完整输出、数量时长、字幕样式、片头与导出参数。模板提交后冻结修订；当前场次的时间校准、必剪事件及人工字幕文字不会复制到下一场。首期来源自动流程仅支持单路；原有手动单路/顺序拼接/双路功能保持可用。

关闭网页不影响后台；Windows 重启后重新运行同一个入口，从任务记录恢复，已完成转录与选片缓存继续使用。下载签名过期时重新识别来源；仅资源身份、画质、分段一致时复用缓存。停止流程不会删除成果；**已确认提交的导出仍会执行**，停止只阻止尚未推进的阶段。服务断线暂时退避重试，持续失败后点“恢复此任务”。

GPU 与 CPU 自动流程共用固定服务端口，**同一时刻只能启用一个配置**。切换前运行 `stop_automation.cmd`，再启动另一入口；它只停止归属核验通过的三个自动服务，保留工作台和数据。CPU 的项目、缓存、调度数据库独立，不会自动出现 GPU 的待审核批次。人工手动工作台仍可分别打开。

运行数据：轻舟 `data/downloads.sqlite3`、调度器 `D:\Codex\FootballWorkflow\runtime\gpu` 或 `cpu`、工厂当前环境的 `output/factory.sqlite3`，连接与日志在 `output/.automation/`。不要公开 `connection.json`、轻舟 `config.json`、扩展 `config.js`。不持久化媒体请求的 Cookie、Authorization 或临时签名地址。

已验证范围和限制见 [自动流程验收报告](../../docs/AUTOMATION_ACCEPTANCE.md)，接口见 [本机 API 合约](../../docs/AUTOMATION_API.md)。直播监测/自动录制仅预留适配边界，尚未启用；无自动投稿功能。

## 五步工作台

**导入素材 → 选择剪辑方式 → 预览审核 → 字幕与包装 → 统一导出**

顶部显示项目、步骤和状态。点击步骤按钮可返回；每次保存的进度、审批、字幕存入 SQLite，关闭浏览器后继续。侧栏只保留工作台、项目库、任务记录、设置。

1. **导入素材**：点“打开文件…”多选录像，或点“打开文件夹…”。弹出本机 Windows 文件窗口，直接读取文件，不上传或复制整场录像。取消选择保留原列表。显示缩略图、时长和可访问状态，路径在详情中。手填路径仅为高级备用。
2. **选择剪辑方式**：素材组织选单路、多段顺序拼接、比赛＋主播双路合成。单路选多个文件表示分别处理；顺序拼接才会首尾连接，可编辑顺序数字。成品形式独立选择短片、连续长片、多段集锦、完整输出。内容标准、数量、时长、比赛分钟校准和必剪事件在展开区域设置。一个项目可保留多套方案，切换后继续审核。
3. **预览审核**：左侧选候选，中间播放，右侧修改标题、原片范围并保存审批。集锦可编辑有序分段。预览为完整时长的低码率视频，保留全部组成片段。多素材集锦默认整批总计生成指定条数；可在第二步切换为分别制作。完整输出也需审核通过。本页没有正式导出操作。
4. **字幕与包装**：逐条修改正文相对 SRT，也可“打开 SRT…”导入 UTF-8 / GB18030 文件。统一选择艺术字样式、颜色、字体、字号、描边和片头。“保存此条人工字幕”保存文字；“保存包装并前往导出”也会保存当前已修改的字幕。改变范围会保留人工字幕并标记需核对，未核对的字幕不能正式导出。可整批关闭字幕；完整输出尚无转录时可在此识别。双路默认用主播声音，也可切换比赛音轨。
5. **统一导出**：全软件唯一正式导出页面。汇总当前方案已通过成品，设置横竖屏、分辨率、编码器及输出目录。问题会列在开始按钮前。点一次“开始导出”，任务在后台运行；完成后集中展示当前/历史成品及“打开输出文件夹”。文件名采用已保存的人工作品标题，附 SRT、ASS、发布文案与原片时间映射 JSON。

任务进度、日志、失败重试在当前步骤直接显示。处理完点击“载入最新进度”；无需来回找任务页面。导出期间修改上游后，已产生的文件保留为历史；未开始的过期导出会拒绝执行，需重新检查并提交。

### 自动字幕 AI 校对

新版默认在自动字幕生成后，使用已配置的 OpenAI 兼容模型分批校对明显错字、足球专名和不通顺表达，保留原始转录和字幕时间轴。人工保存或导入的字幕会跳过自动校对。预览、横屏和竖屏导出共用缓存，不重复转录；选片和校对共用项目 API 预算。

已有项目：进入第四步“字幕与包装”，保持“自动字幕生成后使用 AI 校对”开启，展开“AI 校对记录与重试”，点击“AI 校对本批自动字幕（失败项可重试）”。后台完成后点击“载入最新进度”，即可看到校对后的文字和原文对照；仍可手动修改并保存。第三步生成新预览、第五步正式导出也会复用结果。

校对失败或预算不足时保留原字幕，显式重试只补失败批次。可取消复选框并保存包装来关闭此项目的自动校对；全局开关在 `config/settings.yaml` 的 `subtitle_review.enabled`。大幅改写及数字变化会被拦截；模型仍可能误判专名，发布前请检查。详见 [字幕校对升级与验收报告](../../docs/SUBTITLE_REVIEW_UPGRADE.md)。

## 双路对轴与继续 AI 剪辑

先在第一步导入比赛和主播全部素材。第二步选择双路，指定角色和主播顺序，点“准备双路素材”。准备完成点击“载入最新进度”，仍在第二步配置同步。

偏移定义：**主播录像时间 = 比赛录像时间 + 偏移**。同一进球在比赛 120 秒、主播 360 秒，偏移填 +240 秒。自动共同声音匹配给出建议；不同解说可选语音事件辅助。低置信度请使用同一事件的两个位置手动校准，也可使用已有的开球/进球画面对照工具。画面 AI 需模型支持图片输入，结果仍须预览确认。先生成 20 秒同步预览，再勾选已核对同步，保存设置。

画中画默认完整缩放、宽度 25%、左下角。可选人脸跟踪（`requirements-face.txt`）；比赛音轨可静音、混音或可选 AI 去人声（`requirements-vocals.txt`，首次需下载模型）。

之后选择任意成品形式。双路继续 AI 选片时，直接把所选声音的转录映射到比赛时间轴，仅在两路公共范围内选片；不会先渲染整场合成。正式渲染只合成选定区间，再统一字幕、片头、横竖屏。固定偏移不能修复缺录、删节或变速漂移，这些素材应分段校准。

## 命令行（保持兼容）

```powershell
python main.py probe "D:/videos/live.mp4"
python main.py process "D:/videos/live.mp4" --allow-cpu
python main.py process "D:/videos/live.mp4" --skip-render
python main.py batch "D:/videos" --recursive --allow-cpu --background
python main.py export --project PROJECT_ID --approved-only --orientation both
python main.py jobs --job JOB_ID
python main.py jobs --retry JOB_ID
python main.py workspaces
python main.py workflow-export --workspace WORKSPACE_ID --orientation both --background
```

旧 `process / batch / plan / plan-review / plan-export / compose-*` 命令保持可用。工作台的审批与方案修订记录在新增关联层，使用 `workflow-export` 导出这些工作台记录；旧 `export` 继续导出旧 clips 表审批。`workspaces` 返回工作台 ID。全局 `--config` 放在子命令前。`python main.py --help` 列出完整接口。

## 保存位置与恢复

- 主数据库：`output/factory.sqlite3`，包括原有记录与新增 workspaces / workflow_media / workflow_audit。
- 转录、选片窗口、素材拼接块保存在 `cache/`；ASR 失败重跑可复用已完成块。不要为了重试删除转录缓存。
- 新成品：输出目录下 `workflow_exports/项目/成品/版本/`；预览在缓存内。修改方案后旧视频仍在原位置。
- 升级备份：`output/workbench_upgrade/before.sqlite3`。这是完整在线 SQLite 备份，恢复前关闭应用并另备份现状。
- 原片移动后可看已有预览和成品，但不能重新剪辑；恢复原路径或在第一步导入新素材。
- LLM 超时：当前完成窗口保留，检查 `.env` 服务商、模型及 `OPENAI_TIMEOUT / OPENAI_MAX_TIMEOUT`，从当前步骤重试。不会因此重新转录整场。

## 测试和验收

```powershell
.venv/Scripts/python.exe scripts/run_tests.py
.venv/Scripts/python.exe scripts/verify_workbench.py
```

后者在 `output/workbench_upgrade/media/` 使用程序生成的视频、提供的转录与模拟选片，执行六种工作流，真实编码并完整解码横竖屏样片。它不会调用付费 API。验收报告见 [docs/WORKBENCH_ACCEPTANCE.md](../../docs/WORKBENCH_ACCEPTANCE.md)，简明入口见 [docs/LATEST_VERSION.md](../../docs/LATEST_VERSION.md)。

本次重构不声称验证了真实整场比赛的 Whisper/GPU 推理或真实 API 选片效果；已有处理算法和模型配置继续复用。长时间素材性能取决于模型、硬件和服务商。非原创素材需按实际授权范围使用，切片授权不自动包含赛事转播画面的跨平台使用权。

## 比赛事件快捷输入与自动对轴

必剪事件现在可以一行填写：`G23:10 R40 / G61 V90+3:20`（G 进球、R 红牌、V VAR；斜杠区分上下半场）。
开启默认的「自动识别记分牌并对轴」后，生成方案时会通过已配置的视觉 API 读取抽帧里的比赛时间，无需手填录像分秒。
支持累计计时、独立补时计时器和上下半场分别对时；识别不清楚的事件会提示待处理。
格式、API 配置要求和独立 Python 读取命令见 [比赛自动对轴说明](../../docs/match_clock.md)。

# 本地中文语音识别（1.8.6）

本机已部署 Zipformer Small 中文 CPU 服务，可在侧栏 **设置 → 本地语音识别** 切换，或在 `.env` 配置 `ASR_ENGINE=zipformer_http`、`LOCAL_ASR_URL=http://127.0.0.1:8765`。网页设置另存 `.env.asr`，优先于 `.env` 和启动环境；原 Whisper 模型/GPU 设置保留，可随时切回。

统一启动入口仍为 `launch.cmd`；选择本地中文引擎时，启动器会检查已部署的同级 `Hugging Face` 服务，默认端口未运行才尝试启动，不停止未知进程。服务与工厂使用独立 Python 环境，工厂无需安装 sherpa-onnx。直接使用 CLI 或 `streamlit run` 时，请先运行 `D:\Codex\Hugging Face\Start-ASR-Background.cmd`。

识别默认每 60 秒一块、前后重叠 2 秒，逐块缓存，基于实际 CTC 词元起点生成字幕；字尾是有界估计，并非音素级强制对齐。保留原始文字、词元及其时间戳；中文人名/足球专名仍可能识别错误，模型不自带标点恢复。不以整段文字均分方式伪造时间轴，不自动改用付费识别。

**语音识别与大语言模型是两件事**：Whisper 和 Zipformer 都在本机识别；选片、标题及可选文字校对仍使用你配置的 LLM。关闭“字幕生成后再调用大语言模型校对”可省去这一步调用。仅做本地转录无需 LLM：

```powershell
.venv\Scripts\python.exe main.py process "D:/videos/录像.mp4" --transcript-only --skip-render
.venv\Scripts\python.exe main.py doctor
.venv\Scripts\python.exe scripts/verify_local_asr.py
```

切换引擎不覆盖已有成品和人工字幕；已有工作台项目需重新生成方案，才能采用新转录。旧审核结果保留。实际验证和限制详见 [接入验收报告](../../docs/LOCAL_ASR_ACCEPTANCE.md)。

对已保存的单路剪辑方案，若只想比较字幕而不重新选片，可运行 `python scripts/compare_local_asr.py --workspace 项目ID`。程序在独立缓存重新识别整场音轨，沿用原剪辑范围，创建待审核的字幕对照项目及完整 720p 预览；便于观看的 MP4、SRT/ASS 放在 `output/本地ASR字幕对照/`，不改变原项目，不调用 LLM，不自动审批或发布。

