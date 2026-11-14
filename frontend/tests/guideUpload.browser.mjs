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
    stage: 'review', selected_transport: { outbound: 'o', return: 'r' }, activities: [],
  } };
  let planningWrites = 0;
  let releaseSubmission;
  const submission = new Promise(resolve => { releaseSubmission = resolve; });
  await page.route('http://127.0.0.1:8788/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path.includes('/planning/')) planningWrites++;
    if (request.method() === 'POST' && path.endsWith('/generation')) await submission;
    const data = path.endsWith('/generation') && request.method() === 'POST'
      ? { job_id: 'job' }
      : path.endsWith('/generation') ? { job: null }
      : path.endsWith('/planning/audit') ? { audit: { issues: [] } }
      : path === '/api/plans/upload' ? { plan }
      : { plans: [plan] };
    return route.fulfill({ status: 200, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' }, body: JSON.stringify(data) });
  });
  const sockets = [];
  await page.routeWebSocket('**/generation/ws', socket => {
    sockets.push(socket);
    socket.send(JSON.stringify({ type: 'generation_status', job: null }));
  });
  async function socketAfter(count) {
    await expectSocketCount(count + 1);
    return sockets.at(-1);
  }
  async function expectSocketCount(count) {
    for (let attempt = 0; sockets.length < count && attempt < 100; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.ok(sockets.length >= count, 'generation socket connected');
  }
  await page.goto('http://127.0.0.1:4178/plans/upload/planning');
  await page.waitForURL('**/review');
  await page.getByRole('heading', { name: /路线与预算审核/ }).waitFor();
  await page.getByRole('button', { name: /选活动与攻略/ }).click();
  await page.getByLabel('攻略文字').fill('还没有保存的攻略');
  const dismissed = page.waitForEvent('dialog').then(dialog => dialog.dismiss());
  await page.getByRole('button', { name: /审核路线/ }).click();
  await dismissed;
  assert.equal(await page.getByLabel('攻略文字').inputValue(), '还没有保存的攻略');
  const discarded = page.waitForEvent('dialog').then(dialog => dialog.accept());
  await page.getByRole('button', { name: /审核路线/ }).click();
  await discarded;
  await page.waitForURL('**/review');
  await page.getByRole('button', { name: /选活动与攻略/ }).click();
  await page.getByRole('button', { name: /审核路线/ }).click();
  await page.waitForURL('**/review');
  assert.equal(planningWrites, 0);
  const beforeUpload = sockets.length;
  await page.getByRole('button', { name: /选活动与攻略/ }).click();
  await socketAfter(beforeUpload);
  console.log('PASS the wizard opens review directly and steps preserve the saved route');
  const upload = page.getByLabel('攻略图片');
  assert.equal(await upload.isEnabled(), true);
  await upload.setInputFiles({ name: 'guide.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=', 'base64') });
  const submitted = page.waitForRequest(request => request.url().endsWith('/generation') && request.method() === 'POST');
  await page.getByRole('button', { name: '生成每日行程' }).click();
  await page.getByRole('status').filter({ hasText: '正在提交攻略' }).waitFor();
  assert.equal(await page.getByRole('button', { name: /审核路线/ }).isDisabled(), true);
  assert.ok((await submitted).postDataJSON().image_base64);
  const beforeReconnect = sockets.length;
  releaseSubmission();
  assert.equal(await page.getByText('把想去的地方，整理成旅程').count(), 0);
  let statusSocket = await socketAfter(beforeReconnect);
  await page.getByRole('status').filter({ hasText: '等待生成' }).waitFor();
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
  await page.getByRole('button', { name: '移除图片' }).click();
  await page.getByRole('button', { name: /审核路线/ }).click();
  await page.waitForURL('**/review');
  const beforeReopen = sockets.length;
  await page.getByRole('button', { name: /选活动与攻略/ }).click();
  statusSocket = await socketAfter(beforeReopen);
  sendStatus({ job_id: 'old', status: 'completed' });
  await page.getByLabel('攻略文字').fill('继续编辑攻略');
  assert.match(page.url(), /planning\?step=activities$/);
  const resubmitted = page.waitForRequest(request => request.url().endsWith('/generation') && request.method() === 'POST');
  const beforeRegeneration = sockets.length;
  await page.getByRole('button', { name: '生成每日行程' }).click();
  assert.equal((await resubmitted).postDataJSON().text, '继续编辑攻略');
  statusSocket = await socketAfter(beforeRegeneration);
  sendStatus({ job_id: 'job', status: 'completed' });
  await page.waitForURL('**/review');
  console.log('PASS progress, failure recovery, and mobile layout');
  console.log('PASS guide upload and generation remain available with unresolved place issues');
} finally {
  await browser?.close();
  await new Promise(done => server.close(done));
}
