# 文档索引

首次使用以根目录 [README](../README.md) 为准。以下第一组文档说明独立仓库的安装和维护；保留资料中的绝对路径、部署版本和旧测试数量只描述原本机环境。

## 当前工程文档

| 文档 | 内容 |
| --- | --- |
| [CONFIGURATION.md](CONFIGURATION.md) | 配置文件、覆盖顺序、常用环境变量及数据目录 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 源码模块、数据流和审核不变量 |
| [INTEGRATIONS.md](INTEGRATIONS.md) | 独立下载、调度、发布及中文识别服务 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 开发、回归、媒体验收和源码打包 |
| [AI_OPERATIONS.md](AI_OPERATIONS.md) | 自动操作、字幕复核和导出前必须遵守的约束 |
| [GITHUB_UPLOAD.md](GITHUB_UPLOAD.md) | 本地提交、远程连接及上传前检查步骤 |
| [REPOSITORY_PREPARATION.md](REPOSITORY_PREPARATION.md) | 本次提取、改动、验证和交付范围 |
| [LICENSING.md](LICENSING.md) | 原项目未提供开源许可证的处理说明 |

## 保留的专项资料

- [双路合成](DUAL_COMPOSITION.md)、[画面同步与顺序拼接](VISUAL_SYNC_SEQUENCE.md)、[比赛时钟](match_clock.md)。
- [自动流程 API](AUTOMATION_API.md)、[发布操作](PUBLISHING_GUIDE.md)、[平台标题与 B站](PLATFORM_TITLES_AND_BILIBILI.md)。
- [统一启动](UNIFIED_START.md)、[模型设置](LLM_SETTINGS.md)。其中账号登录与独立服务信息应按实际部署核验。

## 历史验收与计划

目录中的 `*ACCEPTANCE*.md`、`SUBTITLE_REVIEW_UPGRADE.md`、`MONTAGE_REVIEW_FIX.md`、`AI操作与跨项目导出改进计划.md`、`模式与长视频剪辑开发计划.md` 保留原项目的实现背景和历史验证范围，不是待自动执行的任务。

原根目录长篇 README 与早期报告移到 [history/](history/README.md)。旧报告提及的运行 JSON、日志、数据库和媒体不随源码交付。
