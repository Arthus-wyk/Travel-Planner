import React from "react";
import { useEffect, useRef, useState } from "react";
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, CalendarDays, ChevronRight, MessageCircle, Plus, Send, Sparkles, Trash2, WalletCards } from "lucide-react";
import RuntimeLogs from "./RuntimeLogs";
import { NewTripForm, PlanningPage, SheetPage } from "./GuidedPlanner";
import ReviewPage from "./ReviewPage";
import { api, refreshAfterCommit } from "./api";
import { BusyStatus, LoadError } from "./planningUi";

function usePlans() {
  const [plans, setPlans] = useState([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const refresh = async () => {
    setLoading(true); setError("");
    try { const data = await api("/api/plans"); setPlans(data.plans || []); }
    catch (cause) { setError(cause.message); throw cause; }
    finally { setLoading(false); }
  };
  useEffect(() => { refresh().catch(() => {}); }, []);
  return { plans, refresh, error, loading };
}
function ScopedPage({ component: Component, ...props }) {
  const { planId, conversationId } = useParams();
  return <Component key={conversationId || planId} {...props} />;
}

function App() {
  const { plans, refresh, error, loading } = usePlans();
  return (
    <div className="app-shell">
      <Sidebar plans={plans} error={error} loading={loading} refresh={refresh} />
      <main className="main-area">
        <Routes>
          <Route path="/logs" element={<RuntimeLogs api={api} />} />
          <Route path="/" element={<WorkspacePage plans={plans} refreshPlans={refresh} />} />
          <Route path="/plans/new" element={<NewTripForm api={api} refreshPlans={refresh} />} />
          <Route path="/plans/:planId/planning" element={<ScopedPage component={PlanningPage} api={api} refreshPlans={refresh} />} />
          <Route path="/plans/:planId/review" element={<ScopedPage component={ReviewPage} api={api} refreshPlans={refresh} />} />
          <Route path="/plans/:planId/sheet" element={<ScopedPage component={SheetPage} api={api} />} />
          <Route path="/conversations/:conversationId" element={<ScopedPage component={ConversationPage} plans={plans} refreshPlans={refresh} />} />
          <Route path="/plans/:planId" element={<ScopedPage component={PlanDetailPage} plans={plans} refreshPlans={refresh} />} />
          <Route path="/plans/:planId/conversations/:conversationId" element={<ScopedPage component={ConversationPage} plans={plans} refreshPlans={refresh} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Sidebar({ plans, error, loading, refresh }) {
  const [expanded, setExpanded] = useState({});
  const [deleting, setDeleting] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const navigate = useNavigate();
  const location = useLocation();
  async function removePlan(plan) {
    if (deleting || !window.confirm(`确定删除“${plan.title || `${plan.destination} 行程`}”吗？`)) return;
    setDeleting(plan.plan_id); setDeleteError("");
    try {
      await api(`/api/plans/${encodeURIComponent(plan.plan_id)}`, { method: "DELETE" });
      const planPath = `/plans/${encodeURIComponent(plan.plan_id)}`;
      if (location.pathname === planPath || location.pathname.startsWith(`${planPath}/`)) navigate("/");
      await refreshAfterCommit(refresh, setDeleteError);
    } catch (cause) { setDeleteError(cause.message); }
    finally { setDeleting(""); }
  }
  return (
    <aside className="sidebar">
      <Link className="brand" to="/"><Sparkles size={20} /> Travel Studio</Link>
      <Link className="new-chat" to="/plans/new"><Plus size={17} /> 新建行程</Link>
      <Link className="plan-link log-nav-link" to="/logs" title="模型运行日志"><MessageCircle size={16} /><span>运行日志</span></Link>
      <div className="side-section-title">我的行程</div>
      <nav className="plan-nav">
        {loading && <p role="status">正在读取行程…</p>}
        {error && <LoadError error={error} onRetry={() => refresh().catch(() => {})} />}
        {deleteError && <p role="alert" className="delete-plan-error">{deleteError}</p>}
        {plans.map((plan) => (
          <div key={plan.plan_id} className="plan-nav-group">
            <div className="plan-nav-row"><Link to={`/plans/${encodeURIComponent(plan.plan_id)}`} className="plan-link">
              <CalendarDays size={16} /><span>{plan.title || `${plan.destination} 行程`}</span><ChevronRight size={15} />
            </Link><button type="button" className="delete-plan" aria-label={`删除${plan.title || `${plan.destination} 行程`}`} title="删除行程" disabled={Boolean(deleting)} onClick={() => removePlan(plan)}><Trash2 size={15} /></button></div>
            {expanded[plan.plan_id] && <ConversationMiniList planId={plan.plan_id} />}
            <button className="show-conversations" onClick={() => setExpanded((state) => ({ ...state, [plan.plan_id]: !state[plan.plan_id] }))}>
              <MessageCircle size={13} /> {expanded[plan.plan_id] ? "收起对话" : "查看对话"}
            </button>
          </div>
        ))}
        {!loading && !error && !plans.length && <div className="empty-side">还没有行程，先创建旅行计划。</div>}
      </nav>
    </aside>
  );
}

function ConversationMiniList({ planId }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setItems(null); setError("");
    api(`/api/plans/${encodeURIComponent(planId)}/conversations`, { signal: controller.signal }).then(data => setItems(data.conversations || [])).catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [planId, revision]);
  return <div className="conversation-mini-list">{error ? <LoadError error={error} onRetry={() => setRevision(x => x + 1)} /> : items === null ? <p>读取对话中…</p> : !items.length ? <p>暂无对话</p> : items.map(item => <Link key={item.conversation_id} to={`/plans/${encodeURIComponent(planId)}/conversations/${encodeURIComponent(item.conversation_id)}`}>{item.title}</Link>)}</div>;
}

function WorkspacePage({ plans, refreshPlans }) {
  const navigate = useNavigate();
  return <EmptyWorkspace onStart={() => navigate("/plans/new")} plans={plans} refreshPlans={refreshPlans} />;
}

function EmptyWorkspace({ onStart }) {
  return <section className="welcome-card"><Sparkles size={42} /><p className="eyebrow">Travel Plan Studio</p><h1>从填写旅行需求开始</h1><p>选择交通和酒店，整理攻略与想去的地方，再生成可检查的行程单。</p><button className="primary-button" onClick={onStart}><Plus size={18} /> 新建行程</button></section>;
}

function PlanDetailPage({ plans, refreshPlans }) {
  const { planId } = useParams();
  const [plan, setPlan] = useState(null);
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setPlan(null); setError("");
    api(`/api/plans/${encodeURIComponent(planId)}`, { signal: controller.signal }).then(data => setPlan(data.plan)).catch(cause => { if (!controller.signal.aborted) setError(cause.message); });
    return () => controller.abort();
  }, [planId, revision]);
  async function newConversation() {
    if (busy) return;
    setBusy(true); setError("");
    try { const data = await api("/api/conversations", { method: "POST", body: JSON.stringify({ plan_id: planId, title: `${plan?.title || plan?.destination || "行程"} · 新对话` }) });
    navigate(`/plans/${encodeURIComponent(planId)}/conversations/${data.conversation.conversation_id}`); }
    catch (cause) { setError(cause.message); } finally { setBusy(false); }
  }
  if (!plan) return error ? <LoadError error={error} onRetry={() => setRevision(x => x + 1)} /> : <Loading />;
  return <div className="detail-page"><PageHeader title={plan.title || `${plan.destination} 行程`} action={<div className="guided-actions"><Link className="primary-button small" to={`/plans/${encodeURIComponent(planId)}/planning`}>规划向导</Link>{plan.planning?.stage === "confirmed" && <Link className="primary-button small" to={`/plans/${encodeURIComponent(planId)}/sheet`}>行程单</Link>}<button className="primary-button small" disabled={busy} onClick={newConversation}><Plus size={16} /> 对话调整</button></div>} />
    {error && <LoadError error={error} onRetry={() => setRevision(x => x + 1)} />}<div className="plan-hero"><div><div className="eyebrow">当前行程</div><h1>{plan.destination}</h1><p>{plan.start_date} 至 {plan.end_date} · {plan.travelers} 人 · {plan.travel_style}</p></div><div className="hero-metrics"><Metric icon={CalendarDays} label="日期" value={`${plan.start_date} — ${plan.end_date}`} /><Metric icon={WalletCards} label="预算" value={`${plan.budget} ${plan.currency}`} /></div></div>
    <div className="detail-grid"><section className="detail-card"><h2>行程安排</h2>{(plan.itinerary || []).length ? <ItineraryTimeline itinerary={plan.itinerary} routes={plan.routes || []} currency={plan.currency} /> : <EmptyState text="还没有具体安排，可以在对话中继续规划。" />}</section><aside className="detail-card detail-aside"><h2>旅行偏好</h2>{[...(plan.interests || []), ...(plan.constraints || [])].length ? <div className="tag-cloud">{[...(plan.interests || []), ...(plan.constraints || [])].map((item) => <span key={item}>{item}</span>)}</div> : <p className="aside-empty">还没有添加偏好，可在对话中补充。</p>}<h2 className="section-gap">推荐地点</h2>{(plan.recommendations || []).length ? (plan.recommendations || []).map((item) => <div className="recommendation-row" key={item.recommendation_id}><strong>{item.name}</strong><span>{item.category} · {item.priority}</span></div>) : <p className="aside-empty">规划时会在这里整理值得去的地点。</p>}</aside></div>
  </div>;
}

function minutesFromTime(value) {
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value || "")) return null;
  const [hour, minute] = value.split(":").map(Number);
  return hour * 60 + minute;
}

