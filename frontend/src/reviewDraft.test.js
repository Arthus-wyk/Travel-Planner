import test from 'node:test';
import assert from 'node:assert/strict';
import { applyReviewEdit } from './reviewDraft.js';

const plan = {
  budget: 1000,
  planning: { activities: [{ id: 'a' }, { id: 'b' }], unscheduled_items: [] },
  itinerary: [
    { item_id: 'start', day: 1, category: 'home_anchor', anchor_role: 'start' },
    { item_id: 'a', reference_id: 'a', day: 1, category: 'attraction' },
    { item_id: 'b', reference_id: 'b', day: 1, category: 'attraction' },
    { item_id: 'end', day: 1, category: 'hotel_anchor', anchor_role: 'end' },
  ],
  routes: [{ origin_item_id: 'a', destination_item_id: 'b', route_options: { WALK: { success: true, duration_minutes: 5, fare: 0 } } }],
};

test('moves remain local and preserve anchors, order and pending places', () => {
  const reordered = applyReviewEdit(plan, 'move_activity', { item_id: 'b', target_day: 1, before_item_id: 'a' });
  assert.deepEqual(reordered.itinerary.map(x => x.item_id), ['start', 'b', 'a', 'end']);
  const pending = applyReviewEdit(reordered, 'move_activity', { item_id: 'b', target_day: null });
  assert.equal(pending.planning.unscheduled_items[0].manual_pending, true);
  const restored = applyReviewEdit(pending, 'move_activity', { item_id: 'b', target_day: 2 });
  assert.equal(restored.itinerary.find(x => x.item_id === 'b').day, 2);
  assert.equal(restored.planning.unscheduled_items.length, 0);
  assert.deepEqual(plan.itinerary.map(x => x.item_id), ['start', 'a', 'b', 'end']);
});

test('mode preview uses existing time and fare and rejects missing options', () => {
  const changed = applyReviewEdit(plan, 'route_mode', { origin_item_id: 'a', destination_item_id: 'b', travel_mode: 'WALK' });
  assert.equal(changed.routes[0].duration_minutes, 5);
  assert.equal(changed.routes[0].fare, 0);
  assert.throws(() => applyReviewEdit(plan, 'route_mode', { origin_item_id: 'a', destination_item_id: 'b', travel_mode: 'DRIVE' }));
  assert.equal(plan.routes[0].travel_mode, undefined);
});
