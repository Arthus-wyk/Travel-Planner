import React, { useEffect, useRef, useState } from "react";
import { Bike, BusFront, CarFront, Footprints, Route } from "lucide-react";
import PlacePhoto from "./PlacePhoto";
import { sheetMapData, sheetMapProjection, sheetRouteModes } from "./sheetRoutes";

const width = 800;
const height = 440;
const paper = "#f4ecdc";
const ink = "#241f19";
const routeColor = "#a84322";
const modes = {
  WALK: { color: "#a84322", dash: "5 7", Icon: Footprints },
  DRIVE: { color: "#2357a0", Icon: CarFront },
  TRANSIT: { color: "#176745", dash: "13 5 2 5", Icon: BusFront },
  BICYCLE: { color: "#874185", dash: "2 6", Icon: Bike },
  TWO_WHEELER: { color: "#865115", dash: "8 4", Icon: Bike },
};
const defaultMode = { color: routeColor, Icon: Route };
const modeLabel = {
  ...sheetRouteModes, route: "已确认路线", same_place: "同一地点", intercity: "跨城交通", pending: "路线待确认",
};

function stationRole(index, count) {
  if (count === 1) return "出发 / 终点";
  return index === 0 ? "出发" : index === count - 1 ? "终点" : "";
}

function layoutMap(stations, project) {
  const pins = stations.filter(station => station.coordinate).map(station => {
    const [anchorX, anchorY] = project(station.coordinate);
    return { ...station, anchorX, anchorY, x: anchorX, y: anchorY };
  });
  const used = [];
  for (const pin of pins) {
    let ring = 0;
    while (used.some(({ x, y }) => Math.hypot(x - pin.x, y - pin.y) < 62)) {
      ++ring;
      const angle = ring * 2.39996;
      pin.x = Math.max(32, Math.min(width - 32, pin.anchorX + 30 * Math.sqrt(ring) * Math.cos(angle)));
      pin.y = Math.max(32, Math.min(height - 32, pin.anchorY + 30 * Math.sqrt(ring) * Math.sin(angle)));
      if (ring === 64) break;
    }
    used.push(pin);
  }
  const labels = [];
  const overflow = [];
  const byNumber = new Map(pins.map(pin => [pin.number, pin]));
  for (const station of stations) {
    const pin = byNumber.get(station.number);
    const name = Array.from(station.title);
    const lines = [];
    for (let i = 0; i < name.length; i += 12) lines.push(name.slice(i, i + 12).join(""));
    const label = { station, lines, width: Math.max(148, Math.min(name.length, 12) * 28 + 24), height: 38 + lines.length * 31 };
    const x = pin && Math.max(8, Math.min(width - label.width - 8, pin.x - label.width / 2));
    const y = pin && [pin.y - label.height - 34, pin.y + 34].find(top =>
      top >= 8 && top + label.height <= height - 32 &&
      !pins.some(point => point.x >= x - 32 && point.x <= x + label.width + 32 &&
        point.y >= top - 32 && point.y <= top + label.height + 32) &&
      !labels.some(box => box.x < x + label.width + 8 && box.x + box.width + 8 > x &&
        box.y < top + label.height + 8 && box.y + box.height + 8 > top));
    if (y !== undefined) labels.push({ ...label, x, y });
    else overflow.push(label);
  }
  let mapHeight = height;
  for (let i = 0; i < overflow.length; i += 2) {
    const row = overflow.slice(i, i + 2);
    row.forEach((label, column) => labels.push({ ...label, x: 16 + column * 392, y: mapHeight + 12, width: 376 }));
    mapHeight += 12 + Math.max(...row.map(label => label.height));
  }
  return { pins, labels, mapHeight: mapHeight + (overflow.length ? 12 : 0) };
}

function direction(edges, fraction) {
  const length = edges.reduce((sum, edge) => sum + edge.length, 0);
  if (!length) return null;
  let middle = length * fraction;
  const edge = edges.find(edge => {
    if (edge.length > middle) return true;
    middle -= edge.length;
    return false;
  });
  if (!edge) return null;
  const { start: [x, y], end: [nx, ny] } = edge;
  const ratio = middle / edge.length;
  return { translate: `translate(${x + (nx - x) * ratio} ${y + (ny - y) * ratio})`,
    rotate: `rotate(${Math.atan2(ny - y, nx - x) * 180 / Math.PI})` };
}

function wrapLines(ctx, text, maxWidth) {
  const lines = [];
  let line = "";
  for (const char of text) {
    if (line && ctx.measureText(line + char).width > maxWidth) {
      lines.push(line);
      line = "";
    }
    line += char;
  }
  lines.push(line);
  return lines;
}

