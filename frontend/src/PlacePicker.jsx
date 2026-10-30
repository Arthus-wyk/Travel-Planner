import React, { useEffect, useState } from "react";
import { apiUrl } from "./apiUrl";

const statusLabels = { model_matched: "已根据攻略匹配位置", auto_confirmed: "已自动确认位置", manual_confirmed: "已手动确认位置", ambiguous: "位置待核对：请选择具体分店", failed: "定位失败", unresolved: "位置待确认" };

export default function PlacePicker({ item, planId, api, busy, onResolve }) {
  const [query, setQuery] = useState(item.name);
  const [results, setResults] = useState(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [imageFailed, setImageFailed] = useState(false);
  const [imageRetry, setImageRetry] = useState(0);
  useEffect(() => { setImageFailed(false); }, [item.place?.image_cache_key]);
  const candidates = results ?? item.place_candidates ?? [];
  const status = item.place_status || (item.place ? "manual_confirmed" : "unresolved");
  async function search(event) {
    event.preventDefault();
    if (busy || searching) return;
    setSearching(true); setError("");
    try {
      const data = await api(`/api/plans/${encodeURIComponent(planId)}/planning/places/search`, { method: "POST", body: JSON.stringify({ query }) });
      setResults(data.places || []);
    } catch (cause) { setError(cause.message); }
    finally { setSearching(false); }
  }
  function candidateList(places) {
    return <div className="poi-results">{places.map((place) => <div key={place.place_id}><span><strong>{place.name}</strong><small>{place.address}</small></span><button type="button" disabled={busy || searching} onClick={async () => { if (await onResolve(place.place_id)) setResults(null); }}>确认位置</button></div>)}</div>;
  }
  return <div className="place-picker" onChange={event => event.stopPropagation()}>
    <p className={`place-status place-status-${status}`} role="status">{statusLabels[status] || statusLabels.unresolved}</p>
    {item.place && <div className="place-confirmed">
      {item.place.image_cache_key && !imageFailed && <img key={`${item.place.image_cache_key}-${imageRetry}`} src={apiUrl(`/api/maps/images/${item.place.image_cache_key}`)} alt={`${item.name}的位置地图`} loading="lazy" onError={() => setImageFailed(true)} />}
      <div><strong>{item.place.name || item.name}</strong><p>{item.place.address || item.location || "地址待补充"}</p><small>GCJ-02 · {item.place.longitude}, {item.place.latitude}</small>
      {(!item.place.image_cache_key || imageFailed) && <p className="guided-note">地图图片暂不可用，已保留确认的位置与地址。</p>}
      {imageFailed && <button type="button" disabled={busy} onClick={() => { setImageFailed(false); setImageRetry((count) => count + 1); }}>重试地图图片</button>}
      {!item.place.image_cache_key && item.place.place_id && <button type="button" disabled={busy} onClick={() => onResolve(item.place.place_id)}>重新获取地图</button>}</div></div>}
    {item.place_error && <p className="guided-note">{item.place_error}</p>}
    {status === "ambiguous" && results === null && candidateList(candidates)}
    <details open={!item.place || status === "ambiguous" || status === "failed"}><summary>{item.place ? "手动重新定位" : "搜索 POI，确定具体位置"}</summary>
      <form className="poi-search" onSubmit={search}><input aria-label={`搜索${item.name}的具体位置`} value={query} onChange={(e) => setQuery(e.target.value)} maxLength={100} required disabled={busy || searching} /><button disabled={busy || searching}>{searching ? "搜索中…" : "搜索地点"}</button></form>
      {error && <p className="guided-error" role="alert">{error}</p>}
      {results?.length === 0 && <p className="guided-note">未找到地点，请加上城市、区县或更具体的名称。</p>}
      {results !== null && candidateList(results)}
    </details>
  </div>;
}
