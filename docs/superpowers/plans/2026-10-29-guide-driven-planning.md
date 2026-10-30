# 攻略驱动行程 Implementation Plan

> **For agentic workers:** Use executing-plans for integration and dispatching-parallel-agents for the isolated frontend and model modules. Track completion below.

**Goal:** 将交通独立为第一步，第二步分类选点和住宿，复用解析地点位置，由独立模型 Agent 生成每日行程。
**Architecture:** 沿用现有 LangGraph；攻略解析链路保存位置，独立 itinerary_agent 提案、route_agent 算通勤、audit_agent 校验并有限修订。复用 planning JSON 和现有 API。
**Tech Stack:** Python / FastAPI / LangGraph / React / Vite / 高德。
**Spec:** docs/superpowers/specs/2026-10-29-guide-driven-planning-design.md

## Global Constraints

- 保留现有未提交变更；不新增依赖、部署服务、真实预订或付款。
- 火车、飞机、自驾、大巴；自驾往返绑定，仅支持当天到达；一次行程一家酒店。
- 下游按地点 ID 引用第二步保存的 place，不按名字重复定位。
- 用户已要求“开始修改”，直接实施；当前分支 codex/langgraph-stages-1-3，保留工作区中现有未跟踪功能文件，不另建缺少这些文件的 checkout。
- 不提交、推送或合并用户现有大量混合变更。

## Review Focus

- 最新文本未先保存直接生成，地点位置仍正确引用。
- 同名分店手工定位、分类及选择在重解析后保留。
- 酒店与餐饮不重复计费，购买记录不丢。
- 模型非法 JSON / ID / 时间受控失败，旧行程不丢。
- 食物/其他地点在审核页可移动，自驾时刻及车辆数受校验。

### Task 1: 领域数据、地点解析和自动定位

Files: service/travel/{planning,guide,map_planning,itinerary_audit}.py; tests/test_guide_workflow.py; .agents/skills/guide-recognition/SKILL.md。
Interfaces: add_manual_activity(..., category='attraction'); select_activities(plan, choices, categories=None, selected_hotel=...); draft_itinerary(plan, proposal=None)；四类 category。生成前使用同一解析及定位链路。
- [ ] 写并运行交通、分类酒店、位置复用和计费失败测试。
- [ ] 最小实现上述接口；保持直接调用 draft_itinerary 的规则兼容，生产 draft 必走模型。
- [ ] 运行领域及预算回归并修复。

### Task 2: 独立模型行程提案（独立模块）

Files: agent/itinerary.py; tests/test_itinerary_agent.py。
Interface: generate_itinerary_proposal(plan: dict, feedback: list | None = None) -> dict；stops 含 activity_id/day/time_of_day/duration_minutes/reason，unscheduled 含 activity_id/reason，assumptions 和 constraint_results 为列表。
- [ ] 写失败测试验证模型输入含最新原文、位置及人工选择，验证非法输出。
- [ ] 使用现有模型客户端输出并校验 JSON，异步调用限时；无任意工具或数据库写入。
- [ ] 运行单元测试并报告。

### Task 3: 前端流程（独立文件）

Files: frontend/src/{GuidedPlanner,PlacePicker,ReviewPage}.jsx; guided-planner.css。
Interfaces: choose_candidates 接收 outbound/return/drive_departures/vehicles；choose_activities 和 draft 接收 choices/categories/selected_hotel，draft 另含 text。add_activity 含 category。
- [ ] 第一步仅交通，四种工具，自驾车辆数/出发时间；第二步四组地点，酒店单选，默认显示地图。
- [ ] 生成一次提交最新文字、选择、分类及酒店；人工选择不因旁路 action 丢失。
- [ ] food/other 可移动，所有交通文案使用 kind 映射；审核页显示 Agent 理由及问题。
- [ ] npm run build。

### Task 4: 工作流整合及最终验证

Files: agent/planning_graph.py; tests/test_guided_{graph,api}.py; tests/test_map_review.py。
- [ ] graph draft 在文本变化时解析，之后仅引用已有位置；准备输入、模型提案、地图、审核，最多两次修订。
- [ ] 针对旧测试加入模拟模型边界，验证错误不保存、已定位点不重复搜索、固定交通和酒店预算。
- [ ] 运行 unittest discover 全套和前端 build；fresh reviewer 检查实现，处理重要问题。
- [ ] 更新本计划与设计状态，交付结果及真实联调限制。

## Ledger

2026-10-29: 执行开始。Ruling: 在现有 codex 分支增量编辑，避免丢失未提交基础功能；各实现代理文件互不重叠。
