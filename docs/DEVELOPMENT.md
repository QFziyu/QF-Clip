# 开发与验证

## 本地开发

应用从仓库根目录直接运行，无需更改成 `src/` 布局或执行 `pip install .`。`requirements-dev.txt` 复用基础依赖，测试框架为 Python 内置 unittest。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/run_tests.py
```

如需指定媒体工具，在本机 `.env` 设置 `FFMPEG_PATH` / `FFPROBE_PATH`。测试也识别 `FCF_TEST_FFMPEG` / `FCF_TEST_FFPROBE`。未发现工具时部分媒体测试会跳过，应核对报告而非只看退出码。

## 验证范围

| 命令 | 用途 |
| --- | --- |
| `python scripts/run_tests.py` | 常规回归、模拟 API / ASR、Streamlit AppTest、合成媒体用例；输出 JSON 报告 |
| `python scripts/run_tests.py --pattern test_releases.py` | 定向检查源码包、数据排除及本地检查点 |
| `python scripts/test_offline.py` | 限制 Python 进程外网连接及真实模型 CLI 推理；仅允许回环测试服务 |
| `python scripts/verify_workbench.py` | 程序生成素材、六种工作流、真实横竖屏编码与完整解码 |
| `python main.py doctor` | 查看媒体工具、依赖、设备与 API 是否配置，不调用模型 API |

测试应在独立源码副本或干净环境运行，不加载个人服务会话。`run_tests.py` 隔离仓库中的真实 LLM 配置，CI 不提供真实 API 凭据。测试通过不等于实测整场赛事识别、付费 API 或平台投稿。

保留的 `verify_real_download.py`、`verify_local_asr.py`、`verify_publishing_pipeline.py` 等是特定集成验收工具，可能访问外部服务或媒体，需要先阅读脚本和对应历史报告；它们不属于默认 CI。

## GitHub Actions

`.github/workflows/tests.yml` 为 Windows / Python 3.11、3.13 配置依赖安装、FFmpeg、回归及合成媒体验收。CI 在 push、pull request 或手动触发时运行，不包含部署和发布操作。此整理阶段未运行 GitHub 云端工作流。

## 源码交付包

```powershell
.\.venv\Scripts\python.exe scripts/package_source.py
```

默认生成 `dist/FootballClipFactory-v1.9.0-source.zip`。也可用 `--output` 指定新文件。脚本不覆盖已有 ZIP，不上传；包内含逐文件 SHA-256 清单。沿用源码检查点的筛选规则，排除环境文件、运行目录与媒体；新加的源码文件仍需人工检查内容。

Windows 深目录可能触发成品文件名保护。合成验收可使用 `python scripts/verify_workbench.py --destination "$env:TEMP/fcf-media-check"`；真实任务可通过 `OUTPUT_DIR` 指定较短的本机目录。