function displayTime(minutes) {
  if (minutes == null) return "时间待确认";
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function ItineraryTimeline({ itinerary, routes = [], currency }) {
  const byDay = itinerary.reduce((groups, item) => {
    (groups[item.day] ||= []).push(item);
    return groups;
  }, {});
  return <div className="day-timelines">{Object.entries(byDay).sort(([a], [b]) => Number(a) - Number(b)).map(([day, rawItems]) => {
    const items = [...rawItems].sort((a, b) => (minutesFromTime(a.time_of_day) ?? 1440) - (minutesFromTime(b.time_of_day) ?? 1440));
    return <details className="day-timeline" key={day} open={Number(day) === 1 || undefined}><summary className="day-heading"><span>DAY {day}</span><strong>第 {day} 天</strong><small>{items.length} 个安排</small><span className="day-toggle">展开 / 收起</span></summary>{items.map((item, index) => {
      const currentStart = minutesFromTime(item.time_of_day);
      const previous = items[index - 1];
      const previousStart = previous && minutesFromTime(previous.time_of_day);
      const previousEnd = previousStart != null && previous.duration_minutes ? previousStart + previous.duration_minutes : null;
      const connectionMinutes = currentStart != null && previousEnd != null ? currentStart - previousEnd : null;
      const nextItem = items[index + 1];
      const route = nextItem && routes.find((candidate) => candidate.origin_item_id === item.item_id && candidate.destination_item_id === nextItem.item_id);
      const routeDistance = route?.localized_values?.distance?.text || (route?.distance_meters != null ? `${(route.distance_meters / 1000).toFixed(1)} 公里` : null);
      const routeDuration = route?.localized_values?.duration?.text || route?.duration || null;
      const routeSummary = route ? [routeDistance, routeDuration].filter(Boolean).join(" · ") : null;
      return <React.Fragment key={item.item_id}><article className="itinerary-row timeline-activity"><strong>{item.time_of_day || "待定"}</strong><div><h3>{item.title}</h3><p>{item.location}</p><small>{item.duration_minutes ? `游玩 ${item.duration_minutes} 分钟` : "时长待确认"}</small></div><span>{item.estimated_cost || 0} {currency}</span></article>{index < items.length - 1 && <div className="route-connector"><span className="route-line" /><div><strong>{item.location} → {nextItem.location}</strong><p>{routeSummary ? `交通：${routeSummary}` : (connectionMinutes != null && connectionMinutes >= 0 ? `间隔 ${connectionMinutes} 分钟` : "交通时间待查询")}</p></div></div>}</React.Fragment>;
    })}</details>;
  })}</div>;
}

function ConversationPage({ plans, refreshPlans }) {
  const { planId, conversationId } = useParams();
  const plan = plans.find((item) => item.plan_id === planId);
  return <div className="conversation-page"><PageHeader title={plan?.title || "对话"} backHref={planId ? `/plans/${encodeURIComponent(planId)}` : "/"} action={<Link className="back-link" to={`/logs?conversation_id=${encodeURIComponent(conversationId)}`}>运行日志 <ChevronRight size={15} /></Link>} /><div className="conversation-layout">{plan && <PlanContextCard plan={plan} />}<Chat key={conversationId} conversationId={conversationId} planId={planId} refreshPlans={refreshPlans} /></div></div>;
}

function Chat({ conversationId, refreshPlans }) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [warning, setWarning] = useState("");
  const [failed, setFailed] = useState("");
  const [uncertain, setUncertain] = useState(false);
  const [loading, setLoading] = useState(true);
  const lock = useRef(false);
  const [revision, setRevision] = useState(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    api(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, { signal: controller.signal }).then(data => { setMessages(data.messages || []); }).catch(cause => { if (!controller.signal.aborted) setError(cause.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [conversationId, revision]);
  async function send(retryContent) {
    const content = (typeof retryContent === 'string' ? retryContent : draft).trim();
    if (!content || lock.current || uncertain) return;
    lock.current = true; setSending(true); setError("");
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, { method: "POST", body: JSON.stringify({ content }) });
      if (!mounted.current) return;
      setMessages(data.messages || []); setDraft(current => current.trim() === content ? "" : current); setFailed("");
      await refreshAfterCommit(refreshPlans, setWarning);
    } catch (cause) { if (mounted.current) { setError(cause.message); setFailed(content); setUncertain(Boolean(cause.uncertain)); } }
    finally { lock.current = false; if (mounted.current) setSending(false); }
  }
  return <section className="chat-card"><div className="chat-header"><h2>行程对话</h2><span>{messages.filter(item => item.role !== "tool").length} 条消息</span></div>
    {error && <LoadError error={error} onRetry={() => setRevision(x => x + 1)} />}
    {failed && <div className="guided-error"><p>未确认发送成功的原文：{failed}</p>{uncertain ? <><p>请先读取消息核实结果；如果服务仍在执行，请等待完成。</p><button onClick={() => { if (window.confirm('已核实本条消息没有成功执行，允许重新发送？')) setUncertain(false); }}>我已核实未执行</button></> : <button disabled={sending} onClick={() => send(failed)}>重试发送原文</button>}</div>}
    {warning && <p role="status">{warning}<button onClick={() => refreshAfterCommit(refreshPlans, setWarning).then(() => {})}>刷新列表</button></p>}
    <div className="messages">{loading ? <p role="status">读取消息中…</p> : <>{!messages.length && !error && <p>暂无消息，可以输入旅行需求。</p>}{messages.map(message => <Message key={message.message_id} message={message} conversationId={conversationId} />)}</>}<BusyStatus task={sending ? '正在思考并更新行程' : ''} /></div>
    <div className="composer"><textarea value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) send(); }} placeholder="告诉 Agent 你想如何调整这次行程…" /><button className="primary-button" onClick={() => send()} disabled={sending || loading || uncertain || !draft.trim()}><Send size={17} />{sending ? '发送中' : '发送'}</button></div></section>;
}

