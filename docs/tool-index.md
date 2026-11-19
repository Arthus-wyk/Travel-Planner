# 工具实现和输入输出索引

此表从当前注册声明核对，覆盖 26 个公开工具、7 个请求私有审核工具和 1 个委派工具。
输入模型声明字段、类型、默认值和校验。输出列为业务 data 模型，完整 MCP 输出还包含共享结果信封。

## 实际业务代码位置

公开工具的业务入口目前集中在 [service/mcp/server.py](../service/mcp/server.py)，继续调用 service/travel、service/storage 和 service/integrations。
agent/tools/implementations 下的 plan、itinerary、recommendation、memory 的 service.py 主要重新导出这些函数，mcp_tools.py 将函数与输入输出模型绑定。
maps/service.py 额外负责压缩地图返回字段。审核工具在 implementations/review.py 内绑定行程并调用旅行服务，委派执行逻辑位于 agent/chat/review/parent.py。
因此下表的“实际业务入口”指向函数定义处，沿用现有调用路径。

例如 create_travel_plan 的调用路径为 catalog.public_registry → implementations/plan/mcp_tools.specs → implementations/plan/service 导入的 create_travel_plan → service/mcp/server.create_travel_plan。

- [公开注册总入口](../agent/tools/catalog.py) 汇总各领域的 mcp_tools.py。
- [MCP 服务端与私有会话](../agent/tools/mcp.py) 生成 tools/list 的 inputSchema 和 outputSchema。
- [Dispatcher](../agent/tools/dispatcher.py) 执行输入校验、权限检查、有限重试和输出校验。
- [共享 ToolResult](../agent/tools/schemas/outputs.py) 使用 status、data、error。success 为完整结果，partial 带有效结果和缺失项，error 为 data=null。
- [共享错误](../agent/tools/schemas/errors.py) 定义错误码、retryable 和 details。
- [审核注册](../agent/tools/implementations/review.py) 绑定当前行程。apply/optimize 仅在 writable 且非草稿会话时注册。
- [委派注册](../agent/tools/implementations/delegation.py) 用当前允许角色生成动态输入模型。

