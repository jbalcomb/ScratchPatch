// Measures every [data-ui] element of the dashboard in Chromium at 1280 x 800 and writes
// layout.json plus an annotated screenshot (layout-live.png) to the given folder.
// Usage: node tools/measure-layout.cjs [out-dir]   (needs the playwright package)
const { chromium } = require('playwright');
const http = require('http');
const fs = require('fs');
const path = require('path');
const EXT = path.resolve(__dirname, '..');
const OUT = path.resolve(process.argv[2] || 'layout-out');
fs.mkdirSync(OUT, { recursive: true });
const para = (t, n) => Array.from({ length: n }, (_, i) => `<p>${t} sentence number ${i} explains more about ${t} and why ${t} matters for readers today.</p>`).join('');
const pages = {
  '/solar': `<html lang="en"><head><title>Solar power basics</title><meta name="description" content="A primer on solar panels."></head><body><article><h1>Solar power</h1>${para('solar energy', 60)}</article><input id="box"></body></html>`,
  '/pasta': `<html><head><title>Cooking pasta</title></head><body><main><h1>Pasta</h1>${para('fresh pasta', 40)}</main></body></html>`,
};
const server = http.createServer((req, res) => { res.setHeader('content-type', 'text/html'); res.end(pages[req.url] || 'nope'); });

async function measure(page) {
  return page.evaluate(() => {
    const col = document.body.getBoundingClientRect();
    const cs = getComputedStyle(document.body);
    const ox = col.left + parseFloat(cs.paddingLeft);
    const seen = new Map();
    for (const el of document.querySelectorAll('[data-ui]')) {
      const id = el.dataset.ui;
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) continue;
      if (seen.has(id)) { seen.get(id).count++; continue; }
      seen.set(id, { id, x: Math.round(r.left - ox), y: Math.round(r.top + scrollY), w: Math.round(r.width), h: Math.round(r.height), count: 1,
        text: (el.innerText || el.placeholder || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 50) });
    }
    return { columnLeft: Math.round(ox), columnWidth: Math.round(col.width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)), pageHeight: document.documentElement.scrollHeight, items: [...seen.values()] };
  });
}

async function annotate(page, ids) {
  await page.evaluate((ids) => {
    let n = 0;
    for (const id of ids) {
      const el = document.querySelector(`[data-ui="${id}"]`);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const region = !id.includes('.');
      const box = document.createElement('div');
      Object.assign(box.style, { position: 'absolute', left: r.left + scrollX + 'px', top: r.top + scrollY + 'px', width: r.width + 'px', height: r.height + 'px',
        outline: region ? '2px dashed #c026d3' : '1px solid #dc2626', pointerEvents: 'none', zIndex: 9999 });
      const tag = document.createElement('div');
      tag.textContent = region ? id : String(++n);
      Object.assign(tag.style, { position: 'absolute', left: (region ? r.left - 40 : r.left - 5) + scrollX + 'px', top: (region ? r.top : r.top - 7) + scrollY + 'px',
        font: '700 9px/11px system-ui', color: '#fff', background: region ? '#c026d3' : '#dc2626', padding: '0 3px', borderRadius: '6px', zIndex: 10000, whiteSpace: 'nowrap' });
      document.body.append(box, tag);
    }
  }, ids);
}

(async () => {
  await new Promise((r) => server.listen(8766, r));
  const base = 'http://localhost:8766';
  const ctx = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(require('os').tmpdir(), 'tab-ledger-')), {
    headless: true, channel: 'chromium', viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2,
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
  });
  let [sw] = ctx.serviceWorkers(); if (!sw) sw = await ctx.waitForEvent('serviceworker');
  const extId = sw.url().split('/')[2];
  const p1 = ctx.pages()[0]; await p1.goto(base + '/solar');
  const p2 = await ctx.newPage(); await p2.goto(base + '/pasta');
  await p1.bringToFront(); await p1.waitForTimeout(800);
  await p1.mouse.move(300, 300); await p1.mouse.wheel(0, 1500); await p1.waitForTimeout(300);
  await p1.evaluate(() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('p')); getSelection().removeAllRanges(); getSelection().addRange(r); });
  await p1.keyboard.press('Control+C'); await p1.click('#box'); await p1.keyboard.press('Control+V');
  await p1.waitForTimeout(1500);

  const dash = await ctx.newPage();
  await dash.goto(`chrome-extension://${extId}/dashboard.html`);
  await dash.waitForSelector('.tab'); await dash.waitForTimeout(1200);
  await dash.click('#save'); await dash.waitForTimeout(500);
  await dash.evaluate(() => { document.querySelector('#status').textContent = 'Snapshot saved.'; });
  const live = await measure(dash);
  const ids = live.items.map((i) => i.id);
  await annotate(dash, ids);
  await dash.screenshot({ path: path.join(OUT, 'layout-live.png'), fullPage: true });

  await dash.reload(); await dash.waitForSelector('.tab'); await dash.waitForTimeout(1000);
  await dash.click('[data-ui="R8.view"]'); await dash.waitForTimeout(400);
  const saved = await measure(dash);
  fs.writeFileSync(path.join(OUT, 'layout.json'), JSON.stringify({ live, saved }, null, 1));
  await ctx.close(); server.close();
})().catch((e) => { console.error(e); process.exit(1); });
