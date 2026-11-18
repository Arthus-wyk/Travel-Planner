import { applyReviewEdit, canSetDeparture, lodgingCompletionOffer, overnightMismatch, previewSchedule, rebaseReviewAdditions } from './reviewDraft';
import ReviewPlaceDialog from "./ReviewPlaceDialog";
import ReviewAssistant from "./ReviewAssistant";
import RouteMap from "./RouteMap";
import PlacePhoto from "./PlacePhoto";
import React, { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ActivityDetailsEditor } from "./GuidedPlanner";
import { Clock3, GripVertical, Plus, Wallet } from "lucide-react";
import "./guided-planner.css";
import { apiUrl } from "./apiUrl";
import { categoryLabels, isMovableStop, transportLabels } from "./planningLabels";

import { refreshAfterCommit } from "./api";
import { auditDestination } from "./auditActions";
import { validMoney, focusIssue } from "./planningValidation";
import { BusyStatus, LoadError, useUnsavedChanges } from "./planningUi";

const encode = encodeURIComponent;
const modes = { WALK: "步行", TRANSIT: "公交", DRIVE: "驾车" };

function LodgingCompletionDialog({ hotel, busy, error, onClose, onConfirm }) {
  const dialog = useRef(null);
  useEffect(() => { dialog.current.showModal(); }, []);
  return <dialog ref={dialog} className="review-place-dialog" aria-labelledby="lodging-completion-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <h2 id="lodging-completion-title">自动补全住宿</h2>
    <p>目前只在第 {hotel.day} 天 {hotel.time_of_day} 选择了「{hotel.hotel_name || hotel.title}」。是否用这家酒店补全行程单里的住宿起终点？</p>
    <p className="guided-note">确认后会更新其他日期不一致的住宿起终点，并保存修改、重新计算路线和时间。</p>
    {error && <p className="guided-error" role="alert">{error}</p>}
    <BusyStatus task={busy ? "正在补全住宿并重新计算路线" : ""} />
    <div className="guided-actions">
      <button type="button" disabled={busy} onClick={onClose}>暂不补全</button>
      <button type="button" className="primary-button" disabled={busy} onClick={onConfirm}>补全并保存</button>
    </div>
  </dialog>;
}

