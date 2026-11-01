"""The phone pages: plain HTML and a little JavaScript over the JSON API (API.md). No build step, no CDN."""

CSS = """
:root { --bg:#f6f6f4; --card:#fff; --fg:#1d1d1b; --muted:#6b6b66; --line:#d6d6cf; --accent:#2f6f4f; --accent-fg:#fff;
        --bad:#a33; --warn:#9a6a00; --warn-bg:#fff4d6; --bad-bg:#fde8e8; --ok-bg:#e6f2ea; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161615; --card:#1f1f1d; --fg:#ececea; --muted:#9a9a94; --line:#3a3a37; --accent:#6fbf8f; --accent-fg:#0d1f15;
          --bad:#e77; --warn:#e8b84a; --warn-bg:#3a2f12; --bad-bg:#3a1d1d; --ok-bg:#183024; }
}
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--fg); font:16px/1.45 system-ui, sans-serif; margin:0; }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:10px 16px; z-index:2;
         display:flex; gap:14px; overflow-x:auto; white-space:nowrap; }
header a { color:var(--muted); text-decoration:none; font-weight:600; }
header a.on { color:var(--fg); }
main { max-width:960px; margin:0 auto; padding:16px; }
h1 { font-size:1.35rem; margin:0 0 12px; }
h2 { font-size:1.05rem; margin:22px 0 8px; }
p.muted, .muted { color:var(--muted); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; margin-bottom:10px; }
a.card { display:block; color:inherit; text-decoration:none; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.grow { flex:1 1 auto; min-width:0; }
button, .btn { font:inherit; font-weight:600; padding:10px 14px; border-radius:8px; border:1px solid var(--line);
               background:var(--card); color:var(--fg); cursor:pointer; text-decoration:none; display:inline-block; }
button.primary, .btn.primary { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); }
button.danger { color:var(--bad); }
button:disabled { opacity:.5; }
input, select, textarea { font:inherit; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card);
                          color:var(--fg); width:100%; }
label { display:block; font-size:.85rem; color:var(--muted); margin:8px 0 3px; }
h2.suspect { color:var(--warn); }
.suspect input, input.suspect { border-color:var(--warn); background:var(--warn-bg); }
.tag { display:inline-block; font-size:.78rem; font-weight:600; padding:1px 8px; border-radius:99px; border:1px solid var(--line); }
.tag.review { background:var(--warn-bg); border-color:var(--warn); }
.tag.retake, .tag.failed { background:var(--bad-bg); border-color:var(--bad); }
.tag.auto, .tag.approved { background:var(--ok-bg); border-color:var(--accent); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:10px; }
.grid img { width:100%; aspect-ratio:1; object-fit:cover; border-radius:8px; background:var(--line); display:block; }
.split { display:grid; grid-template-columns:1fr; gap:16px; }
@media (min-width:760px) { .split { grid-template-columns:minmax(0, 5fr) minmax(0, 6fr); } .photos { position:sticky; top:60px; align-self:start; max-height:calc(100vh - 80px); overflow:auto; } }
.photos img { width:100%; border-radius:8px; margin-bottom:8px; display:block; }
table { width:100%; border-collapse:collapse; }
td, th { padding:4px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
td.num, th.num { text-align:right; }
.items input { padding:6px; }
.checks li.bad { color:var(--bad); }
.msg { margin:10px 0; min-height:1.4em; }
.msg.bad { color:var(--bad); }
.thumbs { display:flex; gap:8px; overflow-x:auto; margin:12px 0; }
.thumbs img { height:120px; border-radius:6px; }
.big { width:100%; padding:22px; font-size:1.15rem; }
.counts { display:grid; grid-template-columns:repeat(auto-fill, minmax(140px, 1fr)); gap:10px; }
.counts a { display:block; text-decoration:none; color:inherit; }
.counts b { display:block; font-size:1.6rem; }
"""

NAV = [("", "Home"), ("capture", "Capture"), ("review", "Review"), ("items", "Items"), ("products", "Products"),
       ("shopping", "Shopping"), ("reconcile", "Reconcile"), ("picker", "Google Photos")]

