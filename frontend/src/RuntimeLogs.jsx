import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Activity, ArrowLeft, ChevronRight, Clock3, RefreshCw, Search } from "lucide-react";
import "./runtime-logs.css";

const statuses = { completed: "已完成", failed: "失败", cancelled: "已取消", interrupted: "已中断", limit_reached: "达到轮次上限", running: "运行中 / 未收尾", legacy: "历史日志" };
const titles = {
  run_start: "开始运行", run_end: "运行结束", model_request: "模型请求", model_response: "模型响应", model_error: "模型请求失败",
  message: "消息", tool_use: "调用工具", tool_result: "工具结果", intent_understanding: "意图分析",
  no_tool_guard: "工具调用护栏", memory_extraction: "记忆提取", api_summary: "Token 汇总",
  tengu_api_query: "API 请求", tengu_api_success: "API 成功", tengu_api_error: "API 异常 / 重试",
};
const dateTime = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const duration = (value) => value == null ? "—" : `${(value / 1000).toFixed(2)} 秒`;

function preview(event) {
  const value = event.input ?? event.content ?? event.answer ?? event.result ?? event.assistant_content ?? (typeof event.message === "object" ? event.message.content : event.message);
  return typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
}

function isError(event) {
  return event.type === "model_error" || event.type === "tengu_api_error" ||
    (event.type === "run_end" && event.status !== "completed") ||
    (event.type === "tool_result" && /"success"\s*:\s*false|"status"\s*:\s*"failed"/.test(event.result || ""));
}

