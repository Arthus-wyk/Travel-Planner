> 历史记录：当前实现已由 [Agent 核心重写计划](2026-10-16-agent-core-rewrite.md) 替代；本文保留用于追溯。

# 迁移执行记录

计划：docs/superpowers/plans/2026-10-16-agent-migration.md，仅执行阶段 1–3。

- 基线：21 项测试通过。新增离线场景后 33 项通过。
- 阶段 2：LangGraph 1.2.15 支持 Python 3.10；单节点接入后 43 项通过。
- 决定：在现有 codex/langgraph-stages-1-3 分支工作，保留已有未提交变更，不自动提交。
- 决定：新旧运行方式共享单步实现，避免维护两份模型/工具循环。
- 决定：单节点图为中间验证版本；最终提交保留拆分后的节点图。
- 决定：图状态仅存数据，Ollama/MCP/日志留在本次请求的运行对象内。
- 决定：审批和恢复属于阶段 4–5，本轮不接通，也不宣称已经支持。
- 阶段 3：进行中，先补 run_turn、领域筛选、多工具顺序和图步数回归。
- 阶段 3：完成。共享 QueryRuntime，六个图节点，HTTP 默认 graph，可显式切回 legacy，CLI 保持顺序调度。
- 决定：工具循环回归合并到 test_agent_graph.py，避免创建重复测试文件；tool_router 直接复用，不改用户已有变更。
- 修复：异常后的消息回写回归，先观察新旧执行器测试失败，再使用 finally 和逐节点状态快照修复，两项针对性测试通过。
- 最终验证：uv run python -m unittest discover -s tests → 57 项通过（62.227 秒）；过程中有 unittest asyncio 调试模式的慢回调提示，没有失败。
- 启动检查：uv run --locked python api_server.py --help、uv run --locked python client.py --help → 均通过；git diff --check → 通过。
- 未验证：真实 Ollama/MySQL/Google Maps 联调；审批、持久化恢复和多 Agent 不在本轮范围。
- 独立审查：异常历史回写修复后，未发现其他可证实的新增问题。
- 既存边界：API 在压缩历史后仍使用压缩前的 turn_start 截取待保存消息，长会话中可能漏存本轮回复；HEAD 已存在，本轮未扩大到修复该问题。
