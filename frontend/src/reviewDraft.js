import { isMovableStop } from './planningLabels.js';

export function canSetDeparture(item) {
  return Boolean(item && (item.anchor_role === 'start' || isMovableStop(item)));
}

export function overnightMismatch(previous, first) {
  if (!previous || !first || previous.lodging_pending || first.lodging_pending) return false;
  const end = previous.arrival_location ?? previous.location;
  const a = previous.arrival_location ? null : previous.place?.place_id;
  const b = first.place?.place_id;
  return a && b ? a !== b : Boolean(end && first.location && end.trim() !== first.location.trim());
}

export function lodgingCompletionOffer(plan, days, preferredItemId) {
  if (plan?.planning?.stage !== 'review' || days < 2) return null;
  const items = plan.itinerary || [];
  const lodging = items.filter(item => !item.lodging_pending && item.reference_id &&
    ['hotel', 'hotel_anchor', 'hotel_arrival'].includes(item.category));
  const selected = lodging.filter(item => item.category === 'hotel' || item.reference_id.startsWith('manual-'));
  const hotels = selected.length ? selected : lodging.length === 1 ? lodging : [];
  if (new Set(hotels.map(item => item.reference_id)).size !== 1) return null;
  const source = hotels.find(item => item.item_id === preferredItemId) ||
    hotels.find(item => item.lodging_selected_here) || hotels[0];
  for (let day = 1; day <= days; day++) {
    const stops = items.filter(item => item.day === day);
    const start = day === 1
      ? stops.find(item => item.category === 'hotel_arrival')
      : stops.find(item => item.category === 'hotel_anchor' && item.anchor_role === 'start');
    const end = day < days
      ? stops.find(item => item.category === 'hotel_anchor' && item.anchor_role === 'end')
      : stops.find(item => item.category === 'hotel' && item.anchor_role === 'checkout');
    if ([start, end].some(item =>
      !item || item.lodging_pending || overnightMismatch(source, item))) {
      const shown = preferredItemId && previewSchedule(plan, source.day).find(item => item.item_id === source.item_id);
      return { ...source, time_of_day: shown?.time_of_day || source.time_of_day };
    }
  }
  return null;
}

export function rebaseReviewAdditions(base, latest, edits) {
  if (latest.planning.stage !== 'review' || edits.some(edit => edit.action !== 'review_place' || edit.payload.replace)) return null;
  const baseItems = new Map(base.itinerary.map(item => [item.item_id, item]));
  const savedIds = new Set(latest.itinerary.map(item => item.item_id));
  const items = [...latest.itinerary];
  const remapped = new Map();
  const recognized = new Set();
  const replay = [];
  for (const { action, payload } of edits) {
    const poiId = payload.poi_id ?? payload.place?.place_id;
    const anchorId = remapped.get(payload.item_id) ?? payload.item_id;
    const index = items.findIndex(item => item.item_id === anchorId);
    const anchor = items[index];
    if (!anchor) return null;
    if (!remapped.has(payload.item_id)) {
      const original = baseItems.get(anchorId);
      if (!original || ['day', 'category', 'anchor_role', 'location', 'arrival_location'].some(key => original[key] !== anchor[key])
          || original.place?.place_id !== anchor.place?.place_id) return null;
    }
    const duration = payload.category === 'attraction' ? payload.duration_minutes : payload.category === 'food' ? 60 : 0;
    const originalId = `guided-${anchor.day}-${payload.activity_id}`;
    const existing = items.find(item => item.reference_id === payload.activity_id);
    const next = items[index + 1];
    const match = existing ?? (next && savedIds.has(next.item_id) && !baseItems.has(next.item_id) &&
      !recognized.has(next.item_id) ? next : null);
    const matches = match?.day === anchor.day && match.category === payload.category
      && poiId && match.place?.place_id === poiId && match.duration_minutes === duration
      && (match.visit_tips || '') === (payload.visit_tips || '').trim();
    if (existing && !matches) return null;
    if (matches) {
      recognized.add(match.item_id);
      remapped.set(originalId, match.item_id);
      continue;
    }
    replay.push({ action, payload: { ...payload, item_id: anchorId } });
    remapped.set(originalId, originalId);
    items.splice(index + 1, 0, { item_id: originalId, day: anchor.day });
  }
  return replay;
}

export function previewSchedule(plan, day) {
  const items = plan.itinerary.filter(item => item.day === day);
  const routes = new Map((plan.routes || []).map(route => [route.origin_item_id, route]));
  const minutes = (time = '09:30') => Number(time.slice(0, 2)) * 60 + Number(time.slice(3));
  let previous;
  let end;
  return items.map((item, index) => {
    const route = routes.get(previous?.item_id);
    const duration = route?.destination_item_id === item.item_id ? route.duration_minutes : undefined;
    let start = minutes(item.time_of_day) + (item.day_offset || 0) * 1440;
    if (!index && canSetDeparture(item)) {
      start = minutes(item.requested_time ?? plan.planning.day_departures?.[day] ?? item.time_of_day);
    } else if (index && item.category !== 'intercity_transport') {
      start = item.requested_time ? minutes(item.requested_time) : end + (duration ?? 30);
    }
    previous = item;
    end = start + (item.duration_minutes || 0);
    const hours = Math.floor(start / 60) % 24;
    return { ...item, time_of_day: [hours, start % 60].map(value => String(value).padStart(2, '0')).join(':'), day_offset: Math.floor(start / 1440) };
  });
}

