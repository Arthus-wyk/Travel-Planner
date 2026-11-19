# 统一 Pydantic 工具契约与 MCP 调用实施计划

> 实施时按任务顺序推进。每项先用现有 unittest 写出验收断言，再实现并运行对应回归。完成后按 `REVIEW.md` 交给新上下文中的只读 Reviewer。

**目标**是让所有供 Agent 调用的工具通过 MCP 暴露，从同一个注册表生成 function call 定义，用统一结果协议回传，并分开程序重试与模型纠错。

**设计依据**是本轮已确定的每工具独立输入和业务输出、共享泛型外层和错误模型，以及三种结果状态与两层恢复。沿用 Python 3.10+、Pydantic v2、仓库锁定的 MCP SDK、现有 LangGraph 循环和 unittest。

## 范围与验收契约

- 覆盖现有 26 个公开 MCP 工具、7 个审核工具和 `delegate_task`。公开 MCP 保留工具名和既有参数语义，审核与委派工具只出现在对应请求的私有 MCP 会话。固定规划节点、HTTP 使用的领域函数和地图集成继续沿用当前调用方式。
- `ToolResult[T]` 序列化为 `status`、`data`、`error`。按状态校验字段组合，并让 MCP `outputSchema` 表达这三种组合。每个工具的 `T` 对应自己的业务输出模型。只有确实能返回部分结果的工具才定义可保留的数据和缺失项，成功状态仍校验完整数据。

| `status` | `data` | `error` | MCP `isError` |
| --- | --- | --- | --- |
| `success` | 完整且已验证的业务数据 | `null` | `false` |
| `partial` | 有效业务数据，标明未完成项 | `PARTIAL_FAILURE` | `false` |
| `error` | `null` | 共享错误对象 | `true` |

共享 `ToolError` 包含 `code`、面向模型的安全 `message`、`retryable`，以及按需提供的 `retry_after_seconds` 和结构化 `details`。`retryable` 表示能否原样重放该次操作；程序预算耗尽后回传 `retryable=false` 并提供耗尽原因，模型仍可改参数或换工具。行程版本冲突、试算被拒绝等业务分支通过稳定的 `details.reason` 保留。

| 错误码 | Dispatcher | Agent |
| --- | --- | --- |
| `INVALID_ARGUMENT` | 返回参数路径和可用提示 | 修正参数后再调用 |
| `UPSTREAM_TIMEOUT` | 对可安全重放的调用有限重试 | 预算耗尽则改用其他方案或说明限制 |
| `RATE_LIMITED` | 在总时限内遵循上游等待时间 | 超出本轮预算则推迟或换工具 |
| `NOT_FOUND` | 返回明确缺失的资源 | 换搜索目标或告知用户 |
| `PERMISSION_DENIED` | 停止执行 | 提示所需授权 |
| `INTERNAL_ERROR` | 记录可定位的错误；仅对已分类的临时故障重试 | 尝试备选方案，必要时说明限制 |
| `PARTIAL_FAILURE` | 保留已校验的数据 | 使用有效结果并补齐缺失部分 |

## 1. 建立类型协议和注册表

- [x] 在 `agent/tools/schemas/{inputs,outputs,errors}.py` 定义共享工具签名、Pydantic 泛型结果与七类错误。使用三态约束拒绝无数据的 `success/partial`、无错误的 `error` 和其他非法组合。选择一个无副作用地图工具打通第一个契约。
- [x] 在 `agent/tools/registry.py` 让 `ToolSpec` 注册 `input_model`、`output_model`、处理器、描述、角色权限和是否可安全重放。模型 function call 从注册表的输入模型生成，MCP 列表使用同一输入与泛型输出。保持 `_retry_of` 仅用于模型侧失败关联，进入业务参数前剥离。
- [x] 在 `pyproject.toml` 显式声明 Pydantic v2，更新 `uv.lock` 并检查差异。用 `tests/test_tooling_architecture.py` 验证重复注册失败、参数严格校验、三种结果的序列化和注册表生成的 Schema。

## 2. 统一普通工具的 MCP 执行路径

- [x] 更新 `agent/tools/{catalog,mcp}.py`、`mcp_server.py`、`infrastructure/mcp.py`。由 MCP 适配器将已注册的工具列入 `tools/list`，使用 SDK 的低层 `tools/call` 处理器返回 `CallToolResult`，同时填写 `structuredContent` 和 `isError`。当关闭 SDK 的默认输入校验以统一错误响应时，适配器必须先用已注册的 Pydantic 输入模型校验，再执行处理器。直接返回 `CallToolResult` 时 SDK 会跳过输出校验，适配器必须在序列化前自行校验。客户端还需验证收到的结构化结果，传输异常或畸形响应进入结构化错误路径。
- [x] 在 `agent/tools/schemas/{plan,itinerary,maps,recommendation,memory}.py` 按工具定义独立输入和业务输出模型。共享地点、路线等领域类型可以复用。迁移对应的 `implementations/*/mcp_tools.py` 定义，由注册表向 MCP 暴露。旧业务字典只在工具边界逐个转换，移除依赖文本前缀猜测结果的逻辑。
- [x] 把首个地图工具的 MCP 往返测试扩展到全部 26 个公开工具的注册契约。更新 `tests/test_mcp_contract.py` 和输入快照前逐项比较名称、参数、默认值、空值和必填约束。使用真实 MCP 客户端会话验证 `tools/list` 与 `tools/call`，在业务集成处使用替身。

## 3. 把程序级恢复集中到 Dispatcher

