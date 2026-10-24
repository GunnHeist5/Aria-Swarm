// Screenshots every mockup screen at desktop and phone widths, then exercises the
// interactive features and runs the quality checks. Exits 1 if any check fails, so the
// overnight loop can reject a bad iteration without trusting the agent's own report.
// Usage: npm run shot
import { chromium } from 'playwright';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const outDir = resolve('shots');
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const url = pathToFileURL(resolve('mockup/index.html')).href;
const sizes = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'phone', width: 390, height: 844 },
];
const errors = []; // hard failures: the loop reverts the iteration if any are present
const notes = []; // information only

let baseline = { touchTargets: {} };
try {
  baseline = JSON.parse(readFileSync(resolve('scripts/baseline.json'), 'utf8'));
} catch {}

const browser = await chromium.launch();
try {
  for (const size of sizes) {
    const tag = `[${size.name}]`;
    const check = (ok, msg) => { if (!ok) errors.push(`${tag} ${msg}`); };
    const context = await browser.newContext({ viewport: { width: size.width, height: size.height } });
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(`${tag} ${e.message}`));
    // Resource failures are reported below by URL; webfonts are optional (offline falls back to system fonts).
    page.on('console', (m) => {
      if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(`${tag} console: ${m.text()}`);
    });
    page.on('requestfailed', (r) => {
      if (!/fonts\.(googleapis|gstatic)\.com/.test(r.url())) errors.push(`${tag} failed to load ${r.url()}`);
    });
    // The dark screen switcher is a review aid; hide it so it doesn't cover content in screenshots.
    const open = async (route) => {
      await page.goto('about:blank');
      await page.goto(`${url}#${route}`);
      await page.addStyleTag({ content: '.screens{display:none !important}' });
    };
    const finished = (label) =>
      page
        .waitForFunction(() => document.querySelector('#run-text')?.textContent.includes('agents'), null, { timeout: 20000 })
        .catch(() => errors.push(`${tag} ${label}: run did not finish within 20s`));
    const sideways = async (label) => {
      const s = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
      check(!s, `page scrolls sideways (${label})`);
    };
    const inViewport = (box) => box && box.x >= -1 && box.x + box.width <= size.width + 1;

    for (const route of ['home', 'trace', 'phone']) {
      await open(route);
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${outDir}/${size.name}-${route}.png`, fullPage: true });
      await sideways(route);
    }

    // Workspace: a fresh load starts the sample run.
    await open('workspace');
    await page.waitForTimeout(2500);
    const head = page.locator('#step-forecaster .step-head');
    if (await head.count()) await head.click();
    await page.screenshot({ path: `${outDir}/${size.name}-workspace-running.png`, fullPage: true });

    await finished('default run');
    await page.waitForTimeout(3000); // the final answer streams in block by block
    await page.screenshot({ path: `${outDir}/${size.name}-workspace-done.png`, fullPage: true });
    await sideways('workspace');

    // Touch targets: at least 44px tall. Inline number links are text, so they are exempt.
    const small = await page.evaluate(() =>
      [...document.querySelectorAll('button, a, input, select, textarea, [role=radio]')]
        .filter((e) => !e.matches('a.num') && !e.closest('.screens, [hidden]'))
        .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.height < 44; })
        .map((e) => `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}${e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).join('.') : ''}`),
    );
    const allowed = baseline.touchTargets?.[size.name] ?? 0;
    check(small.length <= allowed, `${small.length} touch targets under 44px (baseline ${allowed}): ${small.join(', ')}`);
    if (small.length) notes.push(`${tag} ${small.length} touch targets under 44px: ${small.join(', ')}`);

    // Number popover stays on screen.
    await page.locator('#answer a.num').first().hover();
    await page.waitForTimeout(400);
    const pop = page.locator('#numpop');
    check(await pop.isVisible(), 'number popover did not open on hover');
    if (await pop.isVisible()) {
      check(inViewport(await pop.boundingBox()), 'number popover runs off screen');
      await page.screenshot({ path: `${outDir}/${size.name}-popover.png` });
    }
    await page.mouse.move(0, 0);
    await page.keyboard.press('Escape');

    // Analog drawer opens, fits, and closes with Esc.
    const opener = page.locator('[data-open-analogs]').first();
    if (await opener.count()) {
      await opener.scrollIntoViewIfNeeded();
      await opener.click();
      const drawer = page.locator('#analog-drawer');
      check(await drawer.isVisible(), 'analog drawer did not open');
      check(inViewport(await drawer.boundingBox()), 'analog drawer runs off screen');
      await page.screenshot({ path: `${outDir}/${size.name}-drawer.png` });
      await page.keyboard.press('Escape');
      check(!(await drawer.isVisible()), 'analog drawer did not close on Esc');
    } else errors.push(`${tag} "See the 5 analogs" button not found`);

    // Data-mode switch.
    const cloud = page.locator('[data-mode="cloud"]');
    await cloud.scrollIntoViewIfNeeded();
    await cloud.click();
    check((await cloud.getAttribute('aria-checked')) === 'true', 'data-mode switch did not select Private cloud');
    await page.locator('[data-mode="private"]').click();

    // Keyboard: Ctrl+K focuses search.
    await page.keyboard.press('Control+k');
    check(await page.evaluate(() => document.activeElement?.id === 'search'), 'Ctrl+K did not focus search');
    await page.evaluate(() => document.activeElement?.blur());

    // Phone: the model panel collapses behind a toggle; desktop never shows the toggle.
    const toggle = page.locator('#panel-toggle');
    if (size.width <= 720) {
      await toggle.scrollIntoViewIfNeeded();
      await toggle.click();
      check((await toggle.getAttribute('aria-expanded')) === 'true', 'View model toggle did not expand');
      check(await page.locator('#tab-model').isVisible(), 'model tab not visible after View model');
      await page.screenshot({ path: `${outDir}/${size.name}-model-open.png`, fullPage: true });
    } else {
      check(!(await toggle.isVisible()), 'View model toggle is showing on desktop');
    }

    // Esc leaves the trace view.
    await open('trace');
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    check(await page.evaluate(() => location.hash === '#workspace'), 'Esc did not leave the trace view');

    // Attach files, send, hit the failed step, retry to completion.
    await open('workspace');
    await finished('run before attach');
    await page.locator('#attach-btn').click();
    check(await page.locator('#att').isVisible(), 'attachment tray did not open');
    await page.locator('#msg').fill('Retest with the interview notes');
    await page.locator('#msg').press('Enter');
    const retry = page.locator('[data-retry]');
    await retry.waitFor({ state: 'visible', timeout: 15000 }).catch(() => errors.push(`${tag} failed-step Retry never appeared`));
    if (await retry.isVisible()) {
      await page.screenshot({ path: `${outDir}/${size.name}-failed-step.png`, fullPage: true });
      await sideways('failed step');
      await retry.click();
      await finished('run after retry');
    }

    await context.close();
  }
} finally {
  await browser.close();
}

const report = [...errors, ...notes.map((n) => `note: ${n}`)];
writeFileSync(`${outDir}/errors.txt`, errors.length ? errors.join('\n') + '\n' : 'No errors.\n');
console.log(`Screenshots saved to ${outDir}`);
console.log(errors.length ? `Problems found:\n${errors.join('\n')}` : 'No errors.');
for (const n of notes) console.log(`note: ${n}`);
process.exit(errors.length ? 1 : 0);