COMMON_JS = r"""
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = c => c == null ? '' : (c < 0 ? '-' : '') + '$' + (Math.abs(c) / 100).toFixed(2);
const toCents = s => { s = String(s ?? '').replace(/[$,\s]/g, ''); if (s === '') return null;
  const n = Number(s); return isNaN(n) ? NaN : Math.round(n * 100); };
async function api(path, opts = {}) {
  if (opts.json !== undefined) { opts.body = JSON.stringify(opts.json); opts.headers = {'Content-Type': 'application/json'}; opts.method = opts.method || 'POST'; }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}
function say(text, bad) { const m = $('#msg'); if (m) { m.textContent = text; m.className = 'msg' + (bad ? ' bad' : ''); } }
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
"""

BODIES = {}

BODIES[""] = ("Receipts", """
<h1>Receipts</h1>
<div class="counts" id="counts"></div>
<p class="muted">Photos land in Immich; the service reads them with the local model. Receipts that need a look are under Review.</p>
<script>
api('/api/summary').then(c => {
  const tiles = [['Pending', c.pending, '/review'], ['To review', c.review, '/review'], ['Retake', c.retake, '/review'],
    ['Unmatched items', c.unmatched_items, '/items'], ['Products', c.products, '/products'], ['Due to buy', c.due, '/shopping'],
    ['Receipts without bank match', c.unreconciled_receipts, '/reconcile'], ['Charges without receipt', c.unreconciled_bank, '/reconcile']];
  $('#counts').innerHTML = tiles.map(([t, n, u]) => `<a class="card" href="${u}"><b>${n}</b>${t}</a>`).join('');
}).catch(e => $('#counts').textContent = e.message);
</script>""")

BODIES["capture"] = ("Capture", """
<h1>Capture</h1>
<div class="row" style="margin-bottom:12px">
  <label style="margin:0"><input type="radio" name="kind" value="receipt" checked style="width:auto"> Receipt</label>
  <label style="margin:0"><input type="radio" name="kind" value="product" style="width:auto"> Product</label>
</div>
<input id="cam" type="file" accept="image/*" capture="environment" hidden>
<input id="pick" type="file" accept="image/*" multiple hidden>
<button class="big primary" id="shoot">Take photo</button>
<div class="thumbs" id="thumbs"></div>
<div class="row">
  <button id="more" disabled>Add another photo</button>
  <button id="choose">Choose from phone</button>
  <span class="grow"></span>
  <button id="done" class="primary" disabled>Done</button>
</div>
<label for="note">Note (optional)</label><input id="note">
<div id="msg" class="msg"></div>
<p class="muted">A long receipt: photograph it top to bottom with some overlap, tapping <b>Add another photo</b> between shots.
All photos until <b>Done</b> are one receipt.</p>
<script>
let shots = [];
const kind = () => document.querySelector('input[name=kind]:checked').value;
function draw() {
  $('#thumbs').innerHTML = shots.map((f, i) => `<img src="${URL.createObjectURL(f)}" alt="photo ${i + 1}">`).join('');
  $('#done').disabled = !shots.length; $('#more').disabled = !shots.length;
  $('#shoot').style.display = shots.length ? 'none' : '';
}
$('#shoot').onclick = $('#more').onclick = () => $('#cam').click();
$('#choose').onclick = () => $('#pick').click();
$('#cam').onchange = $('#pick').onchange = e => { shots.push(...e.target.files); e.target.value = ''; draw(); };
$('#done').onclick = async () => {
  const fd = new FormData(); shots.forEach((f, i) => fd.append('photo', f, f.name || `photo${i + 1}.jpg`));
  if ($('#note').value) fd.append('note', $('#note').value);
  $('#done').disabled = true; say('Uploading ' + shots.length + ' photo(s)…');
  try {
    const r = await api(kind() === 'receipt' ? '/api/receipts' : '/api/products', {method: 'POST', body: fd});
    say(kind() === 'receipt' ? `Receipt ${r.id} saved; it is being read now.` : `Product ${r.id} saved; it is being read now.`);
    shots = []; $('#note').value = ''; draw();
  } catch (e) { say(e.message, true); $('#done').disabled = false; }
};
</script>""")