function Message({ message, conversationId }) {
  if (message.role === "tool") return <div className="tool-message"><span>工具调用</span><code>{message.tool_name || "tool"}</code><p>{message.content}</p></div>;
  const role = message.role === "user" ? "user" : "assistant";
  return <div className={`message ${role}`}><div className="message-role">{role === "user" ? "你" : "Agent"}</div><p>{message.content}</p>{message.metadata?.run_id && <Link className="log-message-link" to={`/logs?conversation_id=${encodeURIComponent(conversationId)}&run_id=${encodeURIComponent(message.metadata.run_id)}`}>查看本次运行日志 <ChevronRight size={12} /></Link>}{message.metadata?.actions?.map((action) => <Link className="message-action" key={action.href} to={action.href}>{action.label} <ChevronRight size={15} /></Link>)}</div>;
}

function PlanContextCard({ plan }) { return <aside className="context-card"><div className="eyebrow">当前行程</div><h2>{plan.title || `${plan.destination} 行程`}</h2><p>{plan.destination}</p><dl><div><dt>日期</dt><dd>{plan.start_date} — {plan.end_date}</dd></div><div><dt>人数</dt><dd>{plan.travelers} 人</dd></div><div><dt>预算</dt><dd>{plan.budget} {plan.currency}</dd></div></dl><Link to={`/plans/${encodeURIComponent(plan.plan_id)}`}>查看完整行程 <ChevronRight size={15} /></Link></aside>; }
function PageHeader({ title, action, backHref }) { return <header className="page-header">{backHref && <Link to={backHref} className="back-link"><ArrowLeft size={17} /> 返回</Link>}<h1>{title}</h1>{action}</header>; }
function Metric({ icon: Icon, label, value }) { return <div className="metric"><Icon size={18} /><span>{label}</span><strong>{value}</strong></div>; }
function EmptyState({ text }) { return <div className="empty-state">{text}</div>; }
function Loading() { return <div className="loading">正在加载…</div>; }

export default App;
