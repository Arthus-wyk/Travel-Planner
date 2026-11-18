"""经实际 HTTP 路由调用真实模型和高德，使用内存行程并输出对话。"""

import argparse
import json
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api.server import create_app
from service.config import get_env_or_dotenv
from service.mcp.server import amap_search_places
from service.storage.planning_jobs import fingerprint
from service.travel.planning import create_draft
from service.utils.model_client import AVAILABLE_MODELS, create_model_client

SCENARIOS = {
    "audit": "请审核第一天的路线节奏和费用。草稿总预算是多少？",
    "delay": "把岳王庙延后，但先问我延到几点，给我几个可选时间。",
    "place": "帮我在第一天添加一处西湖附近的博物馆，先搜索并展示可选的地点卡片。",
    "budget": "把行程总预算改成 800 元。",
    "mixed": "第一天保留必去公园，再加两个景点和一家餐厅，先搜索并按需求分组展示候选，让我选择。",
    "reference": "把第二个加入那天，先给我试算预览，不要保存。",
}


def _make_plan() -> dict:
    """从高德获取真实起始景点，再构建只存在于内存的审核行程。"""
    result = amap_search_places("杭州岳王庙", max_results=5, region_code="杭州")
    place = next((item for item in result.get("places", []) if item.get("place_id") and "岳" in item.get("name", "")), None)
    if place is None:
        raise RuntimeError(result.get("message") or "高德没有返回岳王庙的有效 POI")
    plan = create_draft({"destination": "杭州", "departure_city": "杭州", "start_date": "2026-11-21",
                         "trip_days": 1, "travelers": 1, "budget": 1000}, "live-review")
    plan["planning"].update(
        stage="review", day_departures={"1": "09:00"},
        selected_transport={"outbound": "out", "return": "back"},
        transport_candidates=[{"id": "out", "arrive_time": "07:00", "direction": "outbound"},
                              {"id": "back", "arrive_time": "20:00", "direction": "return"}],
        activities=[{"id": "park", "name": place["name"], "category": "attraction", "choice": "optional",
                     "duration_minutes": 60, "estimated_cost": 0, "place": place}],
    )
    plan["itinerary"] = [{"item_id": "guided-1-park", "reference_id": "park", "day": 1,
                          "category": "attraction", "title": place["name"], "location": place["address"],
                          "time_of_day": "09:00", "duration_minutes": 60, "estimated_cost": 0,
                          "day_offset": 0, "visit_tips": "", "place": place}]
    return plan


def _first_action(block: dict) -> tuple[dict, str] | None:
    """按显示顺序选择首个选项、卡片或预览按钮。"""
    if block["type"] == "choice":
        return {"action_id": block["action_id"], "option_index": 0}, block["options"][0]
    if block["type"] == "place_candidates" and block["places"]:
        if block.get("action_id"):
            ids = [key for group in block.get("groups", [])
                   for key in group["place_ids"][:group["selection_count"] or 1]]
            indexes = [index for index, place in enumerate(block["places"]) if place["place_id"] in ids]
            return {"action_id": block["action_id"], "option_indexes": indexes}, "按各组数量选择候选"
        place = block["places"][0]
        return {"action_id": place["action_id"]}, place["name"]
    if block["type"] == "change_preview":
        return {"action_id": block["action_id"]}, "应用到草稿"
    return None


