# 路线驱动的每日行程修改计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 只决定每天的地点顺序和建议交通方式，由地图路线和确定性排程生成完整行程。

**Architecture:** 保留现有 LangGraph、生成任务与检查点。规划模型提交按天排序的轻量提案；服务端绑定地点、查路线、计算时间并审查，审查反馈驱动有限次重新提案。

**Tech Stack:** Python、LangGraph、高德 Web 服务 API、现有 React 审核界面、pytest。

**Spec:** `docs/superpowers/specs/2026-10-30-route-grounded-itinerary.md`。本设计替换 `2026-10-29-itinerary-output-contract.md` 中模型必须输出精确时刻且不能输出交通方式的部分。

## Global Constraints

- 固定往返班次、已购项目、酒店及地点身份只由服务端持有，模型不能覆盖。
- 模型不输出最终时间、坐标、费用或路线详情；地点身份继续通过冻结列表位置绑定。
- 路线查询失败须显式反馈，不能作为已验证路线提交。
- 保留输入指纹、断点续跑和结果提交前的并发校验；失败不覆盖原计划。

## Review Focus

- 相同名称的两家店：位置绑定仍指向不同的已确认 POI（任务 1）。
- 一日游同时有去返固定车次：排程不能越过返程时间（任务 3）。
- 首末站无地图路线或车站未确认：结果明确待核对（任务 2、3）。
- 攻略指定上午但路线到达已是下午：审查报冲突，不悄悄改写要求（任务 3、4）。
- 恢复检查点时旧状态缺少新字段：按版本拒绝旧检查点并安全重新生成（任务 5）。

---

### Task 1: 替换模型提案契约

**Files:** Modify `agent/itinerary.py`; modify `tests/test_itinerary_agent.py`; modify `tests/test_itinerary_output_contract.py`.

**Interfaces:** `generate_itinerary_proposal(plan: dict, feedback: list | None = None) -> dict` 返回 `{"days": [{"day": 1, "visits": [{"position": 1, "travel_mode": "WALK"}]}], "unscheduled": [{"position": 2, "reason": "..."}]}`。每个旅行日恰好一个 `days` 项；所有输入地点在 `visits` 或 `unscheduled` 中恰好出现一次。服务器将位置映射为 `activity_id`，不信任模型提供的身份字段。

- [ ] 写失败测试：三日分配、日内顺序、同名 POI、空地点、遗漏/重复位置、非法交通方式和意外字段。
- [ ] 运行 `python -m pytest tests/test_itinerary_agent.py tests/test_itinerary_output_contract.py -q`，确认新测试因旧契约失败。
- [ ] 修改 Schema、提示词、`_validate_proposal`；将模型输出规范化为服务端内部 ID 提案，保留非空未安排原因。
- [ ] 运行上述测试，确认通过。

### Task 2: 用建议方式查询真实路线

**Files:** Modify `service/travel/planning.py:435`; modify `agent/planning_graph.py:266`; modify `tests/test_guide_generation.py`.

**Interfaces:** `draft_itinerary(plan: dict, proposal: dict | None = None) -> dict` 接受 Task 1 的内部 ID 提案，创建按天顺序的活动和相邻路线占位，路线保存建议 `travel_mode`。`route_agent(state)` 按此方式调用 `amap_compute_route`，保存来源、时长和不可用原因；无有效结果时标记待核对。

- [ ] 写失败测试：两段不同交通方式、首末站路线、地图失败及方式回退；检查地点顺序与路线对齐。
- [ ] 运行 `python -m pytest tests/test_guide_generation.py -q`，确认失败点。
- [ ] 修改组装与路线查询；移除统一 `TRANSIT` / `DRIVE` 和固定 30 分钟作为已验证路线的假设。保留已有人工改路线入口。
- [ ] 运行上述测试，确认通过。

### Task 3: 地图结果落成具体时刻

**Files:** Modify `service/travel/planning.py:672`; modify `agent/planning_graph.py`; modify `tests/test_guided_planning.py`; modify `tests/test_guide_graph_workflow.py`.

**Interfaces:** 新增 `schedule_itinerary(plan: dict) -> dict`：从固定车次/酒店锚点开始，按地点时长、路线时长、缓冲和时间窗计算活动开始时间；返回带最终时间和冲突标记的计划。新增图节点 `schedule_agent`，连接 `route_agent → schedule_agent → audit_agent`。

- [ ] 写失败测试：地图耗时改变活动时刻、一日游、末日返程、时间窗冲突、路线未知时不得宣称排程已验证。
- [ ] 运行 `python -m pytest tests/test_guided_planning.py tests/test_guide_graph_workflow.py -q`，确认失败点。
- [ ] 实现排程函数与图节点；仅排程节点计算最终时刻，固定班次不可移动。
- [ ] 运行上述测试，确认通过。

### Task 4: 分开格式重试与审查修订

**Files:** Modify `agent/planning_graph.py:240`; modify `service/travel/itinerary_audit.py`; modify `tests/test_guide_graph_workflow.py`; modify `tests/test_guide_generation.py`.

**Interfaces:** 状态改为 `format_attempts` 和 `revision_attempts`，分别最多 3 次；格式失败只增加前者，审查后再规划只增加后者。审查反馈包含冲突地点/日、地图失败或硬约束原因；不可自动修复的问题返回 `needs_attention`。

- [ ] 写失败测试：一次格式失败后仍允许三轮审查修订；持续非法输出有界停止；无路线或硬约束冲突进入人工核对。
- [ ] 运行 `python -m pytest tests/test_guide_graph_workflow.py tests/test_guide_generation.py -q`，确认失败点。
- [ ] 调整状态、分支条件和审查结果，不让审查反馈覆盖格式错误反馈。
- [ ] 运行上述测试，确认通过。

### Task 5: 检查点与界面兼容

**Files:** Modify `service/travel/generation_jobs.py`; modify `service/storage/planning_jobs.py`; modify `frontend/src/GuideGeneration.jsx`; modify `frontend/src/GuidedPlanner.jsx`; modify `tests/test_guide_generation.py`; modify `frontend/tests/generationPolling.test.js`.

**Interfaces:** 检查点增加 `schema_version`，旧版本重启生成；`next_stage` 支持 `schedule_agent`。任务结果区分 `failed` 与需要人工核对的 `needs_attention`，前端展示具体问题并提供已有的地点核对/审核入口。

- [ ] 写失败测试：各新节点续跑、旧检查点失效、失败不提交、待人工核对时问题可见且可继续编辑。
- [ ] 运行 `python -m pytest tests/test_guide_generation.py -q` 和 `node --test frontend/tests/generationPolling.test.js`，确认失败点。
- [ ] 更新检查点、阶段映射及前端状态展示；保持原计划和人工编辑功能。
- [ ] 运行受影响的 Python 与前端测试，再运行 `git diff --check`；用一次离线生成样本和一次配置好高德的联调样本检查完整链路。

## 交付检查

- [ ] 全量运行相关行程测试，记录通过数和任何环境限制。
- [ ] 对照规范核验模型输出、路线来源、最终时间、审查反馈、检查点和人工入口。
- [ ] 只提交本计划涉及的文件；工作区现有其他未提交改动须保留。
