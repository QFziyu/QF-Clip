> 历史安装记录。路径、端口、版本和通过数量仅描述当时环境；新安装以根目录 README 为准。

> 最新版 1.5.1 的 UI 重构验收见 [五步工作台验收报告](../../docs/WORKBENCH_ACCEPTANCE.md)。以下为原有算法的历史验证记录。

# 最终版验证与复现

> 此页保留 1.0 基线验证。当前 GPU 补丁版与真实录像测试见 [GPU_REPAIR_REPORT.md](GPU_REPAIR_REPORT.md)。当前主目录的严格版本检查应使用 `verify v1.0.1-gpu --primary`。

版本：1.0.0 / v1.0-final。验收日期：2026-10-14。正式验收结论、功能范围和用户测试步骤见 [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md)。

本机 Windows 11 / Python 3.13.5 / FFmpeg 7.1。实际执行了真实媒体编码、真实 tiny Whisper CPU 推理、本地 HTTP 协议请求和真实独立队列进程；没有调用线上付费 LLM，也没有使用真实比赛录像。所有自动登记的审批仅针对测试夹具，不代表真人内容审核。

## 可复现命令

从 `D:\Codex\FootballClipFactory` 运行；以下 `python` 指 `.venv\Scripts\python.exe`：

```powershell
python main.py doctor
python scripts/run_tests.py
python scripts/queue_smoke.py

# 已有本地 tiny 模型和合成语音时，不下载、不使用付费 API。
python scripts/acceptance.py

# 初次准备本地 tiny 模型与 Windows 中文合成语音；首次下载约 75 MB。
python scripts/smoke_asr.py --allow-cpu
# 已下载后禁止访问模型下载服务。
python scripts/smoke_asr.py --allow-cpu --offline

# 无模型、无 API 的基础演示；实际编码，转录与选片是模拟。
python main.py demo --approve --export --full-hd

# 逐个完整解码当前清单中的文件，验证分辨率、时长、字幕与映射。
python scripts/verify_exports.py "output/PROJECT_ID/export_manifest.json"

# 发布完整性（包括压缩包与数据库备份）。
python scripts/release_manager.py verify v0.1-baseline
python scripts/release_manager.py verify v0.9-rc
python scripts/release_manager.py verify v1.0-final --primary
```

`scripts/acceptance.py` 需要 `cache/models/tiny` 与 `output/asr_smoke/synthetic_zh.wav`，缺少时明确报错，不擅自下载。每次验收写入独立的 `output/acceptance-runs/时间戳/`。`scripts/queue_smoke.py` 生成独立配置与数据库，模拟转录及选片，不需要 Whisper 模型或 API。

## 测试证据

| 验证 | 证据 |
| --- | --- |
| 基线完整自动化：99 项通过，无失败、错误或跳过 | JSON 结果（从原始日志保留）（原安装运行产物：`output/validation/v1.0-test-results.json`，不随源码分发）、final_test_run.log（原安装运行产物：`output/final_test_run.log`，不随源码分发） |
| 真实 tiny CPU → HTTP 夹具 → 预览 → 审批 → 4 个 Full HD 成品 → 缓存复用 | 端到端报告（原安装运行产物：`output/acceptance-runs/20261014-213358/report.json`，不随源码分发）、完整解码结果（原安装运行产物：`output/acceptance-runs/20261014-213358/exports/ccfb07bc63d6888aab1f933a/verification.json`，不随源码分发） |
| 真实后台进程、3 条预览、6 个测试分辨率导出 | 后台队列报告（原安装运行产物：`output/queue_smoke/report.json`，不随源码分发） |
| CUDA 推理失败后明确允许 CPU 回退 | 运行记录（原安装运行产物：`output/final_gpu_check/report.json`，不随源码分发） |
| 最初 21.28 秒中文合成语音识别与参考文本 | ASR 报告（原安装运行产物：`output/asr_smoke/report.json`，不随源码分发） |
| 当前依赖、工具与设备探测 | 环境检查（原安装运行产物：`output/final_environment.json`，不随源码分发）、[固定依赖](../../requirements.lock.txt) |

真实媒体测试覆盖中文路径、多音轨、非零 PTS、延迟音轨补静音、精确首帧、横竖屏、ASS 硬字幕、完整解码和 NVENC 失败回退。缓存/数据库测试覆盖部分 ASR 恢复、损坏缓存隔离、预算损坏拒绝清零、JSON Schema 降级、证据约束、人工修改持久化、不可变转录绑定和并发审批/导出。UI 使用 Streamlit AppTest 验证，并做实际浏览器检查。

真实推理曾暴露 faster-whisper 1.2.1 与 PyAV 19 不兼容，已固定 `av==16.1.0` 并加入真实音频解码回归。最终阶段还修复词时间戳整数/小数规范化导致的快照误报，使用实际 Whisper 输出保留回归夹具；旧版转录损坏时不允许已审核片段悄悄继承新字幕。

## 尚不能由这些测试证明的事项

真实足球解说准确率、线上 LLM 选片质量、60～180 分钟生产吞吐及 medium CUDA 推理未验证。本机虽然可见一块 NVIDIA GPU 且 NVENC 编码成功，Whisper CUDA 实际加载仍报告缺少 `cublas64_12.dll`；CPU/int8 回退已实际成功。`doctor` 的设备可见性检查不等于成功推理。

53 项依赖按实际安装元数据递归锁定，安装脚本在现有环境离线执行通过；未做新电脑纯净安装。回退版本源码和数据隔离，默认复用主项目依赖，需要时可以单独安装回退环境。

真实素材验收请先选择 5～10 分钟录像，在最终版中确认音轨、至少 3 条字幕与选片范围、审批保存及横竖屏导出，再扩大到整场录像。