BODIES["review"] = ("Review", """
<h1>Review</h1>
<div class="row"><select id="filter" style="width:auto">
  <option value="status=parsed">Needs a look</option><option value="status=pending">Pending</option>
  <option value="status=failed">Failed</option><option value="status=approved">Approved</option><option value="">All</option>
</select></div>
<div id="list" style="margin-top:12px"></div>
<script>
async function load() {
  const r = await api('/api/receipts?limit=100&' + $('#filter').value);
  $('#list').innerHTML = r.receipts.map(x => `<a class="card" href="/review/${x.id}"><div class="row">
    <span class="grow"><b>${esc(x.merchant || 'Receipt ' + x.id)}</b> · ${esc(x.purchase_date || '')}<br>
    <span class="muted">${x.photos} photo(s), ${x.items} items${x.suspect.length ? ' · check ' + x.suspect.join(', ') : ''}</span></span>
    <span>${money(x.total)}</span> <span class="tag ${x.bucket || x.status}">${x.bucket === 'retake' ? 'retake photo' : (x.bucket || x.status)}</span></div></a>`).join('')
    || '<p class="muted">Nothing here.</p>';
}
$('#filter').onchange = load; load().catch(e => $('#list').textContent = e.message);
</script>""")

BODIES["review/"] = ("Receipt", """
<div class="split">
  <div class="photos" id="photos"></div>
  <div>
    <div class="row"><h1 class="grow" id="title">Receipt</h1><span id="bucket"></span></div>
    <div id="msg" class="msg"></div>
    <div id="form"></div>
  </div>
</div>
<script>
const id = Number(location.pathname.split('/').pop());
let r;
const FIELDS = [['merchant', 'Store'], ['purchase_date', 'Date (YYYY-MM-DD)'], ['total', 'Total'], ['tax', 'Tax'], ['location', 'Location']];
async function load() {
  r = await api('/api/receipts/' + id);
  $('#title').textContent = r.merchant || 'Receipt ' + id;
  $('#bucket').innerHTML = `<span class="tag ${r.bucket || r.status}">${r.status === 'approved' ? 'approved' : (r.bucket || r.status)}</span>`;
  $('#photos').innerHTML = r.images.map(i => `<a href="${i.url}" target="_blank"><img src="${i.url}" alt="photo ${i.position}"></a>`).join('')
    + (r.order_text ? `<pre class="card" style="white-space:pre-wrap">${esc(r.order_text)}</pre>` : '');
  const sus = new Set(r.suspect);
  const val = (f, v) => (f === 'total' || f === 'tax') ? (v == null ? '' : (v / 100).toFixed(2)) : (v ?? '');
  const dis = r.disagreements || {};
  $('#form').innerHTML = (r.error ? `<p class="msg bad">${esc(r.error)}</p>` : '')
    + (r.bucket === 'retake' ? '<p class="msg bad">The total or date couldn\\'t be read: add a new photo below, or type them in.</p>' : '')
    + FIELDS.map(([f, l]) => `<div class="${sus.has(f) ? 'suspect' : ''}"><label for="f_${f}">${l}${dis[f] ? ' · engines read: '
      + esc(Object.entries(dis[f]).map(([e, v]) => e + ' ' + ((f === 'total' || f === 'tax') ? money(v) : v)).join(', ')) : ''}</label>
      <input id="f_${f}" value="${esc(val(f, r[f]))}" ${f === 'total' || f === 'tax' ? 'inputmode="decimal"' : ''}></div>`).join('')
    + `<h2 class="${sus.has('items') ? 'suspect' : ''}">Items</h2>
      <table class="items"><thead><tr><th>Text</th><th class="num">Qty</th><th class="num">Price</th><th></th></tr></thead>
      <tbody id="items"></tbody></table>
      <div class="row" style="margin-top:6px"><button id="addrow">Add item</button><span class="grow muted" id="sum"></span></div>
      <h2>Checks</h2><ul class="checks">${(r.checks || []).map(c => `<li class="${c.ok ? '' : 'bad'}">${c.ok ? '✓' : '✗'} ${esc(c.name)}: ${esc(c.detail)}</li>`).join('')}</ul>
      <div class="row" style="margin-top:14px"><button class="primary" id="approve">Approve</button><button id="save">Save</button>
        <button id="reparse">Read again</button></div>
      <h2>More</h2>
      <div class="row"><input type="file" id="more" accept="image/*" capture="environment" hidden><button id="addphoto">Add a photo (retake)</button>
        <input id="into" placeholder="receipt #" inputmode="numeric" style="width:110px"><button id="merge">Merge into</button>
        <button class="danger" id="del">Delete</button></div>
      <p class="muted">${r.runs.map(x => esc(x.engine) + ' ' + (x.seconds ?? '?') + ' s' + (x.error ? ' (' + esc(x.error) + ')' : '')).join(' · ')}</p>`;
  $('#items').innerHTML = r.items.map(rowHtml).join('');
  bind(); sum();
}
function rowHtml(i) {
  const m = i.product_name ? `<br><span class="muted">→ ${esc(i.product_name)}</span>` : (i.match === 'skip' ? '<br><span class="muted">not a product</span>' : '');
  return `<tr><td><input class="t" value="${esc(i.raw_text)}">${m}</td><td class="num"><input class="q" value="${i.quantity ?? ''}" inputmode="decimal" style="width:60px"></td>
    <td class="num"><input class="p" value="${i.line_total == null ? '' : (i.line_total / 100).toFixed(2)}" inputmode="decimal" style="width:90px"></td>
    <td><button class="x" title="Remove">✕</button></td></tr>`;
}
function items() {
  return [...document.querySelectorAll('#items tr')].map(tr => ({raw_text: tr.querySelector('.t').value,
    quantity: tr.querySelector('.q').value === '' ? null : Number(tr.querySelector('.q').value), line_total: toCents(tr.querySelector('.p').value)}))
    .filter(i => i.raw_text.trim() || i.line_total != null);
}
function sum() {
  const its = items(), t = toCents($('#f_total').value), x = toCents($('#f_tax').value) || 0;
  const s = its.reduce((a, i) => a + (i.line_total || 0), 0) + x;
  $('#sum').textContent = `items + tax = ${money(s)}` + (t != null && !isNaN(t) ? (Math.abs(s - t) <= 2 ? ' ✓ matches the total' : ` · total ${money(t)}, off by ${money(s - t)}`) : '');
}
function bind() {
  document.querySelectorAll('#items .x').forEach(b => b.onclick = () => { b.closest('tr').remove(); sum(); });
  document.querySelectorAll('input').forEach(i => i.oninput = sum);
  $('#addrow').onclick = () => { $('#items').insertAdjacentHTML('beforeend', rowHtml({raw_text: '', quantity: null, line_total: null})); bind(); };
  $('#save').onclick = () => send(false);
  $('#approve').onclick = () => send(true);
  $('#reparse').onclick = async () => { await api(`/api/receipts/${id}/reparse`, {json: {}}); say('Reading again… reload in a minute.'); };
  $('#addphoto').onclick = () => $('#more').click();
  $('#more').onchange = async e => { const fd = new FormData(); fd.append('photo', e.target.files[0]);
    try { await api(`/api/receipts/${id}/photos`, {method: 'POST', body: fd}); say('Photo added; reading again… reload in a minute.'); } catch (x) { say(x.message, true); } };
  $('#merge').onclick = async () => { const into = Number($('#into').value); if (!into) return say('Type the receipt number to merge into.', true);
    try { await api(`/api/receipts/${id}/merge`, {json: {into}}); location.href = '/review/' + into; } catch (x) { say(x.message, true); } };
  $('#del').onclick = async () => { if (!confirm('Delete this receipt? The photos stay in Immich.')) return;
    await api('/api/receipts/' + id, {method: 'DELETE'}); location.href = '/review'; };
}
async function send(approve) {
  const body = {items: items(), approve};
  for (const [f] of FIELDS) { const v = $('#f_' + f).value.trim(); body[f] = (f === 'total' || f === 'tax') ? toCents(v) : (v || null); }
  if (Number.isNaN(body.total) || Number.isNaN(body.tax) || body.items.some(i => Number.isNaN(i.line_total))) return say('A price isn\\'t a number.', true);
  try {
    await api('/api/receipts/' + id, {json: body});
    if (approve) { const next = (await api('/api/receipts?status=parsed&limit=1')).receipts[0];
      if (next) location.href = '/review/' + next.id; else location.href = '/review'; }
    else { await load(); say('Saved.'); }
  } catch (e) { say(e.message, true); }
}
load().catch(e => say(e.message, true));
</script>""")

