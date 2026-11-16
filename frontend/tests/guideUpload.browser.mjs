// Run after npm run build with Playwright available through NODE_PATH.
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import assert from 'node:assert/strict';
import { applyReviewEdit } from '../src/reviewDraft.js';

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
  await page.addInitScript(() => {
    class MapStub { add() {} addControl() {} setFitView() {} destroy() {} }
    window.AMap = { Map: MapStub, Marker: class {}, ToolBar: class {}, Scale: class {} };
  });
  page.setDefaultTimeout(5000);
  const plan = { plan_id: 'upload', destination: '杭州', start_date: '2026-11-01', end_date: '2026-11-03', planning: {
    stage: 'review', selected_transport: { outbound: 'o', return: 'r' }, activities: [],
  } };
  const poi = { place_id: 'poi', name: '西湖', address: '杭州市西湖区', longitude: 120, latitude: 30 };
  const hotels = ['湖畔酒店', '西溪酒店'].map((name, index) => ({
    ...poi, name, place_id: `hotel-${index}`, address: `杭州市${name}`,
  }));
  const searchQueries = [];
  const defaultModel = 'deepseek-ai/DeepSeek-V4.1-Flash';
  const qwenModel = 'Qwen/Qwen3.5-122B-A10B';
  let planningWrites = 0;
  let staleReview = false;
  let recoverReview = false;
  let completeStay = false;
  let lodgingSave;
  let confirming = false;
  const recoveryRequests = [];
  let releaseSubmission;
  const submission = new Promise(resolve => { releaseSubmission = resolve; });
  await page.route('http://127.0.0.1:8788/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path.endsWith('/places/search')) searchQueries.push(request.postDataJSON().query);
    else if (request.method() === 'POST' && path.includes('/planning/')) planningWrites++;
    if (request.method() === 'POST' && path.endsWith('/generation')) await submission;
    const saving = request.method() === 'POST' && path.endsWith('/review_changes');
    if (recoverReview && saving) recoveryRequests.push(request.postDataJSON());
    const conflict = saving && (staleReview || recoverReview && recoveryRequests.length === 1);
    let recovered;
    if (recoverReview && saving && !conflict) {
      const edit = recoveryRequests.at(-1).operations[0];
      const updated = applyReviewEdit(plan, edit.action, { ...edit.payload, place: poi });
      const inserted = updated.itinerary.find(item => item.reference_id === edit.payload.activity_id);
      inserted.time_of_day = '10:43';
      updated.routes = [{ origin_item_id: 'a1', destination_item_id: inserted.item_id,
        duration_minutes: 13, travel_mode: 'WALK', source: '高德地图' }];
      recovered = { plan: updated, plan_version: 'v3', audit: { issues: [] } };
    }
    if (completeStay && saving) {
      lodgingSave = request.postDataJSON();
      recovered = { plan: { ...plan, itinerary: plan.itinerary.map(item => ({
        ...item, ...(item.category === 'hotel_anchor' && {
          lodging_pending: false, reference_id: 'manual-existing', hotel_name: hotels[0].name,
          location: hotels[0].address, place: hotels[0],
        }),
      })).concat([
        { item_id: 'night-two-end', day: 2, category: 'hotel_anchor', anchor_role: 'end', location: hotels[0].address, place: hotels[0] },
        { item_id: 'night-three-start', day: 3, category: 'hotel_anchor', anchor_role: 'start', location: hotels[0].address, place: hotels[0] },
        { item_id: 'night-three-end', day: 3, category: 'hotel', anchor_role: 'checkout', location: hotels[0].address, place: hotels[0] },
      ]) }, plan_version: 'v4', audit: { issues: [] } };
    }
    const data = conflict ? { success: false, message: '行程已在其他页面修改' }
      : recovered ?? (
        path.endsWith('/places/search') ? { places: [...hotels, poi] }
      : path === '/api/models' ? { models: [defaultModel, qwenModel], default: defaultModel }
      : path.endsWith('/generation') && request.method() === 'POST'
      ? { job_id: 'job' }
      : path.endsWith('/generation') ? { job: null }
      : path.endsWith('/planning/audit') ? { audit: { issues: plan.itinerary ? [
        { type: 'time_conflict', severity: 'high', target: 'a1', message: '西湖停留与后续行程冲突。' },
        { type: 'pace_risk', severity: 'medium', target: 2, message: '第二天行程较紧凑。' },
      ] : [], budget: { estimated_total: 0, items: [] } } }
      : path.endsWith('/planning/confirm') && confirming ? { plan: { ...plan, planning: { ...plan.planning, stage: 'confirmed' } } }
      : path.endsWith('/planning/map') ? { points: [] }
      : path === '/api/plans/upload' ? { plan, plan_version: recoverReview ? 'v2' : 'v1' }
      : { plans: [plan] });
    return route.fulfill({ status: conflict ? 409 : 200, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' }, body: JSON.stringify(data) });
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
  await page.getByLabel('模型', { exact: true }).selectOption(qwenModel);
  const submitted = page.waitForRequest(request => request.url().endsWith('/generation') && request.method() === 'POST');
  await page.getByRole('button', { name: '生成每日行程' }).click();
  await page.getByRole('status').filter({ hasText: '正在提交攻略' }).waitFor();
  assert.equal(await page.getByRole('button', { name: /审核路线/ }).isDisabled(), true);
  assert.ok((await submitted).postDataJSON().image_base64);
  assert.equal((await submitted).postDataJSON().model, qwenModel);
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
  const retryRequest = page.waitForRequest(request => request.url().endsWith('/generation?retry=true') && request.method() === 'POST');
  const beforeRetry = sockets.length;
  await page.getByRole('button', { name: '重试生成', exact: true }).click();
  assert.equal((await retryRequest).postDataJSON().image_base64, (await submitted).postDataJSON().image_base64);
  statusSocket = await socketAfter(beforeRetry);
  sendStatus({ job_id: 'job', status: 'failed', error: '地图服务暂时不可用' });
  await page.getByText('这次生成未能完成').waitFor();
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
  assert.deepEqual((await resubmitted).postDataJSON(), { text: '继续编辑攻略', model: defaultModel });
  statusSocket = await socketAfter(beforeRegeneration);
  sendStatus({ job_id: 'job', status: 'completed' });
  await page.waitForURL('**/review');
  console.log('PASS progress, failure recovery, and mobile layout');
  console.log('PASS guide upload and generation remain available with unresolved place issues');

  plan.budget = 1000;
  plan.planning.activities = [{ id: 'a', name: '西湖', category: 'attraction', choice: 'must', place_status: 'failed', place_candidates: [poi] }];
  plan.planning.guide_auto = true;
  await page.getByRole('button', { name: /选活动与攻略/ }).click();
  await page.getByRole('heading', { name: '根据攻略生成每日行程' }).waitFor();
  assert.equal(await page.getByRole('heading', { name: '攻略与想去的地方' }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '确认位置', exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '手动添加地点' }).count(), 0);
  console.log('PASS place matching stays in the agent workflow');

  plan.itinerary = [{ item_id: 'a1', reference_id: 'a', category: 'attraction', day: 1, title: '西湖',
    time_of_day: '09:00', duration_minutes: 90, location: poi.address, place: poi },
  { item_id: 'night-end', day: 1, category: 'hotel_anchor', anchor_role: 'end', lodging_pending: true,
    title: '返回酒店', time_of_day: '20:00', location: '待选择住宿' },
  { item_id: 'night-start', day: 2, category: 'hotel_anchor', anchor_role: 'start', lodging_pending: true,
    title: '从酒店出发', time_of_day: '09:00', location: '待选择住宿' }];
  plan.routes = [];
  plan.planning.guide_days = [{ day: 1, text: '第一天：游西湖' }, { day: 2, text: '第二天：去灵隐寺' }];
  await page.getByRole('button', { name: /审核路线/ }).click();
  await page.getByText('第一天：游西湖', { exact: true }).waitFor();
  assert.equal(await page.getByText('第二天：去灵隐寺', { exact: true }).count(), 0);
  const overview = page.locator('.review-day-overview');
  await page.locator('.review-costs summary').scrollIntoViewIfNeeded();
  const overviewTop = (await overview.boundingBox()).y;
  await page.locator('.review-costs summary').click();
  assert.equal((await overview.boundingBox()).y, overviewTop);
  await page.locator('.review-costs summary').click();
  const duration = page.locator('#review-a1').getByLabel('停留时长（分钟）');
  assert.equal(await page.locator('#review-a1').getAttribute('draggable'), null);
  assert.equal(await page.locator('#review-a1 .review-drag-handle').getAttribute('draggable'), 'true');
  assert.equal(await page.locator('#review-a1 img[draggable="true"]').count(), 0);
  await page.locator('#review-a1').getByText('西湖停留与后续行程冲突。').waitFor();
  await page.getByRole('button', { name: /第 2 天.*1 条提示/ }).click();
  await page.getByText('第二天行程较紧凑。', { exact: true }).waitFor();
  await page.getByRole('button', { name: /第 1 天.*1 条提示/ }).click();
  await page.locator('#review-a1').getByText('停留信息', { exact: false }).click();
  await duration.fill('70');
  await duration.fill('');
  await duration.fill('80');
  assert.equal(await duration.isVisible(), true);
  assert.equal(await page.getByLabel('可拖动和缩放的路线地图').isVisible(), true);
  await page.getByRole('button', { name: '在西湖后添加行程' }).click();
  let placeDialog = page.getByRole('dialog', { name: '添加行程' });
  const searchBox = placeDialog.getByLabel('搜索地点');
  await searchBox.fill('西');
  await searchBox.fill('西湖');
  await placeDialog.getByRole('button', { name: /西湖 杭州市西湖区/ }).click();
  assert.deepEqual(searchQueries, ['西湖']);
  await placeDialog.getByLabel('行程类型').selectOption('attraction');
  await placeDialog.getByLabel('停留时间（分钟）').fill('45');
  await placeDialog.getByRole('button', { name: '添加到行程' }).click();
  assert.equal(await page.locator('#review-a1 + .review-drop-slot + article strong').textContent(), '西湖');
  await page.getByRole('button', { name: '选择住宿' }).click();
  placeDialog = page.getByRole('dialog', { name: '选择住宿' });
  await placeDialog.getByLabel('搜索地点').fill('湖畔');
  await placeDialog.getByRole('button', { name: /湖畔酒店/ }).click();
  await placeDialog.getByRole('button', { name: '应用住宿' }).click();
  const lodgingDialog = page.getByRole('dialog', { name: '自动补全住宿' });
  await lodgingDialog.getByText(/第 1 天.*湖畔酒店/).waitFor();
  await lodgingDialog.getByRole('button', { name: '暂不补全' }).click();
  await page.getByRole('button', { name: '修改酒店' }).click();
  placeDialog = page.getByRole('dialog', { name: '修改酒店' });
  await placeDialog.getByLabel('搜索地点').fill('西溪');
  await placeDialog.getByRole('button', { name: /西溪酒店/ }).click();
  await placeDialog.getByRole('button', { name: '应用住宿' }).click();
  await page.getByRole('button', { name: /^第 2 天/ }).click();
  await page.locator('#review-night-start').getByText(/前一天结束地点（西溪酒店）/).waitFor();
  await page.getByRole('button', { name: /^第 1 天/ }).click();
  if (!await duration.isVisible()) await page.locator('#review-a1 summary').click();
  await duration.fill('');
  const beforeSave = planningWrites;
  await page.getByRole('button', { name: /^第 2 天/ }).click();
  await page.getByText('第二天：去灵隐寺', { exact: true }).waitFor();
  assert.equal(await page.getByText('第一天：游西湖', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '保存所有修改' }).click();
  await page.getByText('请返回对应日期修正尚未填写完整的调整项').waitFor();
  assert.equal(planningWrites, beforeSave);
  await page.getByRole('button', { name: /^第 1 天/ }).click();
  assert.equal(await duration.inputValue(), '');
  await duration.fill('80');
  await page.getByLabel('当天出发时间').fill('08:40');
  await page.getByRole('button', { name: /^第 2 天/ }).click();
  await page.getByRole('button', { name: /^第 1 天/ }).click();
  assert.equal(await page.getByLabel('当天出发时间').inputValue(), '08:40');
  assert.equal(await duration.inputValue(), '80');
  assert.equal(planningWrites, beforeSave);
  staleReview = true;
  const queuedSave = page.waitForRequest(request => request.url().includes('/planning/review_changes') && request.method() === 'POST');
  await page.getByRole('button', { name: '保存所有修改' }).click();
  const saved = (await queuedSave).postDataJSON();
  assert.ok(saved.operations.some(operation => operation.action === 'day_departure' && operation.payload.time === '08:40'));
  assert.ok(saved.operations.some(operation => operation.action === 'update_activity' && operation.payload.duration_minutes === 80));
  assert.equal(saved.operations.filter(operation => operation.action === 'review_place').length, 3);
  const reload = page.getByRole('button', { name: '重新读取并核实' });
  await reload.waitFor();
  page.once('dialog', dialog => dialog.dismiss());
  await reload.click();
  assert.equal(await duration.inputValue(), '80');
  page.once('dialog', dialog => dialog.accept());
  await reload.click();
  await page.getByText('所有修改已保存', { exact: true }).waitFor();
  assert.equal(await duration.inputValue(), '90');
  console.log('PASS off-day validation and version conflict recovery');

  staleReview = false;
  recoverReview = true;
  await page.getByRole('button', { name: '在西湖后添加行程' }).click();
  placeDialog = page.getByRole('dialog', { name: '添加行程' });
  await placeDialog.getByLabel('搜索地点').fill('西湖');
  await placeDialog.getByRole('button', { name: /西湖 杭州市西湖区/ }).click();
  await placeDialog.getByLabel('行程类型').selectOption('food');
  await placeDialog.getByRole('button', { name: '添加到行程' }).click();
  await page.getByRole('button', { name: '保存所有修改' }).click();
  await page.getByText('修改已保存', { exact: true }).waitFor();
  assert.deepEqual(recoveryRequests.map(request => request.plan_version), ['v1', 'v2']);
  assert.deepEqual(recoveryRequests[0].operations, recoveryRequests[1].operations);
  await page.locator('article time').getByText('10:43', { exact: true }).waitFor();
  await page.locator('.review-route-leg small').getByText('13 分钟', { exact: true }).waitFor();
  console.log('PASS stale additions retry with the latest version and display recalculated times');

  recoverReview = false;
  completeStay = true;
  plan.itinerary.unshift({ item_id: 'first-hotel', reference_id: 'manual-existing', day: 1,
    category: 'hotel_arrival', title: '抵达酒店', hotel_name: hotels[0].name, location: hotels[0].address,
    time_of_day: '08:00', place: hotels[0] });
  await page.goto('http://127.0.0.1:4178/plans/upload/review');
  await lodgingDialog.getByText(/第 1 天 08:00.*湖畔酒店/).waitFor();
  await lodgingDialog.getByRole('button', { name: '补全并保存' }).click();
  await page.getByText('修改已保存', { exact: true }).waitFor();
  assert.deepEqual(lodgingSave.operations, [{ action: 'complete_lodging', payload: { activity_id: 'manual-existing' } }]);
  assert.equal(await lodgingDialog.count(), 0);
  await page.locator('#review-night-end').getByText(/返回酒店.*湖畔酒店/).waitFor();
  console.log('PASS entering review offers lodging completion and saves it immediately');

  confirming = true;
  await page.reload();
  await lodgingDialog.getByRole('button', { name: '暂不补全' }).click();
  await page.locator('#review-a1').getByText('西湖停留与后续行程冲突。').waitFor();
  await page.getByRole('button', { name: '确认并生成行程单' }).click();
  await page.waitForURL('**/sheet');
  console.log('PASS route warnings appear before confirmation and allow opening the itinerary sheet');
} finally {
  await browser?.close();
  await new Promise(done => server.close(done));
}
