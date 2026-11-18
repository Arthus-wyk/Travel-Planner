// Run after npm run build, with Playwright available through NODE_PATH.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const { chromium } = createRequire(import.meta.url)('playwright');
const root = resolve(import.meta.dirname, '../dist');
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' });
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('https://fonts.googleapis.com/**', route => route.abort());
  await page.route('http://travel.test/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    return route.fulfill({ path: resolve(root, pathname.startsWith('/assets/') ? pathname.slice(1) : 'index.html') });
  });
  const stop = (item_id, title, time_of_day, longitude, latitude) => ({
    item_id, title, time_of_day, place: { longitude, latitude, place_id: item_id }, visit_tips: `${title}游览提示`,
  });
  const first = stop('hotel', '湖畔酒店', '08:00', 120.1, 30.1);
  const second = stop('trail', '北岸步道', '10:00', 120.12, 30.105);
  const dayTwo = stop('museum', '市立博物馆', '09:00', 120.13, 30.12);
  const gallery = stop('gallery', '市立美术馆', '14:00', 120.14, 30.13);
  const crowded = Array.from({ length: 6 }, (_, index) =>
    stop(`visit-${index}`, `湖畔历史文化保护区游览地点第 ${index + 1} 号`, `1${index}:30`, 120.12, 30.105));
  const unlocated = stop('unlocated', '地点待确认的老街', '18:00');
  const segment = {
    origin_item_id: first.item_id, destination_item_id: second.item_id, mode: 'WALK', paths: ['120.1,30.1;120.12,30.105'],
  };
  const sheet = {
    title: '湖畔旅行', dates: ['2026-11-16', '2026-11-17'], travelers: 2,
    transport: { outbound: {}, return: {} }, budget: { items: [] }, currency: 'CNY',
    days: [{ day: 1, items: [second, first, ...crowded, unlocated] }, { day: 2, items: [dayTwo, gallery] }],
    routes: [{ ...segment, travel_mode: 'WALK', legs: [{ instruction: '沿湖步行' }] }],
  };
  let releaseFailure;
  const pendingFailure = new Promise(resolve => { releaseFailure = resolve; });
  let generationRequests = 0;
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/sheet')) return route.fulfill({ json: { sheet } });
    if (url.pathname.endsWith('/roadbook')) {
      assert.equal(route.request().method(), 'POST');
      if (++generationRequests === 1) return pendingFailure.then(() =>
        route.fulfill({ status: 502, json: { detail: '路书概述生成失败，请重试。' } }));
      return route.fulfill({ json: { summaries: { 1: '沿湖走访北岸步道。', 2: '参观市立博物馆。' } } });
    }
    if (url.pathname.endsWith('/roadbook/map')) return route.fulfill({ json: {
      days: { 1: { segments: [segment] }, 2: { segments: [{
        origin_item_id: dayTwo.item_id, destination_item_id: 'gallery', mode: 'TRANSIT',
        paths: ['120.13,30.12;120.14,30.13'],
      }] } },
    } });
    if (url.pathname.includes('/place-photos/')) return route.fulfill({
      contentType: 'image/png',
      body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+sL1sAAAAASUVORK5CYII=', 'base64'),
    });
    if (url.pathname.endsWith('/planning/map')) {
      assert.equal(url.searchParams.get('sheet'), 'true');
      if (url.searchParams.get('day') === '2') return route.fulfill({ status: 502, json: { message: '地图暂时不可用' } });
      return route.fulfill({ json: {
        points: [{ item_id: second.item_id, location: '120.12,30.105' }, { item_id: first.item_id, location: '120.1,30.1' }],
        segments: [segment],
        basemap: {
          center: '120.11,30.1025', zoom: 14, width: 800, height: 440,
          image: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+sL1sAAAAASUVORK5CYII=',
        },
      } });
    }
    return route.fulfill({ json: { plans: [] } });
  });
  const generating = page.waitForRequest(request => new URL(request.url()).pathname.endsWith('/roadbook'));
  await page.goto('http://travel.test/plans/lake/sheet');
  await generating;
  await page.getByRole('status').filter({ hasText: '正在准备路书' }).waitFor();
  assert.equal(await page.getByRole('heading', { name: '旅程总览' }).count(), 0);
  releaseFailure();
  await page.getByRole('alert').filter({ hasText: '每日概述生成失败' }).waitFor();
  assert.equal(await page.getByRole('heading', { name: '旅程总览' }).count(), 0);
  await page.getByRole('button', { name: '重新读取' }).click();
  await page.getByText('沿湖走访北岸步道。').waitFor();
  await page.getByRole('button', { name: '查看第 2 天概述' }).press('Enter');
  await page.getByText('参观市立博物馆。').waitFor();
  const overview = page.getByRole('group', { name: /全程路线地图/ });
  const routeColors = await overview.locator('[data-roadbook-day]').evaluateAll(lines => lines.map(line => line.getAttribute('stroke')));
  assert.equal(new Set(routeColors).size, 2);
  await page.getByRole('button', { name: '查看第 1 天概述' }).press('Enter');
  await page.getByRole('button', { name: '预览第 1 天 →' }).click();
  const map = page.getByRole('group', { name: '第 1 天路线图' });
  await map.locator('image').waitFor({ state: 'attached' });
  const labels = map.locator('.sheet-map-label .sheet-map-name');
  assert.deepEqual(new Set(await labels.allTextContents()), new Set([first, second, ...crowded, unlocated].map(item => item.title)));
  assert.equal(await map.locator('.sheet-map-pin').count(), 8);
  assert.equal(await page.getByRole('heading', { name: '行程检查', exact: true }).count(), 0);
  assert.equal(await map.locator('polyline[stroke-dasharray]').count(), 1);
  assert.equal(await map.locator('.lucide-footprints').count(), 1);
  await map.getByRole('img', { name: '湖畔酒店的照片' }).waitFor();
  await map.getByRole('button', { name: /1 湖畔酒店/ }).press('Enter');
  await page.getByText('注意：湖畔酒店游览提示').waitFor();
  await page.getByText('沿湖步行', { exact: true }).waitFor();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '保存为图片' }).click();
  const png = await download;
  assert.match(png.suggestedFilename(), /湖畔旅行.*第 1 天.*\.png$/);
  const bytes = await readFile(await png.path());
  assert.equal(bytes.toString('hex', 0, 8), '89504e470d0a1a0a');
  assert.equal(bytes.readUInt32BE(16), 1600);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal((await labels.all()).length, 9);
  for (const label of await labels.all()) assert(await label.isVisible());
  const boxes = await map.locator('.sheet-map-label rect').evaluateAll(rectangles =>
    rectangles.map(rectangle => rectangle.getBoundingClientRect().toJSON()));
  const bounds = await map.boundingBox();
  for (const [index, box] of boxes.entries()) {
    assert(box.left >= bounds.x && box.right <= bounds.x + bounds.width);
    assert(box.top >= bounds.y && box.bottom <= bounds.y + bounds.height);
    for (const next of boxes.slice(index + 1)) {
      assert(box.right <= next.left || box.left >= next.right || box.bottom <= next.top || box.top >= next.bottom);
    }
  }
  await page.getByRole('button', { name: '第 2 天', exact: true }).click();
  await page.getByText('路线地图读取失败', { exact: false }).waitFor();
  await page.getByRole('group', { name: '第 2 天路线图' }).getByRole('button', { name: /1 市立博物馆/ }).waitFor();
  assert.equal(await map.count(), 0);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  assert.deepEqual(errors, []);
  console.log('PASS sheet map order, keyboard details, day switching, fallback, mobile and PNG export');
} finally {
  await browser.close();
}
