# 行程生成与审核页对齐实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 让生成结果符合攻略与真实地点，使审核页提供每日路线、费用和备注编辑，并将未确定列表严格限定为用户手动暂存区。

**Architecture:** 保留现有 LangGraph、API 和 React 组件。LLM 提取及排列地点，程序处理 POI 校验、锚点、路线、排程、预算和风险；生成异常进入审核问题，不进入用户暂存区。

**Tech Stack:** Python、LangGraph、unittest、React、Vite、现有高德适配。

**Spec:** 本次用户要求及 `docs/superpowers/specs/2026-10-30-agent-graph-responsibilities-design.md`；冲突处以本计划中的最新约束为准，替代旧计划“遗漏地点进入未确定列表”的规则。

## Global Constraints

- `planning.unscheduled_items` 仅供用户手动移入、拖入和移出；graph 不生成其内容。首次生成为空。
- graph 不再用活动差集填写 `unscheduled`、`unplaced_activities` 或用户暂存区。
- 用户已暂存的项目在重新生成时保留，并从本次自动排程输入中排除；不重复加入每日行程。
- 未定位、排序遗漏、容量不足等生成问题通过 audit issues 和生成状态展示，不能伪装为成功或自动移入暂存区。
- 程序固定插入家、所选酒店和已选城际交通；酒店不得成为普通游玩活动。
- 仅收攻略中明确的餐馆店名；住宿区域作为住宿搜索线索，菜名作为备注。
- 有效 POI 候选由程序筛选和 LLM 最终决定，前端不以人工确认位置作为生成必经步骤。
- 费用仅计当前实际安排及已支付项目；未知价格不计入金额，不显示为免费。
- 不新增价格提供商，不编造酒店房价、门票、交通班次或价格。
- 保留已有未提交改动；历史行程通过明确重新生成更新，不在 GET 页面时改写数据。

## Review Focus

- 模型遗漏地点时有明确重试反馈，不能静默漏排或放入用户暂存区。
- 重新生成及检查点恢复不会复活旧地点、重复地点或清空用户暂存项目。
- 超过午夜的行程不会被截为多个 23:59 项目。
- 住宿区域、具体酒店、菜名、饭店及同义景点正确区分。
- 暂存项目不进入未支付费用合计，已支付金额仍保留。

---

### Task 1：锁定用户暂存区的写入边界

**Files:** `agent/itinerary.py`, `agent/planning_graph.py`, `service/travel/planning.py`, `service/travel/itinerary_audit.py`, `service/travel/generation_jobs.py`。

**Tests:** `tests/test_itinerary_agent.py`, `tests/test_route_grounded_proposal.py`, `tests/test_guide_generation.py`, `tests/test_map_review.py`。

**Interfaces:** `days[].visits[].position` 继续作为模型输出；`planning.unscheduled_items` 保留现有手动编辑接口。`move_activity(..., target_day=None)` 是移入暂存区的入口，重新生成只复制用户已有暂存内容。

- [x] 写失败测试：首次生成暂存区为空；LLM 漏排触发反馈重试；无定位地点只生成 issue；用户手动移入后才能出现暂存卡片；重新生成保留手动暂存且不再排入。
- [x] 运行 `python -m unittest discover -s tests -p test_route_grounded_proposal.py -q`，确认新断言在旧实现下失败。
- [x] 删除 `_validate_proposal`、`prepare_draft`、`draft_itinerary` 自动计算并填充暂存区的分支；已定位且有效的排程输入要求恰好安排一次。未定位地点不交给排序模型，输出独立定位问题。
- [x] 将排序遗漏、无效 POI 和手动暂存的审核文案分开；手动暂存本身不作为模型失败或阻止确认的原因。
- [x] 为新生成记录写入契约版本，并更新检查点版本；旧的自动未安排记录不得被当作新用户手动暂存。历史列表继续可读，重新生成时按新规则重建并明确提示旧生成结果已更新。
- [x] 运行对应测试，确认通过。

### Task 2：修复提取、分类、匹配和重生成合并

**Files:** `.agents/skills/guide-recognition/SKILL.md`, `service/travel/guide.py`, `service/travel/place_matching.py`, `service/travel/map_planning.py`, `service/travel/planning.py`。

**Tests:** `tests/test_guide_workflow.py`, `tests/test_guide_generation.py`, `tests/test_guide_graph_workflow.py`。

**Interfaces:** 沿用 `places`、`note`、`category` 和 POI 数据；住宿区域存为住宿搜索线索，不生成景点；以高德 `place_id` 合并同一实体，并重新映射约束引用。

- [x] 写失败测试：龙翔桥/湖滨、吴山广场/定安路作为住宿区域线索；葱包烩不成为店名；酒店不进入游玩列表；饭店按 POI 类型成为 food；断桥与断桥残雪同 POI 合并。
- [x] 补测重生成：删除原攻略地点后旧自动活动不复活；用户明确添加的活动和编辑保留；未知来源旧数据不能仅因有坐标无限保留。
- [x] 修改提取 prompt 与必要字段，保留时间描述于 note；区域建议用于限定酒店搜索，不把推荐区域误当具体酒店或变成换酒店约束。
- [x] 对搜索候选按城市和类别过滤，可靠唯一候选直接采用，其余交 LLM；调整名称子串硬拦截，使有效候选选择不因别名被推翻。无有效候选输出定位失败问题。
- [x] 合并地点时保留原文备注与约束引用；统一普通入口和自动生成入口的匹配规则。
- [x] 运行三组对应测试，确认通过。

### Task 3：修复排程溢出与时长审核

**Files:** `agent/itinerary.py`, `agent/planning_graph.py`, `service/travel/planning.py`, `service/travel/itinerary_audit.py`。

