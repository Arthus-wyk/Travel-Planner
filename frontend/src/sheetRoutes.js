export function orderedSheetStops(items, routes) {
  const ordered = [...items].sort((a, b) => (a.day_offset || 0) - (b.day_offset || 0)
    || (a.time_of_day || '').localeCompare(b.time_of_day || ''));
  return ordered.map((item, index) => ({
    item,
    route: routes.find(route => route.origin_item_id === item.item_id
      && route.destination_item_id === ordered[index + 1]?.item_id),
  }));
}

export const sheetRouteModes = { WALK: '步行', TRANSIT: '公共交通', DRIVE: '驾车', BICYCLE: '骑行', TWO_WHEELER: '电动车' };

export function sheetStopTime(item) {
  const offset = item.day_offset === 1 ? '次日 ' : item.day_offset > 1 ? `+${item.day_offset} 天 ` : '';
  return offset + (item.time_of_day || '待定');
}

function coordinates(value) {
  if (typeof value !== 'string' || !/^\s*-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?\s*$/.test(value)) return null;
  const [lon, lat] = value.split(',').map(Number);
  return Math.abs(lon) <= 180 && Math.abs(lat) <= 90 ? [lon, lat] : null;
}

export function sheetMapData(stops, data) {
  const points = new Map((data?.points || []).map(point => [point.item_id, coordinates(point.location)]));
  const segments = new Map((data?.segments || []).map(segment => [segment.origin_item_id, segment]));
  const lines = [];
  const stations = stops.map(({ item, route }, index) => {
    const next = stops[index + 1]?.item;
    const segment = segments.get(item.item_id);
    const paths = next && segment?.destination_item_id === next.item_id
      ? segment.paths.map(path => path.split(';').map(coordinates)).filter(path => path.length > 1 && path.every(Boolean))
      : [];
    const mode = sheetRouteModes[segment?.mode] ? segment.mode : 'route';
    for (const path of paths) lines.push({ mode, path, origin: index + 1 });
    const place = item.place;
    const coordinate = points.get(item.item_id) || (place && !item.lodging_pending && item.category !== 'home_anchor'
      ? coordinates(`${place.longitude},${place.latitude}`)
      : null);
    const nextMode = !next ? null : paths.length ? mode
      : route?.source === 'same_place' ? 'same_place' : route?.intercity ? 'intercity' : 'pending';
    return { number: index + 1, title: item.hotel_name || item.title, time: sheetStopTime(item), coordinate, nextMode };
  });
  return { stations, lines };
}

function mercator([lon, lat]) {
  const radians = Math.max(-85, Math.min(85, lat)) * Math.PI / 180;
  return [lon / 360, -Math.asinh(Math.tan(radians)) / (2 * Math.PI)];
}

export function sheetMapProjection({ stations, lines }, basemap, width, height) {
  const center = coordinates(basemap?.center);
  if (center && Number.isInteger(basemap.zoom)) {
    const [x, y] = mercator(center);
    const scale = 256 * 2 ** basemap.zoom;
    return coordinate => {
      const [px, py] = mercator(coordinate);
      return [width / 2 + (px - x) * scale, height / 2 + (py - y) * scale];
    };
  }
  const points = stations.flatMap(station => station.coordinate ? [station.coordinate] : [])
    .concat(lines.flatMap(line => line.path)).map(mercator);
  if (!points.length) return () => [width / 2, height / 2];
  const [left, right, top, bottom] = points.reduce(([l, r, t, b], [x, y]) => [
    Math.min(l, x), Math.max(r, x), Math.min(t, y), Math.max(b, y),
  ], [Infinity, -Infinity, Infinity, -Infinity]);
  const scale = Math.min((width - 160) / Math.max(right - left, 1e-6), (height - 128) / Math.max(bottom - top, 1e-6));
  return coordinate => {
    const [x, y] = mercator(coordinate);
    return [width / 2 + (x - (left + right) / 2) * scale, height / 2 + (y - (top + bottom) / 2) * scale];
  };
}

function instructions(steps) {
  return (steps || []).map(step => step.instruction).filter(text => typeof text === 'string' && text.trim());
}

export function routeInstructions(route) {
  if (route.source === 'provisional_estimate' || route.source === 'lodging_pending') return [];
  return (route.legs || []).flatMap(leg => {
    if (typeof leg.instruction === 'string' && leg.instruction.trim()) return [leg.instruction];
    const steps = instructions(leg.walking?.steps);
    if (!steps.length && Number(leg.walking?.distance) > 0) steps.push(`步行约 ${leg.walking.distance} 米`);
    const line = leg.bus?.buslines?.[0] || leg.railway;
    if (typeof line?.name === 'string' && line.name.trim()) {
      const from = line.departure_stop?.name;
      const to = line.arrival_stop?.name;
      steps.push(`${typeof from === 'string' && from ? `在${from}` : ''}乘坐${line.name}${typeof to === 'string' && to ? `，到${to}下车` : ''}`);
    }
    if (Number(leg.taxi?.distance) > 0) steps.push(`乘出租车约 ${leg.taxi.distance} 米`);
    return steps;
  });
}
