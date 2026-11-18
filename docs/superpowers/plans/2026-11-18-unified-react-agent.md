# Unified ReAct AgentLoop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 用指代 rewrite 和统一的父子 ReAct 循环替换意图分类与审核 DAG 协调。

**Architecture:** 扩展现有 runner/graph/nodes，统一消息、工具执行、校验、预算和收尾。父 Agent 通过受控 delegate_task 工具调用专业子会话，专业角色以 Prompt 和白名单装配。Workflow 持有业务快照、断点、成果及最终保存边界。

**Tech Stack:** Python asyncio、现有 LangGraph、ToolRegistry/ToolExecutor、OpenAI Chat Completions 严格结构化输出、unittest、React。

**Spec:** ../specs/2026-11-18-unified-react-agent-design.md

## Global Constraints

- 用户入口只做指代 rewrite，不生成 intent、domains、slots、task_plan 或前置 ready/clarify 检查。
- 每个 Agent 有 8 次正常工具额度；首次失败后最多 3 次免费纠错，成功重置失败链。
- 每个子 Agent 有独立 messages，同 task_id 延续上下文和预算。
- 父子使用同一个循环与工具执行入口，权限与白名单由应用装配。
- 原生工具调用和最终结构化结果分别校验，文本代码块不触发执行。
- 保留工具裁剪、部分成果、行程版本、授权保存、已选地点校验与断点续跑。
- 用户已批准实施；代码及离线验收已落盘，真实外部回放待单独授权。

## Review Focus

- 指代不明确、数量/否定/授权被 rewrite 改坏，以及改写失败。
- 同任务续跑与多个子会话的消息、失败链、预算隔离。
- 请求在断点认领、模型等待和保存不同阶段被取消。
- 任务版本变化后旧候选继续进入当前结果。
- 流式 length 或断流时完整的第一个工具调用被错误执行。

## 文件职责

| 文件 | 改动职责 |
|---|---|
| 新建 agent/chat/rewrite.py | 指代 rewrite 的输入、封闭输出契约、调用及保守降级 |
| agent/chat/state.py、context.py | 用户改写、角色配置、私有状态、结果与调用资源 |
| agent/chat/runner.py、graph.py、nodes.py | 共用循环、角色初始化、工具白名单、预算、纠错和证据收尾 |
| agent/chat/prompts.py | 普通角色 Prompt 与消息构造，移除旧意图块约定 |
| 新建 tools/delegation.py | 父级委派 ToolSpec、参数校验、角色和会话范围约束 |
| agent/chat/review_parent.py | 父角色 Prompt、工具装配及审核结果适配，删除独立协调循环 |
| agent/chat/review_children.py | 专业角色 Prompt、task_id 私有会话和 run_turn 入口装配 |
| agent/chat/review_execution.py | 共用预算和操作时限，统一失败链归属 |
| agent/chat/review_contracts.py | rewrite/任务结果/父级交付 Schema，移除 INTENT_SCHEMA 和旧任务表 Schema |
| agent/chat/review_workflow.py | 用户入口 rewrite、父会话启动、业务版本与最终提交 |
| service/travel/review_interaction.py | 分组候选、澄清及断点恢复、取消与保存未知处理 |
| service/storage/review_checkpoints.py | 断点认领、释放和完成状态，按故障场景验证 |
| service/utils/model_client.py、infrastructure/llm.py | 流式完整性、结构化响应、错误向循环传递 |
| service/chat.py、service/api/server.py | HTTP 结果字段和根运行日志适配 |
| frontend/src/ReviewAssistant.jsx、RuntimeLogs.jsx | 候选分组与 rewrite/父子调用日志展示，按实际消费调整 |
| agent/chat/intent.py、router.py、review_scheduler.py | 替换调用方并全仓检索后删除旧分类、关键词路由及 DAG 专属实现 |

## Task 1. 指代 rewrite 与入口契约

- [x] 为原样输入、单一指代、多重歧义、数量、否定与授权保留添加失败测试。
- [x] 新建 rewrite.py，使用有界模型调用及严格 Schema；核对引用来源和行程范围。
- [x] 普通聊天和审核入口传入原文、rewrite 和必要上下文，移除 understand/check_review_intent。
- [x] 更新 service/chat.py 与 API 夹具，执行 tests/test_api_chat.py 和相关 rewrite 测试。
- [x] 验收一次请求只出现 rewrite 和后续 AgentLoop，无独立意图识别与完整性模型调用。

## Task 2. 共用角色 AgentLoop

- [x] 添加任意角色 Prompt 初始化、任务/extra_data 消息顺序及角色 scope 测试。
- [x] 扩展现有 runner/context/state，初始化私有 messages；graph 去掉 understand/domain route 节点。
- [x] 工具可见集合由角色 registry 与服务端 scope 交集决定。
- [x] 合并预算与纠错规则，8 次正常调用与每失败链 3 次免费重试分别计数。
- [x] 工具失败先进入模型，超限/异常保留已验证成果，并进行一次有界收尾。
- [x] 普通聊天及子角色复用同一组 model/tools/verify 节点，运行 tests/test_agent_runner.py 与可靠性用例。

