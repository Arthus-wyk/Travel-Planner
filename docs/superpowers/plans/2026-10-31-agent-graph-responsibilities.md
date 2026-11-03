# 行程 Graph 节点职责实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 按确认的设计，让模型只提取、匹配与排列真实地点，由程序组装锚点、选最快路线、归集已知费用并审核风险。

**Architecture:** 保留现有 LangGraph 和计划结构，收窄 itinerary 模型输出，并在现有路线、预算和审核函数中补齐确定性处理。高德的缺失字段保持未知，不使用模拟价格。

**Tech Stack:** Python、LangGraph、现有高德 Web Service 适配层、unittest。

**Spec:** `docs/superpowers/specs/2026-10-30-agent-graph-responsibilities-design.md`

## Global Constraints

- 保留工作区已有未提交改动；不重写用户文件。
- 模型不能发明 POI、坐标、报价或交通班次。
- 家、酒店及已选城际交通由程序固定插入。
- 预算只计已购、用户输入或 API 返回且语义明确的金额。

## Review Focus

- 地点同时安排和未安排、重复或遗漏：程序算差集，重复拒绝。
- POI 跨城、错误类别或无坐标：不自动采纳。
- 路线某方式失败：其余方式仍可比较，全部失败标记未验证。
- 高德人均消费不得作房价或门票；缺价不当作免费。
- 审核修改与检查点恢复后不丢失未确定地点或人工费用。

### Task 1: 每日顺序契约与程序差集

**Files:** `agent/itinerary.py`, `service/travel/planning.py`, `agent/planning_graph.py`, `tests/test_route_grounded_proposal.py`, `tests/test_guide_graph_workflow.py`

**Interfaces:** 模型输出 `days: [{day, visits: [{position}]}]`；服务端输出旧 `proposal` 结构且 `unscheduled` 由已选地点求差集。

- [x] 写测试：遗漏地点进入未确定列表，重复地点被拒，模型请求不含交通方式或未安排字段。
- [x] 运行测试并确认因旧契约失败。
- [x] 改模型 schema、提示词和校验；保留有限重试及反馈位置。
- [x] 改行程组装和已有调用测试，确认锚点仍固定。
- [x] 运行相关 unittest。

### Task 2: 地点提取与自动匹配

**Files:** `.agents/skills/guide-recognition/SKILL.md`, `service/travel/guide.py`, `service/travel/place_matching.py`, `tests/test_guide_generation.py`, `tests/test_guide_workflow.py`

**Interfaces:** 提取结果保留地点类型及备注；匹配只可选择经过城市、类别校验的高德候选。

- [x] 写测试：多个候选由 LLM 选；错误类别或城市的候选被拒；无候选不编造位置。菜名过滤写入提取指令。
- [x] 运行测试并确认预期失败。
- [x] 最小修改提取指令与搜索匹配逻辑；保留已有手动定位。
- [x] 运行相关 unittest。

### Task 3: 最快路线与可追溯费用

**Files:** `service/integrations/travel_amap.py`, `service/mcp/server.py`, `agent/planning_graph.py`, `service/travel/planning.py`, `service/travel/itinerary_audit.py`, `tests/test_amap.py`, `tests/test_guided_planning.py`, `tests/test_guide_graph_workflow.py`

**Interfaces:** route_agent 比较 WALK、TRANSIT、DRIVE 的有效时长；路线和饭店金额带来源；预算合计不使用模拟额度或未知价格。

- [x] 写测试：最快有效路线胜出、失败方式跳过、全失败标风险、公交与打车费用按正确单位计入。
- [x] 写测试：POI 人均只计饭店；酒店与门票未知；旧模拟候选不计入；已购费用保留。
- [x] 运行测试并确认预期失败。
- [x] 改高德响应映射、路线节点、候选价格和预算审核。
- [x] 运行相关 unittest。

### Task 4: 完整工作流与前端说明

**Files:** `agent/planning_graph.py`, `service/travel/generation_jobs.py`, `frontend/src/ReviewPage.jsx`, `tests/test_guide_generation.py`, `tests/test_map_review.py`

**Interfaces:** 检查点能恢复新增节点；审核页显示已知费用和未知项；非排序故障不触发模型重试。

- [x] 写测试：生成工作流的节点顺序、未确定地点保留、未知费用提醒及恢复下一节点。
- [x] 运行测试并确认预期失败。
- [x] 接入费用归集与审查文案；原检查点状态兼容，无须增加版本。
- [x] 运行完整 Python unittest 和前端构建；报告未验证的外部 API 行为。
