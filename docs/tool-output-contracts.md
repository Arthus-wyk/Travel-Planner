# 工具输出结构清单

按 2026-11-18 工作区源码整理。覆盖 26 个 MCP 工具与 7 个审核页本地工具，共 33 个不同名称。这里描述当前返回结构，尚未调整任何工具输出。

## 1. 返回链路与通用包装

业务函数返回 RawResult，经 MCP 传输或本地 ToolExecutor 执行后，统一成为下面的 AgentToolResult。后面工具表展示 RawResult，完整 Agent 输出的 data 等于该 RawResult。

```ts
type AgentToolResult = {
  tool: string;
  success: boolean;
  data: unknown;        // 原始业务返回对象整体，通常仍含 success
  message: string;      // 业务 message/error，缺失时为 ""
  error_type: string;   // 成功为 ""，业务失败缺失分类时为 "tool_error"
};
```

例如地点搜索的实际 Agent 输出具有两层 success。

```json
{
  "tool": "amap_search_places",
  "success": true,
  "data": {"success": true, "query": "公园", "places": [], "raw_places": []},
  "message": "",
  "error_type": ""
}
```

普通聊天执行节点额外添加 `operation_id: string`。审核子 Agent 额外添加下面两项。

```ts
type ReviewToolFeedback = AgentToolResult & {
  arguments: Record<string, unknown>;
  budget: {
    remaining_tool_calls: number;
    retry_remaining: Record<string, number>;
    retry_exhausted: string[];
  };
};
```

MCP 传输结果还可能包含 `content` 文本块、`structuredContent` 与 `isError`。适配器优先取 structuredContent，否则拼接文本，之后交给 normalize_result。旧的 `{status:"success",result:"JSON字符串"}` 也会解包。MCP 的 isError 优先于业务 success。

来源为 [统一结果](../tooling/results.py)、[MCP 适配](../tooling/mcp.py)、[执行器](../agent/chat/executor.py)、[普通聊天节点](../agent/chat/nodes.py)、[审核子 Agent](../agent/chat/review_children.py)。

### 通用失败结构

```ts
type BusinessFailure = {
  success: false;
  message: string;
  error_type?: string;
  // 各工具可能另加 valid_categories、conflicting_item_id 等字段
};
```

- 工具不存在、参数校验和权限失败由执行器直接返回 `AgentToolResult`，通常 `data=null`，error_type 分别为 unknown_tool、validation_error、permission_denied。
- handler 抛异常时执行器返回 `success=false,data=null,error_type=异常类名,message=异常说明`。
- 数据库业务函数通常返回 `{success:false,message:"Database error..."}`，未单独指定 error_type。
- 高德失败可能为 `{success:false,message,http_status,retry_after}`，或 `{success:false,message,infocode}`，或只有 `{success:false,message}`。http_status 是整数，retry_after 可以为空，infocode 为提供方错误码字符串。
- 审核运行时还会返回 timeout、budget_exhausted、retry_parameters_required、decision_required、PlanConflict。这些属于执行边界错误。

未声明的业务错误字段通常缺席，不能假设每个失败结果都有统一错误码、retryable 或 details。

## 2. 26 个 MCP 工具

下面所有表内结构都是业务原始返回值。`T[]` 表示数组，`?` 表示字段可能缺席，`null` 表示该字段明确可能为空。涉及已有行程对象的输出保留其历史附加字段。

### 行程计划工具，6 个

| 工具 | 正常返回结构 | 特殊失败与说明 |
|---|---|---|
| create_travel_plan | `{success:true,plan:Plan}` | 计划已存在、日期/人数/预算/列表非法、数据库错误。直接创建的 plan 没有 planning 字段 |
| update_travel_profile | `{success:true,plan:Plan}` | 未提供更新、无有效非空更新、非法字段或值时通常带 `error_type:"validation_error"` |
| get_travel_plan | `{success:true,plan:Plan}` | 计划不存在或数据库错误 |
| summarize_travel_plan | `{success:true,summary:PlanSummary}` | 计划不存在或数据库错误 |
| list_travel_plans | `{success:true,plans:PlanListEntry[]}` | 数据库错误 |
| delete_travel_plan | `{success:true,message:string}` | 计划不存在或数据库错误，成功不返回 plan 或 deleted_plan_id |