## Task 3. 原生委派与子会话

- [x] 先添加父级 delegate_task 原生调用、子工具白名单、深度限制与结构化结果测试。
- [x] 实现 tools/delegation.py，委派入参包含 task_id、role、task、extra_data，引用按当前运行校验。
- [x] 在 review_children.py 管理每个 task_id 的 Prompt、任务更新、消息与预算。
- [x] 在 review_parent.py 装配父 Prompt 和委派 registry，通过同一 run_turn 运行。
- [x] 验证父能看到子失败并决定改任务、换角色、澄清或交付；子失败后可改参数、换白名单工具或返回。
- [x] 验证独立委派在并发上限 3 下互不污染；依赖结果的后续委派由父级下一轮发起。
- [x] 接入原生委派后删除旧 kind=tasks/depends_on/retry 协议与 DAG 专属调用。

## Task 4. 当前成果、任务版本与输入体积

- [x] 添加修订任务后旧候选失效、同版本超时保留成果的回归测试。
- [x] 给成果关联 task_id/task_version，统一当前结果与诊断历史的读取规则。
- [x] 父子只传紧凑摘要及成果引用，子会话保留自身工具历史。
- [x] 将 preview_review_changes 的完整应用结果与发给模型的验收摘要分开。
- [x] 验证所有发给模型的试算、搜索、路线和任务结果都不包含 draft_plan、原始 POI 或路线几何副本。

## Task 5. 用户选择与断点取消

- [x] 添加“必去公园 + 两个新增景点 + 餐饮”分组选择和最终提案验收测试。
- [x] 父级候选结果提供各组需求和可选项，移除整句第一个数量的 regex 规则。
- [x] 更新审核交互和卡片消费，澄清和用户选择恢复到相应父级任务上下文。
- [x] 添加认领中、模型执行中、写入开始后、保存完成后四种取消测试。
- [x] 保存前取消安全释放，保存开始后依据事务完成标记核实，禁止取消后重复写入。
- [x] 覆盖原有版本冲突、用户选定 POI、试算拒绝、提交一次和结果重放路径。

## Task 6. 流式完整性与调用追踪

- [x] 添加 length、content_filter、refusal、断流和半批工具参数错误测试，断言没有工具执行。
- [x] 模型响应完成且结束原因有效后才交付整批调用，原生调用标识和 tool 回复逐一对应。
- [x] 日志关联 root run、父子 Agent、task_id/task_version、operation_id/retry_of。
- [x] 根运行单独记录 run_end，子任务结束不覆盖根运行状态。
- [x] 日志页面读取新 rewrite 事件，并继续显示历史意图事件。

## Task 7. 收敛旧路径与端到端验收

- [x] 全仓检索 understand、semantic、task_plan、check_review_intent、Scheduler、旧任务表和消息块，更新调用方、测试、文档后删除失效代码。
- [x] 保留有效业务断言，将意图分类与 DAG 过程测试替换为 rewrite、原生委派和最终目标断言。
- [x] 运行普通聊天、API、审核工作流、HTTP、模型传输、高德与地图兼容性测试。
- [x] 执行前端构建、Python 编译、可用静态检查及差异检查。
- [ ] 真实回放“第一天公园 + 两个景点 + 餐饮”和一次多轮指代请求，核对完成状态、输入体积、耗时及错误恢复。
- [x] 单列环境阻塞及既有失败，不能把模拟测试通过表述为真实提供方验收通过。

## 验收标准

入口只进行指代 rewrite，父级自主决定是否需要补充信息。父子循环由同一执行器驱动，工具权限不同，子会话有独立 Prompt、任务、额外数据和调用历史。失败后调用方模型能依据错误继续行动，预算和失败链不会因继续同一 task_id 而重置。四项已确认缺陷均有回归覆盖，工具裁剪与保存链路通过原有入口验收。

## 实施验收记录

入口已去除分类，父子均复用 runner/graph/nodes。任务契约迁移到 review_tasks.py，旧 intent.py/router.py/review_scheduler.py 已删除。原生委派、私有会话、独立预算、按组数量、当前成果版本、取消安全、试算与保存链路均有离线回归。

额外修复：任务并发结束重新核对依赖版本；失效任务列入剩余事项；缺失分组不签发选择动作；原生同名工具按 call_id 配对；父委派不以一次底层操作时限截断整个子循环；压缩保留当前任务及完整工具消息组。

真实模型与高德回放被自动审批拒绝：要求用户明确批准测试数据外发。未以其他路径重试该回放。本地回归使用禁止外部 HTTP 的保护，结果记录在 logs/unified-react-verification.txt。

最终本地验证：相关套件 206 项中 204 项通过；两个既有失败分别是 service.cli 缺失、MCP 快照缺少已有 refresh 参数（当前 HEAD 的服务签名已包含该参数）。最后变更路径额外复核 43 项全部通过，记录在 logs/unified-react-final-paths.txt。Vite 构建、Python compileall 和 git diff --check 通过。Ruff 未安装，未宣称静态规则检查通过；未运行全仓完整套件。
