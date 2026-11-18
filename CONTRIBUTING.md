# 参与开发

先阅读 `AGENTS.md`，再阅读与改动有关的文档。建议从 CPU 模式开始，不依赖独立下载器、调度器或发布服务。

1. 使用 Python 3.11+ 创建项目虚拟环境，安装 `requirements-dev.txt`，配置 FFmpeg / FFprobe。
2. 从 `.env.example` 创建个人 `.env`。新增配置项时同步更新配置说明和空值示例，不提交实际凭据。
3. 维护现有模块边界：页面调用业务层，数据库操作放在 `storage/`，耗时操作进入任务队列。
4. 保留人工字幕、标题、星级和审批。任务失败必须保留失败状态、原文缓存及恢复入口。
5. 运行 `python scripts/run_tests.py`。涉及媒体处理时运行 `python scripts/verify_workbench.py`。
6. PR 中说明触发问题、修改后的行为、验证范围、跳过项和数据兼容性。

数据库结构变更应兼容旧数据，迁移前使用 SQLite 在线备份。用临时数据库与合成媒体做测试，避免操作个人安装中的真实项目。不要复制旧数据库来覆盖现有数据库。

新安装和 CI 使用 `requirements.txt` 声明依赖；`requirements.lock.txt` 是原 Windows 环境的固定版本快照，不含哈希，更新它时需要重新安装和运行验证。

协作不等同于许可授予；对外发布前应先确定项目许可证，见 `docs/LICENSING.md`。
