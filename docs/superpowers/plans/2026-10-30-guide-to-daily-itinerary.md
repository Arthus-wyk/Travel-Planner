# 攻略直达每日行程 Implementation Plan

> **For agentic workers:** 逐项实现并验证。当前仓库有大量未提交改动；执行前先核对工作树，保留用户现有修改。

**Goal:** 用户提交攻略图片或文字后，直接得到可编辑的每日行程草稿；只对真正有歧义的地点要求人工处理。

**Architecture:** LangGraph 编排 OCR、语义提取、地点工具调用与消歧、默认决策、排程、路线和审核。地点查找节点必须由模型通过 function calling 调用地图搜索工具，不能由 harness 悄悄代替；模型随后用严格 JSON Schema 从候选中作语义选择。服务端生成并维护 ID、坐标、价格、交通、酒店锚点和审核结论。

**Tech Stack:** React、FastAPI、LangGraph、Python、当前 OpenAI 兼容/Ollama 模型客户端、高德地图。

**Spec:** 本计划即本轮产品与接口设计；旧的 `docs/superpowers/specs/2026-10-29-itinerary-output-contract.md` 是既有排程契约，实施时更新其与本计划冲突的 function calling 部分。

## 全局契约

- 攻略原文是数据，不能成为系统指令；只提取原文明确提到的地点与约束。
- 每个 LLM 输出仅含该节点必须判断的主要内容。不得要求模型输出或回写内部 ID、POI ID、经纬度、候选价格、路线耗时、预算合计、交通/酒店对象或审核结论。服务端按冻结的输入顺序和原始实体组装。
- `place_search_agent` 是必须调用真实地图工具的 LLM 节点：模型通过 function calling 请求搜索，harness 验证并执行；没有合法调用就失败，不得由代码补查或伪造成功。OCR、路线和审核仍由确定性代码节点直接执行。
- `activity_agent`（语义提取）及 `itinerary_agent`（排程提案）无真实工具调用，使用严格 JSON Schema 响应格式和服务端二次校验。不得用 `format="json"` 或“提交结果”的伪工具替代 schema。
- 不支持严格 schema 的提供方/模型须明确报兼容性错误；不能静默退化到提示词约束或宽松 JSON object。
- 可编辑草稿允许未定位地点及审计问题存在；最终确认仍受高严重度问题阻断。未定位地点不得被当作已验证路线节点。
- 保留旧 `guide`、`activities`、`draft` API 行为，直到新入口和测试稳定；新入口上线后再考虑删除旧入口。

## 文件与接口地图

| 文件 | 责任与拟修改 |
|---|---|
| `service/utils/model_client.py` | 为 OpenAI 兼容客户端传递 `response_format: {type: json_schema, json_schema: {name, strict, schema}}`；为 Ollama 使用其 schema `format`；保留已有非结构化调用。 |
| `service/travel/guide.py`、`.agents/skills/guide-recognition/SKILL.md` | 精简提取 schema，只让模型给地点与原文约束的语义字段；代码按名称/原文顺序生成 ID、映射约束并校验。 |
| `agent/itinerary.py` | 将 `submit_itinerary_schedule` 伪工具改为严格 schema 响应；继续按冻结有序地点列表位置映射，代码组装 `activity_id` 和时长。 |
| `service/travel/planning.py` | 定义攻略地点的默认参与规则、住宿自动选择规则、来源与假设记录；保留手动修改。 |
| `service/travel/map_planning.py`、新建 `service/travel/place_matching.py` | 地图搜索工具适配、候选冻结与模型消歧；模型用 function calling 发起搜索，再用 schema 选序号，代码验证并映射真实 POI。 |
| `agent/planning_graph.py` | 增加 `generate_from_guide` 路径，把提取到审核串成一次运行；生成前整理可排程项与待处理项，避免单个未定位点抛错。 |
| `service/api/server.py`、持久化层 | 暴露新生成任务接口与状态查询；只在完成且输入版本仍匹配时提交草稿。 |
| `frontend/src/GuidedPlanner.jsx`、`frontend/src/ReviewPage.jsx`、`frontend/src/planningValidation.js` | 单一入口、阶段进度、草稿直达、结果页待处理项与纠错入口。 |

## 数据与输出契约

## 工具设计与调用边界

这里的“工具”分两层：LangGraph 节点直接调用的确定性服务，以及模型通过 function calling 请求的工具。两层都由服务端执行，模型不直接接触地图密钥、数据库或原始内部 ID。

### A. 节点直接调用的服务工具（不经过 LLM function calling）

