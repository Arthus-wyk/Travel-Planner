import React, { useEffect, useRef, useState } from "react";
import { watchGenerationSocket } from "./generationSocket";

const steps = ["检查旅行需求", "识别攻略地点", "查找真实地点并匹配候选", "整理住宿与地点", "准备每日安排", "安排每日行程", "计算路线", "检查时间和预算"];
const stages = { queued: "等待生成", starting: "准备生成", coordinator: steps[0], activity_agent: steps[1], place_match_agent: steps[2], default_choices: steps[3], prepare_draft: steps[4], itinerary_agent: steps[5], route_agent: steps[6], audit_agent: steps[7] };

export default function GuideGeneration({ api, planId, text, onText, snapshot, onComplete, onManual, manualAvailable, disabled, onRunning }) {
  const [connection, setConnection] = useState("connecting");
  const [elapsed, setElapsed] = useState(0);
  const [file, setFile] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [retry, setRetry] = useState(0);
  const submittingRef = useRef(false);
  const callbacks = useRef({ onComplete, onRunning });
  callbacks.current = { onComplete, onRunning };
  const active = submitting || ["queued", "running"].includes(job?.status);
  useEffect(() => { callbacks.current.onRunning?.(active); }, [active]);
  useEffect(() => {
    return watchGenerationSocket(planId, next => {
        setJob(next);
        if (next?.status === "completed" && localStorage.getItem(`generation-seen-${planId}`) !== next.job_id) {
          localStorage.setItem(`generation-seen-${planId}`, next.job_id);
          // Only navigate for a task started here or still relevant to this draft.
          callbacks.current.onComplete();
          return;
        }
        setError(["failed", "needs_input"].includes(next?.status) ? next.error : "");
    }, setConnection);
  }, [api, planId, retry]);
  useEffect(() => {
    if (!active) return;
    setElapsed(0);
    const timer = setInterval(() => setElapsed(value => value + 1), 1000);
    return () => clearInterval(timer);
  }, [active]);
  async function generate() {
    if (submittingRef.current || active) return;
    submittingRef.current = true; setSubmitting(true); setError("");
    try {
      const payload = { text, ...snapshot };
      if (file) {
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("图片读取失败")); reader.readAsDataURL(file);
        });
        payload.image_base64 = String(data).split(",")[1];
      }
      await api(`/api/plans/${encodeURIComponent(planId)}/generation`, { method: "POST", body: JSON.stringify(payload) });
      setRetry(x => x + 1);
    } catch (cause) { setError(cause.message); setRetry(x => x + 1); }
    finally { submittingRef.current = false; setSubmitting(false); }
  }
  const failed = job?.status === "failed";
  const needsInput = job?.status === "needs_input";
  return <section className="guided-card activity-import" aria-labelledby="guide-generation-title">
    <h2 id="guide-generation-title">根据攻略生成每日行程</h2><p>贴上攻略文字或上传图片。我们会识别地点、安排每天的停留时间，再检查路线与预算。</p>
    <label>攻略图片<input type="file" accept="image/*" disabled={active || disabled} onChange={event => {
      const next = event.target.files?.[0];
      if (next && (!next.type.startsWith("image/") || next.size > 10 * 1024 * 1024)) { setError("请选择不超过 10 MB 的图片"); event.target.value = ""; return; }
      setFile(next || null);
    }} /></label>
    {file && <p className="guided-note">已选择 {file.name} <button type="button" disabled={active} onClick={() => setFile(null)}>移除图片</button></p>}
    <label>攻略文字<textarea id="guide-input" rows="5" disabled={active || disabled || Boolean(file)} value={text} onChange={event => onText(event.target.value)} placeholder="例如：第一天到达后逛西湖，第二天上午去灵隐寺，午餐在附近吃…" /></label>
    <p className="guided-note">上传图片时会识别图片内容；移除图片后可使用文字。生成失败后保留原行程。</p>
    <button className="primary-button" disabled={active || disabled || (!file && !text.trim()) || text.length > 20000} onClick={generate}>{failed || needsInput ? "重新识别并生成" : "生成每日行程"}</button>
    {disabled && !active && <p className="guided-note">请先处理地点问题或等待当前修改完成。</p>}
    {active && <div className="generation-progress" role="status" aria-live="polite">
      <strong><span className="generation-spinner" />{submitting ? "正在提交攻略" : stages[job?.stage] || "正在处理攻略"}</strong>
      <p>已等待 {elapsed} 秒 · {connection === "connected" ? "实时进度已连接" : "连接恢复中，后台任务继续执行"}</p>
      <ol>{steps.map((label, index) => {
        const current = steps.indexOf(stages[job?.stage]);
        return <li key={label} className={index < current ? "done" : index === current ? "current" : ""}>{index < current ? "✓" : index + 1} · {label}</li>;
      })}</ol><small>可刷新页面继续查看，遇到不明确的地点会保留在草稿中。</small>
      {job?.job_id && <small>任务编号：{job.job_id}</small>}
    </div>}
    {job?.status === "completed" && <button onClick={() => callbacks.current.onComplete()}>打开每日行程</button>}
    {error && <div className="generation-error" role="alert"><strong>{needsInput ? "请核对攻略地点" : "这次没有生成完成"}</strong><p>{error}</p><p>{needsInput ? "可以修改攻略后重新识别，或在下方添加地点后用已有地点继续规划。" : "已完成的步骤会保留。使用相同攻略再次提交，会从上次成功的步骤继续。"}</p>{needsInput && <button type="button" disabled={!manualAvailable || disabled} onClick={onManual}>用已有地点继续规划</button>}<button type="button" onClick={() => setRetry(x => x + 1)}>刷新任务状态</button></div>}
  </section>;
}