注册来源为 [计划工具](../tools/plan/mcp_tools.py)，实际业务实现在 [service/mcp/server.py](../service/mcp/server.py)。

### 日程、路线及审核工具，8 个

| 工具 | 正常返回结构 | 特殊失败与说明 |
|---|---|---|
| add_itinerary_item | `{success:true,item:ItineraryItem}` | 时间冲突、日期/费用/时长非法、计划不存在、数据库错误 |
| update_itinerary_item | `{success:true,item:ItineraryItem}` | 返回修改后的完整 item，保留已有扩展字段；项目不存在、非法更新或时间冲突时失败 |
| delete_itinerary_item | `{success:true,deleted_item_id:string,affected_days:number[],affected_item_ids:string[]}` | 同时移除关联路线；返回受影响项目与日期，不返回完整 plan |
| move_itinerary_item | `{success:true,item:ItineraryItem,affected_days:number[],affected_item_ids:string[]}` | 冲突失败可能额外包含 `conflicting_item_id:string` |
| list_itinerary | `{success:true,itinerary:ItineraryItem[]}` | 可按 day 筛选，按日期、时间及 item_id 排序 |
| update_itinerary_routes | `{success:true,routes:StoredRoute[]}` | routes 输入非法、端点不属于行程、交通模式非法时失败；返回本次保存的路线 |
| reconcile_travel_plan | `{success:boolean,plan_id:string,affected_days:number[],affected_item_ids:string[],routes:StoredRoute[],route_errors:object[],audit:Audit,suggestions:string[]}` | 部分路线失败时 success=false，但已刷新的路线和审核仍存在；部分刷新结果可能已经保存 |
| audit_travel_plan | `Audit` | 工具执行成功时始终 success=true，即使存在高风险问题；业务审核状态看 status 与 issues |

注册来源为 [日程工具](../tools/itinerary/mcp_tools.py)，实际业务实现在 [service/mcp/server.py](../service/mcp/server.py)。

### 高德工具，5 个

| 工具 | 正常返回结构 | 特殊失败与说明 |
|---|---|---|
| amap_search_places | `{success:true,query:string,places:CompactPlace[]}` | 无匹配地点仍可 success=true 且 places=[]；验证或上游错误返回通用高德失败 |
| amap_search_hotels | 与 amap_search_places 相同 | query 是拼接后的“目的地 + 搜索条件 + 酒店”，没有单独 hotels 字段 |
| amap_compute_route | `CompactComputedRoute` | 无路线时 `{success:false,message}`；返回耗时最短的一条路线摘要 |
| amap_geocode | `{success:boolean,geocodes:CompactGeocode[],coordinate_system:"GCJ-02",message:string}` | 无匹配时 success=false，geocodes=[] |
| amap_static_map | `{success:true,center:string,coordinate_system:"GCJ-02",image_cache_key:string,image_url:string}` | center 为 "经度,纬度"；image_url 为应用 API 的相对图片地址，验证与上游错误返回失败 |

注册来源为 [地图工具](../tools/maps/mcp_tools.py)，工具返回值在 [地图工具服务](../tools/maps/service.py) 裁剪。内部 [地图业务接口](../service/mcp/server.py) 和 [高德适配](../service/integrations/travel_amap.py) 继续提供规划及画图使用的完整对象。

### 推荐与费用工具，4 个

| 工具 | 正常返回结构 | 特殊失败与说明 |
|---|---|---|
| add_recommendation | `{success:true,recommendation:Recommendation}` | 标签、费用、优先级、计划或数据库校验失败 |
| update_recommendation | `{success:true,recommendation:Recommendation}` | 返回完整更新对象，保留已有扩展字段；无更新、项目不存在或非法字段时失败 |
| rank_recommendations | `{success:true,ranked:RankedCandidate[],count:number,scoring_notes:string[]}` | count 是评分前候选总数，可能大于截断后的 ranked.length；candidate 原样嵌入 |
| add_budget_item | `{success:true,budget_item:BudgetItem}` | 费用非法、计划不存在或数据库错误 |