export function applyReviewEdit(plan, action, payload) {
  const draft = structuredClone(plan);
  const pending = draft.planning.unscheduled_items ||= [];
  if (!draft.planning.day_departures) {
    const departures = draft.planning.day_departures = {};
    for (const item of draft.itinerary) {
      if (canSetDeparture(item) && item.time_of_day) departures[item.day] ??= item.time_of_day;
    }
  }
  const changedPlaces = new Set();
  if (action === 'review_place') {
    const index = draft.itinerary.findIndex(x => x.item_id === payload.item_id);
    const anchor = draft.itinerary[index];
    if (!anchor) throw new Error('找不到行程卡片');
    const { place, category, activity_id, visit_tips, replace } = payload;
    const duration = category === 'attraction' ? payload.duration_minutes : category === 'food' ? 60 : 0;
    const details = { place, location: place.address || place.name, visit_tips };
    draft.planning.activities.push({ id: activity_id, name: place.name, category, duration_minutes: duration, ...details });
    if (replace) {
      const targets = [anchor];
      if (anchor.lodging_pending) {
        const end = anchor.anchor_role === 'end';
        const paired = draft.itinerary.find(x => x.lodging_pending && x.day === anchor.day + (end ? 1 : -1) && x.anchor_role === (end ? 'start' : 'end'));
        if (paired) targets.push(paired);
      }
      for (const item of targets) {
        Object.assign(item, details, { reference_id: activity_id, hotel_name: place.name });
        if (item.category === 'hotel') Object.assign(item, { title: place.name, hotel_name: null });
        delete item.lodging_pending;
        changedPlaces.add(item.item_id);
      }
    } else {
      draft.itinerary.splice(index + 1, 0, { ...details, item_id: `guided-${anchor.day}-${activity_id}`,
        day: anchor.day, time_of_day: anchor.time_of_day, reference_id: activity_id, title: place.name,
        category, duration_minutes: duration, estimated_cost: 0 });
    }
  } else if (action === 'move_activity') {
    const item = [...draft.itinerary, ...pending].find(x => x.item_id === payload.item_id && isMovableStop(x));
    if (!item) throw new Error('找不到可移动地点');
    if (draft.planning.guide_auto && !item.place && payload.target_day != null) throw new Error('请先确认该地点的位置');
    draft.itinerary = draft.itinerary.filter(x => x !== item);
    draft.planning.unscheduled_items = pending.filter(x => x !== item);
    item.day = payload.target_day; item.day_offset = 0; item.manual_pending = item.day == null;
    delete item.requested_time;
    if (item.manual_pending) draft.planning.unscheduled_items.push(item);
    else {
      const before = draft.itinerary.findIndex(x => x.day > item.day || x.day === item.day &&
        (x.item_id === payload.before_item_id || !payload.before_item_id && (x.anchor_role === 'end' || x.item_id.endsWith('-return'))));
      draft.itinerary.splice(before < 0 ? draft.itinerary.length : before, 0, item);
    }
  } else if (action === 'update_activity') {
    const item = draft.itinerary.find(x => x.reference_id === payload.activity_id && isMovableStop(x));
    const activity = draft.planning.activities.find(x => x.id === payload.activity_id);
    if (!item || !activity) throw new Error('找不到停留地点');
    const { activity_id, ...details } = payload;
    Object.assign(item, details); Object.assign(activity, details);
    if (details.time_of_day) item.requested_time = details.time_of_day;
    else delete item.requested_time;
  } else if (action === 'route_mode') {
    const route = draft.routes.find(x => x.origin_item_id === payload.origin_item_id && x.destination_item_id === payload.destination_item_id);
    const option = route?.route_options?.[payload.travel_mode];
    if (!option?.success) throw new Error('该交通方式暂无可用路线');
    Object.assign(route, option, { travel_mode: payload.travel_mode, mode_locked: true, source: '高德地图', error: null });
  } else if (action === 'transport_stops') {
    const item = draft.itinerary.find(x => x.item_id === payload.item_id);
    Object.assign(item, { location: payload.departure_location, arrival_location: payload.arrival_location, station_confirmed: true });
  } else if (action === 'day_departure') {
    draft.planning.day_departures[payload.day] = payload.time;
    const first = draft.itinerary.find(item => item.day === payload.day);
    if (first) delete first.requested_time;
  } else if (action === 'update_budget') draft.budget = payload.budget;
  else if (action === 'acknowledge_purchase') {
    const record = draft.planning.purchases.find(x => x.reference_id === payload.reference_id);
    if (record) record.change_acknowledged = true;
  }
  if (action === 'move_activity' || action === 'review_place') {
    const nextById = new Map();
    const previousByDay = new Map();
    for (const item of draft.itinerary) {
      const previous = previousByDay.get(item.day);
      if (previous) nextById.set(previous, item.item_id);
      previousByDay.set(item.day, item.item_id);
    }
    draft.routes = draft.routes.filter(route => nextById.get(route.origin_item_id) === route.destination_item_id &&
      !changedPlaces.has(route.origin_item_id) && !changedPlaces.has(route.destination_item_id));
  } else if (action === 'transport_stops') {
    draft.routes = draft.routes.filter(route => ![route.origin_item_id, route.destination_item_id].includes(payload.item_id));
  }
  return draft;
}
