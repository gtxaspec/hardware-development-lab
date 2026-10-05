#!/usr/bin/env node
/* UART browser regressions; starts demo.py and its simulated TCP consoles. */
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

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const port = index => `[data-uart="${index}"]`;
let demo, browser, panel, passed = 0, failed = 0;

async function open(page) {
  await page.click('#uartSection > summary');
  await page.waitForFunction(() => document.querySelectorAll('[data-uart]').length === 25);
}

async function output(page, pattern) {
  await page.waitForFunction(pattern => new RegExp(pattern).test(document.getElementById('uartOutput').textContent), pattern);
}

async function connected(page) {
  await page.waitForFunction(() => document.getElementById('uartConnection').textContent === 'Connected');
}

// Every body POSTed to the input endpoint, in the order the page sent them.
function inputs(page) {
  const sent = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/uart/input')) sent.push(request.postDataJSON());
  });
  return sent;
}

async function test(name, run) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
  const errors = [];
  context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
  const page = await context.newPage();
  try {
    await page.goto(panel, { waitUntil: 'networkidle' });
    await run(page);
    assert.deepEqual(errors, [], 'unhandled browser errors');
    passed++;
    console.log(`PASS ${name}`);
  } catch (error) {
    failed++;
    console.error(`FAIL ${name}: ${error.stack}`);
  } finally {
    await context.close();
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

  await test('on-demand streaming, switching, and all 24 labeled consoles', async page => {
    const requests = [];
    page.on('request', request => {
      if (request.url().includes('/api/uart/stream')) requests.push(request.url());
    });
    await open(page);
    assert.equal(requests.length, 0, 'listing UARTs must not open a stream');
    await page.click(port(0));
    await output(page, 'ttyUSB0 demo: console output');
    assert.equal(await page.locator('#uartConnection').textContent(), 'Connected');
    assert.equal((await page.locator('#uartOutput').textContent()).includes('\x1b'), false);
    await page.click(port(1));
    await output(page, 'ttyUSB1 demo: console output');
    assert.equal((await page.locator('#uartOutput').textContent()).includes('ttyUSB0'), false);
    await page.click(port('all'));
    await output(page, '\\[ttyUSB23\\] ttyUSB23 demo: console output');
    await output(page, '\\[ttyUSB0\\] ttyUSB0 demo: console output');
    assert.equal(await page.locator('#uartConnection').textContent(), '24 / 24 connected');
    assert.equal(requests.length, 3, 'All UARTs uses one HTTP stream');
  });

  await test('disconnect, close, reconnect, and clear', async page => {
    await open(page);
    await page.click(port(2));
    await output(page, 'ttyUSB2 demo');
    await page.click('#uartConnect');
    await pause(200); // let the last batched paint finish
    const retained = await page.locator('#uartOutput').textContent();
    await pause(700);
    assert.equal(await page.locator('#uartOutput').textContent(), retained);
    await page.click('#uartClear');
    assert.equal(await page.locator('#uartOutput').textContent(), '');
    await page.click('#uartConnect');
    await output(page, 'ttyUSB2 demo');
    await page.click('#uartSection > summary');
    await page.waitForFunction(() => uartSession === null);
    await open(page);
    assert.equal(await page.locator('#uartConnect').textContent(), 'Connect');
    await page.click('#uartConnect');
    await page.waitForFunction(() => document.getElementById('uartConnection').textContent === 'Connected');
  });

  await test('console focus cannot trigger relay shortcuts and polling preserves output', async page => {
    await open(page);
    await page.click(port(3));
    await output(page, 'ttyUSB3 demo');
    let writes = 0;
    page.on('request', request => { if (['POST', 'PUT', 'DELETE'].includes(request.method())) writes++; });
    await page.focus('#uartOutput');
    await page.keyboard.type('aze');
    await page.keyboard.press('Enter');
    const preserved = await page.evaluate(async () => {
      const before = uartOutput.textContent;
      const node = uartOutput;
      await fetchState(true);
      return node === document.getElementById('uartOutput') && uartOutput.textContent.startsWith(before);
    });
    assert.equal(preserved, true);
    assert.equal(writes, 0);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'uartOutput');
  });

  await test('network failure retries and access denial stops retrying', async page => {
    let attempts = 0;
    await page.route('**/api/uart/stream?*', async route => {
      if (++attempts === 1) await route.abort('failed');
      else await route.continue();
    });
    await open(page);
    await page.click(port(4));
    await output(page, 'ttyUSB4 demo');
    assert.equal(attempts, 2);
    await page.unroute('**/api/uart/stream?*');
    let denials = 0;
    await page.route('**/api/uart/stream?*', route => {
      denials++;
      return route.fulfill({ status: 403, json: { error: 'Forbidden' } });
    });
    await page.click(port(5));
    await page.waitForFunction(() => document.getElementById('uartConnection').textContent === 'UART access denied');
    await pause(2200);
    assert.equal(denials, 1);
    assert.equal(await page.evaluate(() => uartSession), null);
    assert.equal(await page.locator('#tokenBar').isVisible(), true);
  });

  await test('terminal text, partial lines, per-UART escapes, and bounded scrollback', async page => {
    const result = await page.evaluate(() => {
      const log = new UartLog();
      log.feed(0, '\x1b[3'); log.feed(1, 'other\n');
      log.feed(0, '2mhello\x1b[0m\rH\npro'); log.feed(0, 'mpt> ');
      const partial = log.text(true);
      const controls = new UartLog();
      controls.feed(0, '\x1b]title'); controls.feed(0, '\x07\x1b(B12\b3\t€');
      const terminal = controls.text(false);
      const rows = new UartLog();
      rows.feed(0, Array.from({ length: 2500 }, (_, i) => `line${i}\n`).join(''));
      const bounded = new UartLog();
      bounded.feed(0, ('x'.repeat(1000) + '\n').repeat(300));
      const long = new UartLog(); long.feed(0, 'x'.repeat(12000));
      return { partial, terminal, rowCount: rows.lines.length, first: rows.text(false).split('\n')[0],
        trimmed: rows.trimmed, chars: bounded.size, long: long.size };
    });
    assert.equal(result.partial, '[ttyUSB1] other\n[ttyUSB0] Hello\n[ttyUSB0] prompt> ');
    assert.equal(result.terminal, '13      €');
    assert.equal(result.rowCount, 2000);
    assert.equal(result.first, 'line500');
    assert.equal(result.trimmed, true);
    assert.ok(result.chars <= 200000);
    assert.ok(result.long <= 4096);
    await open(page);
    await page.evaluate(() => {
      document.getElementById('uartConsole').hidden = false;
      uartLog.feed(0, '<img src=x onerror="window.uartInjected=true">'); paintUartOutput();
    });
    assert.equal(await page.locator('#uartOutput img').count(), 0);
    assert.equal(await page.evaluate(() => window.uartInjected), undefined);
    assert.match(await page.locator('#uartOutput').textContent(), /<img src=x/);
  });

  await test('follow output and narrow-screen layout', async page => {
    await open(page);
    await page.click(port('all'));
    await output(page, 'ttyUSB23 demo');
    await page.click('#uartConnect');
    await page.evaluate(() => {
      uartLog.feed(0, 'a long console line '.repeat(20) + '\n');
      uartLog.feed(0, 'scrollback\n'.repeat(100)); paintUartOutput();
    });
    assert.equal(await page.evaluate(() => uartOutput.scrollHeight - uartOutput.scrollTop - uartOutput.clientHeight < 2), true);
    await page.evaluate(() => { uartOutput.scrollTop = 0; });
    await page.waitForFunction(() => !document.getElementById('uartFollow').checked);
    await page.evaluate(() => { uartLog.feed(0, 'next line\n'); paintUartOutput(); });
    assert.equal(await page.evaluate(() => uartOutput.scrollTop), 0);
    await page.check('#uartFollow');
    assert.equal(await page.evaluate(() => uartOutput.scrollHeight - uartOutput.scrollTop - uartOutput.clientHeight < 2), true);
    await page.locator('#uartSection').screenshot({ path: '/tmp/relay-uart-desktop.png' });
    await page.setViewportSize({ width: 375, height: 812 });
    const fits = await page.evaluate(() => {
      const box = document.getElementById('uartSection').getBoundingClientRect();
      return box.left >= 0 && box.right <= innerWidth && document.documentElement.scrollWidth <= innerWidth;
    });
    assert.equal(fits, true, 'long console lines must not widen the page');
    await page.locator('#uartSection').screenshot({ path: '/tmp/relay-uart-mobile.png' });
  });

  await test('pop-out keeps its console and scrollback after the main panel closes', async page => {
    await open(page);
    await page.click(port(6));
    await output(page, 'ttyUSB6 demo');
    await page.evaluate(() => { uartLog.feed(6, 'saved before pop-out\n'); paintUartOutput(); });
    const requests = [];
    page.context().on('request', request => requests.push({ page: request.frame().page(),
      url: request.url(), method: request.method() }));
    const opened = page.waitForEvent('popup');
    await page.click('#uartPopout');
    const popup = await opened;
    await output(popup, 'saved before pop-out');
    await popup.waitForFunction(() => document.getElementById('uartConnection').textContent === 'Connected');
    assert.equal(new URL(popup.url()).search, '?console=6');
    assert.match(await popup.title(), /^ttyUSB6 · UART console/);
    assert.equal(await popup.evaluate(() => window.opener), null);
    assert.equal(await popup.locator('.header').isVisible(), false);
    assert.equal(await popup.locator('#uartPorts').isVisible(), false);
    assert.equal(await popup.locator('#uartPopout').isVisible(), false);
    await page.click(port(7));
    await output(page, 'ttyUSB7 demo');
    assert.equal(await popup.locator('#uartName').textContent(), 'ttyUSB6');
    await page.close();
    const length = (await popup.locator('#uartOutput').textContent()).length;
    await popup.waitForFunction(length => uartOutput.textContent.length > length, length);
    await popup.evaluate(() => { document.body.tabIndex = -1; document.body.focus(); });
    await popup.keyboard.type('aze');
    await popup.reload({ waitUntil: 'domcontentloaded' });
    await output(popup, 'ttyUSB6 demo');
    assert.equal((await popup.locator('#uartOutput').textContent()).includes('saved before pop-out'), false,
      'scrollback handoff is consumed once');
    const consoleRequests = requests.filter(request => request.page === popup);
    assert.equal(consoleRequests.some(request => request.url.includes('/api/state')), false, 'pop-out does not poll hardware');
    assert.equal(consoleRequests.some(request => request.method !== 'GET'), false, 'console shortcuts never write hardware');
  });

  await test('All UARTs pop-out keeps its controls and fills a resized window', async page => {
    await open(page);
    await page.click(port('all'));
    await output(page, 'ttyUSB23 demo');
    const opened = page.waitForEvent('popup');
    await page.click('#uartPopout');
    const popup = await opened;
    await output(popup, '\\[ttyUSB23\\] ttyUSB23 demo');
    await popup.waitForFunction(() => document.getElementById('uartConnection').textContent === '24 / 24 connected');
    assert.match(await popup.title(), /^All UARTs · UART console/);
    await popup.click('#uartConnect');
    await pause(200);
    await popup.click('#uartClear');
    assert.equal(await popup.locator('#uartOutput').textContent(), '');
    await popup.click('#uartConnect');
    await output(popup, '\\[ttyUSB0\\] ttyUSB0 demo');
    for (const size of [{ width: 1000, height: 650 }, { width: 390, height: 600 }]) {
      await popup.setViewportSize(size);
      const layout = await popup.evaluate(() => {
        const box = uartOutput.getBoundingClientRect();
        return { height: box.height, fits: box.left >= 0 && box.right <= innerWidth && box.bottom <= innerHeight,
          scrollsPage: document.documentElement.scrollHeight > innerHeight + 1 };
      });
      assert.equal(layout.fits, true);
      assert.equal(layout.scrollsPage, false);
      assert.ok(layout.height > size.height - 200, 'console uses the window height');
      await popup.screenshot({ path: `/tmp/relay-uart-popout-${size.width}.png` });
    }
  });

  await test('blocked pop-out leaves the embedded stream running', async page => {
    await open(page);
    await page.click(port(8));
    await output(page, 'ttyUSB8 demo');
    await page.evaluate(() => { window.open = () => null; });
    await page.click('#uartPopout');
    assert.match(await page.locator('#toasts').textContent(), /Allow pop-ups/);
    const length = (await page.locator('#uartOutput').textContent()).length;
    await page.waitForFunction(length => uartOutput.textContent.length > length, length);
    assert.equal(await page.locator('#uartConnection').textContent(), 'Connected');
  });

  await test('standalone console can recover from an access-token prompt', async page => {
    const popup = await page.context().newPage();
    const requests = [];
    popup.on('request', request => requests.push(request.url()));
    await popup.route('**/api/uarts', route => route.request().headers().authorization === 'Bearer test-console-access'
      ? route.fulfill({ json: { uarts: [{ index: 9, name: 'ttyUSB9', port: 3009 }], read_only: true } })
      : route.fulfill({ status: 403, json: { error: 'Forbidden' } }));
    await popup.route('**/api/uart/stream?*', route => {
      assert.equal(route.request().headers().authorization, 'Bearer test-console-access');
      return route.fulfill({ contentType: 'text/event-stream', body:
        'event: ready\ndata: {"uarts":[9]}\n\nevent: output\ndata: {"uart":9,"text":"authorized console\\n"}\n\n' });
    });
    await popup.goto(panel + '?console=9');
    await popup.waitForSelector('#tokenBar', { state: 'visible' });
    assert.match(await popup.locator('#tokenMsg').textContent(), /view consoles/);
    await popup.fill('#tokenInput', 'test-console-access');
    await popup.click('#tokenBar button:has-text("Save")');
    await output(popup, 'authorized console');
    assert.equal(requests.some(url => url.includes('/api/state') || url.includes('test-console-access')), false);
    await popup.goto(panel + '?console=invalid');
    assert.match(await popup.locator('#uartHelp').textContent(), /Invalid UART/);
  });

  await test('consoles open read only, and interactive never outlives its console', async page => {
    const sent = inputs(page);
    await open(page);
    await page.click(port(3));
    await connected(page);
    assert.equal(await page.isVisible('#uartInteractiveLabel'), true);
    assert.equal(await page.isChecked('#uartInteractive'), false);
    assert.equal(await page.isHidden('#uartInput'), true);
    await page.focus('#uartOutput');
    await page.keyboard.type('reboot');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Control+c');
    await page.check('#uartInteractive');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'uartInput');
    const opened = page.waitForEvent('popup');
    await page.click('#uartPopout');
    const popup = await opened;
    await connected(popup);
    assert.equal(await popup.isChecked('#uartInteractive'), false, 'a pop-out starts read only');
    await popup.close();
    await page.click(port(4));
    await connected(page);
    assert.equal(await page.isChecked('#uartInteractive'), false, 'switching UART kept typing on');
    await page.click(port(3));
    await connected(page);
    assert.equal(await page.isChecked('#uartInteractive'), false);
    await page.check('#uartInteractive');
    await page.click('#uartConnect');
    assert.equal(await page.isChecked('#uartInteractive'), false);
    assert.equal(await page.isDisabled('#uartInteractive'), true);
    await page.click(port('all'));
    assert.equal(await page.isVisible('#uartInteractiveLabel'), false, 'All UARTs has no single target');
    await pause(300);
    assert.deepEqual(sent, [], 'read-only consoles sent input');
  });

  await test('typing reaches the console in order, with line edits and control keys', async page => {
    const sent = inputs(page);
    await open(page);
    await page.click(port(5));
    await connected(page);
    await page.check('#uartInteractive');
    await page.keyboard.type('helo');
    await page.keyboard.press('Backspace');
    await page.keyboard.type('p');
    // The echo of the backspace is \b ESC[J; the trailing space is the cursor cell.
    await page.waitForFunction(() => uartOutput.textContent.split('\n').pop() === 'help ');
    assert.equal(await page.locator('#uartOutput .uart-cursor').count(), 1);
    await page.keyboard.press('Enter');
    await output(page, "demo: ran 'help'");
    await page.keyboard.type('ab');
    for (const key of ['ArrowLeft', 'ArrowUp', 'Tab', 'Escape', 'Delete', 'Control+a', 'Control+c']) {
      await page.keyboard.press(key);
    }
    await output(page, '\\^C');
    await page.waitForFunction(() => uartOutput.textContent.split('\n').pop() === 'demo$  ');
    assert.equal(sent.map(body => body.data).join(''),
      'helo\x7fp\rab\x1b[D\x1b[A\t\x1b\x1b[3~\x01\x03');
    assert.ok(sent.every(body => body.uart === 5));
    assert.equal(await page.evaluate(() => document.activeElement.id), 'uartInput', 'Tab must not leave the console');
    await page.keyboard.press('Shift+Tab');
    assert.notEqual(await page.evaluate(() => document.activeElement.id), 'uartInput', 'Shift+Tab must leave it');
  });

  await test('a selection keeps Ctrl+C for copying, and multi-line paste asks first', async page => {
    const sent = inputs(page);
    await open(page);
    await page.click(port(6));
    await output(page, 'ttyUSB6 demo');
    await page.check('#uartInteractive');
    // Dispatched rather than pressed: a repaint between selecting and pressing
    // would drop the selection and turn the copy into a real interrupt.
    const ctrlC = () => page.evaluate(() => uartOutput.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'c', ctrlKey: true, bubbles: true, cancelable: true })));
    const copied = await page.evaluate(() => {
      const range = document.createRange();
      range.selectNodeContents(uartOutput.firstChild);
      getSelection().removeAllRanges(); getSelection().addRange(range);
      return !uartOutput.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', ctrlKey: true, bubbles: true, cancelable: true }));
    });
    assert.equal(copied, false, 'Ctrl+C over a selection was taken from the browser');
    await page.evaluate(() => getSelection().removeAllRanges());
    await ctrlC();
    const dialogs = [];
    page.on('dialog', dialog => {
      dialogs.push(dialog.message());
      return dialogs.length === 1 ? dialog.dismiss() : dialog.accept();
    });
    const paste = text => page.evaluate(text => {
      const data = new DataTransfer(); data.setData('text/plain', text);
      uartInput.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
    }, text);
    await paste('echo 1\necho 2\n');
    await paste('echo 1\r\necho 2\n');
    await paste('uname\n');
    await page.keyboard.insertText('ls');
    const deadline = Date.now() + 4000;
    while (sent.map(body => body.data).join('') !== '\x03echo 1\recho 2\runame\rls' && Date.now() < deadline) await pause(50);
    assert.equal(sent.map(body => body.data).join(''), '\x03echo 1\recho 2\runame\rls');
    assert.equal(dialogs.length, 2, 'only multi-line pastes ask');
    assert.match(dialogs[0], /^Paste 2 lines into ttyUSB6\?/);
  });

  await test('refused input is dropped, never replayed, and view-only access hides the switch', async page => {
    await open(page);
    await page.click(port(7));
    await connected(page);
    await page.check('#uartInteractive');
    await page.route('**/api/uart/input', route => route.fulfill({ status: 409,
      json: { error: 'ttyUSB7 is not connected; open its console first' } }));
    await page.keyboard.type('rm');
    await page.waitForFunction(() => /not connected/.test(document.getElementById('toasts').textContent));
    await page.unroute('**/api/uart/input');
    const sent = inputs(page);
    await page.keyboard.type('x');
    await output(page, 'x');
    assert.deepEqual(sent.map(body => body.data), ['x'], 'input queued before the failure was replayed');
    await page.route('**/api/uarts', route => route.fulfill({ json: {
      uarts: [{ index: 7, name: 'ttyUSB7', port: 3007, input: false }], read_only: true } }));
    await page.click('#uartSection > summary');
    await page.click('#uartSection > summary');
    await page.waitForFunction(() => document.querySelectorAll('[data-uart]').length === 2);
    await page.click(port(7));
    await connected(page);
    assert.equal(await page.isVisible('#uartInteractiveLabel'), false);
    assert.match(await page.locator('#uartHint').textContent(), /^Output starts/);
  });

  await test('line-editor escapes rewrite the current line instead of piling up', async page => {
    const result = await page.evaluate(() => {
      const run = (...chunks) => {
        const log = new UartLog();
        for (const chunk of chunks) log.feed(0, chunk);
        const size = log.lines.reduce((total, row) => total + row.chars.length, 0);
        return { text: log.text(false), consistent: size === log.size, split: log.split(0) };
      };
      return {
        backspace: run('# lsx', '\b\x1b[', 'J'),
        history: run('# ls -la', '\r# pwd\x1b[J'),
        edits: run('abcdef\x1b[3D\x1b[P\x1b[2@XY\x1b[C\x1b[K'),
        eraseStart: run('hello world\x1b[6G\x1b[1K'),
        eraseChars: run('hello world\x1b[3G\x1b[3X'),
        eraseLine: run('old text\x1b[2Knew'),
        redraw: run('old text\r\x1b[2Knew'),
        screen: run('keep\x1b[?25l\x1b[2J\x1b[H\x1b[1;1H\x1b[6n\x1b[999C!'),
        back: run('ab\x1b[999D!'),
        cursor: run('one\ntwo\x1b[2D'),
        fresh: run('one\n'),
        empty: run(),
      };
    });
    assert.equal(result.backspace.text, '# ls');
    assert.equal(result.history.text, '# pwd');
    assert.equal(result.edits.text, 'abcXYe');
    assert.equal(result.eraseStart.text, '      world');
    assert.equal(result.eraseChars.text, 'he    world');
    assert.equal(result.eraseLine.text, '        new');
    assert.equal(result.redraw.text, 'new');
    assert.equal(result.screen.text, 'keep!');
    assert.equal(result.back.text, '!b');
    assert.deepEqual(result.cursor.split, ['one\nt', 'w', 'o']);
    assert.deepEqual(result.fresh.split, ['one\n', ' ', '']);
    assert.deepEqual(result.empty.split, ['', ' ', '']);
    for (const [name, outcome] of Object.entries(result)) assert.ok(outcome.consistent, `${name} miscounted its size`);
  });

  console.log(`\n${passed}/${passed + failed} UART browser regressions passed`);
  process.exitCode = failed ? 1 : 0;
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
