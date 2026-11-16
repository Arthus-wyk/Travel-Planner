import React, { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import "./guided-planner.css";
import GuideGeneration from "./GuideGeneration";
import { refreshAfterCommit } from "./api";
import { transportIssues, validMoney } from "./planningValidation";
import { BusyStatus, IssueList, LoadError, useUnsavedChanges } from "./planningUi";
import { transportLabels, categoryLabels, isMovableStop } from "./planningLabels";
import { orderedSheetStops, routeInstructions, sheetRouteModes, sheetStopTime } from "./sheetRoutes";
import SheetDayMap from "./SheetDayMap";
import RoadbookOverview from "./RoadbookOverview";
import PlacePhoto from "./PlacePhoto";

const encode = (value) => encodeURIComponent(value);
const returnDate = (start, days) => {
  if (!start || !Number.isInteger(Number(days)) || Number(days) < 1) return "待计算";
  const date = new Date(`${start}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + Number(days) - 1);
  return date.toISOString().slice(0, 10);
};

export function NewTripForm({ api, refreshPlans }) {
  const navigate = useNavigate();
  const [form, setForm] = useState({ destination: "", departure_city: "", home_location: "", start_date: "", trip_days: 3, travelers: 1, budget: "", currency: "CNY", interests: "", constraints: "", pace: "moderate" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const createLock = useRef(false);
  const [uncertain, setUncertain] = useState(false);
  const field = (name, value) => setForm((current) => ({ ...current, [name]: value }));
  async function submit(event) {
    event.preventDefault(); if (createLock.current || uncertain) return; createLock.current = true; setBusy(true); setError("");
    try {
      const payload = { ...form, trip_days: Number(form.trip_days), travelers: Number(form.travelers), budget: Number(form.budget), interests: form.interests.split(/[,，、]/).map((x) => x.trim()).filter(Boolean), constraints: form.constraints.split(/[,，、]/).map((x) => x.trim()).filter(Boolean) };
      const created = await api("/api/plans/drafts", { method: "POST", body: JSON.stringify(payload) });
      void refreshAfterCommit(refreshPlans);
      navigate(`/plans/${encode(created.plan.plan_id)}/planning`);
    } catch (cause) { setError(cause.message); setUncertain(Boolean(cause.uncertain)); }
    finally { createLock.current = false; setBusy(false); }
  }
  return <div className="guided-page new-trip-page"><header className="guided-header"><Link to="/">← 返回</Link><h1>创建旅行计划</h1><p>先选择往返交通，再核对攻略、分类地点与住宿。</p></header>
    <form className="guided-card guided-form" onSubmit={submit}>
      <label>目的地<input required value={form.destination} onChange={(e) => field("destination", e.target.value)} placeholder="例如：杭州" /></label>
      <label>出发城市<input required value={form.departure_city} onChange={(e) => field("departure_city", e.target.value)} placeholder="例如：上海" /></label>
      <label className="wide">家或实际出发地点<input value={form.home_location} onChange={(e) => field("home_location", e.target.value)} placeholder="不填则使用出发城市" /></label>
      <label>出发日期<input required type="date" value={form.start_date} onChange={(e) => field("start_date", e.target.value)} /></label>
      <label>旅行天数<input required type="number" min="1" max="365" step="1" value={form.trip_days} onChange={(e) => field("trip_days", e.target.value)} /></label>
      <label>返回日期<input readOnly value={returnDate(form.start_date, form.trip_days)} /></label>
      <label>人数<input required type="number" min="1" step="1" value={form.travelers} onChange={(e) => field("travelers", e.target.value)} /></label>
      <label>总预算<input required type="number" min="0" step="0.01" value={form.budget} onChange={(e) => field("budget", e.target.value)} /></label>
      <label>币种<input value="CNY 人民币" readOnly /></label>
      <label>旅行节奏<select value={form.pace} onChange={(e) => field("pace", e.target.value)}><option value="relaxed">轻松</option><option value="moderate">适中</option><option value="active">充实</option></select></label>
      <label className="wide">兴趣（用逗号分隔）<input value={form.interests} onChange={(e) => field("interests", e.target.value)} placeholder="博物馆，美食" /></label>
      <label className="wide">特殊限制（用逗号分隔）<input value={form.constraints} onChange={(e) => field("constraints", e.target.value)} placeholder="例如：行动不便，避免深夜活动" /></label>
      {error && <p className="guided-error wide" role="alert">{error}</p>}
      {uncertain && <p className="wide">请先在侧栏或首页核实是否已创建行程。<button type="button" onClick={() => { if (window.confirm("已核实没有创建成功，允许重新创建？")) setUncertain(false); }}>已核实未创建</button></p>}
      <button className="primary-button wide" disabled={busy || uncertain}>{busy ? "创建中…" : "创建行程，选择交通"}</button>
    </form></div>;
}

function CandidateCard({ item, name, selected, onSelect, currency }) {
  const label = item.name || `${transportLabels[item.kind] || item.kind} ${item.origin} → ${item.destination}`;
  return <label className={`guided-option ${selected ? "selected" : ""}`}><input type="radio" name={name} checked={selected} onChange={onSelect} />
    <span><strong>{label}</strong><small>{item.date ? `${item.date} ${item.depart_time}–${item.arrive_time || "到达时间待计算"}` : `${item.location} · ${item.nights} 晚`}</small>{item.kind === "drive" && item.duration_minutes != null && <small>约 {item.duration_minutes} 分钟{item.distance_meters != null && ` · ${(item.distance_meters / 1000).toFixed(1)} 公里`}</small>}</span>
    <span className="guided-price">{item.cost_unknown || item.estimated_total == null ? "费用未知" : `${item.estimated_total} ${currency}`}<small>{item.kind === "drive" ? "燃油、路桥费待核对" : item.date ? "班次时间仅供规划" : "酒店房价待查询"}</small></span>
  </label>;
}

export function PlanningPage({ api, refreshPlans }) {
  const { planId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const viewingActivities = searchParams.get("step") === "activities";
  const generationJobId = useLocation().state?.generationJobId;
  const [plan, setPlan] = useState(null);
  const [selection, setSelection] = useState({ outbound: "", return: "" });
  const [kinds, setKinds] = useState({ outbound: "train", return: "train" });
  const [driveDepartures, setDriveDepartures] = useState({ outbound: "07:00", return: "17:00" });
  const [vehicles, setVehicles] = useState(1);
  const [guideText, setGuideText] = useState("");
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [hasGuideImage, setHasGuideImage] = useState(false);
  const [error, setError] = useState("");
  const requestBusy = useRef(false);
  const [dirty, setDirty] = useState(false);
  const [notice, setNotice] = useState("");
  const [task, setTask] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [revision, setRevision] = useState(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const savedGuideText = plan?.planning?.draft_text ?? plan?.planning?.guide_text ?? "";
  const allowNavigation = useUnsavedChanges(dirty || guideText !== savedGuideText || hasGuideImage || busy || generating);
  function hydrate(nextPlan) {
    const state = nextPlan.planning || {};
    setPlan(nextPlan); setGuideText(state.draft_text ?? state.guide_text ?? "");
    setDirty(false);
    const selected = state.selected_transport || {};
    setSelection({ outbound: selected.outbound || "", return: selected.return || "" });
    setKinds(Object.fromEntries(["outbound", "return"].map(direction => [direction, (state.transport_candidates || []).find(item => item.id === selected[direction])?.kind || "train"])));
    setDriveDepartures({ outbound: "07:00", return: "17:00", ...state.drive_departures });
    setVehicles(state.vehicles || 1);
  }
  useEffect(() => {
    let active = true;
    setPlan(null); setError("");
    api(`/api/plans/${encode(planId)}`).then(({ plan }) => {
      if (!active) return;
      hydrate(plan);
    }).catch((cause) => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [api, planId, revision]);
  useEffect(() => {
    // A saved review opens directly unless the URL explicitly requests activity editing.
    if (!viewingActivities && plan?.planning?.stage === "review") {
      navigate(`/plans/${encode(planId)}/review`, { replace: true });
    }
  }, [plan, planId, navigate, viewingActivities]);
  async function action(name, payload = {}) {
    if (requestBusy.current || uncertain) return null;
    if (name === "candidates" && plan.planning?.stage === "candidates" && dirty && !window.confirm("重新查询会重置尚未确认的交通选择，是否继续？")) return null;
    requestBusy.current = true; setBusy(true); setError("");
    const labels = { candidates: "正在查询交通候选", choose_candidates: "正在保存交通并查询住宿" };
    setTask(labels[name] || "正在保存");
    try {
      if (name === "candidates" && (plan.planning?.stage === "activities" || viewingActivities)) payload = { text: guideText, ...payload };
      const result = await api(`/api/plans/${encode(planId)}/planning/${name}`, { method: "POST", body: JSON.stringify(payload) });
      if (!mounted.current) return result;
      hydrate(result.plan);
      setNotice("修改已保存");
      void refreshAfterCommit(refreshPlans, setNotice);
      return result;
    } catch (cause) { if (mounted.current) { setError(cause.message); setUncertain(Boolean(cause.uncertain)); } return null; }
    finally { requestBusy.current = false; if (mounted.current) { setBusy(false); setTask(""); } }
  }
  async function reloadGeneratedPlaces() {
    // Keep inputs locked while loading the partial result so hydration cannot overwrite a new edit.
    setBusy(true);
    try {
      const { plan } = await api(`/api/plans/${encode(planId)}`);
      if (mounted.current) hydrate(plan);
    } catch (cause) {
      if (mounted.current) setError(cause.message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  function changeKind(direction, kind) {
    const nextKinds = kind === "drive" ? { outbound: "drive", return: "drive" } : kinds[direction] === "drive" ? { outbound: kind, return: kind } : { ...kinds, [direction]: kind };
    setKinds(nextKinds);
    setSelection((current) => Object.fromEntries(["outbound", "return"].map((side) => {
      const candidates = (plan.planning?.transport_candidates || []).filter((item) => item.direction === side && item.kind === nextKinds[side]);
      const selected = candidates.some((item) => item.id === current[side]) ? current[side] : nextKinds[side] === "drive" ? candidates[0]?.id || "" : "";
      return [side, selected];
    })));
  }
  if (!plan) return <div className="guided-page">{error ? <LoadError error={error} onRetry={() => setRevision(x => x + 1)} /> : <p role="status">正在加载旅行计划…</p>}</div>;
  const state = plan.planning || {};
  const transports = state.transport_candidates || [];
  const stage = state.stage || "requirements";
  const canReview = ["review", "confirmed"].includes(stage);
  const displayStage = viewingActivities && canReview ? "activities" : stage;
  const driving = kinds.outbound === "drive";
  const transportProblems = transportIssues(selection, driving, vehicles, driveDepartures);
  if (stage === "review" && !viewingActivities) return <p role="status">正在打开路线审核…</p>;
  return <div className="guided-page" aria-busy={busy || generating}><header className="guided-header"><Link to={`/plans/${encode(planId)}`}>← 行程详情</Link><h1>{plan.destination}规划向导</h1><p className="guided-head-meta"><span>{plan.start_date} 至 {plan.end_date}</span><span>{plan.travelers} 人</span><span>预算 {plan.budget} {plan.currency}</span></p></header>
    <nav className="guided-steps" aria-label="规划步骤">{[['candidates', '1 选交通'], ['activities', '2 选活动与攻略'], ['review', '3 审核路线'], ['confirmed', '4 行程单']].map(([step, label], index) => {
      const current = ['requirements', 'candidates'].includes(displayStage) ? 0 : ['activities', 'review', 'confirmed'].indexOf(displayStage) + 1;
      const enabled = index === 0 || (index === 1 && Boolean(state.selected_transport?.outbound)) || (index === 2 && canReview) || (index === 3 && stage === 'confirmed');
      return <button key={step} type="button" className={index === current ? 'active' : index < current ? 'complete' : ''} aria-current={index === current ? 'step' : undefined} disabled={!enabled || busy || generating || uncertain} onClick={async () => {
        if (step === displayStage) return;
        if (step === 'candidates') await action('candidates');
        else if (step === 'activities') {
          if (stage === 'candidates' && dirty) { setError('交通选择尚未确认，请先点击“确认交通，继续”。'); return; }
          // Browsing activities must leave the existing review available.
          if (canReview) setSearchParams({ step: "activities" });
          else await action('edit_places');
        }
        else navigate(`/plans/${encode(planId)}/${step === 'review' ? 'review' : 'sheet'}`);
      }}>{index < current ? '✓ ' : ''}{label}</button>;
    })}</nav>
    {error && <p className="guided-error" role="alert">{error}</p>}
    {Object.entries(state.lodging_search_errors || {}).map(([area, reason]) => <p key={area} className="guided-note" role="status">{area}住宿查询失败：{reason}</p>)}
    {notice && <p role="status">{notice}{notice.includes('列表') && <button onClick={() => refreshAfterCommit(refreshPlans, setNotice)}>刷新列表</button>}</p>}
    <BusyStatus task={task} />
    {uncertain && <div className="guided-error"><p>为避免重复执行，请先重新读取已保存状态。若服务仍在执行，请等待完成后再重试。</p><button disabled={busy} onClick={() => { if (!dirty || window.confirm('重新读取会放弃尚未保存的修改，是否继续？')) { setUncertain(false); setRevision(x => x + 1); } }}>重新读取并核实</button></div>}
    {stage === "requirements" && <section className="guided-card"><h2>准备查询交通</h2><p>查询火车、飞机、大巴及自驾方案。</p><button className="primary-button" disabled={busy || !plan.departure_city} onClick={() => action("candidates")}>查询交通候选</button>{!plan.departure_city && <p className="guided-error">当前行程缺少出发城市，请新建表单行程。</p>}</section>}
    {stage === "candidates" && <section className="guided-card"><h2>选择往返交通</h2><p className="guided-note">火车、飞机和大巴班次时间仅用于规划，费用未知且不计入合计。自驾按高德路线估算时间，燃油与路桥费用待补充，仅支持当天到达。</p>
      <fieldset className="guided-fieldset" disabled={busy || uncertain} onChange={() => setDirty(true)}>
      {[["outbound", "去程"], ["return", "返程"]].map(([direction, title]) => <div key={direction} id={`transport-${direction}`}><h3>{title}</h3><label className="guided-transport-kind">交通工具<select aria-label={`${title}交通工具`} value={kinds[direction]} onChange={(e) => changeKind(direction, e.target.value)}>{Object.entries(transportLabels).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}</select></label><div className="guided-options">{transports.filter((item) => item.direction === direction && item.kind === kinds[direction]).map((item) => <CandidateCard key={item.id} item={item} name={direction} selected={selection[direction] === item.id} onSelect={() => setSelection((current) => ({ ...current, [direction]: item.id }))} currency={plan.currency} />)}</div>{!transports.some((item) => item.direction === direction && item.kind === kinds[direction]) && <p className="guided-note">暂无该交通工具方案，请重新查询交通候选。</p>}</div>)}
      {driving && <><p className="guided-note">自有车辆往返联动；市内交通默认驾车。费用按车辆计算。</p><div className="guided-drive-inputs" id="drive-inputs"><label>车辆数<input type="number" min="1" max="100" step="1" value={vehicles} onChange={(e) => setVehicles(e.target.value)} /></label>{[["outbound", "去程出发时间"], ["return", "返程出发时间"]].map(([direction, label]) => <label key={direction}>{label}<input type="time" required value={driveDepartures[direction]} onChange={(e) => setDriveDepartures((current) => ({ ...current, [direction]: e.target.value }))} /></label>)}</div></>}
      <IssueList issues={transportProblems} />
      <div className="guided-actions"><button className="primary-button" disabled={transportProblems.length > 0} onClick={() => action("choose_candidates", { ...selection, ...(driving ? { drive_departures: driveDepartures, vehicles: Number(vehicles) } : {}) })}>确认交通，继续</button><button onClick={() => action("candidates")}>重新查询交通候选</button></div>
      </fieldset>
    </section>}
    {displayStage === "activities" && <GuideGeneration api={api} planId={planId} initialJobId={generationJobId} text={guideText} onText={setGuideText} onAttachmentChange={setHasGuideImage} disabled={busy || uncertain} onRunning={setGenerating} onNeedsInput={reloadGeneratedPlaces} onComplete={() => { allowNavigation(); navigate(`/plans/${encode(planId)}/review`, { replace: true }); }} />}

    {displayStage === "confirmed" && <section className="guided-card"><h2>已确认行程</h2><p>行程单可查看、打印，并登记交通、酒店与门票购买价格。</p><Link className="primary-button" to={`/plans/${encode(planId)}/sheet`}>查看行程单</Link></section>}
  </div>;
}

export function GenerationView({ generation, activities = [] }) {
  if (!generation) return null;
  const display = (entry) => typeof entry === "string" ? entry : [entry.message || entry.description || entry.requirement || entry.type, entry.reason].filter(Boolean).join(" · ") || JSON.stringify(entry);
  const name = (id) => activities.find((item) => item.id === id)?.name || id;
  return <section className="guided-card guided-generation"><h2>攻略规划说明</h2>
    <h3>规划假设</h3>{generation.assumptions?.length ? <ul>{generation.assumptions.map((entry, index) => <li key={index}>{display(entry)}</li>)}</ul> : <p className="guided-note">没有额外假设。</p>}
    <h3>未安排的地点</h3>{generation.unscheduled?.length ? <ul>{generation.unscheduled.map((entry, index) => <li key={index}>{entry.activity_id && <strong>{name(entry.activity_id)}： </strong>}{display(entry)}</li>)}</ul> : <p className="guided-note">没有未安排的地点。</p>}
    <h3>攻略要求满足情况</h3>{generation.constraint_results?.length ? <ul>{generation.constraint_results.map((entry, index) => <li key={index}>{typeof entry === "object" && <strong>{entry.status || (entry.satisfied === true ? "已满足" : entry.satisfied === false ? "未满足" : "待核对")} · </strong>}{display(entry)}</li>)}</ul> : <p className="guided-note">没有单独的攻略约束。</p>}
  </section>;
}

export function ActivityDetailsEditor({ item, onEdit, busy, live = false, draft }) {
  const [expanded, setExpanded] = useState(Boolean(draft?.invalid));
  function submit(event) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const ticket = values.get("ticket_required") ?? (item.ticket_required == null ? "unknown" : item.ticket_required ? "yes" : "no");
    onEdit(item.reference_id, { ...(!live && { time_of_day: String(values.get("time_of_day")) }), duration_minutes: Number(values.get("duration_minutes")),
      ticket_required: ticket === "unknown" ? null : ticket === "yes",
      estimated_cost: Number(values.get("estimated_cost")), visit_tips: String(values.get("visit_tips") || "") });
  }
  return <details className="guided-item-editor" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}><summary>调整停留信息</summary><form data-review-form={live ? item.item_id : undefined} onSubmit={submit} onChange={live ? event => { const form = event.currentTarget; if (form.checkValidity()) submit({ preventDefault() {}, currentTarget: form }); } : undefined}>
    {!live && <label>开始时间<input name="time_of_day" type="time" defaultValue={item.time_of_day} required /></label>}
    <label>停留时长（分钟）<input name="duration_minutes" type="number" min="30" max="600" step="1" defaultValue={draft?.values.duration_minutes ?? (item.duration_minutes || 90)} required /></label>
    {item.category === "attraction" && <label>是否需要门票<select name="ticket_required" defaultValue={draft?.values.ticket_required ?? (item.ticket_required == null ? "unknown" : item.ticket_required ? "yes" : "no")}><option value="unknown">待确认</option><option value="yes">需要</option><option value="no">不需要</option></select></label>}
    <label>预计总费用（元）<input name="estimated_cost" type="number" min="0" step="0.01" defaultValue={draft?.values.estimated_cost ?? (item.estimated_cost || 0)} required /></label>
    <label className="wide">停留注意事项<textarea name="visit_tips" maxLength="500" rows="2" defaultValue={draft?.values.visit_tips ?? (item.visit_tips || "")} placeholder="例如预约、开放时间或交通提醒" /></label>
    {!live && <button disabled={busy}>保存并重新审核</button>}
  </form></details>;
}


function SheetLeg({ route, from, to }) {
  if (!route) return <p className="guided-note sheet-leg">这段路线待核对。</p>;
  if (route.source === "same_place") return <p className="guided-note sheet-leg">同一地点，继续下一项安排。</p>;
  if (route.source === "lodging_pending") return <p className="guided-note sheet-leg">住宿确定后可核对这段路线。</p>;

  const steps = routeInstructions(route);
  const estimated = route.source === "provisional_estimate";
  const distance = route.distance_meters;
  return <div className="sheet-leg">
    <p className="sheet-leg-heading"><strong>{sheetRouteModes[route.travel_mode] || "前往下一站"}</strong>
      {route.station_transfer && <span>车站 / 机场接驳</span>}
      {route.duration_minutes > 0 && <span>约 {route.duration_minutes} 分钟</span>}
      {distance > 0 && !estimated && <span>{distance < 1000 ? `${distance} 米` : `${(distance / 1000).toFixed(1)} 公里`}</span>}
      {estimated && <span className="sheet-leg-estimate">临时估算</span>}
    </p>
    <p className="guided-note">{route.origin || from.arrival_location || from.location} → {route.destination || to.location}</p>
    {steps.length > 0 ? <ol className="sheet-directions">{steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
      : <p className="guided-note">{estimated ? "出发前请核对实时路线。" : "暂无逐步指引，出发前请查导航。"}</p>}
  </div>;
}

function SheetDay({ api, planId, title, day, items, routes, date, active }) {
  const stops = orderedSheetStops(items, routes);
  const stopId = index => `sheet-day-${day}-stop-${index + 1}`;
  function openStation(number) {
    const details = document.getElementById(stopId(number - 1));
    if (details) {
      details.open = true;
      details.querySelector("summary").focus();
    }
  }
  return <section className="guided-day-panel sheet-day" id={`sheet-day-${day}`} data-active={active}>
    <h3>第 {day} 天 <small>{date}</small></h3>
    {!stops.length ? <p className="guided-note">当天暂无行程安排。</p> : <>
      {active && <SheetDayMap api={api} planId={planId} day={day} date={date} title={title} stops={stops} onStation={openStation} />}
      <ol className="sheet-route-timeline" aria-label={`第 ${day} 天行程与交通`}>
        {stops.map(({ item, route }, index) => {
          const next = stops[index + 1]?.item;
          const place = item.arrival_location ? `${item.location} → ${item.arrival_location}` : item.location;
          return <li key={item.item_id} className="sheet-route-stop"><details id={stopId(index)}>
            <summary className="sheet-stop-heading">
              <span className="sheet-stop-label">
              <span className="sheet-stop-number" aria-hidden="true">{index + 1}</span>
              <time>{sheetStopTime(item)}</time>
              <strong>{item.title}{item.hotel_name && ` · ${item.hotel_name}`}</strong>
              </span>
              <PlacePhoto item={item} />
            </summary>
            <div className="sheet-stop-detail">
              {place && <p className="guided-note">{place}</p>}
              {item.arrival_location && !item.station_confirmed && <p className="guided-note">具体上下车地点待核对。</p>}
              {isMovableStop(item) && <p className="guided-item-meta">约 {item.duration_minutes || 90} 分钟 · {categoryLabels[item.category]}{item.category === "attraction" ? ` · 门票${item.ticket_required == null ? "待确认" : item.ticket_required ? "需要" : "无需"}` : item.category === "food" && item.place?.cost_per_person ? ` · 地图人均 ${item.place.cost_per_person} 元` : ` · 费用${item.estimated_cost > 0 ? ` ${item.estimated_cost} 元` : "未知"}`}{item.category === "attraction" && item.ticket_required && ` · 门票费用${item.estimated_cost > 0 ? ` ${item.estimated_cost} 元` : "未知"}`}</p>}
              {item.visit_tips && <p className="guided-item-tips">注意：{item.visit_tips}</p>}
              {item.reason && <p className="guided-item-tips">安排理由：{item.reason}</p>}
              {next && <SheetLeg route={route} from={item} to={next} />}
            </div>
          </details></li>;
        })}
      </ol>
    </>}
  </section>;
}

export function SheetPage({ api }) {
  const { planId } = useParams();
  const [sheet, setSheet] = useState(null);
  const [summaries, setSummaries] = useState({});
  const [selectedDay, setSelectedDay] = useState(null);
  const [prices, setPrices] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [uncertain, setUncertain] = useState(false);
  const purchaseLock = useRef(false);
  const load = () => api(`/api/plans/${encode(planId)}/sheet`).then(({ sheet }) => setSheet(sheet));
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const url = `/api/plans/${encode(planId)}`;
    setSheet(null);
    setSelectedDay(null);
    setPrices({});
    setError("");

    async function prepareRoadbook() {
      try {
        const { sheet } = await api(`${url}/sheet`, { signal });
        if (signal.aborted) return;
        const { summaries } = await api(`${url}/roadbook`, { method: "POST", signal });
        if (signal.aborted) return;
        setSummaries(summaries);
        setSheet(sheet);
      } catch (cause) {
        if (!signal.aborted) setError(`无法展示路书。${cause.status === 502 ? "每日概述生成失败，请重试。" : cause.message}`);
      }
    }
    prepareRoadbook();
    return () => controller.abort();
  }, [api, planId, revision]);
  async function buy(item) {
    const price = prices[item.reference_id] ?? item.actual_total;
    if (purchaseLock.current || uncertain) return;
    if (!validMoney(price)) { setError('实际购买价格须为非负金额，最多两位小数；免费请填写 0。'); return; }
    purchaseLock.current = true; setBusy(true); setError("");
    try {
      await api(`/api/plans/${encode(planId)}/planning/purchase`, { method: "POST", body: JSON.stringify({ reference_id: item.reference_id, actual_total: Number(price) }) });
      try { await load(); } catch { setError('购买金额已保存，但行程单刷新失败，请重新读取。'); }
    } catch (cause) { setError(cause.message); setUncertain(Boolean(cause.uncertain)); }
    finally { purchaseLock.current = false; setBusy(false); }
  }
  if (!sheet) return <div className="guided-page">{error ? <LoadError error={error} onRetry={() => setRevision(x => x + 1)} /> : <BusyStatus task="正在准备路书" />}</div>;
  const activeDay = sheet.days.some(({ day }) => day === selectedDay) ? selectedDay : null;
  return <div className="guided-page guided-sheet"><header className="guided-header"><Link to={`/plans/${encode(planId)}/planning`}>← 返回规划</Link><h1>{sheet.title} · 路书</h1><p className="guided-head-meta"><span>{sheet.dates.join(" 至 ")}</span><span>{sheet.travelers} 人</span></p><button onClick={() => window.print()}>打印 / 保存为 PDF</button></header>
    {error && <LoadError error={error} onRetry={() => setRevision(x => x + 1)} />}
    {uncertain && <button onClick={() => { setUncertain(false); setRevision(x => x + 1); }}>重新读取购买记录并核实</button>}
    <section className="guided-card sheet-itinerary"><h2>{activeDay ? "分天预览" : "旅程总览"}</h2>
      {sheet.days.length > 0 && <nav className="review-day-nav sheet-day-nav" aria-label="选择行程日期">
        <button type="button" aria-pressed={!activeDay} onClick={() => setSelectedDay(null)}>总览</button>
        {sheet.days.map(({ day }) => <button key={day} type="button" aria-pressed={day === activeDay} aria-controls={`sheet-day-${day}`} onClick={() => setSelectedDay(day)}>第 {day} 天</button>)}
      </nav>}
      {!activeDay && <RoadbookOverview api={api} planId={planId} sheet={sheet} summaries={summaries} onDay={setSelectedDay} />}
      <div className="guided-days">{sheet.days.map(({ day, items }) => <SheetDay key={day} api={api} planId={planId} title={sheet.title} day={day} items={items} active={day === activeDay} routes={sheet.routes || []} date={returnDate(sheet.dates[0], day)} />)}</div>
    </section>
    <section className="guided-card"><h2>交通与住宿</h2><p>去程：{transportLabels[sheet.transport.outbound.kind] || sheet.transport.outbound.kind} {sheet.transport.outbound.date} {sheet.transport.outbound.depart_time} 至 {sheet.transport.outbound.arrive_time}</p><p>返程：{transportLabels[sheet.transport.return.kind] || sheet.transport.return.kind} {sheet.transport.return.date} {sheet.transport.return.depart_time} 至 {sheet.transport.return.arrive_time}</p>
      {(sheet.hotels ?? (sheet.hotel ? [sheet.hotel] : [])).map(hotel => <p key={hotel.id}>酒店：{hotel.name} {hotel.location}{hotel.nights > 0 && ` · ${hotel.nights} 晚`}</p>)}
      {!sheet.hotel && !sheet.hotels?.length && <p>一日行程，无住宿</p>}
      <p className="guided-note">未知价格不计入合计；已购项目按记录的实际支付价格计算。</p></section>
    <section className="guided-card"><h2>购买记录与预算</h2><p className="sheet-budget-summary"><span>已付 <data value={sheet.budget.paid_total}>{sheet.budget.paid_total}</data></span><span>已知费用合计 <data value={sheet.budget.estimated_total}>{sheet.budget.estimated_total}</data></span><span>扣除已知费用后 <data value={sheet.budget.remaining}>{sheet.budget.remaining}</data> {sheet.currency}</span></p><div className="guided-options">{sheet.budget.items.map((item) => <div className="guided-option" key={item.reference_id}><span><strong>{item.label}</strong><small>{item.category} · {item.cost_unknown ? "费用未知" : `已知费用 ${item.estimate}`}</small></span>{item.bookable === false ? <small>{item.retained_expense ? "已保留的历史支出（只读）" : "来自地图路线或用户记录"}</small> : <div className="guided-purchase"><input type="number" min="0" step="0.01" aria-label={`${item.label}实际购买价格`} value={prices[item.reference_id] ?? (item.purchased ? item.actual_total : "")} onChange={(e) => setPrices((current) => ({ ...current, [item.reference_id]: e.target.value }))} placeholder="实际总价" /><button disabled={busy || uncertain || !validMoney(prices[item.reference_id] ?? (item.purchased ? item.actual_total : ""))} onClick={() => buy(item)}>{item.purchased ? "更新已购价格" : "标记已购买"}</button></div>}</div>)}</div></section>
  </div>;
}
