import React, { useEffect, useRef, useState } from 'react';

let loading;
function loadMap() {
  if (window.AMap) return Promise.resolve(window.AMap);
  if (loading) return loading;
  const key = import.meta.env.VITE_AMAP_JS_KEY;
  if (!key) return Promise.reject(new Error('请配置 VITE_AMAP_JS_KEY 和高德 JS API 安全密钥后重启前端。'));
  window._AMapSecurityConfig = import.meta.env.VITE_AMAP_SERVICE_HOST
    ? { serviceHost: import.meta.env.VITE_AMAP_SERVICE_HOST }
    : { securityJsCode: import.meta.env.VITE_AMAP_SECURITY_CODE };
  loading = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}&plugin=AMap.ToolBar,AMap.Scale`;
    script.onload = () => window.AMap ? resolve(window.AMap) : reject(new Error('高德地图加载失败，请检查 JS API 配置。'));
    script.onerror = () => { script.remove(); reject(new Error('高德地图加载失败，请检查网络后重试。')); };
    document.head.appendChild(script);
  }).catch(error => { loading = null; throw error; });
  return loading;
}

export default function RouteMap({ data }) {
  const container = useRef(null);
  const instance = useRef(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    let map;
    setError('');
    loadMap().then(AMap => {
      if (!active) return;
      map = new AMap.Map(container.current, { zoom: 12, resizeEnable: true });
      instance.current = map;
      const overlays = data.points.map((point, index) => new AMap.Marker({ position: point.location.split(',').map(Number), title: `${index + 1}. ${point.title}`, label: { content: String(index + 1), direction: 'top' } }));
      for (const path of data.paths || []) overlays.push(new AMap.Polyline({ path: path.split(';').map(point => point.split(',').map(Number)), strokeColor: '#4f46e5', strokeWeight: 5 }));
      map.add(overlays);
      map.addControl(new AMap.ToolBar());
      map.addControl(new AMap.Scale());
      map.setFitView(null, false, [50, 50, 50, 50], 17);
    }).catch(cause => { if (active) setError(cause.message); });
    return () => { active = false; instance.current = null; map?.destroy(); };
  }, [data, revision]);
  return <><div ref={container} style={{ height: 440, width: '100%' }} aria-label="可拖动和缩放的路线地图" />{error ? <p role="alert">{error} <button onClick={() => setRevision(value => value + 1)}>重试地图</button></p> : <button type="button" onClick={() => instance.current?.setFitView(null, false, [50, 50, 50, 50], 17)}>显示全程</button>}</>;
}