async function downloadMap(svg, stations, title, subtitle, legend, source) {
  const mapHeight = svg.viewBox.baseVal.height;
  const artwork = svg.cloneNode(true);
  // shortcut: standalone SVG rasterization cannot load external POI photos; embed image bytes if photo export is added.
  artwork.querySelectorAll("foreignObject").forEach(node => node.remove());
  const blob = new Blob([new XMLSerializer().serializeToString(artwork)], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  try {
    const map = new Image();
    map.src = url;
    await map.decode();
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    ctx.font = "16px sans-serif";
    const entries = stations.map((station, index) => [
      station.number, station.time, station.title, stationRole(index, stations.length),
      station.coordinate ? "" : "地点待确认", station.nextMode ? `下一段：${modeLabel[station.nextMode]}` : "",
    ].filter(Boolean).join("　"));
    const rows = entries.flatMap(entry => wrapLines(ctx, entry, width - 48));
    const canvasHeight = mapHeight + 172 + rows.length * 22;
    canvas.width = width * 2;
    canvas.height = canvasHeight * 2;
    ctx.scale(2, 2);
    ctx.fillStyle = paper;
    ctx.fillRect(0, 0, width, canvasHeight);
    ctx.fillStyle = ink;
    ctx.font = "bold 28px sans-serif";
    ctx.fillText(title, 24, 38, width - 48);
    ctx.font = "16px sans-serif";
    ctx.fillText(subtitle, 24, 64);
    ctx.drawImage(map, 0, 82, width, mapHeight);
    ctx.fillText(source, 24, mapHeight + 109);
    ctx.fillText(legend, 24, mapHeight + 132);
    rows.forEach((row, index) => ctx.fillText(row, 24, mapHeight + 157 + index * 22));
    const png = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
    if (!png) throw new Error("PNG export failed");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(png);
    link.download = `${title.replace(/[<>:"/\\|?*]/g, "_")}-${subtitle}.png`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } finally { URL.revokeObjectURL(url); }
}

export default function SheetDayMap({ api, planId, day, date, title, stops, onStation }) {
  const svg = useRef(null);
  const [map, setMap] = useState({ busy: true });
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const [exportError, setExportError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setMap({ busy: true });
    api(`/api/plans/${encodeURIComponent(planId)}/planning/map?day=${day}&sheet=true`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setMap({ data }); })
      .catch(() => { if (!controller.signal.aborted) setMap({ error: true }); });
    return () => controller.abort();
  }, [api, planId, day, revision]);
  const data = sheetMapData(stops, map.data);
  const basemap = map.data?.basemap;
  const project = sheetMapProjection(data, basemap, width, height);
  const { pins, labels, mapHeight } = layoutMap(data.stations, project);
  const legs = new Map();
  for (const { origin, mode, path } of data.lines) {
    if (!legs.has(origin)) legs.set(origin, { mode, paths: [] });
    legs.get(origin).paths.push(path.map(project));
  }
  const subtitle = `第 ${day} 天　${date}`;
  const usedModes = [...new Set(data.lines.map(line => line.mode))];
  const legend = usedModes.map(mode => modeLabel[mode]).join("　");
  const source = basemap?.image ? "地图与保存路线：高德地图" : pins.length ? "已定位地点与路线示意" : "当天暂无可绘制的地点";

  async function save() {
    setSaving(true);
    setExportError("");
    try { await downloadMap(svg.current, data.stations, title, subtitle, legend, source); }
    catch { setExportError("路线图片生成失败，请重试。"); }
    finally { setSaving(false); }
  }

  return <div className="sheet-map">
    <div className="sheet-map-heading"><span>{subtitle}</span><button type="button" className="secondary-button" disabled={saving || map.busy} onClick={save}>{saving ? "正在生成图片…" : "保存为图片"}</button></div>
    {map.busy && <p role="status" className="guided-note">正在读取路线底图…</p>}
    {map.error && <p role="status" className="guided-note">路线地图读取失败，仍可查看当天站点。 <button type="button" onClick={() => setRevision(value => value + 1)}>重试地图</button></p>}
    <svg ref={svg} className="sheet-map-art" viewBox={`0 0 ${width} ${mapHeight}`} width={width} height={mapHeight} role="group" aria-label={`第 ${day} 天路线图`}>
      <defs><pattern id="sheet-map-grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="#d8ccb6" strokeWidth="1" /></pattern></defs>
      <rect width={width} height={mapHeight} fill={paper} />
      {basemap?.image ? <image href={basemap.image} width={width} height={height} /> : <rect width={width} height={height} fill="url(#sheet-map-grid)" />}
      {[...legs].map(([origin, { mode, paths }]) => {
        const edges = paths.flatMap(points => points.slice(1).map((end, index) => {
          const start = points[index];
          return { start, end, length: Math.hypot(end[0] - start[0], end[1] - start[1]) };
        }));
        const arrow = direction(edges, 0.77);
        const midpoint = direction(edges, 0.5);
        const { color, dash, Icon } = modes[mode] || defaultMode;
        return <g key={origin} aria-label={modeLabel[mode]}>
          {paths.map((points, index) => {
            const polyline = points.map(point => point.join(",")).join(" ");
            return <g key={index}>
              <polyline points={polyline} fill="none" stroke={paper} strokeWidth="7" strokeLinejoin="round" />
              <polyline points={polyline} fill="none" stroke={color} strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" strokeDasharray={dash} />
            </g>;
          })}
          {arrow && <path transform={`${arrow.translate} ${arrow.rotate}`} d="M-7-6L7 0-7 6Z" fill={color} stroke={paper} strokeWidth="1" />}
          {midpoint && <g transform={midpoint.translate} aria-hidden="true">
            <circle r="13" fill="#fffaf0" stroke={color} strokeWidth="2" />
            <Icon x="-8" y="-8" width="16" height="16" color={color} />
          </g>}
        </g>;
      })}
      {!pins.length && <text x={width / 2} y={height / 2} textAnchor="middle" fontSize="30" fill={ink}>{map.busy ? "正在读取地点" : source}</text>}
      <g fontFamily="Microsoft YaHei UI, PingFang SC, sans-serif" fill={ink}>
        {pins.map(pin => {
          const { number, x, y } = pin;
          return <g key={number} className="sheet-map-pin" role="button" tabIndex="0" aria-label={`${number} ${pin.title} ${pin.time}`}
            onClick={() => onStation(number)} onKeyDown={event => {
              if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onStation(number); }
            }}>
            <title>{pin.title} {pin.time}</title>
            {(x !== pin.anchorX || y !== pin.anchorY) && <path d={`M${pin.anchorX} ${pin.anchorY}L${x} ${y}`} stroke={ink} strokeWidth="2" />}
            <circle cx={x} cy={y} r="30" fill="#fffaf0" stroke={ink} strokeWidth="2" />
            <text x={x} y={y} textAnchor="middle" dominantBaseline="central" fontSize="17" fontWeight="700">{number}</text>
            <foreignObject x={x - 27} y={y - 27} width="54" height="54" pointerEvents="none">
              <div className="roadbook-photo-pin">
                <PlacePhoto item={stops[number - 1].item} mapPin />
                <span>{pin.title}</span>
              </div>
            </foreignObject>
          </g>;
        })}
        {mapHeight > height && <path d={`M0 ${height}H${width}`} stroke={ink} />}
        {labels.map(({ station, lines, x, y, width: labelWidth, height: labelHeight }) => {
          const role = stationRole(station.number - 1, data.stations.length);
          const meta = [station.number, station.time, role, !station.coordinate && "地点待确认"].filter(Boolean).join("　");
          return <g key={station.number} className="sheet-map-label" onClick={() => onStation(station.number)}>
            <rect x={x} y={y} width={labelWidth} height={labelHeight} rx="5" fill="#fffaf0" stroke={ink} />
            <text x={x + 12} y={y + 25} fontSize="20" textLength={meta.length * 14 > labelWidth - 24 ? labelWidth - 24 : undefined} lengthAdjust="spacingAndGlyphs">{meta}</text>
            <text className="sheet-map-name" fontSize="28" fontWeight="600">{lines.map((line, index) =>
              <tspan key={index} x={x + 12} y={y + 55 + index * 31}>{line}</tspan>)}</text>
          </g>;
        })}
      </g>
    </svg>
    <div className="sheet-map-legend roadbook-mode-key">
      <span>{source}</span>
      {usedModes.map(mode => {
        const { color, dash, Icon } = modes[mode] || defaultMode;
        return <span key={mode} style={{ color }}><svg viewBox="0 0 28 12" width="28" height="12" aria-hidden="true">
          <path d="M2 6H26" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeDasharray={dash} />
        </svg><Icon size={16} aria-hidden="true" />{modeLabel[mode]}</span>;
      })}
    </div>
    <ol className="sheet-map-index" aria-label="路线站点">
      {data.stations.map((station, index) => <li key={station.number}>
        <button type="button" onClick={() => onStation(station.number)}>
          <span className="sheet-stop-number">{station.number}</span><time>{station.time}</time><strong>{station.title}</strong>
          <small>{[stationRole(index, data.stations.length), !station.coordinate && "地点待确认", station.nextMode && `下一段：${modeLabel[station.nextMode]}`].filter(Boolean).join("，")}</small>
        </button>
      </li>)}
    </ol>
    {exportError && <p role="alert">{exportError}</p>}
  </div>;
}
