# QF-Clip · 足球直播切片工作台

本地足球录像的 AI 辅助剪辑与人工审核工具。导入录像后，完成语音识别、候选选片、完整预览、字幕修改与横竖屏导出。当前源码版本为 **1.9.0**。

本仓库面向 **Windows 11 / Python 3.11+**。界面使用 Streamlit，任务和审核记录存入 SQLite，媒体处理使用 FFmpeg。本地制作可独立运行；下载调度、浏览器发布和 Zipformer 服务是可选的独立项目，未包含在本仓库中。

## Windows 客户端下载

前往 [Releases](https://github.com/QFziyu/QF-Clip/releases/latest) 下载完整 Windows x64 客户端。客户使用 Release 中的客户端 ZIP，不要使用 GitHub 自动生成的 Source code 压缩包。

完整解压后双击 `launch.cmd` 即可打开本地工作台；API 在左侧“配置 API”图形化填写，不需要手改文件。

## 功能

- 五步工作台：导入素材 → 选择剪辑方式 → 预览审核 → 字幕与包装 → 统一导出。
- 素材组织：单路、多段顺序拼接、比赛画面与主播画中画双路合成。
- 成品形式：短切片、连续长段、多段集锦、完整输出。
- 本地 Whisper 识别、分块缓存，以及可选的 LLM 文字校对、选片与标题生成。
- AI 推荐分与理由、人工星级、人工标题和字幕、审批及版本历史。
- SRT / ASS 字幕、横竖屏 MP4、文案与原片时间映射；后台任务支持恢复和重试。
- 共同声音对轴、可选语义与画面事件辅助；同步结果需人工预览确认。

自动候选需要人工审核。字幕语义校对只处理识别出的文字；它不是第二次音频识别。真实 API、完整赛事素材、GPU 推理和平台投稿效果需在实际部署中分别验证。

## Windows 客户端启动

客户使用带运行环境的 **QF-Clip-Windows-x64-Client.zip**：完整解压后双击 `launch.cmd`，默认以 CPU 模式在系统浏览器打开本地工作台。不需要预装 Python，不需要先配置 API。浏览器中的页面是本地应用，不是独立离线 HTML 文件。

源码 ZIP 不包含 Python 和依赖：首次双击时会调用安装脚本，自动寻找 Python 3.11–3.13 x64 并安装依赖，需要联网。若电脑没有 Python，请使用上面的客户端包。API Key 只影响需要大模型的功能，不影响打开界面。FFmpeg/FFprobe 和语音识别模型在实际处理媒体时另外准备；客户端不捆绑个人配置或 API 密钥。

## 快速开始

先安装 64 位 Python 3.11–3.13 和包含 `libx264`、`aac`、`subtitles/libass` 的 FFmpeg / FFprobe，并确认两者可通过 PATH 调用。项目不附带解释器、模型权重或 FFmpeg。

在本仓库根目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

脚本创建 `.venv`、安装 `requirements.txt`，并在 `.env` 不存在时复制 `.env.example`。已有环境、配置和数据不会被覆盖。若 `python` 指向错误解释器，可加 `-Python "C:\path\to\python.exe"`。

## 在界面配置 API（推荐）

启动后点击左侧 **配置 API**，填写服务商提供的 **API 地址、API Key、模型名称**，点击 **保存并使用此来源**。保存后可点击 **测试已保存配置**；测试会产生一次模型调用，可能计费。

密钥使用密码框，保存后输入框清空；下次留空会保留原密钥。程序自动保存到本机，不需要编辑配置文件。API 未配置不会阻止打开工作台。不要向别人发送自动生成的密钥配置文件。

下面仅为命令行/高级部署的环境变量示例，普通客户无需修改：

```dotenv
# 语音识别在本机运行；首次使用 Whisper 会下载所选模型。
ASR_ENGINE=faster_whisper
WHISPER_MODEL=medium
DEVICE=cpu
COMPUTE_TYPE=int8

# 使用 AI 选片、标题或校对时填写。模型 ID 由 API 服务商提供。
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=
SUBTITLE_REVIEW_ENABLED=false

# 若工具不在 PATH，填实际可执行文件的完整路径。
FFMPEG_PATH=ffmpeg
FFPROBE_PATH=ffprobe
```

运行诊断，再以 CPU 配置启动：

```powershell
.\.venv\Scripts\python.exe main.py doctor
.\launch.cmd --profile cpu
```

Windows 下建议将成品输出到较短路径（例如 `D:/fcf-output`），避免深层文件名超过路径限制。

CPU 默认打开 `http://127.0.0.1:8502/`，GPU 默认 8501；端口冲突时以终端显示为准。CPU 配置使用独立的 `runtime-cpu/` 数据。启动器的 `--profile` 会明确选择识别设备及编码方式；直接运行 CLI 则使用配置文件中的设备。

不需要 LLM 的合成演示（仅生成程序测试图与模拟转录）：

```powershell
.\.venv\Scripts\python.exe main.py demo
```

使用 NVIDIA GPU 时需兼容的显卡和驱动，另装可选运行库：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1 -Gpu
.\launch.cmd --profile gpu
```

其他可选依赖：`requirements-face.txt`（人脸检测）、`requirements-vocals.txt`（去人声）、`requirements-publish.txt`（发布浏览器客户端，还需独立发布服务）。原环境固定版本快照在 `requirements.lock.txt`；可用安装脚本的 `-Locked` 选项尝试复现，新机器的安装仍需实际验证。

## 基本操作

1. 在第一步选择本地录像。程序引用原文件，重新渲染时需保持原路径可访问。
2. 选择素材组织、成品形式、数量和时长。双路素材先对轴并确认同步。
3. 在完整预览中审核候选，修改标题和时间范围，保存人工审批。
4. 修改或导入相对成品时间轴的 SRT，设置字幕和片头；范围变化后核对保留的人工字幕。
5. 在统一导出页检查问题，选择已通过条目和画幅，再提交本地导出。

人工修改和审批存入数据库。失败任务保留原因与已有缓存；在原任务上恢复或重试。发布功能需单独启用及确认。

常用 CLI：

```powershell
.\.venv\Scripts\python.exe main.py --help
.\.venv\Scripts\python.exe main.py probe "D:/videos/match.mp4"
.\.venv\Scripts\python.exe main.py process "D:/videos/match.mp4" --transcript-only --skip-render
.\.venv\Scripts\python.exe main.py process "D:/videos/match.mp4" --allow-cpu --background
.\.venv\Scripts\python.exe main.py jobs --job JOB_ID
.\.venv\Scripts\python.exe main.py workspaces
.\.venv\Scripts\python.exe main.py workflow-export --workspace WORKSPACE_ID --orientation both --background
```

`workflow-export` 使用工作台审核记录；旧 `export` 使用旧 clips 表。完整接口以各子命令 `--help` 为准。全局 `--config` 参数放在子命令前。

## 项目结构

```text
FootballClipFactory/
├── README.md                      # 项目介绍、安装与启动
├── AGENTS.md / SKILL.md            # AI 维护与操作约束
├── CONTRIBUTING.md / SECURITY.md   # 协作和敏感数据处理说明
├── .env.example / .gitignore       # 无密钥的配置模板与提交排除规则
├── .editorconfig / .gitattributes  # 编码、缩进及换行约定
├── .github/                       # Windows CI、Issue 和 PR 模板
├── launch.cmd / stop_automation.cmd
├── main.py                        # 命令行入口
├── requirements*.txt              # 基础、测试、固定版本及可选依赖
├── config/                        # 默认配置、选片规则、足球术语与示例
├── prompts/                       # 选片提示词
├── core/                          # 识别、选片、字幕、合成、任务与导出引擎
├── storage/                       # SQLite 数据访问代码（必须提交）
├── ui/                            # Streamlit 工作台与设置页面
├── scripts/                       # 安装、启动、控制、验收和源码打包工具
├── tests/                         # unittest、模拟接口和合成媒体用例
├── docs/                          # 配置、架构、集成、维护与历史记录
├── input/                         # 可选本地素材目录，仅提交 .gitkeep
├── cache/                         # 运行后生成缓存，仅提交 .gitkeep
└── output/                        # 运行后生成数据库/成品，仅提交 .gitkeep
```

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/run_tests.py
.\.venv\Scripts\python.exe scripts/verify_workbench.py --destination "$env:TEMP/fcf-media-check"
```

第一条测试命令生成 `output/test_results.json`；第二条使用合成媒体检查工作台导出和完整解码，不需付费 API 或模型下载。缺少独立 `FootballWorkflow` 时，仅其协调器集成测试被明确跳过。本仓库的 GitHub Actions 配置测试 Windows / Python 3.11、3.13；远程工作流尚需上传后运行确认。

## 文档

- [文档索引](docs/README.md) · [配置说明](docs/CONFIGURATION.md) · [架构与数据流](docs/ARCHITECTURE.md)
- [可选外部服务](docs/INTEGRATIONS.md) · [开发与测试](docs/DEVELOPMENT.md) · [AI 操作约束](docs/AI_OPERATIONS.md)
- [GitHub 提交步骤](docs/GITHUB_UPLOAD.md) · [本次整理与验证记录](docs/REPOSITORY_PREPARATION.md)

仓库包含程序源码与示例配置。原安装的 `.env*` 私有文件、登录会话、配对信息、媒体、数据库、模型和历史运行产物不随源码分发。历史报告描述原安装环境，不能视为新安装验收结果。

原目录未提供开源许可证，本次整理未代为授予许可。正式对外开源前，由项目权利人确定并添加 `LICENSE`；参见 [许可说明](docs/LICENSING.md)。
