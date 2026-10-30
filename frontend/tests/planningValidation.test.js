import test from 'node:test';
import assert from 'node:assert/strict';
import { activitySnapshot, activityIssues, transportIssues, validMoney } from '../src/planningValidation.js';

const plan = { start_date: '2026-11-01', end_date: '2026-11-03', planning: { activities: [
  { id: 'hotel', name: '酒店', category: 'hotel' },
  { id: 'visit', name: '西湖', category: 'attraction', choice: 'must', duration_minutes: 90 },
] } };
test('hotel and required visits without coordinates have actionable blockers', () => {
  const issues = activityIssues(plan, { choices: {}, categories: {}, details: {}, selected_hotel: 'hotel' });
  assert.deepEqual(issues.filter(x => x.blocking).map(x => x.activityId), ['hotel', 'visit']);
  assert.ok(issues.every(x => x.targetId));
});
test('excluded invalid duration does not block or enter snapshot', () => {
  const edits = { choices: { visit: 'exclude' }, categories: {}, details: { visit: { duration_minutes: 0 } }, selected_hotel: '' };
  assert.equal(activitySnapshot(plan, edits).details.visit, undefined);
  assert.equal(activityIssues({ ...plan, end_date: plan.start_date }, edits).length, 0);
});
test('selected hotel cancels previous exclusion', () => {
  const result = activitySnapshot(plan, { choices: { hotel: 'exclude' }, categories: {}, details: {}, selected_hotel: 'hotel' });
  assert.equal(result.choices.hotel, 'optional');
});
test('optional unknown locations warn rather than block', () => {
  const issues = activityIssues({ ...plan, end_date: plan.start_date }, { choices: { visit: 'optional' }, categories: {}, details: {}, selected_hotel: '' });
  assert.equal(issues.length, 1);
  assert.equal(issues[0].blocking, false);
});
test('vehicle limits, empty times and missing choices explain blocked transport', () => {
  for (const vehicles of [0, 101, 1.5, '']) assert.ok(transportIssues({ outbound: 'a', return: 'b' }, true, vehicles, { outbound: '07:00', return: '17:00' }).length);
  for (const vehicles of [1, 100]) assert.equal(transportIssues({ outbound: 'a', return: 'b' }, true, vehicles, { outbound: '07:00', return: '17:00' }).length, 0);
  assert.equal(transportIssues({}, false, 1, {}).length, 2);
});
test('money distinguishes empty from free and rejects invalid precision', () => {
  for (const value of ['', ' ', -1, 'Infinity', 'NaN', '1.001']) assert.equal(validMoney(value), false);
  for (const value of [0, '0', '12.30']) assert.equal(validMoney(value), true);
});
test('day trip never submits a hotel selection', () => {
  const result = activitySnapshot({ ...plan, end_date: plan.start_date }, { selected_hotel: 'hotel' });
  assert.equal(result.selected_hotel, null);
});
