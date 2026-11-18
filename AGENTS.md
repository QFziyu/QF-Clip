# Football Clip Factory 工程约束

编辑字幕、工作台、任务队列或 AI 接口前，阅读 `docs/AI_OPERATIONS.md`。

不得把 CPU 识别标签当成 API 复核结果。复核启用时失败必须保留失败状态与原文缓存；不能静默回退为“已完成”。保留 AI 评分/理由、人工星级、下一条操作、自动完整预览和全部人工修改。

修改代码后运行 `scripts/run_tests.py`。涉及渲染需运行合成 FFmpeg 验收。部署到固定主入口前备份数据，禁止覆盖用户配置或用旧数据库替换新数据库。

AI 操作优先使用 `scripts/agent_control.py` 或配对 HTTP 接口；先查询 capabilities、summary，再预检提交。导出与发布是独立授权范围。
