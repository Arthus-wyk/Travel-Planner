export function auditDestination(issue, plan) {
  if (issue.type.startsWith('budget')) return { kind: 'budget' };
  if (issue.type === 'purchased_item_changed') return { kind: 'purchase', id: issue.target };
  if (typeof issue.target === 'number') return { kind: 'day', day: issue.target };
  const targets = Array.isArray(issue.target) ? issue.target : [issue.target];
  const pending = (plan.planning?.unscheduled_items || []).find(x => targets.includes(x.item_id) || targets.includes(x.reference_id));
  if (pending?.manual_pending) return { kind: 'pending', id: pending.item_id };
  const items = (plan.itinerary || []).filter(x => targets.includes(x.item_id) || targets.includes(x.reference_id));
  const transport = items.find(x => x.category === 'intercity_transport' && !x.station_confirmed);
  const stop = items.find(x => ['attraction', 'food', 'other'].includes(x.category));
  const item = transport || stop || items[0];
  if (item) return { kind: 'item', day: item.day, id: item.item_id };
  return { kind: 'general' };
}
