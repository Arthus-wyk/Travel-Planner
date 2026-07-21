"""基于评分、价格、类别和长期偏好给候选地点排序。"""

from __future__ import annotations

from typing import Any


def rank_candidates(
    candidates: list[dict[str, Any]],
    *,
    preferences: dict[str, Any] | None = None,
    category: str = "",
    max_results: int = 5,
) -> dict[str, Any]:
    """批量评分并截取前 N 项，同时保留理由和取舍供回答解释。"""
    preferences = preferences or {}
    ranked = []
    for candidate in candidates:
        score, reasons, tradeoffs = score_candidate(candidate, preferences, category)
        ranked.append(
            {
                "score": score,
                "name": candidate.get("name") or candidate.get("displayName"),
                "category": category or candidate.get("category"),
                "reasons": reasons,
                "tradeoffs": tradeoffs,
                "candidate": candidate,
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return {
        "success": True,
        "ranked": ranked[:max(1, max_results)],
        "count": len(ranked),
        "scoring_notes": [
            "评分综合考虑评价、评价数量、价格、类别匹配和用户偏好关键词。",
            "外部数据缺失时会降低确定性，但不会直接淘汰候选。",
        ],
    }


def score_candidate(
    candidate: dict[str, Any],
    preferences: dict[str, Any],
    category: str,
) -> tuple[int, list[str], list[str]]:
    """计算单个候选项的 0-100 分；加分代表匹配，扣分代表风险。"""
    score = 50
    reasons: list[str] = []
    tradeoffs: list[str] = []

    rating = candidate.get("rating")
    if isinstance(rating, (int, float)):
        score += int((float(rating) - 3.5) * 12)
        reasons.append(f"评分 {rating}")
    else:
        tradeoffs.append("缺少评分数据")

    rating_count = candidate.get("user_rating_count") or candidate.get("userRatingCount")
    if isinstance(rating_count, int):
        if rating_count >= 1000:
            score += 10
            reasons.append("评价数量充足")
        elif rating_count < 50:
            score -= 6
            tradeoffs.append("评价数量较少")

    price_level = str(candidate.get("price_level") or candidate.get("priceLevel") or "")
    budget_preferences = flatten_preferences(preferences, "budget")
    if price_level:
        if any(word in " ".join(budget_preferences) for word in ("省钱", "经济", "便宜", "低预算")):
            if price_level in {"PRICE_LEVEL_INEXPENSIVE", "1", "2"}:
                score += 8
                reasons.append("价格档位符合低预算偏好")
            elif price_level in {"PRICE_LEVEL_EXPENSIVE", "PRICE_LEVEL_VERY_EXPENSIVE", "4"}:
                score -= 10
                tradeoffs.append("价格可能偏高")
        elif any(word in " ".join(budget_preferences) for word in ("舒适", "品质", "高端", "五星")):
            if price_level in {"PRICE_LEVEL_MODERATE", "PRICE_LEVEL_EXPENSIVE", "2", "3"}:
                score += 5
                reasons.append("价格档位符合品质偏好")

    text = candidate_text(candidate).lower()
    for key in ("travel_interests", "hotel", "restaurant", "pace", "constraints"):
        for preference in flatten_preferences(preferences, key):
            if preference and preference.lower() in text:
                score += 6
                reasons.append(f"匹配偏好：{preference}")

    if category:
        category_lower = category.lower()
        types = " ".join(str(item).lower() for item in candidate.get("types", []))
        if category_lower in text or category_lower in types:
            score += 5
            reasons.append(f"匹配类别：{category}")

    status = candidate.get("business_status") or candidate.get("businessStatus")
    if status and status != "OPERATIONAL":
        score -= 30
        tradeoffs.append(f"营业状态需确认：{status}")

    if not reasons:
        reasons.append("基础信息可用，但缺少强匹配信号")

    return max(0, min(100, score)), reasons, tradeoffs


def flatten_preferences(preferences: dict[str, Any], key: str) -> list[str]:
    """把画像中的单值或列表统一为字符串列表。"""
    value = preferences.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def candidate_text(candidate: dict[str, Any]) -> str:
    """汇总候选项文本，供偏好关键词匹配。"""
    parts = []
    for key in ("name", "address", "formattedAddress", "why", "notes", "category"):
        value = candidate.get(key)
        if value:
            parts.append(str(value))
    parts.extend(str(item) for item in candidate.get("tags", []) or [])
    parts.extend(str(item) for item in candidate.get("types", []) or [])
    return " ".join(parts)
