"""Register typed recommendation tools with their existing domain handlers."""
from agent.tools.implementations.recommendation import service
from agent.tools.schemas import recommendation as schemas
from agent.tools.schemas.inputs import ToolSpec

TOOL_NAMES = ("add_recommendation", "update_recommendation", "rank_recommendations", "add_budget_item")
TOOL_DOMAINS = {"add_recommendation": {"recommendation"}, "update_recommendation": {"recommendation"}, "rank_recommendations": {"recommendation", "hotel", "place", "restaurant"}, "add_budget_item": {"budget"}}

TOOL_RISKS = {}


def specs() -> list[ToolSpec]:
    contracts = (
        ("add_recommendation", schemas.AddRecommendationInput, schemas.AddRecommendationOutput, False),
        ("update_recommendation", schemas.UpdateRecommendationInput, schemas.UpdateRecommendationOutput, False),
        ("rank_recommendations", schemas.RankRecommendationsInput, schemas.RankRecommendationsOutput, True),
        ("add_budget_item", schemas.AddBudgetItemInput, schemas.AddBudgetItemOutput, False),
    )
    return [
        ToolSpec(name, handler.__doc__ or name, inputs, handler, frozenset(TOOL_DOMAINS[name]),
                 TOOL_RISKS.get(name, "low"), output_model=outputs, replay_safe=replay_safe)
        for name, inputs, outputs, replay_safe in contracts
        for handler in [getattr(service, name)]
    ]
