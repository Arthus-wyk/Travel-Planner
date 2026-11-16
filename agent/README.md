# Agent 代码入口

| 包 | 入口 | 职责 |
| --- | --- | --- |
| [`agent.chat`](chat/) | [`run_turn`](chat/runner.py) | 聊天单轮、意图提取、工具选择与调用、回复和记忆处理 |
| [`agent.planning`](planning/) | [`run_planning_action`](planning/planning_graph.py) | 引导式行程生成、路线与排程、审核及修订 |
| [`agent.planning`](planning/) | [`generate_roadbook`](planning/roadbook.py) | 根据行程生成路书 |

HTTP 聊天通过 `service/chat.py` 调用聊天包的 `run_turn`。规划 API 和后台生成任务使用规划包。两个包各自维护编排逻辑，共用 `service/travel` 的旅行领域规则。聊天的 MCP 工具契约仍在 `tools` 与 `tooling`。

后续 ReAct 多 Agent 的工具盘点与提示词见[系统 Prompt 草案](../docs/chat-react-system-prompts.md)。
