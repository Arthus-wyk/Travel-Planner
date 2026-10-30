import { apiUrl } from './apiUrl.js';

export function createApi(fetcher = (...args) => fetch(...args)) {
  return async (path, options = {}) => {
    const { timeoutMs = (options.method && options.method !== 'GET' ? 180000 : 30000), signal, ...request } = options;
    const controller = new AbortController();
    const abort = () => controller.abort(signal.reason);
    if (signal?.aborted) abort();
    else signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => controller.abort(new DOMException('Timeout', 'TimeoutError')), timeoutMs);
    const writing = request.method && request.method !== 'GET';
    try {
      const response = await fetcher(apiUrl(path), { ...request, signal: controller.signal, headers: { 'Content-Type': 'application/json', ...request.headers } });
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
