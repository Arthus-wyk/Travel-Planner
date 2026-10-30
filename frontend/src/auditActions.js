export function auditDestination(issue, plan) {
  if (issue.type.startsWith('budget')) return { kind: 'budget' };
  if (issue.type === 'purchased_item_changed') return { kind: 'purchase', id: issue.target };
  if (['missing_route', 'stale_route', 'route_estimate'].includes(issue.type)) return { kind: 'routes' };
  if (['itinerary_outdated', 'missing_start_anchor', 'missing_end_anchor', 'unresolved_activity'].includes(issue.type)) return { kind: 'replan' };
  const targets = Array.isArray(issue.target) ? issue.target : [issue.target];
  const pending = (plan.planning?.unscheduled_items || []).find(x => targets.includes(x.item_id) || targets.includes(x.reference_id));
  if (pending) return { kind: 'pending', id: pending.item_id };
  const item = (plan.itinerary || []).find(x => targets.includes(x.item_id) || targets.includes(x.reference_id));
  if (item) return ['home_anchor', 'hotel_anchor', 'hotel_arrival', 'intercity_transport'].includes(item.category) ? { kind: 'replan' } : { kind: 'item', day: item.day, id: item.item_id };
  if (typeof issue.target === 'number') return { kind: 'day', day: issue.target };
  return { kind: 'replan' };
}
