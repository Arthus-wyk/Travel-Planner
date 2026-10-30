import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ActivityDetailsEditor, AuditView, GenerationView } from "./GuidedPlanner";
import "./guided-planner.css";
import { apiUrl } from "./apiUrl";
import { categoryLabels, isMovableStop, transportLabels } from "./planningLabels";

import { refreshAfterCommit } from "./api";
import { auditDestination } from "./auditActions";
import { validMoney, focusIssue } from "./planningValidation";
import PlacePicker from "./PlacePicker";
import { BusyStatus, LoadError } from "./planningUi";

const encode = encodeURIComponent;
const modes = { WALK: "步行", TRANSIT: "公交", DRIVE: "驾车" };

export default function ReviewPage({ api, refreshPlans }) {
  const { planId } = useParams();
  const navigate = useNavigate();
  const [plan, setPlan] = useState(null);
  const [audit, setAudit] = useState(null);
  const [day, setDay] = useState(1);
  const [map, setMap] = useState(null);
  const [mapError, setMapError] = useState("");
  const [mapBusy, setMapBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const requestBusy = useRef(false);
  const [error, setError] = useState("");
  const [dragged, setDragged] = useState(null);
  const [target, setTarget] = useState(null);
  const [revision, setRevision] = useState(0);
  const [mapRevision, setMapRevision] = useState(0);
  const [notice, setNotice] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [budget, setBudget] = useState("");
  const [focusTarget, setFocusTarget] = useState(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (focusTarget) { focusIssue(focusTarget); setFocusTarget(null); } }, [focusTarget, day]);
  useEffect(() => {
    let active = true;
    setPlan(null); setAudit(null); setDay(1); setError("");
    Promise.all([api(`/api/plans/${encode(planId)}`), api(`/api/plans/${encode(planId)}/planning/audit`)]).then(([data, check]) => {
      if (!active) return;
      if (!["review", "confirmed"].includes(data.plan.planning?.stage)) { navigate(`/plans/${encode(planId)}/planning`, { replace: true }); return; }
      setPlan(data.plan); setBudget(data.plan.budget); setAudit(check.audit);
    }).catch((cause) => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [api, planId, navigate, revision]);
  useEffect(() => {
    if (!plan) return;
    let active = true;
    setMap(null); setMapError(""); setMapBusy(true);
    api(`/api/plans/${encode(planId)}/planning/map?day=${day}`).then((data) => { if (active) setMap(data); })
      .catch((cause) => { if (active) setMapError(cause.message); })
      .finally(() => { if (active) setMapBusy(false); });
    return () => { active = false; };
  }, [api, planId, plan, day, mapRevision]);
  async function action(name, payload = {}) {
    if (requestBusy.current || uncertain) return null;
    requestBusy.current = true;
    setBusy(true); setError("");
    try {
      const data = await api(`/api/plans/${encode(planId)}/planning/${name}`, { method: "POST", body: JSON.stringify(payload) });
      if (!mounted.current) return data;
      setPlan(data.plan); setBudget(data.plan.budget); setAudit(data.audit || null);
      setNotice('修改已保存');
      void refreshAfterCommit(refreshPlans, setNotice);
      return data;
    } catch (cause) { if (mounted.current) { setError(cause.message); setUncertain(Boolean(cause.uncertain)); } return null; }
    finally { requestBusy.current = false; if (mounted.current) setBusy(false); }
  }
  async function move(id, targetDay, before = null) {
    setDragged(null); setTarget(null);
    if (!busy) await action("move_activity", { item_id: id, target_day: targetDay, before_item_id: before });
  }
  function dropProps(targetDay, before = null) {
    const key = `${targetDay}:${before || "end"}`;
    return {
      onDragOver: (event) => { if (busy) return; event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = "move"; setTarget(key); },
      onDragLeave: () => setTarget((current) => current === key ? null : current),
      onDrop: (event) => { event.preventDefault(); event.stopPropagation(); const id = event.dataTransfer.getData("text/plain") || dragged; if (id && id !== before) move(id, targetDay, before); },
    };
  }
  if (!plan) return <div className="guided-page">{error ? <LoadError error={error} onRetry={() => setRevision(x => x + 1)} /> : <p role="status">加载路线与预算审核…</p>}</div>;
  const state = plan.planning || {};
  const editable = state.stage === "review";
  const days = Math.round((new Date(`${plan.end_date}T00:00:00Z`) - new Date(`${plan.start_date}T00:00:00Z`)) / 86400000) + 1;
  const items = (plan.itinerary || []).filter((item) => item.day === day).sort((a, b) => a.time_of_day.localeCompare(b.time_of_day));
  const pending = state.unscheduled_items || [];
  const routeFrom = (id) => (plan.routes || []).find((route) => route.origin_item_id === id);
  async function editPlaces() { if (await action('edit_places')) navigate(`/plans/${encode(planId)}/planning`); }
  function handleIssue(issue) {
    if (!editable) { focusIssue('confirmed-repair'); return; }
    const destination = auditDestination(issue, plan);
    if (destination.kind === 'budget') { focusIssue('review-budget'); return; }
    if (destination.kind === 'routes') { focusIssue('review-route-tools'); return; }
    if (destination.kind === 'purchase') { focusIssue(`purchase-${destination.id}`); return; }
    if (destination.kind === 'replan') { focusIssue('review-replan-tools'); return; }
    if (destination.kind === 'day') { setDay(destination.day); setFocusTarget('review-today'); return; }
    if (destination.kind === 'pending') { setFocusTarget(`review-${destination.id}`); return; }
    setDay(destination.day); setFocusTarget(`review-${destination.id}`);
  }
  const highIssues = (audit?.issues || []).filter(x => x.severity === 'high');
  function slot(before = null) {
    return editable && <div className={`review-drop-slot ${dragged ? "available" : ""} ${target === `${day}:${before || "end"}` ? "target" : ""}`} {...dropProps(day, before)}>{dragged ? "放在这里" : ""}</div>;
  }
  function card(item, isPending = false) {
    const movable = editable && isMovableStop(item);
    const reason = item.reason || state.generation?.stops?.find((stop) => stop.activity_id === item.reference_id)?.reason;
    const index = map?.points?.findIndex((p) => p.item_id === item.item_id) ?? -1;
    return <article key={item.item_id} id={`review-${item.item_id}`} tabIndex={-1} className={`review-stop ${movable ? "movable" : "fixed"} ${dragged === item.item_id ? "dragging" : ""}`} draggable={movable && !busy && !uncertain}
      onDragStart={(event) => { if (!movable || event.target.closest("input,button,select,textarea,summary,form")) { event.preventDefault(); return; } event.dataTransfer.setData("text/plain", item.item_id); event.dataTransfer.effectAllowed = "move"; setDragged(item.item_id); }}
      onDragEnd={() => { setDragged(null); setTarget(null); }}>
      <div className="review-stop-heading"><span className="stop-number">{index >= 0 && index < 10 ? index + 1 : movable ? "⠿" : "•"}</span><div><strong>{item.title}</strong><small>{item.location}</small></div><time>{isPending ? "待安排" : item.time_of_day}</time></div>
      <p className="guided-item-meta">{item.duration_minutes ? `约 ${item.duration_minutes} 分钟` : "起终点"}{!isMovableStop(item) && " · 固定节点"}{categoryLabels[item.category] && ` · ${categoryLabels[item.category]}`}</p>
      {reason && <p className="guided-item-tips">安排理由：{reason}</p>}
      {item.category === "intercity_transport" && <><p className="guided-note">{transportLabels[(state.transport_candidates || []).find((candidate) => candidate.id === item.reference_id)?.kind] || "城际交通"} · 出发：{item.location} → 到达：{item.arrival_location || "待确认"}{!item.station_confirmed && " · 请确认实际车站 / 机场"}</p>{editable && <details className="guided-item-editor"><summary>设置车站 / 机场接驳地点</summary><form onSubmit={(event) => { event.preventDefault(); const values = new FormData(event.currentTarget); action("transport_stops", { item_id: item.item_id, departure_location: values.get("departure_location"), arrival_location: values.get("arrival_location") }); }}><label>出发站 / 机场<input name="departure_location" defaultValue={item.station_confirmed ? item.location : ""} required maxLength={200} placeholder="例如：上海虹桥站" /></label><label>到达站 / 机场<input name="arrival_location" defaultValue={item.station_confirmed ? item.arrival_location : ""} required maxLength={200} placeholder="例如：杭州东站" /></label><button disabled={busy || uncertain}>保存并计算接驳</button></form></details>}</>}
      {item.place?.image_cache_key && <details className="review-place-map"><summary>查看地点地图</summary><img src={apiUrl(`/api/maps/images/${item.place.image_cache_key}`)} alt={`${item.title}的位置地图`} loading="lazy" /></details>}
      {movable && <div className="review-card-controls">{isPending ? <button disabled={busy || uncertain} onClick={() => move(item.item_id, day)}>加入第 {day} 天</button> : <>
        <button disabled={busy || uncertain} onClick={() => move(item.item_id, null)}>移至未确定</button>
        <select aria-label={`移动${item.title}至其他日期`} value="" disabled={busy || uncertain} onChange={(e) => { if (e.target.value) move(item.item_id, Number(e.target.value)); }}><option value="">移动到其他天…</option>{Array.from({ length: days }, (_, n) => n + 1).filter((n) => n !== day).map((n) => <option key={n} value={n}>第 {n} 天</option>)}</select>
        {items.filter((x) => isMovableStop(x)).findIndex((x) => x.item_id === item.item_id) > 0 && <button disabled={busy || uncertain} onClick={() => { const visits = items.filter((x) => isMovableStop(x)); move(item.item_id, day, visits[visits.findIndex((x) => x.item_id === item.item_id) - 1].item_id); }}>上移</button>}
      </>}</div>}
      {movable && !isPending && <ActivityDetailsEditor key={`${item.item_id}-${item.time_of_day}-${item.duration_minutes}-${item.estimated_cost}-${item.ticket_required}-${item.visit_tips}`} item={item} busy={busy} onEdit={(id, details) => action("update_activity", { activity_id: id, ...details })} />}
    </article>;
  }
  return <div className="guided-page review-page" aria-busy={busy}>
    <header className="guided-header"><Link to={`/plans/${encode(planId)}/planning`}>← 规划向导</Link><h1>{plan.destination} · 路线与预算审核</h1><p>{plan.start_date} 至 {plan.end_date} · {plan.travelers} 人</p></header>
    {error && <p className="guided-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}{notice.includes('列表') && <button onClick={() => refreshAfterCommit(refreshPlans, setNotice)}>刷新列表</button>}</p>}
    <BusyStatus task={busy ? '正在保存、计算路线与更新审核' : ''} />
    {uncertain && <div className="guided-error"><p>结果待核实，请重新读取已保存状态；若服务仍在执行，请等待完成。</p><button onClick={() => { setUncertain(false); setRevision(x => x + 1); }}>重新读取并核实</button></div>}
    {state.guide_auto && <section className="guided-card"><h2>攻略匹配与待处理地点</h2>
      {state.lodging_assumption && <p>{state.lodging_assumption}</p>}
      {(state.activities || []).filter(item => item.choice !== "exclude" && !item.place && item.category !== "hotel").map(item => <article key={item.id}>
        <h3>{item.name}{item.day ? ` · 原文第 ${item.day} 天` : ""}</h3><p>{item.note}</p>
        <PlacePicker item={item} api={api} planId={planId} busy={busy || !editable} onResolve={poi => action("resolve_place", { activity_id: item.id, poi_id: poi })} />
        <button disabled={busy || !editable} onClick={() => action("exclude_activity", { activity_id: item.id })}>移除此地点</button>
      </article>)}
      {editable && <button disabled={busy || uncertain} onClick={async () => {
        setBusy(true);
        try { await api(`/api/plans/${encode(planId)}/generation`, { method: "POST", body: JSON.stringify({ text: state.guide_text || "" }) }); navigate(`/plans/${encode(planId)}/planning`); }
        catch (cause) { setError(cause.message); } finally { setBusy(false); }
      }}>按当前地点重新生成</button>}
    </section>}
    <section className="guided-card review-summary"><h2>{highIssues.length ? `有 ${highIssues.length} 项需要处理` : '审核状态'}</h2><AuditView audit={audit} plan={plan} onIssue={handleIssue} busy={busy || uncertain} />{!editable && <div id="confirmed-repair" className="guided-actions"><p>已确认行程需先返回地点选择，修改后重新生成并审核；购买记录会保留。</p><button disabled={busy || uncertain} onClick={editPlaces}>返回编辑行程</button></div>}</section>
    <section className="review-map-panel" aria-label={`第${day}天的路线地图`}>
      <div className="review-map-caption"><strong>第 {day} 天 · 行程路线图</strong><span>高德地图 · 地点编号对应下方卡片</span></div>
      {mapBusy ? <div className="review-map-placeholder" role="status">正在读取当天路线地图…</div> : map && !mapError ? <img key={mapRevision} className="review-day-map" onError={() => setMapError("地图图片加载失败，地点与文字路线仍可查看。")} src={apiUrl(map.image_url)} alt={`第${day}天的地点和规划路线`} /> : <div className="review-map-placeholder" role="status">{mapError || "确认地点后显示路线地图"}</div>}
      {mapError && <button disabled={mapBusy} onClick={() => setMapRevision(x => x + 1)}>重试当天地图</button>}
      {map?.warnings?.map((warning) => <p key={warning} className="guided-note">{warning}</p>)}
      {map?.marker_limit && <p className="guided-note">静态地图最多标记 10 个地点，完整行程见下方卡片。</p>}
    </section>
    <div className="review-columns">
      <section className="review-today" id="review-today" tabIndex={-1}><nav className="review-day-nav" aria-label="选择行程日期">{Array.from({ length: days }, (_, n) => n + 1).map((n) => <button key={n} aria-pressed={day === n} onClick={() => setDay(n)}>第 {n} 天</button>)}</nav>
        <h2>当天行程 <small>{items.filter((x) => isMovableStop(x)).length} 个停留地点</small></h2>
        {busy && <p role="status" className="guided-note">正在保存、计算路线与更新审核…</p>}
        <div className="review-day-list" {...(editable ? dropProps(day) : {})}>
          {items.map((item) => { const route = routeFrom(item.item_id); return <React.Fragment key={item.item_id}>
            {isMovableStop(item) && slot(item.item_id)}
            {card(item)}
            {route && <div className="review-route-leg"><span>↓ {route.station_transfer ? "车站 / 机场接驳" : "前往下一站"}</span><select aria-label={`${item.title}至下一站的交通方式`} value={route.travel_mode || "TRANSIT"} disabled={busy || uncertain || !editable} onChange={(e) => action("route_mode", { origin_item_id: route.origin_item_id, destination_item_id: route.destination_item_id, travel_mode: e.target.value })}>{Object.entries(modes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><small>{route.duration_minutes} 分钟{route.distance_meters != null && ` · ${(route.distance_meters / 1000).toFixed(1)} 公里`}{route.source === "provisional_estimate" && " · 临时估算"}</small>{route.error && <p role="status">{route.error}</p>}</div>}
          </React.Fragment>; })}
          {slot()}
        </div>
      </section>
      <aside className={`review-pending ${target === "null:end" ? "target" : ""}`} {...(editable ? dropProps(null) : {})}><h2>未确定的行程 <small>{pending.length}</small></h2><p className="guided-note">把地点卡片拖到这里暂存，再切换日期，拖回当天行程。</p>{pending.length ? pending.map((item) => card(item, true)) : <div className="review-pending-empty">拖动卡片到这里<br />或点击卡片上的“移至未确定”</div>}</aside>
    </div>
    <GenerationView generation={state.generation} activities={state.activities || []} />
    <section className="guided-card"><h2>时间与预算检查</h2><AuditView audit={audit} plan={plan} onIssue={handleIssue} busy={busy || uncertain} />
      {editable && <>
        <form id="review-budget" className="review-budget-form" onSubmit={event => { event.preventDefault(); if (validMoney(budget)) action('update_budget', { budget: Number(budget) }); }}><label>调整总预算（元）<input type="number" min="0" step="0.01" required value={budget} onChange={event => setBudget(event.target.value)} /></label><button disabled={busy || uncertain || !validMoney(budget)}>保存预算并重新审核</button></form>
        <div id="review-route-tools" className="guided-actions"><button disabled={busy || uncertain} onClick={() => action('recalculate_routes')}>重新计算路线与审核</button><span>保留当前地点和日期；缺失的起终点请重新生成行程。</span></div>
        <div id="review-replan-tools" className="guided-actions"><button disabled={busy || uncertain} onClick={editPlaces}>修改地点、要求或重新生成</button><button disabled={busy || uncertain} onClick={async () => { if (await action('candidates')) navigate(`/plans/${encode(planId)}/planning`); }}>重新比较交通</button></div>
        {(state.purchases || []).filter(record => highIssues.some(issue => issue.type === 'purchased_item_changed' && issue.target === record.reference_id)).map(record => <div key={record.reference_id} id={`purchase-${record.reference_id}`} tabIndex={-1} className="guided-issues"><p>{record.label} 已支付 {record.actual_total} 元，当前已不在行程中。确认保留该笔支出后仍计入预算；此操作不办理退票或退款。</p><button disabled={busy || uncertain} onClick={() => action('acknowledge_purchase', { reference_id: record.reference_id })}>确认变更并保留支出</button><button disabled={busy || uncertain} onClick={editPlaces}>返回重新选择</button></div>)}
      </>}
      <p className="guided-note">停留开始时间可在卡片内调整；拖动后按路线耗时重新排时。家、酒店起终点和已选城际交通固定。交通预算沿用每人每日交通额度，路线选择会更新交通时间。</p>
      <div className="guided-actions guided-primary-actions">{editable ? <><button disabled={busy || uncertain} onClick={async () => { if (await action("edit_places")) navigate(`/plans/${encode(planId)}/planning`); }}>返回地点选择</button><button className="primary-button" disabled={busy || uncertain || !audit || (audit.issues || []).some((x) => x.severity === "high")} onClick={async () => { if (await action("confirm")) navigate(`/plans/${encode(planId)}/sheet`); }}>确认并生成行程单</button></> : <Link className="primary-button" to={`/plans/${encode(planId)}/sheet`}>查看行程单</Link>}</div>
    </section>
  </div>;
}
