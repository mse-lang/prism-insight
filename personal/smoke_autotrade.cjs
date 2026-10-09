// Isolated development check only. KIS authentication and live start are mocked.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

async function main() {
  const url = process.argv[2] || 'http://127.0.0.1:8867';
  const output = process.argv[3] || path.join(process.cwd(), 'runtime', 'personal', 'autotrade-test.png');
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(url + '/#autotrade');
  await page.locator('#auto-symbols').filter({ visible: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('#auto-budget').value !== '');
  assert.match(await page.locator('#auto-status-title').textContent(), /정지/);
  await page.locator('#auto-budget').fill('100000');
  await page.locator('#auto-daily-buy').fill('300000');
  await page.locator('#auto-symbols').fill('005930, 000660');
  await page.locator('#auto-save-button').click();
  await page.waitForFunction(() => !document.querySelector('#auto-start-button').disabled);
  const beforeCheck = await (await page.request.get(url + '/api/state')).json();
  await page.locator('#auto-check-button').click();
  await page.waitForFunction(() => document.querySelector('#auto-preview').textContent.includes('005930'));
  const initial = await (await page.request.get(url + '/api/state')).json();
  assert.equal(initial.orders.length, beforeCheck.orders.length, 'read-only preview must not create orders');
  await page.locator('#auto-start-button').click();
  await page.waitForFunction(() => document.querySelector('#auto-status-title').textContent.includes('실행 중'));
  await page.waitForFunction(() => document.querySelector('#auto-events').textContent.includes('시작'));
  await page.locator('#auto-stop-button').click();
  await page.waitForFunction(() => document.querySelector('#auto-status-title').textContent.includes('정지'));
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#auto-budget').value === '100000');
  assert.match(await page.locator('#auto-status-title').textContent(), /정지/);
  await page.locator('#auto-mode').selectOption('kis-live');
  await page.locator('#auto-save-button').click();
  await page.waitForFunction(() => document.querySelector('#auto-mode-heading').textContent.includes('실계좌'));
  assert.equal(await page.locator('#auto-start-button').isDisabled(), true, 'unconnected live account cannot start');

  const state = await (await page.request.get(url + '/api/autotrade')).json();
  let fake = { ...state, connection: { configured: true, ready: true, environment: 'live', account_masked: '12****78-01', message: '가상 연결 검증' } };
  const liveRequests = [];
  await page.route('**/api/autotrade', route => route.fulfill({ json: fake }));
  await page.route('**/api/autotrade/connect', route => route.fulfill({ json: fake }));
  await page.route('**/api/autotrade/start', route => {
    liveRequests.push(route.request().postDataJSON());
    fake = { ...fake, running: true, message: '가상 시작 검증' };
    return route.fulfill({ json: fake });
  });
  // No broker request occurs: all these calls stay inside the test browser.
  await page.locator('#broker-environment').selectOption('live');
  await page.locator('#broker-app-key').fill('fake-ui-key');
  await page.locator('#broker-app-secret').fill('fake-ui-secret');
  await page.locator('#broker-account-no').fill('12345678');
  await page.locator('#broker-product-code').fill('01');
  await page.locator('#broker-connect-form button[type="submit"]').click();
  await page.waitForFunction(() => !document.querySelector('#auto-start-button').disabled);
  assert.equal(await page.locator('#broker-app-key').inputValue(), '');
  assert.equal(await page.locator('#broker-app-secret').inputValue(), '');
  await page.locator('#auto-start-button').click();
  await page.locator('#live-dialog').waitFor({ state: 'visible' });
  assert.match(await page.locator('#live-confirm-details').textContent(), /12\*\*\*\*78-01/);
  assert.equal(await page.locator('#live-confirm-button').isDisabled(), true);
  assert.equal(liveRequests.length, 0);
  await page.locator('#live-confirm-checkbox').check();
  await page.locator('#live-confirm-button').click();
  await page.locator('#live-dialog').waitFor({ state: 'hidden' });
  assert.deepEqual(liveRequests, [{ confirm_live: true, review_token: state.review_token }]);

  let checkEntered = false, stopRequests = 0;
  await page.route('**/api/autotrade/check', async route => {
    checkEntered = true;
    await new Promise(resolve => setTimeout(resolve, 1500));
    await route.fulfill({ json: fake });
  });
  await page.route('**/api/autotrade/stop', route => {
    stopRequests++;
    fake = { ...fake, running: false, message: '가상 중지 검증' };
    return route.fulfill({ json: fake });
  });
  await page.locator('#auto-check-button').click();
  await page.waitForFunction(() => document.querySelector('#auto-check-button').disabled);
  assert.equal(checkEntered, true);
  assert.equal(await page.locator('#auto-stop-button').isDisabled(), false, 'stop must remain usable during a long check');
  await page.locator('#auto-stop-button').click();
  await page.waitForFunction(() => document.querySelector('#auto-status-title').textContent.includes('정지'));
  assert.equal(stopRequests, 1);

  await page.unrouteAll({ behavior: 'wait' });
  const realState = await (await page.request.get(url + '/api/state')).json();
  assert.equal(realState.autotrade.connection.configured, false, 'mock authentication must not reach the real server');
  assert.equal(realState.autotrade.running, false);
  const headers = { 'X-CSRF-Token': realState.csrf_token };
  await page.request.post(url + '/api/autotrade/config', { headers, data: { mode: 'paper' } });
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#auto-mode-heading').textContent.includes('로컬'));
  await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
  fs.mkdirSync(path.dirname(output), { recursive: true });
  await page.screenshot({ path: output, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#auto-budget').value === '100000');
  const width = await page.evaluate(() => ({ body: document.documentElement.scrollWidth, viewport: innerWidth }));
  assert.ok(width.body <= width.viewport + 2, `mobile overflow: ${JSON.stringify(width)}`);
  await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
  await page.screenshot({ path: output.replace(/\.png$/, '-mobile.png'), fullPage: true });
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('Automation UI passed: persistence, read-only preview, local start/stop, disconnected-live guard, mocked connection + explicit live confirmation, desktop/mobile. No broker order was sent.');
}
main().catch(error => { console.error(error.stack); process.exit(1); });
