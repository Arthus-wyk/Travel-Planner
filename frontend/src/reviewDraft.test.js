import test from 'node:test';
import assert from 'node:assert/strict';
import { applyReviewEdit, lodgingCompletionOffer, overnightMismatch, previewSchedule, rebaseReviewAdditions } from './reviewDraft.js';

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
  assert.deepEqual(reordered.routes, []);
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

test('review insertion keeps the order and invalidates the replaced route', () => {
  const draft = applyReviewEdit(plan, 'review_place', {
    item_id: 'a', activity_id: 'new', category: 'food',
    place: { place_id: 'poi', name: '餐厅', address: '地址' }, visit_tips: '预约',
  });
  assert.deepEqual(draft.itinerary.map(x => x.category), ['home_anchor', 'attraction', 'food', 'attraction', 'hotel_anchor']);
  assert.equal(draft.itinerary[2].visit_tips, '预约');
  assert.deepEqual(draft.routes, []);
});

test('stale additions keep newer route changes and recognize an earlier save', () => {
  const meal = { item_id: 'a', activity_id: 'new', category: 'food',
    place: { place_id: 'meal', name: '餐厅', address: '地址' }, visit_tips: '预约' };
  const park = { ...meal, item_id: 'guided-1-new', activity_id: 'park', category: 'attraction',
    place: { ...meal.place, place_id: 'park' }, duration_minutes: 90 };
  const previousSave = applyReviewEdit(plan, 'review_place', { ...meal, activity_id: 'already-saved' });
  const latest = { ...previousSave, budget: 2500, planning: { ...previousSave.planning, stage: 'review' } };
  const edits = [meal, park].map(payload => ({ action: 'review_place', payload }));
  const replay = rebaseReviewAdditions(plan, latest, edits);
  assert.deepEqual(replay.map(edit => edit.payload.item_id), ['guided-1-already-saved']);
  assert.equal(rebaseReviewAdditions(plan, latest, edits.slice(0, 1)).length, 0);
  const moved = { ...latest, itinerary: latest.itinerary.map(item => item.item_id === 'a' ? { ...item, day: 2 } : item) };
  assert.equal(rebaseReviewAdditions(plan, moved, edits), null);
});

test('filling an empty night updates both anchors and a later change warns at the next start', () => {
  const end = { ...plan.itinerary.at(-1), lodging_pending: true };
  const start = { item_id: 'next', day: 2, category: 'hotel_anchor', anchor_role: 'start', lodging_pending: true };
  const draft = { ...plan, itinerary: [...plan.itinerary.slice(0, -1), end, start] };
  const hotel = { item_id: 'end', activity_id: 'hotel-a', category: 'hotel', replace: true,
    place: { place_id: 'poi-a', name: '甲酒店', address: '同一个地址' } };
  const filled = applyReviewEdit(draft, 'review_place', hotel);
  assert.equal(filled.itinerary.at(-1).hotel_name, '甲酒店');
  assert.equal(filled.itinerary.at(-1).lodging_pending, undefined);
  assert.equal(overnightMismatch(...filled.itinerary.slice(-2)), false);
  const changed = applyReviewEdit(filled, 'review_place', { ...hotel, activity_id: 'hotel-b',
    place: { ...hotel.place, place_id: 'poi-b', name: '乙酒店' } });
  assert.equal(overnightMismatch(...changed.itinerary.slice(-2)), true);
});

test('one manual hotel offers to fill mismatching or missing overnight slots', () => {
  const hotel = { item_id: 'hotel', reference_id: 'manual-one', day: 1, category: 'hotel',
    title: '酒店', location: '甲', time_of_day: '13:00' };
  const other = { item_id: 'end', reference_id: 'generated', day: 1, category: 'hotel_anchor', anchor_role: 'end', location: '乙' };
  const draft = { ...plan, planning: { stage: 'review' }, itinerary: [hotel, other] };
  assert.equal(lodgingCompletionOffer(draft, 2).time_of_day, '13:00');
  const end = { ...other, location: '甲' };
  const start = { ...end, day: 2, anchor_role: 'start' };
  const arrival = { ...hotel, category: 'hotel_arrival' };
  const checkout = { ...start, category: 'hotel', anchor_role: 'checkout' };
  assert.equal(lodgingCompletionOffer({ ...draft, itinerary: [arrival, end, start, checkout] }, 2), null);
  assert.equal(lodgingCompletionOffer({ ...draft, itinerary: [hotel, { ...start, reference_id: 'manual-two' }] }, 2), null);
});

test('changing a stay keeps route choices and the preview uses the daily departure and selected mode', () => {
  let draft = applyReviewEdit(plan, 'update_activity', { activity_id: 'a', duration_minutes: 60 });
  draft = applyReviewEdit(draft, 'route_mode', { origin_item_id: 'a', destination_item_id: 'b', travel_mode: 'WALK' });
  draft = applyReviewEdit(draft, 'day_departure', { day: 1, time: '08:30' });
  const items = previewSchedule(draft, 1);
  assert.equal(items[0].time_of_day, '08:30');
  assert.equal(items[1].time_of_day, '09:00');
  assert.equal(items[2].time_of_day, '10:05');
});