export default function RuntimeLogs({ api }) {
  const [params, setParams] = useSearchParams();
  const conversationId = params.get("conversation_id") || "";
  const selectedId = params.get("run_id") || "";
  const status = params.get("status") || "";
  const since = params.get("since") || "";
  const until = params.get("until") || "";
  const [runs, setRuns] = useState([]);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [truncated, setTruncated] = useState(false);
  const [revision, setRevision] = useState(0);
  const [category, setCategory] = useState("all");
  const [search, setSearch] = useState("");
  const query = new URLSearchParams({ conversation_id: conversationId, status, since, until }).toString();

  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    api(`/api/logs/runs?${query}`).then((data) => {
      if (active) { setRuns(data.runs); setTruncated(data.truncated); }
    }).catch((cause) => { if (active) { setError(cause.message); setRuns([]); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [query, revision, api]);

  useEffect(() => {
    let active = true;
    setDetail(null); setDetailError(""); setCategory("all"); setSearch("");
    if (!selectedId) { setDetailLoading(false); return; }
    setDetailLoading(true);
    api(`/api/logs/runs/${encodeURIComponent(selectedId)}?conversation_id=${encodeURIComponent(conversationId)}`).then((data) => {
      if (active) setDetail(data);
    }).catch((cause) => { if (active) setDetailError(cause.message); })
      .finally(() => { if (active) setDetailLoading(false); });
    return () => { active = false; };
  }, [selectedId, conversationId, revision, api]);

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

  const events = detail?.events || [];
  const responses = events.filter((event) => event.type === "model_response");
  const inputTokens = responses.reduce((sum, event) => sum + (event.usage?.inputTokens || 0), 0);
  const outputTokens = responses.reduce((sum, event) => sum + (event.usage?.outputTokens || 0), 0);
  const visible = events.filter((event) => {
    const matchesCategory = category === "all" || (category === "model" ? /^(model_|tengu_api_|api_summary)/.test(event.type) : category === "tool" ? event.type.startsWith("tool_") : isError(event));
    return matchesCategory && (!search || JSON.stringify(event).toLowerCase().includes(search.toLowerCase()));
  });

  return <div className="logs-page">
    <header className="page-header"><Activity size={24} /><h1>模型运行日志</h1><button className="log-refresh" onClick={() => setRevision((value) => value + 1)} disabled={loading || detailLoading}><RefreshCw size={16} />刷新</button></header>
    <p className="logs-intro">从最终回答追溯模型上下文、请求重试和工具执行。日志包含对话内容，仅供本机诊断。</p>
    {conversationId && <Link className="back-link" to={`/conversations/${encodeURIComponent(conversationId)}`}><ArrowLeft size={15} /> 返回对话</Link>}
    <form key={query} className="log-filters" onSubmit={applyFilters}>
      <label>会话 ID<input name="conversation_id" defaultValue={conversationId} placeholder="全部会话" /></label>
      <label>运行状态<select name="status" defaultValue={status}><option value="">全部状态</option>{Object.entries(statuses).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
      <label>起始日期（UTC）<input type="date" name="since" defaultValue={since} /></label>
      <label>结束日期（UTC）<input type="date" name="until" defaultValue={until} /></label>
      <button className="primary-button small" type="submit"><Search size={15} />筛选</button>
    </form>
    {error && <p role="alert" className="log-alert">{error}</p>}
    {truncated && <p className="log-notice">当前显示最近 100 个会话、每会话末尾 5000 条事件，最多 200 次运行。可按会话缩小范围。</p>}
    <div className="logs-layout">
      <section className="log-run-list" aria-label="运行列表"><div className="log-list-heading"><strong>运行记录</strong><span>{runs.length} 次</span></div>
        {loading ? <p className="empty-state">正在加载…</p> : !runs.length ? <p className="empty-state">暂无匹配的运行日志。发送一条新消息后刷新。</p> : runs.map((run) => <button key={run.runId} className={`log-run ${selectedId === run.runId ? "selected" : ""}`} onClick={() => selectRun(run.runId)}>
          <span className="log-run-top"><span className={`log-status ${run.status}`}>{statuses[run.status] || run.status}</span><time>{dateTime(run.startedAt)}</time></span>
          <strong>{String(run.input || "未记录用户输入").slice(0, 120)}</strong><span className="log-run-bottom"><span>{run.model || "旧日志"} · {duration(run.durationMs)}</span><ChevronRight size={15} /></span><small title={run.sessionId}>{run.sessionId}</small>
        </button>)}
      </section>
      <section className="log-detail" aria-label="运行详情">
        {detailLoading ? <p className="empty-state">正在加载运行详情…</p> : detailError ? <p role="alert" className="log-alert">{detailError}</p> : !detail ? <div className="log-placeholder"><Activity size={36} /><h2>选择一次运行</h2><p>查看完整事件时间线与请求详情</p></div> : <>
          <div className="log-detail-heading"><div><span className={`log-status ${detail.run.status}`}>{statuses[detail.run.status] || detail.run.status}</span><h2>{detail.run.model || "历史会话"}</h2></div><code title={selectedId}>{selectedId}</code></div>
          <div className="log-metrics"><div><Clock3 size={16} /><span>总耗时</span><strong>{duration(detail.run.durationMs)}</strong></div><div><span>模型请求</span><strong>{events.filter((event) => event.type === "model_request").length} 次</strong></div><div><span>输入 / 输出 Token</span><strong>{inputTokens.toLocaleString()} / {outputTokens.toLocaleString()}</strong></div></div>
          {(detail.truncated || detail.run.status === "legacy") && <p className="log-notice">{detail.run.status === "legacy" ? "历史日志按会话合并，无法还原未记录的请求上下文。" : "读取范围有限，较早的事件可能已被截断。"}</p>}
          <div className="log-event-filters"><label>事件类型<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">全部事件</option><option value="model">模型 / API</option><option value="tool">工具执行</option><option value="error">异常</option></select></label><label>搜索详情<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="工具名、关键词、错误…" /></label><span>{visible.length} 条事件</span></div>
          <ol className="log-timeline">{visible.map((event, index) => <li key={`${event.timestamp}-${index}`} className={isError(event) ? "error" : ""}>
            <div className="log-event-time"><time>{new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour12: false })}</time><small>+{((new Date(event.timestamp) - new Date(detail.run.startedAt)) / 1000).toFixed(2)}s</small></div>
            <details className="log-event"><summary><span><strong>{titles[event.type] || event.type}</strong>{event.tool && <code>{event.tool}</code>}{event.attempt != null && <small>尝试 {event.attempt}</small>}</span><small>{event.durationMs != null ? duration(event.durationMs) : "展开详情"}</small></summary><p className="log-event-preview">{preview(event).slice(0, 240)}</p><pre>{JSON.stringify(event, null, 2)}</pre></details>
          </li>)}</ol>{!visible.length && <p className="empty-state">没有匹配的事件。</p>}
        </>}
      </section>
    </div>
  </div>;
}
