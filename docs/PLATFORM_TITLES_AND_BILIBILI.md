> 保留的原项目资料。本机路径、版本和验收数量描述原部署环境；新安装以[项目 README](../README.md)和[文档索引](README.md)为准。

# 双平台标题、字幕默认值与B站投稿

## 标题

第三步保留作品标题（用于本地文件名），新增独立的“抖音投稿标题”和“B站投稿标题”。第五步按所选发布平台冻结对应标题，抖音标题不会误用到B站。修改字幕或范围后请重新检查标题。

新方案默认按实际保留的字幕生成双平台标题。抖音采用人物/球队与具体问题、反差的短标题；B站提供球队、讨论背景与具体看点。建议长度是软件的编辑策略，并非平台推荐算法承诺。模型必须返回当前片段中可核对的原话作为依据。

“AI 生成本批抖音 / B站标题”和“AI 重新生成本条双平台标题”支持缓存、失败重试。人工修改的作品标题与平台标题保留；自动修改已审核项会将其返回待审核。若模型不可用，可点“从已有话题恢复本批标题（不联网）”，使用本地已保存的AI话题索引恢复具体名称。

多段集锦此前以“精选集锦 1”等占位名覆盖话题标题。现已修复：组装阶段先保留真实话题名，随后可生成双平台版本。

## 字幕

默认横、竖屏字号由34改为25.5，精确为原来的75%。新增“字幕下移（画面宽度 %）”，默认5%。实际底部间距 = 原底部间距 − 画面宽度 × 5%；触底时保留4像素安全距离。原横屏底距只有36像素，因此1080p横屏不能再下降96像素，否则会出画；软件会限制到安全底边。竖屏默认底距105像素，在1080像素宽时降至51像素。

人工字幕文字与时间不变；旧预览和成品保留，新外观通过新预览与新导出生成。

## B站连接和投稿

在“设置”选择发布账号平台“B站”，点击连接账号；使用独立Edge窗口正常登录，刷新UID并确认绑定。B站与抖音使用不同浏览器配置目录，不读取日常浏览器Cookie。

第五步选择“导出并发布到B站”，选定账号和一个画幅，填写1～10个相关标签、当前网页中的分区路径、自制/转载类型；转载需要真实来源。首次使用“准备发布”：上传并填写后停在投稿页，尚未点击立即投稿。检查样片并在设置确认校准后，才可为该账号选择自动提交。

页面变化、身份不一致、登录失效、验证码、自定义封面未校准会暂停协助。提交结果不明不会自动重发；必须使用已知BVID核验。上传成功、投稿成功提示与公开发布是不同状态。

## 本机HTTP接口

沿用 `http://127.0.0.1:17656/api/v1/` 和 `X-Workflow-Token` 配对请求头。

- `POST accounts/connect`：`{"platform":"bilibili"}`。
- `GET accounts`：包含独立的 `bilibili:gpu` 或 `bilibili:cpu` 账号。
- `POST accounts/{id}/refresh`：观察身份；显式提供 `expected_creator_id` 才绑定。
- `POST publish-jobs`：使用原有不可变 `delivery`、`idempotency_key`，`publication.platform` 为 `bilibili`，另需 `copyright`（1自制/2转载）、`source`、`category_path`、`hashtags`、`account_id`、`mode`、`orientation`。
- `POST publish-jobs/{id}/verify`：`{"remote_id":"BV1TWpp6vEm2"}`，可附匹配的B站视频链接。
- 原有查询、重试、停止、样片校准接口继续使用；不确定提交禁止重试。

这是控制本机浏览器的HTTP接口，不是冒充B站开放平台官方API。真实账号页面需要登录和样片校准；本地模拟验收不等于真实平台已发布成功。

## 参考与验证边界

- [抖音用户服务协议](https://www.douyin.com/draft/douyin_agreement/douyin_agreement_user.html?id=6773906068725565448)：标题应与实际内容一致，避免夸张误导。
- [抖音创建视频文档](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-create)：官方接口字段与网页投稿标题不是同一种长度约束，不能混用其他平台规则。
- [B站社区公约](https://member.bilibili.com/studio/convention/) 与 [B站创作课程](https://activity.hdslb.com/blackboard/static/20220413/3a39c8b7178bc50b4dc5d0700df76d20/lesson3.pdf)：真实表达、标题内容一致，避免标题党。

自动标题、人工文字保护、平台标题冻结、跨平台账号隔离、幂等与回执校验有本地回归测试。`FootballPublisher/tests/bilibili_fixture.py` 用真实Edge访问本机模拟投稿页，覆盖上传控件、标签/分区/来源、准备不提交、表单变更阻断及BVID核验；不访问真实B站，不上传用户素材。
