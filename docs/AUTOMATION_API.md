> 保留的原项目资料。本机路径、版本和验收数量描述原部署环境；新安装以[项目 README](../README.md)和[文档索引](README.md)为准。

# 本机自动生产 API · v1

三个独立进程只以 HTTP/JSON 连接。调度器不得导入两个应用的业务模块或访问其内部数据库。轻舟仍可单独手动下载，工厂原 CLI 与五步工作台仍可单独使用。

| 组件 | 版本 / 地址 | 鉴权 |
|---|---|---|
| 轻舟 | 1.2.0 / `http://127.0.0.1:17653` | `X-Lightboat-Token` |
| 调度器 | 1.0.0 / `http://127.0.0.1:17654` | `X-Workflow-Token` |
| 工厂 | 1.6.0 / `http://127.0.0.1:17655` | `X-Workflow-Token` |

令牌由本机配置提供，不在文档中给出。控制接口拒绝非本机 Host 和网页 Origin，扩展通过轻舟的原有配对规则连接。POST 使用 `Content-Type: application/json`、JSON 对象，最多 2MB。版本不兼容或端口归属不明必须停止启动。GET health 返回 `service`、`api_version:1`、`version`、`capabilities`；调度器与工厂还返回固定的 `profile:gpu|cpu`。

## 调度器

- `GET /api/v1/health`、`GET /connections`（以下接口均带 `/api/v1` 前缀）：组件、扩展心跳。
- `GET /templates` / `POST /templates`：读取/保存工厂制作模板。
- `POST /flows`：`{idempotency_key, source_url, template_id, template_revision, title?}`。同键同参数返回同 ID，不同参数 409。模板修订在受理时固定。
- `GET /flows`、`GET /flows/{id}`：状态、下载 ID、制作 ID、工作台 ID、帮助信息、成品列表。
- `POST /flows/{id}/retry`：查询/恢复同一子任务，不新建重复制作。
- `POST /flows/{id}/choose`：`{candidate_id}`，明确选择本次浏览器会话的资源。
- `POST /flows/{id}/confirm`：`{snapshot}`，在唯一导出界面完成审核后提交；转发工厂重新校验快照。
- `POST /flows/{id}/cancel`：停止后续流程跟踪，下载尚未交付时取消下载。已确认工厂导出可继续完成；不删除文件。取消后不能再 confirm/retry/choose。

状态：`acquiring → downloading → preparing → previewing → awaiting_review → exporting → completed`；另有 `needs_assistance / failed / cancelled`。每 5 秒轮询；网络错误最多自动重试 3 次，退避上限 30 秒。权限/身份/格式问题停止自动推进。GET 状态不持有整轮调度锁。

## 轻舟

- `POST /api/v1/jobs`：`{idempotency_key,source_url,title?,minimum_minutes?,require_audio?}`。
- `GET /api/v1/jobs`、`GET /api/v1/jobs/{id}`；`POST .../{retry,pause,cancel,choose}`。
- `POST /api/v1/bridge/{claim,heartbeat,candidates,assist}`：扩展任务租约、确认、资源候选与协助。任务/命令 ID 必须匹配。租约 95 秒，浏览器 alarm 每 30 秒唤醒；3 分钟仍无法确定回放转为人工协助。扩展关闭/休眠后可恢复租约，不依赖永久定时器。

受理同一事务登记幂等键和下载 ID。Cookie、Authorization 和临时签名 URL 仅内存/浏览器会话保留；数据库不保存。续传重新解析资源，核对站点资源 ID、画质、分段路径/范围/时长指纹；变化时停止，不能混用分段。默认两任务、每任务六段下载。普通 MP4 支持下载后校验，不支持未知时长的 MP4 自动择优，需要人工明确选择。

完成记录 `artifact` 的公共字段：`verified:true,path,bytes,duration,source_identity,video:{width,height,codec},audio_tracks:[{index,codec}]`。先合并到临时文件、FFprobe 校验、原子移动，再提交完成记录。重启遇到已原子交付但未提交记录时重新校验该确定目标。调度器只接受 `completed` 且 verified 的记录。

## 工厂

- `GET/POST /api/v1/templates`：模板包括 `id/revision/name/settings/packaging/export/source`。首期 `organization:single`；不接受 mock、transcript_path、凭据等外部参数。
- `POST /api/v1/productions`：`{idempotency_key,flow_id?,name?,artifact,template}`。验证文件位于配对根目录，存在、大小、时长、音视频轨一致；只引用素材不复制。事务内登记制作 ID 与确定的工作台 ID。
- `GET /api/v1/productions/{id}`：状态、工作台、prepare/preview/export 子任务、确认记录、成品。
- `POST .../{id}/confirm`：核对当前通过项、人工字幕及必剪事件、快照修订；`production:{id}:export:{digest}` 为队列幂等键，重复确认返回相同任务。新修订产生新导出，历史成品保留。
- `POST .../{id}/retry`：复用失败/中断的队列任务及既有 ASR/LLM 缓存。

工作台任务与幂等映射在同一队列事务登记；若受理后 HTTP 响应丢失，原键重试得到原任务。转录/渲染共用现有单重队列。空闲磁盘低于 1GB 暂停新生产，分段落盘另检查 256MB 余量。此阈值不能代替容量规划。

## 环境与录制边界

每个 flow 固定 GPU/CPU 环境，工厂健康检查拒绝跨环境衔接。固定端口仅允许一套自动配置。所有进程可独立重启，重新启动同一入口即恢复；不安装 Windows 自启动项。

未来录制器适配器提交已关闭并通过音视频检查的文件与顺序清单，复用 production 的 artifact 入口。首期没有开播检测、边录边剪或自动发布。
