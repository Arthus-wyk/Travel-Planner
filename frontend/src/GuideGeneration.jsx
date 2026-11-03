import React, { useEffect, useRef, useState } from "react";
import { ArrowUp, Paperclip, X, Compass, Check, ChevronDown, RotateCcw, CircleAlert } from "lucide-react";
import { watchGenerationSocket } from "./generationSocket";

const steps = ["检查旅行需求", "识别攻略地点", "查找真实地点并匹配候选", "整理住宿与地点", "准备每日安排", "安排每日行程", "计算路线", "检查时间和预算"];
const stages = { queued: "等待生成", starting: "准备生成", coordinator: steps[0], generate_guide: steps[1], activity_agent: steps[1], search_places: steps[2], place_match_agent: steps[2], default_choices: steps[3], prepare_draft: steps[4], itinerary_agent: steps[5], route_agent: steps[6], schedule_agent: steps[6], audit_agent: steps[7] };

export default function GuideGeneration({ api, planId, text, onText, snapshot, onComplete, onManual, manualAvailable, disabled, onRunning }) {
  const [connection, setConnection] = useState("connecting");
  const [elapsed, setElapsed] = useState(0);
  const [file, setFile] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [retry, setRetry] = useState(0);
  const submittingRef = useRef(false);
  const fileInput = useRef(null);
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
  const current = steps.indexOf(stages[job?.stage]);
  const canGenerate = !active && !disabled && (file || text.trim()) && text.length <= 20000;
  return <section className="guide-studio" aria-labelledby="guide-generation-title">
    <header className="guide-studio-heading"><span className="guide-emblem"><Compass size={26} strokeWidth={1.5} /></span>
      <h2 id="guide-generation-title">根据攻略生成每日行程</h2>
      <p>把收藏的攻略交给我，一起安排下一程。</p>
    </header>
    <div className={`guide-composer ${active ? "is-working" : ""}`}>
      <label className="guide-input-label" htmlFor="guide-input">攻略文字</label>
      <textarea id="guide-input" rows="5" disabled={active || disabled || Boolean(file)} value={text} onChange={event => onText(event.target.value)} placeholder={file ? "已添加攻略图片，将优先识别图片中的内容" : "粘贴你的旅行攻略…\n想去的地点、收藏的小店，或一段随手记下的旅行灵感。"} />
      {file && <div className="guide-attachment"><Paperclip size={15} /><span>{file.name}</span><button type="button" aria-label="移除图片" disabled={active || disabled} onClick={() => { setFile(null); fileInput.current.value = ""; }}><X size={15} /></button></div>}
      <div className="guide-composer-toolbar">
        <input ref={fileInput} className="guide-file-input" type="file" aria-label="攻略图片" accept="image/*" disabled={active || disabled} onChange={event => {
          const next = event.target.files?.[0];
          if (next && (!next.type.startsWith("image/") || next.size > 10 * 1024 * 1024)) { setError("请选择不超过 10 MB 的图片"); event.target.value = ""; return; }
          setFile(next || null);
        }} />
        <button className="guide-upload-button" type="button" disabled={active || disabled} onClick={() => fileInput.current.click()}><Paperclip size={17} />添加攻略图片</button>
        <button className="guide-send" type="button" disabled={!canGenerate} onClick={generate} aria-label={failed || needsInput ? "重新识别并生成" : "生成每日行程"}>{active ? <span className="generation-spinner" /> : <ArrowUp size={20} />}<span>{active ? "生成中" : failed || needsInput ? "重新生成" : "生成行程"}</span></button>
      </div>
    </div>
    <p className="guide-composer-hint">支持文字或图片（最大 10 MB） · 自动匹配地点、安排路线与核对预算</p>
    {text.length > 20000 && <p role="alert" className="guided-note">攻略文字超过 20,000 字，请精简后再生成。</p>}
    {disabled && !active && <p className="guided-note">请先处理地点问题或等待当前修改完成。</p>}
    {(active || error) && <div className={`guide-response ${error && !active ? "has-error" : ""}`}>
      <span className="guide-response-avatar">{error && !active ? <CircleAlert size={19} /> : <Compass size={20} />}</span>
      <div className="guide-response-body">
        {active && <><div className="guide-status" role="status" aria-live="polite"><strong className="guide-thinking">{submitting ? "正在提交攻略" : stages[job?.stage] || "正在处理攻略"}</strong><span className="guide-pulse" aria-hidden="true" /></div>
          <p className="guide-status-meta">已用时 {elapsed} 秒{connection !== "connected" && " · 正在恢复连接，后台继续执行"}</p>
          <details className="guide-process"><summary>查看执行过程<ChevronDown size={14} /></summary>
            <ol>{steps.map((label, index) => <li key={label} className={index < current ? "done" : index === current ? "current" : ""} aria-current={index === current ? "step" : undefined}><span className="guide-step-marker">{index < current ? <Check size={12} /> : index + 1}</span>{label}{index === current && <span className="guide-step-state">处理中</span>}</li>)}</ol>
          </details><p className="guide-response-note">可以刷新页面，回来后继续查看进度。</p></>}
        {error && !active && <div role="alert"><strong>{needsInput ? "还需要确认几个地点" : "这次生成未能完成"}</strong><p>{needsInput ? "请修改攻略中的地点描述后重试，也可以用已有地点继续规划。" : "你的攻略还在，原行程不会因本次失败被覆盖。可以重新尝试生成。"}</p>
          <details className="guide-error-detail"><summary>查看错误详情</summary><p>{error}</p>{job?.job_id && <small>任务编号：{job.job_id}</small>}</details>
          <div className="guide-recovery"><button type="button" disabled={!canGenerate} onClick={generate}><RotateCcw size={14} />重新生成</button>{needsInput && <button type="button" disabled={!manualAvailable || disabled} onClick={onManual}>用已有地点继续规划</button>}</div>
        </div>}
      </div>
    </div>}
    {job?.status === "completed" && !active && <button className="guide-complete" onClick={() => callbacks.current.onComplete()}><Check size={16} />行程已生成，打开每日行程</button>}
  </section>;
}
