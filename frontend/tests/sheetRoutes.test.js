import test from 'node:test';
import assert from 'node:assert/strict';
import { orderedSheetStops, routeInstructions, sheetMapData } from '../src/sheetRoutes.js';

test('overnight stops sort after evening stops and use only the matching route', () => {
  const items = [{ item_id: 'end', time_of_day: '00:40', day_offset: 1 }, { item_id: 'start', time_of_day: '20:30' }];
  const route = { origin_item_id: 'start', destination_item_id: 'end' };
  assert.deepEqual(orderedSheetStops(items, [route]), [
    { item: items[1], route }, { item: items[0], route: undefined },
  ]);
});

test('directions follow walking and transit order, with one vehicle per segment', () => {
  const legs = [{ walking: { steps: [{ instruction: '沿湖滨路步行' }] }, bus: { buslines: [
    { name: '地铁 1 号线', departure_stop: { name: '龙翔桥' }, arrival_stop: { name: '凤起路' } },
    { name: '备用线路' },
  ] } }, { walking: { distance: '180' } }];
  assert.deepEqual(routeInstructions({ legs }), ['沿湖滨路步行', '在龙翔桥乘坐地铁 1 号线，到凤起路下车', '步行约 180 米']);
  assert.deepEqual(routeInstructions({ legs: [{ instruction: '沿环城西路向北行驶' }] }), ['沿环城西路向北行驶']);
  assert.deepEqual(routeInstructions({ source: 'provisional_estimate', legs }), []);
});

test('map numbers follow the itinerary, and unresolved locations and legs stay visible in the key', () => {
  const stops = orderedSheetStops([
    { item_id: 'late', title: '茶馆', time_of_day: '00:30', day_offset: 1 },
    { item_id: 'start', title: '酒店', time_of_day: '08:00' },
    { item_id: 'middle', title: '步道', time_of_day: '10:00' },
  ], []);
  const data = sheetMapData(stops, {
    points: [{ item_id: 'middle', location: '120.2,30.2' }, { item_id: 'start', location: '120.1,30.1' }],
    segments: [{ origin_item_id: 'start', destination_item_id: 'middle', mode: 'WALK', paths: ['120.1,30.1;120.2,30.2'] }],
  });
  assert.deepEqual(data.stations.map(({ number, coordinate, nextMode }) => [number, coordinate, nextMode]), [
    [1, [120.1, 30.1], 'WALK'], [2, [120.2, 30.2], 'pending'], [3, null, null],
  ]);
  assert.equal(data.stations[2].time, '次日 00:30');
  assert.equal(data.lines.length, 1);
});
