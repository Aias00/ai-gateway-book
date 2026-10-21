/* Check the built reader or a deployed BOOK_URL; does not assess book content. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
  const base = (process.env.BOOK_URL || 'http://127.0.0.1:8767/').replace(/\/?$/, '/');
  try {
    const context = await browser.newContext({ colorScheme: 'dark', viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const response = await page.goto(base);
    assert.equal(response.status(), 200);
    const info = await (await page.request.get(base + 'build-info.json')).json();
    const index = await (await page.request.get(base + 'search-index.json')).json();
    const first = info.chapters[0];
    const second = info.chapters[1] || first;
    await page.locator('#theme:not([hidden])').waitFor();
    assert.equal(await page.locator('body').evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(247, 247, 243)');
    assert.equal(await page.locator('.chapter-list li').count(), info.chapter_count);
    await page.locator('.primary').click();
    await page.waitForURL('**/' + first.slug + '.html');
    assert.equal((await page.locator('article h1').innerText()).trim(), first.title);
    assert((await page.locator('article').innerText()).length > 50, 'Static chapter body missing');
    await page.locator('#larger').click();
    assert.equal(await page.locator('article').evaluate(element => getComputedStyle(element).fontSize), '19px');
    await page.locator('#theme').click();
    assert.equal(await page.locator('body').evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(28, 31, 36)');
    assert.equal(await page.evaluate(() => localStorage.getItem('aias-reader-theme')), 'dark');
    await page.reload();
    assert.equal(await page.locator('article').evaluate(element => getComputedStyle(element).fontSize), '19px');
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    const other = await context.newPage();
    await other.goto(base + 'chapters/' + second.slug + '.html');
    assert.equal(await other.locator('html').getAttribute('data-theme'), 'dark');
    await other.locator('#theme').click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'light');
    await other.close();
    await page.locator('#search').fill(index[Math.floor(index.length / 2)].title);
    await page.locator('#search-form button').click();
    await page.waitForFunction(() => document.querySelector('#search-status').textContent.startsWith('找到'));
    assert(await page.locator('#search-results a').count() > 0);
    await page.locator('#search').fill('no-such-book-search-term-4e7c');
    await page.locator('#search-form button').click();
    await page.waitForFunction(() => document.querySelector('#search-status').textContent === '找到 0 篇相关章节');
    const screenshots = process.env.BOOK_SCREENSHOTS || path.join(os.tmpdir(), info.download_slug + '-reader');
    fs.mkdirSync(screenshots, { recursive: true });
    await page.screenshot({ path: path.join(screenshots, 'desktop-light.png'), fullPage: true });
    const widths = [];
    for (const width of [320, 390]) {
      await page.setViewportSize({ width, height: 844 });
      for (const route of ['', ...info.chapters.map(chapter => 'chapters/' + chapter.slug + '.html')]) {
        await page.goto(base + route);
        const actual = await page.evaluate(() => document.documentElement.scrollWidth);
        assert(actual <= width, `${width}px overflow at ${route}: ${actual}`);
      }
      await page.goto(base + 'chapters/' + first.slug + '.html');
      const details = page.locator('#contents');
      assert.equal(await details.getAttribute('open'), null);
      await page.locator('#contents > summary').click();
      assert.notEqual(await details.getAttribute('open'), null);
      await page.locator('#sidebar nav a').nth(Math.min(1, info.chapter_count - 1)).click();
      await page.waitForURL('**/' + second.slug + '.html');
      assert.equal(await details.getAttribute('open'), null);
      await page.locator('#theme').click();
      await page.screenshot({ path: path.join(screenshots, `mobile-${width}-dark.png`), fullPage: true });
      await page.locator('#theme').click();
      widths.push({ width, chapters: info.chapter_count, overflow: false });
    }
    const native = await browser.newContext({ javaScriptEnabled: false, colorScheme: 'dark', viewport: { width: 320, height: 844 } });
    const plain = await native.newPage();
    await plain.goto(base + 'chapters/' + first.slug + '.html');
    assert.equal(await plain.locator('body').evaluate(element => getComputedStyle(element).backgroundColor), 'rgb(247, 247, 243)');
    assert.equal((await plain.locator('article h1').innerText()).trim(), first.title);
    assert(await plain.locator('article').innerText(), 'No-JS article empty');
    if (info.chapter_count > 1) {
      await plain.locator('.pager a[rel="next"]').click();
      await plain.waitForURL('**/' + second.slug + '.html');
    }
    assert.equal(await plain.evaluate(() => document.documentElement.scrollWidth), 320);
    await native.close();
    const blocked = await browser.newContext();
    await blocked.addInitScript(() => Object.defineProperty(window, 'localStorage', { get() { throw new Error('Storage disabled'); } }));
    const denied = await blocked.newPage();
    await denied.goto(base + 'chapters/' + first.slug + '.html');
    await denied.locator('#theme').click();
    assert.equal(await denied.locator('html').getAttribute('data-theme'), 'dark');
    await blocked.close();
    for (const file of [info.download_slug + '.epub', info.download_slug + '.html', 'book.md', 'lab.zip']) {
      const download = await page.request.get(base + 'downloads/' + file);
      assert(download.ok(), file);
      assert((await download.body()).length > 0, file);
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: 'PASS', url: base, chapters: info.chapter_count,
      checks: ['light default under dark OS', 'EPUB links', 'static chapters', 'search', 'font persistence',
        'shared theme key and tab sync', 'mobile native contents menu', 'all chapters at 320/390px',
        'no-JS reading and pagination', 'storage denied', 'downloads', 'no JS errors'], widths, screenshots }));
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
