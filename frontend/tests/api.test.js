import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, refreshAfterCommit } from '../src/api.js';

test('non JSON failure reports readable status', async () => {
  const api = createApi(async () => new Response('<html>bad gateway</html>', { status: 502 }));
  await assert.rejects(api('/plans'), /502/);
});
test('server validation message survives request wrapper', async () => {
  const api = createApi(async () => Response.json({ success: false, message: '请选择酒店', code: 'hotel' }, { status: 400 }));
  await assert.rejects(api('/plans'), e => e.message === '请选择酒店' && e.status === 400 && e.code === 'hotel');
});
test('secondary refresh cannot reject successful write', async () => {
  let warning;
  await refreshAfterCommit(async () => { throw new Error('offline'); }, message => { warning = message; });
  assert.match(warning, /已保存/);
});
test('write timeout reports uncertain result and never retries', async () => {
  let calls = 0;
  const api = createApi((url, { signal }) => { calls++; return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true })); });
  await assert.rejects(api('/plans', { method: 'POST', timeoutMs: 10 }), e => e.uncertain === true && /核实/.test(e.message));
  assert.equal(calls, 1);
});
test('unreadable write response has uncertain outcome even with HTTP 200', async () => {
  const api = createApi(async () => new Response('{"plan":', { status: 200 }));
  await assert.rejects(api('/plans', { method: 'POST' }), e => e.uncertain === true);
});
