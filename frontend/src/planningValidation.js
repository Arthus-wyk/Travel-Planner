export const hasCoordinates = (place) => Boolean(place && typeof place.longitude === 'number' && typeof place.latitude === 'number' && Number.isFinite(place.longitude) && Number.isFinite(place.latitude) && Math.abs(place.longitude) <= 180 && Math.abs(place.latitude) <= 90);
export const validMoney = (value) => value !== null && value !== undefined && String(value).trim() !== '' && Number.isFinite(Number(value)) && Number(value) >= 0 && Math.abs(Number(value) * 100 - Math.round(Number(value) * 100)) < 1e-7;
export const placeTarget = (id) => `activity-${id}`;
export function activitySnapshot(plan, edits) {
  const { choices = {}, categories = {}, details = {}, selected_hotel = '' } = edits;
  const selectedHotel = plan.start_date === plan.end_date ? '' : selected_hotel;
  const normalized = { ...choices };
  if (selectedHotel) normalized[selectedHotel] = 'optional';
  return { choices: normalized, categories, selected_hotel: selectedHotel || null,
    details: Object.fromEntries((plan.planning?.activities || []).filter(item => (normalized[item.id] ?? item.choice) !== 'exclude' && details[item.id]).map(item => [item.id,
      (categories[item.id] ?? item.category) === 'hotel' ? { visit_tips: details[item.id].visit_tips || '' } : details[item.id]])) };
}
export function activityIssues(plan, edits) {
  const snapshot = activitySnapshot(plan, edits);
  const items = plan.planning?.activities || [];
  const issues = [];
  const hotel = items.find(x => x.id === snapshot.selected_hotel && (snapshot.categories[x.id] ?? x.category) === 'hotel');
  if (plan.end_date !== plan.start_date && !hotel) issues.push({ code: 'hotel_required', message: '请选择一家住宿酒店', targetId: 'category-hotel', blocking: true });
  for (const item of items) {
    const category = snapshot.categories[item.id] ?? item.category;
    const choice = snapshot.choices[item.id] ?? item.choice ?? 'optional';
    if (choice === 'exclude' || (category === 'hotel' && item !== hotel)) continue;
    const duration = snapshot.details[item.id]?.duration_minutes ?? item.duration_minutes ?? 90;
    const tips = snapshot.details[item.id]?.visit_tips ?? item.visit_tips ?? '';
    const add = (code, message, blocking = true, field) => issues.push({ code, message: `${item.name}：${message}`, blocking, activityId: item.id, targetId: placeTarget(item.id), field });
    if (category !== 'hotel' && (!Number.isInteger(duration) || duration < 30 || duration > 600)) add('duration', '停留时长须为 30–600 的整数分钟', true, 'duration_minutes');
    if (tips.length > 500) add('tips', '备注最长 500 字', true, 'visit_tips');
    if (!hasCoordinates(item.place)) add('location', item === hotel || choice === 'must' ? '请搜索并确认具体位置' : '位置未确认，将暂不安排', item === hotel || choice === 'must');
  }
  if ((edits.text || '').length > 20000) issues.push({ code: 'guide_length', message: '攻略文字最长 20000 字', targetId: 'guide-input', blocking: true });
  return issues;
}
export function transportIssues(selection, driving, vehicles, departures) {
  const issues = [];
  for (const [side, label] of [['outbound', '去程'], ['return', '返程']]) {
    if (!selection[side]) issues.push({ message: `请选择${label}交通`, targetId: `transport-${side}` });
    if (driving && !/^([01]\d|2[0-3]):[0-5]\d$/.test(departures[side] || '')) issues.push({ message: `请填写${label}出发时间`, targetId: 'drive-inputs' });
  }
  if (driving && (!Number.isInteger(Number(vehicles)) || Number(vehicles) < 1 || Number(vehicles) > 100)) issues.push({ message: '车辆数须为 1–100 的整数', targetId: 'drive-inputs' });
  return issues;
}

export function focusIssue(targetId, field) {
  const target = document.getElementById(targetId);
  if (!target) return;
  for (let node = target; node; node = node.parentElement) {
    if (node.tagName === 'DETAILS') node.open = true;
  }
  target.querySelectorAll('details').forEach(node => { node.open = true; });
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  const input = (field && target.querySelector(`[data-field="${field}"]`)) || target.querySelector('input,select,textarea,button') || target;
  input.focus({ preventScroll: true });
}