BODIES["items"] = ("Items", """
<h1>Unmatched items</h1>
<p class="muted">Link receipt lines to catalog products. Once linked, the same text matches by itself next time.</p>
<div id="msg" class="msg"></div>
<div id="list"></div>
<script>
let products = [];
async function load() {
  products = (await api('/api/products')).products;
  const r = await api('/api/items/unmatched?limit=100');
  const opts = '<option value="">Pick a product…</option>' + products.map(p => `<option value="${p.id}">${esc([p.brand, p.name, p.size].filter(Boolean).join(' '))}</option>`).join('');
  $('#list').innerHTML = r.items.map(i => `<div class="card" data-id="${i.id}"><div class="row"><b class="grow">${esc(i.raw_text)}</b><span>${money(i.line_total)}</span></div>
    <div class="muted">${esc(i.merchant || '')} · ${esc(i.purchase_date || '')} · <a href="/review/${i.receipt_id}">receipt ${i.receipt_id}</a></div>
    <div class="row" style="margin-top:8px">
      ${i.suggestion ? `<button class="primary yes" data-p="${i.suggestion.product_id}">${esc(i.suggestion.name)} (${i.suggestion.by})</button>` : ''}
      <select class="pick grow" style="width:auto">${opts}</select>
      <button class="new">New product</button><button class="skip">Not a product</button></div></div>`).join('')
    || '<p class="muted">Every item is matched.</p>';
  document.querySelectorAll('.card[data-id]').forEach(c => {
    const id = c.dataset.id, go = async body => { try { const res = await api(`/api/items/${id}/match`, {json: body});
      say(`Matched ${res.matched} item(s).`); load(); } catch (e) { say(e.message, true); } };
    c.querySelector('.yes')?.addEventListener('click', e => go({product_id: Number(e.target.dataset.p)}));
    c.querySelector('.pick').onchange = e => e.target.value && go({product_id: Number(e.target.value)});
    c.querySelector('.skip').onclick = () => go({skip: true});
    c.querySelector('.new').onclick = () => { const name = prompt('Product name', c.querySelector('b').textContent); if (name) go({new_product: {name}}); };
  });
}
load().catch(e => say(e.message, true));
</script>""")