注册来源为 [推荐工具](../tools/recommendation/mcp_tools.py)，评分来源为 [排序器](../service/travel/recommendation_ranker.py)。

### 记忆与校验工具，3 个

| 工具 | 正常返回结构 | 特殊失败与说明 |
|---|---|---|
| read_travel_preference_profile | `{success:true,path:string,profile_markdown:string,profile:PreferenceProfile}` | 本地文件不存在时会创建；文件系统异常通过执行边界报告 |
| update_travel_preference_profile | `{success:true,path:string,changed:Partial<PreferenceProfile>,profile_markdown:string,profile:PreferenceProfile}` | 类别非法或 updates 非对象时 `{success:false,message,valid_categories:Record<string,string>}` |
| verify_last_tool_result | `{success:true,verification:Verification}` | arguments 非对象时失败；外层 success=true 只代表校验器执行完成，verification.status 仍可能 failed |

注册来源为 [记忆工具](../tools/memory/mcp_tools.py)，实现来源为 [记忆存储](../service/storage/memory_store.py) 和 [结果校验](../service/travel/tool_verification.py)。

## 3. 审核页本地工具，7 个

这组工具由 review_registry 装配，没有通过上述 26 个 MCP 工具注册。默认只读 registry 有 5 个，writable=true 且没有 draft 时再加入两个写工具。父 Agent 的子任务委派由 Scheduler 完成，目前没有另行注册名为 delegate 的工具。

| 工具 | 业务原始返回结构 | 说明 |
|---|---|---|
| get_review_context | `CompactReviewContext` | 未指定 day 返回全程摘要；指定 day 限定日程、路线与相关问题，预算保持整趟口径 |
| search_review_places | `{success:true,query:string,places:CompactPlace[]}` | 与 MCP 搜索共用裁剪规则；失败字段保持高德返回值 |
| search_review_hotels | `{success:true,query:string,places:CompactPlace[]}` | 同样使用 places 字段 |
| compare_review_route | `CompactComputedRoute` | 端点必须来自当前行程；参数错误可抛异常，由执行器包装为失败 |
| preview_review_changes | `ReviewSnapshot & {accepted:boolean,blocking_issues:Issue[],optimization_message:string|null,draft_plan?:Plan,operations?:Operation[]}` | draft 模式额外返回 draft_plan 与 operations；accepted=false 仍是一次成功执行的试算 |
| apply_review_changes | 成功为 `ReviewSnapshot & {applied:boolean,optimization_message:string|null}` | 拒绝为 `{success:false,error_type:"review_rejected",message:string,preview:ReviewSnapshot & {accepted:false,blocking_issues:Issue[]}}` |
| optimize_review_routes | 与 apply_review_changes 相同 | applied=false 表示没有应用优化；错误可能包含 PlanConflict 或保存后读取失败 |

Snapshot 类原始返回对象没有 success 字段。normalize_result 在没有显式失败信号时判定执行成功。preview 与两个保存工具的 audit 仍是完整 Audit。

来源为 [审核 registry](../tools/review.py) 和 [试算与验收](../service/travel/review.py)。

## 4. 共享嵌套结构

以下是便于讨论的 TypeScript 风格结构说明，不是仓库已经强制执行的完整输出 Schema。开放字段用 unknown 表示，历史数据和规划生成的数据可能带额外字段。

### Plan、PlanSummary 与 PlanListEntry

```ts
type Plan = {
  plan_id: string; title: string; destination: string;
  start_date: string; end_date: string; departure_city: string;
  travelers: number; budget: number; currency: string;
  travel_style: string; interests: string[]; constraints: string[];
  pace: string; notes: string; created_at: string; updated_at: string;
  itinerary: ItineraryItem[]; routes: StoredRoute[];
  recommendations: Recommendation[]; budget_items: BudgetItem[];
  planning?: Record<string, unknown>;
};

type PlanSummary = {
  plan_id: string; destination: string; dates: string;
  travelers: number; budget: number; currency: string;
  interests: string[]; constraints: string[]; scheduled_days: number[];
  itinerary_item_count: number; recommendation_count: number; budget_item_count: number;
  estimated_itinerary_cost: number; estimated_recommendation_cost: number;
  budget_item_total: number;
  tracked_cost_range: {min: number; max: number};
  remaining_budget_range: {min: number; max: number};
  remaining_budget_after_budget_items: number;
};

type PlanListEntry = {
  plan_id: string; title: string; destination: string; dates: string;
  travelers: number; budget: number; currency: string; updated_at: string;
};
```

