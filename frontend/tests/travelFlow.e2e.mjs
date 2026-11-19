import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { chromium } = createRequire(import.meta.url)('playwright');
const origin = process.argv[2];
const browser = await chromium.launch({ channel: process.env.TRAVEL_E2E_BROWSER || undefined });
try {
  const page = await browser.newPage();
  assert.ok((await page.request.get(`${origin}/api/health`)).ok());
  // Keep the browser offline except for the real local application.
  await page.route('**/*', route => new URL(route.request().url()).origin === origin
    ? route.continue() : route.abort());
  page.setDefaultTimeout(15000);
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + 30);
  await page.goto(`${origin}/plans/new`);
  for (const [label, value] of Object.entries({
    目的地: '杭州', 出发城市: '上海', 出发日期: date.toISOString().slice(0, 10),
    人数: '2', 总预算: '5000',
  })) {
    await page.getByLabel(label, { exact: true }).fill(value);
  }
  await page.getByRole('button', { name: '创建行程，选择交通' }).click();
  await page.getByRole('button', { name: '查询交通候选' }).click();
  for (const [label, direction] of [['去程', 'outbound'], ['返程', 'return']]) {
    await page.getByLabel(`${label}交通工具`).selectOption('train');
    await page.locator(`#transport-${direction}`).getByRole('radio').first().check();
  }
  await page.getByRole('button', { name: '确认交通，继续' }).click();
  await page.getByLabel('攻略文字').fill('第一天游览西湖，第二天参观灵隐寺，住湖畔酒店。');
  await page.getByRole('button', { name: '生成每日行程' }).click();
  const next = await Promise.race([
    page.waitForURL('**/review').then(() => 'review'),
    page.getByText('这次生成未能完成').waitFor().then(() => 'failed'),
  ]).catch(async error => {
    throw new Error(`生成阶段超时：${(await page.getByRole('main').innerText()).slice(-1200)}`, { cause: error });
  });
  const planUrl = page.url().replace(/\/(planning|review)(\?.*)?$/, '');
  const get = async path => {
    const response = await page.request.get(`${origin}/api${new URL(planUrl).pathname}${path}`);
    assert.ok(response.ok(), `GET ${path}: ${response.status()}`);
    return response.json();
  };
  const { job } = await get('/generation');
  assert.equal(next, 'review', JSON.stringify(job));
  assert.equal(job.status, 'completed');
  const { plan } = await get('');
  const visits = plan.itinerary.filter(stop => stop.category === 'attraction');
  assert.deepEqual(visits.map(stop => [stop.day, stop.title]), [[1, '西湖'], [2, '灵隐寺']]);
  const { audit } = await get('/planning/audit');
  const transit = audit.budget.items.filter(item => item.category === 'local_transit');
  assert.ok(transit.length > 0, 'budget contains a priced local transit leg');
  assert.ok(transit.every(item => item.counted_total === 24), 'a 12 CNY fare is counted for two travelers');
  assert.equal(audit.budget.estimated_total, 24 * transit.length);
  assert.equal(audit.budget.remaining, 5000 - audit.budget.estimated_total);
  assert.equal((await page.request.get(`${origin}/api${new URL(planUrl).pathname}/sheet`)).status(), 400);

  const stop = page.locator(`#review-${visits[0].item_id}`);
  const duration = stop.getByLabel('停留时长（分钟）');
  const minutes = String(visits[0].duration_minutes + 5);
  await stop.getByText('调整停留信息', { exact: true }).click();
  const saved = page.waitForResponse(response =>
    new URL(response.url()).pathname.endsWith('/planning/review_changes')
    && response.request().method() === 'POST');
  await duration.fill(minutes);
  await duration.blur();
  const saveResponse = await saved;
  assert.ok(saveResponse.ok(), await saveResponse.text());
  await page.getByText('所有修改已保存', { exact: true }).waitFor();
  await page.reload();
  await stop.getByText('调整停留信息', { exact: true }).click();
  assert.equal(await duration.inputValue(), minutes);

  await page.getByRole('button', { name: '路线助手' }).click();
  await page.getByRole('textbox', { name: '输入路线问题' }).fill('请将总预算改为 4900 元');
  await page.getByRole('button', { name: '发送' }).click();
  const saveProposal = page.getByRole('button', { name: '确认并保存' });
  await saveProposal.waitFor().catch(async error => {
    throw new Error(`路线助手未生成可保存的试算：${await page.getByRole('log').innerText()}`, { cause: error });
  });
  assert.equal((await get('')).plan.budget, 5000);
  await saveProposal.click();
  await page.getByText(/已保存调整，路线和日程已更新/).waitFor();
  await page.reload();
  assert.equal((await get('')).plan.budget, 4900);
  await page.getByRole('button', { name: '确认并生成行程单' }).click();
  await page.waitForURL('**/sheet');
  await page.getByRole('heading', { name: /杭州.*路书/ }).waitFor();
  const confirmed = (await get('')).plan;
  assert.equal(confirmed.planning.stage, 'confirmed');
  assert.equal(confirmed.itinerary.find(item => item.item_id === visits[0].item_id).duration_minutes, Number(minutes));
  assert.equal((await get('/sheet')).sheet.days.length, 3);
} finally {
  await browser.close();
}
