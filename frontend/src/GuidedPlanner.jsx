import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import "./guided-planner.css";
import PlacePicker from "./PlacePicker";
import GuideGeneration from "./GuideGeneration";
import { refreshAfterCommit } from "./api";
import { activitySnapshot as buildSnapshot, activityIssues, transportIssues, validMoney, placeTarget, focusIssue, hasCoordinates } from "./planningValidation";
import { BusyStatus, IssueList, LoadError, useUnsavedChanges } from "./planningUi";
import { transportLabels, categoryLabels, isMovableStop } from "./planningLabels";

const encode = (value) => encodeURIComponent(value);
const returnDate = (start, days) => {
  if (!start || !Number.isInteger(Number(days)) || Number(days) < 1) return "选择出发日期和天数后显示";
  const date = new Date(`${start}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + Number(days) - 1);
  return date.toISOString().slice(0, 10);
};

export function NewTripForm({ api, refreshPlans }) {
  const navigate = useNavigate();
  const [form, setForm] = useState({ destination: "", departure_city: "", home_location: "", start_date: "", trip_days: 3, travelers: 1, budget: "", currency: "CNY", daily_food_per_person: 100, daily_local_transit_per_person: 30, interests: "", constraints: "", pace: "moderate" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const createLock = useRef(false);
  const [uncertain, setUncertain] = useState(false);
  const field = (name, value) => setForm((current) => ({ ...current, [name]: value }));
  async function submit(event) {
    event.preventDefault(); if (createLock.current || uncertain) return; createLock.current = true; setBusy(true); setError("");
    try {
      const payload = { ...form, trip_days: Number(form.trip_days), travelers: Number(form.travelers), budget: Number(form.budget), daily_food_per_person: Number(form.daily_food_per_person), daily_local_transit_per_person: Number(form.daily_local_transit_per_person), interests: form.interests.split(/[,，、]/).map((x) => x.trim()).filter(Boolean), constraints: form.constraints.split(/[,，、]/).map((x) => x.trim()).filter(Boolean) };
      const created = await api("/api/plans/drafts", { method: "POST", body: JSON.stringify(payload) });
      void refreshAfterCommit(refreshPlans);
      navigate(`/plans/${encode(created.plan.plan_id)}/planning`);
    } catch (cause) { setError(cause.message); setUncertain(Boolean(cause.uncertain)); }
    finally { createLock.current = false; setBusy(false); }
  }
  return <div className="guided-page"><header className="guided-header"><Link to="/">← 返回</Link><h1>创建旅行计划</h1><p>先选择往返交通，再核对攻略、分类地点与住宿。</p></header>
    <form className="guided-card guided-form" onSubmit={submit}>
      <label>目的地<input required value={form.destination} onChange={(e) => field("destination", e.target.value)} placeholder="例如：杭州" /></label>
      <label>出发城市<input required value={form.departure_city} onChange={(e) => field("departure_city", e.target.value)} placeholder="例如：上海" /></label>
      <label>家或实际出发地点<input value={form.home_location} onChange={(e) => field("home_location", e.target.value)} placeholder="不填则使用出发城市" /></label>
      <label>出发日期<input required type="date" value={form.start_date} onChange={(e) => field("start_date", e.target.value)} /></label>
      <label>旅行天数<input required type="number" min="1" max="365" step="1" value={form.trip_days} onChange={(e) => field("trip_days", e.target.value)} /></label>
      <label>返回日期<input readOnly value={returnDate(form.start_date, form.trip_days)} /></label>
      <label>人数<input required type="number" min="1" step="1" value={form.travelers} onChange={(e) => field("travelers", e.target.value)} /></label>
      <label>总预算<input required type="number" min="0" step="0.01" value={form.budget} onChange={(e) => field("budget", e.target.value)} /></label>
      <label>币种<input value="CNY 人民币（模拟价格）" readOnly /></label>
      <label>每人每天餐饮预算<input required type="number" min="0" step="0.01" value={form.daily_food_per_person} onChange={(e) => field("daily_food_per_person", e.target.value)} /></label>
      <label>每人每天市内交通预算<input required type="number" min="0" step="0.01" value={form.daily_local_transit_per_person} onChange={(e) => field("daily_local_transit_per_person", e.target.value)} /></label>
      <label>旅行节奏<select value={form.pace} onChange={(e) => field("pace", e.target.value)}><option value="relaxed">轻松</option><option value="moderate">适中</option><option value="active">充实</option></select></label>
      <label>兴趣（用逗号分隔）<input value={form.interests} onChange={(e) => field("interests", e.target.value)} placeholder="博物馆，美食" /></label>
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
    <span className="guided-price">{item.cost_unknown || item.estimated_total == null ? "费用待补充" : `${item.estimated_total} ${currency}`}<small>{item.kind === "drive" ? "按车辆估算 · 燃油、路桥费待核对" : "模拟班次与价格"}</small></span>
  </label>;
}

export function PlanningPage({ api, refreshPlans }) {
  const { planId } = useParams();
  const navigate = useNavigate();
  const [plan, setPlan] = useState(null);
  const [selection, setSelection] = useState({ outbound: "", return: "" });
  const [kinds, setKinds] = useState({ outbound: "train", return: "train" });
  const [driveDepartures, setDriveDepartures] = useState({ outbound: "07:00", return: "17:00" });
  const [vehicles, setVehicles] = useState(1);
  const [choices, setChoices] = useState({});
  const [categories, setCategories] = useState({});
  const [details, setDetails] = useState({});
  const [selectedHotel, setSelectedHotel] = useState("");
  const [guideText, setGuideText] = useState("");
  const [newPlace, setNewPlace] = useState("");
  const [newLocation, setNewLocation] = useState("");
  const [newCategory, setNewCategory] = useState("attraction");
  const [busy, setBusy] = useState(false);
  const [jobRunning, setJobRunning] = useState(false);
  const [error, setError] = useState("");
  const requestBusy = useRef(false);
  const [dirty, setDirty] = useState(false);
  const [notice, setNotice] = useState("");
  const [task, setTask] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [revision, setRevision] = useState(0);
  const [showExcluded, setShowExcluded] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const allowNavigation = useUnsavedChanges(dirty || busy || Boolean(newPlace.trim() || newLocation.trim()));
  function hydrate(nextPlan) {
    const state = nextPlan.planning || {};
    setPlan(nextPlan); setGuideText(state.draft_text ?? state.guide_text ?? "");
    setChoices(Object.fromEntries((state.activities || []).map(item => [item.id, item.choice || "optional"])));
    setCategories(Object.fromEntries((state.activities || []).map(item => [item.id, item.category || "attraction"])));
    setDetails(Object.fromEntries((state.activities || []).map(item => [item.id, { duration_minutes: item.duration_minutes ?? 90, visit_tips: item.visit_tips || "" }])));
    setSelectedHotel(state.selected_hotel || ""); setDirty(false);
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
  async function action(name, payload = {}, file = null) {
    if (requestBusy.current || uncertain) return null;
    if (name === "candidates" && plan.planning?.stage === "candidates" && dirty && !window.confirm("重新查询会重置尚未确认的交通选择，是否继续？")) return null;
    requestBusy.current = true; setBusy(true); setError("");
    const labels = { guide: "正在识别攻略图片", activities: "正在识别地点与要求", draft: "正在生成并检查路线", candidates: "正在查询交通候选", resolve_place: "正在确认地点", choose_activities: "正在保存地点选择", choose_candidates: "正在保存交通并查询住宿", add_activity: "正在添加地点" };
    setTask(labels[name] || "正在保存");
    try {
      if (plan.planning?.stage === 'activities') payload = { ...activitySnapshot(), text: guideText, ...payload };
      if (file) {
        const dataUrl = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("图片读取失败，请重试。")); reader.readAsDataURL(file); });
        payload = { ...payload, image_base64: String(dataUrl).split(",")[1] };
      }
      const result = await api(`/api/plans/${encode(planId)}/planning/${name}`, { method: "POST", body: JSON.stringify(payload) });
      if (!mounted.current) return result;
      hydrate(result.plan);
      if (name === "guide" || name === "activities") setGuideText(result.plan.planning?.guide_text || "");
      setNotice("修改已保存");
      void refreshAfterCommit(refreshPlans, setNotice);
      return result;
    } catch (cause) { if (mounted.current) { setError(cause.message); setUncertain(Boolean(cause.uncertain)); } return null; }
    finally { requestBusy.current = false; if (mounted.current) { setBusy(false); setTask(""); } }
  }
  const edits = { choices, categories, details, selected_hotel: selectedHotel, text: guideText };
  const activitySnapshot = () => buildSnapshot(plan, edits);
  async function generateFromSavedPlaces() {
    if (activityIssues(plan, edits).some(issue => issue.blocking)) return;
    if (await action("draft", { ...activitySnapshot(), text: plan.planning?.guide_text || "" })) {
      allowNavigation(); navigate(`/plans/${encode(planId)}/review`);
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
  const activities = state.activities || [];
  const stage = state.stage || "requirements";
  const driving = kinds.outbound === "drive";
  const transportProblems = transportIssues(selection, driving, vehicles, driveDepartures);
  const problems = activityIssues(plan, edits);
  const blockers = problems.filter(x => x.blocking);
  const detailProblems = blockers.filter(x => x.code === 'duration' || x.code === 'tips' || x.code === 'guide_length');
  const validDetails = !detailProblems.length;
  const needsHotel = plan.end_date !== plan.start_date;
  return <div className="guided-page" aria-busy={busy}><header className="guided-header"><Link to={`/plans/${encode(planId)}`}>← 行程详情</Link><h1>{plan.destination} · 规划向导</h1><p>{plan.start_date} 至 {plan.end_date} · {plan.travelers} 人 · 预算 {plan.budget} {plan.currency}</p></header>
    <nav className="guided-steps" aria-label="规划步骤">{[['candidates', '1 选交通'], ['activities', '2 选活动与攻略'], ['review', '3 审核路线'], ['confirmed', '4 行程单']].map(([step, label], index) => {
      const current = ['requirements', 'candidates'].includes(stage) ? 0 : ['activities', 'review', 'confirmed'].indexOf(stage) + 1;
      const enabled = index === 0 || (index === 1 && Boolean(state.selected_transport?.outbound)) || (index === 2 && ['review', 'confirmed'].includes(stage)) || (index === 3 && stage === 'confirmed');
      return <button key={step} type="button" className={index === current ? 'active' : index < current ? 'complete' : ''} aria-current={index === current ? 'step' : undefined} disabled={!enabled || busy || uncertain} onClick={async () => {
        if (step === stage) return;
        if (step === 'candidates') { if (!validDetails && stage === 'activities') { setError('请先修正地点详情再保存并返回交通选择。'); return; } await action('candidates'); }
        else if (step === 'activities') {
          if (stage === 'candidates' && dirty) { setError('交通选择尚未确认，请先点击“确认交通，继续”。'); return; }
          await action('edit_places');
        }
        else { allowNavigation(); navigate(`/plans/${encode(planId)}/${step === 'review' ? 'review' : 'sheet'}`); }
      }}>{index < current ? '✓ ' : ''}{label}</button>;
    })}</nav>
    {error && <p className="guided-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}{notice.includes('列表') && <button onClick={() => refreshAfterCommit(refreshPlans, setNotice)}>刷新列表</button>}</p>}
    <BusyStatus task={task} />
    {uncertain && <div className="guided-error"><p>为避免重复执行，请先重新读取已保存状态。若服务仍在执行，请等待完成后再重试。</p><button disabled={busy} onClick={() => { if (!dirty || window.confirm('重新读取会放弃尚未保存的修改，是否继续？')) { setUncertain(false); setRevision(x => x + 1); } }}>重新读取并核实</button></div>}
    {stage === "requirements" && <section className="guided-card"><h2>准备查询交通</h2><p>查询火车、飞机、大巴及自驾方案。</p><button className="primary-button" disabled={busy || !plan.departure_city} onClick={() => action("candidates")}>查询交通候选</button>{!plan.departure_city && <p className="guided-error">当前行程缺少出发城市，请新建表单行程。</p>}</section>}
    {stage === "candidates" && <section className="guided-card"><h2>选择往返交通</h2><p className="guided-note">火车、飞机、大巴班次、时间和价格均为模拟，仅用于规划。自驾按高德路线估算，燃油与路桥费用待补充，仅支持当天到达。</p>
      <fieldset className="guided-fieldset" disabled={busy || uncertain} onChange={() => setDirty(true)}>
      {[["outbound", "去程"], ["return", "返程"]].map(([direction, title]) => <div key={direction} id={`transport-${direction}`}><h3>{title}</h3><label className="guided-transport-kind">交通工具<select aria-label={`${title}交通工具`} value={kinds[direction]} onChange={(e) => changeKind(direction, e.target.value)}>{Object.entries(transportLabels).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}</select></label><div className="guided-options">{transports.filter((item) => item.direction === direction && item.kind === kinds[direction]).map((item) => <CandidateCard key={item.id} item={item} name={direction} selected={selection[direction] === item.id} onSelect={() => setSelection((current) => ({ ...current, [direction]: item.id }))} currency={plan.currency} />)}</div>{!transports.some((item) => item.direction === direction && item.kind === kinds[direction]) && <p className="guided-note">暂无该交通工具方案，请重新查询交通候选。</p>}</div>)}
      {driving && <><p className="guided-note">自有车辆往返联动；市内交通默认驾车。费用按车辆计算。</p><div className="guided-drive-inputs" id="drive-inputs"><label>车辆数<input type="number" min="1" max="100" step="1" value={vehicles} onChange={(e) => setVehicles(e.target.value)} /></label>{[["outbound", "去程出发时间"], ["return", "返程出发时间"]].map(([direction, label]) => <label key={direction}>{label}<input type="time" required value={driveDepartures[direction]} onChange={(e) => setDriveDepartures((current) => ({ ...current, [direction]: e.target.value }))} /></label>)}</div></>}
      <IssueList issues={transportProblems} />
      <div className="guided-actions"><button className="primary-button" disabled={transportProblems.length > 0} onClick={() => action("choose_candidates", { ...selection, ...(driving ? { drive_departures: driveDepartures, vehicles: Number(vehicles) } : {}) })}>确认交通，继续</button><button onClick={() => action("candidates")}>重新查询交通候选</button></div>
      </fieldset>
    </section>}
    {["activities", "review", "confirmed"].includes(stage) && <GuideGeneration api={api} planId={planId} text={guideText} onText={value => { setGuideText(value); setDirty(true); }} snapshot={activitySnapshot()} disabled={busy || uncertain || blockers.length > 0} manualAvailable={activities.some(item => item.category !== "hotel" && choices[item.id] !== "exclude")} onManual={generateFromSavedPlaces} onRunning={setJobRunning} onComplete={() => { allowNavigation(); navigate(`/plans/${encode(planId)}/review`); }} />}
    {stage === "activities" && <section className="guided-card activity-workspace"><div className="activity-intro"><div><h2>把想去的地方，整理成旅程</h2><p className="guided-note">提交攻略即可生成每日草稿，也可以展开高级编辑调整。</p></div><span className="activity-total">{activities.length} 个地点</span></div>
      <div className="guided-save-status" role="status">{busy ? '保存中…' : dirty ? '有未保存的修改' : '已保存'}{newPlace.trim() && ' · 有尚未添加的地点'}{guideText !== (state.guide_text || '') && ' · 攻略文字已修改，下次识别或生成时会重新解析'}</div>
      <details><summary>调整地点与住宿</summary>
      <fieldset className="guided-fieldset" disabled={busy || uncertain || jobRunning} onChange={event => { if (event.target.type !== 'file' && event.target.type !== 'checkbox' && !event.target.closest('.place-picker,.guided-add')) setDirty(true); }}>
      <div className="guided-actions guided-primary-actions"><span>{blockers.length ? `请先处理 ${blockers.length} 项地点问题` : "调整后可在上方生成每日行程"}</span><button disabled={!validDetails} onClick={() => action("choose_activities", { ...activitySnapshot(), text: guideText })}>保存地点调整</button></div>

      <IssueList issues={problems} />
      <div className="guided-actions"><label><input type="checkbox" checked={showExcluded} onChange={e => { setShowExcluded(e.target.checked); e.stopPropagation(); }} />显示已排除地点</label></div>
      {Object.entries(categoryLabels).sort(([a], [b]) => a === "hotel" ? -1 : b === "hotel" ? 1 : 0).map(([category, label]) => <details className="guided-category activity-category" id={`category-${category}`} key={category} open={category === "hotel" || undefined}><summary>{category === "hotel" ? "选择住宿" : label} <small>共 {activities.filter(x => (categories[x.id] || x.category) === category).length} 个 · 必去 {activities.filter(x => (categories[x.id] || x.category) === category && choices[x.id] === "must").length} 个 · 待定位 {activities.filter(x => (categories[x.id] || x.category) === category && choices[x.id] !== "exclude" && !hasCoordinates(x.place)).length} 个</small></summary>{category === "hotel" && <p className="guided-note">本次行程选择一家住宿；酒店估价为模拟价格，已购金额优先计入预算。{!needsHotel && "一日游无需选择住宿。"}</p>}
        <div className="guided-options">{activities.filter((item) => (categories[item.id] || item.category || "attraction") === category && (showExcluded || choices[item.id] !== "exclude")).map((item) => {
          const hotel = (state.hotel_candidates || []).find((candidate) => candidate.id === item.id);
          return <div className="guided-place-option" id={placeTarget(item.id)} tabIndex={-1} key={item.id}><div className="guided-option"><span><strong>{item.name}</strong><small>{item.location} · {item.source === "simulated_suggestion" ? "主题建议（需补充具体地点）" : item.source}{item.note ? ` · ${item.note}` : ""}</small>{category === "hotel" && <small>住宿估价：{hotel?.estimated_total ?? item.estimated_total ?? "待补充"} {plan.currency}{hotel?.nights != null && ` · ${hotel.nights} 晚`}（模拟）</small>}</span>
          <select aria-label={`${item.name}的分类`} value={category} onChange={(e) => { setCategories((current) => ({ ...current, [item.id]: e.target.value })); if (selectedHotel === item.id && e.target.value !== "hotel") setSelectedHotel(""); }}>{Object.entries(categoryLabels).map(([value, title]) => <option key={value} value={value}>{title}</option>)}</select>
          {category === "hotel" ? <label className="guided-hotel-choice"><input type="radio" name="selected-hotel" disabled={!needsHotel} checked={selectedHotel === item.id} onChange={() => { setSelectedHotel(item.id); setChoices(current => ({ ...current, [item.id]: "optional" })); }} />作为住宿</label> : <select aria-label={`${item.name}的选择`} value={choices[item.id] || item.choice || "optional"} onChange={(e) => setChoices((current) => ({ ...current, [item.id]: e.target.value }))}><option value="must">必去</option><option value="optional">可选</option><option value="exclude">不去</option></select>}</div>
          <details className="guided-item-editor guided-place-details"><summary>{category === "hotel" ? "住宿备注" : "停留时长与备注"}</summary><div className="guided-place-detail-fields">{category !== "hotel" && <label>停留时长（分钟）<input type="number" aria-invalid={problems.some(x => x.activityId === item.id && x.code === "duration")} data-field="duration_minutes" min="30" max="600" step="1" value={details[item.id]?.duration_minutes ?? item.duration_minutes ?? 90} onChange={(e) => setDetails((current) => ({ ...current, [item.id]: { ...current[item.id], duration_minutes: Number(e.target.value) } }))} /></label>}<label>备注<textarea data-field="visit_tips" rows="2" maxLength="500" value={details[item.id]?.visit_tips ?? item.visit_tips ?? ""} onChange={(e) => setDetails((current) => ({ ...current, [item.id]: { ...current[item.id], visit_tips: e.target.value } }))} /></label></div></details>
          <PlacePicker item={item} planId={planId} api={api} busy={busy || uncertain || !validDetails} onResolve={(poiId) => action("resolve_place", { activity_id: item.id, poi_id: poiId })} /></div>;
        })}</div>{!activities.some((item) => (categories[item.id] || item.category || "attraction") === category) && <p className="guided-note">暂无{label}，可以识别攻略或在下方添加。</p>}{category === "hotel" && selectedHotel && <button type="button" onClick={() => { setSelectedHotel(""); setDirty(true); }}>取消住宿选择</button>}</details>)}
      <details className="activity-manual"><summary>＋ 手动添加地点</summary><div className="guided-add"><input aria-label="新增地点名称" value={newPlace} onChange={(e) => setNewPlace(e.target.value)} placeholder="手动添加地点名称" /><input aria-label="新增地点地址" value={newLocation} onChange={(e) => setNewLocation(e.target.value)} placeholder="地址（可选）" /><select aria-label="新增地点分类" value={newCategory} onChange={(e) => setNewCategory(e.target.value)}>{Object.entries(categoryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button disabled={!newPlace.trim() || !validDetails} onClick={async () => { if (await action("add_activity", { name: newPlace, location: newLocation, category: newCategory })) { setNewPlace(""); setNewLocation(""); } }}>添加地点</button></div></details>
      
      

      </fieldset></details>
    </section>}
    {stage === "review" && <section className="guided-card"><h2>地点选择已完成</h2><p>路线地图、每日行程和预算检查在独立审核页中查看。</p><div className="guided-actions"><Link className="primary-button" to={`/plans/${encode(planId)}/review`}>打开路线与预算审核</Link><button disabled={busy} onClick={() => action("edit_places")}>修改地点选择</button></div></section>}
    {stage === "confirmed" && <section className="guided-card"><h2>已确认行程</h2><p>行程单可查看、打印，并登记交通、酒店与门票购买价格。</p><Link className="primary-button" to={`/plans/${encode(planId)}/sheet`}>查看行程单</Link></section>}
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

export function AuditView({ audit, plan, onIssue, busy = false }) {
  const issues = audit?.issues || [];
  const total = audit?.metrics?.tracked_cost_range?.max;
  return <div className="guided-audit"><p>预算 {plan.budget} {plan.currency}{total != null && ` · 当前预计 ${total} · 余额 ${Math.round((plan.budget - total) * 100) / 100}`} · {issues.some((item) => item.type === "budget_overrun") ? "已超预算" : "请核对估算和实际价格"}</p>
    {issues.length ? <ul>{issues.map((item, index) => <li className={`severity-${item.severity}`} key={`${item.type}-${index}`}><strong>{item.severity === "high" ? "必须处理" : "提示"}</strong> {item.message}{onIssue && <button type="button" disabled={busy} onClick={() => onIssue(item)}>去处理</button>}</li>)}</ul> : <p>未发现行程问题。</p>}
  </div>;
}

export function ActivityDetailsEditor({ item, onEdit, busy }) {
  function submit(event) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const ticket = values.get("ticket_required") ?? (item.ticket_required == null ? "unknown" : item.ticket_required ? "yes" : "no");
    onEdit(item.reference_id, { time_of_day: String(values.get("time_of_day")), duration_minutes: Number(values.get("duration_minutes")),
      ticket_required: ticket === "unknown" ? null : ticket === "yes",
      estimated_cost: Number(values.get("estimated_cost")), visit_tips: String(values.get("visit_tips") || "") });
  }
  return <details className="guided-item-editor"><summary>调整停留信息</summary><form onSubmit={submit}>
    <label>开始时间<input name="time_of_day" type="time" defaultValue={item.time_of_day} required /></label>
    <label>停留时长（分钟）<input name="duration_minutes" type="number" min="30" max="600" step="1" defaultValue={item.duration_minutes || 90} required /></label>
    {item.category === "attraction" && <label>是否需要门票<select name="ticket_required" defaultValue={item.ticket_required == null ? "unknown" : item.ticket_required ? "yes" : "no"}><option value="unknown">待确认</option><option value="yes">需要</option><option value="no">不需要</option></select></label>}
    <label>预计总费用（元）<input name="estimated_cost" type="number" min="0" step="0.01" defaultValue={item.estimated_cost || 0} required /></label>
    <label className="wide">停留注意事项<textarea name="visit_tips" maxLength="500" rows="2" defaultValue={item.visit_tips || ""} placeholder="例如预约、开放时间或交通提醒" /></label>
    <button disabled={busy}>保存并重新审核</button>
  </form></details>;
}

function DayList({ items, routes, onMove, onEdit, busy = false }) {
  const [dragged, setDragged] = useState(null);
  const [dropTarget, setDropTarget] = useState(null);
  const days = [...new Set(items.map((item) => item.day))].sort((a, b) => a - b);
  function drop(event, day, beforeId) {
    event.preventDefault();
    const itemId = event.dataTransfer.getData("text/plain") || dragged;
    setDragged(null); setDropTarget(null);
    if (itemId && itemId !== beforeId && !busy) onMove?.(itemId, day, beforeId);
  }
  function slot(day, beforeId = null) {
    if (!onMove) return null;
    const key = `${day}:${beforeId || "end"}`;
    return <div key={`slot-${key}`} className={`guided-drop-slot ${dragged ? "is-available" : ""} ${dropTarget === key ? "is-target" : ""}`}
      onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "move"; setDropTarget(key); }}
      onDragLeave={() => setDropTarget((current) => current === key ? null : current)}
      onDrop={(event) => drop(event, day, beforeId)}>{dragged && <span>放在这里</span>}</div>;
  }
  return <div className="guided-days">{days.map((day) => {
    const ordered = items.filter((item) => item.day === day).sort((a, b) => a.time_of_day.localeCompare(b.time_of_day));
    const returnTransport = day === days[days.length - 1] ? ordered.filter((item) => item.category === "intercity_transport").at(-1) : null;
    return <section className="guided-day-panel" key={day}><h3>第 {day} 天</h3><div className="guided-day-stack">
      {ordered.map((item) => isMovableStop(item) ? <React.Fragment key={item.item_id}>
        {slot(day, item.item_id)}
        <article className={`guided-activity-card ${dragged === item.item_id ? "is-dragging" : ""}`}
          draggable={Boolean(onMove) && !busy}
          onDragStart={onMove ? (event) => {
            if (event.target.closest("form, input, select, textarea, summary, button")) { event.preventDefault(); return; }
            event.dataTransfer.setData("text/plain", item.item_id);
            event.dataTransfer.effectAllowed = "move";
            setDragged(item.item_id);
          } : undefined}
          onDragEnd={() => { setDragged(null); setDropTarget(null); }}>
          <div className="guided-card-top"><time>{item.time_of_day}</time><span className="guided-card-grip" aria-hidden="true">⠿</span><div><strong>{item.title}</strong><small>{item.location}</small></div>{onMove && <span className="guided-card-hint">拖动卡片</span>}</div>
          <p className="guided-item-meta">约 {item.duration_minutes || 90} 分钟 · {categoryLabels[item.category] || "停留地点"}{item.category === "attraction" ? ` · 门票${item.ticket_required == null ? "待确认" : item.ticket_required ? "需要" : "无需"}` : ` · 预计费用 ${item.estimated_cost ?? "待补充"} 元`}{item.category === "attraction" && item.ticket_required && ` · 预计 ${item.estimated_cost || 0} 元`}</p>
          {item.visit_tips && <p className="guided-item-tips">注意：{item.visit_tips}</p>}
          {item.reason && <p className="guided-item-tips">安排理由：{item.reason}</p>}
          {onEdit && <ActivityDetailsEditor key={`${item.reference_id}-${item.duration_minutes}-${item.ticket_required}-${item.estimated_cost}-${item.visit_tips}`} item={item} onEdit={onEdit} busy={busy} />}
        </article>
      </React.Fragment> : <React.Fragment key={item.item_id}>{(item.item_id === returnTransport?.item_id || (!returnTransport && item.anchor_role === "end")) && slot(day)}<div className="guided-fixed-stop"><time>{item.time_of_day}</time><div><strong>{item.title}</strong><small>{item.location}</small></div></div></React.Fragment>)}
    </div></section>;
  })}{routes.some((route) => route.source === "provisional_estimate") && <p className="guided-note">地点间通勤暂按估算安排，出行前请核对实时路线。</p>}</div>;
}

export function SheetPage({ api }) {
  const { planId } = useParams();
  const [sheet, setSheet] = useState(null);
  const [prices, setPrices] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [uncertain, setUncertain] = useState(false);
  const purchaseLock = useRef(false);
  const load = () => api(`/api/plans/${encode(planId)}/sheet`).then(({ sheet }) => setSheet(sheet));
  useEffect(() => { let active = true; setSheet(null); setPrices({}); setError(""); api(`/api/plans/${encode(planId)}/sheet`).then(({ sheet }) => { if (active) setSheet(sheet); }).catch(cause => { if (active) setError(cause.message); }); return () => { active = false; }; }, [api, planId, revision]);
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
  if (!sheet) return <div className="guided-page">{error ? <LoadError error={error} onRetry={() => setRevision(x => x + 1)} /> : <p>正在生成行程单…</p>}</div>;
  return <div className="guided-page guided-sheet"><header className="guided-header"><Link to={`/plans/${encode(planId)}/planning`}>← 返回规划</Link><h1>{sheet.title} · 行程单</h1><p>{sheet.dates.join(" 至 ")} · {sheet.travelers} 人</p><button onClick={() => window.print()}>打印 / 保存为 PDF</button></header>
    {error && <LoadError error={error} onRetry={() => setRevision(x => x + 1)} />}
    {uncertain && <button onClick={() => { setUncertain(false); setRevision(x => x + 1); }}>重新读取购买记录并核实</button>}<section className="guided-card"><h2>交通与住宿</h2><p>去程：{transportLabels[sheet.transport.outbound.kind] || sheet.transport.outbound.kind} {sheet.transport.outbound.date} {sheet.transport.outbound.depart_time}–{sheet.transport.outbound.arrive_time}</p><p>返程：{transportLabels[sheet.transport.return.kind] || sheet.transport.return.kind} {sheet.transport.return.date} {sheet.transport.return.depart_time}–{sheet.transport.return.arrive_time}</p><p>酒店：{sheet.hotel?.name || "一日行程，无住宿"} {sheet.hotel?.location || ""}</p><p className="guided-note">候选价格是模拟价，已购项目以记录的实际支付价格计算。</p></section>
    <section className="guided-card"><h2>每日安排</h2><DayList items={sheet.days.flatMap((day) => day.items)} routes={sheet.routes} /></section>
    <section className="guided-card"><h2>购买记录与预算</h2><p>已付 {sheet.budget.paid_total} · 当前预计合计 {sheet.budget.estimated_total} · 剩余 {sheet.budget.remaining} {sheet.currency}</p><div className="guided-options">{sheet.budget.items.map((item) => <div className="guided-option" key={item.reference_id}><span><strong>{item.label}</strong><small>{item.category} · 估算 {item.estimate}（模拟或待确认）</small></span>{item.bookable === false ? <small>{item.retained_expense ? "已保留的历史支出（只读）" : "按每日预算估算"}</small> : <div className="guided-purchase"><input type="number" min="0" step="0.01" aria-label={`${item.label}实际购买价格`} value={prices[item.reference_id] ?? (item.purchased ? item.actual_total : "")} onChange={(e) => setPrices((current) => ({ ...current, [item.reference_id]: e.target.value }))} placeholder="实际总价" /><button disabled={busy || uncertain || !validMoney(prices[item.reference_id] ?? (item.purchased ? item.actual_total : ""))} onClick={() => buy(item)}>{item.purchased ? "更新已购价格" : "标记已购买"}</button></div>}</div>)}</div></section>
    <section className="guided-card"><h2>行程检查</h2><AuditView audit={sheet.audit} plan={{ budget: sheet.budget_limit, currency: sheet.currency }} /></section>
  </div>;
}
