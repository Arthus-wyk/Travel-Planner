# Agent 核心重写执行记录

计划：`2026-10-16-agent-core-rewrite.md`。

## 范围与决定

- 用户已授权按计划重写，并要求目录/职责清晰、中文注释和后续多 Agent 接口。
- 保留当前工作分支和用户已有未提交修改，不自动提交或覆盖用户改动。改动前的源码备份为忽略目录中的 `data/agent-core-before-rewrite.zip`。
- 单一执行核心为 agent/runner、graph、nodes；HTTP 和 CLI 均调用 run_turn。旧目录、旧循环、双注册表、双调度器和确认标志均移出运行路径。
- 行程审计、推荐排序等稳定算法保留既有修改，迁移到 service/travel；存储格式和路径保留。
- MCP 连接生命周期提取为 infrastructure/mcp.py，避免应用服务和 CLI 各维护一套连接代码。
- 自动审批拒绝递归删除旧目录；改用可恢复的 Move-Item，将旧目录及旧转发/策略/重试文件放入忽略的 data/ 归档。活动代码树中不保留旧实现，未绕过删除审批执行同一递归删除命令。
- jsonschema 原已由 MCP 安装；现明确声明为直接依赖，用于唯一 Executor 的完整参数校验，没有新增框架。
- 多 Agent 只保留并使用真实契约：独立 AgentRequest/Result/Context、allowed_tools、max_rounds、memory_enabled、异步 ToolSpec handler。嵌套调用和并发隔离已有测试，没有增加空协调器或 Agent 基类。

## 完成项

1. 原有基线 57 项通过；外部契约和依赖调用者已核对。
2. 业务、存储与模型传输边界已拆分，低层不反向导入 Agent。
3. 唯一 ToolRegistry、Executor、ToolResult 已接通真实图执行路径，校验工具范围、权限及 JSON Schema。
4. 图核心重新实现，显式当前计划和本轮新消息，不再从提示词反解计划或按压缩前下标持久化。
5. HTTP/CLI 统一入口，对外脚本、HTTP 响应与 MCP 工具契约保留。
6. 活动旧实现移除，中文注释、README、导读和架构边界测试更新。

## 审查与修复

- 独立只读审查发现：新旅行可能缺少 plan 工具、MCP/AnyIO 包装异常可能绕过保存、同名工具成功误清除其他失败。均先观察复现测试失败，再修复并验证通过。
- MCP 的异常保存在连接上下文内部完成，再向上传播；退出时包装异常不会遗漏部分结果。
- 失败按 operation_id 记录；参数修正使用模型侧 _retry_of，执行前移除并验证引用；仅显式重试 ID 可以解除失败，相同参数的新操作不能隐式解除另一项失败。
- 补充未知意图只读回退、自然语言行程修改、重试关联优先级、相同参数独立操作、空模型输出及空 schema 参数对象校验回归。
- MCP 原有 24 项工具的名称和输入 schema 与改动前备份逐项比较一致；固定快照随测试提交。

## 验证

- `uv run --locked python -m unittest discover -s tests`：58 项通过，26.014 秒。旧双执行器的重复用例已整合，不以测试条目数作为覆盖率。
- 针对性验证：部分结果保存、压缩后回复入库、重试优先级、并发状态隔离、专业 Agent 嵌套、记忆 JSONL/Markdown 格式、权限与工具范围、24 项 MCP 契约。
- `uv run --locked python api_server.py --help` 和 `uv run --locked python client.py --help`：通过。
- Ruff 格式检查及 F 类静态检查：通过；`git diff --check`：通过。
- 未调用真实模型、MySQL 或地图；未实现审批、检查点、总委派预算、用户隔离或生产级多 Agent 调度。

## 已知边界

中期记忆仍是单用户全局文件，尚不按 plan_id 过滤。当前执行快照只用于进程内异常产物保留，不支持重启续跑。工具写入不自动重试；真正的恢复和多 Agent 写入协调仍需持久化幂等设计。
