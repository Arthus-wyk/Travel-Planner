import React from "react";
import { useEffect, useMemo, useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, CalendarDays, ChevronRight, MessageCircle, Plus, Send, Sparkles, WalletCards } from "lucide-react";

const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8787";
const itineraryStyles = `.day-timelines{display:grid;gap:24px}.day-heading{display:flex;gap:10px;padding:4px 0 8px;color:#4f46e5}.day-heading span{font-size:.72rem;font-weight:800;letter-spacing:.1em}.day-heading strong{color:#0f172a}.timeline-activity small{color:#94a3b8}.route-connector{display:grid;grid-template-columns:76px minmax(0,1fr);gap:15px;min-height:58px;padding:7px 0;color:#64748b}.route-line{position:relative;justify-self:center;width:2px;background:#c7d2fe}.route-line:before,.route-line:after{position:absolute;left:50%;width:8px;height:8px;border:2px solid #818cf8;border-radius:50%;background:#fff;content:"";transform:translateX(-50%)}.route-line:before{top:0}.route-line:after{bottom:0}.route-connector strong{color:#4f46e5;font-size:.84rem}.route-connector p{margin:3px 0;font-size:.78rem}`;

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.success === false) throw new Error(data.message || "请求失败");
  return data;
}

function usePlans() {
  const [plans, setPlans] = useState([]);
  const refresh = () => api("/api/plans").then((data) => setPlans(data.plans || []));
  useEffect(() => { refresh().catch(console.error); }, []);
  return { plans, refresh };
}

function App() {
  const { plans, refresh } = usePlans();
  return (
    <div className="app-shell">
      <style>{itineraryStyles}</style>
      <Sidebar plans={plans} />
      <main className="main-area">
        <Routes>
          <Route path="/" element={<WorkspacePage plans={plans} refreshPlans={refresh} />} />
          <Route path="/conversations/:conversationId" element={<ConversationPage plans={plans} refreshPlans={refresh} />} />
          <Route path="/plans/:planId" element={<PlanDetailPage plans={plans} refreshPlans={refresh} />} />
          <Route path="/plans/:planId/conversations/:conversationId" element={<ConversationPage plans={plans} refreshPlans={refresh} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Sidebar({ plans }) {
  const [expanded, setExpanded] = useState({});
  return (
    <aside className="sidebar">
      <Link className="brand" to="/"><Sparkles size={20} /> Travel Studio</Link>
      <Link className="new-chat" to="/"><Plus size={17} /> 新建对话</Link>
      <div className="side-section-title">我的行程</div>
      <nav className="plan-nav">
        {plans.map((plan) => (
          <div key={plan.plan_id} className="plan-nav-group">
            <Link to={`/plans/${encodeURIComponent(plan.plan_id)}`} className="plan-link">
              <CalendarDays size={16} /><span>{plan.title || `${plan.destination} 行程`}</span><ChevronRight size={15} />
            </Link>
            {expanded[plan.plan_id] && <ConversationMiniList planId={plan.plan_id} />}
            <button className="show-conversations" onClick={() => setExpanded((state) => ({ ...state, [plan.plan_id]: !state[plan.plan_id] }))}>
              <MessageCircle size={13} /> {expanded[plan.plan_id] ? "收起对话" : "查看对话"}
            </button>
          </div>
        ))}
        {!plans.length && <div className="empty-side">还没有行程，先从新对话开始。</div>}
      </nav>
    </aside>
  );
}

function ConversationMiniList({ planId }) {
  const [items, setItems] = useState([]);
  useEffect(() => { api(`/api/plans/${encodeURIComponent(planId)}/conversations`).then((data) => setItems(data.conversations || [])).catch(console.error); }, [planId]);
  return <div className="conversation-mini-list">{items.map((item) => <Link key={item.conversation_id} to={`/plans/${encodeURIComponent(planId)}/conversations/${encodeURIComponent(item.conversation_id)}`}>{item.title}</Link>)}</div>;
}

function WorkspacePage({ plans, refreshPlans }) {
  const [conversation, setConversation] = useState(null);
  const navigate = useNavigate();
  async function start() {
    const data = await api("/api/conversations", { method: "POST", body: JSON.stringify({ title: "新对话" }) });
    setConversation(data.conversation);
    navigate(`/conversations/${data.conversation.conversation_id}`);
  }
  if (conversation) return <Navigate to={`/conversations/${conversation.conversation_id}`} replace />;
  return <EmptyWorkspace onStart={start} plans={plans} refreshPlans={refreshPlans} />;
}

function EmptyWorkspace({ onStart }) {
  return <section className="welcome-card"><Sparkles size={42} /><p className="eyebrow">Travel Plan Studio</p><h1>从一个对话开始规划旅程</h1><p>创建新行程、修改已有安排，所有消息都会持久保存。</p><button className="primary-button" onClick={onStart}><Plus size={18} /> 新建对话</button></section>;
}

function PlanDetailPage({ plans, refreshPlans }) {
  const { planId } = useParams();
  const [plan, setPlan] = useState(null);
  const navigate = useNavigate();
  useEffect(() => { api(`/api/plans/${encodeURIComponent(planId)}`).then((data) => setPlan(data.plan)).catch(console.error); }, [planId]);
  async function newConversation() {
    const data = await api("/api/conversations", { method: "POST", body: JSON.stringify({ plan_id: planId, title: `${plan?.title || plan?.destination || "行程"} · 新对话` }) });
    navigate(`/plans/${encodeURIComponent(planId)}/conversations/${data.conversation.conversation_id}`);
  }
  if (!plan) return <Loading />;
  return <div className="detail-page"><PageHeader title={plan.title || `${plan.destination} 行程`} action={<button className="primary-button small" onClick={newConversation}><Plus size={16} /> 新建对话</button>} />
    <div className="plan-hero"><div><div className="eyebrow">当前行程</div><h1>{plan.destination}</h1><p>{plan.start_date} 至 {plan.end_date} · {plan.travelers} 人 · {plan.travel_style}</p></div><div className="hero-metrics"><Metric icon={CalendarDays} label="日期" value={`${plan.start_date} — ${plan.end_date}`} /><Metric icon={WalletCards} label="预算" value={`${plan.budget} ${plan.currency}`} /></div></div>
    <div className="detail-grid"><section className="detail-card"><h2>行程安排</h2>{(plan.itinerary || []).length ? <ItineraryTimeline itinerary={plan.itinerary} routes={plan.routes || []} currency={plan.currency} /> : <EmptyState text="还没有具体安排，可以在对话中继续规划。" />}</section><section className="detail-card"><h2>偏好与约束</h2><div className="tag-cloud">{[...(plan.interests || []), ...(plan.constraints || [])].map((item) => <span key={item}>{item}</span>)}</div><h2 className="section-gap">推荐</h2>{(plan.recommendations || []).map((item) => <div className="recommendation-row" key={item.recommendation_id}><strong>{item.name}</strong><span>{item.category} · {item.priority}</span></div>)}</section></div>
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
    return <section className="day-timeline" key={day}><div className="day-heading"><span>DAY {day}</span><strong>第 {day} 天</strong></div>{items.map((item, index) => {
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
    })}</section>;
  })}</div>;
}

