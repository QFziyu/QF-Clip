> 保留的原项目资料。本机路径、版本和验收数量描述原部署环境；新安装以[项目 README](../README.md)和[文档索引](README.md)为准。

# 1.7.0 抖音发布试点 · 实施与验收报告

日期：2026-10-26（北京时间）。交付工厂 1.7.0、独立 FootballPublisher 1.0.0；轻舟 1.2.0、调度器 1.0.0 保持接口兼容。

## 交付结论

已经实现“正式导出 → 校验交付 → 独立发布队列 → 网页上传填写 → 可选自动提交”的程序链路，接入原五步工作台的唯一导出页。原有本地导出保持默认，来源自动任务与手动任务共用审核快照。

**本地软件与模拟平台验收通过；真实抖音账号尚未登录、上传或试发，因此真实平台验收未完成。** 当前是可校准的单账号试点，不能承诺对未测试的抖音页面无人值守。自定义封面的真实网页控件仍需校准，现阶段会暂停协助。官方 H5/SDK 没有被冒充为无人值守后台接口。

## 实现范围

- 独立发布服务：本机 17656、配对令牌、独立 SQLite、任务租约、持久队列、串行浏览器操作、GPU/CPU 数据隔离。
- 工厂交付：只登记完整且当前的正式导出批次；视频完整 SHA-256、FFprobe 时长/尺寸核验，字幕和附件校验，不投递预览、拒绝项、待定项或历史成品。
- 审核绑定：冻结最终人工/自动字幕、标题、说明、画幅、账号、话题、封面文件内容。上游修改后旧待派发授权作废，需要重新确认。
- 可靠交接：工厂持久 outbox 与发布器原子幂等受理；响应丢失可重交同一交付，发布失败不重新转录、选片或渲染。
- 第五步：本地/抖音输出去向、单一发布画幅、冻结内容预览、明确批次授权、任务状态与已知作品 ID/链接。设置页连接、观察并确认账号，样片校准后才显示自动提交。
- 独立 Edge：不读取日常 Edge Cookie。按可见语义操作，账号不明、页面变化、登录/验证码等进入协助；提交前再次检查账号和页面文案。
- 防重复：提交标记先持久化再点击；提交后故障进入待核验，不自动重发。准备表单未核验或停止前阻止下一条上传，避免覆盖当前表单。
- GPU/CPU 发布入口、完整自动生产入口、归属/版本检查、停止服务入口与四组件源码包。

## 实际测试证据

| 验收 | 实际结果 |
|---|---|
| 工厂全量回归 | **314 项通过，0 失败，0 错误，0 跳过**；150.98 秒。含实际 FFmpeg、Streamlit AppTest、模拟 API/ASR |
| 独立发布器回归 | **32 项通过**；覆盖幂等、竞争租约、提交后失联、有限重试、账号绑定、编码 ID、项目过滤、封面篡改、待发布表单保护、页面文案变化 |
| 发布专项工厂回归 | 29 项通过，含新加封面冻结及已发布作品链接检查 |
| 真实 FFmpeg + HTTP 集成 | 生成 4 秒中文/空格路径素材，实际横 320×180、竖 180×320 渲染及完整解码；保留人工标题、说明与 SRT |
| 单画幅及重启补交 | 两种本地画幅只投递授权横屏；模拟受理后响应丢失、重建服务和数据库连接后补交，只有 **1 个任务、1 次私有上传** |
| 真实 Edge 浏览器 | 安装的 Microsoft Edge + Playwright，操作自建 localhost 测试页面；身份、上传、文案、准备不提交、一次提交及指定回执匹配均通过 |
| 实际工作台浏览器 | 检查 1366×768 窗口和窄栏页面：第五步未绑定提示、禁用按钮和设置入口可见，未发现重叠、横向溢出；没有点击连接真实账号 |
| 旧数据核对 | 24 项目、372 候选、6 条工作台媒体记录 ID 全保留；21 个工作台的条目、标题、审批、字幕及范围核对一致；真实发布意图数量为 0 |
| 运行组件 | `output/publishing_upgrade/runtime_checks.json` 记录 GPU/CPU 版本、配置、进程归属和重复启动检查；此项不代表本轮执行过 Whisper GPU 推理 |

可复现命令：

```powershell
.venv\Scripts\python.exe scripts\run_tests.py
.venv\Scripts\python.exe scripts\verify_publishing_pipeline.py
cd ..\FootballPublisher
..\FootballClipFactory\.venv\Scripts\python.exe -m unittest discover -s tests -v
..\FootballClipFactory\.venv\Scripts\python.exe tests\browser_fixture.py ..\FootballClipFactory\output\publishing_upgrade\fixture.mp4 ..\FootballClipFactory\output\publishing_upgrade
```

测试输出保存在 `output/test_results.json`、`output/publishing_upgrade/final_factory_tests.log`、`publisher_tests.log`、`pipeline_report.json`、`browser_fixture.json`、`data_preservation.json`、`ui_settings.png`。样片脚本每次创建独立运行目录，不接触用户生产数据，不请求付费 API或真实平台。

## 仍需真实账号完成的验收

1. 用户选择可发布的测试作品和目标抖音账号，正常登录独立 Edge；实际核对账号身份和上传/编辑/提交控件。
2. 先准备一条样片，检查标题、说明、字幕、封面、声音和账号；完成一次实际投稿及作品 ID 核验。
3. 校准后测试自动提交，以及平台审核、长视频大小/时长限制、登录过期与实际网络异常。当前回执缺少可靠作品 ID 时需人工提供 ID，不会根据相似标题或通用成功提示猜测“已发布”。
4. 自定义封面控件单独校准。未验证的控件会暂停，不能把文件哈希验证解释为封面网页上传成功。

本轮未重新执行真实直播转录、付费 LLM 或一小时抖音上传；以前的长样片测试不替代本次平台验收。咪咕真实完整链路仍依赖用户已登录 Edge 会话，海星已有短样片记录。当前旧项目记录的部分 `E:\record\…` 原片不可访问，已保留其缓存和审批；需恢复原路径才能重新渲染，程序没有猜测替换原片。

## 交付与数据保留

- 主程序：`D:\Codex\FootballClipFactory`。
- 发布模块：`D:\Codex\FootballPublisher`。
- GPU：`launch.cmd --profile gpu --publishing`；CPU：`launch.cmd --profile cpu --publishing`。来源全自动流程使用 `launch.cmd --profile gpu` / `launch.cmd --profile cpu`。
- 操作步骤见 [PUBLISHING_GUIDE.md](PUBLISHING_GUIDE.md)。首次开始应在“设置”连接账号。
- 升级前在线备份：`output/publishing_upgrade/before.sqlite3`。CPU 原有独立库在线备份到新版本数据目录，旧数据目录保留。
- 源码包与 GPU/CPU 快照位于 `D:\Codex\FootballClipFactory_Releases`；仅替换旧版本源码、源码压缩包及清单，保留 `release-data`、`data-snapshots`、原片、缓存和既有成品。包内不含 API 密钥、配对令牌、浏览器会话或发布数据库。

自动提交仍受每批授权和账号样片校准控制。没有将本轮“实施代码”的授权用于真实平台投稿。