- [x] 在 `agent/tools/{dispatcher,retry}.py` 将程序重试作为一次逻辑工具调用内的多次 MCP 请求。初始策略最多 3 次尝试（含首次），退避 0.5 秒、1.5 秒，限流等待取退避与 `retry_after_seconds` 的较大值；等待或下次调用超出剩余总时限时立即交付结构化失败。沿用调用方的时限，取消向上传播。
- [x] 用异常类型、HTTP 状态与上游错误字段分类临时故障。梳理 `service/integrations/travel_amap.py` 的重试调用点，确保同一 Agent 工具调用只有 Dispatcher 重试。直接调用地图集成的固定节点与 HTTP 路径继续使用其现有策略。读取可按工具标记重放；写入、删除和委派默认禁止自动重放，避免在响应丢失时再次执行副作用。
- [x] 用可控时钟和 MCP 替身验证超时后成功、429 等待、等待超出预算、永久错误、取消和写操作响应丢失。断言程序重试不消耗额外的模型轮次，也不再发两条工具结果。

## 4. 将审核与委派迁到请求私有 MCP 会话

- [x] 在 `agent/tools/schemas/{review,delegation}.py` 和对应的 `implementations/` 迁移 8 个工具。通过 SDK 的进程内 MCP 会话传递 `tools/list` 与 `tools/call`。由服务端闭包绑定当前行程、版本、草稿、角色、权限和委派状态。复用现有 MCP 适配器与 Dispatcher，移除绕过 MCP 的模型可调用 handler。
- [x] 改造 `agent/chat/review/{workflow,parent,children}.py`。父角色委派会话和子角色业务工具会话分开，允许委派处理器等待子循环完成。审核写权限只能由当前已授权的保存流程授予，继续保留版本检查、幂等提交和取消屏蔽。公开 `mcp_server.py` 不暴露这些请求私有工具。
- [x] 用现有审核工作流、子角色、原生委派、取消和 HTTP 回归验证。新增边界断言，覆盖两次并发审核之间无法读取或写入对方行程、模型参数无法提升权限、结果未知时不会重复提交。用正式入口跑一条委派到子角色再调用 MCP 工具的链路。

## 5. 迁移模型回传、验证与提示词

- [x] 更新 `agent/chat/runtime/nodes.py`、`agent/chat/schemas/state.py`、`service/travel/tool_verification.py` 及审核结果消费者。仅把已验证的业务数据送入现有证据链。对 `partial` 保留有效证据并记录缺失项，不能据此声称整项完成。用结构化原因继续派发版本冲突和拒绝保存的业务响应。公开 HTTP 数据和历史记录如需兼容，在 `service/chat.py` 边界转换。
- [x] 使用 `role="tool"`、原工具名及原 `tool_call_id` 回传结果。检查 `service/utils/model_client.py` 对旧消息的兼容路径，确保本轮合法结果保持 `tool` 角色。模型级 `_retry_of` 继续关联失败操作、限制同参重放与纠错次数，程序级重试完成后才进入模型恢复。
- [x] 在 `agent/chat/prompts.py` 与审核父、子角色系统提示中复用一套 Tool Error Handling Rules。覆盖三态、全部七个错误码和恢复耗尽，要求用可信结果、不得编造、失败时说明局限。更新 `docs/tool-output-contracts.md`，记录外部 MCP 客户端从 `success/error_type` 迁移到 `status/error` 的方式。
- [x] 运行 ReAct、模型消息、普通聊天与审核回归。验证并行同名工具按 ID 各收一条结果，`partial` 可继续使用，参数错误进入模型改参，权限错误停止调用，耗尽时向用户解释限制。

## 6. 集成验证与交付

- [x] 实施前记录当前工作区差异和测试基线。结束后运行 `uv run python -m unittest discover -s tests -v` 和 `node --test frontend/tests/*.test.js frontend/src/reviewDraft.test.js`，按方法核对既有失败与本次新增失败。
- [x] 按 `.agents/skills/verify-travel-planner/SKILL.md` 执行浏览器主链路，额外验证审核助手的 MCP 调用到授权保存和刷新后的结果。确认本地隔离 MySQL 与 Playwright 配置。评估真实模型、高德与数据库 smoke，缺少条件时记录未验证项。
- [x] 按 `REVIEW.md` 提交仅本任务的 Diff、基线和验证证据给新上下文 Reviewer。重点核对协议三态、同一注册源、重放安全、请求隔离和工具消息 ID 对齐。

## 交付验证

- 34 个公开及私有工具均有独立的 Pydantic 输入和业务输出模型。
- 全量 Python 回归 419 项，405 项通过，5 项失败、8 项错误和 1 项跳过。13 个红测在任务开始的源码快照以相同失败类型复现。候选缺口规则调整后，64 项受影响的审核、委派与恢复测试全部通过。
- Node 原生测试 29 项通过。Playwright 使用独立 MySQL，覆盖新建行程、攻略生成、手动保存、路线助手试算和确认保存、刷新核对及行程单。公开 MCP stdio 工具调用通过。
- 依赖锁、Python 编译、浏览器测试脚本语法和差异检查通过。独立只读 Reviewer 在第 2 轮修复后给出 `pass`。
- 浏览器使用离线模型与地图替身，真实模型和高德未联调。人为设置 10 毫秒时限时 SDK 记录过一次取消警告，该用例通过且无写入。处理器启动前的极短取消竞态未单独覆盖。

验证证据位于 `.uv-cache/typed-tools/`，主要记录为 `final-python.log`、`baseline-failures.log`、`last-review-regression.log`、`node-tests.log` 及 `final-browser.log`。