function ConversationPage({ plans, refreshPlans }) {
  const { planId, conversationId } = useParams();
  const plan = plans.find((item) => item.plan_id === planId);
  return <div className="conversation-page"><PageHeader title={plan?.title || "对话"} backHref={planId ? `/plans/${encodeURIComponent(planId)}` : "/"} /><div className="conversation-layout">{plan && <PlanContextCard plan={plan} />}<Chat conversationId={conversationId} planId={planId} refreshPlans={refreshPlans} /></div></div>;
}

function Chat({ conversationId, planId, refreshPlans }) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const load = () => api(`/api/conversations/${encodeURIComponent(conversationId)}/messages`).then((data) => setMessages(data.messages || []));
  useEffect(() => { load().catch(console.error); }, [conversationId]);
  async function send() {
    const content = draft.trim();
    if (!content || sending) return;
    setSending(true); setDraft("");
    try { const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, { method: "POST", body: JSON.stringify({ content }) }); setMessages(data.messages || []); await refreshPlans(); }
    catch (error) { setMessages((items) => [...items, { message_id: crypto.randomUUID(), role: "assistant", content: `发送失败：${error.message}` }]); }
    finally { setSending(false); }
  }
  return <section className="chat-card"><div className="chat-header"><div><div className="eyebrow">Persistent Thread</div><h2>行程对话</h2></div><span>{messages.filter((item) => item.role !== "tool").length} 条消息</span></div><div className="messages">{messages.map((message) => <Message key={message.message_id} message={message} />)}{sending && <div className="message assistant"><div className="message-role">Agent</div><p>正在思考并更新行程…</p></div>}</div><div className="composer"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) send(); }} placeholder="告诉 Agent 你想如何调整这次行程…" /><button className="primary-button" onClick={send} disabled={sending}><Send size={17} /> {sending ? "发送中" : "发送"}</button></div></section>;
}

function Message({ message }) {
  if (message.role === "tool") return <div className="tool-message"><span>工具调用</span><code>{message.tool_name || "tool"}</code><p>{message.content}</p></div>;
  const role = message.role === "user" ? "user" : "assistant";
  return <div className={`message ${role}`}><div className="message-role">{role === "user" ? "你" : "Agent"}</div><p>{message.content}</p>{message.metadata?.actions?.map((action) => <Link className="message-action" key={action.href} to={action.href}>{action.label} <ChevronRight size={15} /></Link>)}</div>;
}

function PlanContextCard({ plan }) { return <aside className="context-card"><div className="eyebrow">当前行程</div><h2>{plan.title || `${plan.destination} 行程`}</h2><p>{plan.destination}</p><dl><div><dt>日期</dt><dd>{plan.start_date} — {plan.end_date}</dd></div><div><dt>人数</dt><dd>{plan.travelers} 人</dd></div><div><dt>预算</dt><dd>{plan.budget} {plan.currency}</dd></div></dl><Link to={`/plans/${encodeURIComponent(plan.plan_id)}`}>查看完整行程 <ChevronRight size={15} /></Link></aside>; }
function PageHeader({ title, action, backHref }) { return <header className="page-header">{backHref && <Link to={backHref} className="back-link"><ArrowLeft size={17} /> 返回</Link>}<h1>{title}</h1>{action}</header>; }
function Metric({ icon: Icon, label, value }) { return <div className="metric"><Icon size={18} /><span>{label}</span><strong>{value}</strong></div>; }
function EmptyState({ text }) { return <div className="empty-state">{text}</div>; }
function Loading() { return <div className="loading">正在加载…</div>; }

export default App;
