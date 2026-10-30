# Agent 核心重写计划（设计草案）

**Goal:** 让 `agent/` 成为唯一 Agent 实现，删除 `service/agent/` 和重复执行路径，使 HTTP、CLI 调用同一个 LangGraph 核心。

**Architecture:** 重写编排、状态、结果与工具执行边界；旅行数据、地图服务和现有存储格式保持稳定。保留用户可见行为及外部协议，不以保留旧类、旧内部导入和旧控制流作为目标。

**Tech Stack:** 现有 Python 3.10+、LangGraph、Ollama SDK、MCP、MySQL，不新增框架。

**Spec:** 本文为已执行的设计与计划；实施结果、调整和验证见同目录执行记录。

## 1. 目标与范围

- 成功标准是“一套核心、一条工具执行链、清晰的输入输出”，而不是把原文件换个目录。
- HTTP 和 CLI 都调用 `agent.runner.run_turn`，没有 `GraphQueryEngine(QueryEngine)` 继承关系，也没有第二套 while 执行器。
- 对外保持 HTTP 路由及响应结构、MCP 工具名称及参数、现有命令行启动方式；内部 Python 导入路径允许改变。
- 继续使用现有 MySQL、Markdown、JSONL 和地图实现，不迁移已有数据。
- 延续中文注释要求：解释职责、状态变化、失败边界和设计原因，不用注释复述每行代码。
- 本次不实现多 Agent、审批界面、检查点、多用户或新的认证系统；不为这些功能预先建立空基类和插件体系。

## 2. 当前混乱的根因（已核对源码）

| 现状 | 具体问题 | 重写决定 |
|---|---|---|
| `agent/graph.py` 从 `service/agent/client.py` 导入状态、运行时并继承执行器 | 新图仍由旧大文件定义核心职责 | 图节点直接使用新的状态和依赖，不再继承旧引擎 |
| `agent/loop.py` 只是重新导出旧引擎 | 看起来有新入口，实际没有独立职责 | 删除 |
| `agent/router.py` 与 `service/agent/tool_router.py` 并存 | 类路由器和函数路由器不是同一条主链路 | 只保留注册表驱动的路由实现 |
| `ToolRegistry`、`MCPToolRegistry` 和同名兼容别名并存 | 元数据、执行和策略可能各走一套 | 只保留一个 ToolRegistry；MCP 只是 handler 的来源 |
| `agent/executor.py`、`agent/policy.py` 主要被测试调用，主链路直接走 MCP 适配器 | 测试通过不代表策略约束了实际操作 | 新主链路只经一个真实 Executor；未启用审批骨架不继续伪装为权限门禁 |
| 行程审计、推荐排序、偏好文件读写放在 `service/agent/` | 业务服务和存储反向依赖 Agent 包 | 迁到旅行服务和存储层 |
| API 直接处理意图、记忆、日志、执行器与消息保存 | 接口层承担大量编排规则 | 提取应用服务，API 保留 HTTP 适配 |
| 意图模块生成 TaskPlan，图又有一套控制流 | 容易同时存在两个“谁决定下一步”的来源 | 意图只解析需求；图控制步骤；模型决定具体工具 |
| 模型重试辅助文件还包含 MCP 结果转换和重复轮数常量 | 模型传输和 Agent 决策边界不清 | 模型接口只处理模型请求，业务轮数只在 Agent 核心定义 |

## 3. 推荐方案

选择“在新边界下重写核心，按用例切换后删除旧实现”。

- 不选择继续拆分 QueryRuntime：仍容易保留旧依赖方向和双入口。
- 不选择一次重写旅行业务、数据库和前端：风险和本次目标不相称。
- 稳定的算法和校验可以保留经过测试的实现，但必须有唯一归属；混乱的编排重新设计，不机械搬运整个旧类。

### 3.1 目标依赖方向

```text
HTTP API ─→ service/chat.py ─┐
                           ├─→ agent/runner.py → agent/graph.py → agent/nodes.py
CLI ───────────────────────┘                         │
                                     模型适配 / 唯一工具执行器 / 记忆规则
                                                     │
                                       MCP / 旅行服务 / 存储
```

禁止 `agent/` 依赖 HTTP API 或 CLI；禁止 MCP、旅行服务、存储层反向导入 Agent 编排。`graph.py` 不创建网络连接、不读文件、不写数据库。

### 3.2 文件职责

以下是职责规划；实现中不能只增加这些文件而保留原有重复核心。

