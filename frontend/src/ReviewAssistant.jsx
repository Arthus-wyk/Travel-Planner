import React, { useEffect, useRef, useState } from "react";
import { ArrowUp, History, MessageCircle, Plus, X } from "lucide-react";
import { Link } from "react-router-dom";
import PlacePhoto from "./PlacePhoto";

// ============渲染审核交互内容 ============
function ReviewBlock({ block, disabled, editable, send }) {
  const [custom, setCustom] = useState("");
  const [multiple, setMultiple] = useState(block.selection_mode === "multiple");
  const [chosen, setChosen] = useState([]);
  const action = { action_id: block.action_id };
  if (block.type === "choice") {
    return <div className="review-assistant-options">
      <p>{block.question}</p>
      {block.options.map((option, index) => <button type="button" key={option} disabled={disabled}
        onClick={() => send({ ...action, option_index: index }, option)}>{option}</button>)}
      {block.allow_custom && <form onSubmit={event => {
        event.preventDefault();
        if (custom.trim()) send({ ...action, custom_text: custom.trim() }, custom.trim());
      }}>
        <label>自定义答复 <input value={custom} onChange={event => setCustom(event.target.value)} /></label>
        <button disabled={disabled || !custom.trim()}>提交</button>
      </form>}
    </div>;
  }
  if (block.type === "place_candidates") {
    const selectable = editable && Boolean(block.action_id);
    const count = block.selection_count;
    const groups = block.groups || [];
    const selectedIds = new Set(chosen.map(index => block.places[index].place_id));
    const validGroups = groups.every(group => {
      const selectedCount = group.place_ids.filter(id => selectedIds.has(id)).length;
      return group.selection_count == null ? selectedCount > 0 : selectedCount === group.selection_count;
    });
    // ============提交已选地点 ============
    function selectPlaces(indexes) {
      send({ ...action, option_indexes: indexes }, indexes.map(index => block.places[index].name).join("、"));
    }
    return <div>
      {groups.map(group => <p key={group.id}>{group.label}：
        {group.selection_count == null ? "请选择地点" : `请选择 ${group.selection_count} 个`}
        （{group.place_ids.filter(id => selectedIds.has(id)).length} 已选）</p>)}
      {selectable && block.selection_mode === "either" && <label>
        <input type="checkbox" checked={multiple} disabled={disabled}
          onChange={event => { setMultiple(event.target.checked); setChosen([]); }} /> 多选
      </label>}
      <div className="review-assistant-cards">
      {block.places.map((place, index) => <article key={place.action_id || place.place_id}>
        <PlacePhoto item={{ category: "attraction", place }} />
        <h3>{place.name}</h3><p>{place.address}</p>
        {groups.filter(group => group.place_ids.includes(place.place_id)).map(group =>
          <p key={group.id}>{group.label}</p>)}
        {selectable && (multiple ? <label>
          <input type="checkbox" checked={chosen.includes(index)}
            disabled={disabled || (count != null && chosen.length >= count && !chosen.includes(index))}
            onChange={event => setChosen(current => event.target.checked ? [...current, index] : current.filter(item => item !== index))} />
          选择 {place.name}
        </label> : <button type="button" disabled={disabled} onClick={() => selectPlaces([index])}>加入行程</button>)}
      </article>)}
      </div>
      {selectable && multiple && <button type="button"
        disabled={disabled || !chosen.length || !validGroups || (count != null && chosen.length !== count)}
        onClick={() => selectPlaces(chosen)}>确认选择（{chosen.length}{count ? `/${count}` : ""}）</button>}
    </div>;
  }
  if (block.type === "change_preview") {
    return <div className="review-assistant-options">
      <p>{block.operation_count ? `拟修改 ${block.operation_count} 项` : "查看行程调整"}</p>
      <details><summary>查看试算后的日程</summary>
        <ul>{block.itinerary.map((item, index) => <li key={index}>第 {item.day} 天 {item.time_of_day} {item.title}</li>)}</ul>
      </details>
      {(block.audit?.issues || []).map((issue, index) => <p key={index}>{issue.message}</p>)}
      {editable && <button type="button" disabled={disabled} onClick={() => send(action, "确认并保存")}>确认并保存</button>}
    </div>;
  }
  if (block.type === "review_issues") {
    return <ul>{block.issues.map((issue, index) => <li key={index}>{issue.message}</li>)}</ul>;
  }
  return null;
}

