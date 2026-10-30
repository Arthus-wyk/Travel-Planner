import unittest
from unittest.mock import patch

from service.travel.planning import create_draft, add_manual_activity, select_activities
from agent.planning_graph import run_planning_action


class UxRemediationTests(unittest.TestCase):
    def plan(self):
        return create_draft(dict(destination='杭州', departure_city='上海', start_date='2026-11-01', trip_days=3, travelers=1, budget=3000), 'ux')

    def test_excluded_duration_is_ignored_but_included_duration_is_checked(self):
        plan = add_manual_activity(self.plan(), '西湖')
        item = plan['planning']['activities'][0]
        result = select_activities(plan, {item['id']: 'exclude'}, details={item['id']: {'duration_minutes': 0}})
        self.assertEqual(result['planning']['activities'][0]['duration_minutes'], 90)
        with self.assertRaises(ValueError):
            select_activities(plan, {item['id']: 'must'}, details={item['id']: {'duration_minutes': 0}})

    def test_budget_update_reaudits_without_mutating_original(self):
        plan = self.plan()
        result = run_planning_action(plan, 'update_budget', {'budget': 4500})
        self.assertEqual(result['plan']['budget'], 4500)
        self.assertIn('audit', result)
        self.assertEqual(plan['budget'], 3000)
        for value in [-1, '', True, float('inf')]:
            with self.assertRaises(ValueError):
                run_planning_action(plan, 'update_budget', {'budget': value})

    def test_acknowledge_changed_purchase_keeps_cost_and_record(self):
        from service.travel.planning import budget_summary
        plan = self.plan()
        plan['planning']['stage'] = 'review'
        plan['planning']['purchases'] = [{'reference_id': 'old-ticket', 'label': '旧车票', 'category': 'transport', 'actual_total': 100}]
        result = run_planning_action(plan, 'acknowledge_purchase', {'reference_id': 'old-ticket'})
        self.assertEqual(result['plan']['planning']['purchases'][0]['actual_total'], 100)
        self.assertFalse(any(x['type'] == 'purchased_item_changed' for x in result['audit']['issues']))
        self.assertEqual(budget_summary(result['plan'])['paid_total'], 100)
        retained = next(x for x in budget_summary(result['plan'])['items'] if x['reference_id'] == 'old-ticket')
        self.assertFalse(retained['bookable'])
        self.assertTrue(retained['retained_expense'])
        self.assertEqual(len(plan['planning']['purchases']), 1)

    def test_recalculate_routes_does_not_regenerate_itinerary(self):
        plan = self.plan()
        plan['planning']['stage'] = 'review'
        with patch('agent.planning_graph.AMAP_API_KEY', ''), patch('agent.planning_graph.generate_itinerary_proposal') as model:
            result = run_planning_action(plan, 'recalculate_routes', {})
        self.assertIn('route_agent', result['trace'])
        model.assert_not_called()

    def test_transport_roundtrip_preserves_saved_unparsed_guide(self):
        plan = self.plan()
        with patch('agent.planning_graph.AMAP_API_KEY', ''), patch('agent.planning_graph._hotel_places', return_value=[]), patch('service.travel.planning.analyze_guide', side_effect=lambda text: {'text': text, 'places': []}):
            plan = run_planning_action(plan, 'candidates', {})['plan']
            candidates = plan['planning']['transport_candidates']
            selection = {side: next(x['id'] for x in candidates if x['direction'] == side and x['kind'] == 'train') for side in ('outbound', 'return')}
            plan = run_planning_action(plan, 'choose_candidates', selection)['plan']
            plan = run_planning_action(plan, 'choose_activities', {'text': '新保存的西湖攻略'})['plan']
            plan = run_planning_action(plan, 'candidates', {})['plan']
            plan = run_planning_action(plan, 'choose_candidates', selection)['plan']
        self.assertEqual(plan['planning']['draft_text'], '新保存的西湖攻略')

    def test_recalculation_without_map_service_discards_old_geometry(self):
        plan = self.plan()
        plan['planning']['stage'] = 'review'
        plan['itinerary'] = [
            {'item_id': 'a', 'day': 1, 'time_of_day': '09:00', 'title': '甲', 'location': '新起点', 'category': 'attraction', 'duration_minutes': 60},
            {'item_id': 'b', 'day': 1, 'time_of_day': '12:00', 'title': '乙', 'location': '终点', 'category': 'attraction', 'duration_minutes': 60},
        ]
        plan['routes'] = [{'origin_item_id': 'a', 'destination_item_id': 'b', 'origin': '旧起点', 'destination': '终点', 'legs': [{'polyline': 'old'}], 'duration_minutes': 30}]
        with patch('agent.planning_graph.AMAP_API_KEY', ''):
            result = run_planning_action(plan, 'recalculate_routes', {})
        self.assertEqual(result['plan']['routes'][0]['legs'], [])