| 目标位置 | 唯一职责 |
|---|---|
| `agent/runner.py` | 唯一执行入口；调用图、组装 AgentResult、在异常时保留已完成步骤 |
| `agent/state.py` | 定义 AgentRequest、AgentState、AgentResult；只含数据 |
| `agent/context.py` | 定义本轮依赖 AgentContext：模型、工具注册表、日志和存储资源；不进入检查点状态 |
| `agent/graph.py` | 节点注册、条件边和结束条件，不再调用 QueryRuntime |
| `agent/nodes.py` | 真实节点逻辑；只组织本节点的工作，不承载整套循环 |
| `agent/intent.py` | 从明确上下文解析目标、领域、槽位和待澄清信息；不再从系统提示 JSON 反向恢复已经拥有的当前计划 |
| `agent/prompts.py` | 系统提示、当前计划上下文和执行护栏文案 |
| `agent/memory.py` | 记忆召回范围、提取和写入条件；文件 IO 交给存储 |
| `agent/router.py` | 从唯一 ToolRegistry 选择当前工具；无第二套工具名单 |
| `agent/executor.py` | 真实执行前检查、调用唯一 handler、返回规范结果；节点与任何内部调用都经过这里 |
| `agent/compress.py` | 对话压缩规则，不混入模型网络重试策略 |
| `tooling/registry.py`、`tooling/spec.py` | 唯一 ToolRegistry、ToolSpec 定义 |
| `tooling/mcp.py` | 把 MCP 工具定义转换成 ToolSpec/handler，不再维护第二个 Registry 或业务策略 |
| 新增 `tooling/results.py` | ToolResult 规范化，区分传输错误、协议错误和业务失败；只在边界解析一次 |
| 新增 `infrastructure/llm.py` | Ollama 请求、响应规范化、网络重试、流式降级及遥测；上下文压缩回调由 Agent 提供 |
| 新增 `service/chat.py` | HTTP 聊天用例：读取会话/计划、调用核心、保存本轮产物、返回原响应结构 |
| 新增 `service/cli.py` | 参数解析、输入输出、日志会话和 CLI 消息维护，复用同一个 runner |
| 新增 `service/travel/` | 行程审计、推荐排序、旅行工具结果的领域校验 |
| 新增 `service/storage/memory_store.py` | 原 Markdown 偏好及 JSONL 中期记忆的读写，不改变路径和格式 |

`agent/policy.py` 当前未接入的审批骨架不作为已完成安全能力保留。执行器必须保留实际生效的白名单、参数校验及已有权限约束；真正的审批暂停/批准/拒绝闭环以后单独实现，不接受客户端随意传入 `confirmed=true`。如果仍有实际调用者依赖旧策略，先迁移并验证等价约束，再删除旧模块。

主要旧文件的明确去向：

| 旧文件 | 目标 |
|---|---|
| `service/agent/client.py` | 编排重写为 agent/runner、graph、nodes；提示进入 agent/prompts；CLI 进入 service/cli，原文件删除 |
| `service/agent/intent_understanding.py` | 重写为 agent/intent，精简 TaskPlan 与提示词反向解析 |
| `service/agent/tool_router.py` | 唯一实现进入 agent/router，工具说明元数据归 tools/catalog |
| `service/agent/tool_verifier.py` | 通用解析归 tooling/results，旅行规则归 service/travel/tool_verification.py，原文件删除 |
| `service/agent/compress.py` | 规则归 agent/compress，模型重试不再反向导入 |
| `service/agent/memory.py` | 召回与提取规则归 agent/memory，文件 IO 归 service/storage/memory_store |
| `service/agent/preference_profile.py` | service/storage/memory_store，保留原 Markdown 路径及格式 |
| `service/agent/itinerary_audit.py` | service/travel/itinerary_audit.py |
| `service/agent/recommendation_ranker.py` | service/travel/recommendation_ranker.py |
| `service/agent/tools.py`、`agent/loop.py` | 内部转发文件，迁移调用者后删除 |

### 3.3 输入、状态、输出契约

公开入口规划为：`async run_turn(request: AgentRequest, context: AgentContext) -> AgentResult`。

