#!/usr/bin/env node
/* Browser regressions against demo.py only; never connects to a live bench.
 * Run: RELAY_PANEL_PLAYWRIGHT=/path/to/playwright node test-ui-reliability.js
 */
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
let chromium;
try {
  ({ chromium } = require(process.env.RELAY_PANEL_PLAYWRIGHT || 'playwright'));
} catch {
  console.log('SKIP: install playwright or set RELAY_PANEL_PLAYWRIGHT');
  process.exit(0);
}

const relay = '.relay-card[data-board="0"][data-relay="1"]';
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const checked = [];
let panel, browser, demo;

async function api(route, body) {
  const response = await fetch(panel + route, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  assert.equal(response.ok, true, `demo API ${route}: ${response.status}`);
  return response.json();
}

async function settled(page, on) {
  await page.waitForFunction(({ selector, on }) => {
    const card = document.querySelector(selector);
    return card && card.getAttribute('aria-busy') === 'false' &&
      !card.classList.contains('is-unverified') && card.classList.contains('is-on') === on;
  }, { selector: relay, on }, { timeout: 10000 });
}

async function waitFor(check) {
  for (let i = 0; i < 200; i++) {
    if (check()) return;
    await pause(20);
  }
  throw new Error('Timed out waiting for intercepted request');
}

function gate(releases) {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  releases.push(release);
  return { promise, release };
}

async function test(name, run) {
  await api('/api/relay/set', { board: 0, relay: 1, state: false });
  await api('/api/state?fresh=1');
  const page = await browser.newPage();
  const errors = [], releases = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(panel, { waitUntil: 'networkidle' });
    await settled(page, false);
    await run(page, releases);
    assert.deepEqual(errors, [], 'unhandled page errors');
    checked.push({ name, passed: true });
    console.log(`PASS ${name}`);
  } catch (error) {
    checked.push({ name, passed: false });
    console.error(`FAIL ${name}: ${error.stack}`);
  } finally {
    for (const release of releases) release();
    await page.close();
  }
}

