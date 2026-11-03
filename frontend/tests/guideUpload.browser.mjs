// Run after npm run build with Playwright available through NODE_PATH.
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';

const { chromium } = createRequire(import.meta.url)('playwright');
const root = resolve(import.meta.dirname, '../dist');
const server = createServer(async (req, res) => {
  const path = req.url.startsWith('/assets/') ? resolve(root, '.' + req.url) : resolve(root, 'index.html');
  res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[extname(path)] || 'text/plain');
  res.end(await readFile(path));
});
await new Promise(done => server.listen(4178, '127.0.0.1', done));
let browser;
try {
  browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const page = await browser.newPage();
  page.setDefaultTimeout(5000);
  const plan = { plan_id: 'upload', destination: '杭州', start_date: '2026-11-01', end_date: '2026-11-03', planning: {
    stage: 'activities', selected_transport: { outbound: 'o', return: 'r' }, activities: [],
  } };
  await page.route('http://127.0.0.1:8788/**', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const data = path.endsWith('/generation') && request.method() === 'POST'
      ? { job_id: 'job' }
      : path.endsWith('/generation') ? { job: null }
      : path === '/api/plans/upload' ? { plan }
      : { plans: [plan] };
    return route.fulfill({ status: 200, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' }, body: JSON.stringify(data) });
  });
  let statusSocket;
  let connected;
  const reconnected = new Promise(resolve => { connected = resolve; });
  await page.routeWebSocket('**/generation/ws', socket => { if (statusSocket) connected(); statusSocket = socket; socket.send(JSON.stringify({ type: 'generation_status', job: null })); });
  await page.goto('http://127.0.0.1:4178/plans/upload/planning');
  await page.getByText('请选择一家住宿酒店').waitFor({ state: 'attached' });
  const upload = page.getByLabel('攻略图片');
  assert.equal(await upload.isEnabled(), true);
  await upload.setInputFiles({ name: 'guide.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=', 'base64') });
  const submitted = page.waitForRequest(request => request.url().endsWith('/generation') && request.method() === 'POST');
  await page.getByRole('button', { name: '生成每日行程' }).click();
  assert.ok((await submitted).postDataJSON().image_base64);
  assert.equal(await page.getByText('把想去的地方，整理成旅程').count(), 0);
  await reconnected;
  const sendStatus = job => statusSocket.send(JSON.stringify({ type: 'generation_status', job }));
  sendStatus({ job_id: 'job', status: 'running', stage: 'route_agent' });
  await page.getByRole('status').filter({ hasText: '计算路线' }).waitFor();
  await page.getByText('查看执行过程').click();
  assert.equal(await page.locator('.guide-process li.current').textContent(), '7计算路线处理中');
  sendStatus({ job_id: 'job', status: 'failed', error: '地图服务暂时不可用' });
  await page.getByText('这次生成未能完成').waitFor();
  await page.getByText('查看错误详情').click();
  await page.getByText('地图服务暂时不可用').waitFor();
  assert.equal(await page.getByRole('button', { name: '重新生成', exact: true }).isEnabled(), true);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  console.log('PASS progress, failure recovery, and mobile layout');
  console.log('PASS guide upload and generation remain available with unresolved place issues');
} finally {
  await browser?.close();
  await new Promise(done => server.close(done));
}
