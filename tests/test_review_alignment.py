import unittest
from copy import deepcopy
from unittest.mock import patch

from agent.itinerary import _validate_proposal
from service.travel import planning
from service.travel.itinerary_audit import audit_plan
from test_guide_workflow import prepared, POINT


class ReviewAlignmentTests(unittest.TestCase):
    def visit_plan(self):
        plan = prepared()
        plan['planning']['activities'] = [
            {'id': 'a', 'name': '景点', 'category': 'attraction', 'choice': 'must',
             'place': POINT, 'duration_minutes': 90}]
        return plan

    def test_model_omission_is_error_not_pending(self):
        with self.assertRaisesRegex(ValueError, '遗漏'):
            _validate_proposal({'days': [{'day': 1, 'visits': []}]}, [{'id': 'a'}], 1)

    def test_first_generation_never_populates_pending(self):
        plan = self.visit_plan()
        result = planning.draft_itinerary(plan, {'days': [
            {'day': n, 'visits': [{'activity_id': 'a'}] if n == 2 else []} for n in range(1, 4)]})
        self.assertEqual(result['planning']['unscheduled_items'], [])
        self.assertNotIn('unscheduled', result['planning']['generation'])
        moved = planning.move_activity(result, 'guided-2-a', None)
        self.assertEqual(len(moved['planning']['unscheduled_items']), 1)
        regenerated = planning.draft_itinerary(moved, {'days': [
            {'day': n, 'visits': []} for n in range(1, 4)]})
        self.assertEqual(len(regenerated['planning']['unscheduled_items']), 1)
        self.assertNotIn('unplaced_activities', {x['type'] for x in audit_plan(regenerated)['issues']})

    def test_missing_location_is_issue_not_pending(self):
        plan = self.visit_plan()
        plan['planning']['activities'][0].pop('place')
        plan['planning']['guide_auto'] = True
        result = planning.draft_itinerary(plan, {'days': [{'day': n, 'visits': []} for n in range(1, 4)]})
        self.assertEqual(result['planning']['unscheduled_items'], [])
        self.assertIn('unresolved_places', {x['type'] for x in audit_plan(result)['issues']})

    def test_overflow_is_explicit_and_never_clamped(self):
        plan = self.visit_plan()
        plan['planning']['activities'][0]['duration_minutes'] = 600
        result = planning.draft_itinerary(plan, {'days': [
            {'day': n, 'visits': [{'activity_id': 'a'}] if n == 1 else []} for n in range(1, 4)]})
        for route in result['routes']:
            route['duration_minutes'] = 300
        result = planning.schedule_itinerary(result)
        self.assertTrue(any(x.get('day_offset', 0) for x in result['itinerary']))
        self.assertIn('schedule_overflow', {x['type'] for x in audit_plan(result)['issues']})
        self.assertFalse(any(x['time_of_day'] == '23:59' for x in result['itinerary']))

    def test_reextraction_removes_old_auto_places(self):
        plan = self.visit_plan()
        plan['planning']['activities'][0].update(source='guide_text', source_name='旧景点')
        with patch('service.travel.planning.analyze_guide', return_value={
            'text': '新攻略', 'places': [], 'constraints': []}):
            result = planning.build_activities(plan, '新攻略')
        self.assertEqual(result['planning']['activities'], [])

    def test_budget_ignores_unvisited_food(self):
        plan = self.visit_plan()
        plan['planning']['activities'] = [{'id': 'food', 'name': '饭店', 'category': 'food',
                                          'place': {'cost_per_person': 60}}]
        plan['itinerary'] = []
        self.assertEqual(planning.budget_summary(plan)['estimated_total'], 0)

    def test_same_poi_merges_aliases_and_constraints(self):
        from service.travel.place_matching import search_and_match
        plan = self.visit_plan()
        plan['planning']['activities'].append({**deepcopy(plan['planning']['activities'][0]), 'id': 'b', 'note': '清晨去'})
        plan['planning']['guide_constraints'] = [{'type': 'order', 'activity_ids': ['a', 'b']}]
        result = search_and_match(plan)
        self.assertEqual(len(result['planning']['activities']), 1)
        self.assertIn('清晨去', result['planning']['activities'][0]['note'])
        self.assertEqual(result['planning']['guide_constraints'][0]['activity_ids'], ['a'])

    def test_invalid_revision_keeps_complete_draft_and_reports_risk(self):
        from agent.planning_graph import itinerary_agent, after_audit
        plan = self.visit_plan()
        plan['planning']['generation'] = {'contract_version': 3}
        with patch('agent.planning_graph.generate_itinerary_proposal', side_effect=ValueError('遗漏地点')):
            result = itinerary_agent({'plan': plan, 'trace': [], 'format_attempts': 2, 'revision_attempts': 1})
        self.assertTrue(result['revision_failed'])
        self.assertEqual(result['plan']['planning']['generation_issues'][0]['type'], 'revision_failed')
        self.assertEqual(after_audit({**result, 'action': 'draft', 'revision_attempts': 1,
            'audit': {'issues': [{'type': 'schedule_overflow', 'severity': 'high'}]}}), 'done')

    def test_lodging_regions_are_search_hints_not_visits(self):
        from service.travel.guide import _parse_guide_data
        result = _parse_guide_data({'places': [{'name': '龙翔桥/湖滨', 'category': 'lodging_area',
            'note': '住附近'}], 'constraints': []}, '住龙翔桥/湖滨附近')
        self.assertEqual(result['places'], [])
        self.assertEqual(result['lodging_areas'][0]['name'], '龙翔桥/湖滨')

    def test_confirmed_manual_place_survives_new_guide(self):
        plan = planning.add_manual_activity(self.visit_plan(), '手动地点')
        item = plan['planning']['activities'][-1]
        item.update(source='高德 POI', place=POINT, place_status='manual_confirmed')
        with patch('service.travel.planning.analyze_guide', return_value={'text': '新攻略', 'places': []}):
            result = planning.build_activities(plan, '新攻略')
        self.assertTrue(any(x['id'] == item['id'] for x in result['planning']['activities']))

    def test_changed_address_invalidates_auto_match(self):
        plan = self.visit_plan()
        plan['planning']['activities'][0].update(source='guide_text', location_hint='旧地址', place_status='model_matched')
        with patch('service.travel.planning.analyze_guide', return_value={'text': '景点新地址', 'places': [
            {'id': 'a', 'name': '景点', 'category': 'attraction', 'location_hint': '新地址'}]}):
            result = planning.build_activities(plan, '景点新地址')
        self.assertFalse(result['planning']['activities'][0].get('place'))

    def test_lodging_alternatives_cannot_create_hotel_change(self):
        from service.travel.guide import _parse_guide_data
        result = _parse_guide_data({'places': [
            {'name': name, 'category': 'lodging_area'} for name in ('湖滨', '定安路')],
            'constraints': [{'type': 'hotel_change', 'places': ['湖滨', '定安路'], 'source': '也可住定安路'}]}, '住宿建议')
        self.assertEqual(result['constraints'], [])

    def test_explicit_quoted_lodging_hint_survives_model_omission(self):
        from service.travel.guide import _parse_guide_data
        result = _parse_guide_data({'places': [], 'constraints': []}, '优先住「龙翔桥/湖滨」附近。')
        self.assertEqual(result['lodging_areas'][0]['name'], '龙翔桥/湖滨')

    def test_unlocated_constraint_does_not_retry_ordering(self):
        from agent.planning_graph import after_audit
        plan = self.visit_plan()
        plan['planning']['activities'][0].pop('place')
        self.assertEqual(after_audit({'plan': plan, 'action': 'draft', 'revision_attempts': 1,
            'audit': {'guide_constraints': [{'type': 'day', 'hard': True, 'satisfied': False, 'activity_ids': ['a']}]}}), 'done')

    def test_transport_edit_keeps_cross_midnight_order(self):
        plan = {'itinerary': [
            {'item_id': 'a', 'day': 1, 'category': 'attraction', 'time_of_day': '23:00', 'duration_minutes': 60},
            {'item_id': 'b', 'day': 1, 'category': 'attraction', 'time_of_day': '00:30', 'day_offset': 1, 'duration_minutes': 30}],
            'routes': [{'origin_item_id': 'a', 'destination_item_id': 'b', 'duration_minutes': 15}]}
        result = planning.apply_route_times(plan)
        self.assertEqual(result['itinerary'][0]['time_of_day'], '23:00')
        self.assertEqual(result['itinerary'][1]['day_offset'], 1)

    def test_pending_food_cost_removed_but_paid_cost_retained(self):
        plan = self.visit_plan()
        plan['planning']['activities'][0].update(category='food', place={**POINT, 'cost_per_person': 60})
        plan = planning.draft_itinerary(plan, {'days': [
            {'day': n, 'visits': [{'activity_id': 'a'}] if n == 2 else []} for n in range(1, 4)]})
        self.assertEqual(planning.budget_summary(plan)['estimated_total'], 120)
        paid = planning.record_purchase(plan, {'reference_id': 'a', 'actual_total': 100})
        pending = planning.move_activity(paid, 'guided-2-a', None)
        self.assertEqual(planning.budget_summary(pending)['estimated_total'], 100)

    def test_map_query_discards_model_timing_prose(self):
        from types import SimpleNamespace
        from service.travel.place_matching import search_and_match
        plan = self.visit_plan()
        item = plan['planning']['activities'][0]
        item.pop('place')
        item['name'] = '西湖'
        response = SimpleNamespace(content='', tool_calls=[{'function': {'name': 'search_map_places',
            'arguments': {'place_position': 1, 'query': '傍晚去西湖看日落'}}}])
        with patch('service.travel.place_matching._chat', return_value=response), patch(
            'service.travel.place_matching.amap_search_places', return_value={'success': True, 'places': [POINT]}) as search:
            search_and_match(plan)
        search.assert_called_once_with('西湖', region_code='杭州', max_results=5)

    def test_async_guide_timeout_has_specific_error(self):
        import asyncio
        from service.travel.guide import analyze_guide
        with patch('service.travel.guide._extract_with_model', side_effect=asyncio.TimeoutError):
            with self.assertRaisesRegex(ValueError, '超时'):
                analyze_guide('西湖')
