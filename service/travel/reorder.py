"""Optimize existing itinerary stops using verified directed travel times."""

from collections import defaultdict
from copy import deepcopy
from functools import partial
from itertools import combinations

from service.travel.itinerary_audit import MEAL_GAP, MEAL_WINDOWS, is_main_meal, meal_types
from service.travel.parallel import ordered_parallel
from service.travel.planning import MOVABLE_CATEGORIES, _minute, _set_start, _start_minutes


def optimize_itinerary(plan, resolve_route, *, preserve_order=False):
    from ortools.sat.python import cp_model

    items = plan["itinerary"]
    if not items:
        return None, "没有可重排的行程。"
    planning = plan["planning"]
    days = defaultdict(list)
    references = {}
    for i, item in enumerate(items):
        days[item["day"]].append(i)
        if item.get("reference_id") and item["category"] in MOVABLE_CATEGORIES:
            references[item["reference_id"]] = i
    model = cp_model.CpModel()
    durations = [item.get("duration_minutes", 0) for item in items]
    starts = [model.new_int_var(0, 1439, f"start_{i}") for i in range(len(items))]
    for i, item in enumerate(items):
        model.add(starts[i] + durations[i] <= 1440)
        if item.get("requested_time"):
            model.add(starts[i] == _minute(item["requested_time"]))
        if item["category"] == "intercity_transport" or item.get("anchor_role") == "start":
            model.add(starts[i] == _start_minutes(item))

    types = meal_types(plan)
    meals = [i for i, item in enumerate(items) if is_main_meal(item, types)]
    for i in meals:
        evening = model.new_bool_var(f"evening_{i}")
        for (begin, end), use in zip(MEAL_WINDOWS, (evening.Not(), evening)):
            model.add(starts[i] >= begin).only_enforce_if(use)
            model.add(starts[i] + durations[i] <= end).only_enforce_if(use)
    absolute = [starts[i] + (item["day"] - 1) * 1440 for i, item in enumerate(items)]
    for i, j in combinations(meals, 2):
        before = model.new_bool_var(f"meal_{i}_before_{j}")
        model.add(absolute[j] - absolute[i] >= MEAL_GAP).only_enforce_if(before)
        model.add(absolute[i] - absolute[j] >= MEAL_GAP).only_enforce_if(before.Not())

    constraints = list(planning.get("guide_constraints", []))
    for activity in planning.get("activities", []):
        if activity.get("day"):
            constraints.append({"type": "day", "activity_ids": [activity["id"]], "day": activity["day"]})
        if activity.get("time_window"):
            constraints.append({"type": "time_window", "activity_ids": [activity["id"]],
                                "time_window": activity["time_window"]})
    for constraint in constraints:
        if not constraint.get("hard", True):
            continue
        indices = [references[key] for key in constraint.get("activity_ids", []) if key in references]
        kind = constraint["type"]
        if kind == "order":
            for i, j in zip(indices, indices[1:]):
                model.add(absolute[i] < absolute[j])
        for i in indices:
            if kind == "day" and items[i]["day"] != constraint["day"]:
                return None, "现有地点日期与攻略要求冲突，无法在同一天内重排。"
            if kind == "duration" and durations[i] != constraint["duration_minutes"]:
                return None, "现有停留时长与攻略要求冲突，请调整后重排。"
            if kind == "time_window":
                begin, end = map(_minute, constraint["time_window"].split("-"))
                model.add(starts[i] >= begin)
                model.add(starts[i] <= end)

    # Each fixed stop separates the movable stops on either side of it.
    tiers = {}
    pairs = []
    for indices in days.values():
        if preserve_order:
            tiers.update((i, position) for position, i in enumerate(indices))
            pairs.extend(zip(indices, indices[1:]))
            continue
        tier = 0
        for i in indices:
            fixed = items[i]["category"] not in MOVABLE_CATEGORIES
            tiers[i] = tier + int(fixed)
            tier += 2 * int(fixed)
        order = sorted(set(tiers[i] for i in indices))
        for i in indices:
            tiers[i] = order.index(tiers[i])
        pairs.extend((i, j) for i in indices for j in indices if i != j and 0 <= tiers[j] - tiers[i] <= 1)

    legs = dict(zip(pairs, ordered_parallel(partial(resolve_route, items[i], items[j]) for i, j in pairs)))
    verified = {pair: leg for pair, leg in legs.items() if leg.get("source") in {"高德地图", "same_place"}}
    travel_costs = []
    spans = []
    selected = {}
    for day, indices in days.items():
        # Index 0 is a dummy node, connecting the end of the open path to its start.
        nodes = {i: n for n, i in enumerate(indices, 1)}
        begin = model.new_int_var(0, 1439, f"begin_{day}")
        end = model.new_int_var(0, 1440, f"end_{day}")
        floor = planning.get("day_departures", {}).get(str(day))
        model.add(begin >= (_minute(floor) if floor else _start_minutes(items[indices[0]]) % 1440))
        arcs = []
        for i in indices:
            if tiers[i] == tiers[indices[0]]:
                first = model.new_bool_var(f"first_{i}")
                arcs.append((0, nodes[i], first))
                selected[None, i] = first
                model.add(begin == starts[i]).only_enforce_if(first)
            if tiers[i] == tiers[indices[-1]]:
                last = model.new_bool_var(f"last_{i}")
                arcs.append((nodes[i], 0, last))
                model.add(end == starts[i] + durations[i]).only_enforce_if(last)
        for (i, j), leg in verified.items():
            if i not in nodes:
                continue
            use = model.new_bool_var(f"leg_{i}_{j}")
            arcs.append((nodes[i], nodes[j], use))
            selected[i, j] = use
            minutes = leg["duration_minutes"]
            model.add(starts[j] >= starts[i] + durations[i] + minutes).only_enforce_if(use)
            travel_costs.append(minutes * use)
        if any(not any(node == origin for origin, _, _ in arcs) for node in nodes.values()):
            return None, "部分地点缺少可用的高德路线，已保留原行程。"
        model.add_circuit(arcs)
        spans.append(end - begin)

    # Travel time takes priority over the daily span, then prefer earlier starts.
    start_weight = 1440 * len(items) + 1
    model.minimize((1440 * len(days) + 1) * start_weight * sum(travel_costs)
                   + start_weight * sum(spans) + sum(starts))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    status = solver.solve(model)
    if status == cp_model.INFEASIBLE:
        return None, ("缺少部分高德路段，现有可用路线无法满足全部时间约束，已保留原行程。"
                      if len(verified) != len(legs) else "固定时间、饭点或交通耗时存在冲突，已保留原行程。")
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return None, "本次未找到符合所有约束的顺序，已保留原行程。"

    result = deepcopy(plan)
    successors = {i: j for (i, j), use in selected.items() if i is not None and solver.boolean_value(use)}
    ordered = []
    chosen = []
    for indices in days.values():
        i = next(i for i in indices if (None, i) in selected and solver.boolean_value(selected[None, i]))
        while True:
            item = result["itinerary"][i]
            _set_start(item, solver.value(starts[i]))
            ordered.append(item)
            if i not in successors:
                break
            j = successors[i]
            chosen.append(verified[i, j])
            i = j
    result.update(itinerary=ordered, routes=chosen)
    result["planning"]["optimized_timetable"] = True
    return result, "已重排路线和时间。" if status == cp_model.OPTIMAL else "已应用符合约束的路线，求解器尚未证明耗时最短。"
