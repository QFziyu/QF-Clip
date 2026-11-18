> 历史安装记录。路径、端口、版本和通过数量仅描述当时环境；新安装以根目录 README 为准。

# GPU 转录修复与真实录像验证

版本：**1.0.1 / v1.0.1-gpu**  
日期：**2026-10-15**

## 修复结论

已修复 `Library cublas64_12.dll is not found or cannot be loaded`。用户提供的 **63 分 18 秒完整录像，13/13 块已全部使用 Whisper medium / CUDA / int8_float16 完成转录**，生成 **863 个语音段、11,183 个词级时间戳**。运行明确设置 `allow_cpu=False`，未使用 CPU 回退。

原任务已从现有音频缓存恢复，整次运行耗时 **512.86 秒（约 8 分 33 秒）**。GPU 转录通过；完整选片流水线尚未完成，因为当前没有配置 API Key 与 LLM 模型名称。这两种状态在报告中明确区分。

## 原因与修复范围

RTX 4060 Laptop 和 NVIDIA 581.57 驱动能够识别 GPU，但驱动不包含 Whisper 所需的 cuBLAS、cuDNN 等用户态计算库。`nvidia-smi` 显示 CUDA 13.0 表示驱动兼容能力，不等于项目已经装有 `cublas64_12.dll`。Python 也不会自动搜索 NVIDIA wheel 中的 DLL 子目录。

本次在项目 `.venv` 内安装并固定以下 NVIDIA 官方 Python 包：

| 包 | 版本 |
| --- | --- |
| nvidia-cublas-cu12 | 12.9.1.4 |
| nvidia-cuda-runtime-cu12 | 12.9.79 |
| nvidia-cuda-nvrtc-cu12 | 12.9.86 |
| nvidia-cudnn-cu12 | 9.10.2.21 |

cuDNN 与当前 CTranslate2 4.8.2 自带的主 DLL 版本一致。安装未修改 `.env`，未安装完整 CUDA 开发工具链，未替换显示驱动，未修改系统 PATH。

程序改动：

- `core/gpu_runtime.py` 从已安装分发包发现 DLL，在导入 CTranslate2 / faster-whisper 前注册进程加载目录，保持加载句柄有效。也支持系统 PATH 中明确存在的所需 DLL。
- 转录阶段提前检查库加载能力；保留显式 CPU 回退选项。成功加载模型后，日志显示实际设备和计算精度。
- `doctor` 分别显示显卡可见性、DLL 可加载性和 CUDA 就绪状态，不再只凭“能看到显卡”判断运行环境可用。DLL 检查仍不代替模型实际推理。
- 网页将设备字段标为“请求设备”，避免把配置值误认为已经成功使用 GPU。
- `setup.ps1 -Gpu` 和 `requirements-gpu.txt` 支持重复安装；最终版启动器识别当前补丁版本，避免误复用旧服务进程。

## 实际验证

测试素材：`D:\Codex\下载视频\BV1TWpp6vEm2\P01 上半场＋中场休息.mp4`。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 显卡与库加载 | CUDA 设备可见；所需 CUDA/cuDNN DLL 均可加载 | doctor.json（原安装运行产物：`output/gpu_repair/doctor.json`，不随源码分发） |
| 90 秒真实素材短测 | medium，CUDA/int8_float16；12 个语音段、250 个词级时间戳；约 27.58 秒 | sample_report.json（原安装运行产物：`output/gpu_repair/sample_report.json`，不随源码分发）、sample.log（原安装运行产物：`output/gpu_repair/sample.log`，不随源码分发） |
| CPU 回退排除 | `allow_cpu=False`，实际运行记录为 `device=cuda` | 同上短测报告及转录缓存 |
| 自动化回归 | 109 项通过，0 失败、0 错误、0 跳过 | test_run.log（原安装运行产物：`output/gpu_repair/test_run.log`，不随源码分发）、test_results.json（原安装运行产物：`output/gpu_repair/test_results.json`，不随源码分发） |
| 安装可重复性 | 已安装环境中离线执行 `setup.ps1 -Gpu` 成功；所有固定依赖满足，`.env` 哈希不变 | 本次安装器实际执行 |
| 旧版保护 | v0.1-baseline、v0.9-rc、v1.0-final 均保持原有源码/ZIP/数据库备份哈希 | 版本工具完整性检查 |

### 完整录像结果

完整录像时长 **3798.074 秒（63 分 18 秒）**，项目 ID `e65d6d49704553c13fdb1795`，原队列任务 ID `c7d40b82315e420faf8472094f9bc15f`。此次按原来的 hybrid / medium / CUDA / int8_float16 参数重试，复用已有音频，不允许 CPU 回退。

13 个完成块的设备记录全部为 CUDA/int8_float16；合并后的 863 个语音段与 11,183 个词时间戳通过源时长范围校验。随后在禁止加载模型的测试条件下重新调用转录阶段，成功复用完整缓存，证明后续重试无需重新识别这 63 分钟。

原队列目前显示失败，具体原因已变为 `未配置 OPENAI_API_KEY`，发生在**转录完成后的选片阶段**，不再是 CUDA DLL 错误。完整转录位于 transcript.json（原安装运行产物：`cache/e65d6d49704553c13fdb1795/transcript.json`，不随源码分发）。本次没有生成真实 AI 候选或审批成品，也没有对识别文字准确率进行完整人工核对。

最终运行统计保存到 full_recording_report.json（原安装运行产物：`output/gpu_repair/full_recording_report.json`，不随源码分发）。

## 使用方式

继续双击 [launch.cmd](../../launch.cmd)，或打开 [本地审批页](http://127.0.0.1:8501/)。本机已完成 GPU 依赖安装；无需为了这次错误切换到 CPU，也无需重新导入录像或删除缓存。

要继续语义选片，在 `.env` 填写 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，然后在“任务队列”重试原任务。已完成的转录会直接复用。不要把 API 未配置产生的选片错误与 CUDA 加载错误混为一谈。

以后重建环境时：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1 -Gpu
.\.venv\Scripts\python.exe main.py doctor
```

当前补丁源码与数据库备份另存为 `FootballClipFactory_Releases/v1.0.1-gpu`；旧版本不覆盖。`launch.cmd` 默认启动当前补丁版。费用与候选质量仍需要配置真实服务商后验证，本次没有发起付费 LLM 请求。