**Tests:** `tests/test_route_grounded_proposal.py`, `tests/test_guided_planning.py`。

**Interfaces:** 模型只提供顺序；程序使用路线分钟数和停留时长计算实际可行性，固定交通和锚点不被改写。

- [x] 写失败测试：首日到达较晚但有六处景点；返程交通前缓冲不足；跨午夜；缺失一种路线模式仍能比较；所有路线失败显示未验证。
- [x] 移除把溢出时间截到 23:59 的成功排程方式；在提交排程前识别日界和活动窗口溢出，反馈给排序节点进行有限次数调整。
- [x] 明确 prompt：有效地点需要完整覆盖，容量冲突应在其他日期允许的范围内调整；原文硬约束无法满足时保留明确失败风险，不丢地点、不自动填暂存区、不声称生成成功。
- [x] 三种交通方式仍按有效路线时间选最短；若攻略交通偏好与最快方式冲突，在说明中展示该差异。
- [x] 时长审核合计活动和市内交通，区分固定交通、酒店锚点与真正景点数量，避免把零时长锚点计作景点强度。
- [x] 运行对应测试，确认没有重复 23:59 和无声漏排。

### Task 4：预算只计算实际行程

**Files:** `service/travel/planning.py`, `service/travel/itinerary_audit.py`, `service/integrations/travel_amap.py`。

**Tests:** `tests/test_budget_reporting.py`, `tests/test_amap.py`。

**Interfaces:** 沿用 `budget_summary(plan)` 返回的 items；每项区分费用类型、计价人数或车辆、数据来源、已知或未知及已支付金额。

- [x] 写失败测试：手动暂存餐馆不计预计消费；同一饭店重复访问按实际访问次数计算；已支付但已移出行程的支出保留；人均价不作为酒店每晚房价或门票。
- [x] `_billable_items` 根据实际 itinerary 引用生成未支付活动费用；公交按人数、打车按车辆计入；未知项目单独展示且不相加。
- [x] 运行对应测试，确认合计与明细一致。

### Task 5：重排审核页并直接展示有效信息

**Files:** `frontend/src/ReviewPage.jsx`, `frontend/src/GuidedPlanner.jsx`, `frontend/src/PlacePicker.jsx`, `frontend/src/planningLabels.js`, `frontend/src/auditActions.js`, `frontend/src/guided-planner.css`。

**Interfaces:** 使用前四项的真实行程、audit 和预算明细；已有 move、route_mode 和活动编辑 API 继续使用。

- [x] 页面主顺序改为：日期与每日概览 → 当天地图及行程 → 用户暂存区 → 费用明细 → 一份注意事项 → 保存/确认操作。
- [x] 移除审核页首屏人工 POI 确认流程；定位问题展示失败原因和重新匹配入口，用户仍可主动编辑地点。
- [x] 暂存区空时明确说明“用户可将地点移入这里”；只有手动操作产生卡片；不再重复展示“未安排地点”说明列表。
- [x] 卡片直接展示真实类型、攻略备注、门票或饭店人均等已知信息；酒店锚点显示酒店名；去掉无信息量的“按每日顺序安排”。
- [x] 费用表展示已计入金额、来源、人数/车辆和未知费用；风险文案用地点名及自然语言替换内部 ID 和约束枚举。
- [x] 保留拖动、跨天移动、移入/移出暂存区、手动交通方式和停留编辑；重新匹配与生成保留加载、失败和重试状态。
- [x] 使用现有前端测试机制补充暂存入口与单份审核列表检查；运行 `npm run build`。

### Task 6：用当前杭州行程做端到端验收

**Files:** 当前页面及其生成 API；不在读取页面时更改数据库。

- [x] 运行 `python -m unittest discover -s tests -q` 和前端构建，修复本次变更导致的失败。
- [x] 从当前杭州攻略生成新的可审查结果；保留原行程和购买记录，生成实际调用结果与模拟测试结果分开报告。
- [x] 浏览器检查三天：酒店不作为游玩点；无菜名饭店、重复 POI；首次暂存区为空；无法完成生成时有清楚状态；无 23:59 截断。
- [x] 验证手动移入暂存、切换日期再加入、重新生成保留手动暂存；验证备注、路线来源、预算明细和一份注意事项。
- [x] 宽屏与窄屏检查地图、日期切换、卡片和主要操作，保存截图供用户核对。
- [x] 完成独立差异审查并修复三项问题；工作区含原有改动，保持未提交状态。

## 执行与验证记录

- 新增 18 项回归检查。全量 235 项测试通过后，针对实际 Python 3.10 的 asyncio 超时补充修复，18 项专项检查再次通过。前端构建通过。
- 独立审查发现并修复：手动地点定位后来源变化导致丢失、地址变化仍复用旧自动 POI、修改路线忽略跨日偏移。
- 真实模型与高德调用生成了验收副本 `79f3d0b283fe4cbf85989effb7740f59`，原行程未覆盖。该副本是中途版本的真实结果，仍有定位及容量风险，用于页面和手动操作验收，不作为无风险出行方案。
- 最新版本最后一次完整重生成在攻略模型调用阶段超时，未覆盖已保存结果。完整真实链路仍需模型服务恢复后重跑；不能将该项解释为最终模型联调全部通过。
- 浏览器验证三天切换、单份注意事项、费用明细、手动移入暂存、跨天加入和恢复日期；重新加载确认暂存区为零。
- 390px 窄屏发现并修复费用表撑开网格的问题；修复后页面 scrollWidth 与 clientWidth 均为 375px。视口已恢复默认。
- 手动暂存跨重生成的保留已由专项测试验证，未在模型超时后再做实际完整重生成。
