# Agent 代码入口

| 包 | 入口 | 职责 |
| --- | --- | --- |
| [`agent.chat`](chat/) | [`run_turn`](chat/planner.py) | 角色私有消息、原生工具 ReAct 循环、预算、回复和记忆处理 |
| [`agent.planning`](planning/) | [`run_planning_action`](planning/planner.py) | 引导式行程生成、路线与排程、审核及修订 |
| [`agent.planning`](planning/) | [`generate_roadbook`](planning/implementations/roadbook.py) | 根据行程生成路书 |
| [`agent.chat`](chat/) | [`run_review_turn`](chat/review/workflow.py) | 审核入口 rewrite、原生父子委派、业务版本与统一提交 |

HTTP 聊天通过 `service/chat.py` 调用聊天包的 `run_turn`。规划 API 和后台生成任务使用规划包。两个包各自维护编排逻辑，共用 `service/travel` 的旅行领域规则。聊天与规划共用 `agent/tools` 的工具契约、注册和执行入口，MCP 通过同一目录适配。

后续 ReAct 多 Agent 的工具盘点与提示词见[系统 Prompt 草案](../docs/chat-react-system-prompts.md)。

Workflow 将审核快照直接传给父智能体，父智能体据此委派路线审核、地点搜索与编辑。编辑子智能体可以调用 `preview_review_changes` 试算。试算沿用规划图生成候选行程、路线与审核结果，并以当前已保存版本为基线。新增高严重度问题或优化未生效时，候选不通过验收。直接应用仍需调用方授权，由 Workflow 验收并保存本次通过校验的候选。

审核框架可独立调用。调用方通过 `ReviewContext` 绑定已保存的 `plan_id` 和 `plan_version`，默认使用 [三个固定子 Agent](chat/review/children.py)。测试可注入处理器。直接保存的调用方确认本轮有保存授权后设置 `save_requested` 并授予 `review:write`。

审核页通过 `POST /api/plans/{plan_id}/planning/assistant` 提交对话标识。页面每三秒检查待保存编辑，并在发起助手请求前同步待保存批次。接口返回 `choice`、`place_candidates`、`change_preview` 或 `review_issues` 内容块。交互凭据绑定行程和对话，持久化 24 小时。选择后基于最新已保存行程恢复规划，已确认的修改经试算与版本校验后直接写回，页面刷新日程和地图。公开回复单独生成，并过滤内部标识。省略对话标识时保留旧的草稿试算协议，供已有调用方使用。

用户入口调用 `rewrite_request()`，只补全指代；失败或有歧义时保留原文。没有意图分类、关键词工具路由或前置 ready 检查。父 Agent 自己决定是否需要澄清。`AgentResult` 和普通聊天 API 返回 `rewrite`，替代原 `understanding`。

父角色仅配置 `delegate_task`，子角色配置专业工具白名单，二者调用同一个 `run_turn`。委派参数是 `task_id`、`role`、`task`、`extra_data={facts, artifact_refs}`。同 task_id 延续私有消息和预算，任务修订提升版本，当前及依赖成果失效；父级只收紧凑摘要与当前成果引用。旧 JSON 任务表和 DAG 已移除。子任务契约在 `chat/schemas/tasks.py`。

每个 Agent 独立有 8 次正常工具额度。首次失败后可由模型调整参数，显式 `_retry_of=operation_id` 关联最多 3 次免费纠错；成功清除失败链。相同参数不能重试。预算或步骤耗尽后进行有界收尾，异常保留已验证证据和剩余事项。每个模型/底层工具调用独立限时，父级委派不以一个底层工具时限截断完整子循环。

候选由父角色交付 `candidate_groups`，每组包含有效 POI 和自己的选择数量。服务端按组校验并持久化，缺失分组时不签发保存动作。保存仍通过真实 POI、试算、行程版本、权限和事务检查。断点认领及保存线程使用 cancellation shield：保存前取消可释放认领，写入开始后等待事务确定结果，超时保留未知状态供核实。

澄清通过 `PendingClarification` 和 `ClarificationReply` 恢复，断点保存 `request_context`（旧 `intent` 只作历史读取兼容）。新日志使用 `request_rewrite`、`agent_start`、`tool_result`、`agent_end`，关联 Agent/父 Agent/任务版本及操作 ID。子运行不结束根运行。

## 职责分组

```text
agent/
├── chat/
│   ├── planner.py              # 对话公开入口 run_turn
│   ├── prompts.py              # 对话提示和公开回复规则
│   ├── schemas/                # 会话状态、审核输出和子任务契约
│   ├── runtime/                # agent_loop、节点、上下文、超时和记忆
│   └── review/                 # 审核 workflow、父角色和专业子角色
├── planning/
│   ├── planner.py              # 规划公开入口 run_planning_action
│   ├── schemas/                # PlanningState 与节点输出校验
│   ├── runtime/                # 图连接、节点校验、重试和运行上下文
│   └── implementations/        # 协调、候选、提案、路线、排程审核和路书
└── tools/                      # 两个 Agent 包与 MCP 共享
    ├── schemas/                # 工具定义、结果、错误和领域契约
    ├── registry.py             # 唯一 ToolRegistry
    ├── catalog.py              # 工具发现、领域元数据和 MCP 注册
    ├── dispatcher.py           # 权限、参数校验与调用结果分发
    ├── retry.py                # 正常调用额度与失败纠错预算
    ├── calls.py                # 原生调用解析与模型工具 schema
    ├── context.py              # 请求范围的工具权限及元数据
    ├── mcp.py                  # MCP 客户端传输适配
    └── implementations/        # 公开工具注册和转导出、地图结果压缩、私有审核工具绑定
```

公开工具的业务入口目前集中在 `service/mcp/server.py`，再调用 `service/travel`、`service/storage` 和 `service/integrations`。
`tools/implementations` 的四个领域 `service.py` 主要转导出这些函数，`mcp_tools.py` 绑定输入输出契约。各工具的实际定义位置见[工具索引](../docs/tool-index.md)。
规划图的节点重试由 `planning/runtime/agent_loop.py` 统一配置，工具纠错预算由
`tools/retry.py` 管理，不新增第二套重试机制。旧 Python 模块路径已迁移，调用方与
测试 patch 必须使用新路径；HTTP 与 MCP 工具名称、参数和结果契约保持原样。
