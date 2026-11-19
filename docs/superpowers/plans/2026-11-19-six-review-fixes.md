# 六项审核与工具恢复修复验收

本次在现有工作区重构之上完成三批修复。各批修改前快照在 `.uv-cache/six-fixes/batchN-before`，既有失败基线为 `.uv-cache/review-refactor-full.log`。

## 第一批

完整候选答案覆盖每个有候选的搜索任务，选数严格匹配，包括 null。来源有歧义时返回只读部分结果。成果引用带运行 UUID，修订与依赖失效继续生效。

审核回归 95 项通过，新增 HTTP 用例确认草稿与持久交互的缺组结果没有选择动作、checkpoint 或保存。独立 Reviewer 给出 pass，并独立复跑分组 6 项和委派 5 项。

## 第二批

总结恢复要求当前任务没有未解决的工具失败。已验证 POI 在拒绝或中断时保留，失败成果不发布引用。流式与非流式工具结束帧缺少调用时抛出 invalid_tool_response，禁止进入正文恢复。

新增协议与恢复测试覆盖拒绝后空总结、工具协议错误及正常搜索坏总结。测试日志为 `batch2-protocol-tests.log`。模型客户端原有 service.cli 缺失仍列入基线失败。

独立 Reviewer 给出 pass，独立执行新增协议测试 2 项及已有恢复测试 7 项。

随后补充工具超时子场景，首次搜索成功后第二次搜索连续三次 TimeoutError，Dispatcher 耗尽后模型返回空总结。断言 POI 保留、partial、artifact_ref 为 null 且诊断包含 UPSTREAM_TIMEOUT。两个测试方法及全部子场景通过，实现未变。

## 第三批

预期试算业务拒绝使用 INVALID_ARGUMENT，使同轮关联失败操作后改参可继续。外部 MCP 兼容文本和 JSON 文本，错误标记优先；内置契约仍严格校验。内置入口以完整路径识别。

新增兼容测试 4 项、工具架构 11 项和 MCP 契约 7 项通过。新增测试已添加精确 gitignore 例外。

独立 Reviewer 给出 pass，独立执行新增测试、工具架构、MCP 契约及未知写入防重放共 23 项通过。

## 联合验证

- `full.log` 为 Python 全量 428 项，414 通过、5 失败、8 错误、1 跳过。13 个失败或错误的方法名及类别与修复前基线相同。
- `node.log` 为前端 29 项通过。
- `tcp-api.log` 为实际 Uvicorn TCP 请求 3 项通过，覆盖健康检查、正常搜索试算保存、缺组只读及普通/流式部分结果。
- `browser.log` 记录公开 MCP stdio 调用和隔离 MySQL 浏览器主链路通过。
- Python 编译和 git diff --check 通过。

日志位于 `.uv-cache/six-fixes/`。浏览器与 HTTP 使用离线模型及地图替身，真实模型和高德尚未联调。候选来源有歧义时采用只读降级，模型需要重新整理候选分组。
