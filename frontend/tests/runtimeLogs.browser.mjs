// Run after npm run build, with Playwright available through NODE_PATH.
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';

const { chromium } = createRequire(import.meta.url)('playwright');
const root = resolve(import.meta.dirname, '../dist');
let browser;
try {
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  await page.route('http://127.0.0.1:4179/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    const path = pathname.startsWith('/assets/') ? resolve(root, '.' + pathname) : resolve(root, 'index.html');
    const contentType = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' }[extname(path)];
    await route.fulfill({ contentType, body: await readFile(path) });
  });
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const job = 'adea374135994857a7937c6813cd69c6';
  const run = { runId: job, jobId: job, planId: 'plan-1', sessionId: 'plan-1', source: 'generation',
    startedAt: '2026-11-03T07:34:03Z', status: 'failed', input: '行程生成 · plan-1', model: 'qwen2.5:7b', durationMs: 4000 };
  const events = [
    { type: 'run_start', input: '行程生成' },
    { type: 'generation_progress', stage: 'place_match_agent', next: 'prepare_draft' },
    { type: 'model_request', call_id: 'model-1', body: { model: 'qwen2.5:7b', options: { temperature: 0 }, messages: [
      { role: 'system', content: '只输出地点顺序' }, { role: 'user', content: '{"ordered_places":[]}' },
    ] }, stage: 'itinerary_agent' },
    { type: 'model_response', call_id: 'model-1', content: '', body: { message: { content: '', tool_calls: [
      { function: { name: 'search_map_places', arguments: { query: '独山子大峡谷' } } },
    ] } }, usage: { inputTokens: 12, outputTokens: 7 } },
    { type: 'api_request', call_id: 'map-1', method: 'GET', url: 'https://restapi.amap.com/v3/place/text', params: { keywords: '独山子大峡谷' } },
    { type: 'api_response', call_id: 'map-1', body: { info: 'INVALID_PARAMS' }, isError: true, stage: 'search_places' },
    { type: 'model_request', requestId: 'chat-1', messages: [{ role: 'user', content: '对话请求' }], tools: [] },
    { type: 'model_response', requestId: 'chat-1', content: '对话输出', tool_calls: [] },
    { type: 'generation_error', message: '地点位置 1 重复或越界，已安排位置：[]', traceback: 'ValueError: invalid proposal' },
    { type: 'run_end', status: 'failed' },
  ].map((event, index) => ({ timestamp: `2026-11-03T07:34:${String(index + 3).padStart(2, '0')}Z`, ...event }));
  let lastQuery;
  let logRequests = 0;
  const pageTwoRun = { ...run, runId: 'b'.repeat(32), input: '第二页运行' };
  await page.route('http://127.0.0.1:8788/**', route => {
    const url = new URL(route.request().url());
    let data = { plans: [] };
    if (url.pathname.startsWith('/api/logs/')) logRequests++;
    if (url.pathname === '/api/logs/runs') {
      lastQuery = url.searchParams;
      const page = Number(lastQuery.get('page'));
      data = { runs: [page === 2 ? pageTwoRun : run], page, page_count: 2, total: 2, truncated: false };
    }
    else if (url.pathname.includes('/events/')) {
      const event = events.find(item => item.timestamp === decodeURIComponent(url.pathname.split('/').pop()));
      const request = events.find(item => item.type.endsWith('_request') &&
        (event.call_id || event.requestId) && (item.call_id || item.requestId) === (event.call_id || event.requestId));
      data = { event, request };
    } else if (url.pathname.startsWith('/api/logs/runs/')) {
      const term = url.searchParams.get('search')?.toLowerCase();
      if (url.searchParams.get('view') !== 'timeline') {
        data = { run, truncated: false, events };
      } else {
        const requestFor = event => events.find(item => item.type === 'model_request' &&
          (event.call_id || event.requestId) && (item.call_id || item.requestId) === (event.call_id || event.requestId));
        data = { run, truncated: false, events: events
          .filter(event => event.type !== 'model_request' || !events.some(item => item.type === 'model_response' &&
            (event.call_id || event.requestId) === (item.call_id || item.requestId)))
          .filter(event => !term || JSON.stringify([event, requestFor(event)]).toLowerCase().includes(term))
          .map(event => ({ ...event, eventId: event.timestamp, pairedRequest: event.type === 'model_response' && !!requestFor(event) })) };
      }
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, ...data }) });
  });
  await page.goto(`http://127.0.0.1:4179/logs?job_id=${job}`);
  assert.equal(await page.getByRole('textbox', { name: '任务编号' }).inputValue(), job);
  const runList = page.getByRole('region', { name: '运行列表' });
  await runList.getByRole('button', { name: '下一页' }).click();
  await runList.getByText('第二页运行', { exact: true }).waitFor();
  assert.equal(lastQuery.get('page'), '2');
  assert.equal(logRequests, 2);
  await runList.getByRole('button', { name: '上一页' }).click();
  await runList.getByRole('button', { name: /行程生成/ }).click();
  await page.locator('.log-detail-heading h2').getByText('qwen2.5:7b', { exact: true }).waitFor();
  assert.equal(lastQuery.get('job_id'), job);
  assert.equal(await page.locator('.log-timeline > li').count(), 8);
  assert.equal(logRequests, 4);
  const modelResponse = page.locator('.log-event').filter({ has: page.getByText('模型调用（输入 / 输出）', { exact: true }) }).first();
  await modelResponse.locator(':scope > summary').click();
  await modelResponse.getByRole('heading', { name: 'Prompt（模型输入）', exact: true }).waitFor();
  assert.match(await modelResponse.locator('.log-event-section').filter({ hasText: 'Prompt（模型输入）' }).textContent(), /只输出地点顺序/);
  const output = modelResponse.locator('.log-event-section').filter({ has: page.getByRole('heading', { name: '模型输出', exact: true }) });
  assert.match(await output.textContent(), /search_map_places/);
  const afterExpand = logRequests;
  await page.setViewportSize({ width: 390, height: 740 });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.evaluate(() => {
    window.dispatchEvent(new Event('blur'));
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  assert.equal(await modelResponse.evaluate(element => element.open), true);
  assert.equal(logRequests, afterExpand);
  const search = page.getByLabel('搜索详情');
  await search.fill('search_map_places');
  await page.waitForFunction(() => document.querySelectorAll('.log-timeline > li').length === 1);
  await page.getByRole('button', { name: '一键复制上下文', exact: true }).click();
  await page.getByRole('status').getByText('已复制全部日志上下文', { exact: true }).waitFor();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(copied, /只输出地点顺序/);
  assert.match(copied, /对话请求/);
  assert.match(copied, /对话输出/);
  assert.equal(copied.split('只输出地点顺序').length - 1, 1);
  await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); }; });
  await page.getByRole('button', { name: '一键复制上下文', exact: true }).click();
  await page.getByRole('status').getByText('复制失败：denied', { exact: true }).waitFor();
  await search.clear();
  await page.waitForFunction(() => document.querySelectorAll('.log-timeline > li').length === 8);
  const apiResponse = page.locator('.log-event').filter({ has: page.getByText('地图 / API 响应', { exact: true }) });
  await apiResponse.locator(':scope > summary').click();
  await apiResponse.getByRole('heading', { name: '响应内容', exact: true }).waitFor();
  assert.match(await apiResponse.locator('.log-event-section').filter({ hasText: '请求参数' }).textContent(), /独山子大峡谷/);
  assert.match(await apiResponse.locator('.log-event-section').filter({ hasText: '响应内容' }).textContent(), /INVALID_PARAMS/);
  await apiResponse.getByText('原始日志 JSON', { exact: true }).click();
  await apiResponse.locator('.log-raw pre').waitFor();
  assert.match(await apiResponse.locator('.log-raw pre').textContent(), /map-1/);
  const chatResponse = page.locator('.log-event').filter({ has: page.getByText('模型调用（输入 / 输出）', { exact: true }) }).nth(1);
  await chatResponse.locator(':scope > summary').click();
  assert.match(await chatResponse.locator('.log-event-section').filter({ hasText: 'Prompt（模型输入）' }).textContent(), /对话请求/);
  assert.match(await chatResponse.locator('.log-event-section').filter({ hasText: '模型输出' }).textContent(), /对话输出/);
  await page.getByLabel('事件类型').selectOption('error');
  assert.equal(await page.locator('.log-timeline > li').count(), 3);
  await page.getByText('生成异常', { exact: true }).click();
  await page.locator('.log-event pre').getByText('ValueError: invalid proposal', { exact: false }).waitFor();
  await page.getByLabel('事件类型').selectOption('model');
  assert.equal(await page.locator('.log-timeline > li').count(), 4);
  await page.getByLabel('事件类型').selectOption('all');
  assert.equal(await page.getByText('查找真实地点并匹配候选', { exact: true }).count(), 2);
  events.splice(events.findIndex(event => event.type === 'api_request'), 1);
  events.splice(events.findIndex(event => event.type === 'model_request'), 1);
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.locator('.log-detail-heading h2').getByText('qwen2.5:7b', { exact: true }).waitFor();
  const unpaired = page.locator('.log-event').filter({ has: page.getByText('模型响应', { exact: true }) }).first();
  await unpaired.locator(':scope > summary').click();
  await unpaired.getByText('对应请求未保留，无法还原 Prompt。', { exact: true }).waitFor();
  await apiResponse.locator(':scope > summary').click();
  await apiResponse.getByText('对应请求未保留，无法还原请求参数。', { exact: true }).waitFor();
  const mapError = events.find(event => event.type === 'api_response');
  Object.assign(mapError, { type: 'generation_error', url: 'https://restapi.amap.com/v3/place/text',
    params: { keywords: '独山子大峡谷' }, traceback: 'RuntimeError: INVALID_PARAMS' });
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.locator('.log-detail-heading h2').getByText('qwen2.5:7b', { exact: true }).waitFor();
  const apiError = page.locator('.log-event').filter({ has: page.getByText('生成异常', { exact: true }) }).first();
  await apiError.locator(':scope > summary').click();
  await apiError.getByRole('heading', { name: '异常信息', exact: true }).waitFor();
  assert.match(await apiError.getByRole('region', { name: '请求参数', exact: true }).textContent(), /独山子大峡谷/);
  assert.match(await apiError.getByRole('region', { name: '响应内容', exact: true }).textContent(), /INVALID_PARAMS/);
  assert.match(await apiError.getByRole('region', { name: '异常信息', exact: true }).textContent(), /RuntimeError/);
  await page.getByRole('button', { name: '筛选', exact: true }).click();
  await page.waitForURL(url => !url.searchParams.has('run_id') && url.searchParams.get('job_id') === job);
  assert.deepEqual(errors, []);
  console.log('PASS paired model calls, full-context system clipboard copy under filtering, copy failure, task filter, traceback and no browser errors');
} finally {
  await browser?.close();
}
