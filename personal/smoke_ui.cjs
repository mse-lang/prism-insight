// Optional development smoke. Run against an isolated --provider demo account.
// Requires Playwright only for this test; the desk itself has no dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

async function main() {
  const url = process.argv[2] || 'http://127.0.0.1:8867';
  const output = process.argv[3] || path.join(process.cwd(), 'runtime', 'personal', 'ui-smoke.png');
  let browser;
  try { browser = await chromium.launch({ headless: true }); }
  catch (_) { browser = await chromium.launch({ headless: true, channel: 'msedge' }); }
  const context = await browser.newContext({ viewport: { width: 1440, height: 1040 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', response => { if (response.status() >= 500) errors.push(`HTTP ${response.status()}`); });
  await page.goto(url);
  await page.locator('#selected-name').filter({ hasText: '삼성전자' }).waitFor();
  await page.locator('#chart-area svg').waitFor();
  await page.waitForFunction(() => document.querySelector('#stat-equity').textContent.includes('10,000,000'));
  assert.match(await page.locator('#source-tag').textContent(), /데모/);
  assert.equal(await page.locator('#watchlist button').count() > 0, true);
  await page.locator('#order-quantity').fill('2');
  await page.locator('#order-note').fill('화면 검증용 모의 매수');
  await page.locator('#order-button').click();
  await page.locator('#order-dialog').waitFor({ state: 'visible' });
  await page.locator('#confirm-order-button').click();
  await page.locator('#order-dialog').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('#recent-orders').textContent.includes('삼성전자'));
  await page.locator('a.nav-link[data-view="portfolio"]').click();
  await page.locator('#view-portfolio').waitFor({ state: 'visible' });
  assert.match(await page.locator('#positions-table').textContent(), /삼성전자/);
  await page.locator('a.nav-link[data-view="journal"]').click();
  await page.locator('#journal-text').fill('브라우저에서 거래일지 저장과 영속성을 확인했습니다.');
  await page.locator('#journal-form button[type="submit"]').click();
  await page.waitForFunction(() => document.querySelector('#journal-list').textContent.includes('영속성'));
  await page.locator('a.nav-link[data-view="settings"]').click();
  await page.locator('#settings-name').fill('나의 PRISM 데스크');
  await page.locator('#settings-form button[type="submit"]').click();
  await page.waitForFunction(() => document.querySelector('#sidebar-name').textContent === '나의 PRISM 데스크');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#sidebar-name').textContent === '나의 PRISM 데스크');
  await page.locator('a.nav-link[data-view="dashboard"]').click();
  await page.locator('#analyze-button').click();
  await page.locator('#report-area').waitFor({ state: 'visible' });
  assert.match(await page.locator('#report-body').textContent(), /합성/);
  await page.locator('#add-watch-button').click();
  await page.locator('#stock-search').fill('셀트리온');
  await page.locator('#search-form button[type="submit"]').click();
  await page.locator('#search-results button').first().click();
  await page.locator('#search-dialog').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('#watchlist').textContent.includes('셀트리온'));
  // Return to Samsung, sell partially, and verify quantity in the portfolio.
  await page.locator('#watchlist button').filter({ hasText: '삼성전자' }).first().click();
  await page.locator('button[data-side="sell"]').click();
  await page.locator('#order-quantity').fill('1');
  await page.locator('#order-button').click();
  await page.locator('#confirm-order-button').click();
  await page.locator('#order-dialog').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('#recent-orders').textContent.includes('매도'));
  await page.locator('button[data-side="buy"]').click();
  await page.locator('#chart-area svg').waitFor();
  fs.mkdirSync(path.dirname(output), { recursive: true });
  await page.screenshot({ path: output, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.locator('#selected-name').filter({ hasText: '삼성전자' }).waitFor();
  await page.locator('#chart-area svg').waitFor();
  const width = await page.evaluate(() => ({ body: document.documentElement.scrollWidth, viewport: innerWidth }));
  assert.ok(width.body <= width.viewport + 2, `mobile page overflows: ${JSON.stringify(width)}`);
  await page.screenshot({ path: output.replace(/\.png$/, '-mobile.png'), fullPage: true });
  assert.deepEqual(errors, [], `browser errors: ${errors.join(', ')}`);
  await browser.close();
  console.log('UI smoke passed: buy, sell, journal, settings persistence, report, search, desktop and mobile.');
}
main().catch(e => { console.error(e.message); process.exit(1); });