// ============管理审核页行程与待保存草稿 ============
export default function ReviewPage({ api, refreshPlans }) {
  const { planId } = useParams();
  const navigate = useNavigate();
  const activitiesUrl = `/plans/${encode(planId)}/planning?step=activities`;
  const [plan, setPlan] = useState(null);
  const [audit, setAudit] = useState(null);
  const [edits, setEdits] = useState([]);
  const pendingEdits = useRef([]);
  const queuedSave = useRef(null);
  const failedBatch = useRef(null);
  const savedVersion = useRef(null);
  const savedPlan = useRef(null);
  const draftRevision = useRef(0);
  const [draftMaps, setDraftMaps] = useState(null);
  const [formDrafts, setFormDrafts] = useState({});
  const invalidDraft = Object.values(formDrafts).some(form => form.invalid);
  const [editorVersion, setEditorVersion] = useState(0);
  useUnsavedChanges(edits.length > 0 || invalidDraft);
  const [day, setDay] = useState(1);
  const [map, setMap] = useState(null);
  const [mapError, setMapError] = useState("");
  const [mapBusy, setMapBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const requestBusy = useRef(false);
  const [error, setError] = useState("");
  const [activeIssue, setActiveIssue] = useState(null);
  const [dragged, setDragged] = useState(null);
  const [target, setTarget] = useState(null);
  const [revision, setRevision] = useState(0);
  const [mapRevision, setMapRevision] = useState(0);
  const [notice, setNotice] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [budget, setBudget] = useState("");
  const [focusTarget, setFocusTarget] = useState(null);
  const [placeEditor, setPlaceEditor] = useState(null);
  const [dismissedHotel, setDismissedHotel] = useState(null);
  const [preferredHotelItem, setPreferredHotelItem] = useState(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (focusTarget) { focusIssue(focusTarget.id, focusTarget.field); setFocusTarget(null); } }, [focusTarget, day]);
  useEffect(() => {
    let active = true;
    setError("");
    Promise.all([api(`/api/plans/${encode(planId)}`), api(`/api/plans/${encode(planId)}/planning/audit`)]).then(([data, check]) => {
      if (!active) return;
      if (!["review", "confirmed"].includes(data.plan.planning?.stage)) { navigate(`/plans/${encode(planId)}/planning`, { replace: true }); return; }
      savedVersion.current = data.plan_version; savedPlan.current = data.plan;
      draftRevision.current += 1; setDraftMaps(null);
      pendingEdits.current = [];
      failedBatch.current = null;
      setEdits([]); setFormDrafts({}); setEditorVersion(value => value + 1);
      setDismissedHotel(null); setPreferredHotelItem(null);
      setPlan(data.plan); setBudget(data.plan.budget); setAudit(check.audit); setDay(1); setUncertain(false);
    }).catch((cause) => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [api, planId, navigate, revision]);
  useEffect(() => {
    if (savedVersion.current === null) return;
    let active = true;
    setMap(null); setMapError(""); setMapBusy(true);
    api(`/api/plans/${encode(planId)}/planning/map?day=${day}`).then((data) => { if (active) setMap(data); })
      .catch((cause) => { if (active) setMapError(cause.message); })
      .finally(() => { if (active) setMapBusy(false); });
    return () => { active = false; };
  }, [api, planId, day, mapRevision, editorVersion]);
  // ============编辑页面草稿或调用已有保存入口 ============
  async function action(name, payload = {}, { quiet = false } = {}) {
    if (uncertain) return null;
    setActiveIssue(null);
    if (["move_activity", "update_activity", "route_mode", "transport_stops", "update_budget", "acknowledge_purchase", "day_departure", "review_place"].includes(name)) {
      try {
        const next = applyReviewEdit(plan, name, payload);
        const serialized = JSON.stringify(next);
        if (serialized === JSON.stringify(plan)) return { success: true };
        setPlan(next);
        pendingEdits.current = !requestBusy.current && serialized === JSON.stringify(savedPlan.current)
          ? [] : [...pendingEdits.current, { action: name, payload }];
        setEdits(pendingEdits.current);
        draftRevision.current += 1; setDraftMaps(null);
        setNotice(pendingEdits.current.length ? "有未保存的修改" : ""); setError("");
        return { success: true };
      } catch (cause) { setError(cause.message); return null; }
    }
    if (requestBusy.current) return null;
    if ((edits.length || invalidDraft) && name !== "review_changes") { setError("请先保存所有修改"); return null; }
    requestBusy.current = true;
    if (!quiet) setBusy(true);
    setError("");
    const path = `/api/plans/${encode(planId)}`;
    function post(values) {
      const body = name === "review_changes" ? { ...values, operations: values.operations.map(edit => {
        if (edit.action !== "review_place") return edit;
        const { place, ...details } = edit.payload;
        return { ...edit, payload: details };
      }) } : values;
      return api(`${path}/planning/${name}?include_version=true`, { method: "POST", body: JSON.stringify(body) });
    }
    try {
      let data;
      try {
        data = await post(payload);
      } catch (cause) {
        if (cause.status !== 409 || name !== "review_changes" || !mounted.current ||
            payload.operations.some(edit => edit.action !== "review_place" || edit.payload.replace)) throw cause;
        const latest = await api(path);
        if (!mounted.current) return null;
        const operations = rebaseReviewAdditions(savedPlan.current, latest.plan, payload.operations);
        if (!operations || !latest.plan_version) throw cause;
        if (operations.length) {
          setNotice("已读取最新行程，正在重新保存新增地点…");
          data = await post({ operations, plan_version: latest.plan_version });
        } else {
          const { audit: latestAudit } = await api(`${path}/planning/audit`);
          data = { ...latest, audit: latestAudit };
        }
      }
      if (!mounted.current) return data;
      const remainder = name === "review_changes" ? pendingEdits.current.slice(payload.operations.length) : [];
      failedBatch.current = null;
      applySavedPlan(data, remainder);
      if (!quiet && !remainder.length) setFormDrafts({});
      setNotice(data.optimization_message || '修改已保存');
      void refreshAfterCommit(refreshPlans, setNotice);
      return data;
    } catch (cause) {
      if (mounted.current) {
        setError(cause.message);
        setUncertain(Boolean(cause.uncertain || cause.status === 409));
        if (name === "review_changes") failedBatch.current = payload.operations;
      }
      return null;
    }
    finally { requestBusy.current = false; if (mounted.current) setBusy(false); }
  }

  // ============应用已保存的新版本并重放在途期间的手动编辑 ============
  function applySavedPlan(data, remaining) {
    savedVersion.current = data.plan_version;
    savedPlan.current = data.plan;
    pendingEdits.current = remaining;
    setEdits(remaining);
    draftRevision.current += 1;
    setDraftMaps(remaining.length ? null : (data.maps ?? null));
    setAudit(data.audit || null);
    setMapRevision(value => value + 1);
    let displayed;
    try {
      displayed = remaining.reduce((current, edit) => applyReviewEdit(current, edit.action, edit.payload), data.plan);
    } catch (cause) {
      setUncertain(true);
      setError("行程已更新，部分待保存编辑需要重新核对。");
      displayed = data.plan;
    }
    setPlan(displayed);
    setBudget(displayed.budget);
    if (!remaining.length) setEditorVersion(value => value + 1);
  }

  // ============先保存有效编辑，再发起路线助手请求 ============
  async function assistantTurn(payload, onProgress) {
    if (uncertain || invalidDraft) throw new Error("请先完成当前编辑");
    if (queuedSave.current) await queuedSave.current;
    if (pendingEdits.current.length) {
      onProgress?.("正在同步页面修改…");
      const saved = await saveChanges({ quiet: true });
      if (!saved || pendingEdits.current.length) throw new Error("还有未保存的页面修改，请稍后重试");
    }
    if (requestBusy.current) throw new Error("请先完成当前编辑");
    requestBusy.current = true;
    const sentRevision = draftRevision.current;
    try {
      const result = await api(`/api/plans/${encode(planId)}/planning/assistant?stream=true`, {
        method: "POST",
        onProgress,
        timeoutMs: 75000,
        body: JSON.stringify({ ...payload, draft_revision: sentRevision }),
      });
      if (mounted.current && result.status === "saved") {
        applySavedPlan(result, pendingEdits.current);
        setNotice("行程已更新");
        void refreshAfterCommit(refreshPlans, setNotice);
      } else if (mounted.current && result.status === "save_unknown") {
        setUncertain(true);
      }
      return result;
    } catch (cause) {
      if (mounted.current && payload.action_id && !cause.status) setUncertain(true);
      throw cause;
    } finally {
      requestBusy.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  // ============保存当前队列中的一批操作 ============
  async function saveChanges({ quiet = false } = {}) {
    if (queuedSave.current) return await queuedSave.current;
    if (!pendingEdits.current.length || requestBusy.current || uncertain) return null;
    if (quiet && failedBatch.current === pendingEdits.current) return null;
    if (invalidDraft) {
      if (!quiet) setError("请返回对应日期修正尚未填写完整的调整项");
      return null;
    }
    const saving = action("review_changes", { operations: pendingEdits.current, plan_version: savedVersion.current }, { quiet });
    queuedSave.current = saving;
    try {
      return await saving;
    } finally {
      queuedSave.current = null;
    }
  }

  // ============定时检查并静默保存有效编辑 ============
  const saveOnTimer = useRef(saveChanges);
  saveOnTimer.current = saveChanges;
  useEffect(() => {
    const timer = setInterval(() => { void saveOnTimer.current({ quiet: true }); }, 3000);
    return () => clearInterval(timer);
  }, []);
  async function move(id, targetDay, before = null) {
    setDragged(null); setTarget(null);
    if (formDrafts[id]?.invalid) { setError("请先修正该地点的停留信息"); return; }
    if (!busy && await action("move_activity", { item_id: id, target_day: targetDay, before_item_id: before })) {
      setFormDrafts(current => {
        const next = { ...current };
        delete next[id];
        return next;
      });
    }
  }
  function dropProps(targetDay, before = null) {
    const key = `${targetDay}:${before || "end"}`;
    return {
      onDragOver: (event) => { if (!dragged || busy || uncertain) return; event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = "move"; setTarget(key); },
      onDragLeave: () => setTarget((current) => current === key ? null : current),
      onDrop: (event) => { event.preventDefault(); event.stopPropagation(); if (dragged && !busy && !uncertain && dragged !== before) move(dragged, targetDay, before); },
    };
  }
  if (!plan) return <div className="guided-page">{error ? <LoadError error={error} onRetry={() => setRevision(x => x + 1)} /> : <p role="status">加载路线与预算审核…</p>}</div>;
  const state = plan.planning || {};
  const editable = state.stage === "review";
  const days = Math.round((new Date(`${plan.end_date}T00:00:00Z`) - new Date(`${plan.start_date}T00:00:00Z`)) / 86400000) + 1;
  const lodgingOffer = !placeEditor && !invalidDraft && !uncertain && lodgingCompletionOffer(plan, days, edits.length ? preferredHotelItem : null);
  const editsTimetable = edits.some(edit => !["update_budget", "acknowledge_purchase"].includes(edit.action));
  const items = !draftMaps && (editsTimetable || state.day_departures && !state.optimized_timetable) && plan.itinerary?.length
    ? previewSchedule(plan, day)
    : (plan.itinerary || []).filter((item) => item.day === day);
  const departureForm = `day-departure-${day}`;
  const previousEnd = plan.itinerary?.findLast(item => item.day === day - 1);
  const mismatched = overnightMismatch(previousEnd, items[0]);
  const pending = (state.unscheduled_items || []).filter(item => item.manual_pending);
  const legacy = state.generation?.contract_version !== 3;
  const costs = audit?.budget;
  const guideText = state.guide_days?.find(section => section.day === day)?.text;
  const mapHasDraftChanges = edits.some(edit => ['move_activity', 'route_mode', 'transport_stops', 'review_place'].includes(edit.action));
  const routeFrom = id => {
    const index = items.findIndex(item => item.item_id === id);
    return (plan.routes || []).find(route => route.origin_item_id === id && route.destination_item_id === items[index + 1]?.item_id);
  };
  function selectRoute(route, mode) {
    action("route_mode", { origin_item_id: route.origin_item_id, destination_item_id: route.destination_item_id, travel_mode: mode });
  }

  // Opening the editor is navigation. Saving a new draft is what replaces the route.
  function editPlaces() { navigate(activitiesUrl); }
  const issues = (audit?.issues || []).map((issue, index) => ({ ...auditDestination(issue, plan), message: issue.message, index }));
  const isOnRoute = issue => issue.day >= 1 && issue.day <= days;
  function issueHints(matches) {
    return issues.filter(matches).map(issue => <p className="review-issue-hint" key={issue.index}>{issue.message}</p>);
  }
  function issueAt(id) {
    return activeIssue?.id === id && <p className="review-issue-hint">{activeIssue.message}</p>;
  }
  async function confirmItinerary() {
    setError("");
    if (uncertain) {
      setError("请先重新读取已保存的行程。");
      setFocusTarget({ id: 'review-reload' });
      return;
    }
    const invalid = Object.entries(formDrafts).find(([, form]) => form.invalid);
    if (invalid) {
      const [id, form] = invalid;
      setError("请补全或修正标出的输入后再确认。");
      if (form.day) setDay(form.day);
      const targetId = id === 'budget' ? 'review-budget' : id.startsWith('day-departure-') ? id : `review-${id}`;
      setActiveIssue({ id: targetId, message: "请补全或修正此处输入。" });
      setFocusTarget({ id: targetId, field: form.field });
      return;
    }
    if (edits.length) {
      setError("请先保存所有修改，再确认行程。");
      setFocusTarget({ id: 'review-save' });
      return;
    }
    if (!audit) {
      setError("暂无审核结果，请重新读取行程。");
      setFocusTarget({ id: 'review-reload' });
      return;
    }
    if (await action("confirm")) navigate(`/plans/${encode(planId)}/sheet`);
  }
  function slot(before = null) {
    return editable && <div className={`review-drop-slot ${dragged ? "available" : ""} ${target === `${day}:${before || "end"}` ? "target" : ""}`} {...dropProps(day, before)}>{dragged ? "放在这里" : ""}</div>;
  }
  function editTransportStops(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.checkValidity()) return;
    const values = new FormData(form);
    const departure = values.get('departure_location').trim();
    const arrival = values.get('arrival_location').trim();
    if (departure && arrival) action('transport_stops', { item_id: form.dataset.reviewForm, departure_location: departure, arrival_location: arrival });
  }
  function card(item, isPending = false) {
    const movable = editable && isMovableStop(item);
    const lodging = ["hotel", "hotel_anchor", "hotel_arrival"].includes(item.category);
    const meal = item.category === "food" && state.activities?.find(activity => activity.id === item.reference_id)?.meal_type;
    const category = item.category === "food"
      ? ({ main: "正餐", dessert: "甜点" }[meal] || "餐饮（暂按正餐排程）")
      : categoryLabels[item.category];
    const reason = item.reason || state.generation?.stops?.find((stop) => stop.activity_id === item.reference_id)?.reason;
    const index = map?.points?.findIndex((p) => p.item_id === item.item_id) ?? -1;
    return <article key={item.item_id} id={`review-${item.item_id}`} tabIndex={-1} className={`review-stop ${movable ? "movable" : "fixed"} ${dragged === item.item_id ? "dragging" : ""}`}>
      <div className="review-stop-content">
      <div className="review-stop-heading"><span className="stop-number">{index >= 0 ? index + 1 : movable ? "⠿" : "•"}</span><div><strong>{item.title}{item.hotel_name && ` · ${item.hotel_name}`}</strong><small>{item.location}</small></div><time>{isPending ? "手动暂存" : `${item.day_offset ? `次日+${item.day_offset} ` : ""}${item.time_of_day}`}</time>
        {editable && !isPending && item.anchor_role !== "end" && !item.item_id.endsWith("-return") &&
          <button type="button" className="review-add-stop" disabled={uncertain} aria-label={`在${item.title}后添加行程`} onClick={() => setPlaceEditor({ item, replace: false })}><Plus size={16} aria-hidden="true" /></button>}
        {movable && !busy && !uncertain && <span className="review-drag-handle" draggable title="拖动调整顺序"
          onDragStart={(event) => {
            event.dataTransfer.setData("text/plain", item.item_id);
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setDragImage(event.currentTarget.closest(".review-stop"), 24, 24);
            setDragged(item.item_id);
          }}
          onDragEnd={() => { setDragged(null); setTarget(null); }}><GripVertical size={18} aria-hidden="true" /></span>}</div>
      {mismatched && item.item_id === items[0].item_id && <p className="review-issue-hint" role="status">前一天结束地点（{previousEnd.hotel_name || previousEnd.arrival_location || previousEnd.location}）与今天出发地点不同，请核对衔接安排。</p>}
      {editable && lodging && <button type="button" disabled={uncertain} onClick={() => setPlaceEditor({ item, replace: true })}>{item.lodging_pending ? "选择住宿" : "修改酒店"}</button>}
      {issueAt(`review-${item.item_id}`)}
      {issueHints(issue => (issue.kind === "item" || issue.kind === "pending") && issue.id === item.item_id)}
      <p className="guided-item-meta">{[item.duration_minutes ? `约 ${item.duration_minutes} 分钟` : item.anchor_role && "起终点", category].filter(Boolean).join(" · ")}</p>
      {reason && reason !== "按每日顺序安排" && <p className="guided-item-tips">安排理由：{reason}</p>}
      {item.visit_tips && <p className="review-visit-note">注意事项：{item.visit_tips}</p>}
      {item.category === "attraction" && <p className="guided-note">{item.ticket_required === false ? "原文标注免票" : item.estimated_cost > 0 ? `门票参考费用 ${item.estimated_cost} 元` : "门票费用未知，未计入合计"}</p>}
      {item.category === "food" && <p className="guided-note">{item.place?.cost_per_person ? `高德人均 ${item.place.cost_per_person} 元` : "人均消费未知，未计入合计"}</p>}
      {item.category === "intercity_transport" && <><p className="guided-note">{transportLabels[(state.transport_candidates || []).find((candidate) => candidate.id === item.reference_id)?.kind] || "城际交通"} · 出发：{item.location} → 到达：{item.arrival_location || "待确认"}{!item.station_confirmed && " · 请确认实际车站 / 机场"}</p>{editable && <details className="guided-item-editor"><summary>设置车站 / 机场接驳地点</summary><form key={editorVersion} data-review-form={item.item_id} onChange={editTransportStops} onSubmit={editTransportStops}>
        <label>出发站 / 机场<input name="departure_location" defaultValue={formDrafts[item.item_id]?.values.departure_location ?? (item.station_confirmed ? item.location : "")} required maxLength={200} disabled={uncertain} placeholder="例如：上海虹桥站" /></label>
        <label>到达站 / 机场<input name="arrival_location" defaultValue={formDrafts[item.item_id]?.values.arrival_location ?? (item.station_confirmed ? item.arrival_location : "")} required maxLength={200} disabled={uncertain} placeholder="例如：杭州东站" /></label>
        <button disabled={busy || uncertain}>应用接驳地点</button></form></details>}</>}
      {item.place?.image_cache_key && <details className="review-place-map"><summary>查看地点地图</summary><img src={apiUrl(`/api/maps/images/${item.place.image_cache_key}`)} alt={`${item.title}的位置地图`} loading="lazy" /></details>}
      {movable && <div className="review-card-controls">{isPending ? <button disabled={busy || uncertain} onClick={() => move(item.item_id, day)}>加入第 {day} 天</button> : <>
        <button disabled={busy || uncertain} onClick={() => move(item.item_id, null)}>移至暂存区</button>
        <select aria-label={`移动${item.title}至其他日期`} value="" disabled={busy || uncertain} onChange={(e) => { if (e.target.value) move(item.item_id, Number(e.target.value)); }}><option value="">移动到其他天…</option>{Array.from({ length: days }, (_, n) => n + 1).filter((n) => n !== day).map((n) => <option key={n} value={n}>第 {n} 天</option>)}</select>
        {items.filter((x) => isMovableStop(x)).findIndex((x) => x.item_id === item.item_id) > 0 && <button disabled={busy || uncertain} onClick={() => { const visits = items.filter((x) => isMovableStop(x)); move(item.item_id, day, visits[visits.findIndex((x) => x.item_id === item.item_id) - 1].item_id); }}>上移</button>}
      </>}</div>}
    {movable && !isPending && <ActivityDetailsEditor key={`${item.item_id}-${editorVersion}`} item={item} draft={formDrafts[item.item_id]} busy={busy || uncertain} live onEdit={(id, details) => action("update_activity", { activity_id: id, ...details })} />}
      </div>
      <PlacePhoto item={item} />
    </article>;
  }
  return <div className="guided-page review-page" aria-busy={busy} onChangeCapture={event => {
    const form = event.target.closest('form');
    if (form?.dataset.reviewForm) {
      const values = Object.fromEntries(new FormData(form));
      const invalid = !form.checkValidity() || (form.id === 'review-budget' && !validMoney(values.budget))
        || (Object.hasOwn(values, 'departure_location') && (!values.departure_location.trim() || !values.arrival_location.trim()));
      const field = [...form.elements].find(input => input.validity && !input.validity.valid)?.name || event.target.name;
      setFormDrafts(current => ({ ...current, [form.dataset.reviewForm]: { values, invalid, field, day } }));
    }
  }}><fieldset disabled={busy} className="review-workspace">
    {placeEditor && <ReviewPlaceDialog {...placeEditor} planId={planId} api={api} onClose={() => setPlaceEditor(null)}
      onApply={async payload => {
        if (!await action("review_place", payload)) return;
        if (payload.category === "hotel") {
          setPreferredHotelItem(payload.replace ? payload.item_id : `guided-${placeEditor.item.day}-${payload.activity_id}`);
        }
        setPlaceEditor(null);
      }} />}
    {lodgingOffer && dismissedHotel !== lodgingOffer.reference_id &&
      <LodgingCompletionDialog key={lodgingOffer.reference_id} hotel={lodgingOffer} busy={busy} error={error}
        onClose={() => setDismissedHotel(lodgingOffer.reference_id)}
        onConfirm={async () => {
          const completion = { action: "complete_lodging", payload: { activity_id: lodgingOffer.reference_id } };
          if (await action("review_changes", { operations: [...edits, completion], plan_version: savedVersion.current })) {
            setDismissedHotel(lodgingOffer.reference_id);
          }
        }} />}
    <header className="guided-header"><Link to={`/plans/${encode(planId)}`}>← 行程详情</Link><h1>{plan.destination} · 路线与预算审核</h1><p>{plan.start_date} 至 {plan.end_date} · {plan.travelers} 人</p></header>
    <nav className="guided-steps" aria-label="规划步骤">
      <button type="button" className="complete" disabled={busy || uncertain || !state.selected_transport?.outbound} onClick={editPlaces}>✓ 2 选活动与攻略</button>
      <button type="button" className="active" aria-current="step">3 审核路线</button>
    </nav>
    <p role="status" className="guided-note">每日路线已生成，可调整地点顺序并核对费用。</p>
    <ReviewAssistant key={planId} planId={planId} editable={editable} disabled={busy || uncertain || invalidDraft} onTurn={assistantTurn} />
    {issueHints(issue => !isOnRoute(issue) && issue.kind !== "pending" && (issue.kind !== "purchase" || !editable))}
    {editable && <div className="guided-actions" role="status"><button id="review-save" className="primary-button" disabled={busy || uncertain || !edits.length} onClick={() => saveChanges()}>立即保存</button><span>{edits.length || invalidDraft ? "有效修改会自动保存。" : "所有修改已保存"}</span></div>}
    {error && <p className="guided-error" role="alert">{error}</p>}
    {(state.activities || []).filter(item => item.choice !== "exclude" && item.place_error).map(item => <p key={item.id} className="guided-note" role="status">{item.name}：{item.place_error}</p>)}
    {Object.entries(state.lodging_search_errors || {}).map(([area, reason]) => <p key={area} className="guided-note" role="status">{area}住宿查询失败：{reason}</p>)}
    {notice && <p role="status">{notice}{notice.includes('列表') && <button onClick={() => refreshAfterCommit(refreshPlans, setNotice)}>刷新列表</button>}</p>}
    <BusyStatus task={busy ? '正在保存、计算路线与更新审核' : ''} />
    {(uncertain || !audit) && <div id="review-reload" className="guided-error"><p>请重新读取已保存的行程。</p><button onClick={() => {
      if ((edits.length || invalidDraft) && !window.confirm("重新读取会放弃尚未保存的修改，是否继续？")) return;
      setRevision(x => x + 1);
    }}>重新读取并核实</button></div>}
    {legacy && <p className="guided-error">这是旧规则生成的行程，可能包含重复地点或旧分类。重新生成将按最新规则整理；旧版自动未安排记录不会作为手动暂存保留。</p>}
    <div className="review-day-toolbar">
      <nav className="review-day-nav" aria-label="选择行程日期">{Array.from({ length: days }, (_, n) => n + 1).map((n) => {
        const count = issues.filter(issue => issue.day === n).length;
        return <button key={n} aria-pressed={day === n} onClick={() => setDay(n)}>第 {n} 天{count > 0 && <small> · {count} 条提示</small>}</button>;
      })}</nav>
      <details className="review-costs">
        <summary><Wallet size={17} aria-hidden="true" />预计费用 {costs && <strong>¥{costs.estimated_total}</strong>}</summary>
        <div className="review-cost-popover">
          {issueAt('review-budget')}
          <p className="guided-note">已知费用及实际支出，不含未知费用。</p>
          {costs && <><div className="review-cost-table"><table><thead><tr><th>项目</th><th>计价依据</th><th>来源</th><th>金额（元）</th></tr></thead><tbody>{costs.items.map((item, index) => <tr key={`${item.reference_id}-${index}`}><td>{item.label}{item.retained_expense && "（已支付，已移出行程）"}</td><td>{item.basis || "—"}</td><td>{item.purchased ? "实际支付" : item.source || "用户记录"}</td><td>{item.purchased ? item.actual_total : item.cost_unknown ? "未知 · 不计入" : item.counted_total}</td></tr>)}</tbody></table></div><p><strong>已知费用合计 {costs.estimated_total} 元</strong> · 总预算 {plan.budget} 元</p></>}
          {editable && <form id="review-budget" data-review-form="budget" className="review-budget-form" onSubmit={event => { event.preventDefault(); if (validMoney(budget)) action('update_budget', { budget: Number(budget) }); }}><label>调整总预算（元）<input name="budget" type="number" min="0" step="0.01" required value={budget} disabled={uncertain} onChange={event => { setBudget(event.target.value); if (validMoney(event.target.value)) action("update_budget", { budget: Number(event.target.value) }); }} /></label><button disabled={busy || uncertain || !validMoney(budget)}>应用预算调整</button></form>}
        </div>
      </details>
    </div>
    <div className="review-day-overview"><strong>第 {day} 天</strong><span>{items.filter(isMovableStop).length} 个停留地点</span><span>活动 {items.filter(isMovableStop).reduce((sum, x) => sum + (x.duration_minutes || 0), 0)} 分钟</span><span>交通 {(plan.routes || []).filter(r => items.some(x => x.item_id === r.origin_item_id)).reduce((sum, r) => sum + (r.duration_minutes || 0), 0)} 分钟</span></div>
    {issueHints(issue => issue.kind === "day" && issue.day === day)}
    <section className="review-map-panel" aria-label={`第${day}天的路线地图`}>
        <div className="review-map-caption"><strong>第 {day} 天行程路线图</strong><span>高德地图，地点编号对应下方卡片</span></div>
      {draftMaps ? (draftMaps[day]?.points ? <RouteMap data={draftMaps[day]} /> : <div className="review-map-placeholder">当天草稿暂无可用地图</div>)
        : mapBusy ? <div className="review-map-placeholder" role="status">正在读取当天路线地图…</div> : map?.points && !mapError ? <RouteMap data={map} /> : <div className="review-map-placeholder" role="status">{mapError || "确认地点后显示路线地图"}</div>}
      {mapHasDraftChanges && <p className="guided-note">当前地图显示已保存的路线。保存后更新。</p>}
      {mapError && <button disabled={mapBusy} onClick={() => setMapRevision(x => x + 1)}>重试当天地图</button>}
      {map?.warnings?.map((warning) => <p key={warning} className="guided-note">{warning}</p>)}
    </section>
    {editable && canSetDeparture(items[0]) && <form className="review-departure" id={departureForm} key={`${departureForm}-${editorVersion}`} data-review-form={departureForm} onSubmit={event => event.preventDefault()}>
      <label><span><Clock3 size={19} aria-hidden="true" />当天出发时间</span><input name="time" type="time" required disabled={uncertain}
        defaultValue={formDrafts[departureForm]?.values.time ?? items[0].time_of_day}
        onChange={event => { if (event.target.validity.valid) action('day_departure', { day, time: event.target.value }); }} /></label>
      {issueAt(departureForm)}
    </form>}
    <div className="review-columns">
      <section className="review-today" id="review-today" tabIndex={-1}>
        <h2>当天行程 <small>{items.filter((x) => isMovableStop(x)).length} 个停留地点</small></h2>
        {guideText && <blockquote className="review-guide-excerpt"><strong>当天攻略原文</strong><p>{guideText}</p></blockquote>}
        {busy && <p role="status" className="guided-note">正在保存、计算路线与更新审核…</p>}
        <div className="review-day-list">
          {items.map((item) => { const route = routeFrom(item.item_id); return <React.Fragment key={item.item_id}>
            {isMovableStop(item) && slot(item.item_id)}
            {card(item)}
            {route && <div className="review-route-leg"><span>↓ {route.station_transfer ? "车站 / 机场接驳" : "前往下一站"}</span><select aria-label={`${item.title}至下一站的交通方式`} value={route.travel_mode || "TRANSIT"} disabled={busy || uncertain || !editable} onChange={(e) => selectRoute(route, e.target.value)}>{Object.entries(modes).map(([value, label]) => <option key={value} value={value} disabled={!route.route_options?.[value]?.success}>{label}{route.route_options?.[value]?.success ? ` · ${route.route_options[value].duration_minutes ?? Math.ceil(parseFloat(route.route_options[value].duration) / 60)} 分钟 · ${route.route_options[value].fare == null ? "费用未知" : `${route.route_options[value].fare} 元`}` : " · 暂无路线"}</option>)}</select><small>{route.duration_minutes} 分钟{route.distance_meters != null && ` · ${(route.distance_meters / 1000).toFixed(1)} 公里`}{route.fare != null && route.source === "高德地图" && ` · 参考费用 ${route.fare} 元`}{route.source === "provisional_estimate" && " · 临时估算"}</small>{route.error ? <p role="status">{route.error}</p> : Object.entries(route.route_options || {}).filter(([, option]) => !option.success && option.message).map(([mode, option]) => <small key={mode}>{modes[mode]}：{option.message}</small>)}</div>}
          </React.Fragment>; })}
          {slot()}
        </div>
      </section>
      <aside className={`review-pending ${target === "null:end" ? "target" : ""}`} {...(editable ? dropProps(null) : {})}><h2>手动暂存区 <small>{pending.length}</small></h2><p className="guided-note">这里只保存你手动移入的地点。可切换日期后加入当天行程。</p>{pending.length ? pending.map((item) => card(item, true)) : <div className="review-pending-empty">暂无手动暂存地点<br />可将当天地点移入这里</div>}</aside>
    </div>
    <section className="review-footer">
      {!editable && <div id="confirmed-repair"><button disabled={busy} onClick={editPlaces}>返回编辑行程</button></div>}
      {editable && <>
        <div id="review-route-tools" className="guided-actions">
          <button disabled={busy || uncertain || invalidDraft || edits.length > 0 || !savedVersion.current}
            onClick={() => action('optimize_routes', { plan_version: savedVersion.current })}>按高德耗时重排行程</button>
          <button disabled={busy || uncertain || invalidDraft || edits.length > 0}
            onClick={() => action('recalculate_routes')}>重新计算路线与审核</button>
          <span>重排只调整当天地点顺序，保留固定时间和饭点约束。</span>
        </div>
        <div id="review-replan-tools" className="guided-actions"><button disabled={busy || uncertain} onClick={editPlaces}>补充攻略地点或住宿</button><button disabled={busy || uncertain || invalidDraft || edits.length > 0} onClick={() => navigate(activitiesUrl)}>按攻略重新匹配并生成</button><button disabled={busy || uncertain} onClick={async () => { if (await action('candidates')) navigate(`/plans/${encode(planId)}/planning`); }}>重新比较交通</button></div>
        {(state.purchases || []).filter(record => issues.some(issue => issue.kind === 'purchase' && issue.id === record.reference_id)).map(record => <div key={record.reference_id} className="guided-issues"><p>{record.label} 已支付 {record.actual_total} 元，当前已不在行程中。可确认保留该笔支出，此操作不办理退票或退款。</p><button disabled={busy || uncertain} onClick={() => action('acknowledge_purchase', { reference_id: record.reference_id })}>确认变更并保留支出</button><button disabled={busy || uncertain} onClick={editPlaces}>返回重新选择</button></div>)}
      </>}
      <div className="guided-actions guided-primary-actions">{editable
        ? <button className="primary-button" disabled={busy} onClick={confirmItinerary}>确认并生成行程单</button>
        : state.needs_replan ? null : <Link className="primary-button" to={`/plans/${encode(planId)}/sheet`}>查看行程单</Link>}</div>
    </section>
  </fieldset></div>;
}