// ============读取当前行程的对话历史 ============
function loadChats(key) {
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (Array.isArray(saved?.chats) && saved.chats.every(chat =>
      typeof chat?.id === "string" && Array.isArray(chat.messages) && chat.messages.length
      && chat.messages.every(item => typeof item?.message === "string"
        && (item.role === "user" || item.role === "assistant")))) return saved;
  } catch { /* 浏览器存储不可用时仍可在当前页面聊天。 */ }
  return { activeId: null, chats: [] };
}

// ============展开浮动聊天与管理历史 ============
export default function ReviewAssistant({ planId, editable, disabled, onTurn }) {
  const key = `travel-planner:review-chat:${planId}`;
  const [saved, setSaved] = useState(() => loadChats(key));
  const [open, setOpen] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [message, setMessage] = useState("");
  const [pendingId, setPendingId] = useState(null);
  const [progress, setProgress] = useState("");
  const [storageError, setStorageError] = useState(false);
  const [choiceError, setChoiceError] = useState("");
  const requestBusy = useRef(false);
  const launcher = useRef(null);
  const composer = useRef(null);
  const scroll = useRef(null);
  const active = saved.chats.find(chat => chat.id === saved.activeId);
  const messages = active?.messages ?? [];
  const pendingBlock = messages.at(-1)?.blocks?.find(block => block.action_id);

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(saved));
      setStorageError(false);
    } catch {
      setStorageError(true);
    }
  }, [key, saved]);

  useEffect(() => { if (open && !showHistory) composer.current?.focus(); }, [open, showHistory, saved.activeId]);
  useEffect(() => {
    if (open && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [open, showHistory, saved.activeId, messages.length, pendingId, progress]);

  // ============切换到新对话或历史对话 ============
  function openChat(id) {
    setSaved(current => ({ ...current, activeId: id }));
    setMessage(""); setChoiceError(""); setShowHistory(false);
  }

  // ============收起窗口并归还键盘焦点 ============
  function closeChat() {
    setOpen(false);
    launcher.current?.focus();
  }

  // ============将消息追加到发起请求的对话 ============
  function append(id, entry, activate = false) {
    setSaved(current => {
      const existing = current.chats.find(chat => chat.id === id);
      const chat = { id, messages: [...(existing?.messages ?? []), entry] };
      return { activeId: activate ? id : current.activeId,
        chats: [chat, ...current.chats.filter(item => item.id !== id)] };
    });
  }

  // ============提交消息或交互按钮并保存回复 ============
  async function send(payload, label) {
    if (requestBusy.current || disabled || !label) return;
    const id = active?.id ?? (crypto.randomUUID?.() ?? crypto.getRandomValues(new Uint32Array(4)).join("-"));
    requestBusy.current = true;
    setPendingId(id);
    setProgress("正在连接路线助手…");
    setChoiceError("");
    if (!payload.action_id) append(id, { role: "user", message: label }, true);
    if (payload.message) setMessage("");
    try {
      const history = messages.filter(item => !item.error && item.message).slice(-12)
        .map(item => ({ role: item.role, content: item.message.slice(0, 1200) }));
      const result = await onTurn({ ...payload, history, conversation_id: id }, setProgress);
      if (payload.action_id && (result.status === "failed" || result.status === "save_unknown")) {
        const issues = (result.blocks ?? []).flatMap(block =>
          block.type === "review_issues" ? block.issues.map(issue => issue.message) : []);
        setChoiceError([result.message || "操作未完成，请稍后重试。", ...issues].join(" "));
        return;
      }
      if (payload.action_id) append(id, { role: "user", message: label });
      append(id, { role: "assistant", message: result.message ?? "", blocks: result.blocks, runId: result.run_id });
    } catch (cause) {
      if (payload.action_id) setChoiceError(cause.message || "提交未完成，请重试。");
      else append(id, { role: "assistant", message: cause.message || "请求失败，请重试。", error: true });
    } finally {
      requestBusy.current = false;
      setPendingId(null);
      setProgress("");
    }
  }

  return <div className="review-chat-dock">
    {open && <section id="review-chat-panel" className="review-assistant" aria-labelledby="review-assistant-title"
      onKeyDown={event => { if (event.key === "Escape") closeChat(); }}>
      <header className="review-chat-header">
        <div className="review-chat-heading"><MessageCircle size={18} aria-hidden="true" />
          <h2 id="review-assistant-title">路线助手</h2></div>
        <button type="button" className="review-chat-icon" aria-label="历史对话" aria-pressed={showHistory}
          title="历史对话" onClick={() => setShowHistory(current => !current)}><History size={17} /></button>
        <button type="button" className="review-chat-icon" aria-label="新对话" title="新对话"
          onClick={() => openChat(null)}><Plus size={18} /></button>
        <button type="button" className="review-chat-icon" aria-label="关闭聊天" title="关闭聊天"
          onClick={closeChat}><X size={18} /></button>
      </header>
      {showHistory ? <nav className="review-chat-history" aria-label="历史对话">
        <h3>历史对话</h3>
        {!saved.chats.length && <p>暂无历史对话</p>}
        {saved.chats.map(chat => <button type="button" key={chat.id}
          aria-current={chat.id === saved.activeId ? "true" : undefined}
          onClick={() => openChat(chat.id)}>{chat.messages[0].message}</button>)}
      </nav> : <>
        <div className="review-chat-messages" ref={scroll} role="log" aria-label="聊天消息">
          {!messages.length && <div className="review-chat-empty">
            <h3>{editable ? "想怎么调整路线？" : "想了解这条路线？"}</h3>
            <p>{editable ? "可以请我检查行程、找景点，或试算修改。" : "行程已确认，可以查询路线和费用。"}</p>
          </div>}
          {messages.map((turn, index) => <div key={index} className={`review-chat-message is-${turn.role}`}>
            {turn.message && <p role={turn.error ? "alert" : undefined}>{turn.message}</p>}
            {turn.runId && <Link className="log-message-link"
              to={`/logs?run_id=${encodeURIComponent(turn.runId)}`}>查看运行日志</Link>}
            {(turn.blocks || []).filter(block => !block.action_id).map((block, blockIndex) => <ReviewBlock key={blockIndex} block={block}
              disabled={disabled || Boolean(pendingId) || index !== messages.length - 1} editable={editable} send={send} />)}
          </div>)}
          {pendingId === saved.activeId && pendingId && <p className="review-chat-status" role="status">{progress}</p>}
        </div>
        {pendingBlock && <div className="review-chat-choices" aria-label="等待选择">
          <ReviewBlock key={pendingBlock.action_id} block={pendingBlock} editable={editable}
            disabled={disabled || Boolean(pendingId)} send={send} />
          {choiceError && <p role="alert">{choiceError}</p>}
        </div>}
        <form className="review-chat-composer" onSubmit={event => {
          event.preventDefault();
          const text = message.trim();
          if (text) send({ message: text }, text);
        }}>
          <textarea ref={composer} aria-label="输入路线问题" placeholder="输入路线问题…" rows={2} maxLength={4000}
            value={message} onChange={event => setMessage(event.target.value)} onKeyDown={event => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                event.preventDefault();
                event.currentTarget.form.requestSubmit();
              }
            }} />
          <button type="submit" aria-label="发送" disabled={disabled || Boolean(pendingId) || !message.trim()}><ArrowUp size={19} /></button>
        </form>
        {storageError && <p className="review-chat-status" role="alert">浏览器无法保存对话，刷新后可能丢失。</p>}
      </>}
    </section>}
    <button ref={launcher} type="button" className="review-chat-launcher" aria-expanded={open}
      aria-controls={open ? "review-chat-panel" : undefined} onClick={() => open ? closeChat() : setOpen(true)}>
      {open ? <X size={22} aria-hidden="true" /> : <MessageCircle size={21} aria-hidden="true" />}
      <span>{open ? "收起聊天" : "路线助手"}</span>
    </button>
  </div>;
}