BODIES["products"] = ("Products", """
<div class="row"><h1 class="grow">Products</h1><a class="btn" href="/capture">Add product photo</a></div>
<input id="q" placeholder="Search" style="margin-bottom:12px">
<div class="grid" id="grid"></div>
<script>
let t;
async function load() {
  const r = await api('/api/products?q=' + encodeURIComponent($('#q').value));
  $('#grid').innerHTML = r.products.map(p => `<a href="/products/${p.id}" style="color:inherit;text-decoration:none">
    ${p.image_url ? `<img src="${p.image_url}" alt="" loading="lazy">` : '<img alt="">'}
    <b>${esc(p.name || (p.status === 'pending' ? 'Reading…' : 'Unnamed'))}</b><br><span class="muted">${esc([p.brand, p.size].filter(Boolean).join(' · '))}
    ${p.purchases ? `<br>${p.purchases}× · ${money(p.last_price)} at ${esc(p.last_store || '?')}` : ''}</span></a>`).join('') || '<p class="muted">No products yet.</p>';
}
$('#q').oninput = () => { clearTimeout(t); t = setTimeout(load, 250); };
load().catch(e => $('#grid').textContent = e.message);
</script>""")

BODIES["products/"] = ("Product", """
<div class="split"><div class="photos" id="photos"></div><div>
<h1 id="title">Product</h1><div id="msg" class="msg"></div>
<label>Name</label><input id="name"><label>Brand</label><input id="brand"><label>Size</label><input id="size">
<div class="row" style="margin-top:10px"><button class="primary" id="save">Save</button>
<input id="into" placeholder="product #" inputmode="numeric" style="width:110px"><button id="merge">Merge into</button></div>
<h2>Receipt text that means this product</h2><ul id="aliases"></ul>
<h2>Purchases</h2><table><thead><tr><th>Date</th><th>Store</th><th class="num">Price</th></tr></thead><tbody id="hist"></tbody></table>
</div></div>
<script>
const id = Number(location.pathname.split('/').pop());
async function load() {
  const p = await api('/api/products/' + id);
  $('#title').textContent = p.name || 'Product ' + id;
  ['name', 'brand', 'size'].forEach(f => $('#' + f).value = p[f] || '');
  $('#photos').innerHTML = p.images.map(i => `<img src="${i.url}" alt="">`).join('');
  $('#aliases').innerHTML = p.aliases.map(a => `<li>${esc(a)}</li>`).join('') || '<li class="muted">none yet</li>';
  $('#hist').innerHTML = p.history.map(h => `<tr><td><a href="/review/${h.receipt_id}">${esc(h.date)}</a></td><td>${esc(h.merchant || '')}${h.location ? ' · ' + esc(h.location) : ''}</td>
    <td class="num">${money(h.line_total)}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">No purchases yet.</td></tr>';
}
$('#save').onclick = async () => { try { await api('/api/products/' + id, {json: {name: $('#name').value, brand: $('#brand').value, size: $('#size').value}}); say('Saved.'); load(); } catch (e) { say(e.message, true); } };
$('#merge').onclick = async () => { const into = Number($('#into').value); if (!into) return;
  try { await api(`/api/products/${id}/merge`, {json: {into}}); location.href = '/products/' + into; } catch (e) { say(e.message, true); } };
load().catch(e => say(e.message, true));
</script>""")

