import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Activity, ArrowLeft, ChevronRight, Clock3, RefreshCw, Search } from "lucide-react";
import "./runtime-logs.css";

const statuses = { completed: "已完成", failed: "失败", needs_input: "待补充信息", cancelled: "已取消", interrupted: "已中断", limit_reached: "达到轮次上限", running: "运行中 / 未收尾", legacy: "历史日志" };
const titles = {
  run_start: "开始运行", run_end: "运行结束", model_request: "模型请求", model_response: "模型响应", model_error: "模型请求失败",
  model_cancelled: "模型调用已取消",
  message: "消息", tool_use: "调用工具", tool_result: "工具结果", intent_understanding: "历史意图分析",
  request_rewrite: "指代改写", agent_start: "Agent 启动", agent_end: "Agent 结束",
  no_tool_guard: "工具调用护栏", memory_extraction: "记忆提取", api_summary: "Token 汇总",
  tengu_api_query: "API 请求", tengu_api_success: "API 成功", tengu_api_error: "API 异常 / 重试",
  generation_progress: "生成进度", generation_error: "生成异常", api_request: "地图 / API 请求", api_response: "地图 / API 响应",
};
const stages = { coordinator: "检查旅行需求", generate_guide: "识别攻略地点", activity_agent: "识别攻略地点", search_places: "查找真实地点并匹配候选", place_matching: "地点匹配", place_match_agent: "查找真实地点并匹配候选", default_choices: "整理住宿与地点", prepare_draft: "准备每日安排", itinerary_agent: "安排每日行程", route_agent: "计算路线", schedule_agent: "安排时间", audit_agent: "检查时间和预算", generation_job: "生成任务", resuming: "恢复任务" };
const dateTime = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const duration = (value) => value == null ? "—" : `${(value / 1000).toFixed(2)} 秒`;

function preview(event) {
  if (event.type === "request_rewrite") return event.rewritten_text || event.original_text || "";
  if (event.type === "intent_understanding" && event.stage?.startsWith("review_intent")) {
    return event.goal || event.semantic?.primary_intent || "";
  }
  const value = event.input ?? event.content ?? event.answer ?? event.result ?? event.assistant_content ?? (typeof event.message === "object" ? event.message.content : event.message);
  return typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
}

function isError(event) {
  return event.isError || event.type === "generation_error" || event.type === "model_error" || event.type === "model_cancelled" || event.type === "tengu_api_error" ||
    (event.type === "run_end" && event.status !== "completed") ||
    (event.type === "tool_result" && /"success"\s*:\s*false|"status"\s*:\s*"failed"/.test(event.result || ""));
}

function EventDetails({ event, request }) {
  const [rawOpen, setRawOpen] = useState(false);
  const sections = [];
  if (event.type === "model_request" || event.type === "model_response") {
    const input = event.type === "model_request" ? event : request;
    const { messages, ...parameters } = input?.body || { model: input?.model, tools: input?.tools, responseFormat: input?.responseFormat };
    sections.push(["Prompt（模型输入）", input?.messages ?? messages ?? input?.content ?? "对应请求未保留，无法还原 Prompt。"]);
    if (input) sections.push(["模型请求参数", parameters]);
    if (event.type === "model_response") {
      sections.push(["模型输出", event.body?.message ?? event.body?.choices?.[0]?.message ?? event.body ?? { content: event.content, tool_calls: event.tool_calls }]);
    }
  } else if (event.type === "api_request" || event.type === "api_response" || (event.type === "generation_error" && event.url && event.params)) {
    const input = event.type === "api_request" ? event : request;
    sections.push(["请求参数", input || event.params != null ? {
      method: input?.method ?? event.method, url: input?.url ?? event.url,
      query: input?.params ?? event.params ?? input?.query, body: input?.body,
    } : "对应请求未保留，无法还原请求参数。"]);
    if (event.type !== "api_request") sections.push(["响应内容", event.body ?? event.content ?? "未记录响应内容"]);
    if (event.type === "generation_error") sections.push(["异常信息", event.traceback ?? event.message]);
  }
  if (!sections.length) return <><p className="log-event-preview">{preview(event).slice(0, 240)}</p><pre>{JSON.stringify(event, null, 2)}</pre></>;
  return <>
    {event.status != null && <p className="log-event-preview">HTTP 状态码：{event.status}</p>}
    {sections.map(([label, value]) => <section className="log-event-section" aria-label={label} key={label}><h3>{label}</h3><pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre></section>)}
    <details className="log-raw" onToggle={toggle => setRawOpen(toggle.currentTarget.open)}><summary>原始日志 JSON</summary>{rawOpen && <pre>{JSON.stringify(event, null, 2)}</pre>}</details>
  </>;
}

