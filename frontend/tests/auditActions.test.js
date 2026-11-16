import test from 'node:test';
import assert from 'node:assert/strict';
import { auditDestination } from '../src/auditActions.js';
const plan = { itinerary: [{ item_id: 'stop', reference_id: 'visit', day: 2, category: 'attraction' }], planning: { unscheduled_items: [{ item_id: 'pending', reference_id: 'p', manual_pending: true }] } };
test('audit targets resolve days, item ids and reference ids distinctly', () => {
  assert.deepEqual(auditDestination({ type: 'pace_risk', target: 2 }, plan), { kind: 'day', day: 2 });
  assert.deepEqual(auditDestination({ type: 'time_conflict', target: ['stop', 'x'] }, plan), { kind: 'item', day: 2, id: 'stop' });
  assert.equal(auditDestination({ type: 'unplaced_activities', target: ['p'] }, plan).kind, 'pending');
});
test('a missing station takes precedence over a visit when both are in the conflict', () => {
  const stops = [...plan.itinerary, { item_id: 'return', day: 2, category: 'intercity_transport', station_confirmed: false }];
  assert.deepEqual(auditDestination({ type: 'transport_buffer', target: ['stop', 'return'] }, { ...plan, itinerary: stops }),
    { kind: 'item', day: 2, id: 'return' });
});
test('audit issues with day or node targets stay with the route', () => {
  assert.equal(auditDestination({ type: 'budget_overrun', target: null }, plan).kind, 'budget');
  assert.equal(auditDestination({ type: 'missing_route', target: ['stop'] }, plan).kind, 'item');
  assert.deepEqual(auditDestination({ type: 'missing_end_anchor', target: 2 }, plan), { kind: 'day', day: 2 });
  assert.equal(auditDestination({ type: 'purchased_item_changed', target: 'old' }, plan).kind, 'purchase');
  assert.equal(auditDestination({ type: 'itinerary_outdated', target: null }, plan).kind, 'general');
});
test('fixed nodes show their schedule warnings', () => {
  const fixedPlan = { itinerary: [{ item_id: 'fixed', category: 'home_anchor', day: 1 }], planning: {} };
  assert.deepEqual(auditDestination({ type: 'missing_schedule_time', target: 'fixed' }, fixedPlan),
    { kind: 'item', day: 1, id: 'fixed' });
});