def _run_scenario(client: TestClient, plan: dict, scenario: str, log) -> bool:
    """发送前端请求，自动续跑并记录用户可见回复。"""
    payload = {"plan_version": fingerprint(plan), "draft_revision": 1,
               "operations": [{"action": "update_budget", "payload": {"budget": 900}}]}
    message = SCENARIOS[scenario]
    log(f"\n## {scenario}\n\n用户：{message}")
    action = {"message": message}
    if scenario == "reference":
        action["history"] = [{"role": "user", "content": "第一天有岳王庙，我想再选一个景点。"},
                             {"role": "assistant", "content": "第一天的两个候选：第一个是西湖，第二个是灵隐寺。"}]
    seen = set()
    for _ in range(6):
        response = client.post(f"/api/plans/{plan['plan_id']}/planning/assistant", json={**payload, **action})
        result = response.json()
        log(f"\n助手（HTTP {response.status_code}，{result.get('status', 'error')}）：{result.get('message', '')}")
        for block in result.get("blocks", []):
            if block["type"] == "place_candidates":
                seen.add("places")
                for index, place in enumerate(block["places"], 1):
                    log(f"- {index}. {place['name']} · {place.get('address', '')} · POI {place['place_id']}")
            elif block["type"] == "choice":
                seen.add("choice")
                log(block["question"])
                for index, option in enumerate(block["options"], 1):
                    log(f"- {index}. {option}")
                if block.get("allow_custom"):
                    log("- 自定义")
            elif block["type"] == "change_preview":
                seen.add("preview")
                log("拟应用：" + json.dumps(block["operations"], ensure_ascii=False))
            elif block["type"] == "review_issues":
                for issue in block["issues"]:
                    log(f"- 审核阻塞：{issue['message']}")
        if not response.is_success or result.get("status") in {"failed", "conflict"}:
            return False
        if result.get("status") == "draft_updated":
            log("草稿操作：" + json.dumps(result["operations"], ensure_ascii=False))
            log("地图日期：" + ", ".join(result["maps"]))
            return {"place": "places", "budget": "preview", "delay": "choice",
                    "mixed": "places", "reference": "preview"}.get(scenario) in seen
        first = next((option for block in result.get("blocks", []) if (option := _first_action(block))), None)
        if first is None:
            return scenario == "audit" and result.get("status") == "answer"
        action, label = first
        log(f"\n用户（自动选第一项）：{label}")
    log("结果：达到交互轮数上限。")
    return False


def main() -> int:
    """装配真实外部调用、内存缓存和禁止写库的测试边界。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=AVAILABLE_MODELS[0])
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--output", type=Path, default=Path("logs/review-assistant-live.md"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    secrets = [get_env_or_dotenv(key) for key in ("AMAP_API_KEY", "LLM_API_KEY", "SILICONFLOW_API_KEY")]
    cache = {}
    with args.output.open("w", encoding="utf-8") as output, ExitStack() as stack:
        def log(line: str) -> None:
            """逐行写入可查看日志，过滤配置里的外部 API 密钥。"""
            for secret in secrets:
                if secret:
                    line = line.replace(secret, "[已脱敏]")
            print(line, flush=True)
            output.write(line + "\n")
            output.flush()

        log(f"# 路线审核助手真实调用日志\n\n时间：{datetime.now().isoformat(timespec='seconds')}\n模型：{args.model}")
        try:
            client = create_model_client()
            stack.enter_context(patch("service.travel.review_interaction.create_model_client", return_value=client))
            stack.enter_context(patch("service.integrations.travel_amap.read_map_cache",
                                      side_effect=lambda key, **_: deepcopy(cache.get(key))))
            stack.enter_context(patch("service.integrations.travel_amap.write_map_cache",
                                      side_effect=lambda key, data, ttl: cache.update({key: deepcopy(data)})))
            stack.enter_context(patch("service.storage.travel_db.connect",
                                      side_effect=AssertionError("真实测试禁止访问数据库")))
            stack.enter_context(patch("service.storage.planning_jobs.save_review_changes",
                                      side_effect=AssertionError("真实测试禁止保存行程")))
            plan = _make_plan()
            stack.enter_context(patch("service.travel.review_interaction.get_plan",
                                      side_effect=lambda key: (deepcopy(plan), None) if key == plan["plan_id"] else (None, None)))
            http = stack.enter_context(TestClient(create_app(Path("mcp_server.py"), args.model)))
            scenarios = SCENARIOS if args.scenario == "all" else [args.scenario]
            failed = []
            for scenario in scenarios:
                try:
                    if not _run_scenario(http, plan, scenario, log):
                        failed.append(scenario)
                except Exception as exc:
                    log(f"运行异常：{exc}")
                    failed.append(scenario)
            log(f"\n结果：{len(scenarios) - len(failed)}/{len(scenarios)} 通过")
            if failed:
                log("未通过：" + ", ".join(failed))
            return int(bool(failed))
        except Exception as exc:
            log(f"\n启动失败：{exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