BODIES["shopping"] = ("Shopping", """
<h1>Shopping list</h1>
<p class="muted" id="rule"></p>
<div id="list"></div>
<script>
api('/api/shopping-list').then(r => {
  $('#rule').textContent = 'Rule: ' + r.rule + '.';
  $('#list').innerHTML = r.items.map(i => `<div class="card"><div class="row"><b class="grow"><a href="/products/${i.product_id}">${esc([i.brand, i.name, i.size].filter(Boolean).join(' '))}</a></b>
    <span>${money(i.last_price)}</span></div><div class="muted">every ~${i.every_days} days · last ${esc(i.last_bought)} (${i.overdue_days >= 0 ? i.overdue_days + ' days overdue' : 'due soon'})
    · usually ${esc(i.usual_store || '?')}${i.best_store ? ` · best ${money(i.best_price)} at ${esc(i.best_store)}` : ''}</div></div>`).join('')
    || '<p class="muted">Nothing due. Products need two or more purchases on approved receipts.</p>';
}).catch(e => $('#list').textContent = e.message);
</script>""")

BODIES["reconcile"] = ("Reconcile", """
<h1>Reconcile</h1>
<div id="msg" class="msg"></div>
<div class="row"><input type="file" id="csv" accept=".csv,text/csv" hidden><button id="import">Import bank CSV</button>
<select id="account" style="width:auto"><option>checking</option><option>bills</option><option>savings</option><option>credit</option></select>
<button id="sync">Sync from budget</button><button class="primary" id="run">Match</button></div>
<h2>Online order (Instacart)</h2>
<textarea id="order" rows="4" placeholder="Paste the order confirmation email text"></textarea>
<div class="row" style="margin-top:6px"><button id="addorder">Add order</button></div>
<div class="split" style="margin-top:10px"><div><h2>Receipts with no bank charge</h2><div id="receipts"></div></div>
<div><h2>Charges with no receipt</h2><div id="bank"></div></div></div>
<script>
let sel = null;
async function load() {
  const r = await api('/api/reconcile');
  $('#receipts').innerHTML = r.receipts_unmatched.map(x => `<div class="card pickable" data-id="${x.id}"><div class="row"><span class="grow">
    <a href="/review/${x.id}">${esc(x.merchant || 'Receipt ' + x.id)}</a> · ${esc(x.purchase_date || '')}${x.source !== 'in-store' ? ' · ' + esc(x.source) : ''}</span><b>${money(x.total)}</b></div></div>`).join('')
    || '<p class="muted">None.</p>';
  $('#bank').innerHTML = r.bank_unmatched.map(b => `<div class="card"><div class="row"><span class="grow">${esc(b.date)} · ${esc(b.description)}
    <span class="muted">(${esc(b.account || '')})</span></span><b>${money(-b.amount)}</b><button class="link" data-b="${b.id}">Match selected</button></div></div>`).join('')
    || '<p class="muted">None.</p>';
  document.querySelectorAll('.pickable').forEach(c => c.onclick = e => { if (e.target.tagName === 'A') return;
    document.querySelectorAll('.pickable').forEach(x => x.style.outline = ''); c.style.outline = '2px solid var(--accent)'; sel = Number(c.dataset.id); });
  document.querySelectorAll('.link').forEach(b => b.onclick = async () => { if (!sel) return say('Tap a receipt first.', true);
    await api('/api/reconcile/match', {json: {receipt_id: sel, bank_id: Number(b.dataset.b)}}); sel = null; say('Matched.'); load(); });
}
$('#import').onclick = () => $('#csv').click();
$('#csv').onchange = async e => { const fd = new FormData(); fd.append('file', e.target.files[0]); fd.append('account', $('#account').value);
  try { const r = await api('/api/bank/import', {method: 'POST', body: fd}); say(`${r.new} new of ${r.rows} rows.`); load(); } catch (x) { say(x.message, true); } e.target.value = ''; };
$('#sync').onclick = async () => { try { const r = await api('/api/bank/sync-budget', {json: {}}); say(`${r.new} new of ${r.rows} transactions.`); load(); } catch (x) { say(x.message, true); } };
$('#run').onclick = async () => { const r = await api('/api/reconcile/run', {json: {}}); say(`${r.matched} matched.`); load(); };
$('#addorder').onclick = async () => { try { const r = await api('/api/orders', {json: {text: $('#order').value, source: 'instacart'}});
  $('#order').value = ''; say(`Order saved as receipt ${r.id}; it is being read, then appears under Review.`); } catch (x) { say(x.message, true); } };
load().catch(e => say(e.message, true));
</script>""")

