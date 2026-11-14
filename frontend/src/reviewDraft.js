import { isMovableStop } from './planningLabels.js';

export function applyReviewEdit(plan, action, payload) {
  const draft = structuredClone(plan);
  const pending = draft.planning.unscheduled_items ||= [];
  if (action === 'move_activity') {
    const item = [...draft.itinerary, ...pending].find(x => x.item_id === payload.item_id && isMovableStop(x));
    if (!item) throw new Error('找不到可移动地点');
    if (draft.planning.guide_auto && !item.place && payload.target_day != null) throw new Error('请先确认该地点的位置');
    draft.itinerary = draft.itinerary.filter(x => x !== item);
    draft.planning.unscheduled_items = pending.filter(x => x !== item);
    item.day = payload.target_day; item.day_offset = 0; item.manual_pending = item.day == null;
    delete item.requested_time;
    if (item.manual_pending) draft.planning.unscheduled_items.push(item);
    else {
      const visits = draft.itinerary.filter(x => x.day === item.day && isMovableStop(x));
      const before = visits.findIndex(x => x.item_id === payload.before_item_id);
      visits.splice(before < 0 ? visits.length : before, 0, item);
      draft.itinerary = draft.itinerary.filter(x => x.day !== item.day || !isMovableStop(x));
      const anchors = draft.itinerary.filter(x => x.day === item.day);
      const start = anchors.filter(x => x.anchor_role !== 'end');
      const end = anchors.filter(x => x.anchor_role === 'end');
      draft.itinerary = [...draft.itinerary.filter(x => x.day !== item.day), ...start, ...visits, ...end];
    }
  } else if (action === 'update_activity') {
    const item = draft.itinerary.find(x => x.reference_id === payload.activity_id && isMovableStop(x));
    const activity = draft.planning.activities.find(x => x.id === payload.activity_id);
    if (!item || !activity) throw new Error('找不到停留地点');
    const { activity_id, ...details } = payload;
    Object.assign(item, details); Object.assign(activity, details);
    item.requested_time = details.time_of_day;
  } else if (action === 'route_mode') {
    const route = draft.routes.find(x => x.origin_item_id === payload.origin_item_id && x.destination_item_id === payload.destination_item_id);
    const option = route?.route_options?.[payload.travel_mode];
    if (!option?.success) throw new Error('该交通方式暂无可用路线');
    Object.assign(route, option, { travel_mode: payload.travel_mode, mode_locked: true, source: '高德地图', error: null });
  } else if (action === 'transport_stops') {
    const item = draft.itinerary.find(x => x.item_id === payload.item_id);
    Object.assign(item, { location: payload.departure_location, arrival_location: payload.arrival_location, station_confirmed: true });
  } else if (action === 'update_budget') draft.budget = payload.budget;
  else if (action === 'acknowledge_purchase') {
    const record = draft.planning.purchases.find(x => x.reference_id === payload.reference_id);
    if (record) record.change_acknowledged = true;
  }
  return draft;
}
