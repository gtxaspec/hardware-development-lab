#!/usr/bin/env node
/* Does clicking a relay leave the button showing the relay's real state?
 *
 * The panel serves state from a background sampler, so a poll can carry a
 * picture of the bench taken before the click it was meant to confirm. Applied
 * blindly that repaints the button grey, the relay looks unswitched, and the
 * obvious response is to click again and switch it back off. Nothing short of
 * a real browser catches it: the server is correct throughout, and the bug
 * only appears when a stale poll lands in the window after a write.
 *
 * Needs Playwright, and needs to be told a relay it is allowed to switch,
 * because there is no safe guess on someone else's bench:
 *
 *   PANEL=http://opi:8080 RELAY_PANEL_TEST_RELAY=2:15 ./test-ui.js
 *
 * require() resolves from this file's directory, so if playwright lives
 * somewhere else point RELAY_PANEL_PLAYWRIGHT at it. Skips rather than fails
 * when anything is missing, so it never breaks a run that cannot use it.
 */
let chromium;
try {
  ({ chromium } = require(process.env.RELAY_PANEL_PLAYWRIGHT || 'playwright'));
} catch {
  console.log('SKIP: playwright not found (set RELAY_PANEL_PLAYWRIGHT to its path)');
  process.exit(0);
}

const URL = process.env.PANEL || process.env.RELAY_PANEL_URL;
const TARGET = process.env.RELAY_PANEL_TEST_RELAY;
if (!URL || !TARGET) {
  console.log('SKIP: set PANEL and RELAY_PANEL_TEST_RELAY=<board>:<relay> ' +
              '(a relay with nothing wired to it)');
  process.exit(0);
}
const [board, relay] = TARGET.split(':');
const SEL = `.relay-card[data-board="${board}"][data-relay="${relay}"]`;

const isOn = (page) => page.$eval(SEL, el => el.classList.contains('is-on'));

// Sample every 50 ms and compress into runs, so a momentary revert cannot
// hide between two samples.
async function watch(page, ms) {
  const t0 = Date.now(), seen = [];
  while (Date.now() - t0 < ms) {
    const on = await isOn(page);
    const last = seen[seen.length - 1];
    if (!last || last.on !== on) seen.push({ on, at: Date.now() - t0 });
    await page.waitForTimeout(50);
  }
  return seen;
}
const fmt = (t) => t.map(s => `${s.on ? 'GREEN' : 'grey '}@${s.at}ms`).join('  ->  ');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(URL, { waitUntil: 'networkidle' });
  await page.waitForSelector(SEL);

  let fails = 0;
  const check = (name, ok, detail) => {
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}${detail ? '  ' + detail : ''}`);
    if (!ok) fails++;
  };

  if (await isOn(page)) { await page.click(SEL); await page.waitForTimeout(2500); }
  check('starts grey', !(await isOn(page)));

  // Wait for the optimistic paint before watching. Sampling from the instant
  // of the click races the request round trip, and an initial grey there means
  // only that the answer has not arrived yet - not that anything reverted.
  const goGreen = () => page.waitForFunction(
    sel => document.querySelector(sel).classList.contains('is-on'), SEL,
    { timeout: 5000 });

  console.log(`\n--- click ${board}/${relay} on, then watch for 3.5 s ---`);
  await page.click(SEL);
  let painted = true;
  try { await goGreen(); } catch { painted = false; }
  check('turns green', painted);
  let tl = await watch(page, 3500);
  console.log('  ' + fmt(tl));
  check('stays green, never reverts', tl.every(s => s.on),
        `(${tl.filter(s => !s.on).length} revert(s) to grey)`);

  // Control, done deterministically rather than by racing the poll: hand the
  // page a snapshot that predates the click and see whether it gets in. Racing
  // for it is unreliable now that the panel also re-polls quickly after a
  // write, and a control that only sometimes fires proves nothing on the runs
  // where it does not.
  const staleOff = () => {
    const snap = JSON.parse(JSON.stringify(state));
    snap.covers_writes = 0;              // taken before our write
    for (const b of snap.boards) for (const r of b.relays) r.state = 'off';
    return snap;
  };

  console.log('\n--- control: hand the page a pre-click snapshot ---');
  const refused = await page.evaluate(`(${staleOff})(), applySnapshot((${staleOff})())`);
  check('a pre-click snapshot is refused', refused === false, `(returned ${refused})`);
  check('so the button is still green', await isOn(page));

  const gotIn = await page.evaluate(
    `needWriteSeq = 0, applySnapshot((${staleOff})())`);
  check('without the barrier the same snapshot gets in', gotIn === true,
        `(returned ${gotIn})`);
  check('and the button goes grey - the bug you reported', !(await isOn(page)));

  // Put the relay back through the API, not the page: the control just fed
  // the page a snapshot claiming everything is off, so asking the button what
  // state it is in would leave the relay switched on and the run looking clean.
  await fetch(`${URL}/api/relay/set`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ board: Number(board), relay: Number(relay), state: false }),
  });
  const after = await (await fetch(`${URL}/api/state?fresh=1`)).json();
  const left = after.boards[Number(board)].relays
    .find(r => r.num === Number(relay)).state;
  check('relay left off on the bench', left === 'off', `(reads ${left})`);

  await browser.close();
  console.log(fails ? `\nFAILED (${fails})` : '\nall passed');
  process.exit(fails ? 1 : 0);
})();
