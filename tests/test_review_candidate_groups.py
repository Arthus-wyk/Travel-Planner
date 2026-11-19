import unittest

from agent.chat.review.parent import _groups
from service.travel.review_interaction import _selected_places


class CandidateGroupsTests(unittest.TestCase):
    def setUp(self):
        self.places = [{"place_id": key, "name": key} for key in ("park", "a", "b", "c", "food")]
        self.groups = [
            {"id": "park", "label": "必去公园", "place_ids": ["park"], "selection_count": 1},
            {"id": "sights", "label": "两个景点", "place_ids": ["a", "b", "c"], "selection_count": 2},
            {"id": "food", "label": "餐饮", "place_ids": ["food"], "selection_count": 1},
        ]
        self.state = {"places": self.places, "groups": self.groups, "mode": "multiple", "exact": None}

    def test_mixed_counts_apply_independently(self):
        _groups(self.groups, self.places)
        selection = _selected_places({"option_indexes": [0, 1, 2, 4]}, self.state)
        self.assertEqual([p["place_id"] for p in selection["places"]], ["park", "a", "b", "food"])

    def test_correct_total_does_not_replace_each_group_constraint(self):
        with self.assertRaisesRegex(ValueError, "公园"):
            _selected_places({"option_indexes": [1, 2, 3, 4]}, self.state)
        with self.assertRaisesRegex(ValueError, "两个景点"):
            _selected_places({"option_indexes": [0, 1, 4]}, self.state)

    def test_unknown_or_duplicate_cross_group_poi_is_rejected(self):
        for key in ("unknown", "park"):
            groups = [*self.groups, {"id": "bad", "label": "bad", "place_ids": [key], "selection_count": 1}]
            with self.assertRaises(ValueError):
                _groups(groups, self.places)

    def test_search_scope_rejects_mixed_sources_and_changed_counts(self):
        scopes = (({"park"}, 1), ({"a", "b", "c"}, 2), ({"food"}, 1))
        self.assertEqual(_groups(self.groups, self.places, scopes), self.groups)
        for changed in ({"place_ids": ["a", "food"]}, {"selection_count": 1}):
            groups = [{**self.groups[1], **changed}]
            with self.assertRaisesRegex(ValueError, "同一搜索任务"):
                _groups(groups, self.places, scopes)

    def test_duplicate_indexes_are_rejected(self):
        with self.assertRaises(ValueError):
            _selected_places({"option_indexes": [0, 1, 1, 4]}, self.state)

    def test_complete_delivery_rejects_omitted_search_and_changed_null(self):
        scopes = (({"park"}, 1), ({"a", "b", "c"}, 2), ({"food"}, 1))
        with self.assertRaisesRegex(ValueError, "完整交付"):
            _groups(self.groups[:1], self.places, scopes, require_complete=True)
        with self.assertRaisesRegex(ValueError, "选择数量"):
            _groups(self.groups[:1], self.places, (({"park"}, None),))
        self.assertEqual(_groups(self.groups, self.places, scopes, require_complete=True), self.groups)
        self.assertEqual(_groups(self.groups[:1], self.places, scopes), self.groups[:1])