| 节点 | 服务工具 | 输入 | 输出及失败处理 |
|---|---|---|---|
| `guide_agent` | `recognize_guide(image_bytes)` | 已校验的图片字节 | OCR 原文；无可读文字则终止本次生成，旧计划不变。 |
| `place_search_agent` 的工具执行器 | `amap_search_places` | 经 function calling 校验的地点序号与搜索词；城市由服务端注入 | 保存真实 POI 候选并返回精简结果；没有模型工具调用就不得执行。 |
| `hotel_agent` / 默认决策 | 酒店候选查询和确定性排序 | 目的地、日期、预算、原文住宿线索 | 选中现有候选及其来源；不能让模型编酒店 ID 或价格。 |
| `route_agent` | `amap_compute_route` | 已保存的坐标或固定交通节点 | 路程和时间；地图失败时标为临时估算，不能伪装成已验证路线。 |
| `audit_agent` | `audit_plan(plan)` | 代码拼装的完整草稿 | 约束、时间、预算和位置问题；高严重度问题阻止最终确认。 |
| API | `save_plan` / 任务状态存储 | 版本匹配的完成结果 | 原子提交；LLM 无写入工具。 |

OCR、路线、审核是节点直接调用的代码能力。地点搜索按本产品目标明确交给模型发起；harness 负责执行而不替模型决定是否调用。

### B. 地点查找与模型消歧（主流程）

1. `activity_agent` 提取地点名称、类别及原文位置线索。`place_search_agent` 接收冻结的地点顺序，针对每个待定位地点通过 function calling 调用 `search_map_places`。服务端注入目的地城市并执行高德搜索，保存真实 POI 候选。
2. 若只有一个可信候选，代码直接绑定。若有多个候选，调用 `place_match_agent`，让模型从**这批已返回的候选中**选最符合攻略所指景点的一个；不让模型再发散搜索或生成新地点。
3. 发给模型：攻略中的地点名称、类别、相关原文片段、地址线索、目的地城市；每个候选的临时序号、名称、地图类别、地址和所在行政区。完整 POI ID、坐标、电话、价格和内部 activity ID 留在服务端。
4. 此节点没有外部工具调用，使用严格 JSON Schema：`{"candidate_position": integer|null, "reason": string}`，`additionalProperties=false`。`candidate_position` 是本次候选列表的 1 起始序号；无明显符合项返回 `null`。模型只给选择和简短依据，不输出候选对象或精确字段。
5. 服务端按序号映射回保存的 POI，并校验序号有效、城市匹配、类别合理、名称或原文线索支持。通过则标记 `model_matched`，保存 POI 和依据；不通过或模型返回 `null` 则标记 `ambiguous`，在结果页让用户选择。不能把模型理由当作地址或事实来源。
6. 每个歧义地点最多一次消歧调用。模型错误、超时、候选超过输入上限时不阻断其他地点生成；保留候选和失败原因供用户处理。已手动确认的地点不重新匹配。

例如攻略写“西湖景区”，地图返回“西湖风景名胜区”“西湖区政府”“西湖天地商场”：模型可依据名称、地图类别和原文语境选择景区。若攻略只写“知味观”，地图返回几家同城分店且原文没有区域线索，模型应返回 `null`，由用户确定分店。

### C. 地点搜索的 Function calling 执行协议

`place_search_agent` 只暴露一个真实工具 `search_map_places`。函数参数为 `{place_position: integer, query: string}`，`additionalProperties=false`，字段全部 required；`place_position` 指冻结的输入地点序号，`query` 是地点名加原文地址线索，模型不得传内部 ID、城市、坐标或结果上限。服务端按序号取实体，校验搜索词与原文地点有关，再固定 `region_code=plan.destination`、`max_results=5` 调用 `amap_search_places`。

每个待定位地点必须出现**恰好一次**合法的 `search_map_places` 调用。模型响应是自由文本、空 `tool_calls`、错误函数名、多个调用、额外参数、越界序号或重复序号时，该地点记为 `tool_call_missing/invalid`，不得执行地图搜索，也不得把任务标为定位成功。若服务方支持，传 `tool_choice` 指定该函数并关闭并行工具调用；不论服务方是否遵守，服务端仍校验响应。工具调用、执行结果、输入地点序号和错误码进入 trace，避免只凭最终 POI 推断模型曾调用工具。

