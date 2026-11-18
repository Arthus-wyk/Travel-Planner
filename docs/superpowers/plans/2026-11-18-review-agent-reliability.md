# Review Agent Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 独立工具预算、模型参与的三次纠错、部分成果保留及严格结构化输出。

**Architecture:** 保留 Workflow、Parent、Scheduler 和专业子 Agent 分层。预算与失败链归属每次 Agent 执行，工具证据由应用收集，OpenAI response_format 限制最终决策。

**Tech Stack:** Python asyncio、jsonschema、httpx、unittest。

**Spec:** docs/superpowers/specs/2026-11-18-review-agent-reliability-design.md

## Global Constraints

- 正常工具预算 8 次，纠错重试最多 3 次且不扣正常额度，成功重置失败链。
- 每次失败必须交还模型决策，禁止自动原样重试。
- 保留现有版本、授权、试算和原生工具调用契约。

## Review Focus

- 两个子任务并发拥有独立预算。
- 第八次正常调用失败后仍能完成三次纠错。
- 超时、解析失败和耗尽保留验证结果。
- 父 Agent 无法通过修改任务绕过委派额度。
- 部分结果不能成为未经试算的编辑提案。

## Tasks

- [x] 先添加预算、错误反馈、部分结果及结构化输出回归测试并观察失败。
- [x] 实现独立预算、失败链和证据保留，调整 Workflow 与子循环。
- [x] 实现父委派预算、部分结果汇总与失败恢复。
- [x] 接入严格 Schema 和传输层解析，压缩审核工具结果并取消隐藏重试。
- [x] 运行审核、HTTP、模型客户端测试和静态检查，审查改动与剩余稳定性问题。

## Execution ledger

- Ruling: 在现有工作区原地实施，保留未提交代码且不创建提交。审核模块当前为未跟踪文件，干净 worktree 无法包含待修改的实际实现。
- Ruling: 重试按同一工具的活动失败链识别。成功后清零，三次重试后再次调用该工具消耗一次正常额度并开启新操作，不能无限免费重试。
- Ruling: 父 Agent 的调用为子任务委派，按真实执行次数计数。复用任务不扣额度，失败任务重跑使用同样的三次纠错规则。
- Verification: 审核与恢复 37 项测试通过，模型客户端 14 项通过。原有 test_cli_model_selection_ignores_environment 导入不存在的 service.cli 失败，单独记录并排除，无修改其断言。
- Verification: Python compileall 与 git diff --check 通过，前端 npm run build 通过。Ruff 未安装，未执行 lint。真实模型、高德接口和浏览器交互未验收。
- Environment: 受限环境的 Windows asyncio socketpair 初始化阻塞，经允许回环通信的执行权限运行模拟测试后通过。
- Review: 独立审查发现最终试算失败、模型上下文错误和公开答复错误会丢失成果。均已加入回归覆盖并修复。