- `AgentRequest`：原始输入、既有历史、明确的当前计划；可信的身份和权限来自服务端 AgentContext，不能由用户输入覆盖。核心不靠扫描提示词猜测应用层已经知道的 plan_id。
- `AgentState`：模型工作消息、理解结果、开放工具、模型轮数、待执行调用及索引、校验信息、终止原因、本轮输出。
- `AgentResult`：最终答复、理解摘要、工具事件、本轮公开新消息、工具返回的最新计划和结束状态。API 的导航动作由应用服务根据实际计划构造。
- 普通模型/工具失败返回 failed 结果；意外异常继续传播并携带 partial_result，应用服务可保存已完成产物。取消保持取消语义并记录已完成调用，不自动续跑图。
- `history` 和模型的工作上下文分开；压缩只改变后者。持久化使用明确的 `new_messages`，不再用压缩前的列表下标截取，解决已知漏存回复问题。
- 业务轮数上限仍为 8；网络重试次数属于模型适配层。错误和轮数耗尽必须有明确终止原因，不能包装成业务完成。

### 3.4 工具和结果只有一条链

```text
MCP list_tools + tools/catalog 的受信元数据
    → ToolSpec → ToolRegistry → Router → Executor
    → MCP handler → ToolResult → 领域校验 → 模型上下文
```

- 工具名称、schema、领域、风险从这条链产生；`tools/catalog.py` 汇总各工具模块现有声明，避免另写一份名字列表。
- MCP 返回的 `isError`、文本错误和业务 `success=false` 都必须保留为失败。
- 不再把 JSON 包成字符串再包 JSON，并在多个模块重复解包；旧 HTTP/MCP 输出格式在对应边界做一次兼容转换。
- `verify_last_tool_result` 的校验消息仅是“校验动作已完成”，业务通过与否必须读取校验结论，不能误用外层 success。
- 对外仍保留已有 `verify_last_tool_result` MCP 工具，由共享校验函数实现，不能为了删除 Agent 目录破坏 MCP 客户端。
- 审计等旅行专用判断放在 `service/travel/`；通用结果规范化不写死旅行工具名。

### 3.5 保留能力与有意改变

保留：已有计划优先、相对天数安排、失败后的参数修正、工具顺序、8 轮限制、空承诺护栏、重试与压缩、记忆策略、日志遥测、已有外部启动方式。

改变：HTTP/CLI 统一图调度；内部不再保留 TaskPlan 与图的双重执行权，不再靠 `_internal` 和任意历史切片猜测本轮产物。HTTP `understanding` 既有字段需要兼容时，由边界映射输出，不让兼容字段反过来控制核心。

暂不改变：业务工具功能、预算规则、地图调用、存储格式、数据归属。全局记忆仍是单用户假设，不能因此宣称已支持多用户。

## 4. 从简到难的实施顺序

各步骤已实施；测试、调整和自动审批拦截的处理见执行记录。

### 第一步：固化外部契约和删除清单

**范围：** `tests/`、API/CLI/MCP 入口、依赖扫描。

- [x] 重新运行现有测试并记录当时基线，不能把上次 57 项结果当成新的验证。
- [x] 补真实输出契约用例：已有计划更新、模型 API 失败、工具失败、压缩后回复入库、无效工具/参数。
- [x] 建立旧模块 → 新归属 → 调用者列表，覆盖测试、console script、MCP 业务模块和模型重试辅助模块。

**验收：** 基线是用户可见行为和副作用，测试不再要求旧 QueryEngine 或内部提示词格式必须存在。

### 第二步：先解除业务和存储对 Agent 的反向依赖

**范围：** `service/agent/itinerary_audit.py`、`recommendation_ranker.py`、`preference_profile.py`、`memory.py` 的 IO；`service/mcp/server.py` 及相关测试。

- [x] 审计/排序迁入 `service/travel/`，偏好/中期记忆 IO 迁入 `service/storage/memory_store.py`。
- [x] 迁移所有调用者，保留预算、日期校验和文件更新错误处理。
- [x] 保持旧数据路径及格式不变，确认只读测试不会创建真实用户画像。

**验收：** MCP 业务模块不导入 Agent 包；业务和存储相关测试保持通过。

### 第三步：统一模型和工具边界

**范围：** `tooling/registry.py`、`tooling/spec.py`、`tooling/mcp.py`、新增 `tooling/results.py`、`agent/executor.py`、`agent/router.py`、`tools/catalog.py`、新增 `infrastructure/llm.py`。

- [x] 将 MCP definition/handler 适配进唯一 ToolRegistry，接通真实 Executor；删除同名 Registry 别名。
- [x] 同一执行入口验证开放工具和 JSON Schema 参数，包含类型、必填、范围等约束；复用已安装校验库，MCP 服务端校验仍保留。
- [x] ToolResult 规范化只做一次，分别覆盖协议失败、传输失败和业务失败。
- [x] 从 `withRetry.py` 提炼模型适配；删除无调用者的 MCP 转换、重复轮数和死代码，压缩规则通过回调传入。
- [x] 验证退避、备用模型、认证恢复、流式降级、遥测和写操作不自动重试。

