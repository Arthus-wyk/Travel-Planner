import React, { useEffect, useState } from "react";
import { orderedSheetStops, sheetMapData, sheetMapProjection } from "./sheetRoutes";

const colors = ["#a53c25", "#176745", "#2357a0", "#874185", "#865115", "#176977", "#9e365d", "#4a5380"];
const width = 800;
const height = 440;

export default function RoadbookOverview({ api, planId, sheet, summaries, onDay }) {
  const [map, setMap] = useState(null);
  const [mapError, setMapError] = useState(false);
  const [mapRetry, setMapRetry] = useState(0);
  const [selected, setSelected] = useState(sheet.days[0]?.day);
  const url = `/api/plans/${encodeURIComponent(planId)}/roadbook`;

  useEffect(() => {
    const controller = new AbortController();
    setMapError(false);
    api(`${url}/map`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setMap(value); })
      .catch(() => { if (!controller.signal.aborted) setMapError(true); });
    return () => controller.abort();
  }, [api, url, mapRetry]);

  const days = sheet.days.map(({ day, items }, index) => ({
    day, color: colors[index] || `hsl(${(index * 137.508) % 360} 63% 32%)`,
    ...sheetMapData(orderedSheetStops(items, sheet.routes || []), map?.days?.[day]),
  }));
  const project = sheetMapProjection({
    stations: days.flatMap(day => day.stations), lines: days.flatMap(day => day.lines),
  }, map?.basemap, width, height);
  const active = days.find(day => day.day === selected) || days[0];
  if (!active) return <p className="guided-note">暂无每日行程安排。</p>;
  const layers = [...days.filter(day => day !== active), active];
  const anchor = day => day.lines[0]?.path[Math.floor(day.lines[0].path.length / 2)]
    || day.stations.find(station => station.coordinate)?.coordinate;
  const activeAnchor = anchor(active);
  const [x, y] = activeAnchor ? project(activeAnchor) : [width / 2, height / 2];

  return <div className="roadbook-overview">
    <div className="roadbook-atlas">
      <svg viewBox={`0 0 ${width} ${height}`} role="group" aria-label="全程路线地图，颜色对应旅行日期">
        {map?.basemap?.image && <image href={map.basemap.image} width={width} height={height} />}
        {layers.map(day => <g key={day.day} strokeLinejoin="round" strokeLinecap="round" fill="none">
          {day.lines.map(({ path }, index) => {
            const points = path.map(project).map(point => point.join(",")).join(" ");
            return <g key={index}><polyline points={points} stroke="#fffbef" strokeWidth="8" />
              <polyline points={points} stroke={day.color} strokeWidth="4" data-roadbook-day={day.day} /></g>;
          })}
          {day.stations.filter(({ coordinate }) => coordinate).map(({ number, coordinate }) => {
            const [cx, cy] = project(coordinate);
            return <circle key={number} cx={cx} cy={cy} r="4" fill={day.color} stroke="white" />;
          })}
        </g>)}
        {layers.filter(day => anchor(day)).map(day => {
          const [cx, cy] = project(anchor(day));
          return <g key={day.day} role="button" tabIndex="0" aria-label={`查看第 ${day.day} 天概述`}
            className="roadbook-map-day" onClick={() => setSelected(day.day)} onKeyDown={event => {
              if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelected(day.day); }
            }}>
            <circle cx={cx} cy={cy} r="15" fill={day.color} stroke="white" strokeWidth="2" />
            <text x={cx} y={cy} textAnchor="middle" dominantBaseline="central" fill="white" fontSize="15">{day.day}</text>
          </g>;
        })}
      </svg>
      <div className="roadbook-callout" data-above={y >= height / 2} style={{
        "--day-color": active.color,
        "--map-x": `${Math.max(0, Math.min(100, x / width * 100))}%`,
        "--map-y": `${Math.max(0, Math.min(100, y / height * 100))}%`,
      }}>
        <strong>第 {active.day} 天</strong>
        <p aria-live="polite">{summaries[active.day]}</p>
      </div>
    </div>
    {mapError && <p role="status" className="guided-note">路线暂时无法载入。 <button type="button" onClick={() => setMapRetry(value => value + 1)}>重试地图</button></p>}
    {!map && !mapError && <p role="status" className="guided-note">正在读取全程路线…</p>}
    {map && <p className="sheet-map-legend">{map.basemap?.image ? "底图与路线来自高德地图" : "已定位地点与路线示意"}</p>}
    <div className="roadbook-day-index" aria-label="逐日概述">
      {days.map(day => <button key={day.day} type="button" aria-pressed={active.day === day.day}
        style={{ "--day-color": day.color }} onClick={() => setSelected(day.day)}>第 {day.day} 天</button>)}
      <button type="button" className="primary-button roadbook-preview" onClick={() => onDay(active.day)}>预览第 {active.day} 天 →</button>
    </div>
  </div>;
}
