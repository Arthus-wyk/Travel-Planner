import { apiUrl } from './apiUrl.js';

export function watchGenerationSocket(planId, onJob, onConnection) {
  let stopped = false, socket, timer, attempts = 0;
  function connect() {
    if (stopped) return;
    onConnection('connecting');
    socket = new WebSocket(apiUrl(`/api/plans/${encodeURIComponent(planId)}/generation/ws`).replace(/^http/, 'ws'));
    socket.onopen = () => { attempts = 0; onConnection('connected'); };
    socket.onmessage = event => {
      try { const data = JSON.parse(event.data); if (data.type === 'generation_status') onJob(data.job); }
      catch { onConnection('invalid'); }
    };
    socket.onclose = () => {
      if (!stopped) { onConnection('reconnecting'); timer = setTimeout(connect, Math.min(1000 * 2 ** attempts++, 10000)); }
    };
    socket.onerror = () => socket.close();
  }
  connect();
  return () => { stopped = true; clearTimeout(timer); socket?.close(); };
}
