import test from 'node:test';
import assert from 'node:assert/strict';
import { auditDestination } from '../src/auditActions.js';
const plan = { itinerary: [{ item_id: 'stop', reference_id: 'visit', day: 2 }], planning: { unscheduled_items: [{ item_id: 'pending', reference_id: 'p' }] } };
test('audit targets resolve days, item ids and reference ids distinctly', () => {
  assert.deepEqual(auditDestination({ type: 'pace_risk', target: 2 }, plan), { kind: 'day', day: 2 });
  assert.deepEqual(auditDestination({ type: 'time_conflict', target: ['stop', 'x'] }, plan), { kind: 'item', day: 2, id: 'stop' });
  assert.equal(auditDestination({ type: 'unplaced_activities', target: ['p'] }, plan).kind, 'pending');
});
test('global problems expose their actual repair route', () => {
  assert.equal(auditDestination({ type: 'budget_overrun', target: null }, plan).kind, 'budget');
  assert.equal(auditDestination({ type: 'missing_route', target: ['stop'] }, plan).kind, 'routes');
  assert.equal(auditDestination({ type: 'purchased_item_changed', target: 'old' }, plan).kind, 'purchase');
  assert.equal(auditDestination({ type: 'itinerary_outdated', target: null }, plan).kind, 'replan');
});
test('fixed-node schedule errors lead to regeneration rather than an uneditable card', () => {
  const fixedPlan = { itinerary: [{ item_id: 'fixed', category: 'home_anchor', day: 1 }], planning: {} };
  assert.equal(auditDestination({ type: 'missing_schedule_time', target: 'fixed' }, fixedPlan).kind, 'replan');
});
