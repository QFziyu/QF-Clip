# AI 操作接口与不可回退的工作流约束

在实际仓库或已部署的项目根目录运行。GPU 默认工作台端口 8501，CPU 默认 8502，最终以启动日志为准。先核对页面版本、配置实际来源和数据库路径，再操作；不要另开目录造成新旧数据库分流。

## 字幕与审核

- 本地 CPU Zipformer 是音频识别器。启用语义复核后，LLM 收到的是带时间戳的文字，负责上下文纠错、标点和断句。它没有再次听音频，不能称为“两套音频 ASR 互相验证”。
- 主线为：本地识别并缓存原文 → 已配置的大模型语义复核 → 保守合并纠错、保留时间轴 → 自动生成完整预览 → 人工审核。预览生成是制作任务的一部分，不应要求用户再手动生成一次。
- `complete` 或 `empty` 才是自动复核成功；`pending/failed/partial/unconfigured` 必须显式报告。失败缓存保留，有限重试，不能关闭复核开关或伪造成功状态来“修好任务”。尊重显式不加字幕、关闭复核及人工字幕的选择。
- 人工标题、人工字幕、审批、1～5 星用户评分不可被重新分析覆盖。保留 AI 推荐分、选片理由以及上一条/下一条按钮。旧的缺失分数不能凭空补，需使用实际选段转录进行评分。
- 原始 ASR 不可覆盖。保存校对前后文本、采纳/拒绝的修改、提供方/模型和失败状态。人名不确定时保留，不根据比赛常识编造事件。
- 所有调用复用该源项目预算账本；不能改用新目录规避预算，也不能擅自提高预算上限。版本变更后用真实缓存做复核验证，并区分自动测试和真实 API 结果。

## 统一控制入口

在主目录运行：

```powershell
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py capabilities
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py summary
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py approved
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py projects
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py projects/WORKSPACE_ID
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py projects/WORKSPACE_ID/repair-previews --method POST
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py jobs/JOB_ID
.venv\Scripts\python.exe -X utf8 scripts/agent_control.py jobs/JOB_ID/retry --method POST
```

同一业务层也通过已配对的本机控制服务 `/api/v1/agent/<route>` 提供。沿用本地令牌认证，禁止在日志或报告打印令牌。CLI 返回 JSON，`ok:false` 是失败，不能当作处理完成。

## 跨项目导出

第五步默认汇总所有未归档项目和方案的已通过条目，包括非活动方案。未审核和已拒绝条目不得导出。默认勾选未导出/修改后的内容；旧成品保留为历史。完整列表由 `approved` 返回，每项包含唯一 `key`。

先将选中的 key 写入 UTF-8 JSON：`{"keys":["workspace/variant/item"],"orientations":["horizontal","vertical"]}`，调用 `exports/plan --method POST --json-file request.json`。如有 `issues`，先修复或明确取消问题项，再预检。将返回的计划原样放入 `{"plan":...}`，调用 `exports/submit`。预检后上游变化需重新生成计划；不允许修改计划后伪造审批。

批次幂等键由计划生成。重试原任务可恢复已完成子任务并校验文件，不能重复创建任务来绕过错误。导出过程中上游修改时，冻结的本地成品可完成并归入历史；新版本需重新预检。全局批次不包含发布授权，不会自动发布。

单项目的模板确认与发布仍在第五步“当前项目导出与发布”中。发布必须使用本次人类明确授权，不能从导出授权推断；未知发布结果不可盲目重投。

## 修改程序前后的检查

1. 查看当前进程命令行、代码版本、配置实际来源、数据库路径和任务状态；不读取或展示密钥值。
2. SQLite 使用在线备份。合并数据只添加缺失 ID，不以旧库覆盖新项目、审核和字幕。不要移动/删除缓存或成品来腾空间，除非用户明确要求。
3. `scripts/run_tests.py` 隔离安装目录的真实 LLM 配置。测试失败先修复；合成 FFmpeg 用例与真实 API/真人素材分别报告。
4. 浏览器核对评分、人工星级、上一条/下一条、API 复核状态和可播放预览。第五步核对其他项目的已通过条目确实可见；不为验收而改变真实审批或发布。