| 工具 | 实际业务入口 | 输入模型 | data 输出模型 |
| --- | --- | --- | --- |
| `create_travel_plan` | [create_travel_plan](../service/mcp/server.py#L278) | [CreateTravelPlanInput](../agent/tools/schemas/plan.py#L18) | [CreateTravelPlanOutput](../agent/tools/schemas/plan.py#L35) |
| `update_travel_profile` | [update_travel_profile](../service/mcp/server.py#L383) | [UpdateTravelProfileInput](../agent/tools/schemas/plan.py#L41) | [UpdateTravelProfileOutput](../agent/tools/schemas/plan.py#L46) |
| `get_travel_plan` | [get_travel_plan](../service/mcp/server.py#L1278) | [GetTravelPlanInput](../agent/tools/schemas/plan.py#L52) | [GetTravelPlanOutput](../agent/tools/schemas/plan.py#L56) |
| `summarize_travel_plan` | [summarize_travel_plan](../service/mcp/server.py#L1290) | [SummarizeTravelPlanInput](../agent/tools/schemas/plan.py#L62) | [SummarizeTravelPlanOutput](../agent/tools/schemas/plan.py#L66) |
| `list_travel_plans` | [list_travel_plans](../service/mcp/server.py#L1330) | [ListTravelPlansInput](../agent/tools/schemas/plan.py#L72) | [ListTravelPlansOutput](../agent/tools/schemas/plan.py#L76) |
| `delete_travel_plan` | [delete_travel_plan](../service/mcp/server.py#L1342) | [DeleteTravelPlanInput](../agent/tools/schemas/plan.py#L82) | [DeleteTravelPlanOutput](../agent/tools/schemas/plan.py#L86) |
| `add_itinerary_item` | [add_itinerary_item](../service/mcp/server.py#L501) | [AddItineraryItemInput](../agent/tools/schemas/itinerary.py#L40) | [AddItineraryItemOutput](../agent/tools/schemas/itinerary.py#L53) |
| `update_itinerary_item` | [update_itinerary_item](../service/mcp/server.py#L609) | [UpdateItineraryItemInput](../agent/tools/schemas/itinerary.py#L59) | [UpdateItineraryItemOutput](../agent/tools/schemas/itinerary.py#L65) |
| `delete_itinerary_item` | [delete_itinerary_item](../service/mcp/server.py#L776) | [DeleteItineraryItemInput](../agent/tools/schemas/itinerary.py#L71) | [DeleteItineraryItemOutput](../agent/tools/schemas/itinerary.py#L76) |
| `move_itinerary_item` | [move_itinerary_item](../service/mcp/server.py#L814) | [MoveItineraryItemInput](../agent/tools/schemas/itinerary.py#L84) | [MoveItineraryItemOutput](../agent/tools/schemas/itinerary.py#L91) |
| `list_itinerary` | [list_itinerary](../service/mcp/server.py#L1063) | [ListItineraryInput](../agent/tools/schemas/itinerary.py#L99) | [ListItineraryOutput](../agent/tools/schemas/itinerary.py#L104) |
| `update_itinerary_routes` | [update_itinerary_routes](../service/mcp/server.py#L873) | [UpdateItineraryRoutesInput](../agent/tools/schemas/itinerary.py#L110) | [UpdateItineraryRoutesOutput](../agent/tools/schemas/itinerary.py#L115) |
| `reconcile_travel_plan` | [reconcile_travel_plan](../service/mcp/server.py#L958) | [ReconcileTravelPlanInput](../agent/tools/schemas/itinerary.py#L121) | [ReconcileTravelPlanOutput](../agent/tools/schemas/itinerary.py#L128) |
| `audit_travel_plan` | [audit_travel_plan](../service/mcp/server.py#L87) | [AuditTravelPlanInput](../agent/tools/schemas/itinerary.py#L140) | [AuditTravelPlanOutput](../agent/tools/schemas/itinerary.py#L144) |
| `amap_search_places` | [amap_search_places](../agent/tools/implementations/maps/service.py#L133) | [AmapSearchPlacesInput](../agent/tools/schemas/maps.py#L20) | [AmapSearchPlacesOutput](../agent/tools/schemas/maps.py#L30) |
| `amap_search_hotels` | [amap_search_hotels](../agent/tools/implementations/maps/service.py#L172) | [AmapSearchHotelsInput](../agent/tools/schemas/maps.py#L37) | [AmapSearchHotelsOutput](../agent/tools/schemas/maps.py#L45) |
| `amap_compute_route` | [amap_compute_route](../agent/tools/implementations/maps/service.py#L195) | [AmapComputeRouteInput](../agent/tools/schemas/maps.py#L52) | [AmapComputeRouteOutput](../agent/tools/schemas/maps.py#L64) |
| `amap_geocode` | [amap_geocode](../agent/tools/implementations/maps/service.py#L255) | [AmapGeocodeInput](../agent/tools/schemas/maps.py#L75) | [AmapGeocodeOutput](../agent/tools/schemas/maps.py#L80) |
| `amap_static_map` | [amap_static_map](../agent/tools/implementations/maps/service.py#L265) | [AmapStaticMapInput](../agent/tools/schemas/maps.py#L88) | [AmapStaticMapOutput](../agent/tools/schemas/maps.py#L96) |
| `add_recommendation` | [add_recommendation](../service/mcp/server.py#L1091) | [AddRecommendationInput](../agent/tools/schemas/recommendation.py#L11) | [AddRecommendationOutput](../agent/tools/schemas/recommendation.py#L23) |
| `update_recommendation` | [update_recommendation](../service/mcp/server.py#L1161) | [UpdateRecommendationInput](../agent/tools/schemas/recommendation.py#L29) | [UpdateRecommendationOutput](../agent/tools/schemas/recommendation.py#L35) |
| `rank_recommendations` | [rank_recommendations](../service/mcp/server.py#L95) | [RankRecommendationsInput](../agent/tools/schemas/recommendation.py#L41) | [RankRecommendationsOutput](../agent/tools/schemas/recommendation.py#L48) |
| `add_budget_item` | [add_budget_item](../service/mcp/server.py#L1239) | [AddBudgetItemInput](../agent/tools/schemas/recommendation.py#L56) | [AddBudgetItemOutput](../agent/tools/schemas/recommendation.py#L63) |
| `read_travel_preference_profile` | [read_travel_preference_profile](../service/mcp/server.py#L60) | [ReadTravelPreferenceProfileInput](../agent/tools/schemas/memory.py#L11) | [ReadTravelPreferenceProfileOutput](../agent/tools/schemas/memory.py#L15) |
| `update_travel_preference_profile` | [update_travel_preference_profile](../service/mcp/server.py#L69) | [UpdateTravelPreferenceProfileInput](../agent/tools/schemas/memory.py#L23) | [UpdateTravelPreferenceProfileOutput](../agent/tools/schemas/memory.py#L28) |
| `verify_last_tool_result` | [verify_last_tool_result](../service/mcp/server.py#L119) | [VerifyLastToolResultInput](../agent/tools/schemas/memory.py#L37) | [VerifyLastToolResultOutput](../agent/tools/schemas/memory.py#L43) |
| `get_review_context` | [review.py / review_context_for_day](../agent/tools/implementations/review.py) | [GetReviewContextInput](../agent/tools/schemas/review.py#L59) | [GetReviewContextOutput](../agent/tools/schemas/review.py#L103) |
| `preview_review_changes` | [review.py / preview](../agent/tools/implementations/review.py) | [PreviewReviewChangesInput](../agent/tools/schemas/review.py#L67) | [PreviewReviewChangesOutput](../agent/tools/schemas/review.py#L116) |
| `search_review_places` | [review.py / search](../agent/tools/implementations/review.py) | [SearchReviewPlacesInput](../agent/tools/schemas/review.py#L89) | [SearchReviewPlacesOutput](../agent/tools/schemas/review.py#L134) |
| `search_review_hotels` | [review.py / search_hotels](../agent/tools/implementations/review.py) | [SearchReviewHotelsInput](../agent/tools/schemas/review.py#L93) | [SearchReviewHotelsOutput](../agent/tools/schemas/review.py#L141) |
| `compare_review_route` | [review.py / compare_route](../agent/tools/implementations/review.py) | [CompareReviewRouteInput](../agent/tools/schemas/review.py#L97) | [CompareReviewRouteOutput](../agent/tools/schemas/review.py#L145) |
| `apply_review_changes` | [review.py / save](../agent/tools/implementations/review.py) | [ApplyReviewChangesInput](../agent/tools/schemas/review.py#L81) | [ApplyReviewChangesOutput](../agent/tools/schemas/review.py#L156) |
| `optimize_review_routes` | [review.py / save](../agent/tools/implementations/review.py) | [OptimizeReviewRoutesInput](../agent/tools/schemas/review.py#L85) | [OptimizeReviewRoutesOutput](../agent/tools/schemas/review.py#L161) |
| `delegate_task` | [ReviewDelegations.execute](../agent/chat/review/parent.py) | [DelegateTaskInput](../agent/tools/schemas/delegation.py#L20) | [DelegateTaskOutput](../agent/tools/schemas/delegation.py#L42) |

## 结构化模型输出

| 用途 | 契约 | 调用入口 |
| --- | --- | --- |
| 指代改写 | REWRITE_SCHEMA | agent/chat/runtime/rewrite.py |
| 审核父角色 | PARENT_SCHEMA | agent/chat/review/parent.py，Schema 位于 agent/chat/schemas/review.py |
| 审核子角色 | child_schema(role, operations_schema) | agent/chat/review/children.py，Schema 位于 agent/chat/schemas/review.py |
| 长短期记忆 | MEMORY_SCHEMA | agent/chat/runtime/memory.py |
| 攻略提取 | GUIDE_SCHEMA | service/travel/guide.py |
| 行程编排 | ITINERARY_SCHEMA，附本次日期和位置约束 | agent/planning/implementations/itinerary.py |
| 每日路书 | 本次 days 动态 Schema | agent/planning/implementations/roadbook.py |

公共模型客户端在所有字典 Schema 请求的系统消息中提供完整契约，保留原生 response_format/format，并在返回前校验。
Ollama 的结构化流等待完整输出再验证。公共 Agent 模型请求遇到 empty_output、invalid_json、invalid_schema 时，在原请求预算大于一次的条件下最多纠正一次，纠正请求不提供业务工具。
协议错误、截断、拒答继续走各自失败路径。攻略、行程、路书沿用已有业务校验和恢复边界。
json_repair 仅尝试语法恢复，修复结果仍须通过原 Schema，POI、操作和保存授权继续经过领域校验。