dates 在这两个摘要里是 `YYYY-MM-DD to YYYY-MM-DD` 字符串。planning 是持久化的规划状态，常含 stage、activities、酒店/交通候选、已选交通、购买记录、每天出发时间及未排定项目等。get_travel_plan 与 update_travel_profile 会直接返回这整个状态，工具没有对它投影或裁剪。读取数据库时 planning 通常存在，值可能是空对象。完整 Plan 会把规划状态、日程、路线和候选同时交给模型。

来源为 [数据库读取](../service/storage/travel_db.py)、[创建与摘要](../service/mcp/server.py)、[规划状态](../service/travel/planning.py)。

### 日程、推荐、预算项

```ts
type ItineraryItem = {
  item_id: string; day: number; time_of_day: string;
  title: string; location: string; category: string;
  duration_minutes: number|null; estimated_cost: number;
  booking_required: boolean; notes: string;
  // 上面是 add_itinerary_item 的构造字段。
  // 规划生成的项目还可能含下面这些字段，部分历史项目也可能缺少上面的备注字段。
  reference_id?: string; place?: Place; day_offset?: number;
  anchor_role?: string; arrival_location?: string; departure_location?: string;
  hotel_name?: string; ticket_required?: boolean|null; visit_tips?: string;
  [key: string]: unknown;
};

type Recommendation = {
  recommendation_id: string; name: string; category: string; location: string;
  why: string; tags: string[]; estimated_cost: number;
  priority: string; notes: string;
  [key: string]: unknown;
};

type BudgetItem = {
  budget_item_id: string; category: string; amount: number; description: string;
};

type RankedCandidate = {
  score: number; name: string|null; category: string|null;
  reasons: string[]; tradeoffs: string[];
  candidate: Record<string, unknown>;
};
```

priority 的业务允许值为 low、medium、high。ranking 的 name/category 依赖调用方提供的数据，缺失时可为空。time_of_day 通常为 HH:MM，读到的历史值仍由上层校验处理。

### Place 与地图路线

高德工具向 Agent 返回以下紧凑结构。空值的候选属性省略，费用为 0 时保留。

```ts
type CompactPlace = {
  place_id?:string; name?:string; address?:string;
  latitude?:number; longitude?:number; coordinate_system?:"GCJ-02";
  types?:string[]; rating?:number; cost_per_person?:number;
  user_rating_count?:number; price_level?:unknown; business_status?:string;
};
type CompactComputedRoute = {
  success:true; origin:string; destination:string; travel_mode:string;
  distance_meters:number; duration:string; fare:number|null;
};
type CompactGeocode = {
  formatted_address?:string; location?:string; province?:unknown;
  city?:unknown; district?:unknown; level?:unknown;
};
```

静态图使用 `/api/maps/images/{image_cache_key}` 引用已有图片缓存。MCP 调用方需用该应用 API 的地址解析相对路径。模型结果中不再携带图片 base64。业务函数与持久化对象仍可能使用下面的完整类型。

```ts
type Place = {
  place_id: string|null; name: string|null; address: string;
  latitude: number; longitude: number; coordinate_system: "GCJ-02";
  citycode: string|null; adcode: string|null;
  rating: number|null; cost_per_person: number|null;
  user_rating_count: null; price_level: null; business_status: null;
  types: string[]; amap_uri: string;
};

type ComputedRoute = {
  success: true; origin: string; destination: string; travel_mode: string;
  distance_meters: number; duration: string;  // duration 如 "1469s"
  fare: number|null; static_duration: unknown|null; localized_values: unknown|null;
  legs: object[];
  raw_routes: {
    distanceMeters: number; duration: string; legs: object[];
    source: string; amap_route: object; fare: number|null;
  }[];
};

type StoredRoute = {
  origin_item_id: string; destination_item_id: string; travel_mode: string;
  route_id: string; status: string; updated_at?: string;
  origin?: string; destination?: string; distance_meters?: number|null;
  duration?: string|null; static_duration?: unknown|null; localized_values?: unknown|null;
  [key: string]: unknown;
};
```