工具结果供下一步消歧的字段仅有候选序号、名称、地图类别、地址和行政区。完整 POI ID 与坐标留在服务端。地图调用失败与模型没调用工具须用不同错误码；前者可以重试地图请求，后者是模型/harness 协议失败，不得静默降级为代码搜索。此节点最多一次模型请求及一次合法地图调用/地点；可针对协议错误有限重试模型，但每次都重新验证调用且不得重复执行已成功的搜索。

若当前模型提供方无法可靠返回强制 function calling，生成任务明确报告该能力不兼容；不能悄悄切到代码搜索，因为那会掩盖本次要解决的失败模式。已有手动确认的地点跳过该节点，并在 trace 中记录 `skipped_manual_location`。

### D. 各 LLM 节点的输出方式

| LLM 节点 | 是否调用工具 | 约束方式 | 模型产物 |
|---|---:|---|---|
| 攻略语义提取 `activity_agent` | 否 | 严格 JSON Schema | 原文地点与约束，不含内部 ID。 |
| 地图搜索 `place_search_agent` | 是 | 严格 function calling | `search_map_places` 调用，参数仅地点序号与搜索词。 |
| 地点候选消歧 `place_match_agent` | 否 | 严格 JSON Schema | 候选序号或 `null`、简短依据；代码做最终校验。 |
| 每日排程 `itinerary_agent` | 否 | 严格 JSON Schema | 按输入顺序的 `placements`，不含身份和精确服务数据。 |

现有聊天 Agent 的工具调用属于另一条产品链路，本计划不修改其工具权限或协议。

### 攻略提取模型

输入：OCR 或用户粘贴的原文，作为不可信数据。输出仅 `places` 和 `constraints`。`places` 每项保留 `name/category/address_hint/day/time_window/duration_minutes/ticket_required/note/visit_tips` 等原文可证实信息；`constraints` 只包含类型、原文地点名称列表、原文依据和类型特定值。不要生成 `id`、`activity_id`、`place_status`、坐标或推荐地点。代码去重、生成稳定 ID、把约束名称映射为实体 ID，并在无法唯一映射时留下待处理问题。

### 排程模型

输入：服务端冻结的有序可排程地点、攻略约束、已选交通时间窗、住宿位置与审核反馈。输出仅与输入等长的 `placements` 数组：每项 `day`、`time_of_day`、简短 `reason`；未安排时两者均为 `null` 并说明原因。模型不得输出地点身份、时长、费用、路线或“约束已满足”结论。服务端按数组位置与冻结 ID 列表拼装 `stops/unscheduled`，插入固定锚点，再计算路线和审核。

### 草稿问题

代码生成 `issues`：`code`、`severity`、`activity_id`（内部）、`suggested_day`（若可推断）、`message`、`candidate_places`（若有）。LLM 不直接输出问题 ID 或严重度。前端按天展示待定位、未安排和冲突，并提供定位/移除/调整入口。

## 执行任务

### Task 1–3 实施记录（2026-10-30）

已实现严格 schema 请求、攻略提取响应二次校验和精确字段拒绝、排程从伪工具迁移到 schema，以及对应测试和识别规则更新。OpenAI 兼容接口和 Ollama 的请求格式使用 MockTransport 验证；不支持 schema 的 HTTP 错误不会降级为宽松 JSON。18 项排程测试、12 项客户端测试、60 项攻略相关测试通过；compileall 和 git diff --check 通过。尚未调用真实配置的模型，提供方是否实际执行严格 schema 仍须联调；本地校验会拒绝不符合契约的输出。

### Task 4–7 实施记录（2026-10-30）

已实现 `search_map_places` 的严格函数调用与执行校验、多 POI 的 schema 消歧、确定性类别校验、攻略一键生成图、默认住宿、无住宿的部分草稿、结果页位置修正与排除，以及 MySQL 持久任务和前端状态恢复。任务创建去重，提交在同一事务锁定计划和任务并比较原计划摘要，防止生成结果覆盖并发编辑。独立审查发现的 OCR 被旧文本覆盖、无住宿编辑崩溃、排除酒店被重新选择、候选类别校验缺失和结果页两次请求错误处理问题均已修复。

Ruling: 使用现有 MySQL 保存任务，继续以 FastAPI 后台线程执行，按当前单 API 进程运行；启动时将遗留 queued/running 标记失败，不自动重放付费模型调用。未来多进程部署需先改成带租约的独立 worker。Ollama 不支持 tool_choice 参数，客户端仅传其原生 tools，调用端仍严格校验 tool_calls；未调用工具时绝不执行地图搜索。