function LogEvent({ event, load }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const pending = React.useRef(false);
  async function open(eventToggle) {
    if (!eventToggle.currentTarget.open || data || pending.current) return;
    pending.current = true;
    setError("");
    try { setData(await load(event.eventId)); }
    catch (cause) { setError(cause.message); }
    finally { pending.current = false; }
  }
  return <details className="log-event" onToggle={open}>
    <summary><span><strong>{titles[event.type] || event.type}</strong>{event.stage && <code>{stages[event.stage] || event.stage}</code>}{event.tool && <code>{event.tool}</code>}{event.attempt != null && <small>尝试 {event.attempt}</small>}</span><small>{event.durationMs != null ? duration(event.durationMs) : "展开详情"}</small></summary>
    {data ? <EventDetails event={data.event} request={data.request} /> : error ? <p role="alert">{error}，请收起后重新展开。</p> : <p>正在加载事件详情…</p>}
  </details>;
}

export default function RuntimeLogs({ api }) {
  const [params, setParams] = useSearchParams();
  const conversationId = params.get("conversation_id") || "";
  const jobId = params.get("job_id") || "";
  const selectedId = params.get("run_id") || "";
  const status = params.get("status") || "";
  const since = params.get("since") || "";
  const until = params.get("until") || "";
  const requestedPage = params.get("page") || "1";
  const [runs, setRuns] = useState([]);
  const [pagination, setPagination] = useState({ page: 1, page_count: 1, total: 0 });
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [truncated, setTruncated] = useState(false);
  const [revision, setRevision] = useState(0);
  const [category, setCategory] = useState("all");
  const [search, setSearch] = useState("");
  const [matches, setMatches] = useState(null);
  const [searchError, setSearchError] = useState("");
  const query = new URLSearchParams({ conversation_id: conversationId, job_id: jobId, status, since, until }).toString();
  const page = /^\d+$/.test(requestedPage) && Number.isSafeInteger(Number(requestedPage)) ? Math.max(1, Number(requestedPage)) : 1;
  const detailPath = `/api/logs/runs/${encodeURIComponent(selectedId)}`;
  const detailQuery = new URLSearchParams({ conversation_id: conversationId, view: "timeline" }).toString();

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    api(`/api/logs/runs?${query}&page=${page}`, { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) {
        setRuns(data.runs); setTruncated(data.truncated);
        setPagination({ page: data.page, page_count: data.page_count, total: data.total });
      }
    }).catch((cause) => { if (!controller.signal.aborted) { setError(cause.message); setRuns([]); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [query, page, revision, api]);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null); setDetailError(""); setCategory("all"); setSearch("");
    if (!selectedId) { setDetailLoading(false); return; }
    setDetailLoading(true);
    api(`${detailPath}?${detailQuery}`, { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) setDetail(data);
    }).catch((cause) => { if (!controller.signal.aborted) setDetailError(cause.message); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => controller.abort();
  }, [selectedId, detailPath, detailQuery, revision, api]);

  useEffect(() => {
    setMatches(null); setSearchError("");
    const term = search.trim();
    if (!selectedId || !term) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api(`${detailPath}?${detailQuery}&search=${encodeURIComponent(term)}`, { signal: controller.signal })
        .then(data => { if (!controller.signal.aborted) setMatches(new Set(data.events.map(event => event.eventId))); })
        .catch(cause => { if (!controller.signal.aborted) setSearchError(cause.message); });
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [selectedId, detailPath, detailQuery, search, revision, api]);

  function applyFilters(event) {
    event.preventDefault();
    const next = new URLSearchParams(new FormData(event.currentTarget));
    for (const [key, value] of [...next]) if (!value) next.delete(key);
    setParams(next);
  }
  function selectRun(id) {
    const next = new URLSearchParams(params);
    next.set("run_id", id); setParams(next);
  }
  function changePage(page) {
    const next = new URLSearchParams(params);
    next.set("page", String(page));
    setParams(next);
  }

  const events = detail?.events || [];
  const responses = events.filter((event) => event.type === "model_response");
  const inputTokens = responses.reduce((sum, event) => sum + (event.usage?.inputTokens || 0), 0);
  const outputTokens = responses.reduce((sum, event) => sum + (event.usage?.outputTokens || 0), 0);
  const visible = events.filter((event) => {
    const matchesCategory = category === "all" || (category === "model" ? /^(model_|tengu_api_|api_)/.test(event.type) : category === "tool" ? event.type.startsWith("tool_") : isError(event));
    return matchesCategory && (!matches || matches.has(event.eventId));
  });

  return <div className="logs-page">
    <header className="page-header"><h1>运行日志</h1><button className="secondary-button" onClick={() => setRevision((value) => value + 1)} disabled={loading || detailLoading}><RefreshCw size={16} aria-hidden="true" />刷新</button></header>
    <p className="logs-intro">查看对话与行程生成的阶段进度、模型上下文、地图调用和异常堆栈。日志包含旅行及对话内容，仅供本机诊断。</p>
    {conversationId && <Link className="back-link" to={`/conversations/${encodeURIComponent(conversationId)}`}><ArrowLeft size={15} /> 返回对话</Link>}
    <form key={query} className="log-filters" onSubmit={applyFilters}>
      <label>会话 ID<input name="conversation_id" defaultValue={conversationId} placeholder="全部会话" /></label>
      <label>任务编号<input name="job_id" defaultValue={jobId} placeholder="全部生成任务" /></label>
      <label>运行状态<select name="status" defaultValue={status}><option value="">全部状态</option>{Object.entries(statuses).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      <label>起始日期（UTC）<input type="date" name="since" defaultValue={since} /></label>
      <label>结束日期（UTC）<input type="date" name="until" defaultValue={until} /></label>
      <button className="primary-button small" type="submit"><Search size={15} />筛选</button>
    </form>
    {error && <p role="alert" className="log-alert">{error}</p>}
    {truncated && <p className="log-notice">日志读取范围有限：最近 100 个会话、每会话末尾 5000 条事件；生成日志每组读取最新 8 MiB（含轮转备份）。更早的事件可能不完整。</p>}
    <div className="logs-layout">
      <section className="log-run-list" aria-label="运行列表"><div className="log-list-heading"><strong>运行记录</strong><span>{pagination.total} 次</span></div>
        {!error && pagination.page_count > 1 && <nav className="log-pagination" aria-label="运行记录分页">
          <button type="button" className="secondary-button" disabled={loading || pagination.page <= 1} onClick={() => changePage(pagination.page - 1)}>上一页</button>
          <span aria-live="polite">{pagination.page} / {pagination.page_count}</span>
          <button type="button" className="secondary-button" disabled={loading || pagination.page >= pagination.page_count} onClick={() => changePage(pagination.page + 1)}>下一页</button>
        </nav>}
        {loading ? <p className="empty-state">正在加载…</p> : !runs.length ? <p className="empty-state">暂无匹配的运行日志。生成行程或发送消息后刷新。</p> : runs.map((run) => <button key={run.runId} className={`log-run ${selectedId === run.runId ? "selected" : ""}`} onClick={() => selectRun(run.runId)}>
          <span className="log-run-top"><span className={`log-status ${run.status}`}>{statuses[run.status] || run.status}</span><time>{dateTime(run.startedAt)}</time></span>
          <strong>{String(run.input || "未记录用户输入").slice(0, 120)}</strong><span className="log-run-bottom"><span>{run.model || "未记录模型"} · {duration(run.durationMs)}</span><ChevronRight size={15} /></span><small title={run.jobId || run.sessionId}>{run.source === "generation" ? `任务：${run.jobId}` : run.sessionId}</small>
        </button>)}
      </section>
      <section className="log-detail" aria-label="运行详情">
        {detailLoading ? <p className="empty-state">正在加载运行详情…</p> : detailError ? <p role="alert" className="log-alert">{detailError}</p> : !detail ? <div className="log-placeholder"><Activity size={36} /><h2>选择一次运行</h2><p>查看完整事件时间线与请求详情</p></div> : <>
          <div className="log-detail-heading"><div><span className={`log-status ${detail.run.status}`}>{statuses[detail.run.status] || detail.run.status}</span><h2>{detail.run.model || "历史会话"}</h2></div><code title={selectedId}>{selectedId}</code></div>
          {detail.run.planId && <Link className="back-link" to={`/plans/${encodeURIComponent(detail.run.planId)}/planning`}>查看关联行程 <ChevronRight size={15} /></Link>}
          <div className="log-metrics"><div><Clock3 size={16} /><span>总耗时</span><strong>{duration(detail.run.durationMs)}</strong></div><div><span>模型请求</span><strong>{events.filter((event) => event.type === "model_request").length} 次</strong></div><div><span>输入 / 输出 Token</span><strong>{inputTokens.toLocaleString()} / {outputTokens.toLocaleString()}</strong></div></div>
          {(detail.truncated || detail.run.status === "legacy") && <p className="log-notice">{detail.run.status === "legacy" ? "历史日志按会话合并，无法还原未记录的请求上下文。" : "读取范围有限，较早的事件可能已被截断。"}</p>}
          <div className="log-event-filters"><label>事件类型<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">全部事件</option><option value="model">模型 / API</option><option value="tool">工具执行</option><option value="error">异常</option></select></label><label>搜索详情<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="工具名、关键词、错误…" /></label><span>{visible.length} 条事件</span></div>
          {searchError && <p role="alert">{searchError}</p>}
          {search.trim() && !matches && !searchError && <p role="status">正在搜索日志正文…</p>}
          <ol className="log-timeline">{visible.map(event => <li key={event.eventId} className={isError(event) ? "error" : ""}>
            <div className="log-event-time"><time>{new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour12: false })}</time><small>+{((new Date(event.timestamp) - new Date(detail.run.startedAt)) / 1000).toFixed(2)}s</small></div>
            <LogEvent event={event} load={eventId => api(`${detailPath}/events/${eventId}?${detailQuery}`)} />
          </li>)}</ol>{!visible.length && <p className="empty-state">没有匹配的事件。</p>}
        </>}
      </section>
    </div>
  </div>;
}