(async () => {
  demo = spawn('python3', ['-u', path.join(__dirname, 'demo.py')], {
    cwd: __dirname, detached: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  panel = await new Promise((resolve, reject) => {
    let log = '';
    const timer = setTimeout(() => reject(new Error(`Demo did not start: ${log}`)), 20000);
    demo.stdout.on('data', chunk => {
      log += chunk;
      const match = log.match(/demo panel on (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
    demo.stderr.on('data', chunk => { log += chunk; });
    demo.once('error', error => { clearTimeout(timer); reject(error); });
    demo.once('exit', code => { clearTimeout(timer); reject(new Error(`Demo exited ${code}: ${log}`)); });
  });
  browser = await chromium.launch();

  await test('delayed reply shows pending and cannot send a second toggle', async (page, releases) => {
    const held = gate(releases), requests = [];
    await page.route('**/api/relay/set', async route => {
      requests.push(route.request().postDataJSON());
      const response = await route.fetch();
      await held.promise;
      await route.fulfill({ response });
    });
    await page.click(relay);
    await waitFor(() => requests.length === 1);
    assert.equal(await page.getAttribute(relay, 'aria-busy'), 'true');
    assert.equal(await page.locator(`${relay} .relay-state`).innerText(), 'On…');
    assert.match(await page.locator(`${relay} .relay-status-description`).textContent(), /Turning on/);
    assert.equal(await page.locator(`${relay} .relay-state`).isVisible(), true, 'PCB view shows pending text');
    await page.dispatchEvent(relay, 'click');
    await page.focus(relay);
    await page.keyboard.press('Enter');
    await pause(150);
    assert.deepEqual(requests, [{ board: 0, relay: 1, state: true }]);
    held.release();
    await settled(page, true);
    assert.equal(await page.getAttribute(relay, 'aria-label'), 'Camera 2');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.relay), '1');
  });

  await test('older command reply cannot overwrite a newer hardware observation', async (page, releases) => {
    const held = gate(releases);
    let accepted = false;
    await page.route('**/api/relay/set', async route => {
      const response = await route.fetch();
      accepted = true;
      await held.promise;
      await route.fulfill({ response });
    });
    await page.click(relay);
    await waitFor(() => accepted);
    await api('/api/relay/set', { board: 0, relay: 1, state: false });
    await page.evaluate(() => fetchState(true));
    held.release();
    await settled(page, false);
    assert.equal(await page.getAttribute(relay, 'aria-pressed'), 'false');
  });

  await test('stale snapshots remain rejected after the old four-second fallback', async page => {
    await page.click(relay);
    await settled(page, true);
    const accepted = await page.evaluate(() => {
      const old = JSON.parse(JSON.stringify(state));
      old.covers_writes = 0;
      old.boards[0].relays.forEach(relay => { relay.state = 'off'; });
      waitingSince = Date.now() - WRITE_WAIT_MS - 1;
      return applySnapshot(old);
    });
    assert.equal(accepted, false);
    assert.match(await page.locator(`${relay} .relay-status-description`).textContent(), /Unverified.*ON/);
    await page.evaluate(() => fetchState(true));
    await settled(page, true);
    const olderRead = await page.evaluate(() => {
      const old = JSON.parse(JSON.stringify(state));
      old.sample_seq--;
      old.boards[0].relays[1].state = 'off';
      return applySnapshot(old);
    });
    assert.equal(olderRead, false);
    await settled(page, true);
  });

  await test('failed hardware read retains last state and blocks an ambiguous click', async page => {
    await page.click(relay);
    await settled(page, true);
    let writes = 0;
    page.on('request', request => { if (request.method() === 'POST') writes++; });
    await page.evaluate(() => {
      const failed = JSON.parse(JSON.stringify(state));
      failed.boards[0].error = 'no response at 0x20 on bus 2';
      failed.boards[0].relays.forEach(relay => { relay.state = 'off'; });
      applySnapshot(failed);
    });
    assert.match(await page.locator(`${relay} .relay-status-description`).textContent(), /Unverified.*ON/);
    assert.equal(await page.getAttribute(relay, 'aria-pressed'), null);
    await page.dispatchEvent(relay, 'click');
    await settled(page, true);
    assert.equal(writes, 0);
  });

  await test('timeout reconciles a successful write without resending it', async (page, releases) => {
    const held = gate(releases);
    let accepted = false, writes = 0;
    await page.evaluate(() => {
      const original = api;
      api = (method, path, body, opts = {}) => original(method, path, body,
        { ...opts, timeout: method === 'POST' ? 350 : 5000 });
    });
    await page.route('**/api/state*', route => route.abort('failed'));
    await page.route('**/api/relay/set', async route => {
      writes++;
      const response = await route.fetch();
      accepted = true;
      await held.promise;
      await route.fulfill({ response }).catch(() => {}); // original request may be aborted
    });
    await page.click(relay);
    await waitFor(() => accepted);
    await page.waitForFunction(selector => {
      const card = document.querySelector(selector);
      return card.getAttribute('aria-busy') === 'false' && card.classList.contains('is-unverified');
    }, relay);
    assert.equal(writes, 1);
    await page.unroute('**/api/state*');
    await page.evaluate(() => fetchState(true));
    await settled(page, true);
    assert.equal(writes, 1);
    held.release();
  });

  await test('acknowledged command becomes unverified if readback never succeeds', async page => {
    await page.route('**/api/state*', async route => {
      const response = await route.fetch();
      const snapshot = await response.json();
      snapshot.boards[0].error = 'readback unavailable';
      snapshot.boards[0].relays.forEach(relay => { relay.state = 'off'; });
      await route.fulfill({ json: snapshot });
    });
    await page.click(relay);
    await page.waitForFunction(selector => document.querySelector(selector).getAttribute('aria-busy') === 'true', relay);
    await page.waitForFunction(selector => {
      const card = document.querySelector(selector);
      return card.getAttribute('aria-busy') === 'false' && card.classList.contains('is-unverified');
    }, relay, { timeout: 8000 });
    assert.match(await page.locator(`${relay} .relay-state`).innerText(), /^Unverified/);
    assert.equal(await page.getAttribute(relay, 'aria-pressed'), null);
    const hardware = await api('/api/state?fresh=1');
    assert.equal(hardware.boards[0].relays.find(relay => relay.num === 1).state, 'on');
    await page.unroute('**/api/state*');
    await page.evaluate(() => fetchState(true));
    await settled(page, true);
  });

  await test('an unavailable first reading has no invented last-known OFF state', async page => {
    const failed = await api('/api/state?fresh=1');
    failed.boards[0].error = 'readback unavailable';
    failed.boards[0].relays.forEach(relay => { relay.state = 'off'; });
    await page.route('**/api/state*', route => route.fulfill({ json: failed }));
    await page.reload({ waitUntil: 'networkidle' });
    assert.equal(await page.locator(`${relay} .relay-state`).innerText(), 'Unverified');
    assert.equal(await page.getAttribute(relay, 'aria-pressed'), null);
  });

  await test('restart resets counters and rejects replies from the old process', async (page, releases) => {
    const held = gate(releases);
    let accepted = false;
    await page.route('**/api/relay/set', async route => {
      const response = await route.fetch();
      accepted = true;
      await held.promise;
      await route.fulfill({ response });
    });
    await page.click(relay);
    await waitFor(() => accepted);
    const before = await page.evaluate(() => JSON.parse(JSON.stringify(state)));
    const restarted = { ...before, server_id: 'restarted-demo-process', sample_seq: 1, covers_writes: 0 };
    restarted.boards[0].relays.forEach(relay => { relay.state = 'off'; });
    await page.route('**/api/state*', route => route.fulfill({ json: restarted }));
    assert.equal(await page.evaluate(snapshot => applySnapshot(snapshot), restarted), true);
    held.release();
    await settled(page, false);
    await pause(300);
    assert.equal(await page.evaluate(() => needWriteSeq), 0);
    assert.equal(await page.evaluate(snapshot => applySnapshot(snapshot), before), false);
    await settled(page, false);
  });

  await test('polls, write nudges and fresh requests share one request at a time', async page => {
    let active = 0, peak = 0;
    const paths = [];
    await page.route('**/api/state*', async route => {
      active++;
      peak = Math.max(peak, active);
      paths.push(new URL(route.request().url()).search);
      try {
        await pause(600);
        const response = await route.fetch();
        await route.fulfill({ response });
      } finally { active--; }
    });
    await page.evaluate(() => {
      const normal = fetchState();
      for (let i = 0; i < 10; i++) pokePollSoon();
      return Promise.all([normal, fetchState(), fetchState(true), fetchState(true)]);
    });
    assert.equal(peak, 1);
    assert.deepEqual(paths, ['', '?fresh=1']);
  });

  await test('slider focus preserves nodes while the rest of the bench updates', async page => {
    let polls = 0;
    page.on('request', request => { if (request.url().includes('/api/state')) polls++; });
    await page.evaluate(() => {
      window.savedCard = document.querySelector('.relay-card[data-board="0"][data-relay="1"]');
      window.savedSlider = document.querySelector('.pwm-slider');
      savedSlider.focus();
    });
    await api('/api/relay/set', { board: 0, relay: 1, state: true });
    await settled(page, true);
    await page.waitForTimeout(1300);
    assert.ok(polls >= 2, `expected continued polling, got ${polls}`);
    assert.equal(await page.evaluate(() => savedCard.isConnected && savedSlider.isConnected &&
      document.activeElement === savedSlider), true);
  });

  await test('slider queue recovers after a network failure and keeps only the latest value', async (page, releases) => {
    let count = 0;
    const held = gate(releases), levels = [];
    await page.route('**/api/pwm/set', async route => {
      count++;
      if (count === 1) { await route.abort('failed'); return; }
      const level = route.request().postDataJSON().level;
      levels.push(level);
      const response = await route.fetch();
      if (level === 500) await held.promise;
      await route.fulfill({ response });
    });
    await page.evaluate(() => pwmSend(0, 0, 123));
    assert.equal(await page.evaluate(() => pwmInflight.size), 0);
    await page.evaluate(() => { window.sliderJob = pwmSend(0, 0, 500); });
    await waitFor(() => levels.length === 1);
    await page.evaluate(() => { pwmSend(0, 0, 600); pwmSend(0, 0, 700); });
    held.release();
    await page.evaluate(() => sliderJob);
    assert.deepEqual(levels, [500, 700]);
    assert.equal(await page.evaluate(() => pwmInflight.size), 0);
    await page.evaluate(() => fetchState(true));
    const hardware = await api('/api/state?fresh=1');
    assert.equal(hardware.pwm[0].channels.find(channel => channel.ch === 0).level, 700);
  });

  await test('inline editing survives a poll and Escape restores the control', async page => {
    await page.click('#editModeBtn');
    await page.click(relay);
    const input = page.locator(`${relay} .relay-name-input`);
    await input.fill('not saved');
    await page.evaluate(() => fetchState(true));
    assert.equal(await input.inputValue(), 'not saved');
    let saves = 0;
    page.on('request', request => { if (request.method() === 'PUT') saves++; });
    await input.press('Escape');
    await page.waitForFunction(selector => !document.querySelector(selector), `${relay} input`);
    assert.equal(await page.locator(`${relay} .relay-name`).innerText(), 'Camera 2');
    assert.equal(saves, 0);
  });

  await test('GPIO keyboard activation sends one explicit state and retains focus', async page => {
    const selector = '.gpio-card[data-gpio-wpi="27"]';
    const before = await page.getAttribute(selector, 'aria-pressed');
    const requests = [];
    page.on('request', request => {
      if (request.url().endsWith('/api/gpio/set')) requests.push(request.postDataJSON());
    });
    await page.focus(selector);
    await page.keyboard.press('Space');
    await page.waitForFunction(selector => document.querySelector(selector).getAttribute('aria-busy') === 'false', selector);
    assert.deepEqual(requests, [{ wpi: 27, state: before !== 'true' }]);
    assert.equal(await page.evaluate(selector => document.activeElement === document.querySelector(selector), selector), true);
  });

  const failures = checked.filter(result => !result.passed).length;
  console.log(`\n${checked.length - failures}/${checked.length} browser regressions passed`);
  process.exitCode = failures ? 1 : 0;
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (demo && demo.exitCode === null) {
    process.kill(-demo.pid, 'SIGINT');
    const deadline = setTimeout(() => {
      try { process.kill(-demo.pid, 'SIGKILL'); } catch (_) {}
    }, 5000);
    await new Promise(resolve => demo.once('exit', resolve));
    clearTimeout(deadline);
  }
});
