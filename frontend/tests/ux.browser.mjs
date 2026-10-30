// Run after npm run build. Uses Playwright from NODE_PATH (or a local install).
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const root = resolve(import.meta.dirname, '../dist');
const server = createServer(async (req, res) => {
  try {
    const path = req.url.startsWith('/assets/') ? resolve(root, '.' + req.url) : resolve(root, 'index.html');
    res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[extname(path)] || 'text/plain');
    res.end(await readFile(path));
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(done => server.listen(4177, '127.0.0.1', done));
let browser;
try {
  browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.setDefaultTimeout(8000);
  let plan;
  let listFails = false;
  let chatFails = true;
  let audit = { issues: [] };
  let lastPayload;
  let creates = 0;
  const place = { place_id: 'poi', name: '具体位置', address: '杭州', longitude: 120.1, latitude: 30.2 };
  const makePlan = () => ({ plan_id: 'ux', title: '测试行程', destination: '杭州', departure_city: '上海', start_date: '2026-11-01', end_date: '2026-11-03', travelers: 1, budget: 3000, currency: 'CNY', itinerary: [], routes: [], planning: { stage: 'activities', selected_transport: { outbound: 'o', return: 'r' }, activities: [
    { id: 'hotel', name: '测试酒店', category: 'hotel', choice: 'optional', duration_minutes: 90 },
    { id: 'visit', name: '西湖', category: 'attraction', choice: 'must', duration_minutes: 90, place },
  ], hotel_candidates: [{ id: 'hotel', name: '测试酒店', nights: 2, estimated_total: 200 }] } });
  await page.route('http://127.0.0.1:8788/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 200, headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' } });
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(data) });
    if (path === '/api/plans') return listFails ? reply({ message: '列表刷新失败' }, 500) : reply({ plans: [plan || makePlan(), { plan_id: 'slow', title: '慢行程' }, { plan_id: 'fast', title: '快行程' }] });
    if (path === '/api/plans/drafts') { creates++; plan = makePlan(); plan.planning.stage = 'requirements'; return reply({ plan }); }
    if (path.endsWith('/messages')) {
      if (request.method() === 'POST') return chatFails ? reply({ message: '发送失败测试' }, 500) : reply({ messages: [{ message_id: 'm', role: 'user', content: request.postDataJSON().content }] });
      return reply({ messages: [] });
    }
    if (path.endsWith('/planning/audit')) return reply({ audit });
    if (path.endsWith('/planning/map')) return reply({ message: '地图服务暂时不可用' }, 502);
    if (path.endsWith('/places/search')) return reply({ places: [place] });
    if (path === '/api/plans/slow') { await new Promise(done => setTimeout(done, 500)); return reply({ plan: { ...plan, plan_id: 'slow', destination: '慢目的地' } }); }
    if (path === '/api/plans/fast') return reply({ plan: { ...plan, plan_id: 'fast', destination: '快目的地' } });
    if (request.method() === 'GET' && /^\/api\/plans\//.test(path)) return reply({ plan });
    if (request.method() === 'POST') {
      lastPayload = request.postDataJSON();
      for (const item of plan.planning.activities) {
        item.choice = lastPayload.choices?.[item.id] ?? item.choice;
        item.category = lastPayload.categories?.[item.id] ?? item.category;
        Object.assign(item, lastPayload.details?.[item.id] || {});
      }
      if ('selected_hotel' in lastPayload) plan.planning.selected_hotel = lastPayload.selected_hotel;
      if ('text' in lastPayload) plan.planning.draft_text = lastPayload.text;
      if (path.endsWith('/resolve_place')) plan.planning.activities.find(item => item.id === lastPayload.activity_id).place = place;
      if (path.endsWith('/guide')) { plan.planning.guide_text = '识别出的攻略'; plan.planning.draft_text = '识别出的攻略'; }
      if (path.endsWith('/draft')) plan.planning.stage = 'review';
      if (path.endsWith('/confirm')) plan.planning.stage = 'confirmed';
      if (path.endsWith('/edit_places')) plan.planning.stage = 'activities';
      if (path.endsWith('/update_budget')) { plan.budget = lastPayload.budget; audit = { issues: [] }; }
      return reply({ plan, audit });
    }
    return reply({ message: 'unhandled fixture route' }, 404);
  });

  plan = makePlan();
  await page.goto('http://127.0.0.1:4177/plans/ux/planning');
  await page.getByText('请选择一家住宿酒店', { exact: false }).waitFor();
  assert.equal(await page.getByRole('button', { name: '生成并检查路线', exact: true }).isDisabled(), true);
  await page.getByRole('radio', { name: '作为住宿' }).check();
  await page.getByText('测试酒店：请搜索并确认具体位置').waitFor();
  await page.getByRole('button', { name: '搜索地点', exact: true }).first().click();
  await page.getByRole('button', { name: '确认位置', exact: true }).click();
  await page.getByText('修改已保存', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '生成并检查路线', exact: true }).isEnabled(), true);
  await page.locator('#activity-visit summary').first().click();
  await page.locator('#activity-visit input[type=number]').fill('');
  assert.equal(await page.getByRole('button', { name: '生成并检查路线', exact: true }).isDisabled(), true);
  await page.getByLabel('西湖的选择').selectOption('exclude');
  assert.equal(await page.getByRole('button', { name: '生成并检查路线', exact: true }).isEnabled(), true);
  await page.getByRole('button', { name: '保存地点选择' }).click();
  await page.getByText('修改已保存', { exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('fieldset')?.disabled);
  assert.equal(lastPayload.details.visit, undefined);
  await page.locator('#guide-input').fill('保留这段文字');
  await page.getByRole('button', { name: '保存地点选择' }).click();
  await page.getByText('攻略文字已修改，下次识别或生成时会重新解析', { exact: false }).waitFor();
  await page.waitForFunction(() => !document.querySelector('fieldset')?.disabled);
  await page.locator('input[type=file]').setInputFiles({ name: 'guide.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=', 'base64') });
  await page.waitForFunction(() => document.querySelector('#guide-input')?.value === '识别出的攻略');
  assert.equal(lastPayload.choices.visit, 'exclude');
  assert.ok(lastPayload.image_base64);
  // A dirty navigation is cancellable and a successful generation does not ask again.
  await page.locator('#guide-input').fill('新文字');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('link', { name: '← 行程详情' }).click();
  assert.match(page.url(), /planning$/);
  listFails = true;
  await page.getByRole('button', { name: '生成并检查路线', exact: true }).click();
  await page.waitForURL('**/review');
  console.log('PASS validation, excluded details, upload snapshot, dirty navigation and successful-write/list-failure navigation');

  audit = { issues: [{ type: 'budget_overrun', severity: 'high', message: '当前预算不足', target: null }] };
  await page.reload();
  await page.getByRole('heading', { name: '有 1 项需要处理' }).waitFor();
  assert.equal(await page.getByRole('button', { name: '确认并生成行程单' }).isDisabled(), true);
  await page.locator('.review-summary').getByRole('button', { name: '去处理' }).click();
  await page.locator('#review-budget input').fill('6000');
  await page.getByRole('button', { name: '保存预算并重新审核' }).click();
  await page.waitForFunction(() => ![...document.querySelectorAll('button')].find(node => node.textContent === '确认并生成行程单')?.disabled);
  await page.getByRole('button', { name: '重试当天地图' }).waitFor();
  assert.equal(plan.budget, 6000);
  console.log('PASS audit budget repair and map failure recovery');

  plan.planning.stage = 'confirmed';
  audit = { issues: [{ type: 'budget_overrun', severity: 'high', message: '实际支出超预算', target: null }] };
  await page.reload();
  await page.locator('.review-summary').getByRole('button', { name: '去处理' }).click();
  await page.getByRole('button', { name: '返回编辑行程', exact: true }).click();
  await page.waitForURL('**/planning');
  await page.getByRole('heading', { name: '攻略与想去的地方' }).waitFor();
  console.log('PASS confirmed itinerary has a working repair entry');

  plan.planning.stage = 'candidates';
  plan.planning.transport_candidates = [
    { id: 'o', direction: 'outbound', kind: 'train', origin: '上海', destination: '杭州', date: '2026-11-01', depart_time: '07:00', arrive_time: '09:00', estimated_total: 100 },
    { id: 'o2', direction: 'outbound', kind: 'train', origin: '上海', destination: '杭州', date: '2026-11-01', depart_time: '08:00', arrive_time: '10:00', estimated_total: 100 },
    { id: 'r', direction: 'return', kind: 'train', origin: '杭州', destination: '上海', date: '2026-11-03', depart_time: '17:00', arrive_time: '19:00', estimated_total: 100 },
  ];
  await page.reload();
  await page.locator('#transport-outbound input[type=radio]').nth(1).check();
  await page.getByRole('button', { name: /2 选活动与攻略/ }).click();
  await page.getByText('交通选择尚未确认，请先点击“确认交通，继续”。').waitFor();
  assert.equal(await page.locator('#transport-outbound input[type=radio]').nth(1).isChecked(), true);
  console.log('PASS unconfirmed transport selection is not silently discarded');

  listFails = false;
  page.once('dialog', dialog => dialog.accept());
  await page.goto('http://127.0.0.1:4177/conversations/chat');
  const composer = page.locator('.composer textarea');
  await composer.fill('失败时应保留的旅行需求');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.getByRole('button', { name: '重试发送原文' }).waitFor();
  assert.equal(await composer.inputValue(), '失败时应保留的旅行需求');
  chatFails = false;
  await page.getByRole('button', { name: '重试发送原文' }).click();
  await page.locator('.message.user').waitFor();
  console.log('PASS chat input retention and retry');

  await page.goto('http://127.0.0.1:4177/plans/new');
  for (const [label, value] of [['目的地', '杭州'], ['出发城市', '上海'], ['出发日期', '2026-11-01'], ['总预算', '3000']]) await page.getByLabel(label, { exact: true }).fill(value);
  listFails = true;
  await page.getByRole('button', { name: '创建行程，选择交通' }).click();
  await page.waitForURL('**/planning');
  await page.getByRole('heading', { name: '准备查询交通' }).waitFor();
  assert.equal(creates, 1);

  plan = makePlan(); listFails = false;
  plan.planning.selected_hotel = 'hotel'; plan.planning.activities[0].place = place;
  plan.planning.activities.push(...Array.from({ length: 20 }, (_, i) => ({ id: `extra-${i}`, name: `地点 ${i}`, category: 'attraction', choice: 'optional', duration_minutes: 60, place })));
  await page.reload();
  await page.getByRole('heading', { name: '攻略与想去的地方' }).waitFor();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const actionBox = await page.getByRole('button', { name: '生成并检查路线', exact: true }).boundingBox();
  assert.ok(actionBox && actionBox.y >= 0 && actionBox.y < 1000, 'primary action remains visible in a long place list');
  await page.screenshot({ path: resolve(root, 'ux-planner-check.png'), fullPage: false });
  console.log('PASS primary action remains visible with more than 20 places');
  await page.getByRole('link', { name: '慢行程', exact: true }).click();
  await page.getByRole('link', { name: '快行程', exact: true }).click();
  await page.locator('.plan-hero h1').getByText('快目的地', { exact: true }).waitFor();
  await page.waitForTimeout(650);
  assert.equal(await page.locator('.plan-hero h1').textContent(), '快目的地');
  console.log('PASS delayed old-plan response cannot replace the current plan');
  assert.deepEqual(errors, []);
  console.log('PASS draft creation independent of secondary failure; no browser runtime errors');
} finally {
  await browser?.close();
  await new Promise(done => server.close(done));
}