**验收：** 未开放工具和非法参数到不了 handler；MCP 外层成功、内层失败不能被当成业务成功。

### 第四步：重新实现纯粹的图核心

**范围：** `agent/state.py`、`context.py`、`runner.py`、`graph.py`、`nodes.py`、`intent.py`、`prompts.py`、`memory.py`、`compress.py`。

- [x] 先实现 AgentRequest/State/Result 和唯一 run_turn 契约，明确历史、工作上下文与本轮输出的区别。
- [x] 在新边界下写意图、模型、工具、校验和结束节点；不复制 QueryRuntime 或保留 QueryEngine 继承。
- [x] 意图解析接收显式当前计划，图拥有流程控制，模型拥有具体工具选择；任务建议不再作为第二套控制器。
- [x] 多工具逐项执行和校验，错误后允许模型修正，轮数耗尽明确结束。
- [x] 进程内异常保留已完成的工具上下文；不伪装为跨进程恢复。每个请求独立依赖和状态，不缓存 MCP 会话到全局图。
- [x] 用假模型、假 MCP 覆盖正常/失败/取消/压缩/空输出/空承诺/多工具顺序；在入口行为上比较，而非让新测试继承旧引擎测试类。

**验收：** 新核心不导入 `service.agent`，不依赖 HTTP、CLI 或 QueryRuntime。

### 第五步：HTTP 与 CLI 原子切换

**范围：** 新增 `service/chat.py`、`service/cli.py`；修改 `service/api/server.py`、`client.py`、`main.py`、`pyproject.toml`、`uv.lock` 和入口测试。

- [x] 应用服务从数据库读计划与公开历史，使用统一结果保存公开新消息，API 不再组装内部模型消息。
- [x] CLI 使用同一个 runner；保留 exit/quit、/usage、--model、--server、--session-id 行为及不自动恢复历史的现有说明。
- [x] 迁移记忆召回/写回与日志，确保每轮各发生一次，显式不参考历史时不写入旧记忆策略禁止的数据。
- [x] 核对 HTTP 响应、工具事件、导航、错误处理；CLI/HTTP 同一脚本输入产生相同业务操作。
- [x] 核对所有现有 MCP 工具名称和 schema，避免业务归属迁移改变对外服务。

**验收：** HTTP 和 CLI 实际调用同一套图；压缩后仍保存本轮最终回复，写入与导航不重复。

### 第六步：删除旧核心并收尾

**范围：** 删除 `service/agent/`、`agent/loop.py`、重复引擎/注册表/路由、旧策略骨架和 `service/utils/withRetry.py`（其调用者全部迁移后）；更新测试及文档。

- [x] 删除 QueryEngine、QueryRuntime、GraphQueryEngine 旧实现及 TRAVEL_AGENT_RUNTIME 双执行器配置。
- [x] 不留下 `from 新模块 import 旧名字` 的内部兼容空壳；仓库内调用方同步改到新路径。
- [x] 保留外部脚本入口和协议，更新中文导读；原计划标记为被本计划替代，保留历史记录。
- [x] 通过全量离线测试、API/CLI --help、MCP 工具清单/参数契约、未解析导入检查。
- [x] 用一项架构约束检查确保 `service/agent/` 已删除且不存在旧导入；确认业务/存储未反向依赖 Agent。

**验收：** 一套入口、一套状态、一套注册表、一套执行器、一张图；没有靠保留旧目录才通过的测试。

## 5. 审查重点和限制

1. 压缩导致列表长度变化：持久化只认 new_messages，不认旧下标。
2. MCP isError 与业务失败：不能外层成功就声称完成。
3. 写入后异常/取消：保留已完成调用记录，不自动重新执行写入。
4. 当前计划与历史冲突：显式当前计划和本轮要求优先，不能错误另建计划。
5. 生命周期：MCP 连接在每次调用期间有效，不在两次请求或 CLI/API 之间共享。

重写阶段的“回退”使用变更前可恢复的 Git 状态，不长期维护第二套执行器。当前工作区含已有未提交修改，实际实施前先明确并保存该状态，不能清理或覆盖用户改动。

真实模型、MySQL 和地图联调与离线验证分开记录，未运行的项目不能写成已验收。审批、检查点、用户隔离和多 Agent 必须在核心重写稳定后单独推进。