ComputedRoute 中 static_duration、localized_values 在当前高德转换函数没有赋值，因此通常是 null。StoredRoute 由 update_itinerary_routes 复制输入后补 route_id、status、updated_at，没有固定允许的输出字段全集。reconcile 生成另一种较小的 StoredRoute。规划器还可能使用 duration_minutes、intercity、legs、route_options 等扩展字段。

Place 的 citycode/adcode 等来自高德原始字段，缺失或历史上游值可能有差异，上述类型表达主要正常形态，当前代码没有用输出 Schema 保证这些上游字段类型。原始 legs 在不同交通方式下分别来自 steps 或 segments，结构也不同。不要把地图计算结果的秒字符串与规划器的分钟数混用。

### Audit、Issue、预算明细

```ts
type Audit = {
  success: true; plan_id: string|null; status: "ok"|"needs_attention";
  trip_days: number|null;
  metrics: {
    itinerary_item_count: number; recommendation_count: number; budget_item_count: number;
    estimated_itinerary_cost: number; estimated_recommendation_cost: number;
    budget_item_total: number; tracked_cost_range: {min:number;max:number}; budget:number;
  };
  issues: Issue[]; budget: GuidedBudget|null; suggestions: string[];
  guide_constraints: {
    type:string; activity_ids:string[]; source:string;
    satisfied:boolean; hard:boolean; verified:true; reason:string;
  }[];
};

type Issue = {
  type:string; severity:string; message:string;
  target:string|string[]|number|null;
};

type GuidedBudget = {
  items: {
    reference_id:string; label:string; category:string; estimate:number;
    actual_total:number|null; counted_total:number; purchased:boolean;
    source?:string; basis?:string;
    retained_expense?:boolean; bookable?:boolean; selection_changed?:boolean;
    cost_unknown?:boolean; [key:string]:unknown;
  }[];
  estimated_total:number; paid_total:number; remaining:number;
  has_possible_duplicates:boolean;
  has_unknown_ticket_costs:boolean; has_unknown_hotel_costs:boolean;
  has_unknown_transport_costs:boolean; has_unknown_food_costs:boolean;
};
```

Audit 的 budget 在 review/confirmed 阶段是 GuidedBudget，其他阶段为 null。metrics.budget 是预算额度数字。Issue 的 severity 目前为 high/medium/low 等业务值，没有输出枚举约束。账目 items 继承不同费用来源的字段，未采用封闭模型。

来源为 [确定性审核](../service/travel/itinerary_audit.py) 与 [预算汇总](../service/travel/planning.py)。

### 审核上下文

```ts
type ReviewSnapshot = {
  plan_version:string; stage:string; destination:string;
  start_date:string|null; end_date:string|null;
  itinerary:ItineraryItem[]; routes:StoredRoute[]; audit:Audit;
};

type CompactReviewContext = {
  plan_version:string; stage:string; destination:string; day?:number;
  start_date?:string|null; end_date?:string|null;
  itinerary: {
    item_id:string; day:number; title:string;
    time_of_day?:string; location?:string; category?:string; duration_minutes?:number|null;
    reference_id?:string; day_offset?:number; anchor_role?:string;
    estimated_cost?:number; visit_tips?:string; ticket_required?:boolean|null;
  }[];
  routes: {
    origin_item_id:string; destination_item_id:string; travel_mode?:string;
    duration?:string; duration_minutes?:number; fare?:number|null;
    status?:string; source?:string; intercity?:boolean;
  }[];
  audit:{
    issues:Issue[];
    budget:{
      limit:number|null; estimated_total?:number; remaining?:number;
      has_possible_duplicates?:boolean;
      has_unknown_ticket_costs?:boolean; has_unknown_hotel_costs?:boolean;
      has_unknown_transport_costs?:boolean; has_unknown_food_costs?:boolean;
    }|null;
  };
};

type Operation = {
  action:string;
  payload:Record<string,unknown>; // 实际按 action 选择独立输入 Schema
};
```

