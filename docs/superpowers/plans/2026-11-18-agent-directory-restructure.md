# Agent directory restructure Implementation Plan

**Goal:** 按用户示例重组两个 Agent 包，共享工具统一迁入 agent/tools。
**Architecture:** chat/runtime 保存通用循环，chat/review 保存审核编排；planning/planner.py 为入口，runtime 为图编排，implementations 为节点执行。共享工具 schemas、registry、dispatcher、implementations 分离，MCP 继续复用。
**Constraints:** 保留当前未提交修改与业务行为；不增加依赖、空框架或兼容转发模块。内部 Python 导入路径统一迁移。

- [x] 保存工作区基线并运行现有 unittest。
- [x] 迁移共享工具、聊天与规划模块；同步所有导入、动态模块名和测试 patch 路径。
- [x] 更新目录文档；运行 unittest、Node 测试和浏览器主链路（凭证缺失须如实记录）。
- [x] 向新上下文只读 Reviewer 提交基线、迁移映射、任务 Diff 和验证证据。

## 验证记录

- 定向 unittest：125 项通过（`focused-tests.log`）。
- 全量 unittest：405 项，6 failures、8 errors、1 skipped；14 个失败方法均在初始源码快照复现（`final-tests.log`、`baseline-failures.log`、`failure-comparison.txt`）。
- Node 原生测试：29/29 通过（`node-tests.log`）。
- 26 个 MCP 工具名称、描述、输入和输出 Schema 与任务基线完全相同（`mcp-equivalence.txt`）。
- 24 个规划函数 AST 与基线一致，仅共享地图配置改为访问 `service.config`（`function-equivalence.txt`）。
- compileall 与 git diff --check 通过。
- 浏览器主链路运行器已执行，因缺少本地独立 MySQL 的 TRAVEL_DB_* 参数而未启动；未运行真实模型/高德/数据库 smoke。
- 新上下文只读 Reviewer：pass，无本次迁移阻断问题。自动修复轮次 0。

证据保存在 `.uv-cache/agent-restructure/`。旧 Python 导入路径需要迁移；仓库调用方与测试已同步。