BODIES["picker"] = ("Google Photos", """
<h1>Import from Google Photos</h1>
<div id="msg" class="msg"></div>
<div id="body"></div>
<script>
async function load() {
  const s = await api('/api/picker/status');
  if (!s.configured) { $('#body').innerHTML = '<p class="muted">Not set up yet: the service needs a Google Cloud OAuth client (see the receipts README, Google Photos Picker).</p>'; return; }
  if (!s.signed_in) { $('#body').innerHTML = `<a class="btn primary" href="${s.auth_url}">Sign in to Google</a>`; return; }
  $('#body').innerHTML = `<p class="muted">Pick photos in Google Photos; they are copied into Immich. Each receipt photo becomes its own receipt.</p>
    <div class="row"><button class="primary" data-a="receipts">Pick receipts</button><button data-a="products">Pick products</button></div>`;
  document.querySelectorAll('[data-a]').forEach(b => b.onclick = () => start(b.dataset.a));
}
async function start(album) {
  try {
    const s = await api('/api/picker/sessions', {json: {album}});
    // A link, not window.open: phones block pop-ups opened after a network wait.
    $('#body').insertAdjacentHTML('beforeend', `<p><a class="btn primary" target="_blank" href="${esc(s.picker_uri)}/autoclose">Open Google Photos</a></p>`);
    say('Choose photos in Google Photos, then tap Done there. This page shows the import.');
    const t = setInterval(async () => { const st = await api('/api/picker/sessions/' + s.id);
      if (st.state === 'importing') say(`Importing… ${st.imported}`);
      if (st.state === 'done') { clearInterval(t); say(`Imported ${st.imported} photo(s).`); }
      if (st.state === 'error') { clearInterval(t); say(st.error, true); } }, 3000);
  } catch (e) { say(e.message, true); }
}
load().catch(e => say(e.message, true));
</script>""")


def render(path):
    key = path.split("/")[0] + "/" if "/" in path else path
    title, body = BODIES[key]
    section = path.split("/")[0]
    nav = "".join(f'<a href="/{p}" class="{"on" if p == section else ""}">{label}</a>' for p, label in NAV)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} · Receipts</title><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg">'
            f'<meta name="theme-color" content="#2f6f4f"><style>{CSS}</style></head>'
            f'<body><header>{nav}</header><main><script>{COMMON_JS}</script>{body}</main></body></html>')


MANIFEST = """{"name": "Receipts", "short_name": "Receipts", "start_url": "/capture", "display": "standalone",
 "background_color": "#f6f6f4", "theme_color": "#2f6f4f",
 "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}"""

# Installable as an app; always goes to the network (an upload must reach the workstation).
SERVICE_WORKER = "self.addEventListener('fetch', () => {});"

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#2f6f4f"/>
<path d="M20 12h24v40l-4-3-4 3-4-3-4 3-4-3-4 3z" fill="#fff"/><path d="M25 22h14M25 29h14M25 36h9" stroke="#2f6f4f" stroke-width="3"/></svg>"""
