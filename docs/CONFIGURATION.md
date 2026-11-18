# 配置说明

所有应用内的相对路径以仓库根目录为基准。`config/settings.yaml` 是可提交的默认配置，`.env.example` 是不含密钥的个人配置模板。界面保存的个人配置不会写回示例文件。

## 读取优先级

`core/config.py` 内置默认值 → `config/settings.yaml`（或 CLI `--config` 指定文件）→ `.env` → 进程环境变量 → 界面专用设置文件。

界面语音设置存入 `.env.asr`，模型连接设置存入 `.env.llm`；各自只覆盖其负责的键，并优先于旧启动器环境。选片默认规则先合并 `config/clip_rules.yaml`，然后由 YAML 的 `selection` 显式设置覆盖。

启动器的 `--profile cpu` / `gpu` 会设置设备、计算类型及编码器。CPU 还隔离数据库和缓存。直接 CLI 使用普通配置，不自动进入 CPU 独立数据目录。

## 常用配置

| 项目 | 环境变量 / 文件 | 说明 |
| --- | --- | --- |
| 模型接口 | `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL` | API Key 只放个人配置；模型 ID 需服务商支持 |
| 调用等待 | `OPENAI_TIMEOUT`、`OPENAI_MAX_TIMEOUT` | 单次等待与上限，单位秒 |
| 调用预算 | YAML `openai.max_calls`、`budget_usd` | 项目账本限制；价格字段只是本地估算 |
| 语音识别 | `ASR_ENGINE` | `faster_whisper` 或独立服务 `zipformer_http` |
| 本地模型 | `WHISPER_MODEL` | 首次使用可能下载模型 |
| 识别设备 | `DEVICE`、`COMPUTE_TYPE` | CPU 示例为 `cpu` / `int8`；GPU 为 `cuda` / `int8_float16` |
| 中文 ASR 服务 | `LOCAL_ASR_URL` | 默认 `http://127.0.0.1:8765`；仅回环 HTTP |
| 字幕语义校对 | `SUBTITLE_REVIEW_ENABLED` | `true` / `false`，本仓库默认关闭，可在界面启用 |
| 数据位置 | `OUTPUT_DIR`、`CACHE_DIR`、`DATABASE_PATH` | 设置输出目录且未指定数据库时，数据库随输出目录走 |
| 媒体工具 | `FFMPEG_PATH`、`FFPROBE_PATH` | 可执行文件名或完整路径，不追加命令行参数 |
| 编码器 | `FCF_RENDER_ENCODER` / YAML `render.encoder` | `auto`、`libx264`、`h264_nvenc` |
| 字幕样式 | YAML `subtitle`、界面项目包装设置 | 字体需本机已安装；颜色、大小、描边和竖屏参数可调整 |
| 选片规则 | `config/clip_rules.yaml` | 模式、滑动窗口、上下文、时长和去重阈值 |
| 专名 / 提示词 | `config/football_terms.txt`、`prompts/clip_selection.md` | 术语清单与结构化选片提示 |

另有 `FCF_LLM_PROVIDER=api/codex_cli` 及相关 `CODEX_*` 选项。它们是已有适配器的可选能力，具体账号与模型权限以部署时实际结果为准。登录状态在 `storage/codex_official/`，不随仓库分发。

## 数据目录

- 普通 CLI / GPU：默认 `output/factory.sqlite3`、`output/`、`cache/`。
- CPU 启动配置：新安装使用 `runtime-cpu/output/` 和 `runtime-cpu/cache/`。若旁边存在旧版本 CPU 数据，兼容启动器可能优先复用最近的旧 CPU 库。
- 正式成品通常位于输出目录下的 `workflow_exports/`；原片以引用路径保存。
- `.env.llm` / `.env.asr`、模型缓存、CLI 登录状态、任务日志均属于本机数据。

修改范围后人工字幕会保留并要求复核；修改上游配置不代表旧成品会自动更新。备份现有数据后再迁移环境，具体约束见 `AI_OPERATIONS.md`。