验证：108 项相关后端测试、18 项前端测试、Vite 生产构建、compileall 和 git diff --check 通过。新集成测试使用真实 HTTP 模型适配器与 MockTransport，覆盖“无调用即零地图请求”、合法调用仅执行一次、消歧拒绝错误类别、图片原文保留、无住宿拖动、后台失败不写计划、并发修改拒绝提交、事务提交和重启中断。未做真实 MySQL/模型/高德联调或浏览器人工验收，部署前仍需完成 Task 8 中对应实测。

### Task 1：严格 schema 模型客户端

**Files:** `service/utils/model_client.py`；`tests/test_model_client.py`。

- [ ] 测试 OpenAI 兼容请求确实传 `json_schema` 而非 `json_object`；Ollama 传 schema 对象；不支持时明确失败。
- [ ] 实现统一的 `chat(..., format=<JSON Schema>)` 契约，保留旧 `format="json"` 调用的兼容行为，禁止新规划节点使用旧行为。
- [ ] 运行 `python -m unittest tests.test_model_client -v`；核对已配置模型是否真实支持严格 schema，记录兼容性结果。

### Task 2：攻略提取的最小输出

**Files:** `service/travel/guide.py`、`.agents/skills/guide-recognition/SKILL.md`；`tests/test_guide_workflow.py`。

- [ ] 测试模型请求携带严格 schema、无 `tools`，输出多余 ID/坐标字段被拒绝；原文顺序、同名去重、约束映射由代码完成。
- [ ] 精简提示词和 schema；保留已有原文约束能力，服务端校验日期、时段、类别及引用名称。
- [ ] 运行 `python -m unittest tests.test_guide_workflow -v`。

### Task 3：排程提案改为 schema 输出

**Files:** `agent/itinerary.py`；`tests/test_itinerary_agent.py`、`tests/test_itinerary_output_contract.py`。

- [ ] 将现有函数调用测试改为 schema 响应测试：`tools=[]`，仅 `placements`，长度与输入相同，非法时间、额外字段和模型输出 ID 均拒绝。
- [ ] 保留现有冻结顺序和 `_validate_proposal` 的服务端组装；删除 `SUBMIT_ITINERARY_TOOL` 和“必须调用工具”的提示。
- [ ] 运行相关两个测试文件；更新旧输出契约文档。

### Task 4：默认参与、住宿与定位例外

**Files:** `service/travel/planning.py`、`service/travel/map_planning.py`；`tests/test_guide_workflow.py`、`tests/test_guide_graph_workflow.py`。

- [ ] 测试攻略地点默认参与、用户明确排除仍有效；模拟主题建议不自动纳入。
- [ ] 测试多日住宿：明确提到且唯一匹配的攻略酒店优先；否则从可靠的现有酒店候选中按确定性规则选一个并标记 `system_suggested`；无可靠候选则生成不含已确认住宿锚点的待处理草稿，最终确认受阻。
- [ ] 测试一次合法搜索调用返回单一可靠 POI 后自动定位、同名多 POI 进入模型消歧、无匹配进入待处理、用户手动位置与排除决定在重新识别时保留。
- [ ] 运行相关测试，避免改变既有 `draft` 手动路径。

### Task 4A：模型调用地图工具与多候选消歧

**Files:** `agent/planning_graph.py`、`service/travel/map_planning.py`、`service/travel/place_matching.py`、`service/utils/model_client.py`；`tests/test_guide_graph_workflow.py`、`tests/test_map_review.py`、`tests/test_model_client.py`。

- [ ] 先测 `place_search_agent` 请求包含唯一真实工具及强制选择参数；模型返回空/错误/重复 tool call 时地图工具零调用，trace 有明确错误码；合法调用才执行一次高德搜索。
- [ ] 实现工具调用解析、参数校验、地点序号映射、服务端城市注入、调用预算与结果记录；模型客户端支持 `tool_choice` 和禁用并行工具调用。
- [ ] 测试地图返回多个真实候选时调用 `place_match_agent`，请求为 `tools=[]` 且携带严格 schema；模型只输出候选序号或 `null` 与理由。
- [ ] 实现冻结候选列表、脱敏输入、序号到完整 POI 映射和服务端名称/类别/城市校验；通过后记录 `model_matched` 与理由。
- [ ] 测试景区与同名行政机关/商场可区分；几家没有线索的同名分店返回 `null`；越界序号、错误 schema、跨城市候选、模型超时均保留待处理状态。
- [ ] 增加一项从 `generate_from_guide` 走到模拟模型 HTTP 响应和模拟高德工具的集成测试，断言请求包含 `tools/tool_choice`、响应 `tool_calls` 被解析、工具只执行一次、结果进入消歧；不能只 mock `place_search_agent` 的最终返回值。
- [ ] 运行两个相关测试文件；Task 5 在 `place_agent` 后接入此节点，再进入排程。

