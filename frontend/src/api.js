import { apiUrl } from './apiUrl.js';

// ============读取审核助手的实时进度和最终结果 ============
async function readProgress(response, onProgress, onActivity) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) throw new Error("路线助手连接已结束，未收到结果。");
      onActivity();
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "progress") onProgress(event.message);
        if (event.type === "result") return event.data;
        if (event.type === "error") throw Object.assign(new Error(event.message), { status: event.status });
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function createApi(fetcher = (...args) => fetch(...args)) {
  return async (path, options = {}) => {
    const { onProgress, timeoutMs = (options.method && options.method !== 'GET' ? 180000 : 30000), signal, ...request } = options;
    const controller = new AbortController();
    const abort = () => controller.abort(signal.reason);
    if (signal?.aborted) abort();
    else signal?.addEventListener('abort', abort, { once: true });
    let timer;
    const resetTimeout = () => {
      clearTimeout(timer);
      timer = setTimeout(() => controller.abort(new DOMException('Timeout', 'TimeoutError')), timeoutMs);
    };
    resetTimeout();
    const writing = !onProgress && request.method && request.method !== 'GET';
    try {
      const headers = new Headers(request.headers);
      if (request.body != null && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
      const response = await fetcher(apiUrl(path), { ...request, signal: controller.signal, headers });
      if (onProgress && response.ok && response.headers.get("Content-Type")?.includes("application/x-ndjson")) {
        return await readProgress(response, onProgress, resetTimeout);
      }
      const raw = await response.text();
      let data;
      try { data = JSON.parse(raw); } catch { throw Object.assign(new Error(writing ? `服务返回异常（HTTP ${response.status}），操作结果待核实，请重新读取当前状态。` : `服务返回异常（HTTP ${response.status}），请稍后重试`), { status: response.status, uncertain: Boolean(writing) }); }
      if (!response.ok || data.success === false) throw Object.assign(new Error(data.message || `请求失败（HTTP ${response.status}）`), { status: response.status, code: data.code, field: data.field, activityId: data.activity_id });
      return data;
    } catch (cause) {
      if (signal?.aborted) throw cause;
      if (!cause.status) {
        throw Object.assign(new Error(writing ? '连接中断或等待超时，操作结果待核实。请重新读取当前状态，核实后再重试。' : '读取失败或等待超时，请检查服务连接后重试。'), { uncertain: Boolean(writing), cause });
      }
      throw cause;
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  };
}
export const api = createApi();

export async function refreshAfterCommit(refresh, onWarning = () => {}) {
  try { await refresh(); } catch { onWarning('操作已保存，但行程列表更新失败。可重试刷新列表。'); }
}
