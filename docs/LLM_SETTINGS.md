> 保留的原项目资料。本机路径、版本和验收数量描述原部署环境；新安装以[项目 README](../README.md)和[文档索引](README.md)为准。

# 模型与 API 切换

打开工作台左侧「设置」→「模型与 API」。

- 自有 API：填写 Base URL、Key、准确的模型名称和思考等级，点击「保存并使用此来源」。
- Codex CLI：使用官方 CLI 的 ChatGPT 登录。模型可留空使用 CLI 内置默认，也可填写该账户支持的模型。思考等级单独保存。
- 切换来源会带回各自配置；密码框留空保留旧 Key。
- 「检查 Codex 登录状态」不调用模型；「测试已保存配置」会发起一次实际请求。
- 后续开始执行的任务读取新设置；正在执行的任务沿用开始时的配置。失败任务可从任务记录重试。
- 选片、字幕校对、语义对轴、图片对轴共用所选来源与参数。
- 不自动切换来源，不会因为某个来源额度不足而擅自调用另一个付费来源。

网页保存文件是主目录的 .env.llm，优先于 .env 和启动进程中继承的 LLM 变量；删除该文件可恢复原配置。该文件可能包含密钥，不应分享。不会修改 Codex 的全局配置或导出登录令牌。

Codex 调用采用 codex exec --ignore-user-config --ephemeral --sandbox read-only --output-schema，只传当前请求需要的文字和图片。它复用 CLI 登录，清除子进程中的 API Key 与自定义 API 地址，并要求 ChatGPT 登录。模型和思考等级作为本次调用参数传入。默认指 CLI 内置默认，因忽略用户全局配置，不会继承其自定义模型或代理。

Codex 使用账户的 Codex 订阅限额；Plus 不是普通 API Key 余额。项目只记录调用次数，不估算订阅美元费用；独立 Codex 记录不会被旧 API 的本地预算阻挡，仍保留 max_calls 约束。

思考等级可用值取决于模型和客户端，选项不代表所有模型都支持。不支持时显示失败，调整参数后再试。API 发送 reasoning_effort；Codex 发送 model_reasoning_effort。改变来源、模型或指定思考等级会区分缓存；原始录像、转录及审批记录仍可复用。

参考：
- https://learn.chatgpt.com/docs/auth
- https://learn.chatgpt.com/docs/non-interactive-mode
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
