import React, { useEffect, useRef, useState } from 'react';

export default function ReviewPlaceDialog({ item, replace, planId, api, onClose, onApply }) {
  const dialog = useRef(null);
  const input = useRef(null);
  const [query, setQuery] = useState('');
  const [place, setPlace] = useState(null);
  const [results, setResults] = useState([]);
  const [status, setStatus] = useState('');
  const [category, setCategory] = useState(replace ? 'hotel' : '');
  const [tips, setTips] = useState(replace ? item.visit_tips || '' : '');

  useEffect(() => { dialog.current.showModal(); }, []);
  useEffect(() => {
    if (place || !query.trim()) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setStatus('正在搜索…');
      try {
        const data = await api(`/api/plans/${encodeURIComponent(planId)}/planning/places/search`, {
          method: 'POST', body: JSON.stringify({ query: query.trim() }), signal: controller.signal, timeoutMs: 12000,
        });
        if (controller.signal.aborted) return;
        const candidates = data.places.filter(x => x.place_id && x.name && Number.isFinite(x.latitude) && Number.isFinite(x.longitude));
        setResults(candidates);
        setStatus(candidates.length ? '' : '没有找到地点，试试完整名称或地址。');
      } catch {
        if (!controller.signal.aborted) setStatus('搜索失败，请检查连接后重试。');
      }
    }, 350);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [api, planId, place, query]);

  function submit(event) {
    event.preventDefault();
    if (!place || !category) return;
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    onApply({
      item_id: item.item_id, activity_id: `manual-${id}`,
      replace, category, poi_id: place.place_id, place, visit_tips: tips,
      ...(category === 'attraction' && { duration_minutes: Number(new FormData(event.currentTarget).get('duration')) }),
    });
  }

  return <dialog ref={dialog} className="review-place-dialog" aria-labelledby="review-place-title"
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <h2 id="review-place-title">{replace ? item.lodging_pending ? '选择住宿' : '修改酒店' : '添加行程'}</h2>
    {!replace && <p className="guided-note">添加到「{item.title}」之后</p>}
    <form onSubmit={submit}>
      <label>搜索地点<input ref={input} type="search" value={query} autoFocus autoComplete="off" maxLength={100}
        onChange={event => { setQuery(event.target.value); setPlace(null); setResults([]); setStatus(''); }}
        onKeyDown={event => {
          if (event.key === 'ArrowDown' && results.length) {
            event.preventDefault();
            dialog.current.querySelector('.review-place-results button')?.focus();
          }
        }} /></label>
      {status && <p className="guided-note" role="status">{status}</p>}
      {!place && results.length > 0 && <ul className="review-place-results" aria-label="地点搜索建议">{results.map(result =>
        <li key={result.place_id}><button type="button" onClick={() => {
          setPlace(result); setQuery(result.name); setResults([]);
          input.current.focus();
        }}><strong>{result.name}</strong><small>{result.address}</small></button></li>)}</ul>}
      {place && <><p className="guided-note">已选择 {place.name} · {place.address}</p>
        {!replace && <label>行程类型<select value={category} required onChange={event => setCategory(event.target.value)}>
          <option value="">请选择</option><option value="hotel">住宿</option><option value="attraction">景点</option><option value="food">餐厅</option>
        </select></label>}
        {category === 'attraction' && <label>停留时间（分钟）<input name="duration" type="number" min="30" max="600" step="1" defaultValue="90" required /></label>}
        {category && <label>注意事项<textarea rows={3} maxLength={500} value={tips} onChange={event => setTips(event.target.value)} /></label>}
      </>}
      <div className="guided-actions"><button type="button" onClick={onClose}>取消</button>
        <button className="primary-button" disabled={!place || !category}>{replace ? '应用住宿' : '添加到行程'}</button></div>
    </form>
  </dialog>;
}