### Task 5：LangGraph 单次生成路径

**Files:** `agent/planning_graph.py`；`tests/test_guided_graph.py`、`tests/test_guide_graph_workflow.py`。

- [ ] 新增 `generate_from_guide` action：图片走 OCR，文字跳过 OCR，汇合于提取、`place_search_agent` 工具调用、模型消歧、默认决策、排程、路线、审核。
- [ ] 将生成前缺坐标的硬错误改为该 action 的待处理项；仅已定位项进入正式路线；旧 `draft` 行为按回归测试保持。
- [ ] 审核反馈仅重排可排程项，最多三次；始终使用同一冻结 ID 顺序；更新输入版本则丢弃旧结果。
- [ ] 验证图片、文字、空攻略、模型格式错误、高德失败、单个歧义地点、审核仍有高严重度问题的 trace 和结果。

### Task 6：可靠运行与 API

**Files:** `service/api/server.py`、`service/storage/travel_db.py` 或现有任务存储模块；`tests/test_guided_api.py`。

- [ ] 新增创建任务、按任务 ID 查询状态的接口；状态至少包含 `queued/running/completed/failed`、当前阶段、错误、最终 `plan_id`。同一计划和输入摘要重复提交返回同一进行中任务。
- [ ] 在后台执行图；只在成功完成且计划版本未变化时提交结果，失败保留旧行程。刷新页面可恢复任务状态；任务不会因浏览器超时重复执行。
- [ ] 设计进程重启后的任务恢复/明确失败规则，并测试并发编辑导致旧结果不覆盖新计划。此任务须按项目当前部署方式选用持久任务实现，不能仅放进进程内字典。
- [ ] 运行 API 与持久化相关测试。

### Task 7：单一入口与结果页修正

**Files:** `frontend/src/GuidedPlanner.jsx`、`frontend/src/ReviewPage.jsx`、`frontend/src/planningValidation.js`、`frontend/tests/`。

- [ ] 攻略区域提供图片或文字输入和“生成每日行程”；图片上传不再自动启动独立识别请求。提交后进入任务进度，完成后打开结果页。
- [ ] 结果页按天显示正式安排与待处理项，标出攻略依据、系统假设和未安排原因；允许在此选择歧义 POI、调整日程、返回高级地点编辑。
- [ ] 生成前只校验输入合法性；最终确认仍按审核高严重度问题禁用。测试刷新恢复、重复点击、失败重试和未保存文字处理。
- [ ] 运行前端测试与构建。

### Task 8：端到端验收与文档

**Files:** `tests/test_guided_api.py`、`frontend/tests/`、`README.md`、相关 `docs/superpowers/specs/`。

- [ ] 用“多天攻略含顺序、用餐、酒店”验证一次提交直达草稿；仅有歧义地点时其他天仍可用；无地点时给清楚反馈；重新生成保留手动修改。
- [ ] 用真实已配置模型各跑一次提取与排程，验证 schema 兼容性及响应时间；记录不兼容提供方的错误表现。
- [ ] 更新用户流程和模型输出契约；运行后端相关测试、前端测试和构建，记录结果。

## Review Focus

1. 攻略写“第 4 天”，旅程只有 3 天：草稿显示约束冲突，不默默改到其他天（Task 2、5）。
2. 两家同名店：模型有原文区域线索时可选择并由代码校验；无区分线索时返回 `null`，其他地点照常成稿（Task 4A、5）。
3. 生成期间用户改动交通或酒店：旧任务不得覆盖新计划（Task 6）。
4. 攻略文字包含伪系统指令或内部 ID：当作数据；输出中的精确字段不被采用（Task 2、3）。
5. 模型/地图服务失败或前端刷新：旧行程仍可访问，任务状态可查询，重试不造成重复写入（Task 5、6、7）。
6. 模型未调用 `search_map_places`：地图调用计数必须为零，trace 明确显示工具协议失败；不能出现成功定位结果（Task 4A、5）。

## 建议实施顺序

先完成 Task 1–3 的模型契约，再做 Task 4–5 的完整草稿链路，最后接 Task 6–7 的可靠 API 与界面。Task 8 是最终验收。每个任务在相关测试通过后独立提交；不得因当前工作树已有未提交改动而覆盖或顺手提交无关文件。
