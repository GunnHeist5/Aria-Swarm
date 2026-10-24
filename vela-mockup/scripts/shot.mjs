// Screenshots every mockup screen at desktop and phone widths, plus the
// workspace mid-run and after the run. Prints JavaScript errors.
// Usage: npm run shot
import { chromium } from 'playwright';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
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
const errors = [];

const browser = await chromium.launch();
try {
  for (const size of sizes) {
    const context = await browser.newContext({ viewport: { width: size.width, height: size.height } });
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(`[${size.name}] ${e.message}`));
    // Resource failures are reported below by URL; webfonts are optional (offline falls back to system fonts).
    page.on('console', (m) => {
      if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(`[${size.name}] console: ${m.text()}`);
    });
    page.on('requestfailed', (r) => {
      if (!/fonts\.(googleapis|gstatic)\.com/.test(r.url())) errors.push(`[${size.name}] failed to load ${r.url()}`);
    });
    // The dark screen switcher is a review aid; hide it so it doesn't cover content in screenshots.
    const open = async (route) => {
      await page.goto('about:blank');
      await page.goto(`${url}#${route}`);
      await page.addStyleTag({ content: '.screens{display:none !important}' });
    };

    for (const route of ['home', 'trace', 'phone']) {
      await open(route);
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${outDir}/${size.name}-${route}.png`, fullPage: true });
    }

    // Workspace: a fresh load starts the sample run.
    await open('workspace');
    await page.waitForTimeout(2500);
    const head = page.locator('#step-forecaster .step-head');
    if (await head.count()) await head.click();
    await page.screenshot({ path: `${outDir}/${size.name}-workspace-running.png`, fullPage: true });

    await page
      .waitForFunction(() => document.querySelector('#run-text')?.textContent.includes('agents'), null, { timeout: 20000 })
      .catch(() => errors.push(`[${size.name}] run did not finish within 20s`));
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${outDir}/${size.name}-workspace-done.png`, fullPage: true });

    const sideways = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (sideways) errors.push(`[${size.name}] page scrolls sideways on #workspace`);

    await context.close();
  }
} finally {
  await browser.close();
}

writeFileSync(`${outDir}/errors.txt`, errors.length ? errors.join('\n') + '\n' : 'No errors.\n');
console.log(`Screenshots saved to ${outDir}`);
console.log(errors.length ? `Problems found:\n${errors.join('\n')}` : 'No errors.');