get_review_context 的单日和全程结果均按字段白名单裁剪，保留日期范围。日程不包含嵌套 POI、地图链接或长备注，路线不包含 legs、route_options 和原始上游对象，预算不包含逐项账目。reference_id、原费用、编辑提示、门票状态、锚点与跨日信息用于保持编辑依据。完整 Audit 的 metrics、status、suggestions、guide_constraints 不进入该工具的上下文结果。preview 与保存工具仍按上方 ReviewSnapshot 描述，draft_plan 包含完整计划。

### 偏好与校验结论

```ts
type PreferenceProfile = {
  travel_interests:string[]; budget:string[]; trip_length:string[];
  hotel:string[]; departure_city:string[]; pace:string[];
  restaurant:string[]; constraints:string[]; notes:string[];
};

type Verification =
  | {status:"passed"|"warning";tool:string;message:string;warnings:string[];checks:string[]}
  | {status:"failed";tool:string;message:string;retryable:boolean;checks:string[]}
  | {status:"warning";tool:string;message:string;raw_result:string};
```

raw_result 是非 JSON 结果的最多 1000 字符摘录。valid_categories 的值是分类显示名，例如 `{"hotel":"酒店偏好"}`。画像更新成功时 changed 只包含本次新增的偏好。

## 5. 调整输出时需要同步检查的消费位置

1. 统一包装和错误判定。normalize_result 当前将整个业务返回对象放入 data。直接改为只保留内层 payload 会改变所有调用方的访问路径。
2. 普通聊天节点。nodes.execute_tool、verify_tool 依赖 success、data、message，并将 operation_id 与结果传入模型。
3. 审核子 Agent。_remember_result 直接读取 data.places、data.routes，并保存 compare_review_route 的整个 data。_parse_result 通过 place_id 验证模型选中的候选。
4. 审核父 Agent、Workflow 和 HTTP。依赖 plan_version、accepted、blocking_issues、applied、draft_plan、operations 等具体字段。preview 执行成功和提案通过验收是两个判断。
5. 行程生成、地图及规划服务。直接调用 amap_search_places、amap_compute_route 等 Python 函数，因此修改业务原始返回值会同时影响工具之外的调用者。
6. 校验器。verify_last_tool_result 及应用内 verify_tool_result 直接读取业务 success、plan、places、issues、route_errors，调整返回路径需要同步修改。
7. 测试夹具与前端。审核 HTTP、模型客户端和地图/规划用例都使用现有字段。前端的候选卡片、试算预览、地图和预算也沿用这些数据。

来源为 [普通节点](../agent/chat/nodes.py)、[审核子循环](../agent/chat/review_children.py)、[审核 Workflow](../agent/chat/review_workflow.py)、[审核交互](../service/travel/review_interaction.py)、[地图工具](../tools/review.py)。

## 6. 当前契约中值得重点决定的差异

- success 可能表示执行成功，也可能表示全部路线刷新成功。审核通过与否另用 accepted/status/verification.status 表示。
- 业务失败的 error_type、message、retryable 等字段不统一。reconcile 部分失败时顶层可能没有 message，错误原因只在 route_errors 内。
- 搜索没有结果仍可成功，酒店搜索使用 places 字段。地理编码没有匹配则失败。
- 大量接口返回整份 Plan、行程快照、原始 POI、raw_routes、图片 base64 或完整候选，模型输入体积随内容增长。
- StoredRoute、planning、raw_places、legs、candidate 和某些账目字段是开放对象。当前工具定义有 input_schema，ToolSpec 没有逐工具 output_schema，现有 TypedDict 也只覆盖少数字段。
- 审核 Agent 的结构化输出 Schema 约束模型生成的决策，工具业务返回值需要另行定义输出契约。

## 7. 核对方式与边界

逐项读取注册表、业务函数返回分支、normalize_result 与直接消费位置，并核对注册名称覆盖。没有调用写库、记忆更新、真实高德或模型接口。高德原始对象与持久化规划扩展字段按开放结构列出，没有把示例形态当成已强制执行的 Schema。
